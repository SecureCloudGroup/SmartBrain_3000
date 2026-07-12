"""End-to-end validation of the Knowledge feature: ingest a real PDF, then prove it is
indexed and findable by lexical (keyword) search, semantic (meaning) search, the Chat
kb_search tool, and the HTTP API the UI drives.

Knowledge search is a principal SmartBrain feature, so this exercises the WHOLE pipeline
with a genuine PDF (built in-memory, read back by the same pypdf the app uses):
  PDF bytes -> ingest.from_file -> ingest.store / kb.add (encrypted) -> embeddings index
  -> kb.search / kb.semantic_search / tools.kb_search / GET /api/kb/search.

Embeddings are faked deterministically (an indicator vector over the known test tokens) so
semantic ranking is exercised without a live embed model — a token that appears in a chunk
lands in that chunk's vector, so cosine cleanly reflects token overlap.
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from _pdfgen import long_pages, make_pdf
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, ingest, tools
from smartbrain_3000.audit import AuditLog
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.secrets import gen_master_key

# Distinctive nonsense tokens: no accidental substring matches, and each maps to its own
# embedding dimension below so semantic ranking is collision-free.
_VOCAB = [
    "zorblax", "quuxfrob", "plimbert", "grizznak", "wobbleton", "snortle",
    "fizzlewick", "vorpadene", "mungwangle", "crumbdiddle", "splonktastic", "begurfled",
]
_FAKE_MODEL = "ollama/fake-embed"


# --- deterministic fake embeddings ---------------------------------------------------------

def _fake_embed(text: str, *_a, **_k) -> list[float]:
    """Indicator vector over _VOCAB: 1.0 for each known token present in the text. Cosine then
    reflects token overlap exactly (no hash collisions). A tiny floor avoids a zero vector."""
    low = str(text).lower()
    vec = [1.0 if tok in low else 0.0 for tok in _VOCAB]
    if not any(vec):
        vec[0] = 1e-3
    return vec


def _use_fake_embed(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "embed", _fake_embed)
    monkeypatch.setattr(gateway, "embed_model", lambda *_a, **_k: _FAKE_MODEL)


# --- unit-level fixtures -------------------------------------------------------------------

def _kb() -> tuple[KnowledgeBase, duckdb.DuckDBPyConnection, bytes]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return KnowledgeBase(conn, key), conn, key


def _titles(results: list[dict]) -> list[str]:
    return [r["title"] for r in results]


# ============================================================================================
# A. PDF ingestion — text extraction across every page
# ============================================================================================

def test_pdf_ingest_extracts_text_from_every_page() -> None:
    pdf = make_pdf([
        "ZORBLAX financial summary for the QUUXFROB entity this quarter.",
        "Section two covers GRIZZNAK compliance obligations in full.",
        "Deep on the third page: the SNORTLE provision is binding forever.",
    ])
    title, text = ingest.from_file("Perenial Value SPAC (01665749).pdf", pdf)
    # No /Title metadata in our PDF -> title is the real uploaded filename (the #30 fix).
    assert title == "Perenial Value SPAC (01665749).pdf"
    for token in ("ZORBLAX", "QUUXFROB", "GRIZZNAK", "SNORTLE"):
        assert token in text, f"{token} missing from extracted PDF text"


def test_pdf_ingest_rejects_non_pdf_binary() -> None:
    with pytest.raises(ingest.IngestError):
        ingest.from_file("evil.pdf", b"%PDF-1.4 not actually a pdf body at all")


# ============================================================================================
# B. Storage — encrypted at rest
# ============================================================================================

def test_pdf_content_encrypted_at_rest() -> None:
    kb, conn, _ = _kb()
    _, text = ingest.from_file("secret.pdf", make_pdf(["The FIZZLEWICK clause is confidential."]))
    kb.add("secret.pdf", text)
    raw = b"".join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM documents;").fetchall())
    assert b"FIZZLEWICK" not in raw and b"confidential" not in raw  # plaintext never on disk


# ============================================================================================
# C. Lexical (keyword) search — reaches every page, any document
# ============================================================================================

def _store_three_page_doc(kb: KnowledgeBase) -> str:
    _, text = ingest.from_file("brief.pdf", make_pdf([
        "Opening ZORBLAX and QUUXFROB overview.",
        "Middle GRIZZNAK section with detail.",
        "Closing SNORTLE provision on the last page.",
    ]))
    return kb.add("Quarterly Brief", text)


def test_lexical_search_finds_token_on_every_page() -> None:
    kb, _, _ = _kb()
    did = _store_three_page_doc(kb)
    for token in ("ZORBLAX", "GRIZZNAK", "SNORTLE"):  # first, middle, LAST page
        hits = kb.search(token)
        assert [h["id"] for h in hits] == [did], f"{token} (a real page token) not found"


def test_lexical_search_is_case_insensitive() -> None:
    kb, _, _ = _kb()
    did = _store_three_page_doc(kb)
    assert [h["id"] for h in kb.search("snortle")] == [did]  # lowercase query, uppercase in doc


def test_lexical_search_isolates_the_matching_document() -> None:
    kb, _, _ = _kb()
    a = kb.add("Alpha", "the ZORBLAX report is here")
    b = kb.add("Beta", "the WOBBLETON memo is here")
    assert [h["id"] for h in kb.search("ZORBLAX")] == [a]
    assert [h["id"] for h in kb.search("WOBBLETON")] == [b]


def test_lexical_search_ranks_higher_frequency_first() -> None:
    kb, _, _ = _kb()
    _ = kb.add("Rare", "PLIMBERT mentioned once.")
    often = kb.add("Often", "PLIMBERT PLIMBERT PLIMBERT three times.")
    hits = kb.search("PLIMBERT")
    assert hits[0]["id"] == often and hits[0]["score"] >= hits[-1]["score"]


def test_lexical_search_snippet_carries_context() -> None:
    kb, _, _ = _kb()
    kb.add("Doc", "intro words then the VORPADENE addendum follows with more words")
    hits = kb.search("VORPADENE")
    assert "VORPADENE" in hits[0]["snippet"]


def test_lexical_search_absent_token_returns_nothing() -> None:
    kb, _, _ = _kb()
    _store_three_page_doc(kb)
    assert kb.search("XXTOTALLYABSENTXX") == []


# ============================================================================================
# D. Semantic (meaning) search — over the embeddings index
# ============================================================================================

def test_semantic_search_ranks_the_matching_pdf(monkeypatch) -> None:
    _use_fake_embed(monkeypatch)
    kb, _, _ = _kb()
    a = kb.add("Alpha", ingest.from_file("a.pdf", make_pdf(["the ZORBLAX filing"]))[1])
    b = kb.add("Beta", ingest.from_file("b.pdf", make_pdf(["the WOBBLETON filing"]))[1])
    ingest.embed_doc(kb, a, "Alpha", kb.get(a)["content"], _FAKE_MODEL)
    ingest.embed_doc(kb, b, "Beta", kb.get(b)["content"], _FAKE_MODEL)
    hits = kb.semantic_search(gateway.embed("ZORBLAX", _FAKE_MODEL), _FAKE_MODEL)
    assert [h["id"] for h in hits] == [a]  # only the meaning-matching doc, not Beta


def test_semantic_search_skips_model_mismatch() -> None:
    kb, _, _ = _kb()
    did = kb.add("Doc", "GRIZZNAK content")
    kb.put_embeddings(did, [_fake_embed("GRIZZNAK content")], "ollama/model-A")
    # Query embedded under a DIFFERENT model -> its vectors are skipped (a space mismatch).
    assert kb.semantic_search(_fake_embed("GRIZZNAK"), "ollama/model-B") == []


def test_multichunk_pdf_late_token_is_embedded_and_found(monkeypatch) -> None:
    # The long-doc guarantee: EVERY chunk is embedded, not just the head. A unique token that
    # lands only in a late chunk must still be semantically findable.
    _use_fake_embed(monkeypatch)
    kb, conn, _ = _kb()
    _, text = ingest.from_file("long.pdf", make_pdf(long_pages("MUNGWANGLE")))
    assert len(text) > 4000, "doc must exceed one chunk to exercise multi-chunk embedding"
    did = kb.add("Long Record", text)  # title has no test token, so only a late chunk holds MUNGWANGLE
    ingest.embed_doc(kb, did, "Long Record", text, _FAKE_MODEL)
    chunks = conn.execute("SELECT COUNT(*) FROM embeddings WHERE doc_id = ?;", [did]).fetchone()[0]
    assert chunks >= 2, "a >4000-char doc must store multiple chunk vectors"
    hits = kb.semantic_search(gateway.embed("MUNGWANGLE", _FAKE_MODEL), _FAKE_MODEL)
    assert [h["id"] for h in hits] == [did]  # the deep token was embedded and is found


# ============================================================================================
# E. Reindex — backfills embeddings added while the gateway was down
# ============================================================================================

def test_reindex_backfills_pdf_embeddings_then_semantic_finds(monkeypatch) -> None:
    kb, _, _ = _kb()
    _, text = ingest.from_file("late.pdf", make_pdf(["the CRUMBDIDDLE disclosure"]))
    did = kb.add("Late Doc", text)  # stored WITHOUT embeddings (gateway was down)
    assert kb.docs_needing_embedding(_FAKE_MODEL) == [did]
    _use_fake_embed(monkeypatch)  # gateway back
    embedded, _skipped, failed, _err = ingest.reindex_pending(kb, _FAKE_MODEL)
    assert embedded == 1 and failed == 0
    hits = kb.semantic_search(gateway.embed("CRUMBDIDDLE", _FAKE_MODEL), _FAKE_MODEL)
    assert [h["id"] for h in hits] == [did]


# ============================================================================================
# F. The Chat kb_search tool — keyword AND meaning, incl. the un-indexed case
# ============================================================================================

def _tool_ctx() -> tuple[tools.ToolContext, AuditLog]:
    kb, conn, key = _kb()
    return tools.ToolContext(kb=kb), AuditLog(conn, key)


def test_kb_search_tool_finds_pdf_by_meaning(monkeypatch) -> None:
    _use_fake_embed(monkeypatch)
    ctx, audit = _tool_ctx()
    _, text = ingest.from_file("f.pdf", make_pdf(["the SPLONKTASTIC ruling applies"]))
    did = ctx.kb.add("Ruling", text)
    ingest.embed_doc(ctx.kb, did, "Ruling", text, _FAKE_MODEL)
    result = tools.run(ctx, audit, "kb_search", {"query": "SPLONKTASTIC"}, actor="assistant")
    assert result["degraded"] is False and [r["title"] for r in result["results"]] == ["Ruling"]


def test_kb_search_tool_finds_unindexed_pdf_by_keyword(monkeypatch) -> None:
    # The reported bug (now fixed): with an embed model available, a document NOT yet in the
    # semantic index must still be found by keyword. Real PDF, no embeddings stored.
    _use_fake_embed(monkeypatch)  # semantic IS available...
    ctx, audit = _tool_ctx()
    _, text = ingest.from_file("g.pdf", make_pdf(["the BEGURFLED memorandum is filed"]))
    ctx.kb.add("Memo", text)  # ...but the doc is never embedded (reindex pending)
    result = tools.run(ctx, audit, "kb_search", {"query": "BEGURFLED"}, actor="assistant")
    assert result["degraded"] is False  # not a fallback — semantic was reachable
    assert [r["title"] for r in result["results"]] == ["Memo"]  # keyword scan still finds it


# ============================================================================================
# G. HTTP API — the real endpoints the Knowledge UI calls
# ============================================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "kb.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        test_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield test_client


def test_http_upload_pdf_then_lexical_and_semantic_search(client: TestClient, monkeypatch) -> None:
    _use_fake_embed(monkeypatch)  # embed-on-add succeeds, so both modes work immediately
    pdf = make_pdf([
        "ZORBLAX opening statement and QUUXFROB terms.",
        "GRIZZNAK obligations continue on the second page.",
        "The final SNORTLE clause sits on the last page.",
    ])
    up = client.post("/api/kb/upload?filename=Perenial Value SPAC (01665749).pdf", content=pdf)
    assert up.status_code == 200, up.text
    doc_id = up.json()["id"]

    # Listed with the real filename as its title (no stale .DOCX — #30).
    docs = client.get("/api/kb").json()["documents"]
    assert [d["title"] for d in docs] == ["Perenial Value SPAC (01665749).pdf"]

    # Full text stored + decryptable, including the deep page.
    got = client.get(f"/api/kb/{doc_id}").json()
    assert "ZORBLAX" in got["content"] and "SNORTLE" in got["content"]

    # Lexical finds tokens from every page (incl. the last).
    for token in ("ZORBLAX", "GRIZZNAK", "SNORTLE"):
        r = client.get("/api/kb/search", params={"q": token, "mode": "lexical"}).json()
        assert [h["id"] for h in r["results"]] == [doc_id], f"lexical missed {token}"

    # Semantic finds it too (embedded on add), not degraded.
    r = client.get("/api/kb/search", params={"q": "SNORTLE", "mode": "semantic"}).json()
    assert r["degraded"] is False and [h["id"] for h in r["results"]] == [doc_id]


def test_http_semantic_degrades_to_lexical_when_gateway_down(client: TestClient, monkeypatch) -> None:
    # No reachable embed model: upload still stores the doc (embed-on-add skipped), and a
    # semantic search transparently degrades to keyword and still finds it — never silent.
    def _boom(*_a, **_k):
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(gateway, "embed", _boom)
    monkeypatch.setattr(gateway, "embed_model", lambda *_a, **_k: _FAKE_MODEL)
    up = client.post("/api/kb/upload?filename=down.pdf", content=make_pdf(["the WOBBLETON filing endures"]))
    assert up.status_code == 200
    doc_id = up.json()["id"]
    r = client.get("/api/kb/search", params={"q": "WOBBLETON", "mode": "semantic"}).json()
    assert r["degraded"] is True and [h["id"] for h in r["results"]] == [doc_id]


def test_http_reindex_backfills_then_semantic_finds(client: TestClient, monkeypatch) -> None:
    # Upload while the gateway is down (no embeddings), then reindex once it's back and confirm
    # the doc becomes semantically searchable — the real "add now, index later" recovery path.
    monkeypatch.setattr(gateway, "embed", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(gateway, "embed_model", lambda *_a, **_k: _FAKE_MODEL)
    up = client.post("/api/kb/upload?filename=r.pdf", content=make_pdf(["the VORPADENE exhibit is attached"]))
    doc_id = up.json()["id"]
    # Semantic can't find it yet (no vectors) — degrades to lexical, which still matches.
    pre = client.get("/api/kb/search", params={"q": "VORPADENE", "mode": "semantic"}).json()
    assert pre["degraded"] is True

    _use_fake_embed(monkeypatch)  # gateway back
    rr = client.post("/api/kb/reindex").json()
    assert rr["embedded"] == 1 and rr["failed"] == 0
    post = client.get("/api/kb/search", params={"q": "VORPADENE", "mode": "semantic"}).json()
    assert post["degraded"] is False and [h["id"] for h in post["results"]] == [doc_id]
