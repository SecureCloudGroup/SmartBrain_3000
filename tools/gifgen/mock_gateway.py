#!/usr/bin/env python3
"""Tiny mock OpenAI-compatible gateway for deterministic, synthetic GIF demos.

Stands in for Bifrost (SMARTBRAIN_LLM_GATEWAY_URL) AND a host Ollama (port 11434),
so the real app renders canned chat/tool-call/model-list responses in full
isolation — no real provider keys, no touching a real Bifrost. See README.md.

Endpoints:
  GET  /reset                      -> clear configured providers (use before clip 02)
  GET  /v1/models                  -> catalog for the currently "configured" providers
  POST /v1/chat/completions        -> stream (SSE) or JSON; emits add_task/kb_search tool calls
  POST /v1/embeddings              -> a fixed vector (reindex + semantic search work)
  POST /api/providers              -> mark a provider configured (register_ollama/set_provider)
  POST /api/providers/<n>/keys     -> 200
  DELETE /api/providers/<n>        -> unconfigure
  GET  /api/tags  (port 11434)     -> Ollama detection ("Found Ollama running")

Run:  python3 mock_gateway.py 38099   (also binds :11434 for Ollama detection)
"""
import json
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"providers": set()}

OLLAMA_MODEL = {"id": "ollama/llama3.1:8b", "name": "Llama 3.1 8B", "supported_methods": ["chat"]}
EMBED_MODEL = {"id": "ollama/nomic-embed-text:v1.5", "name": "nomic-embed-text:v1.5", "supported_methods": ["embed"]}
ANTHROPIC_MODEL = {"id": "anthropic/claude-3-5-sonnet-latest", "name": "Claude 3.5 Sonnet", "supported_methods": ["chat"]}


def models_payload():
    data = []
    if "ollama" in STATE["providers"]:
        data += [OLLAMA_MODEL, EMBED_MODEL]
    if "anthropic" in STATE["providers"]:
        data.append(ANTHROPIC_MODEL)
    return {"object": "list", "data": data}


def _tomorrow():
    return (date.today() + timedelta(days=1)).isoformat()


def decide(messages):
    """Return {'text': ...} or {'tool': {'name','arguments'}} from the conversation."""
    last_user, has_tool_result, last_tool = "", False, ""
    for m in messages:
        if m.get("role") == "user":
            last_user = m.get("content") or ""
        if m.get("role") == "tool":
            has_tool_result = True
        if m.get("role") == "assistant" and m.get("tool_calls"):
            last_tool = (m["tool_calls"][-1].get("function") or {}).get("name", "")
    lu = last_user.lower()
    if has_tool_result:
        if last_tool == "kb_search" or "lease" in json.dumps(messages).lower():
            return {"text": "Your apartment lease runs 12 months at $1,800/month, due on the 1st, "
                            "with 60 days' notice to vacate (landlord: Pat Rivera)."}
        if last_tool == "add_task":
            return {"text": "Done — I’ve added that to your planner."}
        return {"text": "All set."}
    if "milk" in lu or "dentist" in lu or ("add" in lu and "task" in lu):
        title = "Buy milk" if "milk" in lu else ("Call the dentist" if "dentist" in lu else "New task")
        args = {"title": title}
        if "dentist" in lu or "tomorrow" in lu:
            args["due_date"] = _tomorrow()
        return {"tool": {"name": "add_task", "arguments": args}}
    if "lease" in lu or "knowledge" in lu:
        return {"tool": {"name": "kb_search", "arguments": {"query": "apartment lease terms"}}}
    if "help" in lu or "what can you do" in lu:
        return {"text": "I can chat, search your private Knowledge, track tasks in the Planner, and run "
                        "scheduled briefings. Anything that changes data or reaches out waits for your approval first."}
    if "summar" in lu and "task" in lu:
        return {"text": "You have 3 open tasks: Call the dentist (today), Submit expense report (this week), "
                        "and Buy birthday gift (no date)."}
    return {"text": "Sure — happy to help. Ask me anything, or try the suggestions above."}


def _tool_call_obj(name, args):
    return {"index": 0, "id": "call_demo1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path.startswith("/reset"):
            STATE["providers"].clear()
            return self._json({"ok": True})
        if self.path.rstrip("/") == "/v1/models":
            return self._json(models_payload())
        if self.path.startswith("/api/tags"):  # Ollama detection
            return self._json({"models": [{"name": "llama3.1:8b"}, {"name": "nomic-embed-text:v1.5"}]})
        return self._json({"ok": True})

    def do_DELETE(self):
        parts = self.path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "providers":
            STATE["providers"].discard(parts[2])
        return self._json({"ok": True})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/api/providers":
            name = body.get("provider")
            if name:
                STATE["providers"].add(name)
            return self._json({"id": name or "p", "provider": name}, 201)
        if path.startswith("/api/providers/") and path.endswith("/keys"):
            return self._json({"id": "key_demo", "name": body.get("name", "k")}, 201)
        if path == "/v1/embeddings":
            return self._json({"object": "list", "model": body.get("model", "mock"),
                               "data": [{"object": "embedding", "index": 0, "embedding": [0.0123] * 768}]})
        if path == "/v1/chat/completions":
            return self._chat(body)
        return self._json({"ok": True})

    def _chat(self, body):
        messages = body.get("messages") or []
        has_tools = bool(body.get("tools"))
        stream = bool(body.get("stream"))
        d = decide(messages)
        use_tool = "tool" in d and has_tools
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def send(obj):
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                self.wfile.flush()

            if use_tool:
                tc = _tool_call_obj(d["tool"]["name"], d["tool"]["arguments"])
                send({"choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]})
                send({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            else:
                for word in (d.get("text") or "").split(" "):
                    send({"choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]})
                send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        # non-streaming (chat_with_tools / chat)
        if use_tool:
            msg = {"role": "assistant", "content": None,
                   "tool_calls": [_tool_call_obj(d["tool"]["name"], d["tool"]["arguments"])]}
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": d.get("text") or "Sure."}
            finish = "stop"
        return self._json({"id": "chatcmpl-demo", "object": "chat.completion",
                           "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                           "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})


def serve(port):
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    gw = int(sys.argv[1]) if len(sys.argv) > 1 else 38099
    threading.Thread(target=serve, args=(11434,), daemon=True).start()  # Ollama detection
    print(f"mock gateway on :{gw} + ollama on :11434", flush=True)
    serve(gw)
