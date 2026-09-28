"""HTTP surface for the NI Library page (R9): browse/search the installed Library, and the user's own
local sources (add / list / delete). See ``library_index`` for the pack and storage rules.

Every route rides SessionGuard like the rest of /api. The pack is public catalog data, but the page is
part of the unlocked app, so reads 423 while locked (local sources need the vault regardless).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db, library_client, library_index, tools

router = APIRouter()


class LocalSourceIn(BaseModel):
    """The Add-a-source form. Values only — a key is never typed here (``needs_key`` flags it)."""

    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000)
    description: str = Field(default="", max_length=600)
    category: str = Field(min_length=1, max_length=80)
    access_kind: str = Field(default="http_json", max_length=20)
    needs_key: bool = False
    # also suggest it to the SmartBrain Library (the address template and description, never a value)
    suggest: bool = False


def _unlocked_ni(request: Request):
    store = getattr(request.app.state, "ni", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


def _index(request: Request) -> library_index.LibraryIndex:
    _unlocked_ni(request)
    idx = getattr(request.app.state, "library_index", None)
    if idx is None:
        idx = library_index.LibraryIndex(db.resolve_db_path().parent)
        request.app.state.library_index = idx
    return idx


def _known_categories(idx: library_index.LibraryIndex) -> set[str]:
    return {f"{c['id']}/{s['id']}" for c in idx.taxonomy() for s in c["subcategories"]}


@router.get("/api/library/status")
def library_status(request: Request) -> dict:
    return _index(request).status()


@router.post("/api/library/install")
def library_install(request: Request) -> dict:
    """Download + verify + unpack the pinned pack (first use). Idempotent."""
    idx = _index(request)
    try:
        idx.install()
    except library_index.LibraryIndexError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return idx.status()


def _require_installed(idx: library_index.LibraryIndex) -> None:
    if not idx.installed():
        raise HTTPException(status_code=409, detail="the Library isn't downloaded yet")


@router.get("/api/library/taxonomy")
def library_taxonomy(request: Request) -> dict:
    idx = _index(request)
    _require_installed(idx)
    return {"categories": [{**c, "subcategories": [{k: v for k, v in s.items() if k != "keywords"}
                                                     for s in c["subcategories"]]} for c in idx.taxonomy()]}


@router.get("/api/library/sources")
def library_sources(request: Request, q: str = Query("", max_length=200), category: str = Query("", max_length=40),
                    subcategory: str = Query("", max_length=40), tier: str = Query("", max_length=20),
                    status: str = Query("", max_length=20), offset: int = Query(0, ge=0, le=100_000),
                    limit: int = Query(20, ge=1, le=library_index.MAX_PAGE)) -> dict:
    idx = _index(request)
    _require_installed(idx)
    out = idx.search(q, category, subcategory, tier, status, offset, limit)
    # the user's own sources lead the first page (they said so), searched in memory
    mine = [] if offset or (tier and tier != "local") else library_index.LocalSources(
        _unlocked_ni(request)).search(q, category, subcategory)
    out["local"] = [_local_row(r) for r in mine]
    return out


def _local_row(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "description": r["description"], "provider": r["provider"]["name"],
            "authority": "community", "tier": "local", "geo": "local", "access_kind": r["access"]["kind"],
            "auth": r["access"]["auth"], "terms": "unverified", "cadence": "irregular", "status": "unvalidated",
            "categories": r["categories"], "url_template": r["access"]["url_template"]}


@router.get("/api/library/sources/{source_id}")
def library_source(request: Request, source_id: str) -> dict:
    idx = _index(request)
    if source_id.startswith("local-"):
        for r in library_index.LocalSources(_unlocked_ni(request)).list():
            if r["id"] == source_id:
                return r
        raise HTTPException(status_code=404, detail="no such source")
    _require_installed(idx)
    rec = idx.get(source_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="no such source")
    return rec


@router.get("/api/library/local")
def library_local_list(request: Request) -> dict:
    return {"sources": [_local_row(r) for r in library_index.LocalSources(_unlocked_ni(request)).list()]}


@router.post("/api/library/local")
def library_local_add(request: Request, body: LocalSourceIn) -> dict:
    idx = _index(request)
    _require_installed(idx)
    store = _unlocked_ni(request)
    try:
        record = library_index.validate_local(body.model_dump(), _known_categories(idx))
        library_index.LocalSources(store).add(record)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    suggested = bool(body.suggest) and library_client.queue_suggestion(store, record)
    request.app.state.audit.append(
        "user", "library_local_add", "reviewed", "executed", True,
        args_summary=tools.summarize({"host": record["provider"]["name"], "category": record["categories"][0]}),
        result_summary=tools.summarize({"id": record["id"]}))
    return {**_local_row(record), "suggested": suggested}


@router.delete("/api/library/local/{source_id}")
def library_local_delete(request: Request, source_id: str) -> dict:
    store = _unlocked_ni(request)
    if not library_index.LocalSources(store).delete(source_id):
        raise HTTPException(status_code=404, detail="no such source")
    request.app.state.audit.append("user", "library_local_delete", "reviewed", "executed", True,
                                   args_summary=tools.summarize({"id": source_id}),
                                   result_summary=tools.summarize({}))
    return {"ok": True}
