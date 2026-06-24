"""Pending-approval state machine for REVIEWED / IRREVERSIBLE tool calls (H4b).

A dangerous tool call NEVER runs inline: it is parked as an encrypted
``pending_actions`` row and only runs after the user approves it. State:
``pending -> approved -> executed`` or ``pending -> denied``. The single-use
execution claim (``approved -> executed``) plus the per-transition lock make it
impossible for an IRREVERSIBLE action to run twice or run unapproved.

The full args + the unlock-session id live in the encrypted body (AAD
``pending:`` + id); tool/tier/status are plaintext for the tiles. A row whose
stored session id differs from the current unlock session is rejected — so a
lock+unlock invalidates everything still pending.
"""

from __future__ import annotations

import json
import os
import threading
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import MASTER_KEY_BYTES

_NONCE_BYTES = 12
_LIST_LIMIT = 200  # max pending rows surfaced (verifiable bound)
_TTL_SECONDS = 3600  # a pending action expires after an hour
# Serializes the read-then-write of a status transition on the shared DuckDB
# connection so two concurrent approves/claims cannot both win. Module-level (not
# per-ApprovalStore) on purpose: every store wraps the SAME process-wide DuckDB
# connection, so a per-instance lock would not actually serialize the underlying
# read-then-write and two stores could both claim the same pending action.
_CAS_LOCK = threading.Lock()


