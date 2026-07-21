"""Tests for the background document-summary tree (B1)."""

from __future__ import annotations

import duckdb

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import scheduler
from smartbrain_3000.docsummaries import CHUNK_CHARS, DOC_IDX, SummaryStore, expected_chunks
from smartbrain_3000.secrets import gen_master_key


def _store() -> tuple[SummaryStore, duckdb.DuckDBPyConnection]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return SummaryStore(conn, gen_master_key()), conn


def test_expected_chunks_math() -> None:
    assert expected_chunks(0) == 0
    assert expected_chunks(1) == 1
    assert expected_chunks(CHUNK_CHARS) == 1
    assert expected_chunks(CHUNK_CHARS + 1) == 2
    assert expected_chunks(CHUNK_CHARS * 7) == 7


def test_rows_roundtrip_sealed_and_length_bound() -> None:
    store, conn = _store()
    store.put("d1", 0, "chunk zero", 100, "m")
    store.put("d1", DOC_IDX, "whole doc", 100, "m")
    assert store.chunk_texts("d1", 100) == ["chunk zero"]
    assert store.doc_summary("d1", 100) == "whole doc"
    # A different current length means STALE — reads act as if nothing is stored.
    assert store.chunk_texts("d1", 101) == []
    assert store.doc_summary("d1", 101) is None
    # Ciphertext at rest: the summary text never appears in the raw table.
    raw = conn.execute("SELECT ciphertext FROM doc_summaries;").fetchall()
    assert all(b"whole doc" not in bytes(r[0]) for r in raw)


def test_next_work_walks_chunks_then_reduce_then_idle() -> None:
    store, _ = _store()
    doc = {"id": "d1", "title": "T", "content": "x" * (CHUNK_CHARS + 5)}  # 2 chunks
    w1 = store.next_work([doc])
    assert w1 == {"doc": doc, "kind": "chunk", "idx": 0}
    store.put("d1", 0, "s0", len(doc["content"]), "m")
    assert store.next_work([doc])["idx"] == 1
    store.put("d1", 1, "s1", len(doc["content"]), "m")
    assert store.next_work([doc])["kind"] == "reduce"
    store.put("d1", DOC_IDX, "final", len(doc["content"]), "m")
    assert store.next_work([doc]) is None
    assert store.progress("d1", len(doc["content"]))["complete"] is True


def test_next_work_restarts_a_changed_document() -> None:
    store, _ = _store()
    store.put("d1", 0, "old", 100, "m")
    store.put("d1", DOC_IDX, "old-doc", 100, "m")
    changed = {"id": "d1", "title": "T", "content": "y" * 200}  # length moved -> stale tree
    w = store.next_work([changed])
    assert w["kind"] == "chunk" and w["idx"] == 0
    assert store.doc_summary("d1", 100) is None, "stale rows were cleared"


def test_sweep_stale_drops_deleted_documents() -> None:
    store, conn = _store()
    store.put("gone", 0, "s", 10, "m")
    store.put("kept", 0, "s", 10, "m")
    assert store.sweep_stale({"kept"}) == 1
    assert conn.execute("SELECT count(*) FROM doc_summaries WHERE doc_id = 'gone';").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM doc_summaries WHERE doc_id = 'kept';").fetchone()[0] == 1


def test_auto_summarize_builds_a_tree_end_to_end(monkeypatch) -> None:
    # Drive the scheduler pass with a fake model layer: two ticks' worth of budget
    # builds chunk rows then the reduced doc row, resumably.
    from smartbrain_3000 import gateway, summarize
    from smartbrain_3000.kb import KnowledgeBase

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    kb = KnowledgeBase(conn, key)
    doc_id = kb.add("Book", "z" * (CHUNK_CHARS + 10))  # 2 chunks
    monkeypatch.setattr(gateway, "local_available", lambda: True)
    monkeypatch.setattr(gateway, "load_routes", lambda c: {"chat": "prov/model"})
    monkeypatch.setattr(summarize, "map_chunk", lambda m, t, f, chunk, i, n: f"S{i}")
    monkeypatch.setattr(summarize, "reduce_parts", lambda m, t, f, parts: " + ".join(parts))
    scheduler._auto_summarize(conn, key)
    store = SummaryStore(conn, key)
    content_len = CHUNK_CHARS + 10
    assert store.doc_summary(doc_id, content_len) == "S0 + S1"
    assert store.progress(doc_id, content_len)["complete"] is True


def test_auto_summarize_respects_missing_model(monkeypatch) -> None:
    from smartbrain_3000 import gateway

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    monkeypatch.setattr(gateway, "local_available", lambda: True)
    monkeypatch.setattr(gateway, "load_routes", lambda c: {})
    monkeypatch.setattr(gateway, "resolve_model", lambda cap, routes: None)
    scheduler._auto_summarize(conn, gen_master_key())  # must simply no-op, never raise


def test_auto_summarize_stands_aside_when_user_is_active(monkeypatch) -> None:
    # A 30s map call in flight when a chat arrives reads as a hang (oMLX serves one
    # request at a time) — the pass must not run at all unless the machine is idle.
    from smartbrain_3000 import gateway, summarize
    from smartbrain_3000.kb import KnowledgeBase

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    KnowledgeBase(conn, key).add("Doc", "z" * 100)
    monkeypatch.setattr(gateway, "local_available", lambda: True)
    monkeypatch.setattr(gateway, "load_routes", lambda c: {"chat": "prov/model"})

    def must_not_run(*a, **k):
        raise AssertionError("summarize pass ran while the user was active")

    monkeypatch.setattr(summarize, "map_chunk", must_not_run)
    scheduler._auto_summarize(conn, key, idle=False)  # skips silently
