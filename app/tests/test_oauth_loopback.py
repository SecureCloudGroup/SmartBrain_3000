"""Gmail OAuth loopback redirect: TLS-aware redirect_uri() + the http->https helper.

Google's installed-app flow needs an http:// loopback redirect. Over TLS the app has no
HTTP listener, so redirect_uri() points at a loopback helper port and oauth_loopback 302s the
callback to the https app. These tests pin both halves (the URI computation and the redirect).
"""

from __future__ import annotations

import http.client

import pytest

from smartbrain_3000 import email_oauth, oauth_loopback, serve

_OAUTH_ENV = (
    "SMARTBRAIN_OAUTH_REDIRECT", "SMARTBRAIN_TLS_CERT",
    "SMARTBRAIN_OAUTH_HELPER_PORT", "SMARTBRAIN_PORT",
)


def _clear(monkeypatch) -> None:
    for var in _OAUTH_ENV:
        monkeypatch.delenv(var, raising=False)


# --- redirect_uri() ---------------------------------------------------------

def test_redirect_uri_http_mode_uses_app_port(monkeypatch) -> None:
    _clear(monkeypatch)
    assert email_oauth.redirect_uri() == "http://localhost:33000/api/email/oauth/callback"


def test_redirect_uri_http_mode_honors_custom_port(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SMARTBRAIN_PORT", "39000")
    assert email_oauth.redirect_uri() == "http://localhost:39000/api/email/oauth/callback"


def test_redirect_uri_tls_uses_helper_port(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SMARTBRAIN_TLS_CERT", "/app/certs/cert.pem")  # TLS on -> helper
    assert email_oauth.redirect_uri() == "http://localhost:33001/api/email/oauth/callback"


def test_redirect_uri_tls_honors_custom_helper_port(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SMARTBRAIN_TLS_CERT", "/app/certs/cert.pem")
    monkeypatch.setenv("SMARTBRAIN_OAUTH_HELPER_PORT", "34001")
    assert email_oauth.redirect_uri() == "http://localhost:34001/api/email/oauth/callback"


def test_redirect_uri_override_wins_but_must_be_loopback(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SMARTBRAIN_TLS_CERT", "/app/certs/cert.pem")  # override beats TLS logic
    monkeypatch.setenv("SMARTBRAIN_OAUTH_REDIRECT", "http://127.0.0.1:9/api/email/oauth/callback")
    assert email_oauth.redirect_uri() == "http://127.0.0.1:9/api/email/oauth/callback"
    monkeypatch.setenv("SMARTBRAIN_OAUTH_REDIRECT", "https://evil.example/callback")  # non-loopback
    with pytest.raises(email_oauth.EmailOAuthError):
        email_oauth.redirect_uri()


# --- serve.helper_port() (collision guard) ----------------------------------

def test_helper_port_default_when_distinct(monkeypatch) -> None:
    monkeypatch.delenv("SMARTBRAIN_OAUTH_HELPER_PORT", raising=False)
    assert serve.helper_port(33000) == 33001


def test_helper_port_honors_env(monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_OAUTH_HELPER_PORT", "34567")
    assert serve.helper_port(33000) == 34567


def test_helper_port_rejects_collision_with_app_port(monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_OAUTH_HELPER_PORT", "33000")
    with pytest.raises(RuntimeError):
        serve.helper_port(33000)


def test_helper_port_rejects_default_collision(monkeypatch) -> None:
    monkeypatch.delenv("SMARTBRAIN_OAUTH_HELPER_PORT", raising=False)
    with pytest.raises(RuntimeError):  # app is on the helper's default 33001
        serve.helper_port(33001)


# --- oauth_loopback helper (http -> https 302) ------------------------------

def _location(port: int, path: str, host: str) -> tuple[int, str | None]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": host})  # explicit Host overrides the default
        resp = conn.getresponse()
        loc = resp.getheader("Location")
        resp.read()
        return resp.status, loc
    finally:
        conn.close()


@pytest.fixture()
def helper():
    server = oauth_loopback.start("127.0.0.1", 0, 33000)  # port 0 -> OS assigns
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_helper_forwards_callback_with_query(helper) -> None:
    _server, port = helper
    status, loc = _location(port, "/api/email/oauth/callback?code=abc&state=xyz", host="localhost")
    assert status == 302
    assert loc == "https://localhost:33000/api/email/oauth/callback?code=abc&state=xyz"


def test_helper_preserves_loopback_host(helper) -> None:
    _server, port = helper
    status, loc = _location(port, "/api/email/oauth/callback?code=1", host="127.0.0.1")
    assert status == 302 and loc == "https://127.0.0.1:33000/api/email/oauth/callback?code=1"


def test_helper_collapses_nonloopback_host_no_open_redirect(helper) -> None:
    _server, port = helper
    status, loc = _location(port, "/api/email/oauth/callback?code=1", host="evil.example")
    assert status == 302 and loc == "https://localhost:33000/api/email/oauth/callback?code=1"


def test_helper_non_callback_path_goes_to_root(helper) -> None:
    _server, port = helper
    status, loc = _location(port, "/wp-admin?x=1", host="localhost")
    assert status == 302 and loc == "https://localhost:33000/"
