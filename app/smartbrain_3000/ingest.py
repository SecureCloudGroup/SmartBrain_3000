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
_EMBED_TIMEOUT = 15.0  # interactive embed-on-add / rename: fail fast (best-effort; backfilled later)
# A cold local embed model (e.g. MLX/oMLX loading bge-m3) can take ~50s on its FIRST request.
# The backfill path waits it out so a single reindex succeeds instead of timing out at 15s (then
# working on the 2nd try). Bifrost's per-provider timeout (300s) is the real upstream ceiling.
_REINDEX_EMBED_TIMEOUT = 120.0
_TEXT_EXT = frozenset({".txt", ".md", ".markdown", ".csv", ".json", ".log", ".rst", ".text"})
_HTML_EXT = frozenset({".html", ".htm"})
# Document extensions that mark an extracted title as "filename-shaped" rather than a real title —
# e.g. a PDF exported from Word whose embedded /Title metadata is still the original "Report.DOCX".
# When the extracted title ends in one of these we distrust it and use the real uploaded filename
# / URL tail instead, so the KB never shows a wrong/stale extension (like .DOCX on a .pdf).
_FILENAME_EXTS = frozenset({
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".rtf", ".odt", ".ods", ".odp",
    ".pages", ".key", ".numbers", ".epub", ".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".json",
})


class IngestError(Exception):
    """Content could not be fetched or extracted into usable text."""


def _resolve_title(extracted: str, fallback: str) -> str:
    """Prefer the extractor's title, but use ``fallback`` (the uploaded filename / URL tail) when
    the extracted title is empty OR looks like a filename ending in a document extension — e.g. a
    PDF whose /Title metadata is still the original 'Report.DOCX'. This keeps a wrong/stale
    extension out of the KB name. Splitext removes only the last segment, so a real title like
    'v1.2 spec' (ext '.2 spec', not a doc ext) is kept."""
    t = extracted.strip()
    _, ext = os.path.splitext(t)
    return t if (t and ext.lower() not in _FILENAME_EXTS) else fallback


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


def _extract_pdf(data: bytes) -> tuple[str, str, list[int]]:
    """Return (title, text, page_starts) from PDF bytes via pypdf (bounded pages + cumulative cap).

    ``page_starts[i]`` is the character offset in ``text`` where page i+1 begins. This is what makes
    "page 12" possible in a citation: the page boundaries are known here and NOWHERE else, and used
    to be flattened away by the join. Offsets are kept exact through the join (which inserts one
    separator char) and through the leading strip.

    Caps cumulative text DURING extraction so a single text-bomb page can't blow memory before
    _MAX_TEXT is applied. All pypdf access (incl. metadata) is inside the try so any parser error
    surfaces as IngestError, not a 500.
    """
    from pypdf import PdfReader  # lazy: heavy dep, only on the ingest path

    assert isinstance(data, (bytes, bytearray)), "data must be bytes"
    assert data, "data is empty"
    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        page_starts: list[int] = []
        pos = 0
        for page in reader.pages[:_MAX_PDF_PAGES]:  # bounded page loop
            piece = page.extract_text() or ""
            if pos + len(piece) >= _MAX_TEXT:  # cumulative cap = true memory ceiling
                page_starts.append(pos)
                parts.append(piece[: _MAX_TEXT - pos])
                break
            page_starts.append(pos)
            parts.append(piece)
            pos += len(piece) + 1  # +1 for the "\n" that the join below inserts
        title = (reader.metadata.title if reader.metadata and reader.metadata.title else "") or ""
    except Exception as exc:  # malformed / encrypted PDF
        raise IngestError(f"could not read PDF: {exc}") from None
    text = "\n".join(parts)
    lead = len(text) - len(text.lstrip())  # the strip below shifts every offset left by this much
    text = text.strip()
    starts = [max(0, p - lead) for p in page_starts]
    return title.strip(), text, starts


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


def _clamp_pages(page_starts: list[int], text_len: int) -> list[int]:
    """Drop page starts that fall past the (possibly truncated) text, so no citation points nowhere."""
    return [p for p in page_starts if p < text_len]


