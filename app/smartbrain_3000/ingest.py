"""Extract text from a URL or uploaded file and store it in the Knowledge base.

URL fetches go through the SSRF guard (netguard.safe_fetch_bytes). PDFs are
parsed with pypdf, HTML with trafilatura, and text-family files are decoded
directly. Stored documents are embedded best-effort on add so they are
immediately searchable. Heavy parsers are imported lazily (only on the ingest
path) to keep app startup fast.
"""

from __future__ import annotations

import io
import logging
import os
from urllib.parse import urlparse

import httpx

from . import gateway, kb, netguard

log = logging.getLogger(__name__)

_MAX_TEXT = 1_000_000  # cap on stored text per document (chars)
_MAX_PDF_PAGES = 1000  # bounded page loop
_TEXT_EXT = frozenset({".txt", ".md", ".markdown", ".csv", ".json", ".log", ".rst", ".text"})
_HTML_EXT = frozenset({".html", ".htm"})


class IngestError(Exception):
    """Content could not be fetched or extracted into usable text."""


def _title_from_url(url: str) -> str:
    """Derive a human-ish title from a URL (last path segment, else host)."""
    assert isinstance(url, str), "url must be str"
    assert url, "url required"
    parsed = urlparse(url)
    tail = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
    return tail or parsed.netloc or url


def _looks_binary(data: bytes) -> bool:
    """True if the bytes look binary (a NUL byte in the first chunk) — unsafe as text."""
    assert isinstance(data, (bytes, bytearray)), "data must be bytes"
    return b"\x00" in data[:1024]


def _extract_pdf(data: bytes) -> tuple[str, str]:
    """Return (title, text) from PDF bytes via pypdf (bounded pages + cumulative cap).

    Caps cumulative text DURING extraction so a single text-bomb page can't blow
    memory before _MAX_TEXT is applied. All pypdf access (incl. metadata) is inside
    the try so any parser error surfaces as IngestError, not a 500.
    """
    from pypdf import PdfReader  # lazy: heavy dep, only on the ingest path

    assert isinstance(data, (bytes, bytearray)), "data must be bytes"
    assert data, "data is empty"
    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        total = 0
        for page in reader.pages[:_MAX_PDF_PAGES]:  # bounded page loop
            piece = page.extract_text() or ""
            if total + len(piece) >= _MAX_TEXT:  # cumulative cap = true memory ceiling
                parts.append(piece[: _MAX_TEXT - total])
                break
            parts.append(piece)
            total += len(piece)
        title = (reader.metadata.title if reader.metadata and reader.metadata.title else "") or ""
    except Exception as exc:  # malformed / encrypted PDF
        raise IngestError(f"could not read PDF: {exc}") from None
    return title.strip(), "\n".join(parts).strip()


def _extract_html(html: str, url: str) -> tuple[str, str]:
    """Return (title, main-article-text) from HTML via trafilatura."""
    import trafilatura  # lazy: heavy dep

    assert isinstance(html, str), "html must be str"
    assert isinstance(url, str), "url must be str"  # may be empty (file upload)
    try:
        text = trafilatura.extract(html, url=url or None, include_comments=False) or ""
        meta = trafilatura.extract_metadata(html)
    except Exception as exc:  # malformed / pathological HTML (lxml errors, recursion)
        raise IngestError(f"could not read HTML: {exc}") from None
    title = meta.title if (meta is not None and getattr(meta, "title", None)) else ""
    return (title or "").strip(), text.strip()


def _dispatch(data: bytes, content_type: str, hint_url: str) -> tuple[str, str]:
    """Pick an extractor by magic bytes / content-type, falling back to the URL hint.

    Magic bytes and content-type are authoritative; the ``.pdf`` URL hint is only
    consulted when the content-type is generic (so an HTML page served at a
    ``.pdf`` URL is never handed to the PDF parser).
    """
    assert isinstance(data, (bytes, bytearray)), "data must be bytes"
    assert data, "data is empty"
    ctl = content_type.lower()
    if data[:5] == b"%PDF-" or "pdf" in ctl:
        return _extract_pdf(data)
    if "html" in ctl or "xml" in ctl:
        return _extract_html(data.decode("utf-8", "replace"), hint_url)
    generic = ctl.startswith("application/octet-stream") or not ctl
    if generic and hint_url.lower().endswith(".pdf"):
        return _extract_pdf(data)
    if hint_url.lower().endswith((".html", ".htm")):
        return _extract_html(data.decode("utf-8", "replace"), hint_url)
    if _looks_binary(data):  # don't store an unrecognized binary blob as garbled text
        raise IngestError(
            "unsupported file type — supported: PDF, HTML, and text (.txt, .md, .csv, .json). "
            "Word/Office (.docx, .pptx, .xlsx), images, and other binaries aren't supported."
        )
    return "", data.decode("utf-8", "replace").strip()  # text/* or json — store as-is


