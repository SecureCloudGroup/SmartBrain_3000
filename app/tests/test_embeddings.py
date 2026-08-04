"""Tests for KB2: gateway.embed + encrypted embedding storage + cosine (KB2)."""

from __future__ import annotations

import json
import struct

import duckdb
import httpx
import pytest
from cryptography.exceptions import InvalidTag

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.secrets import gen_master_key


def _kb(master_key: bytes | None = None) -> KnowledgeBase:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return KnowledgeBase(conn, master_key or gen_master_key())


def _mock(handler) -> httpx.Client:
    return httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))


# --- gateway.embed --------------------------------------------------------

def test_embed_model_env(monkeypatch) -> None:
    monkeypatch.delenv("SMARTBRAIN_EMBED_MODEL", raising=False)
    assert gateway.embed_model() == "ollama/nomic-embed-text:v1.5"
    monkeypatch.setenv("SMARTBRAIN_EMBED_MODEL", "ollama/mxbai-embed-large:latest")
    assert gateway.embed_model() == "ollama/mxbai-embed-large:latest"


def test_gateway_embed_forms_request_and_parses() -> None:
    # A non-prefix embed model must have byte-identical wire behavior — the task-prefix path
    # is nomic-specific (test_gateway_embed_prepends_document_prefix_for_nomic covers that).
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    with _mock(handler) as client:
        out = gateway.embed("hello", "ollama/mxbai-embed-large:latest", client=client)
    assert out == pytest.approx([0.1, 0.2, 0.3])
    assert seen["path"] == "/v1/embeddings"
    assert seen["body"] == {"model": "ollama/mxbai-embed-large:latest", "input": "hello"}


def test_gateway_embed_raises_on_error_envelope() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    with _mock(handler) as client:
        with pytest.raises(gateway.GatewayError) as info:
            gateway.embed("hi", "ollama/nope", client=client)
    assert info.value.status_code == 404
    assert info.value.message == "model not found"


def test_gateway_embed_raises_on_malformed_200() -> None:
    # Raw bytes (not json=) so a real NaN reaches the parser — httpx refuses to
    # serialize NaN, but the gateway can return one and embed() must reject it.
    bodies = (b'{"data": []}', b'{"data": [{}]}', b'{"data": [{"embedding": [1.0, NaN]}]}')
    for body in bodies:
        with _mock(lambda req, b=body: httpx.Response(200, content=b)) as client:
            with pytest.raises(gateway.GatewayError):
                gateway.embed("hi", "ollama/x", client=client)


# --- cosine (now a vectorised mat-vec inside the search index) -------------

def test_index_cosine_scores_identical_and_orthogonal() -> None:
    from smartbrain_3000.kbindex import _VecBlock

    block = _VecBlock(2)
    block.add("same", [[1.0, 0.0]])
    block.add("orth", [[0.0, 1.0]])
    block.add("opposite", [[-1.0, 0.0]])
    best = block.best_by_doc([1.0, 0.0], min_score=-2.0)  # below -1 so even an opposite vector is scored
    assert best["same"][0] == pytest.approx(1.0)
    assert best["orth"][0] == pytest.approx(0.0, abs=1e-6)
    assert best["opposite"][0] == pytest.approx(-1.0)
    # In production min_score is 0.0, so a negatively-correlated document is never surfaced.
    assert set(block.best_by_doc([1.0, 0.0], min_score=0.0)) == {"same"}


def test_index_cosine_zero_and_nonfinite_query_score_nothing() -> None:
    # Degenerate input must yield no scores rather than NaN ranking the corpus at random.
    from smartbrain_3000.kbindex import _VecBlock

    block = _VecBlock(2)
    block.add("d", [[1.0, 1.0]])
    assert block.best_by_doc([0.0, 0.0], min_score=-1.0) == {}
    huge = 1e200  # overflows float32 -> inf norm -> non-finite
    assert block.best_by_doc([huge, huge], min_score=-1.0) == {}


