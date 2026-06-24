# Third-Party Licenses

SmartBrain_3000 itself is licensed under the Elastic License 2.0 (see
[`LICENSE`](LICENSE)). It builds on the open-source components below; each is the
property of its respective authors and is used under its own license. This file
is a summary — the authoritative license text ships with each package.

## Runtime (Python — `app/pyproject.toml`)

| Component | Purpose | License |
|-----------|---------|---------|
| [FastAPI](https://github.com/fastapi/fastapi) | HTTP framework | MIT |
| [Starlette](https://github.com/encode/starlette) | ASGI toolkit (via FastAPI) | BSD-3-Clause |
| [Uvicorn](https://github.com/encode/uvicorn) | ASGI server | BSD-3-Clause |
| [DuckDB](https://github.com/duckdb/duckdb) | Embedded database | MIT |
| [cryptography](https://github.com/pyca/cryptography) | AES-GCM at rest | Apache-2.0 OR BSD-3-Clause |
| [argon2-cffi](https://github.com/hynek/argon2-cffi) | Passphrase KDF | MIT |
| [httpx](https://github.com/encode/httpx) | HTTP client (gateway, OAuth, Gmail) | BSD-3-Clause |
| [MCP SDK](https://github.com/modelcontextprotocol/python-sdk) | Model Context Protocol server | MIT |
| [Pydantic](https://github.com/pydantic/pydantic) | Request validation (via FastAPI) | MIT |
| [pypdf](https://github.com/py-pdf/pypdf) | PDF text extraction (Knowledge ingest) | BSD-3-Clause |
| [trafilatura](https://github.com/adbar/trafilatura) | HTML article extraction (Knowledge ingest) | Apache-2.0 |
| [aiortc](https://github.com/aiortc/aiortc) | WebRTC peer (remote access) | BSD-3-Clause |
| [websockets](https://github.com/python-websockets/websockets) | WebSocket client (remote signaling) | BSD-3-Clause |

## Frontend (built into the served SPA — `web/package.json`)

| Component | Purpose | License |
|-----------|---------|---------|
| [Svelte](https://github.com/sveltejs/svelte) | UI framework | MIT |
| [SvelteKit](https://github.com/sveltejs/kit) | App framework / static adapter | MIT |
| [Vite](https://github.com/vitejs/vite) | Build tool | MIT |
| [TypeScript](https://github.com/microsoft/TypeScript) | Types (build-time) | Apache-2.0 |
| [@noble/ed25519](https://github.com/paulmillr/noble-ed25519) | Ed25519 channel-auth for remote access (bundled) | MIT |
| [qrcode](https://github.com/soldair/node-qrcode) | Pairing QR rendering (bundled) | MIT |
| [marked](https://github.com/markedjs/marked) | Renders the docs into the help page (build-time) | MIT |

## Companion services (run as separate containers / on the host)

| Component | Role | License |
|-----------|------|---------|
| [Bifrost](https://github.com/maximhq/bifrost) | LLM gateway (compose service) | Apache-2.0 |
| [Ollama](https://github.com/ollama/ollama) | Optional local model server (host) | MIT |
| [coturn](https://github.com/coturn/coturn) | STUN/TURN server (remote-access node, optional) | BSD-3-Clause |
| [Caddy](https://github.com/caddyserver/caddy) | Reverse proxy / TLS for the remote-access node (optional) | Apache-2.0 |

If you believe an attribution here is inaccurate or incomplete, please open an
issue or email **info@securecloudgroup.com**.
