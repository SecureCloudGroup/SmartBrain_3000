"""Bridge a remote device's request frames to the local app over loopback HTTP.

This is the load-bearing core of the WebRTC remote-access path (P2P-first, with a
content-blind relay fallback). A paired phone sends framed HTTP requests over a
DTLS-encrypted WebRTC DataChannel; this module turns each frame into a call
against the app's own loopback (`http://127.0.0.1:33000`) and frames the response
back. The data plane is end-to-end encrypted between phone and Desktop, so any
relay only ever carries ciphertext — the app never exposes a plaintext port.

Phase 1 scope: the framing + the request/response proxy ONLY. There is no WebRTC
here yet — `aiortc` and the signaling loop arrive in a later phase and will call
`handle_frame` off the event loop via `asyncio.to_thread`, passing an
`httpx.Client` bound to the loopback base URL.

Two invariants this module must never violate (a remote peer is untrusted):
  * a peer can reach ONLY `/api/*` — never `/mcp` admin, the SPA, or arbitrary paths;
  * every size is bounded, so a peer cannot exhaust memory.

The wire contract (shared with the client's `protocol.ts`, built in a later phase):
  request frame : {"id": str, "method": str, "path": "/api/...", "headers": {..}, "body": bytes|str}
  response frame: {"id": str, "status": int, "headers": {..}, "body": bytes}
"""

from __future__ import annotations

import json
import os

import httpx

LOOPBACK_BASE = "http://127.0.0.1:33000"  # default; loopback_client() matches the live scheme
_LOOPBACK_TIMEOUT = 60.0
_ALLOWED_PREFIX = "/api/"
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_MAX_BODY_BYTES = 25 * 1024 * 1024  # 25 MB — matches the Knowledge upload cap
# Only these client-supplied headers are forwarded; Host/Content-Length/cookies and
# anything else are dropped so a peer cannot smuggle headers into the local call.
_FORWARD_REQUEST_HEADERS = frozenset({"content-type", "accept", "accept-language"})
# Only these response headers are returned; hop-by-hop/cookie headers are dropped.
_FORWARD_RESPONSE_HEADERS = frozenset({"content-type", "content-disposition", "cache-control"})


class FrameError(ValueError):
    """A request frame violated the wire contract (shape, size, method, or path)."""


def parse_request(frame: dict) -> dict:
    """Validate a request frame; return a normalized {method, path, headers, body}.

    Raises ``FrameError`` for anything malformed, oversized, a disallowed method,
    or a path outside ``/api/`` (the peer-reachable surface).
    """
    assert isinstance(frame, dict), "frame must be a dict"
    method = str(frame.get("method") or "").upper()
    path = str(frame.get("path") or "")
    if method not in _ALLOWED_METHODS:
        raise FrameError(f"method not allowed: {method!r}")
    if not path.startswith(_ALLOWED_PREFIX) or "://" in path or path.startswith("//"):
        raise FrameError(f"path must be a local {_ALLOWED_PREFIX}* path, got {path!r}")
    # A leading "/api/" is not enough: the HTTP client normalizes dot-segments against
    # the loopback base, so "/api/../mcp" would escape to /mcp. Reject any "..", and any
    # control/backslash chars that could rewrite the target or smuggle a request line.
    if ".." in path.split("/") or any(c in path for c in "\\\r\n\t") or "\x00" in path:
        raise FrameError(f"illegal path segment in {path!r}")
    body = frame.get("body") or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, (bytes, bytearray)):
        raise FrameError("body must be bytes or a string")
    if len(body) > _MAX_BODY_BYTES:
        raise FrameError("request body exceeds the size cap")
    src_headers = frame.get("headers") or {}
    assert isinstance(src_headers, dict), "headers must be a dict"
    headers = {k: v for k, v in src_headers.items() if str(k).lower() in _FORWARD_REQUEST_HEADERS}
    return {"method": method, "path": path, "headers": headers, "body": bytes(body)}


def handle_frame(frame: dict, client) -> dict:
    """Proxy one request frame to the local app; return a response frame.

    ``client`` is anything with ``.request(method, url, headers=, content=)`` that
    returns an httpx-style response — in production an ``httpx.Client`` bound to
    ``LOOPBACK_BASE`` (called via ``asyncio.to_thread``), in tests a Starlette
    ``TestClient``. Never raises on an upstream failure: it returns a clean error
    frame so a single bad request can't tear down the DataChannel. A frame with no
    ``id`` is unrecoverable (no one to reply to) and trips an assertion.
    """
    rid = frame.get("id") if isinstance(frame, dict) else None
    assert isinstance(rid, str) and rid, "request frame must carry a string id"
    try:
        req = parse_request(frame)
    except FrameError as exc:
        return _error_frame(rid, 400, str(exc))
    try:
        resp = client.request(req["method"], req["path"], headers=req["headers"], content=req["body"])
    except Exception as exc:  # any local/transport failure -> clean error frame, never crash
        return _error_frame(rid, 502, f"upstream error: {type(exc).__name__}")
    assert resp is not None, "client.request must return a response"
    return {
        "id": rid,
        "status": int(resp.status_code),
        "headers": _response_headers(resp),
        "body": bytes(resp.content),
    }


def _response_headers(resp) -> dict:
    """Return only the safe subset of response headers (drop hop-by-hop/cookies)."""
    assert resp is not None, "response required"
    out = {k: v for k, v in dict(resp.headers).items() if str(k).lower() in _FORWARD_RESPONSE_HEADERS}
    return out


def _error_frame(rid: str, status: int, detail: str) -> dict:
    """Build a JSON error response frame the client can surface like any API error."""
    assert isinstance(rid, str) and rid, "error frame needs a request id"
    assert 400 <= status <= 599, "error frame status must be 4xx/5xx"
    body = json.dumps({"detail": detail}).encode("utf-8")
    return {"id": rid, "status": status, "headers": {"content-type": "application/json"}, "body": body}


def _loopback_base() -> str:
    """The app's own bind URL — scheme MUST match the live server: HTTPS when TLS is
    configured (the LAN overlay), else HTTP. A mismatch makes httpx raise
    RemoteProtocolError (TLS read over a plaintext expectation), which the bridge would
    surface to a remote device as a 502. Port follows SMARTBRAIN_PORT."""
    port = os.environ.get("SMARTBRAIN_PORT", "33000")
    scheme = "https" if os.environ.get("SMARTBRAIN_TLS_CERT") else "http"
    assert port.isdigit(), f"SMARTBRAIN_PORT must be numeric, got {port!r}"
    assert scheme in ("http", "https"), "loopback scheme invariant"
    return f"{scheme}://127.0.0.1:{port}"


def loopback_client() -> httpx.Client:
    """An httpx.Client bound to the app's own loopback, for the WebRTC bridge to call.

    handle_frame is sync and is driven off the event loop (asyncio.to_thread) by the
    peer, so a sync client is correct here. The timeout bounds a slow upstream call.
    Verification is skipped for an HTTPS loopback self-call (the bridge is calling its
    own process on 127.0.0.1 under its own mkcert cert — there is no MITM surface here).
    """
    base = _loopback_base()
    return httpx.Client(base_url=base, timeout=_LOOPBACK_TIMEOUT, verify=not base.startswith("https"))
