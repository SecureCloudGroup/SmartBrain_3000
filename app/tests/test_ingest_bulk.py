"""Bulk ingest: dedupe, non-blocking uploads, and a background indexer that actually drains.

Three problems this covers:

1. NO DEDUPE. Ingesting the same URL or file twice created two independent documents, which then
   both turned up in every search, forever.
2. UPLOADS BLOCKED ON EMBEDDING. ingest.store embedded inline, and embedding a long document is
   dozens of sequential model calls that SERIALIZE on a local model — so each upload held its HTTP
   request open for as long as that took, and a multi-file drop was minutes of blocking.
3. THE BACKLOG BARELY DRAINED. The background indexer did 5 documents per 30-second tick, so 100
   files took ~10 minutes to become semantically searchable and 1,000 took over an hour. Meanwhile
   the manual reindex ran the WHOLE backlog synchronously inside one HTTP request.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, ingest
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.secrets import gen_master_key


def _kb() -> KnowledgeBase:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return KnowledgeBase(conn, gen_master_key())


# --- 1. dedupe -------------------------------------------------------------------------------

def test_identical_content_is_not_stored_twice() -> None:
    kb = _kb()
    first = ingest.store(kb, "Lease", "the lease renews in March", embed=False)
    again = ingest.store(kb, "Lease (copy)", "the lease renews in March", embed=False)
    assert again["duplicate"] is True
    assert again["id"] == first["id"], "the SAME document is returned, not a second copy"
    assert kb.count_docs() == 1
    assert len(kb.search("lease")) == 1, "a duplicate would turn up in every search forever"


def test_different_content_is_still_stored() -> None:
    kb = _kb()
    a = ingest.store(kb, "A", "first document", embed=False)
    b = ingest.store(kb, "B", "second document", embed=False)
    assert b["duplicate"] is False and a["id"] != b["id"]
    assert kb.count_docs() == 2


def test_re_ingesting_a_url_returns_the_existing_document(monkeypatch) -> None:
    kb = _kb()
    monkeypatch.setattr(
        ingest.netguard, "safe_fetch_bytes",
        lambda url: {"content": b"<html><body><article>stable article body</article></body></html>",
                     "content_type": "text/html", "final_url": "https://example.com/a"},
    )
    first = ingest.ingest_url(kb, "https://example.com/a")
    second = ingest.ingest_url(kb, "https://example.com/a")
    assert second["duplicate"] is True and second["id"] == first["id"]
    assert kb.count_docs() == 1


def test_deleting_a_document_frees_its_content_for_re_adding() -> None:
    # The dedupe map must not keep pointing at a document that no longer exists.
    kb = _kb()
    first = ingest.store(kb, "Doc", "unique body", embed=False)
    kb.delete(first["id"])
    again = ingest.store(kb, "Doc", "unique body", embed=False)
    assert again["duplicate"] is False and again["id"] != first["id"]
    assert kb.count_docs() == 1


# --- 2. uploads don't block on embedding -----------------------------------------------------

def test_store_can_defer_embedding(monkeypatch) -> None:
    kb = _kb()
    calls: list[str] = []
    monkeypatch.setattr(ingest, "embed_doc", lambda *a, **k: calls.append("embedded"))

    ingest.store(kb, "Deferred", "body one", embed=False)
    assert calls == [], "embed=False must not embed inline — that is what blocked the upload"

    ingest.store(kb, "Inline", "body two", embed=True)
    assert calls == ["embedded"], "the inline path still works for single adds"


def test_a_deferred_document_is_immediately_keyword_searchable() -> None:
    # The trade that makes deferral acceptable: no vectors yet, but the BM25 index is updated on
    # add, so the user can find what they just uploaded straight away.
    kb = _kb()
    ingest.store(kb, "Fresh", "the QUOKKA report", embed=False)
    assert [h["title"] for h in kb.search("quokka")] == ["Fresh"]
    assert kb.docs_pending_embedding("m") == 1, "and it is correctly reported as still to index"


# --- 3. the indexer drains, and nothing runs unbounded ----------------------------------------

def test_reindex_stops_at_its_time_budget_instead_of_running_for_hours() -> None:
    kb = _kb()
    for i in range(30):
        ingest.store(kb, f"Doc {i}", f"body {i}", embed=False)

    def slow_embed(*_a, **_k):
        time.sleep(0.02)  # stand in for a real embed call

    import smartbrain_3000.ingest as ing
    original = ing.embed_doc
    ing.embed_doc = slow_embed
    try:
        started = time.monotonic()
        embedded, _skipped, _failed, _err = ingest.reindex_pending(kb, "m", budget_seconds=0.1)
        elapsed = time.monotonic() - started
    finally:
        ing.embed_doc = original

    assert elapsed < 1.0, "the budget must cut the run short"
    assert embedded < 30, "it stopped mid-backlog rather than grinding through everything"
    assert embedded > 0, "but it did make progress"


def test_reindex_without_a_budget_still_finishes_the_backlog(monkeypatch) -> None:
    kb = _kb()
    for i in range(5):
        ingest.store(kb, f"Doc {i}", f"body {i}", embed=False)
    monkeypatch.setattr(ingest, "embed_doc", lambda *a, **k: None)
    embedded, _s, _f, _e = ingest.reindex_pending(kb, "m")
    assert embedded == 5


def test_the_background_indexer_budget_is_a_real_fraction_of_a_tick() -> None:
    # Regression guard on the constants: the indexer used to do 5 docs per 30s tick (~10 minutes for
    # 100 files). It must now be able to work for most of a tick, and still be bounded.
    from smartbrain_3000 import main, scheduler

    assert scheduler._AUTO_REINDEX_SECONDS >= 10.0
    assert scheduler._AUTO_REINDEX_SECONDS < main._TICK_SECONDS, "must leave the tick room to breathe"
    assert scheduler._AUTO_REINDEX_MAX_DOCS >= 100, "a verifiable ceiling, but not a throttle"


# --- the API surface -------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "kb.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        c.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield c


def test_index_status_reports_the_indexing_backlog(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(gateway, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    client.post("/api/kb", json={"title": "A", "content": "alpha"})
    client.post("/api/kb", json={"title": "B", "content": "beta"})

    body = client.get("/api/kb/index-status").json()
    assert body["total"] == 2
    assert body["pending"] == 2, "nothing embedded (no model) -> everything is pending"
    assert body["indexed"] == 0
    assert body["model"]


def test_upload_returns_without_embedding_and_reports_duplicates(client: TestClient, monkeypatch) -> None:
    embedded: list[str] = []
    monkeypatch.setattr(ingest, "embed_doc", lambda *a, **k: embedded.append("x"))

    r = client.post("/api/kb/upload?filename=notes.md", content=b"# Title\nthe wombat report")
    assert r.status_code == 200 and r.json()["duplicate"] is False
    assert embedded == [], "the upload request must not block on embedding"

    dup = client.post("/api/kb/upload?filename=notes-copy.md", content=b"# Title\nthe wombat report")
    assert dup.json()["duplicate"] is True and dup.json()["id"] == r.json()["id"]
    assert len(client.get("/api/kb").json()["documents"]) == 1


def test_reindex_reports_what_is_still_pending(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(ingest, "embed_doc", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    client.post("/api/kb", json={"title": "A", "content": "alpha"})
    body = client.post("/api/kb/reindex").json()
    assert body["failed"] == 1
    assert body["pending"] == 1, "it must say what is left rather than pretend it finished"