def test_index_zero_stored_vector_does_not_become_nan() -> None:
    from smartbrain_3000.kbindex import _VecBlock

    block = _VecBlock(2)
    block.add("zero", [[0.0, 0.0]])
    best = block.best_by_doc([1.0, 0.0], min_score=-1.0)
    assert best["zero"][0] == pytest.approx(0.0)  # not NaN


# --- embedding storage ----------------------------------------------------

def test_put_and_get_embedding_roundtrip() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    kb.put_embedding(doc_id, [0.5, -0.25, 0.75], "ollama/nomic-embed-text:v1.5")
    vector, model = kb.get_embedding(doc_id)
    assert vector == pytest.approx([0.5, -0.25, 0.75])
    assert model == "ollama/nomic-embed-text:v1.5"


def test_get_embedding_absent_returns_none() -> None:
    assert _kb().get_embedding("missing") is None


def test_semantic_search_ranks_by_similarity() -> None:
    kb = _kb()
    m = "m1"
    near = kb.add("Near", "near")
    far = kb.add("Far", "far")
    kb.put_embedding(near, [1.0, 1.0, 0.0], m)
    kb.put_embedding(far, [1.0, 0.0, 0.0], m)
    results = kb.semantic_search([1.0, 1.0, 0.0], m)
    assert [r["id"] for r in results] == [near, far]
    assert results[0]["score"] > results[1]["score"]
    # A hit is now a CITATION: chunk_idx records WHICH chunk matched (so the snippet quotes that
    # passage), and source/page/page_label/offset say where it came from, what a "page" is called in
    # that format (page / slide / sheet), and where to open the document.
    assert set(results[0]) == {
        "id", "title", "score", "snippet", "chunk_idx", "source", "page", "page_label", "offset",
    }


def test_chunk_text_splits_long_doc_with_title_prefix() -> None:
    from smartbrain_3000.kb import _CHUNK_CHARS, chunk_text

    chunks = chunk_text("Title", "x" * (_CHUNK_CHARS * 2 + 10))
    assert len(chunks) == 3  # two full chunks + remainder
    assert all(c.startswith("Title\n") for c in chunks)  # title reachable in every chunk


def test_put_embeddings_multiple_chunks_searchable_per_chunk() -> None:
    kb = _kb()
    m = "m1"
    doc = kb.add("Doc", "long")
    # chunk 0 points one way, chunk 1 another — a query matching only chunk 1 still finds the doc.
    kb.put_embeddings(doc, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], m)
    hits = kb.semantic_search([0.0, 1.0, 0.0], m)  # matches chunk 1 only
    assert [h["id"] for h in hits] == [doc]


def test_semantic_filters_nonpositive() -> None:
    kb = _kb()
    m = "m1"
    orth = kb.add("Orthogonal", "x")
    kb.put_embedding(orth, [0.0, 1.0, 0.0], m)  # cosine 0.0 — filtered
    opp = kb.add("Opposite", "y")
    kb.put_embedding(opp, [-1.0, 0.0, 0.0], m)  # cosine -1.0 — also filtered
    assert kb.semantic_search([1.0, 0.0, 0.0], m) == []


def test_semantic_skips_stale_model_and_dim() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    kb.put_embedding(doc_id, [1.0, 0.0, 0.0], "old-model")
    assert kb.semantic_search([1.0, 0.0, 0.0], "new-model") == []  # stale model skipped
    assert kb.semantic_search([1.0, 0.0], "old-model") == []  # dim mismatch skipped


def test_semantic_skips_orphan_vector() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    kb.put_embedding(doc_id, [1.0, 0.0, 0.0], "m1")
    kb._conn.execute("DELETE FROM documents WHERE id = ?;", [doc_id])  # orphan the vector
    assert kb.semantic_search([1.0, 0.0, 0.0], "m1") == []


def test_delete_cascades_embedding() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    kb.put_embedding(doc_id, [1.0, 0.0, 0.0], "m1")
    kb.delete(doc_id)
    assert kb.get(doc_id) is None
    assert kb.get_embedding(doc_id) is None


