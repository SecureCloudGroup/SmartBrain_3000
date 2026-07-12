"""Tests for Knowledge ingestion (ingest.py + the /api/kb/* upload routes + tool)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import gateway, ingest, netguard, tools

_HTML = b"""<!doctype html><html><head><title>About Widgets</title></head><body>
<article><h1>About Widgets</h1>
<p>Widgets are small mechanical components used across many industries. They have
been manufactured since the early twentieth century and remain essential today.</p>
<p>This article explains how widgets are produced, the materials involved, and the
quality controls that keep them reliable in demanding environments.</p></article>
</body></html>"""


class _StubKB:
    """Minimal KnowledgeBase stand-in for store() tests."""

    def __init__(self) -> None:
        self.docs: dict = {}
        self.embeds: dict = {}

    def add(self, title: str, content: str) -> str:
        doc_id = f"d{len(self.docs)}"
        self.docs[doc_id] = (title, content)
        return doc_id

    def put_embedding(self, doc_id: str, vector, model: str) -> None:
        self.embeds[doc_id] = (vector, model)

    def put_embeddings(self, doc_id: str, vectors, model: str) -> None:
        self.embeds[doc_id] = (vectors[0], model)  # first chunk — matches the single-chunk assert


def test_from_file_text() -> None:
    title, text = ingest.from_file("notes.md", b"# Hello\nworld")
    assert title == "notes.md" and text == "# Hello\nworld"


def test_from_file_unsupported_type() -> None:
    with pytest.raises(ingest.IngestError):
        ingest.from_file("malware.exe", b"MZ\x90\x00")


def test_from_file_empty_text_rejected() -> None:
    with pytest.raises(ingest.IngestError):
        ingest.from_file("blank.txt", b"   \n  ")


def test_from_file_binary_without_text_ext_rejected() -> None:
    # An extension-less file that is actually binary must not be stored as garbled text.
    with pytest.raises(ingest.IngestError):
        ingest.from_file("report", b"\x00\x01\x02\xff\xfe")


def test_malformed_pdf_raises_ingest_error() -> None:
    # A %PDF header but garbage body: pypdf raises -> wrapped as IngestError (not 500).
    with pytest.raises(ingest.IngestError):
        ingest.from_file("broken.pdf", b"%PDF-1.4 totally not a real pdf body")


def test_from_file_pdf_ignores_filename_shaped_metadata_title(monkeypatch) -> None:
    # A PDF exported from Word keeps the original ".DOCX" name in its /Title metadata; using that
    # would wrongly title a .pdf upload "…​.DOCX". Fall back to the actual uploaded filename instead.
    monkeypatch.setattr(ingest, "_extract_pdf", lambda data: ("Perenial Value SPAC (01665749).DOCX", "body"))
    title, text = ingest.from_file("Perenial Value SPAC (01665749).pdf", b"%PDF-1.4 fake")
    assert title == "Perenial Value SPAC (01665749).pdf"  # the real uploaded name, not the stale .DOCX
    assert text == "body"


def test_from_file_pdf_keeps_real_metadata_title(monkeypatch) -> None:
    # A genuine PDF title (not filename-shaped) is still preferred over the raw filename.
    monkeypatch.setattr(ingest, "_extract_pdf", lambda data: ("Q3 Earnings Report", "body"))
    title, _ = ingest.from_file("download (3).pdf", b"%PDF-1.4 fake")
    assert title == "Q3 Earnings Report"


def test_from_url_octet_stream_binary_rejected(monkeypatch) -> None:
    monkeypatch.setattr(netguard, "safe_fetch_bytes", lambda url: {
        "final_url": "https://example.com/blob", "status": 200,
        "content_type": "application/octet-stream", "content": b"\x00\x01\x02\xff binary",
    })
    with pytest.raises(ingest.IngestError):
        ingest.from_url("https://example.com/blob")


def test_from_file_html_extracts_article() -> None:
    title, text = ingest.from_file("about.html", _HTML)
    assert "widget" in text.lower() and len(text) > 80  # trafilatura pulled the body


def test_from_url_html(monkeypatch) -> None:
    monkeypatch.setattr(netguard, "safe_fetch_bytes", lambda url: {
        "final_url": "https://example.com/widgets", "status": 200,
        "content_type": "text/html", "content": _HTML,
    })
    title, text = ingest.from_url("https://example.com/widgets")
    assert "widget" in text.lower()


def test_from_url_pdf_dispatch(monkeypatch) -> None:
    monkeypatch.setattr(netguard, "safe_fetch_bytes", lambda url: {
        "final_url": "https://example.com/doc.pdf", "status": 200,
        "content_type": "application/pdf", "content": b"%PDF-1.4 fake",
    })
    monkeypatch.setattr(ingest, "_extract_pdf", lambda data: ("Paper Title", "extracted pdf text"))
    title, text = ingest.from_url("https://example.com/doc.pdf")
    assert title == "Paper Title" and text == "extracted pdf text"


def test_from_url_fetch_error_becomes_ingest_error(monkeypatch) -> None:
    def boom(url):
        raise netguard.FetchError("blocked non-global address")

    monkeypatch.setattr(netguard, "safe_fetch_bytes", boom)
    with pytest.raises(ingest.IngestError):
        ingest.from_url("http://169.254.169.254/latest/meta-data/")


def test_store_embeds_best_effort(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "embed_model", lambda conn=None: "ollama/test")
    monkeypatch.setattr(gateway, "embed", lambda text, model, **k: [0.1, 0.2, 0.3])
    kb = _StubKB()
    out = ingest.store(kb, "Title", "Body text")
    assert out == {"id": "d0", "title": "Title", "chars": len("Body text")}
    assert kb.embeds["d0"][1] == "ollama/test"  # embedded on add


def test_store_survives_embed_failure(monkeypatch) -> None:
    def boom(text, model, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(gateway, "embed_model", lambda conn=None: "ollama/test")
    monkeypatch.setattr(gateway, "embed", boom)
    kb = _StubKB()
    out = ingest.store(kb, "Title", "Body")
    assert out["id"] == "d0" and "d0" in kb.docs and kb.embeds == {}  # stored, not embedded


class _ReindexKB(_StubKB):
    """_StubKB plus the read side reindex_pending needs."""

    def __init__(self, pending: list[str]) -> None:
        super().__init__()
        self._pending = pending
        for pid in pending:
            self.docs[pid] = (pid, f"content of {pid}")

    def docs_needing_embedding(self, model: str) -> list[str]:
        return list(self._pending)

    def get(self, doc_id: str) -> dict | None:
        if doc_id not in self.docs:
            return None
        title, content = self.docs[doc_id]
        return {"id": doc_id, "title": title, "content": content}


def test_embed_doc_forwards_timeout_to_gateway(monkeypatch) -> None:
    # The per-embed timeout must reach gateway.embed — interactive default, or the long backfill value.
    seen: list[float] = []
    monkeypatch.setattr(gateway, "embed", lambda text, model, *, client=None, timeout=None: seen.append(timeout) or [0.1, 0.2, 0.3])
    kb = _StubKB()
    ingest.embed_doc(kb, "d0", "Title", "body", "m")  # interactive default
    ingest.embed_doc(kb, "d0", "Title", "body", "m", timeout=ingest._REINDEX_EMBED_TIMEOUT)
    assert seen == [ingest._EMBED_TIMEOUT, ingest._REINDEX_EMBED_TIMEOUT]


def test_reindex_pending_waits_out_a_cold_model(monkeypatch) -> None:
    # Regression: a cold local embed model (~50s to load) must not fail the FIRST reindex. The
    # backfill embeds with a generous timeout so oMLX/bifrost can finish loading rather than being
    # cut at the interactive 15s (which caused "failed first, worked on the second try").
    seen: list[float] = []
    monkeypatch.setattr(gateway, "embed", lambda text, model, *, client=None, timeout=None: seen.append(timeout) or [0.1, 0.2, 0.3])
    embedded, _skipped, failed, _err = ingest.reindex_pending(_ReindexKB(["doc-a"]), "m")
    assert embedded == 1 and failed == 0
    assert seen and all(t == ingest._REINDEX_EMBED_TIMEOUT for t in seen)  # long timeout, not 15s
    assert ingest._REINDEX_EMBED_TIMEOUT > ingest._EMBED_TIMEOUT  # backfill is more patient than upload


def test_embed_on_add_stays_fast(monkeypatch) -> None:
    # Interactive upload must NOT block on a cold model — it embeds fast (best-effort) and is
    # backfilled later by reindex if the model was cold.
    seen: list[float] = []
    monkeypatch.setattr(gateway, "embed_model", lambda conn=None: "m")
    monkeypatch.setattr(gateway, "embed", lambda text, model, *, client=None, timeout=None: seen.append(timeout) or [0.1, 0.2, 0.3])
    ingest.store(_StubKB(), "Title", "Body text")
    assert seen == [ingest._EMBED_TIMEOUT]


def test_gateway_embed_wraps_timeout_as_504() -> None:
    import httpx

    class _Timeouts:
        def post(self, *_a, **_k):
            raise httpx.ReadTimeout("model still loading")

    with pytest.raises(gateway.GatewayError) as ei:
        gateway.embed("some text", "m", client=_Timeouts())
    assert ei.value.status_code == 504  # a cold-load timeout surfaces cleanly, not as a raw httpx error


def test_kb_ingest_url_tool_is_reviewed_egress() -> None:
    tool = tools.get_tool("kb_ingest_url")
    assert tool is not None
    assert tool.tier is tools.Tier.REVIEWED and tool.egress is True


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_ingest_url_endpoint_requires_unlock(client: TestClient) -> None:
    assert client.post("/api/kb/ingest-url", json={"url": "https://example.com"}).status_code == 423


def test_upload_endpoint_stores_text(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.post("/api/kb/upload?filename=notes.txt", content=b"hello knowledge")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "notes.txt" and body["chars"] == len("hello knowledge")
    assert any(d["title"] == "notes.txt" for d in client.get("/api/kb").json()["documents"])


def test_ingest_url_endpoint_maps_ingest_error(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})

    def boom(knowledge, url):
        raise ingest.IngestError("no readable text found at that URL")

    monkeypatch.setattr(ingest, "ingest_url", boom)
    r = client.post("/api/kb/ingest-url", json={"url": "https://example.com/empty"})
    assert r.status_code == 400 and "no readable text" in r.json()["detail"]
