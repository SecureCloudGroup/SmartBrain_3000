"""Serve the web app shell + PWA assets from FastAPI (Component D foundation).

Desktop runs at ``http://localhost`` — a secure context, so the service worker
and install prompt work with no certificate. The same FastAPI app serves the
static app shell, the PWA manifest / service-worker / icons, and applies tight
security headers. API (``/api``) and MCP (``/mcp``) routes are registered first
and so take precedence over the SPA fallback declared here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

# Tight CSP for a local app that holds secrets: same-origin only, no inline
# scripts, no framing. The later SvelteKit SPA can widen this if it must.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    # connect-src widened for WebRTC remote access: wss:// signaling broker +
    # stun:/turn: ICE (cross-origin / non-https schemes). Matches the meta CSP in
    # web/svelte.config.js (the policy that governs the SPA page).
    "connect-src 'self' wss: stun: turn:; manifest-src 'self'; worker-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
_HARDENING_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def web_dir() -> Path:
    """Return the directory holding the web app shell + PWA assets."""
    path = Path(__file__).parent / "web"
    assert path.is_dir(), "web assets directory must exist"
    assert (path / "index.html").is_file(), "app shell index.html must exist"
    return path


def safe_file(web: Path, full_path: str) -> Path | None:
    """Resolve ``full_path`` to a real file *inside* ``web``, else None.

    Traversal-safe: resolves first, then requires the result to stay within the
    web root, so ``..`` segments and absolute paths can never escape it. Returns
    None when there is no such file (the caller falls back to the SPA shell).
    """
    assert isinstance(full_path, str), "full_path must be a string"
    web_root = web.resolve()
    assert web_root.is_absolute(), "web root must be absolute"
    target = (web_root / full_path).resolve()
    if target.is_relative_to(web_root) and target.is_file():
        return target
    return None


def add_security_headers(app: FastAPI) -> None:
    """Apply hardening headers everywhere; CSP header on non-HTML responses.

    HTML pages (the SvelteKit SPA) carry their own hash-based CSP via a
    <meta> tag that allow-lists SvelteKit's inline bootstrap by sha256. A CSP
    *header* here would intersect with that meta policy and block the bootstrap,
    so we set the CSP header only on non-HTML (API/JSON) responses. Framing
    protection for HTML still comes from X-Frame-Options (meta frame-ancestors
    is ignored by browsers anyway).
    """
    assert app is not None, "app required"
    assert _CSP, "CSP must be non-empty"

    @app.middleware("http")
    async def _headers(request, call_next):
        response = await call_next(request)
        if not response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", _CSP)
        for name, value in _HARDENING_HEADERS.items():  # fixed, bounded
            response.headers.setdefault(name, value)
        return response


def mount_web(app: FastAPI) -> None:
    """Register PWA asset routes + the SPA fallback (call AFTER API routers)."""
    assert app is not None, "app required"
    web = web_dir()
    assert web.is_absolute(), "web dir must be an absolute path"

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(web / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/service-worker.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        # Root scope so the worker can control the whole origin; no-cache so an
        # updated worker is picked up promptly.
        return FileResponse(
            web / "service-worker.js",
            media_type="text/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        assert isinstance(full_path, str), "full_path must be a string"
        # Let unmatched API/MCP paths 404 as JSON, not as the SPA shell.
        if full_path.startswith(("api/", "mcp/")):
            raise HTTPException(status_code=404, detail="not found")
        target = safe_file(web, full_path)
        assert target is None or isinstance(target, Path), "safe_file contract"
        if target is not None:
            return FileResponse(target)
        # SPA fallback. no-cache so an updated shell/worker propagates promptly.
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-cache"})
