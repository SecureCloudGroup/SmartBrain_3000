"""Integration test: REAL Gmail OAuth httpx code against a REAL local token server.

email_oauth.exchange_code / refresh_access_token were only ever monkeypatched, so the
real httpx POST + JSON parsing never ran in tests. Here we point GOOGLE_TOKEN_URL at a
local server (redirecting the third-party boundary, not faking our code) and exercise
the real request/parse path.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from smartbrain_3000 import email_oauth


class _TokenHandler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)  # consume the form body
        payload = json.dumps({"refresh_token": "rt-123", "access_token": "at-456", "expires_in": 3600}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def token_url(monkeypatch) -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/token"
    monkeypatch.setattr(email_oauth, "GOOGLE_TOKEN_URL", url)
    try:
        yield url
    finally:
        srv.shutdown()
        srv.server_close()


def test_exchange_code_real_http(token_url: str) -> None:
    out = email_oauth.exchange_code("client-id", "client-secret", "auth-code", "verifier")
    assert out["refresh_token"] == "rt-123"
    assert out["access_token"] == "at-456"
    assert out["expires_in"] == 3600


def test_refresh_access_token_real_http(token_url: str) -> None:
    token, expires = email_oauth.refresh_access_token("client-id", "client-secret", "rt-123")
    assert token == "at-456"
    assert expires > 0
