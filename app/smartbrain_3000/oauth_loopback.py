"""Loopback HTTP→HTTPS redirect helper for the Gmail OAuth callback (TLS mode only).

Google's installed-app OAuth flow requires an ``http://`` loopback redirect. In plain-HTTP
mode the app serves the callback itself; but with TLS on (LAN/mobile mode) the app listens
HTTPS only, so Google's redirect to ``http://localhost/...`` would hit the TLS port and fail —
leaving the user stranded after consent.

This tiny helper bridges the gap: it listens HTTP on a dedicated **loopback-only** port and
302-redirects the OAuth callback (with its ``?code&state``) to the real https app. It ONLY
issues redirects — it never reads a request body, proxies data, or touches the vault — and it
only ever redirects to the loopback host, so it can't be turned into an open redirect. The
auth code stays entirely on the user's machine.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLBACK_PATH = "/api/email/oauth/callback"
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


def _make_handler(https_port: int) -> type[BaseHTTPRequestHandler]:
    class _RedirectToHttps(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _redirect(self) -> None:
            # Redirect only to the loopback host the browser used, swapped to the https app
            # port. Any non-loopback Host (can't happen over a loopback-only socket, but be
            # safe) collapses to localhost — never an open redirect to an external origin.
            host = self.headers.get("Host", "localhost").rsplit(":", 1)[0].strip().lower()
            if host not in _LOOPBACK_HOSTS:
                host = "localhost"
            # Forward only the OAuth callback path (with its query); anything else → app root.
            path = self.path if self.path.split("?", 1)[0] == CALLBACK_PATH else "/"
            location = f"https://{host}:{https_port}{path}"
            body = b"Redirecting to the secure app\xe2\x80\xa6\n"
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        do_GET = _redirect
        do_HEAD = _redirect

        def log_message(self, *_args) -> None:  # keep the app's own logs clean
            pass

    return _RedirectToHttps


def start(listen_host: str, listen_port: int, https_port: int) -> ThreadingHTTPServer:
    """Start the loopback redirect helper in a daemon thread; return the server.

    ``listen_host``/``listen_port`` are where this helper binds (compose publishes the port on
    host loopback only); ``https_port`` is the port the real TLS app serves on.
    """
    # listen_port 0 = let the OS assign a free port (used by tests); https_port is the real
    # app port and must be a concrete value.
    assert 0 <= listen_port <= 65535 and 1 <= https_port <= 65535, "ports out of range"
    server = ThreadingHTTPServer((listen_host, listen_port), _make_handler(https_port))
    threading.Thread(target=server.serve_forever, name="oauth-loopback", daemon=True).start()
    return server