def _dispatch(data: bytes, content_type: str, hint_url: str) -> tuple[str, str, list[int]]:
    """Pick an extractor by magic bytes / content-type, falling back to the URL hint.

    Returns (title, text, page_starts) — page_starts is empty for anything that has no pages.

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
        return (*_extract_html(data.decode("utf-8", "replace"), hint_url), [])
    generic = ctl.startswith("application/octet-stream") or not ctl
    if generic and hint_url.lower().endswith(".pdf"):
        return _extract_pdf(data)
    if hint_url.lower().endswith((".html", ".htm")):
        return (*_extract_html(data.decode("utf-8", "replace"), hint_url), [])
    if _looks_binary(data):  # don't store an unrecognized binary blob as garbled text
        raise IngestError(
            "unsupported file type — supported: PDF, HTML, and text (.txt, .md, .csv, .json). "
            "Word/Office (.docx, .pptx, .xlsx), images, and other binaries aren't supported."
        )
    return "", data.decode("utf-8", "replace").strip(), []  # text/* or json — store as-is


def from_url(url: str) -> tuple[str, str, dict]:
    """Fetch a URL (SSRF-guarded) and extract (title, text, meta). Raises IngestError."""
    assert url, "url required"
    try:
        got = netguard.safe_fetch_bytes(url)
    except netguard.FetchError as exc:
        raise IngestError(str(exc)) from None
    title, text, pages = _dispatch(got["content"], got["content_type"], got["final_url"])
    if not text:
        raise IngestError("no readable text found at that URL")
    text = text[:_MAX_TEXT]
    meta = {"source_url": got["final_url"], "mime": got["content_type"], "pages": _clamp_pages(pages, len(text))}
    return _resolve_title(title, _title_from_url(got["final_url"])), text, meta


def from_file(filename: str, data: bytes) -> tuple[str, str, dict]:
    """Extract (title, text, meta) from uploaded file bytes by extension/sniff."""
    assert filename, "filename required"
    assert data, "file is empty"
    ext = os.path.splitext(filename)[1].lower()
    pages: list[int] = []
    if ext == ".pdf" or data[:5] == b"%PDF-":
        title, text, pages = _extract_pdf(data)
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
    text = text[:_MAX_TEXT]
    # The filename is what a citation actually shows the user ("Lease.pdf, p.12"), and it used to be
    # consumed to derive a title and then dropped.
    meta = {"filename": os.path.basename(filename), "pages": _clamp_pages(pages, len(text))}
    return _resolve_title(title, os.path.basename(filename)), text, meta


def embed_doc(knowledge, doc_id: str, title: str, content: str, model: str, *, timeout: float = _EMBED_TIMEOUT) -> None:
    """Chunk title+content, embed each chunk, and store the per-chunk vectors so a
    long document is fully searchable (not just its head). ``timeout`` is per embed request —
    the backfill path raises it so a cold local model's first load doesn't fail the embed."""
    assert doc_id and title and model, "doc id, title, model required"
    assert knowledge is not None, "knowledge base required"
    chunks = kb.chunk_text(title, content)  # bounded by _MAX_CHUNKS
    with httpx.Client(base_url=gateway.gateway_url(), timeout=timeout) as client:
        vectors = [gateway.embed(c, model, client=client, timeout=timeout) for c in chunks]
    knowledge.put_embeddings(doc_id, vectors, model)


def reindex_pending(knowledge, model: str, *, limit: int = 1000, timeout: float = _REINDEX_EMBED_TIMEOUT) -> tuple[int, int, int, str]:
    """Backfill embeddings for docs missing one or on a stale model (best-effort, bounded).

    Uses a generous per-embed ``timeout`` so a COLD local embed model (which can take ~50s to
    load on its first request) is waited out rather than cut short — otherwise the first reindex
    fails and only the second (warm) one works. Returns (embedded, skipped, failed, first_error).
    Shared by the manual /api/kb/reindex route and the scheduler's automated backfill."""
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
            embed_doc(knowledge, doc_id, doc["title"], doc["content"], model, timeout=timeout)
            embedded += 1
        except Exception as exc:  # keep going on a single failure
            failed += 1
            if not error:
                error = str(getattr(exc, "message", exc))
    return embedded, skipped, failed, error


def store(knowledge, title: str, content: str, meta: dict | None = None) -> dict:
    """Add a document to the KB (with provenance ``meta``) and embed it best-effort.

    Embedding failure (e.g. Ollama down) never fails the add — the doc is stored
    and a later reindex backfills the vector.
    """
    assert knowledge is not None, "knowledge base required"
    assert title and content, "title + content required"
    doc_id = knowledge.add(title, content, meta)
    try:  # best-effort: make it immediately semantic-searchable
        model = gateway.embed_model(getattr(knowledge, "conn", None))  # routed embedding model
        embed_doc(knowledge, doc_id, title, content, model)
    except Exception as exc:  # embeddings optional; /api/kb/reindex can backfill
        log.info("embed-on-add skipped for %s: %s", doc_id, exc)
    return {"id": doc_id, "title": title, "chars": len(content)}


def ingest_url(knowledge, url: str) -> dict:
    """Fetch + extract + store a URL. Returns {id, title, chars}."""
    title, text, meta = from_url(url)
    return store(knowledge, title, text, meta)
