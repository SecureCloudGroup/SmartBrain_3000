"""Knowledge base HTTP API (requires unlock).

Documents are encrypted at rest; these endpoints add / list / fetch / search /
delete them. The `search` route is declared before `{doc_id}` so it is not
captured as an id.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import gateway, ingest
from .kb import KnowledgeBase

router = APIRouter()
log = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 25_000_000  # cap on an uploaded file (matches the URL-ingest cap)


class DocIn(BaseModel):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)


class UrlIn(BaseModel):
    url: str = Field(min_length=1)


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)


def _kb(request: Request) -> KnowledgeBase:
    knowledge = getattr(request.app.state, "kb", None)
    if knowledge is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return knowledge


@router.post("/api/kb")
def add_doc(request: Request, body: DocIn) -> dict[str, str]:
    """Store an (encrypted) document; return its id."""
    return {"id": _kb(request).add(body.title, body.content)}


@router.get("/api/kb")
def list_docs(request: Request) -> dict:
    """List documents (id, title, timestamps) — never full content."""
    return {"documents": _kb(request).list_docs()}


@router.patch("/api/kb/{doc_id}")
def rename_doc(request: Request, doc_id: str, body: RenameIn) -> dict[str, bool]:
    """Rename a document (default URL/file names are often cryptic). Re-embeds best-effort
    so the new title is reflected in semantic search; a reindex backfills otherwise."""
    knowledge = _kb(request)
    doc = knowledge.get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title must not be blank")
    knowledge.rename(doc_id, title)
    try:  # best-effort: keep the title-prefixed embeddings current
        ingest.embed_doc(knowledge, doc_id, title, doc["content"], gateway.embed_model(request.app.state.dbx))
    except Exception as exc:  # embeddings optional; reindex can backfill
        log.info("re-embed after rename skipped for %s: %s", doc_id, exc)
    return {"ok": True}


@router.get("/api/kb/search")
def search_docs(request: Request, q: str, mode: str = "lexical") -> dict:
    """Search the KB. mode=lexical (default, no gateway) or semantic.

    Semantic mode embeds ``q`` via the gateway and ranks by cosine similarity;
    if the gateway/Ollama is unavailable it falls back to lexical and sets
    ``degraded: true`` so the switch is observable, never silent.
    """
    knowledge = _kb(request)
    if not q.strip():
        raise HTTPException(status_code=400, detail="query 'q' is required")
    if mode not in ("lexical", "semantic"):
        raise HTTPException(status_code=400, detail="mode must be 'lexical' or 'semantic'")
    if mode == "lexical":
        return {"results": knowledge.search(q)}
    model = gateway.embed_model(request.app.state.dbx)
    try:
        vector = gateway.embed(q, model)
    except Exception as exc:  # gateway/Ollama unavailable — degrade to lexical
        log.warning("semantic search fell back to lexical: %s", exc)
        return {"results": knowledge.search(q), "degraded": True}
    return {"results": knowledge.semantic_search(vector, model), "degraded": False}


@router.post("/api/kb/reindex")
def reindex(request: Request) -> dict:
    """Backfill embeddings for docs missing one or using a stale model.

    Best-effort and bounded: one gateway hiccup is logged and counted, not
    fatal. Re-runnable to converge the corpus once Ollama is available.
    """
    knowledge = _kb(request)
    model = gateway.embed_model(request.app.state.dbx)
    embedded, skipped, failed, error = ingest.reindex_pending(knowledge, model)
    return {"embedded": embedded, "skipped": skipped, "failed": failed, "error": error}


@router.post("/api/kb/ingest-url")
def ingest_url(request: Request, body: UrlIn) -> dict:
    """Fetch a URL (SSRF-guarded), extract its text (PDF/HTML/text), store + embed it."""
    knowledge = _kb(request)
    try:
        return ingest.ingest_url(knowledge, body.url.strip())
    except ingest.IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/api/kb/upload")
async def upload_doc(request: Request, filename: str) -> dict:
    """Ingest an uploaded file (raw request body) by its filename; store + embed it."""
    knowledge = _kb(request)
    if not filename.strip():
        raise HTTPException(status_code=400, detail="filename query parameter required")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")  # reject before buffering
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        title, text = ingest.from_file(filename, data)
        return ingest.store(knowledge, title, text)
    except ingest.IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/api/kb/{doc_id}")
def get_doc(request: Request, doc_id: str) -> dict:
    """Return a single decrypted document."""
    doc = _kb(request).get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


@router.delete("/api/kb/{doc_id}")
def delete_doc(request: Request, doc_id: str) -> dict[str, bool]:
    """Delete a document."""
    _kb(request).delete(doc_id)
    return {"ok": True}
