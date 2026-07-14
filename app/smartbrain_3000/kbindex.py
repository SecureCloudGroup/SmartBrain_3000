"""In-memory search index over the encrypted knowledge base.

WHY THIS EXISTS — the old search had three defects, all of them user-visible:

1. **It went blind past 500 documents.** Lexical search ran
   ``SELECT ... FROM documents ORDER BY created_at DESC LIMIT 500``, so document #501 and older were
   simply unfindable — silently, while the docstrings claimed it "reaches every doc".
2. **It decrypted the whole corpus on every query.** Lexical decrypted every scanned document;
   semantic decrypted every vector *and then every document again* just to cut a snippet. Cosine was
   a hand-rolled Python scalar loop.
3. **Its ranking was raw term frequency**, so a 1 MB document that happens to repeat a common word
   outranked a perfectly on-point 2 KB note.

This module fixes all three. The corpus is decrypted **once** per unlock into an inverted index
(BM25) plus a per-(model, dim) matrix of L2-normalised chunk vectors. A query is then O(postings for
the query's terms) plus one BLAS mat-vec — not O(corpus). Only the handful of documents actually
returned are decrypted again, to cut their snippets from the passage that really matched.

The index is **RAM-only and never persisted**: encryption at rest is unchanged. It is rebuilt on the
next unlock (a fresh KnowledgeBase is constructed per unlock) and maintained incrementally as
documents are added, renamed or deleted, so a write never forces a full rebuild.

Cost, for orientation: postings are small; the vectors dominate. At 768 dims, float32, ~10 chunks per
document that is ~30 MB per 1,000 documents. The first search after unlock pays the one-time build.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading

import numpy as np

log = logging.getLogger(__name__)

# BM25 (Robertson/Sparck-Jones). k1 damps runaway term frequency; b is the length normalisation that
# stops a huge document from winning on sheer size alone.
_K1 = 1.5
_B = 0.75

_MAX_INDEXED_DOCS = 100_000  # verifiable ceiling on the corpus we will index (P10 #2)
_MAX_QUERY_TERMS = 32  # bounds the postings walk per query
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)  # letters/digits; splits on punctuation and underscores


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens. Shared by indexing and querying so they cannot drift apart."""
    assert text is not None, "text required"
    return _TOKEN.findall(text.lower())


