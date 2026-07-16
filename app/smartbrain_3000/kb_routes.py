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


def _refuse_if_vault_owned(request: Request, doc_id: str) -> None:
    """409 when the document is an imported vault's copy (any import-origin membership).

    An update from the vault's publisher may later REPLACE vault-owned documents, so letting the
    user rename or delete one would set up a silent clobber of what they believe is theirs. The
    guarantee is by construction: a doc is editable only when no import-origin membership exists —
    Detach flips the membership to owner-origin, which both unblocks edits and makes every future
    update skip the doc. Owner-origin memberships never block anything.
    """
    store = getattr(request.app.state, "vaults", None)
    if store is None:
        return  # defense in depth: account._set_unlocked always sets kb and vaults together
    info = store.import_provenance(doc_id)
    if info is not None:
        raise HTTPException(
            status_code=409,
            detail=f"this document came from the imported vault “{info['name']}” and a "
            "vault update may replace it — use Detach in that vault's member list to make "
            "this copy yours first",
        )


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
    _refuse_if_vault_owned(request, doc_id)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title must not be blank")
    knowledge.rename(doc_id, title)
    try:  # best-effort: keep the title-prefixed embeddings current
        ingest.embed_doc(knowledge, doc_id, title, doc["content"], gateway.embed_model(request.app.state.dbx))
    except Exception as exc:  # embeddings optional; reindex can backfill
        log.info("re-embed after rename skipped for %s: %s", doc_id, exc)
    return {"ok": True}


_SEARCH_MODES = ("hybrid", "lexical", "semantic")
_MAX_SEARCH_LIMIT = 50


def _scope(request: Request, vault_id: str | None) -> set[str] | None:
    """Restrict a search to one vault's documents. None = search everything.

    An EMPTY vault must scope to an empty set, not to "no scope" — otherwise searching an empty
    vault would silently search the whole library, which is the opposite of what was asked.
    """
    if not vault_id:
        return None
    store = getattr(request.app.state, "vaults", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    if store.get(vault_id) is None:
        raise HTTPException(status_code=404, detail="vault not found")
    return set(store.document_ids(vault_id))


@router.get("/api/kb/search")
def search_docs(request: Request, q: str, mode: str = "hybrid", limit: int = 10, vault: str | None = None) -> dict:
    """Search the KB. mode=hybrid (default), lexical, or semantic.

    HYBRID is the default because keyword and vector search fail in opposite directions: keyword
    nails an exact name or invoice number but misses a paraphrase, vectors do the reverse. Fusing
    them beats either alone, which is why the agent's tool has always merged both — the HTTP API
    just never did.

    Semantic/hybrid embed ``q`` via the gateway; if the gateway is unavailable they fall back to
    lexical and set ``degraded: true`` so the switch is observable, never silent.

    ``vault`` restricts the search to one vault's documents.
    """
    knowledge = _kb(request)
    if not q.strip():
        raise HTTPException(status_code=400, detail="query 'q' is required")
    if mode not in _SEARCH_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(_SEARCH_MODES)}")
    limit = min(max(limit, 1), _MAX_SEARCH_LIMIT)
    scope = _scope(request, vault)
    if mode == "lexical":
        return {"results": knowledge.search(q, limit=limit, scope=scope), "degraded": False}
    model = gateway.embed_model(request.app.state.dbx)
    try:
        vector = gateway.embed(q, model)
    except Exception as exc:  # gateway/embed model unavailable — degrade, but say so
        log.warning("%s search fell back to lexical: %s", mode, exc)
        return {"results": knowledge.search(q, limit=limit, scope=scope), "degraded": True}
    if mode == "semantic":
        return {"results": knowledge.semantic_search(vector, model, limit=limit, scope=scope), "degraded": False}
    return {"results": knowledge.hybrid_search(q, vector, model, limit=limit, scope=scope), "degraded": False}


_REINDEX_BUDGET_SECONDS = 25.0  # a request must RETURN; the background indexer finishes the rest


@router.post("/api/kb/reindex")
def reindex(request: Request) -> dict:
    """Backfill embeddings for docs missing one or using a stale model.

    Bounded by a wall-clock budget so the request always returns. It used to run the whole backlog
    synchronously (default limit 1000 documents, each up to 64 sequential embeds), which on a large
    corpus is an HTTP request that runs for hours. Whatever isn't finished here is drained by the
    background indexer, so `pending` tells the caller how much is left rather than pretending it's done.

    Best-effort: one gateway hiccup is logged and counted, not fatal.
    """
    knowledge = _kb(request)
    model = gateway.embed_model(request.app.state.dbx)
    embedded, skipped, failed, error = ingest.reindex_pending(
        knowledge, model, budget_seconds=_REINDEX_BUDGET_SECONDS
    )
    return {
        "embedded": embedded,
        "skipped": skipped,
        "failed": failed,
        "error": error,
        "pending": knowledge.docs_pending_embedding(model),  # still to do; the indexer will get it
    }


@router.get("/api/kb/index-status")
def index_status(request: Request) -> dict:
    """How much of the knowledge base is semantically indexed.

    Uploads no longer block on embedding, so the UI needs to be able to say "indexing 12 of 40"
    instead of silently looking finished while semantic search can't yet see the new documents.
    """
    knowledge = _kb(request)
    model = gateway.embed_model(request.app.state.dbx)
    total = knowledge.count_docs()
    pending = knowledge.docs_pending_embedding(model)
    return {"total": total, "pending": pending, "indexed": max(0, total - pending), "model": model}


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
        title, text, meta = ingest.from_file(filename, data)
        # embed=False: return as soon as the document is STORED. Embedding a long document is dozens
        # of sequential model calls that serialize on a local model, so embedding inline held each
        # upload's HTTP request open for as long as it took — a multi-file drop was minutes of
        # blocking. The document is keyword-searchable immediately; the background indexer adds the
        # vectors within seconds, and /api/kb/index-status reports the progress.
        return ingest.store(knowledge, title, text, meta, embed=False)
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
    """Delete a document, and drop it from every vault that held it.

    Without the second step a vault would keep pointing at a document that no longer exists — a
    ghost member that inflates its count and would be exported as a missing file.
    """
    knowledge = _kb(request)
    _refuse_if_vault_owned(request, doc_id)
    knowledge.delete(doc_id)
    store = getattr(request.app.state, "vaults", None)
    if store is not None:
        store.forget_document(doc_id)
    return {"ok": True}
