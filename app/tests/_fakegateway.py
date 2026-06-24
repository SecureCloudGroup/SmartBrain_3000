"""A REAL local HTTP server speaking Bifrost's OpenAI-compatible API.

This exists to kill the dangerous test pattern of monkeypatching our OWN gateway
functions (gateway.chat / chat_stream / embed / list_models), which let two
production-breaking bugs ship past a green suite: every web fetch was broken
(httpx.Response is not a context manager) and streaming chat never sent tools (so
the model narrated actions it never performed). Both were invisible because the
real code path was replaced by a lambda.

Here the app's REAL gateway code (real httpx, real streaming, real payload
construction) talks to this server over a real loopback socket. Point
SMARTBRAIN_LLM_GATEWAY_URL at ``server.url`` and you exercise the true wire path.
The server records every request body so a test can assert what we actually sent
(e.g. that the ``tools`` field is present on a streaming request).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGateway:
    """Scriptable, request-recording Bifrost stand-in (real socket, real HTTP)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []  # each: {method, path, body}
        self.reply_text = "ok"          # assistant content for chat
        self.reply_tool_calls: list | None = None  # if set, chat returns a tool call
        # embedding: if set, return it verbatim (passthrough tests); if None, derive a
        # deterministic INPUT-DEPENDENT vector so KB cosine RANKING is actually exercised
        # (a constant vector makes every doc equidistant — ranking would be untestable).
        self.embedding: list[float] | None = None
        self.models = [{"id": "gemini/gemini-2.5-flash", "object": "model"}]
        self.fail_status: int | None = None  # if set, every call returns this status
        self.fail_body = {"error": {"message": "forced failure"}}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "server not started"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def last(self, path: str) -> dict:
        """The most recent recorded request body for ``path`` (asserts one exists)."""
        for rec in reversed(self.requests):
            if rec["path"] == path:
                return rec["body"]
        raise AssertionError(f"no request recorded for {path}")

    def start(self) -> FakeGateway:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        assert self._server is not None, "server not started"
        self._server.shutdown()
        self._server.server_close()


# A tiny fixed vocabulary; the embedding is the per-word count, so texts that share
# words get a high cosine and unrelated texts get a low one — enough to test ranking.
_VOCAB = ("lease", "rent", "renew", "dentist", "dog", "car", "invoice", "tomorrow")


def _embed_text(text: str) -> list[float]:
    """Deterministic bag-of-words embedding over _VOCAB (+ a constant dim so norm > 0)."""
    low = text.lower()
    return [float(low.count(w)) for w in _VOCAB] + [0.01]


def _chat_chunks(text: str, tool_calls: list | None) -> list[str]:
    """OpenAI-style SSE lines for a streamed chat completion."""
    lines: list[str] = []
    if tool_calls is not None:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": tool_calls}}]}))
    else:
        for ch in text:  # one delta per character — proves real incremental streaming
            lines.append("data: " + json.dumps({"choices": [{"delta": {"content": ch}}]}))
    lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    lines.append("data: [DONE]")
    return lines


def _chat_json(text: str, tool_calls: list | None) -> dict:
    """OpenAI-style non-streaming chat completion."""
    message: dict = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}]}


def _make_handler(api: FakeGateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # keep test output clean
            pass

        def _body(self) -> dict:
            length = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                return {}

        def _json(self, status: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _sse(self, lines: list[str]) -> None:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for line in lines:
                self.wfile.write((line + "\n\n").encode())
                self.wfile.flush()

        def do_POST(self) -> None:
            body = self._body()
            api.requests.append({"method": "POST", "path": self.path, "body": body})
            if api.fail_status is not None:
                return self._json(api.fail_status, api.fail_body)
            if self.path == "/v1/chat/completions":
                if body.get("stream"):
                    return self._sse(_chat_chunks(api.reply_text, api.reply_tool_calls))
                return self._json(200, _chat_json(api.reply_text, api.reply_tool_calls))
            if self.path == "/v1/embeddings":
                vec = api.embedding if api.embedding is not None else _embed_text(str(body.get("input", "")))
                return self._json(200, {"data": [{"embedding": vec}]})
            return self._json(200, {"ok": True})  # provider admin etc.

        def do_GET(self) -> None:
            api.requests.append({"method": "GET", "path": self.path, "body": {}})
            if self.path == "/v1/models":
                return self._json(200, {"data": api.models})
            return self._json(200, {"ok": True})

    return Handler
