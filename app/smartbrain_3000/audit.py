"""Append-only encrypted audit log — the system-of-record for tool attempts.

Every tool attempt + approval decision is recorded. Hybrid storage mirroring
the planner: PLAINTEXT metadata columns (ts, actor, tool, tier, decision, ok,
conversation_id) so the Activity view filters without decrypting; the ENCRYPTED
body holds {args_summary, result_summary, error} (AAD ``audit:`` + id).

Append-only by API surface: this class exposes only ``append`` + a bounded
``list`` — no update/delete. That is NOT structural tamper-resistance (a holder
of the DB connection or file could still delete rows); a hash-chain is out of
MVP scope and noted as a known limitation.
"""

from __future__ import annotations

import json
import os
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import MASTER_KEY_BYTES

_NONCE_BYTES = 12
_LIST_LIMIT = 500  # max audit rows returned (verifiable bound)
_DECISIONS = ("proposed", "auto", "approved", "denied", "executed", "errored")
_ACTORS = ("assistant", "user")


class AuditLog:
    """AES-256-GCM append-only audit store over DuckDB's ``audit_log`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    def append(
        self,
        actor: str,
        tool_name: str,
        tier: str,
        decision: str,
        ok: bool,
        *,
        conversation_id: str | None = None,
        args_summary: str = "",
        result_summary: str = "",
        error: str = "",
    ) -> str:
        """Record one tool attempt/decision; return its id."""
        assert actor in _ACTORS, "unknown actor"
        assert decision in _DECISIONS, "unknown decision"
        aid = str(uuid.uuid4())
        nonce = os.urandom(_NONCE_BYTES)
        body = {"args_summary": args_summary, "result_summary": result_summary, "error": error}
        ciphertext = self._aes.encrypt(nonce, json.dumps(body).encode("utf-8"), b"audit:" + aid.encode("utf-8"))
        self._conn.execute(
            "INSERT INTO audit_log (id, actor, tool_name, tier, decision, ok, conversation_id, nonce, ciphertext) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            [aid, actor, tool_name, tier, decision, ok, conversation_id, nonce, ciphertext],
        )
        return aid

    def list(self, limit: int = 100) -> list[dict]:
        """Return audit entries, newest first (decrypts bodies in memory)."""
        assert 1 <= limit <= _LIST_LIMIT, "limit out of range"
        rows = self._conn.execute(
            "SELECT id, ts, actor, tool_name, tier, decision, ok, conversation_id, nonce, ciphertext "
            "FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?;",
            [limit],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for row in rows:  # bounded by limit <= _LIST_LIMIT
            body = self._open(str(row[0]), bytes(row[8]), bytes(row[9]))
            out.append(
                {
                    "id": str(row[0]),
                    "ts": str(row[1]),
                    "actor": str(row[2]),
                    "tool": str(row[3]),
                    "tier": str(row[4]),
                    "decision": str(row[5]),
                    "ok": bool(row[6]),
                    "conversation_id": None if row[7] is None else str(row[7]),
                    "args_summary": body["args_summary"],
                    "result_summary": body["result_summary"],
                    "error": body["error"],
                }
            )
        return out

    def _open(self, aid: str, nonce: bytes, ciphertext: bytes) -> dict:
        """Decrypt a stored audit body bound to ``audit:`` + id."""
        assert aid, "audit id required"
        assert len(nonce) == _NONCE_BYTES, "nonce must be 12 bytes"
        plaintext = self._aes.decrypt(nonce, ciphertext, b"audit:" + aid.encode("utf-8"))
        body = json.loads(plaintext.decode("utf-8"))
        assert isinstance(body, dict), "decrypted body must be a dict"
        return body
