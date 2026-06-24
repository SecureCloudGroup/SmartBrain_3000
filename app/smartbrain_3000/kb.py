"""Local knowledge base: documents encrypted at rest + lexical search.

Each document's ``{title, content}`` is encrypted with AES-256-GCM under the
master key (auth tag bound to the document id) and stored in DuckDB. Search
decrypts in memory and scores by query-term frequency — fine for a
personal-scale KB; a semantic index comes next.
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

from .secrets import MASTER_KEY_BYTES

log = logging.getLogger(__name__)

_NONCE_BYTES = 12
# Three independent scan ceilings for a personal-scale KB (the lead documents the
# linear-cost ceiling separately): lexical scans whole docs, semantic scans rows
# in the per-chunk embeddings table (so it needs ``_MAX_CHUNKS`` more headroom),
# and reindex enumerates pending-doc ids only.
_SEARCH_SCAN_LIMIT = 500  # cap on a search's RESULT count (both lexical and semantic)
_LEXICAL_SCAN_LIMIT = 500  # max docs scanned per lexical search (verifiable bound)
_EMBED_SCAN_LIMIT = _SEARCH_SCAN_LIMIT * 64  # = _SEARCH_SCAN_LIMIT * _MAX_CHUNKS; rows/semantic search
_REINDEX_SCAN_LIMIT = 50_000  # max docs surfaced by docs_needing_embedding (lets reindex converge)
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


class KnowledgeBase:
    """AES-256-GCM document store over DuckDB's ``documents`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """The underlying connection (lets ingest read the routed embedding model)."""
        return self._conn

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

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Lexical search: score docs by query-term frequency; return top hits."""
        assert query, "query must be non-empty"
        assert 1 <= limit <= _SEARCH_SCAN_LIMIT, "limit out of range"
        terms = [t for t in query.lower().split() if t]
        assert terms, "query must contain at least one term"
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext FROM documents ORDER BY created_at DESC "
            f"LIMIT {_LEXICAL_SCAN_LIMIT};"
        ).fetchall()
        scored: list[dict] = []
        for row in rows:  # bounded by _LEXICAL_SCAN_LIMIT
            body = self._open(str(row[0]), bytes(row[1]), bytes(row[2]))
            haystack = f"{body['title']}\n{body['content']}".lower()
            score = sum(haystack.count(term) for term in terms)
            if score > 0:
                scored.append(
                    {
                        "id": str(row[0]),
                        "title": body["title"],
                        "score": score,
                        "snippet": self._snippet(body["content"], terms),
                    }
                )
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:limit]

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

        Skips rows whose stored model/dim differ from the active query (a space
        mismatch would corrupt cosine) and orphan vectors (document deleted).
        """
        assert query_vector, "query vector must be non-empty"
        assert model, "model required"
        assert 1 <= limit <= _SEARCH_SCAN_LIMIT, "limit out of range"
        qdim = len(query_vector)
        rows = self._conn.execute(
            "SELECT doc_id, chunk_idx, nonce, ciphertext, dim, model FROM embeddings "
            f"ORDER BY created_at DESC LIMIT {_EMBED_SCAN_LIMIT};"
        ).fetchall()
        # A doc scores as its best-matching chunk (max cosine over its chunks).
        best: dict[str, float] = {}
        for row in rows:  # bounded by _EMBED_SCAN_LIMIT
            if int(row[4]) != qdim or str(row[5]) != model:
                continue  # stale model / dim mismatch — skip, never score
            try:
                vector = self._open_embedding(str(row[0]), int(row[1]), bytes(row[2]), bytes(row[3]), qdim, model)
            except Exception as exc:  # tampered/corrupt row must not sink the search
                log.warning("skipping unreadable embedding for %s: %s", row[0], exc)
                continue
            score = self._cosine(query_vector, vector)
            if score > best.get(str(row[0]), _SEMANTIC_MIN_SCORE):
                best[str(row[0])] = score
        scored: list[dict] = []
        for doc_id, score in best.items():  # distinct docs, bounded by scan limit
            doc = self.get(doc_id)
            if doc is None:
                continue  # orphan vector (document deleted) — skip
            snippet = self._snippet(doc["content"], [])
            scored.append({"id": doc_id, "title": doc["title"], "score": score, "snippet": snippet})
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:limit]

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

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        """Cosine similarity in [-1,1]; 0.0 for zero/degenerate/non-finite input."""
        assert len(a) == len(b), "vectors must be equal length"
        assert 1 <= len(a) <= _MAX_EMBED_DIM, "vector dim out of range"
        dot = na = nb = 0.0
        for i in range(len(a)):  # bounded by _MAX_EMBED_DIM
            dot += a[i] * b[i]
            na += a[i] * a[i]
            nb += b[i] * b[i]
        if na == 0.0 or nb == 0.0:
            return 0.0
        result = dot / (math.sqrt(na) * math.sqrt(nb))
        return result if math.isfinite(result) else 0.0

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