def test_docs_needing_embedding() -> None:
    kb = _kb()
    a = kb.add("A", "a")
    b = kb.add("B", "b")
    kb.put_embedding(a, [1.0, 0.0], "m1")
    assert kb.docs_needing_embedding("m1") == [b]  # a is current; b has none
    assert set(kb.docs_needing_embedding("m2")) == {a, b}  # m1 is now stale


# --- at-rest security -----------------------------------------------------

def test_embedding_wrong_key_cannot_read() -> None:
    key = gen_master_key()
    kb = _kb(key)
    doc_id = kb.add("t", "c")
    kb.put_embedding(doc_id, [1.0, 2.0, 3.0], "m1")
    other = KnowledgeBase(kb._conn, gen_master_key())
    with pytest.raises(InvalidTag):  # wrong key fails GCM auth — fail closed
        other.get_embedding(doc_id)


def test_embedding_aad_domain_separated() -> None:
    # A documents-body ciphertext copied into the embeddings row for the same
    # doc_id must NOT authenticate as an embedding (AAD prefix differs).
    kb = _kb()
    doc_id = kb.add("t", "c")
    row = kb._conn.execute(
        "SELECT nonce, ciphertext FROM documents WHERE id = ?;", [doc_id]
    ).fetchone()
    kb._conn.execute(
        "INSERT INTO embeddings (doc_id, chunk_idx, nonce, ciphertext, dim, model) VALUES (?, ?, ?, ?, ?, ?);",
        [doc_id, 0, bytes(row[0]), bytes(row[1]), 3, "m1"],
    )
    # Document-body AAD is bare doc_id; embedding AAD is 'embedding:doc|dim|model'
    # — so the body ciphertext cannot authenticate as an embedding. Pin InvalidTag
    # specifically (not bare Exception) so a length-mismatch can't make this pass.
    with pytest.raises(InvalidTag):
        kb.get_embedding(doc_id)


def test_embedding_metadata_tamper_fails_closed() -> None:
    # dim and model are plaintext columns but bound into the AAD: flipping either
    # breaks GCM auth (fail closed), not just a downstream length assertion.
    kb = _kb()
    doc_id = kb.add("t", "c")
    kb.put_embedding(doc_id, [1.0, 2.0, 3.0], "m1")
    kb._conn.execute("UPDATE embeddings SET dim = 5 WHERE doc_id = ?;", [doc_id])
    with pytest.raises(InvalidTag):
        kb.get_embedding(doc_id)
    kb.put_embedding(doc_id, [1.0, 2.0, 3.0], "m1")  # restore a valid row
    kb._conn.execute("UPDATE embeddings SET model = 'evil' WHERE doc_id = ?;", [doc_id])
    with pytest.raises(InvalidTag):
        kb.get_embedding(doc_id)


def test_embedding_encrypted_at_rest() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    kb = KnowledgeBase(conn, gen_master_key())
    doc_id = kb.add("t", "c")
    secret_vec = [3.14159, 2.71828]
    kb.put_embedding(doc_id, secret_vec, "m1")
    raw = bytes(conn.execute("SELECT ciphertext FROM embeddings;").fetchone()[0])
    assert struct.pack(f"<{len(secret_vec)}f", *secret_vec) not in raw  # plaintext not present


# --- migration ------------------------------------------------------------

def test_migrations_create_chunked_embeddings_table() -> None:
    conn = duckdb.connect(":memory:")
    applied = dbmod.run_migrations(conn)
    assert applied == 35  # ... + selfimprove tables + optimizer trial columns (33-35)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('embeddings');").fetchall()}
    assert {"doc_id", "chunk_idx", "nonce", "ciphertext", "dim", "model", "created_at"} <= cols
    assert dbmod.run_migrations(conn) == 0  # idempotent


# --- hardening / boundary coverage ----------------------------------------

def test_vector_from_rejects_bool_and_nonfinite() -> None:
    # bool is an int subclass; NaN/Inf are finite-check failures — all rejected.
    for bad in ([True, 1.0], [1.0, float("nan")], [1.0, float("inf")]):
        with pytest.raises(gateway.GatewayError):
            gateway._vector_from({"data": [{"embedding": bad}]})


