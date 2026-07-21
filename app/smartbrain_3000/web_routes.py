"""Web-search provider configuration API (engine choice + SearXNG URL).

Provider API keys (Brave/Tavily) ride the existing ``/api/secrets`` endpoint under
the ``websearch:<name>:api_key`` namespace — this router persists only the
non-secret choices in the ``meta`` KV, and reports which providers are configured
so the Settings page can show live state. Requires unlock (the choices shape
egress made on the user's behalf).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, search

router = APIRouter()
log = logging.getLogger(__name__)

_MAX_URL = 500


class WebSearchConfig(BaseModel):
    engine: str = Field(default="auto")
    searxng_url: str = Field(default="", max_length=_MAX_URL)


def _require_unlocked(request: Request):
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


@router.get("/api/websearch")
def get_websearch(request: Request) -> dict:
    """Current web-search config + which providers are usable right now."""
    store = _require_unlocked(request)
    conn = request.app.state.dbx
    service = search.service_from(conn, store.get)
    return {
        "engine": (db.meta_get(conn, search.META_ENGINE) or "auto"),
        "searxng_url": db.meta_get(conn, search.META_SEARXNG_URL) or "",
        "configured": service.configured(),
        "engines": list(search.ENGINES),
    }


@router.put("/api/websearch")
def put_websearch(request: Request, body: WebSearchConfig) -> dict:
    """Persist engine choice + SearXNG URL (keys go through /api/secrets)."""
    _require_unlocked(request)
    if body.engine not in search.ENGINES:
        raise HTTPException(status_code=400, detail=f"unknown engine: {body.engine}")
    url = (body.searxng_url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="SearXNG URL must be http(s)")
    conn = request.app.state.dbx
    db.meta_set(conn, search.META_ENGINE, body.engine)
    db.meta_set(conn, search.META_SEARXNG_URL, url)
    return {"ok": True}
