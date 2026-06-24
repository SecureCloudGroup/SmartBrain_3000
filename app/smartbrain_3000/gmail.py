"""Minimal Gmail REST client (H6) — read recent / read one / send.

A capability object the rest of the app acts through. It encapsulates the user's
OAuth credentials (client id/secret + refresh token) and exposes only actions —
it never returns raw credentials, so the credential firewall holds at the
boundary (audit / model / tiles see only results). Access tokens are fetched
from the refresh token on demand and cached until they near expiry. Raw httpx to
the FIXED Gmail endpoint (never user-supplied URLs).
"""

from __future__ import annotations

import base64
import threading
import time
from email.mime.text import MIMEText

import httpx

from . import email_oauth

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_TIMEOUT = 15.0
_MAX_LIST = 25       # cap on messages listed (bounded)
_MAX_MIME_NODES = 256  # cap on the MIME walk (bounded)
_MAX_PARTS = 64        # cap on per-node fan-out (bounded)
_TOKEN_SKEW = 60       # refresh this many seconds before expiry


class GmailError(RuntimeError):
    """Raised on a Gmail API failure (4xx/5xx or transport error)."""


def _b64url_decode(data: str) -> str:
    """Decode Gmail's base64url body data to text (best effort; '' on garbage)."""
    assert isinstance(data, str), "data must be a string"
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "replace")
    except (ValueError, UnicodeError):  # malformed base64url -> best effort empty
        return ""


def _extract_body(payload: dict) -> str:
    """Return the first text/plain part of a message payload (bounded walk)."""
    assert isinstance(payload, dict), "payload must be a dict"
    stack = [payload]
    steps = 0
    while stack and steps < _MAX_MIME_NODES:  # fixed upper bound (P10 #2)
        steps += 1
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        body = node.get("body") or {}
        if node.get("mimeType") == "text/plain" and body.get("data"):
            return _b64url_decode(body["data"])
        for part in (node.get("parts") or [])[:_MAX_PARTS]:  # bounded fan-out
            stack.append(part)
    return ""


def _headers(msg: dict) -> dict[str, str]:
    """Map the From/Subject/Date headers of a Gmail message (case-insensitive)."""
    assert isinstance(msg, dict), "message must be a dict"
    wanted = {"from", "subject", "date"}
    out: dict[str, str] = {}
    for h in (msg.get("payload", {}).get("headers") or [])[:50]:  # bounded
        name = str(h.get("name", "")).lower()
        if name in wanted:
            out[name] = h.get("value", "")
    return out


def _build_raw(to: str, subject: str, body: str) -> str:
    """Build a base64url RFC-2822 message for users.messages.send."""
    assert to and "@" in to, "a valid recipient is required"
    assert isinstance(subject, str) and isinstance(body, str), "subject + body must be strings"
    # Reject CR/LF in header fields — header injection (e.g. a smuggled Bcc:).
    # Raised as GmailError so callers audit + return a clean error, not a 500.
    if any(ch in to or ch in subject for ch in ("\r", "\n")):
        raise GmailError("email headers must not contain newlines")
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii").rstrip("=")


class GmailClient:
    """Acts on one connected Gmail account via the user's OAuth credentials."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        assert client_id and client_secret, "OAuth client creds required"
        assert refresh_token, "refresh token required"
        self._cid = client_id
        self._secret = client_secret
        self._refresh = refresh_token
        self._access = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()  # serialize token refresh across threads

    def _token(self) -> str:
        """Return a valid access token, refreshing via the refresh token if stale."""
        with self._lock:
            if self._access and time.time() < self._expires_at - _TOKEN_SKEW:
                return self._access
            try:
                token, expires_in = email_oauth.refresh_access_token(self._cid, self._secret, self._refresh)
            except email_oauth.EmailOAuthError as exc:  # surface as GmailError -> 502 + audit
                raise GmailError(f"token refresh failed: {exc}") from None
            assert isinstance(token, str) and token, "refresh must return an access token"
            assert expires_in > 0, "token lifetime must be positive"
            self._access = token
            self._expires_at = time.time() + expires_in
            return token

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        """One authenticated Gmail call; raise GmailError on any non-2xx."""
        assert method in ("GET", "POST"), "unsupported method"
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            with httpx.Client(timeout=_TIMEOUT, trust_env=False) as client:
                resp = client.request(method, f"{_API}{path}", headers=headers, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise GmailError(f"gmail request failed: {exc}") from None
        if resp.status_code == 401:
            raise GmailError("Gmail rejected the token (revoked or expired) — reconnect the account")
        if resp.status_code >= 400:
            raise GmailError(f"gmail returned {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def email_address(self) -> str:
        """Return the connected mailbox address (also validates the token)."""
        addr = self._request("GET", "/profile").get("emailAddress", "")
        assert addr, "Gmail profile returned no address"
        return addr

    def list_recent(self, max_results: int = 10) -> list[dict]:
        """Return recent inbox messages (id, from, subject, date, snippet)."""
        cap = min(max(int(max_results), 1), _MAX_LIST)
        listing = self._request("GET", "/messages", params={"maxResults": cap, "q": "in:inbox"})
        assert isinstance(listing, dict), "listing response must be a dict"
        out: list[dict] = []
        for ref in (listing.get("messages") or [])[:cap]:  # bounded by cap
            mid = ref.get("id") if isinstance(ref, dict) else None
            if not mid:  # skip a malformed list entry rather than 500 (P10 #7)
                continue
            msg = self._request(
                "GET", f"/messages/{mid}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            )
            hdr = _headers(msg)
            out.append({
                "id": msg.get("id"), "thread_id": msg.get("threadId"),
                "from": hdr.get("from", ""), "subject": hdr.get("subject", ""),
                "date": hdr.get("date", ""), "snippet": msg.get("snippet", ""),
            })
        return out

    def read_message(self, msg_id: str) -> dict:
        """Return one message with its decoded text/plain body."""
        assert msg_id, "message id required"
        msg = self._request("GET", f"/messages/{msg_id}", params={"format": "full"})
        hdr = _headers(msg)
        return {
            "id": msg.get("id"), "thread_id": msg.get("threadId"),
            "from": hdr.get("from", ""), "subject": hdr.get("subject", ""),
            "date": hdr.get("date", ""), "body": _extract_body(msg.get("payload") or {}),
        }

    def send(self, to: str, subject: str, body: str) -> dict:
        """Send a plain-text email; return the created message + thread ids."""
        assert to and "@" in to, "a valid recipient is required"
        res = self._request("POST", "/messages/send", json_body={"raw": _build_raw(to, subject, body)})
        return {"id": res.get("id"), "thread_id": res.get("threadId")}
