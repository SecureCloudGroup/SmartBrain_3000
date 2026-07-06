"""MCP access-token management (requires unlock).

The MCP server (``mcp_server.py``) is disabled until an access token exists: with
no token, every MCP request is rejected 401. These routes let the unlocked app
mint, read, and revoke that bearer token so an external tool (e.g. OpenClaw) can
be granted read-only access to the Knowledge base. The token is stored encrypted
in the secret store like any other secret.
"""

from __future__ import annotations

import secrets as token_lib

from fastapi import APIRouter, HTTPException, Request

from . import account, mcp_server

router = APIRouter()

_TOKEN_BYTES = 32  # ~256-bit URL-safe access token


def _store(request: Request):
    """Return the unlocked SecretStore, or raise 423 if locked."""
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


@router.get("/api/mcp")
def mcp_info(request: Request) -> dict[str, object]:
    """Report whether MCP access is enabled (a token exists) and the endpoint."""
    store = _store(request)
    has_token = bool(store.get(mcp_server.MCP_TOKEN_KEY))
    return {"endpoint": "/mcp", "enabled": has_token}


@router.get("/api/mcp/token")
def get_token(request: Request) -> dict[str, str | None]:
    """Return the current MCP access token so the user can copy it (or null)."""
    account._require_desktop_local(request)  # the raw token is Desktop-local only
    return {"token": _store(request).get(mcp_server.MCP_TOKEN_KEY)}


@router.post("/api/mcp/token")
def new_token(request: Request) -> dict[str, str]:
    """Mint a fresh MCP access token, replacing any existing one."""
    account._require_desktop_local(request)  # minting returns the raw token in the body -> Desktop-local only
    store = _store(request)
    token = token_lib.token_urlsafe(_TOKEN_BYTES)
    store.put(mcp_server.MCP_TOKEN_KEY, token)
    assert store.get(mcp_server.MCP_TOKEN_KEY) == token, "token must persist"
    return {"token": token}


@router.delete("/api/mcp/token")
def revoke_token(request: Request) -> dict[str, bool]:
    """Revoke MCP access by deleting the token (disables the MCP server)."""
    account._require_desktop_local(request)  # Desktop-local only: a bridged phone must not rotate/revoke the token
    _store(request).delete(mcp_server.MCP_TOKEN_KEY)
    return {"ok": True}
