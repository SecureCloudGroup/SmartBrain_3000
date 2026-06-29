"""Gmail OAuth2 (loopback, PKCE) — clean re-implementation of the v2 flow (H6).

All-local + single-user: the user creates their OWN Google OAuth client (Desktop
or Web type) in Google Cloud Console, registers the loopback redirect below, and
supplies the client_id + client_secret. We run the authorization-code flow with
PKCE, exchange for a refresh token, and store it encrypted in the secret store.

No Google SDK — raw httpx to the FIXED Google endpoints (never user-supplied
URLs, so the SSRF guard that protects ``web_fetch`` is not needed here). The
redirect target is loopback, so the auth code never leaves the user's machine.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets as _secrets
from urllib.parse import urlencode

import httpx

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Minimal scopes: read the inbox + send. (No gmail.modify — no archive/mark-read.)
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)
_DEFAULT_REDIRECT = "http://localhost:33000/api/email/oauth/callback"
_TIMEOUT = 15.0


class EmailOAuthError(RuntimeError):
    """Raised when an OAuth request to Google fails or returns no refresh token."""


def redirect_uri() -> str:
    """The loopback redirect URI the user must register on their OAuth client."""
    uri = os.environ.get("SMARTBRAIN_OAUTH_REDIRECT", _DEFAULT_REDIRECT)
    # Hard raise, NOT assert (asserts are stripped under `python -O`): the loopback redirect is the
    # local-first guarantee that Google delivers the auth code only to the user's own machine.
    if not (uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1")):
        raise EmailOAuthError("SMARTBRAIN_OAUTH_REDIRECT must be a loopback URI (http://localhost or http://127.0.0.1)")
    return uri


def _challenge(verifier: str) -> str:
    """Return the S256 PKCE code_challenge for a verifier (base64url, no pad)."""
    assert len(verifier) >= 43, "PKCE verifier must be >= 43 chars"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_auth_url(client_id: str) -> tuple[str, str, str]:
    """Build the Google consent URL; return (url, state, code_verifier)."""
    assert client_id, "client_id required"
    state = _secrets.token_urlsafe(24)
    verifier = _secrets.token_urlsafe(64)  # ~86 chars, within the 43–128 PKCE range
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",        # force the consent screen so a refresh token is returned
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}", state, verifier


def _post_token(data: dict) -> dict:
    """POST to Google's token endpoint and return the parsed JSON (fail loud)."""
    assert "grant_type" in data, "grant_type required"
    try:
        # trust_env=False: ignore HTTP(S)_PROXY so the token request can't be redirected.
        with httpx.Client(timeout=_TIMEOUT, trust_env=False) as client:
            resp = client.post(GOOGLE_TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        raise EmailOAuthError(f"token request failed: {exc}") from None
    if resp.status_code != 200:
        raise EmailOAuthError(f"token endpoint returned {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    assert isinstance(body, dict), "token response must be a JSON object"
    return body


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str) -> dict:
    """Exchange an authorization code for tokens; require a refresh token."""
    assert client_id and client_secret and code and verifier, "all OAuth params required"
    body = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri(),
    })
    if not body.get("refresh_token"):
        raise EmailOAuthError("Google returned no refresh_token; revoke prior access and reconnect")
    return {
        "refresh_token": body["refresh_token"],
        "access_token": body.get("access_token", ""),
        "expires_in": int(body.get("expires_in", 0)),
    }


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, int]:
    """Trade a refresh token for a fresh access token; return (token, expires_in)."""
    assert client_id and client_secret and refresh_token, "client creds + refresh token required"
    body = _post_token({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })
    token = body.get("access_token")
    if not token:
        raise EmailOAuthError("refresh returned no access_token (revoked? reconnect)")
    # Clamp the lifetime so a missing/0/negative expires_in can't cause a refresh
    # storm (every call would otherwise see the token as already expired).
    return token, max(int(body.get("expires_in", 0)), 60)
