"""Email (Gmail) HTTP API (requires unlock).

Connect flow (loopback OAuth, user-supplied client creds):
  1. POST /api/email/connect {client_id, client_secret} -> stores creds, returns
     the Google consent URL (PKCE state held in app.state until the callback).
  2. the browser completes consent and Google redirects to
     GET /api/email/oauth/callback?code&state -> exchange + store refresh token.
  3. DELETE /api/email/disconnect removes all Gmail credentials.

Reads (list/read) and a user-initiated send are direct, audited actions. The
assistant/scheduler send via the `email_send` tool, which parks for approval.
"""

from __future__ import annotations

import hmac
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import email_account, email_oauth, gmail, tools

router = APIRouter()
log = logging.getLogger(__name__)

_PENDING_TTL = 600  # seconds an in-flight OAuth handshake stays valid


class ConnectIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=400)
    client_secret: str = Field(min_length=1, max_length=400)


class SendIn(BaseModel):
    to: str = Field(min_length=3, max_length=320)
    subject: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=50000)


def _store(request: Request):
    """Return the unlocked SecretStore, or raise 423 if locked."""
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


def _client(request: Request) -> gmail.GmailClient:
    """Return the live GmailClient, or raise 409 if no account is connected."""
    client = getattr(request.app.state, "email", None)
    if client is None:
        raise HTTPException(status_code=409, detail="no email account connected")
    return client


def _gmail_http(exc: gmail.GmailError) -> HTTPException:
    """Map a Gmail failure to HTTP: 401 when the stored refresh token is dead (the
    UI shows a Reconnect banner), 502 for transient/API errors. gmail.py always
    prefixes a refresh failure with 'token refresh failed' (e.g. invalid_grant from
    an OAuth client left in 'Testing' mode)."""
    detail = str(exc)
    code = 401 if detail.startswith("token refresh failed") else 502
    return HTTPException(status_code=code, detail=detail)


@router.get("/api/email/status")
def email_status(request: Request) -> dict:
    """Report connection state + the loopback redirect URI to register on Google."""
    return {**email_account.status(_store(request)), "redirect_uri": email_oauth.redirect_uri()}


@router.post("/api/email/connect")
def email_connect(request: Request, body: ConnectIn) -> dict[str, str]:
    """Return the Google consent URL. Creds are held in memory until OAuth succeeds.

    Nothing is persisted here: the candidate client creds live only in the
    in-flight handshake, so an aborted/forged connect cannot overwrite a working
    connection (the existing refresh token is untouched).
    """
    _store(request)  # require unlock
    url, state, verifier = email_oauth.build_auth_url(body.client_id)
    request.app.state.email_oauth_pending = {
        "state": state, "verifier": verifier, "created_at": time.time(),
        "client_id": body.client_id, "client_secret": body.client_secret,
    }
    return {"auth_url": url}


@router.post("/api/email/reconnect")
def email_reconnect(request: Request) -> dict[str, str]:
    """Re-run OAuth with the already-stored client creds — for when the refresh
    token died (e.g. an OAuth client left in 'Testing' mode expires it after 7 days).
    The user just re-grants on Google; they never re-enter the client id/secret."""
    store = _store(request)
    client_id, client_secret = email_account.client_creds(store)
    if not (client_id and client_secret):
        raise HTTPException(status_code=409, detail="no stored Gmail client to reconnect")
    url, state, verifier = email_oauth.build_auth_url(client_id)
    request.app.state.email_oauth_pending = {
        "state": state, "verifier": verifier, "created_at": time.time(),
        "client_id": client_id, "client_secret": client_secret,
    }
    return {"auth_url": url}


@router.get("/api/email/oauth/callback")
def email_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None) -> RedirectResponse:
    """Finish OAuth: verify state, exchange the code, persist creds + refresh token."""
    pending = getattr(request.app.state, "email_oauth_pending", None)
    request.app.state.email_oauth_pending = None  # single-use: consume on first callback, any outcome
    if error:
        return RedirectResponse(f"/email?error={error}", status_code=303)
    if not code or not state or pending is None:
        raise HTTPException(status_code=400, detail="no pending authorization")
    if time.time() - pending["created_at"] > _PENDING_TTL:
        raise HTTPException(status_code=400, detail="authorization expired; reconnect")
    if not hmac.compare_digest(state, pending["state"]):
        raise HTTPException(status_code=400, detail="state mismatch")
    store = _store(request)
    client_id, client_secret = pending["client_id"], pending["client_secret"]
    try:
        tokens = email_oauth.exchange_code(client_id, client_secret, code, pending["verifier"])
        client = gmail.GmailClient(client_id, client_secret, tokens["refresh_token"])
        address = client.email_address()
    except (email_oauth.EmailOAuthError, gmail.GmailError) as exc:
        log.warning("email connect failed: %s", exc)
        return RedirectResponse("/email?error=connect_failed", status_code=303)
    email_account.save_client_creds(store, client_id, client_secret)  # commit only on success
    email_account.save_connection(store, tokens["refresh_token"], address)
    request.app.state.email = client  # live for the tool + reads immediately
    return RedirectResponse("/email?connected=1", status_code=303)


@router.delete("/api/email/disconnect")
def email_disconnect(request: Request) -> dict[str, bool]:
    """Remove all Gmail credentials and drop the live client."""
    email_account.disconnect(_store(request))
    request.app.state.email = None
    request.app.state.email_oauth_pending = None
    return {"ok": True}


@router.get("/api/email/messages")
def email_messages(request: Request, limit: int = 10) -> dict:
    """List recent inbox messages (headers + snippet)."""
    try:
        return {"messages": _client(request).list_recent(min(max(limit, 1), 25))}
    except gmail.GmailError as exc:
        raise _gmail_http(exc) from None


@router.get("/api/email/messages/{mid}")
def email_message(request: Request, mid: str) -> dict:
    """Read one message with its decoded body."""
    try:
        return _client(request).read_message(mid)
    except gmail.GmailError as exc:
        raise _gmail_http(exc) from None


@router.post("/api/email/send")
def email_send(request: Request, body: SendIn) -> dict:
    """Send an email (user-initiated; the click is the consent). Audited."""
    client = _client(request)
    audit = request.app.state.audit
    # User-authored send: audit only metadata (the user wrote + sent it knowingly,
    # and Gmail keeps the copy in Sent). The assistant-proposed email_send tool
    # records the full proposed args for AI-action forensics (and shows the body on
    # the approval tile for informed consent) — an intentional, documented split.
    summary = tools.summarize({"to": body.to, "subject": body.subject})
    try:
        result = client.send(body.to, body.subject, body.body)
    except gmail.GmailError as exc:
        audit.append("user", "email_send", "irreversible", "errored", False, args_summary=summary, error=str(exc))
        raise _gmail_http(exc) from None
    audit.append("user", "email_send", "irreversible", "executed", True, args_summary=summary, result_summary=tools.summarize(result))
    return result
