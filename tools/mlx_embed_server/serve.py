"""SmartBrain MLX embeddings server — a tiny OpenAI-compatible /v1/embeddings host service.

Why this exists: chat-oriented MLX servers (oMLX) serve only ENCODER embedders and refuse
decoder embedding models — verified live with Qwen3-Embedding ("not an embedding model").
mlx-embeddings loads those models with the correct pooling, so this ~150-line stdlib
server puts one behind the exact API shape the app's Bifrost "mlxe" provider expects.

Endpoints: GET /health, GET /v1/models, POST /v1/embeddings ({"model","input"} — input
may be a string or a list of strings). Optional Bearer auth via --api-key. Single-threaded
by design: the app serializes local-model calls anyway, and MLX wants one request at a time.

Run:  python serve.py [--model mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ]
                      [--served-name qwen3-embedding-0.6b] [--port 8899] [--api-key KEY]
Install as a login service: ./install.sh (see README.md).
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

MODEL = None
TOKENIZER = None
SERVED_NAME = "qwen3-embedding-0.6b"
API_KEY = ""
_MAX_INPUT_ITEMS = 64          # bound one request's batch (the app sends one)
_MAX_INPUT_CHARS = 32_000      # bound one text (app caps at 6000; headroom, not a promise)


class Handler(BaseHTTPRequestHandler):
    server_version = "sb-mlx-embed/1.0"

    def log_message(self, *args) -> None:  # quiet: launchd captures stderr if ever needed
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not API_KEY:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {API_KEY}"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "model": SERVED_NAME})
            return
        if self.path == "/v1/models":
            if not self._authed():
                self._send(401, {"error": {"message": "API key required", "type": "authentication_error"}})
                return
            self._send(200, {"object": "list", "data": [{"id": SERVED_NAME, "object": "model"}]})
            return
        self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self) -> None:
        if self.path != "/v1/embeddings":
            self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
            return
        if not self._authed():
            self._send(401, {"error": {"message": "API key required", "type": "authentication_error"}})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length).decode())
            raw = body.get("input")
            texts = [raw] if isinstance(raw, str) else list(raw or [])
            assert texts and len(texts) <= _MAX_INPUT_ITEMS, "input must be 1..64 texts"
            texts = [str(t)[:_MAX_INPUT_CHARS] for t in texts]
        except Exception:
            self._send(400, {"error": {"message": "body must be {model, input}", "type": "invalid_request_error"}})
            return
        try:
            from mlx_embeddings import generate

            out = generate(MODEL, TOKENIZER, texts=texts)
            data = [{"object": "embedding", "index": i, "embedding": out.text_embeds[i].tolist()}
                    for i in range(len(texts))]  # bounded by _MAX_INPUT_ITEMS
            self._send(200, {"object": "list", "data": data, "model": SERVED_NAME})
        except Exception as exc:  # one bad request must not kill the service
            self._send(500, {"error": {"message": f"embedding failed: {type(exc).__name__}", "type": "server_error"}})


def main() -> None:
    global MODEL, TOKENIZER, SERVED_NAME, API_KEY
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ")
    ap.add_argument("--served-name", default="qwen3-embedding-0.6b",
                    help="model id shown in /v1/models and the app's routing dropdown")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--api-key", default="")
    args = ap.parse_args()
    SERVED_NAME, API_KEY = args.served_name, args.api_key

    from mlx_embeddings import load  # import late: --help must work without mlx installed

    print(f"loading {args.model} …", flush=True)
    MODEL, TOKENIZER = load(args.model)
    print(f"serving {args.served_name} on http://127.0.0.1:{args.port}/v1/embeddings", flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
