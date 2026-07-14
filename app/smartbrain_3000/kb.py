"""Local knowledge base: documents encrypted at rest, searched through an in-memory index.

Each document's ``{title, content}`` is encrypted with AES-256-GCM under the master key (auth tag
bound to the document id) and stored in DuckDB. Search does NOT read that ciphertext per query:
``kbindex.SearchIndex`` decrypts the corpus once per unlock into a BM25 inverted index plus a matrix
of chunk vectors, and every query is answered from RAM. Only the handful of documents actually
returned are decrypted again, to cut their snippets from the passage that really matched.

See ``kbindex`` for why: search used to scan only the 500 newest documents (older ones were silently
unfindable), decrypt the whole corpus on every keystroke, and rank by raw term frequency.
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import kbindex
from .secrets import MASTER_KEY_BYTES

log = logging.getLogger(__name__)

_NONCE_BYTES = 12
_SEARCH_SCAN_LIMIT = 500  # cap on a search's RESULT count
_REINDEX_SCAN_LIMIT = 50_000  # max docs surfaced by docs_needing_embedding (lets reindex converge)
# Ceilings on what the in-memory index holds (P10 #2 — a verifiable bound). Unlike the old
# `LIMIT 500` these are sized for a real corpus, and exceeding them is reported, never silent.
_MAX_INDEXED_VECTORS = 100_000  # ~10k docs at ~10 chunks each; ~300 MB at 768 dims, float32
_SNIPPET_CHARS = 160
_MAX_EMBED_DIM = 4096  # hard upper bound on any embedding vector length
_SEMANTIC_MIN_SCORE = 0.0  # cosine is [-1,1]; only surface positive matches
_CHUNK_CHARS = 4000  # per-chunk size; safely under the embed model's context window
_MAX_CHUNKS = 64  # cap chunks per doc (verifiable bound; ~256k chars covered)


def chunk_text(title: str, content: str) -> list[str]:
    """Split a document into embed-sized chunks, each prefixed with the title so a
    title match stays reachable in every chunk. Bounded by ``_MAX_CHUNKS``."""
    assert title, "title required"
    assert content is not None, "content required"
    body = content.strip()
    if not body:
        return [title]
    chunks = [f"{title}\n{body[i : i + _CHUNK_CHARS]}" for i in range(0, len(body), _CHUNK_CHARS)]
    return chunks[:_MAX_CHUNKS]


def chunk_span(content: str, chunk_idx: int) -> tuple[int, int]:
    """Character range of ``content`` covered by chunk ``chunk_idx`` — the inverse of ``chunk_text``.

    ``chunk_text`` slices ``content.strip()``, so the leading whitespace it dropped has to be added
    back to land on the right characters of the ORIGINAL string. This is what lets a semantic hit
    quote the passage that matched (and, later, cite it).
    """
    assert content is not None, "content required"
    assert chunk_idx >= 0, "chunk idx must be non-negative"
    lead = len(content) - len(content.lstrip())
    body_len = len(content.strip())
    start = min(chunk_idx * _CHUNK_CHARS, body_len)
    end = min(start + _CHUNK_CHARS, body_len)
    return lead + start, lead + end


class KnowledgeBase:
    """AES-256-GCM document store over DuckDB's ``documents`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)
        self._index: kbindex.SearchIndex | None = None

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """The underlying connection (lets ingest read the routed embedding model)."""
        return self._conn

    @property
    def index(self) -> kbindex.SearchIndex:
        """The in-memory search index. Created lazily and built on first search, so constructing a
        KnowledgeBase (which happens on every unlock, and all over the tests) stays free. A fresh
        instance per unlock means the index never outlives the master key."""
        if self._index is None:
            self._index = kbindex.SearchIndex(self)
        return self._index

    # --- index data sources (each decrypts the corpus exactly ONCE, at build time) --------------

    def iter_documents(self, limit: int) -> list[tuple[str, str, str]]:
        """Decrypt every document once: (id, title, content). For the index build only.

        An unreadable row is skipped rather than sinking the whole build — one corrupt document
        must not make the entire knowledge base unsearchable.
        """
        assert limit >= 1, "limit must be positive"
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext FROM documents ORDER BY created_at DESC LIMIT ?;", [limit]
        ).fetchall()
        out: list[tuple[str, str, str]] = []
        for row in rows:  # bounded by `limit`
            doc_id = str(row[0])
            try:
                body = self._open(doc_id, bytes(row[1]), bytes(row[2]))
            except Exception as exc:  # corrupt/tampered row — skip it, keep the index usable
                log.warning("skipping unreadable document %s: %s", doc_id, exc)
                continue
            out.append((doc_id, body["title"], body["content"]))
        return out

    def iter_embeddings(self) -> list[tuple[str, int, dict[str, list[list[float]]]]]:
        """Decrypt every stored vector once, grouped by (model, dim) then doc, in chunk order.

        Grouping by model keeps a mid-reindex corpus (old-model rows beside new-model rows) safe:
        each model's vectors live in their own block and are only ever scored against a query
        embedded by that same model.
        """
        rows = self._conn.execute(
            "SELECT doc_id, chunk_idx, nonce, ciphertext, dim, model FROM embeddings "
            f"ORDER BY doc_id, chunk_idx LIMIT {_MAX_INDEXED_VECTORS};"
        ).fetchall()
        grouped: dict[tuple[str, int], dict[str, list[list[float]]]] = {}
        for row in rows:  # bounded by _MAX_INDEXED_VECTORS
            doc_id, chunk_idx, dim, model = str(row[0]), int(row[1]), int(row[4]), str(row[5])
            try:
                vector = self._open_embedding(doc_id, chunk_idx, bytes(row[2]), bytes(row[3]), dim, model)
            except Exception as exc:  # corrupt row must not sink the build
                log.warning("skipping unreadable embedding for %s: %s", doc_id, exc)
                continue
            grouped.setdefault((model, dim), {}).setdefault(doc_id, []).append(vector)
        return [(model, dim, per_doc) for (model, dim), per_doc in grouped.items()]

    def add(self, title: str, content: str) -> str:
        """Encrypt and store a document; return its new id."""
        assert title, "title must be non-empty"
        assert content is not None, "content must not be None"
        doc_id = str(uuid.uuid4())
        nonce, ciphertext = self._seal(doc_id, title, content)
        self._conn.execute(
            "INSERT INTO documents (id, nonce, ciphertext) VALUES (?, ?, ?);",
            [doc_id, nonce, ciphertext],
        )
        self.index.add_document(doc_id, title, content)  # keep the index live (no-op until it's built)
        return doc_id

    def rename(self, doc_id: str, title: str) -> bool:
        """Change a document's title (content unchanged); False if it doesn't exist."""
        assert doc_id, "doc id required"
        assert title, "title must be non-empty"
        doc = self.get(doc_id)
        if doc is None:
            return False
        nonce, ciphertext = self._seal(doc_id, title, doc["content"])
        self._conn.execute(
            "UPDATE documents SET nonce = ?, ciphertext = ?, updated_at = now() WHERE id = ?;",
            [nonce, ciphertext, doc_id],
        )
        self.index.add_document(doc_id, title, doc["content"])  # title is indexed, so re-index it
        return True

    def get(self, doc_id: str) -> dict | None:
        """Return the decrypted document, or None if absent."""
        assert doc_id, "doc id required"
        row = self._conn.execute(
            "SELECT nonce, ciphertext, created_at, updated_at FROM documents WHERE id = ?;",
            [doc_id],
        ).fetchone()
        if row is None:
            return None
        assert len(row) == 4, "unexpected documents row shape"
        body = self._open(doc_id, bytes(row[0]), bytes(row[1]))
        return {"id": doc_id, "created_at": str(row[2]), "updated_at": str(row[3]), **body}

    def delete(self, doc_id: str) -> None:
        """Remove a document and its embedding (no error if absent)."""
        assert doc_id, "doc id required"
        self._conn.execute("DELETE FROM embeddings WHERE doc_id = ?;", [doc_id])
        self._conn.execute("DELETE FROM documents WHERE id = ?;", [doc_id])
        self.index.remove_document(doc_id)  # or a deleted doc keeps turning up in results
        assert self.get(doc_id) is None, "document must be absent after delete"
        assert self.get_embedding(doc_id) is None, "embedding must be absent after delete"

    def list_docs(self) -> list[dict]:
        """Return id/title/timestamps for all documents (newest first)."""
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext, created_at, updated_at FROM documents "
            "ORDER BY created_at DESC;"
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for row in rows:
            body = self._open(str(row[0]), bytes(row[1]), bytes(row[2]))
            out.append(
                {
                    "id": str(row[0]),
                    "title": body["title"],
                    "created_at": str(row[3]),
                    "updated_at": str(row[4]),
                }
            )
        return out

    def _hit(self, doc_id: str, score: float, chunk_idx: int | None, terms: list[str]) -> dict | None:
        """Build one result row. Decrypts THIS document — only the handful we actually return.

        The snippet is cut from the matched chunk when we know which one it was (a semantic hit), so
        it quotes the passage that caused the match instead of the head of the document.
        """
        doc = self.get(doc_id)
        if doc is None:
            return None  # raced with a delete, or an orphan vector
        content = doc["content"]
        if chunk_idx is not None:
            start, end = chunk_span(content, chunk_idx)
            snippet = self._snippet(content[start:end], terms)
        else:
            snippet = self._snippet(content, terms)
        return {
            "id": doc_id,
            "title": doc["title"],
            "score": score,
            "snippet": snippet,
            "chunk_idx": chunk_idx,
        }

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Lexical search over the WHOLE corpus, ranked by BM25.

        Was: raw term-frequency over only the 500 newest documents (older ones silently unfindable).
        Now: BM25 (which normalises for length, so a long document can't win on size alone) served
        from the in-memory index.
        """
        assert query, "query must be non-empty"
        assert 1 <= limit <= _SEARCH_SCAN_LIMIT, "limit out of range"
        terms = kbindex.tokenize(query)
        assert terms, "query must contain at least one term"
        ranked = self.index.lexical(query, limit)
        hits = [self._hit(doc_id, score, None, terms) for doc_id, score in ranked]
        return [h for h in hits if h is not None]

    def hybrid_search(self, query: str, query_vector: list[float] | None, model: str, limit: int = 10) -> list[dict]:
        """Lexical AND semantic, fused by rank (RRF) — the default for the app and the agent.

        Keyword search finds exact names/numbers; vector search finds meaning. Fusing them beats
        either alone, and RRF fuses on RANK so we never have to invent an exchange rate between a
        BM25 score and a cosine. Degrades to lexical when no query vector is available.
        """
        assert query, "query must be non-empty"
        assert 1 <= limit <= _SEARCH_SCAN_LIMIT, "limit out of range"
        terms = kbindex.tokenize(query)
        assert terms, "query must contain at least one term"
        depth = min(limit * 2, _SEARCH_SCAN_LIMIT)  # look deeper than we return, so fusion can re-rank
        lexical = self.index.lexical(query, depth)
        semantic = (
            self.index.semantic(query_vector, model, depth, _SEMANTIC_MIN_SCORE) if query_vector else []
        )
        chunk_of = {doc_id: chunk_idx for doc_id, _, chunk_idx in semantic}
        fused = kbindex.fuse_rrf([[d for d, _ in lexical], [d for d, _, _ in semantic]])
        hits = [self._hit(doc_id, score, chunk_of.get(doc_id), terms) for doc_id, score in fused[:limit]]
        return [h for h in hits if h is not None]

    def put_embeddings(self, doc_id: str, vectors: list[list[float]], model: str) -> None:
        """Replace a document's per-chunk embedding vectors (one row per chunk)."""
        assert doc_id, "doc id required"
        assert vectors, "at least one vector required"
        assert len(vectors) <= _MAX_CHUNKS, "too many chunks"
        assert model, "model required"
        self._conn.execute("DELETE FROM embeddings WHERE doc_id = ?;", [doc_id])  # replace all chunks
        for idx, vector in enumerate(vectors):  # bounded by _MAX_CHUNKS
            assert 1 <= len(vector) <= _MAX_EMBED_DIM, "vector dim out of range"
            assert all(math.isfinite(x) for x in vector), "vector elements must be finite"
            dim = len(vector)
            nonce = os.urandom(_NONCE_BYTES)
            plaintext = struct.pack(f"<{dim}f", *vector)
            ciphertext = self._aes.encrypt(nonce, plaintext, self._embed_aad(doc_id, idx, dim, model))
            self._conn.execute(
                "INSERT INTO embeddings (doc_id, chunk_idx, nonce, ciphertext, dim, model) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                [doc_id, idx, nonce, ciphertext, dim, model],
            )
        self.index.set_vectors(doc_id, vectors, model)  # a background reindex must reach the index

    def put_embedding(self, doc_id: str, vector: list[float], model: str) -> None:
        """Store a single-chunk embedding (back-compat wrapper over put_embeddings)."""
        assert vector, "vector must be non-empty"
        self.put_embeddings(doc_id, [vector], model)

    def get_embedding(self, doc_id: str) -> tuple[list[float], str] | None:
        """Return (first-chunk vector, model) for a doc, or None if absent."""
        assert doc_id, "doc id required"
        row = self._conn.execute(
            "SELECT nonce, ciphertext, dim, model FROM embeddings WHERE doc_id = ? AND chunk_idx = 0;",
            [doc_id],
        ).fetchone()
        if row is None:
            return None
        assert len(row) == 4, "unexpected embeddings row shape"
        vector = self._open_embedding(doc_id, 0, bytes(row[0]), bytes(row[1]), int(row[2]), str(row[3]))
        return vector, str(row[3])

    def docs_needing_embedding(self, model: str) -> list[str]:
        """Return ids of docs with no embedding or one from a different model."""
        assert model, "model required"
        rows = self._conn.execute(
            "SELECT d.id FROM documents d "
            "WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.doc_id = d.id AND e.model = ?) "
            f"ORDER BY d.created_at DESC LIMIT {_REINDEX_SCAN_LIMIT};",
            [model],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        return [str(r[0]) for r in rows]

    def semantic_search(self, query_vector: list[float], model: str, limit: int = 10) -> list[dict]:
        """Rank docs by cosine similarity to ``query_vector``; return top hits.

        A document scores as its best-matching chunk, and we KEEP which chunk that was — so the
        snippet quotes the passage that actually matched. (It used to be discarded, leaving every
        semantic result showing the first 160 characters of the document, unrelated to the query.)

        Vectors from another embed model live in a separate block and are never scored against this
        query — a cross-model cosine is meaningless.
        """
        assert query_vector, "query vector must be non-empty"
        assert model, "model required"
        assert 1 <= limit <= _SEARCH_SCAN_LIMIT, "limit out of range"
        ranked = self.index.semantic(query_vector, model, limit, _SEMANTIC_MIN_SCORE)
        hits = [self._hit(doc_id, score, chunk_idx, []) for doc_id, score, chunk_idx in ranked]
        return [h for h in hits if h is not None]

    @staticmethod
    def _embed_aad(doc_id: str, chunk_idx: int, dim: int, model: str) -> bytes:
        """AAD for an embedding chunk — domain-separated from the document body AAD.

        Binds the plaintext ``chunk_idx``, ``dim`` and ``model`` columns so tampering
        with them fails GCM authentication (they are otherwise unauthenticated metadata).
        """
        assert doc_id, "doc id required"
        assert chunk_idx >= 0, "chunk idx must be non-negative"
        assert 1 <= dim <= _MAX_EMBED_DIM, "dim out of range"
        assert model, "model required"
        return b"embedding:" + f"{doc_id}|{chunk_idx}|{dim}|{model}".encode("utf-8")

    def _open_embedding(
        self, doc_id: str, chunk_idx: int, nonce: bytes, ciphertext: bytes, dim: int, model: str
    ) -> list[float]:
        """Decrypt + unpack a stored embedding chunk vector for ``doc_id``."""
        assert doc_id, "doc id required"
        assert 1 <= dim <= _MAX_EMBED_DIM, "stored dim out of range"
        plaintext = self._aes.decrypt(nonce, ciphertext, self._embed_aad(doc_id, chunk_idx, dim, model))
        assert len(plaintext) == dim * 4, "embedding blob length mismatch"
        return list(struct.unpack(f"<{dim}f", plaintext))

    def _seal(self, doc_id: str, title: str, content: str) -> tuple[bytes, bytes]:
        """Encrypt {title, content} bound to doc_id; return (nonce, ciphertext)."""
        assert doc_id, "doc id required"
        assert title, "title required"
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps({"title": title, "content": content}).encode("utf-8")
        return nonce, self._aes.encrypt(nonce, plaintext, doc_id.encode("utf-8"))

    def _open(self, doc_id: str, nonce: bytes, ciphertext: bytes) -> dict:
        """Decrypt a stored document body for ``doc_id``."""
        assert doc_id, "doc id required"
        assert len(nonce) == _NONCE_BYTES, "nonce must be 12 bytes"
        plaintext = self._aes.decrypt(nonce, ciphertext, doc_id.encode("utf-8"))
        body = json.loads(plaintext.decode("utf-8"))
        assert "title" in body and "content" in body, "document body malformed"
        return body

    @staticmethod
    def _snippet(content: str, terms: list[str]) -> str:
        """Return a short snippet around the first matching term."""
        assert content is not None, "content required"
        lowered = content.lower()
        positions = [lowered.find(t) for t in terms if t in lowered]
        if not positions:
            return content[:_SNIPPET_CHARS]
        start = max(0, min(positions) - 40)
        return content[start : start + _SNIPPET_CHARS]
