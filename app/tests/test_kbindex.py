"""Knowledge search: correctness, ranking, and index maintenance.

These are the regression guards for three real defects the old search shipped with:

1. It scanned only the 500 NEWEST documents (`ORDER BY created_at DESC LIMIT 500`), so an older
   document was silently unfindable — while the docstrings claimed it "reaches every doc".
2. Every semantic snippet was the first 160 chars of the document, because the chunk that actually
   matched was computed and then thrown away — the preview had nothing to do with the query.
3. Ranking was raw term frequency, so a big document that merely repeats a common word outranked a
   short, perfectly on-point one.
"""

from __future__ import annotations

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import kbindex
from smartbrain_3000.kb import _CHUNK_CHARS, KnowledgeBase, chunk_span
from smartbrain_3000.secrets import gen_master_key


def _kb() -> KnowledgeBase:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return KnowledgeBase(conn, gen_master_key())


# --- 1. the 500-document blindness ----------------------------------------------------------

def test_search_finds_the_oldest_document_in_a_large_corpus() -> None:
    # THE regression guard. The old lexical scan took the 500 newest documents, so in a 600-doc
    # corpus the first ones added were invisible to search — with no error and no warning.
    kb = _kb()
    kb.add("First", "aardvarkzero the very first document ever added")
    for i in range(600):
        kb.add(f"Filler {i}", f"routine filler content number {i}")
    hits = kb.search("aardvarkzero")
    assert [h["title"] for h in hits] == ["First"], "the oldest document must still be findable"
    assert kb.index.doc_count == 601  # the whole corpus is indexed, not a 500-doc window


def test_index_reports_truncation_instead_of_hiding_it(monkeypatch) -> None:
    # If a corpus ever DOES exceed the index ceiling, that must be visible — never silent, which was
    # the original sin. (Ceiling forced down here so the test doesn't have to build 100k documents.)
    monkeypatch.setattr(kbindex, "_MAX_INDEXED_DOCS", 3)
    kb = _kb()
    for i in range(5):
        kb.add(f"Doc {i}", f"content {i}")
    kb.index.ensure_built()
    assert kb.index.truncated is True
    assert kb.index.doc_count == 3


# --- 2. honest snippets ---------------------------------------------------------------------

def test_semantic_snippet_quotes_the_matched_passage_not_the_document_head() -> None:
    # The old code passed an empty term list, so every semantic snippet was content[:160] — the head
    # of the document, unrelated to why it matched. The snippet must come from the chunk that won.
    kb = _kb()
    # Built to exact chunk boundaries: chunk 0 is the head, chunk 1 holds the buried passage.
    head = "INTRO_TOKEN " + "x" * (_CHUNK_CHARS - len("INTRO_TOKEN "))
    doc_id = kb.add("Long", head + "BURIED_TREASURE the passage that matches")
    kb.put_embeddings(doc_id, [[1.0, 0.0], [0.0, 1.0]], "m")  # make chunk 1 the semantic winner

    hits = kb.semantic_search([0.0, 1.0], "m")
    assert hits and hits[0]["id"] == doc_id
    assert hits[0]["chunk_idx"] == 1, "must record WHICH chunk matched"
    assert "INTRO_TOKEN" not in hits[0]["snippet"], "snippet must not be the document head"
    assert "BURIED_TREASURE" in hits[0]["snippet"], "snippet must quote the passage that matched"

    start, end = chunk_span(kb.get(doc_id)["content"], 1)
    assert "BURIED_TREASURE" in kb.get(doc_id)["content"][start:end]


def test_chunk_span_inverts_chunking_including_leading_whitespace() -> None:
    # chunk_text slices content.strip(), so the span must add the stripped prefix back or every
    # citation/snippet lands a few characters off.
    content = "\n\n  " + ("a" * _CHUNK_CHARS) + ("b" * 10)
    s0, e0 = chunk_span(content, 0)
    s1, e1 = chunk_span(content, 1)
    assert content[s0:e0] == "a" * _CHUNK_CHARS
    assert content[s1:e1] == "b" * 10


# --- 3. ranking -----------------------------------------------------------------------------

def test_bm25_prefers_the_on_point_note_over_a_bloated_document() -> None:
    # Raw term frequency let a huge document win by sheer repetition. BM25's length normalisation is
    # what stops that, and it is the difference between "search works" and "search is useless".
    kb = _kb()
    kb.add("Bloated", ("lease " * 400) + ("unrelated padding " * 4000))
    kb.add("On point", "The lease renewal date is March 3rd.")
    hits = kb.search("lease renewal")
    assert hits[0]["title"] == "On point"