def content_hash(content: str) -> str:
    """Fingerprint a document's text, for duplicate detection.

    Held only in memory (the index), never written to disk — a stored hash would be a plaintext
    fingerprint of encrypted content, which is exactly what we don't keep. The index already
    decrypts the whole corpus once per unlock, so hashing it there is free.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class _VecBlock:
    """L2-normalised chunk vectors for ONE (model, dim) pair, so cosine is a plain dot product.

    Vectors from a *different* embed model live in their own block and are never scored against this
    one — a cross-model comparison is meaningless, and this is what makes a mid-reindex corpus (old
    model + new model rows side by side) safe to query.
    """

    def __init__(self, dim: int) -> None:
        assert dim >= 1, "dim must be positive"
        self.dim = dim
        self.rows: list[tuple[str, int]] = []  # row i -> (doc_id, chunk_idx)
        self.matrix = np.zeros((0, dim), dtype=np.float32)

    @staticmethod
    def _normalized(vectors: list[list[float]]) -> np.ndarray:
        block = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(block, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0  # a zero vector stays zero rather than becoming NaN
        return block / norms

    def bulk_load(self, per_doc: dict[str, list[list[float]]]) -> None:
        """Load a whole corpus in ONE pass. Used by the index build.

        Do NOT build by calling add() per document: each add() vstacks the growing matrix and
        rescans every row, which is O(n^2). Measured on 10k documents that was a 19-second index
        build; assembling the matrix once brings it back to linear.
        """
        rows: list[tuple[str, int]] = []
        blocks: list[np.ndarray] = []
        for doc_id, vectors in per_doc.items():  # bounded by the corpus
            if not vectors:
                continue
            blocks.append(self._normalized(vectors))
            rows.extend((doc_id, i) for i in range(len(vectors)))
        if not blocks:
            return
        self.matrix = np.vstack(blocks) if len(blocks) > 1 else blocks[0]
        self.rows = rows

    def add(self, doc_id: str, vectors: list[list[float]]) -> None:
        """Append ONE document's chunk vectors (replacing any it already had). Incremental path."""
        self.remove(doc_id)
        if not vectors:
            return
        block = self._normalized(vectors)
        self.matrix = np.vstack([self.matrix, block]) if self.matrix.size else block
        self.rows.extend((doc_id, i) for i in range(len(vectors)))

    def remove(self, doc_id: str) -> None:
        """Drop every row belonging to ``doc_id``."""
        if not self.rows:
            return
        keep = [i for i, (did, _) in enumerate(self.rows) if did != doc_id]
        if len(keep) == len(self.rows):
            return
        self.rows = [self.rows[i] for i in keep]
        self.matrix = self.matrix[keep] if keep else np.zeros((0, self.dim), dtype=np.float32)

    def best_by_doc(self, query: list[float], min_score: float) -> dict[str, tuple[float, int]]:
        """doc_id -> (best cosine, the chunk_idx that produced it). One mat-vec over every chunk.

        Keeping the winning ``chunk_idx`` is the whole point: the old code computed it and threw it
        away, which is why every semantic snippet was the head of the document instead of the passage
        that actually matched.
        """
        if not self.rows:
            return {}
        # Check finiteness BEFORE the float32 cast: a value beyond float32's range would silently
        # become inf (and warn), and an inf/NaN query would rank the whole corpus at random.
        if not all(math.isfinite(x) for x in query):  # bounded by the vector dim
            return {}
        q = np.asarray(query, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n == 0.0 or not math.isfinite(n):
            return {}
        scores = self.matrix @ (q / n)  # cosine: both sides are unit-length
        best: dict[str, tuple[float, int]] = {}
        for i in np.nonzero(scores > min_score)[0]:  # bounded by the row count
            doc_id, chunk_idx = self.rows[int(i)]
            score = float(scores[int(i)])
            if score > best.get(doc_id, (min_score, -1))[0]:
                best[doc_id] = (score, chunk_idx)
        return best


class SearchIndex:
    """BM25 + vector index over a KnowledgeBase. Built lazily; then kept up to date incrementally."""

    def __init__(self, kb) -> None:
        assert kb is not None, "knowledge base required"
        self._kb = kb
        self._lock = threading.RLock()  # a build and a concurrent write must not interleave
        self._built = False
        self._postings: dict[str, dict[str, int]] = {}  # token -> {doc_id: term frequency}
        self._doc_len: dict[str, int] = {}  # doc_id -> token count (BM25 length normalisation)
        self._titles: dict[str, str] = {}  # kept in RAM: small, and lets us rank/label without a decrypt
        self._by_hash: dict[str, str] = {}  # content hash -> doc_id, for duplicate detection
        self._vecs: dict[tuple[str, int], _VecBlock] = {}  # (model, dim) -> vectors
        self.truncated = False  # True if the corpus exceeded _MAX_INDEXED_DOCS

    # --- build / maintenance -------------------------------------------------------------------

    def ensure_built(self) -> None:
        """Build on first use. The one-time cost of decrypting the corpus, paid once per unlock."""
        with self._lock:
            if self._built:
                return
            self._build_locked()

    def _build_locked(self) -> None:
        docs = self._kb.iter_documents(limit=_MAX_INDEXED_DOCS)
        self.truncated = len(docs) >= _MAX_INDEXED_DOCS
        if self.truncated:
            log.warning("knowledge base exceeds %d documents; search covers the newest %d",
                        _MAX_INDEXED_DOCS, _MAX_INDEXED_DOCS)
        for doc_id, title, content in docs:  # bounded by _MAX_INDEXED_DOCS
            self._index_text(doc_id, title, content)
        for model, dim, per_doc in self._kb.iter_embeddings():
            self._vecs.setdefault((model, dim), _VecBlock(dim)).bulk_load(per_doc)  # one pass, not O(n^2)
        self._built = True
        log.info("search index built: %d docs, %d tokens, %d vector blocks",
                 len(self._doc_len), len(self._postings), len(self._vecs))

    def _index_text(self, doc_id: str, title: str, content: str) -> None:
        tokens = tokenize(f"{title}\n{content}")
        freqs: dict[str, int] = {}
        for tok in tokens:
            freqs[tok] = freqs.get(tok, 0) + 1
        for tok, tf in freqs.items():
            self._postings.setdefault(tok, {})[doc_id] = tf
        self._doc_len[doc_id] = len(tokens)
        self._titles[doc_id] = title
        self._by_hash.setdefault(content_hash(content), doc_id)  # first one wins; a dupe maps to the original

    def find_by_content(self, content: str) -> str | None:
        """The id of an existing document with identical text, if any. Powers duplicate detection."""
        self.ensure_built()
        with self._lock:
            return self._by_hash.get(content_hash(content))

    def add_document(self, doc_id: str, title: str, content: str) -> None:
        """Index a new/updated document. No-op before the first build (the build will pick it up)."""
        with self._lock:
            if not self._built:
                return
            self.remove_document(doc_id)
            self._index_text(doc_id, title, content)

    def remove_document(self, doc_id: str) -> None:
        """Forget a document (its postings and every vector block row)."""
        with self._lock:
            if not self._built:
                return
            for tok in list(self._postings):  # bounded by the vocabulary
                if self._postings[tok].pop(doc_id, None) is not None and not self._postings[tok]:
                    del self._postings[tok]
            self._doc_len.pop(doc_id, None)
            self._titles.pop(doc_id, None)
            for h, did in list(self._by_hash.items()):  # bounded by the corpus
                if did == doc_id:
                    del self._by_hash[h]
            for block in self._vecs.values():
                block.remove(doc_id)

    def set_vectors(self, doc_id: str, vectors: list[list[float]], model: str) -> None:
        """Replace a document's chunk vectors for ``model`` (called after (re)embedding)."""
        with self._lock:
            if not self._built or not vectors:
                return
            dim = len(vectors[0])
            self._vecs.setdefault((model, dim), _VecBlock(dim)).add(doc_id, vectors)

    # --- query ---------------------------------------------------------------------------------

    @property
    def _avg_len(self) -> float:
        return (sum(self._doc_len.values()) / len(self._doc_len)) if self._doc_len else 0.0

    def lexical(self, query: str, limit: int, scope: set[str] | None = None) -> list[tuple[str, float]]:
        """BM25-ranked (doc_id, score), best first. Covers the WHOLE corpus, not the newest 500.

        ``scope`` restricts results to those document ids (a vault). Note that IDF is still computed
        over the WHOLE corpus, not the scope: a word's rarity is a property of the library, and
        recomputing it per-scope would make the same document rank differently depending on which
        vault you happened to search — surprising, and no more correct.
        """
        assert limit >= 1, "limit must be positive"
        self.ensure_built()
        with self._lock:
            terms = tokenize(query)[:_MAX_QUERY_TERMS]
            n = len(self._doc_len)
            if not terms or n == 0:
                return []
            avg = self._avg_len or 1.0
            scores: dict[str, float] = {}
            for term in terms:  # bounded by _MAX_QUERY_TERMS
                postings = self._postings.get(term)
                if not postings:
                    continue
                df = len(postings)
                idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))  # always positive; no negative IDF
                for doc_id, tf in postings.items():  # bounded by the corpus
                    if scope is not None and doc_id not in scope:
                        continue
                    norm = 1.0 - _B + _B * (self._doc_len.get(doc_id, 0) / avg)
                    scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (_K1 + 1.0)) / (tf + _K1 * norm)
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            return ranked[:limit]

    def semantic(
        self, query_vector: list[float], model: str, limit: int, min_score: float,
        scope: set[str] | None = None,
    ) -> list[tuple[str, float, int]]:
        """Cosine-ranked (doc_id, score, matched chunk_idx), best first. ``scope`` restricts to a vault.

        Scoping filters BEFORE the top-k cut, so a scoped search returns the best `limit` documents
        IN the vault — not whatever survives from the best `limit` of the whole corpus.
        """
        assert query_vector, "query vector required"
        assert model, "model required"
        assert limit >= 1, "limit must be positive"
        self.ensure_built()
        with self._lock:
            block = self._vecs.get((model, len(query_vector)))
            if block is None:
                return []  # nothing embedded with this model/dim (e.g. the routed model just changed)
            best = block.best_by_doc(query_vector, min_score)
        if scope is not None:
            best = {d: v for d, v in best.items() if d in scope}
        ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
        return [(doc_id, score, chunk_idx) for doc_id, (score, chunk_idx) in ranked[:limit]]

    def title_of(self, doc_id: str) -> str:
        with self._lock:
            return self._titles.get(doc_id, "")

    @property
    def doc_count(self) -> int:
        with self._lock:
            return len(self._doc_len)


def fuse_rrf(runs: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion of several ranked doc_id lists -> (doc_id, fused score), best first.

    Hybrid search has to merge two incomparable scales — BM25 (unbounded) and cosine ([-1,1]).
    Normalising one against the other means inventing an exchange rate; RRF instead fuses on RANK,
    which is scale-free, and is the standard way to combine a lexical and a semantic run. A document
    found by BOTH runs is pushed up, which is exactly the behaviour we want.
    """
    assert k >= 1, "k must be positive"
    scores: dict[str, float] = {}
    for run in runs:  # bounded by the number of runs (2)
        for rank, doc_id in enumerate(run):  # bounded by each run's limit
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
