"""Tests for the MCP server bearer auth + access-token management (E1).

The full MCP protocol handshake is verified live with a real MCP client; here we
cover the security boundary that must hold deterministically: no token => 401,
token routes are unlock-gated, and a valid token passes the auth layer (so the
MCP transport — not the auth wrapper — handles the request).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import mcp_server

# B8: reading the raw MCP token is Desktop-local only; the WebRTC bridge strips this marker,
# so a bridged-in (paired-phone) request lacks it and is refused with 403.
_LOCAL = {"X-SB-Local": "1"}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _unlock(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})


def test_token_routes_require_unlock(client: TestClient) -> None:
    # The whole /api/mcp/token verb-set is Desktop-local; with the marker present each reaches the unlock check.
    assert client.get("/api/mcp").status_code == 423
    assert client.get("/api/mcp/token", headers=_LOCAL).status_code == 423
    assert client.post("/api/mcp/token", headers=_LOCAL).status_code == 423
    assert client.delete("/api/mcp/token", headers=_LOCAL).status_code == 423


def test_token_mint_read_revoke(client: TestClient) -> None:
    _unlock(client)
    assert client.get("/api/mcp").json() == {"endpoint": "/mcp", "enabled": False}
    assert client.get("/api/mcp/token", headers=_LOCAL).json() == {"token": None}

    token = client.post("/api/mcp/token", headers=_LOCAL).json()["token"]
    assert token and len(token) >= 32
    assert client.get("/api/mcp/token", headers=_LOCAL).json() == {"token": token}
    assert client.get("/api/mcp").json()["enabled"] is True

    assert client.delete("/api/mcp/token", headers=_LOCAL).json() == {"ok": True}
    assert client.get("/api/mcp/token", headers=_LOCAL).json() == {"token": None}


def test_token_routes_refused_from_bridge(client: TestClient) -> None:
    # B8: a bridge-origin request (no X-SB-Local — the bridge strips it) can neither READ, MINT,
    # nor REVOKE the token, even unlocked. Otherwise a paired phone could exfiltrate the raw token
    # (read/mint returns it in the body) or DoS the MCP integration (revoke rotates it away).
    _unlock(client)
    client.post("/api/mcp/token", headers=_LOCAL)  # a token exists, minted Desktop-locally
    assert client.get("/api/mcp/token").status_code == 403     # read refused
    assert client.post("/api/mcp/token").status_code == 403     # mint refused
    assert client.delete("/api/mcp/token").status_code == 403   # revoke refused
    assert client.get("/api/mcp/token", headers=_LOCAL).json()["token"] is not None  # operator's token intact


def test_mcp_rejects_without_token(client: TestClient) -> None:
    # locked + no token => the MCP endpoint is disabled (401, never reachable).
    assert client.get("/mcp/").status_code == 401
    assert client.post("/mcp/", json={}).status_code == 401


def test_mcp_rejects_wrong_token(client: TestClient) -> None:
    _unlock(client)
    client.post("/api/mcp/token", headers=_LOCAL)
    r = client.get("/mcp/", headers={"Authorization": "Bearer not-the-real-token"})
    assert r.status_code == 401


def test_mcp_valid_token_is_delegated_to_the_mcp_app(client: TestClient) -> None:
    _unlock(client)
    token = client.post("/api/mcp/token", headers=_LOCAL).json()["token"]
    # Contrast proves the auth GATE, not just "not 401": no token -> OUR layer 401s;
    # a valid token -> the request is DELEGATED to the real FastMCP app, which rejects this
    # (intentionally malformed) GET at its OWN transport layer with a non-401 4xx. A 5xx
    # would mean we crashed; a 401 would mean we never delegated.
    assert client.get("/mcp/").status_code == 401
    r = client.get("/mcp/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code != 401, "valid token must pass our auth wrapper"
    assert 400 <= r.status_code < 500, f"expected a FastMCP transport 4xx, got {r.status_code}"


def test_only_readonly_kb_tools_exposed() -> None:
    # Credential firewall: the server exposes read-only KB tools and nothing else.
    import asyncio

    server = mcp_server.build_server(lambda: None)
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"kb_search", "kb_read"}


def test_mcp_kb_search_is_lexical_and_gateway_free() -> None:
    # Regression: the MCP kb_search must work with the gateway unavailable —
    # it is lexical only (no Ollama dependency), preserving always-on access.
    import asyncio

    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000.kb import KnowledgeBase
    from smartbrain_3000.secrets import gen_master_key

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    kb = KnowledgeBase(conn, gen_master_key())
    kb.add("Notes", "buy milk and eggs")
    server = mcp_server.build_server(lambda: kb)
    result = asyncio.run(server.call_tool("kb_search", {"query": "milk"}))
    assert "milk" in str(result).lower()