def test_vector_from_rejects_oversize() -> None:
    with pytest.raises(gateway.GatewayError):
        gateway._vector_from({"data": [{"embedding": [0.1] * 5000}]})


def test_gateway_embed_non_json_body() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all", headers={"content-type": "text/plain"})

    with _mock(handler) as client:
        with pytest.raises(gateway.GatewayError) as info:
            gateway.embed("hi", "ollama/x", client=client)
    assert "non-JSON" in info.value.message


def test_put_embedding_rejects_nonfinite() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    with pytest.raises(AssertionError):
        kb.put_embedding(doc_id, [1.0, float("nan")], "m1")


def test_put_embedding_dim_bounds() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    with pytest.raises(AssertionError):
        kb.put_embedding(doc_id, [0.0] * 4097, "m1")  # over _MAX_EMBED_DIM
    kb.put_embedding(doc_id, [0.1] * 4096, "m1")  # at the bound — ok
    vec, _ = kb.get_embedding(doc_id)
    assert len(vec) == 4096


def test_embedding_roundtrip_realistic_dim() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    vector = [(i % 7) * 0.013 - 0.04 for i in range(768)]  # nomic-embed-text size
    kb.put_embedding(doc_id, vector, "m1")
    got, _ = kb.get_embedding(doc_id)
    assert got == pytest.approx(vector, abs=1e-6)


def test_embedding_nonces_differ() -> None:
    kb = _kb()
    a, b = kb.add("A", "a"), kb.add("B", "b")
    kb.put_embedding(a, [1.0, 0.0], "m1")
    n_a1 = bytes(kb._conn.execute("SELECT nonce FROM embeddings WHERE doc_id=?;", [a]).fetchone()[0])
    kb.put_embedding(b, [0.0, 1.0], "m1")
    n_b = bytes(kb._conn.execute("SELECT nonce FROM embeddings WHERE doc_id=?;", [b]).fetchone()[0])
    kb.put_embedding(a, [0.5, 0.5], "m1")  # re-embed a (ON CONFLICT UPDATE)
    n_a2 = bytes(kb._conn.execute("SELECT nonce FROM embeddings WHERE doc_id=?;", [a]).fetchone()[0])
    assert len({n_a1, n_b, n_a2}) == 3  # distinct across docs and across re-embed


def test_embedding_aad_binds_to_doc_id() -> None:
    # Swapping two rows' (nonce,ciphertext) must fail GCM auth — AAD binds doc_id.
    kb = _kb()
    a, b = kb.add("A", "a"), kb.add("B", "b")
    kb.put_embedding(a, [1.0, 0.0, 0.0], "m1")
    kb.put_embedding(b, [0.0, 1.0, 0.0], "m1")
    ra = kb._conn.execute("SELECT nonce, ciphertext FROM embeddings WHERE doc_id=?;", [a]).fetchone()
    kb._conn.execute(
        "UPDATE embeddings SET nonce=?, ciphertext=? WHERE doc_id=?;",
        [bytes(ra[0]), bytes(ra[1]), b],
    )
    with pytest.raises(InvalidTag):
        kb.get_embedding(b)  # a's vector cannot be opened as b


def test_delete_clears_orphan_embedding() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    kb.put_embedding(doc_id, [1.0, 0.0, 0.0], "m1")
    kb._conn.execute("DELETE FROM documents WHERE id=?;", [doc_id])  # orphan the vector
    kb.delete(doc_id)  # must clear the orphan without error
    assert kb.get_embedding(doc_id) is None


def test_index_bounds_are_sized_for_a_real_corpus() -> None:
    # Search is now answered from the in-memory index, so the old per-query scan caps are gone. What
    # remains must be a REAL ceiling, not the old `LIMIT 500` that silently hid older documents:
    # the index must hold far more documents than a single page of results, and cover enough chunks
    # that a fully-chunked corpus still fits.
    from smartbrain_3000 import kb as kbmod
    from smartbrain_3000 import kbindex

    assert kbindex._MAX_INDEXED_DOCS >= 100 * kbmod._SEARCH_SCAN_LIMIT
    assert kbmod._MAX_INDEXED_VECTORS >= kbmod._SEARCH_SCAN_LIMIT * kbmod._MAX_CHUNKS
    assert kbmod._REINDEX_SCAN_LIMIT > kbmod._SEARCH_SCAN_LIMIT


