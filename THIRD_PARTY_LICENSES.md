# Third-Party Licenses

SmartBrain_3000 itself is licensed under the Elastic License 2.0 (see
[`LICENSE`](LICENSE)). It builds on the open-source components below; each is the
property of its respective authors and is used under its own license. This file
is a summary — the authoritative license text ships with each package.

## Runtime (Python — shipped in the Docker image; pinned in `app/requirements.lock`)

The complete runtime set (67 packages) is exact-version pinned in [`app/requirements.lock`](app/requirements.lock). All are permissive (MIT / BSD / Apache-2.0 / ISC / PSF) **except** the two weak/file-level-copyleft components called out below; their notices ship unmodified inside each package.

| Component | Purpose | License |
|-----------|---------|---------|
| aioice | transitive dependency | BSD-3-Clause |
| aiortc | WebRTC peer (remote access) | BSD-3-Clause |
| annotated-doc | transitive dependency | MIT |
| annotated-types | transitive dependency | MIT |
| anyio | transitive dependency | MIT |
| argon2-cffi | Passphrase KDF | MIT |
| argon2-cffi-bindings | transitive dependency | MIT |
| attrs | transitive dependency | MIT |
| av | transitive dependency | BSD-3-Clause |
| babel | transitive dependency | BSD-3-Clause |
| certifi | CA trust store (HTTPS) | MPL-2.0 |
| cffi | transitive dependency | MIT |
| charset-normalizer | transitive dependency | MIT |
| click | transitive dependency | BSD-3-Clause |
| courlan | transitive dependency | Apache-2.0 |
| cryptography | AES-GCM at rest | Apache-2.0 OR BSD-3-Clause |
| dateparser | transitive dependency | BSD-3-Clause |
| dnspython | transitive dependency | ISC |
| duckdb | Embedded DB | MIT |
| fastapi | HTTP framework | MIT |
| google-crc32c | transitive dependency | Apache-2.0 |
| h11 | transitive dependency | MIT |
| htmldate | transitive dependency | Apache-2.0 |
| httpcore | transitive dependency | BSD-3-Clause |
| httptools | transitive dependency | MIT |
| httpx | HTTP client (gateway/OAuth/Gmail) | BSD-3-Clause |
| httpx-sse | transitive dependency | MIT |
| idna | transitive dependency | BSD-3-Clause |
| ifaddr | transitive dependency | MIT |
| jsonschema | transitive dependency | MIT |
| jsonschema-specifications | transitive dependency | MIT |
| jusText | transitive dependency | BSD-2-Clause |
| lxml | transitive dependency | BSD-3-Clause |
| lxml_html_clean | transitive dependency | BSD-3-Clause |
| mcp | Model Context Protocol server | MIT |
| packaging | transitive dependency | Apache-2.0 OR BSD-2-Clause |
| pycparser | transitive dependency | BSD-2-Clause |
| pydantic | Request validation | MIT |
| pydantic-settings | transitive dependency | MIT |
| pydantic_core | transitive dependency | MIT |
| pyee | transitive dependency | MIT |
| Pygments | transitive dependency | BSD-2-Clause |
| PyJWT | transitive dependency | MIT |
| pylibsrtp | transitive dependency | BSD-3-Clause |
| pyOpenSSL | DTLS (via aiortc) | Apache-2.0 |
| pypdf | PDF text extraction | BSD-3-Clause |
| python-dateutil | transitive dependency | Apache-2.0 OR BSD-3-Clause |
| python-dotenv | transitive dependency | BSD-3-Clause |
| python-multipart | transitive dependency | Apache-2.0 |
| pytz | transitive dependency | MIT |
| PyYAML | transitive dependency | MIT |
| referencing | transitive dependency | MIT |
| regex | transitive dependency | Apache-2.0 |
| rpds-py | transitive dependency | MIT |
| six | transitive dependency | MIT |
| sse-starlette | transitive dependency | BSD-3-Clause |
| starlette | ASGI toolkit | BSD-3-Clause |
| tld | TLD parsing (via trafilatura/courlan) | MPL-1.1 / GPL-2.0 / LGPL-2.1 (tri-license — used under MPL-1.1) |
| trafilatura | HTML article extraction | Apache-2.0 |
| typing-inspection | transitive dependency | MIT |
| typing_extensions | transitive dependency | PSF-2.0 |
| tzlocal | transitive dependency | MIT |
| urllib3 | transitive dependency | MIT |
| uvicorn | ASGI server | BSD-3-Clause |
| uvloop | transitive dependency | MIT OR Apache-2.0 |
| watchfiles | transitive dependency | MIT |
| websockets | WebSocket client (signaling) | BSD-3-Clause |

> **certifi (MPL-2.0)** — Mozilla's CA bundle, shipped **unmodified**. MPL-2.0 is file-level (weak) copyleft: it is not viral against SmartBrain's own code; we retain certifi's MPL-2.0 notice in the image.
>
> **tld** is tri-licensed **MPL-1.1 / GPL-2.0-only / LGPL-2.1-or-later**; SmartBrain elects and relies on it under the **MPL-1.1** arm (NOT GPL-2.0). It arrives transitively via `trafilatura → courlan`. (Legal review should confirm this election.)


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