def test_rare_terms_outweigh_common_ones() -> None:
    # IDF: a term that appears in every document carries almost no signal.
    kb = _kb()
    for i in range(20):
        kb.add(f"Common {i}", "the report mentions revenue")
    kb.add("Rare", "the report mentions zebracorn")
    hits = kb.search("report zebracorn")
    assert hits[0]["title"] == "Rare"


# --- 4. hybrid ------------------------------------------------------------------------------

def test_hybrid_finds_what_each_mode_alone_would_miss() -> None:
    kb = _kb()
    exact = kb.add("Invoice", "invoice number QX-7741 is outstanding")   # only keyword can find this
    meaning = kb.add("Bill", "the amount owed on the account")            # only vectors can find this
    kb.put_embedding(exact, [1.0, 0.0], "m")
    kb.put_embedding(meaning, [0.0, 1.0], "m")

    found = {h["id"] for h in kb.hybrid_search("QX-7741", [0.0, 1.0], "m", limit=10)}
    assert exact in found, "the exact keyword hit must survive fusion"
    assert meaning in found, "the semantic-only hit must survive fusion"


def test_hybrid_degrades_to_lexical_without_a_query_vector() -> None:
    kb = _kb()
    kb.add("Tea", "oolong steeps at 90C")
    hits = kb.hybrid_search("oolong", None, "m")
    assert [h["title"] for h in hits] == ["Tea"]


def test_rrf_ranks_a_doc_found_by_both_runs_above_one_found_by_either() -> None:
    fused = dict(kbindex.fuse_rrf([["a", "b"], ["c", "a"]]))
    assert fused["a"] > fused["b"] and fused["a"] > fused["c"]


# --- 5. the index stays true as documents change --------------------------------------------

def test_new_document_is_searchable_immediately() -> None:
    kb = _kb()
    kb.search("anything")  # force the build, so the add below must update it incrementally
    kb.add("Fresh", "quokka sighting")
    assert [h["title"] for h in kb.search("quokka")] == ["Fresh"]


def test_deleted_document_disappears_from_results() -> None:
    kb = _kb()
    doc_id = kb.add("Doomed", "quokka sighting")
    assert kb.search("quokka")
    kb.delete(doc_id)
    assert kb.search("quokka") == [], "a deleted document must not keep turning up"


def test_renamed_document_is_findable_by_its_new_title() -> None:
    kb = _kb()
    doc_id = kb.add("Old name", "body text")
    assert kb.search("body")  # build the index
    kb.rename(doc_id, "Platypus")
    assert [h["title"] for h in kb.search("platypus")] == ["Platypus"]


def test_reembedding_reaches_the_index() -> None:
    # A background reindex writes vectors straight to the store; the live index must see them.
    kb = _kb()
    doc_id = kb.add("Doc", "content")
    assert kb.semantic_search([1.0, 0.0], "m") == []  # nothing embedded yet
    kb.put_embedding(doc_id, [1.0, 0.0], "m")
    assert [h["id"] for h in kb.semantic_search([1.0, 0.0], "m")] == [doc_id]


def test_vectors_from_another_model_are_never_scored() -> None:
    # Mid-reindex the corpus holds old-model and new-model rows side by side. Comparing across
    # embedding spaces is meaningless, so each model's vectors must stay in their own block.
    kb = _kb()
    old = kb.add("Old", "text")
    new = kb.add("New", "text")
    kb.put_embedding(old, [1.0, 0.0], "old-model")
    kb.put_embedding(new, [1.0, 0.0], "new-model")
    assert [h["id"] for h in kb.semantic_search([1.0, 0.0], "new-model")] == [new]


def test_search_survives_an_unreadable_document() -> None:
    # One corrupt row must not make the whole knowledge base unsearchable.
    kb = _kb()
    good = kb.add("Good", "quokka sighting")
    bad = kb.add("Bad", "quokka too")
    kb.conn.execute("UPDATE documents SET ciphertext = ? WHERE id = ?;", [b"\x00" * 32, bad])
    assert [h["id"] for h in kb.search("quokka")] == [good]


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_empty_corpus_returns_nothing_rather_than_raising(mode: str) -> None:
    kb = _kb()
    got = kb.search("anything") if mode == "lexical" else kb.hybrid_search("anything", None, "m")
    assert got == []
