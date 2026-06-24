"""Integration tests: REAL SSRF-guarded egress against a REAL local HTTP server.

The unit tests in test_netguard.py only cover the *validation* paths (bad scheme,
private IP, …), which all raise before any HTTP send — so the SUCCESS path (real
httpx stream + read + close) was never exercised, and the bug that httpx.Response
is not a context manager broke web_search/web_fetch/ingest_url in production while
the suite stayed green. These tests run the real fetch against a real socket. The
only thing stubbed is _validated_ip (which has its own dedicated tests) so the
loopback test server is reachable past the SSRF allowlist — the fetch MECHANISM is
fully real.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from smartbrain_3000 import netguard


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/page":
            body = b"<html><body>hello world</body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("location", "/page")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def server(monkeypatch) -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    # Allow the loopback test server past the SSRF IP allowlist (validation is tested
    # separately); everything else in the fetch path stays real.
    monkeypatch.setattr(netguard, "_validated_ip", lambda host: "127.0.0.1")
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_safe_fetch_real_socket_returns_body(server: str) -> None:
    # Exercises send(stream=True) -> iter_bytes -> close. Before the fix this raised
    # "'Response' object does not support the context manager protocol".
    out = netguard.safe_fetch(f"{server}/page")
    assert out["status"] == 200
    assert "hello world" in out["text"]


def test_safe_fetch_bytes_real_socket(server: str) -> None:
    out = netguard.safe_fetch_bytes(f"{server}/page")
    assert out["status"] == 200
    assert b"hello world" in out["content"]


def test_safe_fetch_follows_redirect_and_closes(server: str) -> None:
    # The redirect branch `continue`s past a streamed response — it must close it too.
    out = netguard.safe_fetch(f"{server}/redirect")
    assert out["status"] == 200 and "hello world" in out["text"]