class ApprovalStore:
    """AES-256-GCM pending-action store with atomic, single-use transitions."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes, session_id: str) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        assert session_id, "session id required"
        self._conn = conn
        self._aes = AESGCM(master_key)
        self._session = session_id

    def create_pending(
        self,
        tool_name: str,
        tier: str,
        args: dict,
        *,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
        turn_state: dict | None = None,
    ) -> str:
        """Park a tool call awaiting approval; return its id.

        ``turn_state`` (the agent's working messages/model/step) is stored so the
        turn can resume after approval (H4c).
        """
        assert tool_name, "tool name required"
        assert isinstance(args, dict), "args must be a dict"
        pid = str(uuid.uuid4())
        nonce = os.urandom(_NONCE_BYTES)
        body = {"args": args, "session": self._session, "turn_state": turn_state}
        ciphertext = self._aes.encrypt(nonce, json.dumps(body).encode("utf-8"), b"pending:" + pid.encode("utf-8"))
        self._conn.execute(
            "INSERT INTO pending_actions (id, turn_id, conversation_id, tool_call_id, tool_name, tier, status, nonce, ciphertext) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?);",
            [pid, turn_id, conversation_id, tool_call_id, tool_name, tier, nonce, ciphertext],
        )
        return pid

    def list_pending(self) -> list[dict]:
        """Return pending rows for THIS session (args preview is the caller's job)."""
        rows = self._conn.execute(
            "SELECT id, tool_name, tier, created_at, nonce, ciphertext FROM pending_actions "
            "WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?;",
            [_LIST_LIMIT],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for row in rows:  # bounded by _LIST_LIMIT
            body = self._open(str(row[0]), bytes(row[4]), bytes(row[5]))
            if body["session"] != self._session:
                continue  # a prior unlock session — invisible now
            out.append(
                {"id": str(row[0]), "tool": str(row[1]), "tier": str(row[2]), "created_at": str(row[3]), "args": body["args"]}
            )
        return out

    def get(self, pid: str) -> dict | None:
        """Return a pending row (decrypted args + status), or None if absent/cross-session."""
        assert pid, "pending id required"
        row = self._conn.execute(
            "SELECT tool_name, tier, status, nonce, ciphertext, "
            "date_diff('second', created_at, now()) FROM pending_actions WHERE id = ?;",
            [pid],
        ).fetchone()
        if row is None:
            return None
        body = self._open(pid, bytes(row[3]), bytes(row[4]))
        if body["session"] != self._session:
            return None  # belongs to a prior unlock session
        return {
            "id": pid,
            "tool_name": str(row[0]),
            "tier": str(row[1]),
            "status": str(row[2]),
            "args": body["args"],
            "result": body.get("result"),
            "turn_state": body.get("turn_state"),
            "expired": int(row[5]) > _TTL_SECONDS,
        }

    def list_for_turn(self, turn_id: str) -> list[dict]:
        """Return all rows for a turn (this session) — for agent resume (H4c)."""
        assert turn_id, "turn id required"
        rows = self._conn.execute(
            "SELECT id, tool_call_id, tool_name, tier, status, nonce, ciphertext FROM pending_actions "
            "WHERE turn_id = ? ORDER BY created_at ASC LIMIT ?;",
            [turn_id, _LIST_LIMIT],
        ).fetchall()
        out: list[dict] = []
        for row in rows:  # bounded by _LIST_LIMIT
            body = self._open(str(row[0]), bytes(row[5]), bytes(row[6]))
            if body["session"] != self._session:
                continue
            out.append(
                {
                    "id": str(row[0]),
                    "tool_call_id": row[1],
                    "tool_name": str(row[2]),
                    "tier": str(row[3]),
                    "status": str(row[4]),
                    "args": body["args"],
                    "result": body.get("result"),
                    "turn_state": body.get("turn_state"),
                }
            )
        return out

    def store_result(self, pid: str, result: dict) -> None:
        """Persist an executed action's result into its encrypted row (for resume).

        No-op unless the row is in ``executed`` state and a result is not yet
        stored, so a double-call cannot overwrite the first result.
        """
        assert pid, "pending id required"
        assert isinstance(result, dict), "result must be a dict"
        row = self._conn.execute(
            "SELECT status, nonce, ciphertext FROM pending_actions WHERE id = ?;", [pid]
        ).fetchone()
        if row is None or str(row[0]) != "executed":
            return
        body = self._open(pid, bytes(row[1]), bytes(row[2]))
        if body.get("result") is not None:
            return  # already stored — second call is a no-op (single-write)
        body["result"] = result
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aes.encrypt(nonce, json.dumps(body).encode("utf-8"), b"pending:" + pid.encode("utf-8"))
        self._conn.execute("UPDATE pending_actions SET nonce = ?, ciphertext = ? WHERE id = ?;", [nonce, ciphertext, pid])

    def _cas(self, pid: str, expected: str, new: str) -> bool:
        """Atomically move pid from ``expected`` to ``new`` (lock-serialized)."""
        assert pid and expected and new, "cas args required"
        with _CAS_LOCK:
            row = self._conn.execute(
                "SELECT status, date_diff('second', created_at, now()) FROM pending_actions WHERE id = ?;",
                [pid],
            ).fetchone()
            if row is None or str(row[0]) != expected or int(row[1]) > _TTL_SECONDS:
                return False
            self._conn.execute(
                "UPDATE pending_actions SET status = ?, resolved_at = now() WHERE id = ?;", [new, pid]
            )
            return True

    def approve(self, pid: str) -> bool:
        """CAS pending -> approved (one approval wins)."""
        return self._cas(pid, "pending", "approved")

    def deny(self, pid: str) -> bool:
        """CAS pending -> denied."""
        return self._cas(pid, "pending", "denied")

    def claim(self, pid: str) -> bool:
        """CAS approved -> executed: the single-use execution claim."""
        return self._cas(pid, "approved", "executed")

    def _open(self, pid: str, nonce: bytes, ciphertext: bytes) -> dict:
        """Decrypt a pending body bound to ``pending:`` + id."""
        assert pid, "pending id required"
        assert len(nonce) == _NONCE_BYTES, "nonce must be 12 bytes"
        plaintext = self._aes.decrypt(nonce, ciphertext, b"pending:" + pid.encode("utf-8"))
        body = json.loads(plaintext.decode("utf-8"))
        assert "args" in body and "session" in body, "pending body malformed"
        return body