def from_url(url: str) -> tuple[str, str]:
    """Fetch a URL (SSRF-guarded) and extract (title, text). Raises IngestError."""
    assert url, "url required"
    try:
        got = netguard.safe_fetch_bytes(url)
    except netguard.FetchError as exc:
        raise IngestError(str(exc)) from None
    title, text = _dispatch(got["content"], got["content_type"], got["final_url"])
    if not text:
        raise IngestError("no readable text found at that URL")
    return (title or _title_from_url(got["final_url"])), text[:_MAX_TEXT]


def from_file(filename: str, data: bytes) -> tuple[str, str]:
    """Extract (title, text) from uploaded file bytes by extension/sniff."""
    assert filename, "filename required"
    assert data, "file is empty"
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf" or data[:5] == b"%PDF-":
        title, text = _extract_pdf(data)
    elif ext in _HTML_EXT:
        title, text = _extract_html(data.decode("utf-8", "replace"), "")
    elif ext in _TEXT_EXT or ext == "":
        if _looks_binary(data):  # an extension-less/text-typed file that is really binary
            raise IngestError("binary file without a recognized text type")
        title, text = "", data.decode("utf-8", "replace").strip()
    else:
        raise IngestError(f"unsupported file type: {ext or 'unknown'}")
    if not text:
        raise IngestError("no readable text found in that file")
    return (title or os.path.basename(filename)), text[:_MAX_TEXT]


def embed_doc(knowledge, doc_id: str, title: str, content: str, model: str) -> None:
    """Chunk title+content, embed each chunk, and store the per-chunk vectors so a
    long document is fully searchable (not just its head)."""
    assert doc_id and title and model, "doc id, title, model required"
    assert knowledge is not None, "knowledge base required"
    chunks = kb.chunk_text(title, content)  # bounded by _MAX_CHUNKS
    with httpx.Client(base_url=gateway.gateway_url(), timeout=15.0) as client:
        vectors = [gateway.embed(c, model, client=client) for c in chunks]
    knowledge.put_embeddings(doc_id, vectors, model)


def reindex_pending(knowledge, model: str, *, limit: int = 1000) -> tuple[int, int, int, str]:
    """Backfill embeddings for docs missing one or on a stale model (best-effort, bounded).

    Returns (embedded, skipped, failed, first_error). Shared by the manual /api/kb/reindex
    route and the scheduler's automated backfill."""
    assert knowledge is not None and model, "knowledge + model required"
    pending = knowledge.docs_needing_embedding(model)[:limit]
    embedded = skipped = failed = 0
    error = ""
    for doc_id in pending:  # bounded by limit
        doc = knowledge.get(doc_id)
        if doc is None:
            skipped += 1
            continue
        try:
            embed_doc(knowledge, doc_id, doc["title"], doc["content"], model)
            embedded += 1
        except Exception as exc:  # keep going on a single failure
            failed += 1
            if not error:
                error = str(getattr(exc, "message", exc))
    return embedded, skipped, failed, error


def store(knowledge, title: str, content: str) -> dict:
    """Add a document to the KB and embed it best-effort; return {id, title, chars}.

    Embedding failure (e.g. Ollama down) never fails the add — the doc is stored
    and a later reindex backfills the vector.
    """
    assert knowledge is not None, "knowledge base required"
    assert title and content, "title + content required"
    doc_id = knowledge.add(title, content)
    try:  # best-effort: make it immediately semantic-searchable
        model = gateway.embed_model(getattr(knowledge, "conn", None))  # routed embedding model
        embed_doc(knowledge, doc_id, title, content, model)
    except Exception as exc:  # embeddings optional; /api/kb/reindex can backfill
        log.info("embed-on-add skipped for %s: %s", doc_id, exc)
    return {"id": doc_id, "title": title, "chars": len(content)}


def ingest_url(knowledge, url: str) -> dict:
    """Fetch + extract + store a URL. Returns {id, title, chars}."""
    title, text = from_url(url)
    return store(knowledge, title, text)
