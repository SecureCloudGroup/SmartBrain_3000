"""Provenance: where a document came from, and which PAGE a match is on.

This is the foundation of citations. The information already existed and was thrown away:
ingest._extract_pdf built a per-page list and immediately flattened it with "\\n".join, so page
boundaries — the only place they are ever known — were lost. The uploaded filename was used to
derive a title and then discarded too. A search hit could therefore only ever say "this document",
never "Lease.pdf, page 12".

Provenance is stored INSIDE the encrypted body: where a document came from is exactly as sensitive
as what it says.
"""

from __future__ import annotations

import duckdb

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ingest
from smartbrain_3000.kb import KnowledgeBase, chunk_span, page_for
from smartbrain_3000.secrets import gen_master_key

from _pdfgen import make_pdf


def _kb() -> KnowledgeBase:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return KnowledgeBase(conn, gen_master_key())


# --- the page map survives extraction ---------------------------------------------------------

def test_pdf_extraction_records_where_each_page_starts() -> None:
    pdf = make_pdf(["alpha page one", "beta page two", "gamma page three"])
    _, text, pages = ingest._extract_pdf(pdf)
    assert len(pages) == 3, "one start offset per page"
    assert pages[0] == 0
    # Each recorded offset must actually land on that page's text in the extracted string.
    assert text[pages[1]:].startswith("beta")
    assert text[pages[2]:].startswith("gamma")


def test_page_for_maps_an_offset_back_to_its_page() -> None:
    meta = {"pages": [0, 100, 250]}
    assert page_for(meta, 0) == 1
    assert page_for(meta, 99) == 1
    assert page_for(meta, 100) == 2  # exactly on the boundary -> the new page
    assert page_for(meta, 249) == 2
    assert page_for(meta, 250) == 3
    assert page_for(meta, 10_000) == 3  # past the end -> the last page
    assert page_for({}, 5) is None, "a document with no pages has no page number"


def test_upload_keeps_the_filename_and_the_page_map() -> None:
    title, text, meta = ingest.from_file("Lease.pdf", make_pdf(["page one text", "page two text"]))
    assert meta["filename"] == "Lease.pdf"  # was previously consumed for the title, then dropped
    assert len(meta["pages"]) == 2
    assert title and text


def test_url_ingest_keeps_the_source_url(monkeypatch) -> None:
    monkeypatch.setattr(
        ingest.netguard, "safe_fetch_bytes",
        lambda url: {"content": b"<html><body><article>Hello there world</article></body></html>",
                     "content_type": "text/html", "final_url": "https://example.com/a/page"},
    )
    _, _, meta = ingest.from_url("https://example.com/a/page")
    assert meta["source_url"] == "https://example.com/a/page"
    assert meta["pages"] == []  # a web page has no page numbers


# --- a search hit is a citation ---------------------------------------------------------------

def test_search_hit_cites_the_source_file_and_page() -> None:
    kb = _kb()
    # Pages big enough that the target lands on page 3 well past the first chunk boundary.
    pages = [("filler " * 500) for _ in range(2)] + ["the PLATYPUS clause governs renewal"]
    title, text, meta = ingest.from_file("Lease.pdf", make_pdf(pages))
    kb.add(title, text, meta)

    hit = kb.search("platypus")[0]
    assert hit["source"] == "Lease.pdf", "the citation must name the file the user recognises"
    assert hit["page"] == 3, "the citation must name the page the passage is actually on"
    assert "PLATYPUS" in text[hit["offset"]:hit["offset"] + 200], "offset must point AT the passage"


def test_semantic_hit_cites_the_page_of_the_chunk_that_matched() -> None:
    kb = _kb()
    long_page = "filler " * 700  # > one chunk, so later pages fall in later chunks
    title, text, meta = ingest.from_file("Report.pdf", make_pdf([long_page, long_page, "the QUOKKA finding"]))
    doc_id = kb.add(title, text, meta)

    n_chunks = len(range(0, len(text.strip()), 4000))
    vectors = [[1.0, 0.0] for _ in range(n_chunks)]
    vectors[-1] = [0.0, 1.0]  # make the LAST chunk (holding the quokka text) the winner
    kb.put_embeddings(doc_id, vectors, "m")

    # A pure VECTOR hit has no query terms, so the honest answer is the page where the matching
    # chunk begins — we know the chunk matched, not which sentence in it did.
    hit = kb.semantic_search([0.0, 1.0], "m")[0]
    assert hit["chunk_idx"] == n_chunks - 1
    start, _ = chunk_span(text, hit["chunk_idx"])
    assert hit["page"] == page_for(meta, start), "page must be derived from the chunk that matched"

    # A HYBRID hit does have terms, so it can pin the exact passage — even though this chunk
    # straddles a page boundary (the chunk starts on an earlier page than the quokka text).
    hybrid = kb.hybrid_search("quokka", [0.0, 1.0], "m")[0]
    assert "QUOKKA" in text[hybrid["offset"]:hybrid["offset"] + 40]
    assert hybrid["page"] == page_for(meta, text.index("QUOKKA")) == 3
    assert hybrid["page"] != hit["page"], "locating the term in the chunk is what fixes the off-by-a-page"


def test_a_note_without_provenance_still_searches_and_cites_nothing() -> None:
    # save_note and pre-provenance documents have no meta at all. They must keep working.
    kb = _kb()
    kb.add("Note", "a plain note about wombats")
    hit = kb.search("wombats")[0]
    assert hit["source"] == "" and hit["page"] is None


def test_documents_sealed_before_provenance_existed_still_open() -> None:
    # Back-compat: an old body is {"title", "content"} with no "meta" key. It must not fail to open.
    kb = _kb()
    doc_id = kb.add("Old", "legacy body")
    nonce, ct = kb._seal.__wrapped__(kb, doc_id, "Old", "legacy body") if hasattr(kb._seal, "__wrapped__") else (None, None)
    # Re-seal by hand in the OLD shape (no "meta" key) to simulate a pre-upgrade row.
    import json
    import os as _os
    old_nonce = _os.urandom(12)
    plain = json.dumps({"title": "Old", "content": "legacy body"}).encode()
    old_ct = kb._aes.encrypt(old_nonce, plain, doc_id.encode())
    kb.conn.execute("UPDATE documents SET nonce=?, ciphertext=? WHERE id=?;", [old_nonce, old_ct, doc_id])

    doc = kb.get(doc_id)
    assert doc["content"] == "legacy body"
    assert doc["meta"] == {}  # defaulted, not asserted
    kb._index = None  # rebuild the index off the legacy row
    assert [h["title"] for h in kb.search("legacy")] == ["Old"]


def test_rename_preserves_provenance() -> None:
    kb = _kb()
    title, text, meta = ingest.from_file("Lease.pdf", make_pdf(["page one", "page two"]))
    doc_id = kb.add(title, text, meta)
    kb.rename(doc_id, "Renamed Lease")
    doc = kb.get(doc_id)
    assert doc["title"] == "Renamed Lease"
    assert doc["meta"]["filename"] == "Lease.pdf", "a rename must not throw the citation source away"
    assert doc["meta"]["pages"]
