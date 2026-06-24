"""Tests for the Gmail OAuth flow, the Gmail client, routes + the email_send tool (H6).

Google + Gmail network calls are mocked; no real OAuth happens here.
"""

from __future__ import annotations

import base64
import email as emaillib
import hashlib
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import email_oauth, gmail, tools
from smartbrain_3000.audit import AuditLog
from smartbrain_3000.secrets import gen_master_key


# --- a fake httpx response/client ----------------------------------------

class _Resp:
    def __init__(self, status: int, data: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._data = data
        self.text = text

    def json(self) -> dict:
        return self._data or {}


class _FakeClient:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, data=None):
        return self._resp

    def request(self, method, url, headers=None, params=None, json=None):
        return self._resp


def _patch_httpx(monkeypatch, module, resp: _Resp) -> None:
    monkeypatch.setattr(module.httpx, "Client", lambda **k: _FakeClient(resp))


# --- OAuth ----------------------------------------------------------------

def test_build_auth_url_has_pkce_and_offline() -> None:
    url, state, verifier = email_oauth.build_auth_url("client-123")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["client-123"] and q["state"] == [state]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["code_challenge_method"] == ["S256"]
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert q["code_challenge"] == [expect]  # challenge matches the verifier
    assert q["redirect_uri"][0].startswith("http://localhost")


def test_exchange_code_requires_refresh_token(monkeypatch) -> None:
    _patch_httpx(monkeypatch, email_oauth, _Resp(200, {"access_token": "a", "expires_in": 3600}))
    with pytest.raises(email_oauth.EmailOAuthError):  # no refresh_token -> error
        email_oauth.exchange_code("c", "s", "code", "verifier")


def test_exchange_code_success(monkeypatch) -> None:
    _patch_httpx(monkeypatch, email_oauth, _Resp(200, {"refresh_token": "r", "access_token": "a", "expires_in": 3600}))
    out = email_oauth.exchange_code("c", "s", "code", "verifier")
    assert out["refresh_token"] == "r" and out["expires_in"] == 3600


def test_token_endpoint_error_raises(monkeypatch) -> None:
    _patch_httpx(monkeypatch, email_oauth, _Resp(400, text="invalid_grant"))
    with pytest.raises(email_oauth.EmailOAuthError):
        email_oauth.refresh_access_token("c", "s", "r")


# --- Gmail client helpers -------------------------------------------------

def test_build_raw_roundtrips() -> None:
    raw = gmail._build_raw("a@b.com", "Hi", "the body")
    msg = emaillib.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert msg["To"] == "a@b.com" and msg["Subject"] == "Hi"
    assert msg.get_payload(decode=True) == b"the body"  # decode the transfer-encoded body


def test_build_raw_rejects_header_injection() -> None:
    with pytest.raises(gmail.GmailError):  # CRLF in recipient -> injected header
        gmail._build_raw("a@b.com\r\nBcc: evil@x.com", "Hi", "body")
    with pytest.raises(gmail.GmailError):  # CRLF in subject
        gmail._build_raw("a@b.com", "Hi\nBcc: evil@x.com", "body")


def test_token_refresh_error_becomes_gmailerror(monkeypatch) -> None:
    def boom(*a):
        raise email_oauth.EmailOAuthError("invalid_grant (revoked)")

    monkeypatch.setattr(email_oauth, "refresh_access_token", boom)
    c = gmail.GmailClient("id", "secret", "refresh")
    with pytest.raises(gmail.GmailError):  # mapped so routes return 502 + audit, not 500
        c._token()


def test_extract_body_walks_nested_parts() -> None:
    data = base64.urlsafe_b64encode(b"hello world").decode().rstrip("=")
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/html", "body": {"data": "x"}},
        {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": data}}]},
    ]}
    assert gmail._extract_body(payload) == "hello world"


