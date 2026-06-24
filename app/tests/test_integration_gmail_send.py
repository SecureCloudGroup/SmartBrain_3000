"""Integration test: REAL gmail.send code against a REAL local Gmail-API stand-in.

The existing test_email.py stubs ``GmailClient._request`` or the whole
``gmail.GmailClient``, so the actual httpx POST to ``/messages/send`` (URL +
JSON body + auth header) is never exercised. If a regression silently dropped
the body the suite stays green — same failure mode as the shipped streaming-
tools and netguard bugs. This test points ``gmail._API`` at a local server and
asserts what we actually put on the wire.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from smartbrain_3000 import email_oauth, gmail


class _GmailHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *a) -> None:
        pass

    def _reply(self, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        type(self).requests.append({
            "path": self.path,
            "auth": self.headers.get("authorization", ""),
            "body": body,
        })
        self._reply({"id": "sent-1", "threadId": "thr-1"})

    def do_GET(self) -> None:
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        type(self).requests.append({"path": self.path, "auth": self.headers.get("authorization", "")})
        if path.endswith("/messages"):  # list
            return self._reply({"messages": [{"id": "m1"}]})
        return self._reply({  # one message (metadata or full) — headers + a text/plain body
            "id": "m1", "threadId": "t1", "snippet": "a short snippet",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "Subject", "value": "Hello there"},
                    {"name": "Date", "value": "Mon, 01 Jan 2026 00:00:00 +0000"},
                ],
                "body": {"data": base64.urlsafe_b64encode(b"the message body").decode().rstrip("=")},
            },
        })


@pytest.fixture()
def fake_gmail(monkeypatch) -> Iterator[list[dict]]:
    _GmailHandler.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _GmailHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/gmail/v1/users/me"
    monkeypatch.setattr(gmail, "_API", base)
    monkeypatch.setattr(
        email_oauth, "refresh_access_token",
        lambda cid, sec, rt: ("test-access-token", 3600),
    )
    try:
        yield _GmailHandler.requests
    finally:
        srv.shutdown()
        srv.server_close()


def test_gmail_send_puts_message_on_the_wire(fake_gmail: list[dict]) -> None:
    # The regression guard: ``send`` must POST a non-empty base64url MIME body
    # to /messages/send carrying both subject and body. A bug that drops either
    # one (or the wrong path) makes the existing _request-faked tests pass while
    # production silently sends empty mail.
    client = gmail.GmailClient("client-id", "client-secret", "refresh-token")
    res = client.send("a@b.com", "the subject", "the body")
    assert res["id"] == "sent-1" and res["thread_id"] == "thr-1"

    assert len(fake_gmail) == 1, fake_gmail
    sent = fake_gmail[0]
    assert sent["path"].endswith("/messages/send"), sent["path"]
    assert sent["auth"] == "Bearer test-access-token"
    raw = sent["body"].get("raw") or ""
    assert raw, "send dropped the message body on the wire"
    # base64url-decode the raw MIME and confirm subject + body were carried.
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
    assert "Subject: the subject" in decoded
    # MIMEText base64-encodes the body part; decode the last non-empty line.
    payload_b64 = decoded.rsplit("\n\n", 1)[-1].strip()
    payload = base64.b64decode(payload_b64).decode("utf-8", "replace")
    assert payload == "the body"


def test_gmail_list_recent_real_http(fake_gmail: list[dict]) -> None:
    # Real list path: GET /messages then GET /messages/{id}; assert parsed headers + auth.
    client = gmail.GmailClient("client-id", "client-secret", "refresh-token")
    msgs = client.list_recent(5)
    assert len(msgs) == 1
    assert msgs[0]["from"] == "alice@example.com"
    assert msgs[0]["subject"] == "Hello there"
    assert msgs[0]["snippet"] == "a short snippet"
    assert all(r["auth"] == "Bearer test-access-token" for r in fake_gmail)
    assert any(r["path"].rstrip("/").endswith("/messages") or "/messages?" in r["path"] for r in fake_gmail)


def test_gmail_read_message_real_http(fake_gmail: list[dict]) -> None:
    # Real read path: GET /messages/{id}?format=full; assert the text/plain body decodes.
    client = gmail.GmailClient("client-id", "client-secret", "refresh-token")
    msg = client.read_message("m1")
    assert msg["subject"] == "Hello there"
    assert msg["body"] == "the message body"
    assert fake_gmail[-1]["path"].endswith("format=full") or "format=full" in fake_gmail[-1]["path"]
