"""MCP server exposing the Knowledge base read-only to external tools.

SmartBrain runs an MCP (Streamable HTTP) server so tools such as OpenClaw can
**search and read the user's Knowledge base — and nothing else**. It is served
on the app's loopback port at ``/mcp``, requires a bearer access token, and only
works while the app is unlocked. No provider keys or secrets are ever exposed
(credential firewall): the only tools are read-only KB access.

The server is built per app instance (``build_server``) so each FastAPI app owns
its own session manager — the MCP transport's ``run()`` is once-per-instance.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from . import gateway

log = logging.getLogger(__name__)

MCP_TOKEN_KEY = "mcp:access_token"  # secret-store key holding the access token


def build_server(get_kb: Callable[[], object]) -> FastMCP:
    """Create a FastMCP server exposing the read-only Knowledge tools.

    ``get_kb`` returns the unlocked KnowledgeBase (or None while locked); it is
    read on each call so the tools always see current lock state.
    """
    assert callable(get_kb), "get_kb must be callable"
    server = FastMCP("SmartBrain Knowledge", stateless_http=True, streamable_http_path="/")

    def _knowledge():
        knowledge = get_kb()
        if knowledge is None:
            raise RuntimeError("SmartBrain is locked; unlock it to use the knowledge base")
        return knowledge

    @server.tool(
        description="Search the user's SmartBrain knowledge base by meaning (semantic, "
        "lexical fallback); returns matching documents as {id, title, snippet, score}."
    )
    def kb_search(query: str, limit: int = 5) -> list[dict]:
        """Read-only semantic search over the knowledge base; degrades to lexical."""
        assert query, "query required"
        assert limit >= 1, "limit must be >= 1"
        knowledge = _knowledge()
        capped = max(1, min(limit, 20))
        model = gateway.embed_model(getattr(knowledge, "conn", None))
        try:
            vector = gateway.embed(query, model)
        except Exception as exc:  # embed model unavailable — degrade to lexical, observably
            log.warning("MCP kb_search fell back to lexical: %s", exc)
            return knowledge.search(query, limit=capped)
        return knowledge.semantic_search(vector, model, limit=capped)

    @server.tool(description="Read one SmartBrain knowledge-base document by its id.")
    def kb_read(doc_id: str) -> dict:
        """Read-only fetch of a single document."""
        assert doc_id, "doc_id required"
        doc = _knowledge().get(doc_id)
        if doc is None:
            raise ValueError(f"document not found: {doc_id}")
        return doc

    return server


async def _send_unauthorized(send) -> None:
    """Emit a 401 JSON response for a missing/invalid MCP token."""
    body = b'{"error":"unauthorized: valid MCP bearer token required (unlock the app first)"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def auth_wrapped_app(server: FastMCP, get_token: Callable[[], str | None]):
    """Wrap the server's Streamable-HTTP ASGI app with bearer-token auth.

    ``get_token`` returns the configured access token (None while locked or
    unset); with no token every request is rejected, so MCP is off by default.
    """
    assert callable(get_token), "get_token must be callable"
    inner = server.streamable_http_app()

    async def auth_app(scope, receive, send):
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode()
        expected = get_token()
        if not expected:
            await _send_unauthorized(send)
            return
        if not hmac.compare_digest(provided.encode(), f"Bearer {expected}".encode()):
            await _send_unauthorized(send)
            return
        await inner(scope, receive, send)

    return auth_app
