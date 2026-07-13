#!/usr/bin/env python3
"""Benchmark knowledge search: index build + query latency as the corpus grows.

Why this exists: search used to decrypt the ENTIRE corpus on every query (semantic decrypted it
twice) and score cosine in a Python scalar loop, so latency grew linearly with the knowledge base.
It also only ever scanned the 500 newest documents, so it "stayed fast" past that point by silently
not looking. Search is now served from an in-memory BM25 + vector index (see kbindex.py); this
measures what that actually bought.

Run (no Docker needed):
    python bench/kb_search/bench_search.py
    python bench/kb_search/bench_search.py --sizes 100,1000,10000 --queries 50
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

import duckdb  # noqa: E402

from smartbrain_3000 import db as dbmod  # noqa: E402
from smartbrain_3000.kb import KnowledgeBase  # noqa: E402
from smartbrain_3000.secrets import gen_master_key  # noqa: E402

DIM = 768  # a typical embedding width (nomic-embed-text, bge-*)
WORDS = (
    "lease renewal invoice contract clause tenant landlord deposit arbitration counsel filing "
    "revenue quarterly statement dividend escrow custodian trustee registration prospectus "
    "meeting minutes agenda deadline reminder proposal budget forecast vendor supplier"
).split()


def _doc(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def _pct(values: list[float], p: float) -> float:
    return statistics.quantiles(values, n=100)[int(p) - 1] if len(values) > 1 else values[0]


def bench(n_docs: int, n_queries: int, words_per_doc: int) -> None:
    rng = random.Random(7)
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    kb = KnowledgeBase(conn, gen_master_key())

    t0 = time.perf_counter()
    for i in range(n_docs):
        doc_id = kb.add(f"Document {i}", _doc(rng, words_per_doc))
        vec = [rng.random() for _ in range(DIM)]
        kb.put_embedding(doc_id, vec, "bench-model")
    ingest_s = time.perf_counter() - t0

    kb._index = None  # drop any incrementally-maintained state: measure a COLD build, as after unlock
    t0 = time.perf_counter()
    kb.index.ensure_built()
    build_s = time.perf_counter() - t0

    qvec = [rng.random() for _ in range(DIM)]
    lex, sem, hyb = [], [], []
    for _ in range(n_queries):
        q = " ".join(rng.choice(WORDS) for _ in range(3))
        t0 = time.perf_counter()
        kb.search(q)
        lex.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        kb.semantic_search(qvec, "bench-model")
        sem.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        kb.hybrid_search(q, qvec, "bench-model")
        hyb.append((time.perf_counter() - t0) * 1000)

    print(f"\n{n_docs:,} docs  ({words_per_doc} words each)")
    print(f"  ingest+embed : {ingest_s:7.2f} s   (setup only, not the thing under test)")
    print(f"  index build  : {build_s * 1000:7.1f} ms  (once per unlock)")
    for name, xs in (("lexical", lex), ("semantic", sem), ("hybrid", hyb)):
        print(f"  {name:<9}    p50 {statistics.median(xs):6.1f} ms   p95 {_pct(xs, 95):6.1f} ms   max {max(xs):6.1f} ms")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="100,1000,10000")
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--words", type=int, default=400, help="words per document (~2.5 KB)")
    args = ap.parse_args()
    print(f"KB search benchmark — {DIM}-dim vectors, {args.queries} queries per size")
    for size in [int(s) for s in args.sizes.split(",")]:
        bench(size, args.queries, args.words)


if __name__ == "__main__":
    main()