# --- big documents: incremental, resumable embedding (migration 26) ------------------------

def _vec(seed: float, dim: int = 8) -> list[float]:
    return [seed + i * 0.01 for i in range(dim)]


def test_chunking_covers_big_documents() -> None:
    from smartbrain_3000.kb import _MAX_CHUNKS, chunk_text

    content = "x" * 300_000  # a ~100-page document; the old 64-chunk cap truncated at 256k
    chunks = chunk_text("Big filing", content)
    assert len(chunks) == 75  # ceil(300k / 4000) — nothing dropped
    assert len(chunk_text("Huge", "y" * 10_000_000)) == _MAX_CHUNKS  # cap still verifiable


def test_partial_run_is_pending_and_resumable() -> None:
    kb = _kb()
    doc_id = kb.add("Doc", "z" * 20_000)  # 5 chunks
    kb.clear_other_models(doc_id, "m1")
    for i in (0, 1, 2):
        kb.put_embedding_chunk(doc_id, i, _vec(i), "m1", total=5)
    assert kb.stored_chunks(doc_id, "m1") == {0, 1, 2}
    assert doc_id in kb.docs_needing_embedding("m1")  # partial => still pending
    assert kb.docs_pending_embedding("m1") == 1  # the STATUS count must agree (UI truth)
    for i in (3, 4):
        kb.put_embedding_chunk(doc_id, i, _vec(i), "m1", total=5)
    kb.finish_embeddings(doc_id, "m1")
    assert doc_id not in kb.docs_needing_embedding("m1")  # complete
    assert kb.docs_pending_embedding("m1") == 0
    assert len(kb.vectors_for(doc_id, "m1")) == 5


def test_legacy_full_write_counts_complete() -> None:
    kb = _kb()
    doc_id = kb.add("Legacy", "content")
    kb.put_embeddings(doc_id, [_vec(1.0)], "m1")
    # Simulate a pre-migration row: NULL total (legacy all-at-once writes had no column).
    kb.conn.execute("UPDATE embeddings SET total = NULL WHERE doc_id = ?;", [doc_id])
    assert doc_id not in kb.docs_needing_embedding("m1")  # NULL total = complete
    assert doc_id in kb.docs_needing_embedding("m2")  # other model still pending


def test_model_switch_never_mixes_models_per_chunk() -> None:
    kb = _kb()
    doc_id = kb.add("Doc", "z" * 8_000)  # 2 chunks
    kb.put_embeddings(doc_id, [_vec(1), _vec(2)], "old-model")
    kb.clear_other_models(doc_id, "new-model")
    kb.put_embedding_chunk(doc_id, 0, _vec(3), "new-model", total=2)
    models = {str(r[0]) for r in kb.conn.execute(
        "SELECT DISTINCT model FROM embeddings WHERE doc_id = ?;", [doc_id]).fetchall()}
    assert models == {"new-model"}  # old rows gone before the new run wrote anything
    assert doc_id in kb.docs_needing_embedding("new-model")  # 1 of 2 => pending


def test_embed_doc_resumes_after_deadline(monkeypatch) -> None:
    """The indexer's budget stops a big doc MID-RUN; the next pass finishes it."""
    import time as timemod

    from smartbrain_3000 import ingest

    kb = _kb()
    content = "w" * 40_000  # 10 chunks
    doc_id = kb.add("Big", content)
    calls = {"n": 0}

    def fake_embed(text, model, client=None, timeout=None):
        calls["n"] += 1
        return _vec(calls["n"])

    monkeypatch.setattr(ingest.gateway, "embed", fake_embed)
    # First pass: deadline already lapsed after the first chunk write.
    start = timemod.monotonic()
    done = ingest.embed_doc(kb, doc_id, "Big", content, "m1", deadline=start)
    assert done is False and calls["n"] == 0  # deadline hit before any embed this pass
    done = ingest.embed_doc(kb, doc_id, "Big", content, "m1",
                            deadline=timemod.monotonic() + 60)
    assert done is True and calls["n"] == 10
    assert doc_id not in kb.docs_needing_embedding("m1")
    assert len(kb.vectors_for(doc_id, "m1")) == 10