def test_client_token_is_cached(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(email_oauth, "refresh_access_token", lambda *a: (calls.append(1), ("tok", 3600))[1])
    c = gmail.GmailClient("id", "secret", "refresh")
    assert c._token() == "tok" and c._token() == "tok"
    assert len(calls) == 1  # second call served from cache


def test_list_and_send_parse(monkeypatch) -> None:
    c = gmail.GmailClient("id", "secret", "refresh")
    captured = {}

    def fake_request(method, path, *, params=None, json_body=None):
        if path == "/messages":
            return {"messages": [{"id": "m1"}]}
        if path.startswith("/messages/") and method == "GET":
            return {"id": "m1", "threadId": "t1", "snippet": "hey",
                    "payload": {"headers": [{"name": "Subject", "value": "Re: lunch"}, {"name": "From", "value": "a@b.com"}]}}
        captured["json"] = json_body
        return {"id": "sent1", "threadId": "t1"}

    monkeypatch.setattr(c, "_request", fake_request)
    msgs = c.list_recent(5)
    assert msgs[0]["subject"] == "Re: lunch" and msgs[0]["from"] == "a@b.com"
    res = c.send("x@y.com", "Hi", "body")
    assert res["id"] == "sent1" and "raw" in captured["json"]


# --- the email_send tool --------------------------------------------------

def _audit() -> AuditLog:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return AuditLog(conn, gen_master_key())


def test_email_send_tool_errors_when_not_connected() -> None:
    audit = _audit()
    ctx = tools.ToolContext(email=None)
    with pytest.raises(ValueError):  # surfaced as a validation-style error, audited
        tools.run(ctx, audit, "email_send", {"to": "a@b.com", "subject": "s", "body": "b"}, actor="user", claim=lambda: True)
    assert audit.list()[0]["decision"] == "errored"


def test_email_send_tool_calls_client_with_claim() -> None:
    audit = _audit()
    sent = {}

    class FakeClient:
        def send(self, to, subject, body):
            sent.update(to=to, subject=subject, body=body)
            return {"id": "1", "thread_id": "t"}

    ctx = tools.ToolContext(email=FakeClient())
    result = tools.run(ctx, audit, "email_send", {"to": "a@b.com", "subject": "s", "body": "b"}, actor="user", claim=lambda: True)
    assert result["id"] == "1" and sent["to"] == "a@b.com"
    assert audit.list()[0]["decision"] == "executed"


def test_email_send_is_irreversible() -> None:
    assert tools.get_tool("email_send").tier is tools.Tier.IRREVERSIBLE


# --- HTTP routes ----------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_email_status_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/email/status").status_code == 423


def test_email_not_connected_then_connect_returns_auth_url(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    s = client.get("/api/email/status").json()
    assert s["connected"] is False and s["redirect_uri"].startswith("http://localhost")
    r = client.post("/api/email/connect", json={"client_id": "cid", "client_secret": "sec"})
    assert "accounts.google.com" in r.json()["auth_url"]
    assert client.app.state.email_oauth_pending is not None  # PKCE state held


def test_email_callback_rejects_bad_state(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/email/connect", json={"client_id": "cid", "client_secret": "sec"})
    r = client.get("/api/email/oauth/callback?code=x&state=WRONG", follow_redirects=False)
    assert r.status_code == 400


def test_email_connect_callback_disconnect_flow(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/email/connect", json={"client_id": "cid", "client_secret": "sec"})
    state = client.app.state.email_oauth_pending["state"]

    monkeypatch.setattr(email_oauth, "exchange_code", lambda *a: {"refresh_token": "r", "access_token": "a", "expires_in": 3600})

    class FakeClient:
        def __init__(self, *a):
            pass

        def email_address(self):
            return "me@example.com"

        def list_recent(self, n):
            return [{"id": "m1", "subject": "hi"}]

    monkeypatch.setattr(gmail, "GmailClient", FakeClient)
    r = client.get(f"/api/email/oauth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303 and "connected=1" in r.headers["location"]
    s = client.get("/api/email/status").json()
    assert s["connected"] is True and s["address"] == "me@example.com"
    assert client.get("/api/email/messages").json()["messages"][0]["id"] == "m1"
    client.delete("/api/email/disconnect")
    assert client.get("/api/email/status").json()["connected"] is False


def test_email_messages_requires_connection(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.get("/api/email/messages").status_code == 409  # not connected


def test_reconnect_without_stored_creds_409(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.post("/api/email/reconnect").status_code == 409  # nothing to reconnect


def test_reconnect_reuses_stored_creds(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    # Connect once so client creds get persisted (commit-on-success).
    client.post("/api/email/connect", json={"client_id": "cid", "client_secret": "sec"})
    state = client.app.state.email_oauth_pending["state"]
    monkeypatch.setattr(email_oauth, "exchange_code", lambda *a: {"refresh_token": "r", "access_token": "a", "expires_in": 3600})

    class FakeClient:
        def __init__(self, *a):
            pass

        def email_address(self):
            return "me@example.com"

    monkeypatch.setattr(gmail, "GmailClient", FakeClient)
    client.get(f"/api/email/oauth/callback?code=abc&state={state}", follow_redirects=False)
    # Reconnect needs no body — it pulls the stored client id/secret.
    r = client.post("/api/email/reconnect")
    assert r.status_code == 200 and "accounts.google.com" in r.json()["auth_url"]
    assert client.app.state.email_oauth_pending is not None


def test_dead_refresh_token_maps_to_401(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})

    class DeadClient:
        def list_recent(self, n):
            raise gmail.GmailError("token refresh failed: invalid_grant")

    client.app.state.email = DeadClient()
    assert client.get("/api/email/messages").status_code == 401  # -> Reconnect banner


def test_connect_does_not_persist_creds_before_consent(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/email/connect", json={"client_id": "cid", "client_secret": "sec"})
    # creds live only in the in-flight handshake until OAuth succeeds
    assert client.get("/api/email/status").json()["has_creds"] is False


def test_callback_is_single_use(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/email/connect", json={"client_id": "cid", "client_secret": "sec"})
    state = client.app.state.email_oauth_pending["state"]
    bad = client.get("/api/email/oauth/callback?code=x&state=WRONG", follow_redirects=False)
    assert bad.status_code == 400
    assert client.app.state.email_oauth_pending is None  # consumed on first callback
    again = client.get(f"/api/email/oauth/callback?code=x&state={state}", follow_redirects=False)
    assert again.status_code == 400  # no pending left to replay


def test_bad_host_header_rejected(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200  # testserver allowed in tests
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400


def test_host_guard_is_case_insensitive(tmp_path, monkeypatch) -> None:
    # Hostnames are case-insensitive; a phone lowercases <Name>.local, so it must
    # still match a configured mixed-case host.
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "h.duckdb"))
    monkeypatch.setenv("SMARTBRAIN_ALLOWED_HOSTS", "My-Studio.local,localhost,127.0.0.1,testserver")
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/health", headers={"host": "my-studio.local"}).status_code == 200
        assert client.get("/api/health", headers={"host": "MY-STUDIO.LOCAL:33000"}).status_code == 200
        assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400
