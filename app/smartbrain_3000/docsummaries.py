"""The background document-summary tree (B1).

A 1000-page book (~600k+ tokens) can never be summarized "quickly" inside a chat turn
on a 32k-context local model — so the work moves to INGEST TIME: a scheduler pass
summarizes each document chunk by chunk in the background, then reduces the chunk
summaries into one whole-document summary. Chat-time ``summarize_document`` becomes a
cached lookup; while the tree is still building it answers from the covered chunks,
honestly flagged.

Chunking is DETERMINISTIC (a fixed ``CHUNK_CHARS``, never model-sized) so the tree's
coverage math survives model switches. Rows seal like every other content column
(AES-GCM, AAD-bound); ``content_len`` is plaintext staleness metadata in the cadence-
field tradition — a document whose length changed invalidates its whole tree, and a
deleted document's rows are swept by the same pass (no hooks inside kb.py).
"""

from __future__ import annotations

import json
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import MASTER_KEY_BYTES

log = logging.getLogger(__name__)

_NONCE_BYTES = 12
CHUNK_CHARS = 24000  # fixed map-chunk size (~6k tokens): deterministic tree geometry
DOC_IDX = -1  # idx of the reduced whole-document summary row
_MAX_TRACKED_DOCS = 10000  # bound on one worker scan (P10 #2)


def expected_chunks(content_len: int) -> int:
    """How many map chunks a document of ``content_len`` chars needs (0 for empty)."""
    assert content_len >= 0, "length cannot be negative"
    return max(0, -(-content_len // CHUNK_CHARS)) if content_len else 0


class SummaryStore:
    """AES-256-GCM summary rows over DuckDB's ``doc_summaries`` table."""

    def __init__(self, conn, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    # -- sealing (memory.py idiom: AAD binds a row to its identity) ------------
    def _seal(self, aad: bytes, body: dict) -> tuple[bytes, bytes]:
        nonce = os.urandom(_NONCE_BYTES)
        return nonce, self._aes.encrypt(nonce, json.dumps(body).encode("utf-8"), aad)

    def _open(self, aad: bytes, nonce: bytes, ciphertext: bytes) -> dict:
        body = json.loads(self._aes.decrypt(nonce, ciphertext, aad).decode("utf-8"))
        assert isinstance(body, dict), "decrypted body must be a dict"
        return body

    @staticmethod
    def _aad(doc_id: str, idx: int) -> bytes:
        return f"docsummary:{doc_id}:{idx}".encode("utf-8")

    # -- writes ---------------------------------------------------------------
    def put(self, doc_id: str, idx: int, text: str, content_len: int, model: str) -> None:
        """Insert/replace one summary row (chunk idx >= 0, or DOC_IDX for the doc row)."""
        assert doc_id and text is not None, "doc_id + text required"
        assert idx >= DOC_IDX, "idx must be DOC_IDX or a chunk index"
        nonce, ciphertext = self._seal(self._aad(doc_id, idx), {"text": text})
        self._conn.execute(
            "INSERT OR REPLACE INTO doc_summaries (doc_id, idx, nonce, ciphertext, content_len, model) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            [doc_id, idx, nonce, ciphertext, content_len, model],
        )

    def clear(self, doc_id: str) -> None:
        self._conn.execute("DELETE FROM doc_summaries WHERE doc_id = ?;", [doc_id])

    # -- reads ----------------------------------------------------------------
    def chunk_texts(self, doc_id: str, content_len: int) -> list[str]:
        """Decrypted chunk summaries in order, ONLY those matching the current length."""
        rows = self._conn.execute(
            "SELECT idx, nonce, ciphertext FROM doc_summaries "
            "WHERE doc_id = ? AND idx >= 0 AND content_len = ? ORDER BY idx ASC;",
            [doc_id, content_len],
        ).fetchall()
        return [self._open(self._aad(doc_id, r[0]), r[1], r[2])["text"] for r in rows]

    def doc_summary(self, doc_id: str, content_len: int) -> str | None:
        """The reduced whole-document summary, or None if absent/stale."""
        row = self._conn.execute(
            "SELECT nonce, ciphertext FROM doc_summaries "
            "WHERE doc_id = ? AND idx = ? AND content_len = ?;",
            [doc_id, DOC_IDX, content_len],
        ).fetchone()
        if row is None:
            return None
        return self._open(self._aad(doc_id, DOC_IDX), row[0], row[1])["text"]

    def done_chunks(self, doc_id: str, content_len: int) -> set[int]:
        rows = self._conn.execute(
            "SELECT idx FROM doc_summaries WHERE doc_id = ? AND idx >= 0 AND content_len = ?;",
            [doc_id, content_len],
        ).fetchall()
        return {r[0] for r in rows}

    def progress(self, doc_id: str, content_len: int) -> dict:
        """{expected, done, complete} for one document at its current length."""
        expected = expected_chunks(content_len)
        done = len(self.done_chunks(doc_id, content_len))
        return {"expected": expected, "done": min(done, expected),
                "complete": expected > 0 and done >= expected
                and self.doc_summary(doc_id, content_len) is not None}

    # -- worker support -------------------------------------------------------
    def sweep_stale(self, live_doc_ids: set[str]) -> int:
        """Drop rows for deleted documents; return how many rows went. Bounded scan."""
        rows = self._conn.execute(
            "SELECT DISTINCT doc_id FROM doc_summaries LIMIT ?;", [_MAX_TRACKED_DOCS]
        ).fetchall()
        gone = [r[0] for r in rows if r[0] not in live_doc_ids]
        for doc_id in gone:  # bounded by the scan limit
            self.clear(doc_id)
        return len(gone)

    def next_work(self, docs: list[dict]) -> dict | None:
        """The next unit of work across ``docs`` (each {id, content}), or None when idle.

        Returns {"doc": doc, "kind": "chunk", "idx": n} for a missing chunk summary, or
        {"doc": doc, "kind": "reduce"} when every chunk is summarized but the doc row
        isn't. Stale rows (length changed) are cleared here so the tree restarts clean.
        Oldest-listed documents first; bounded by the caller's list.
        """
        for doc in docs:  # bounded by the caller (kb list limit)
            content = doc.get("content") or ""
            if not content:
                continue
            length = len(content)
            have_any = self._conn.execute(
                "SELECT count(*) FROM doc_summaries WHERE doc_id = ?;", [doc["id"]]
            ).fetchone()[0]
            done = self.done_chunks(doc["id"], length)
            if have_any and not done and self.doc_summary(doc["id"], length) is None:
                # rows exist but none match the current length -> the doc changed
                self.clear(doc["id"])
            expected = expected_chunks(length)
            for idx in range(expected):  # bounded: ceil(len/CHUNK_CHARS)
                if idx not in done:
                    return {"doc": doc, "kind": "chunk", "idx": idx}
            if expected and self.doc_summary(doc["id"], length) is None:
                return {"doc": doc, "kind": "reduce"}
        return None