def test_store_defers_embedding_for_big_docs(monkeypatch) -> None:
    from smartbrain_3000 import ingest

    kb = _kb()
    calls = {"n": 0}

    def fake_embed(text, model, client=None, timeout=None):
        calls["n"] += 1
        return _vec(1.0)

    monkeypatch.setattr(ingest.gateway, "embed", fake_embed)
    monkeypatch.setattr(ingest.gateway, "embed_model", lambda conn=None: "m1")
    small = ingest.store(kb, "Small", "s" * 4_000)  # 1 chunk -> embeds inline
    assert calls["n"] == 1 and small["duplicate"] is False
    ingest.store(kb, "Big", "b" * 100_000)  # 25 chunks -> deferred to the indexer
    assert calls["n"] == 1  # unchanged: no inline embedding for the big doc


# --- nomic-embed-text task prefixes + storage-scheme marker --------------------------------
#
# nomic-embed-text was trained on task prefixes ("search_document: " on passages,
# "search_query: " on queries) — sending raw text hurts retrieval. The gateway now prepends
# them for nomic models, but a mid-migration corpus still holds vectors that were embedded
# WITHOUT prefixes: fusing those with prefixed query vectors would degrade ranking silently.
# ``embedding_scheme`` returns the storage identity ('...#tp1' for prefix-capable models); old
# rows sit under the bare id, appear stale to the backlog probe, and the background indexer
# re-embeds them under the new identity.

def test_uses_task_prefixes_matches_nomic_only() -> None:
    assert gateway.uses_task_prefixes("ollama/nomic-embed-text:v1.5") is True
    assert gateway.uses_task_prefixes("ollama/nomic-embed-text") is True
    assert gateway.uses_task_prefixes("mlxe/nomic-embed-text-v1.5") is True  # any provider
    # modernbert-embed is trained from the Nomic Embed recipe (same prefixes) — and it is
    # the model oMLX installs actually route embedding to, so missing it here would leave
    # the flagship local config un-prefixed.
    assert gateway.uses_task_prefixes("mlx/nomicai-modernbert-embed-base-bf16") is True
    assert gateway.uses_task_prefixes("ollama/mxbai-embed-large:latest") is False
    assert gateway.uses_task_prefixes("mlx/bge-m3-mlx-fp16") is False
    assert gateway.uses_task_prefixes("openai/text-embedding-3-small") is False


def test_embedding_scheme_marks_nomic_and_leaves_others_unchanged() -> None:
    # The marker is what keeps mid-migration corpora safe: bare-id (pre-prefix) vectors
    # and '#tp1' (prefixed) vectors land in different (model, dim) blocks, so a prefixed
    # query can never silently fuse against a non-prefixed passage.
    assert gateway.embedding_scheme("ollama/nomic-embed-text:v1.5") == "ollama/nomic-embed-text:v1.5#tp1"
    assert gateway.embedding_scheme("mlx/nomicai-modernbert-embed-base-bf16") == "mlx/nomicai-modernbert-embed-base-bf16#tp1"
    assert gateway.embedding_scheme("ollama/mxbai-embed-large:latest") == "ollama/mxbai-embed-large:latest"
    assert gateway.embedding_scheme("openai/text-embedding-3-small") == "openai/text-embedding-3-small"


def test_gateway_embed_prepends_document_prefix_for_nomic() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    with _mock(handler) as client:
        gateway.embed("the cargo vessel departed at dawn", "ollama/nomic-embed-text:v1.5",
                      task="document", client=client)
    # The prefix is on the INPUT; the wire model stays bare (the marker is storage-only).
    assert seen["body"]["input"] == "search_document: the cargo vessel departed at dawn"
    assert seen["body"]["model"] == "ollama/nomic-embed-text:v1.5"
    assert "#" not in seen["body"]["model"]


def test_gateway_embed_prepends_query_prefix_for_nomic() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    with _mock(handler) as client:
        gateway.embed("what did the ship do at sunrise", "ollama/nomic-embed-text:v1.5",
                      task="query", client=client)
    assert seen["body"]["input"] == "search_query: what did the ship do at sunrise"
    assert seen["body"]["model"] == "ollama/nomic-embed-text:v1.5"


def test_gateway_embed_leaves_non_nomic_wire_untouched() -> None:
    # Byte-identical to today for every non-nomic embed model — the prefix convention is
    # nomic-specific, and sending it to a model that wasn't trained on it corrupts results.
    for task in ("document", "query"):
        seen: dict = {}

        def handler(req: httpx.Request, s: dict = seen) -> httpx.Response:
            s["body"] = json.loads(req.content)
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

        with _mock(handler) as client:
            gateway.embed("raw passage", "ollama/mxbai-embed-large:latest",
                          task=task, client=client)
        assert seen["body"] == {"model": "ollama/mxbai-embed-large:latest", "input": "raw passage"}


def test_gateway_embed_prefix_survives_truncation() -> None:
    # The prefix goes on BEFORE the _MAX_EMBED_CHARS cap so a big-chunk head-truncation
    # can never eat the prefix (which would silently send raw text and defeat the point).
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    long_text = "x" * (gateway._MAX_EMBED_CHARS + 1000)
    with _mock(handler) as client:
        gateway.embed(long_text, "ollama/nomic-embed-text:v1.5", task="document", client=client)
    wire = seen["body"]["input"]
    assert wire.startswith("search_document: ")
    assert len(wire) == gateway._MAX_EMBED_CHARS  # total stays within the cap


def test_gateway_embed_rejects_unknown_task() -> None:
    def handler(req):
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    with _mock(handler) as client, pytest.raises(AssertionError):
        gateway.embed("hi", "ollama/nomic-embed-text:v1.5", task="bogus", client=client)


def test_bare_nomic_vectors_read_as_stale_under_new_scheme() -> None:
    # Migration mechanism: docs whose vectors were stored under the BARE nomic id (from before
    # this change) show up in docs_needing_embedding under the current '#tp1' scheme, so the
    # background indexer picks them up and re-embeds them WITH the task prefix.
    kb = _kb()
    doc_id = kb.add("Old", "content")
    kb.put_embedding(doc_id, [1.0, 0.0], "ollama/nomic-embed-text:v1.5")  # legacy row: bare id
    scheme = gateway.embedding_scheme("ollama/nomic-embed-text:v1.5")
    assert scheme == "ollama/nomic-embed-text:v1.5#tp1"
    assert doc_id in kb.docs_needing_embedding(scheme)  # stale under the new scheme
    assert kb.docs_pending_embedding(scheme) == 1


def test_bare_nomic_vectors_do_not_fuse_with_prefixed_queries() -> None:
    # Vectors embedded WITHOUT prefixes must never be scored against a prefixed query vector —
    # that's a cross-scheme comparison, meaningless in the same way a cross-model one is. The
    # semantic block is keyed on the storage scheme, so an old bare-id row is invisible.
    kb = _kb()
    doc_id = kb.add("Old", "content")
    kb.put_embedding(doc_id, [1.0, 0.0], "ollama/nomic-embed-text:v1.5")  # legacy bare id
    scheme = gateway.embedding_scheme("ollama/nomic-embed-text:v1.5")
    assert kb.semantic_search([1.0, 0.0], scheme) == []  # bare-stored row is invisible
    # After the indexer re-embeds under the new scheme, the same query finds it.
    kb.put_embedding(doc_id, [1.0, 0.0], scheme)
    assert [h["id"] for h in kb.semantic_search([1.0, 0.0], scheme)] == [doc_id]
