"""Encrypted planner: tasks with due date/time, priority, tags, recurrence + status.

A task's ``{title, notes, tags}`` is AES-256-GCM encrypted at rest (AAD ``task:`` +
id, domain-separated). ``status``, ``due_date``, ``due_time``, ``priority`` and ``recur``
stay plaintext so the UI can group/sort without decrypting — low-sensitivity metadata,
like the embeddings dim/model columns. Today/Week grouping itself lives in the client
(timezone-aware) over the plaintext due_date.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import MASTER_KEY_BYTES

_NONCE_BYTES = 12
_LIST_LIMIT = 1000  # max tasks loaded (verifiable bound)
_STATUSES = ("open", "done")
_PRIORITIES = ("low", "medium", "high")
_RECURS = ("none", "daily", "weekly")
_MAX_TAGS = 20  # bound tags per task


def _clean_priority(value: str | None) -> str:
    """Clamp priority to a known value (default medium)."""
    return value if value in _PRIORITIES else "medium"


def _clean_recur(value: str | None) -> str:
    """Clamp recurrence to a known value (default none)."""
    return value if value in _RECURS else "none"


def _clean_tags(tags: list[str] | None) -> list[str]:
    """Trim, drop blanks, de-dupe, and bound the tag list."""
    if not tags:
        return []
    seen: list[str] = []
    for t in tags[:_MAX_TAGS]:  # bounded
        s = str(t).strip()
        if s and s not in seen:
            seen.append(s)
    return seen


class Planner:
    """AES-256-GCM task store over DuckDB's ``tasks`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    def add_task(
        self,
        title: str,
        notes: str = "",
        due_date: str | None = None,
        *,
        due_time: str | None = None,
        priority: str = "medium",
        recur: str = "none",
        tags: list[str] | None = None,
    ) -> str:
        """Create a task (open); return its new id."""
        assert title, "title must be non-empty"
        assert notes is not None, "notes must not be None"
        tid = str(uuid.uuid4())
        nonce, ciphertext = self._seal(tid, {"title": title, "notes": notes, "tags": _clean_tags(tags)})
        self._conn.execute(
            "INSERT INTO tasks (id, nonce, ciphertext, status, due_date, priority, due_time, recur) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?);",
            [tid, nonce, ciphertext, due_date, _clean_priority(priority), due_time, _clean_recur(recur)],
        )
        return tid

    def _row_to_task(self, tid: str, nonce, ciphertext, status, due_date, priority, due_time, recur, created, updated) -> dict:
        """Assemble a task dict from a row (decrypts the body)."""
        body = self._open(tid, bytes(nonce), bytes(ciphertext))
        return {
            "id": tid,
            "title": body["title"],
            "notes": body["notes"],
            "tags": body.get("tags", []),
            "status": str(status),
            "due_date": None if due_date is None else str(due_date),
            "due_time": None if due_time is None else str(due_time),
            "priority": _clean_priority(str(priority) if priority is not None else None),
            "recur": _clean_recur(str(recur) if recur is not None else None),
            "created_at": str(created),
            "updated_at": str(updated),
        }

    def list_tasks(self) -> list[dict]:
        """Return all tasks: open first, by due date (nulls last), bounded."""
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext, status, due_date, priority, due_time, recur, created_at, updated_at "
            "FROM tasks ORDER BY (status = 'done'), due_date ASC NULLS LAST, created_at DESC LIMIT ?;",
            [_LIST_LIMIT],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        return [self._row_to_task(str(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]) for r in rows]

    def get_task(self, tid: str) -> dict | None:
        """Return one task, or None if absent."""
        assert tid, "task id required"
        row = self._conn.execute(
            "SELECT nonce, ciphertext, status, due_date, priority, due_time, recur, created_at, updated_at "
            "FROM tasks WHERE id = ?;",
            [tid],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_task(tid, row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8])

    def update_task(
        self,
        tid: str,
        title: str,
        notes: str,
        due_date: str | None,
        *,
        due_time: str | None = None,
        priority: str = "medium",
        recur: str = "none",
        tags: list[str] | None = None,
    ) -> None:
        """Replace a task's editable fields."""
        assert tid, "task id required"
        assert title, "title must be non-empty"
        nonce, ciphertext = self._seal(tid, {"title": title, "notes": notes, "tags": _clean_tags(tags)})
        self._conn.execute(
            "UPDATE tasks SET nonce = ?, ciphertext = ?, due_date = ?, due_time = ?, "
            "priority = ?, recur = ?, updated_at = now() WHERE id = ?;",
            [nonce, ciphertext, due_date, due_time, _clean_priority(priority), _clean_recur(recur), tid],
        )

    def set_status(self, tid: str, status: str) -> None:
        """Mark a task open or done. Completing a recurring task with a due date rolls it
        forward to the next occurrence (stays open) instead of closing it."""
        assert tid, "task id required"
        assert status in _STATUSES, "status must be open/done"
        if status == "done":
            row = self._conn.execute("SELECT recur, due_date FROM tasks WHERE id = ?;", [tid]).fetchone()
            if row is not None and str(row[0]) in ("daily", "weekly") and row[1] is not None:
                self._conn.execute(
                    "UPDATE tasks SET due_date = ?, updated_at = now() WHERE id = ?;",
                    [self._advance(str(row[1]), str(row[0])), tid],
                )
                return  # recurring: roll forward, keep open
        self._conn.execute("UPDATE tasks SET status = ?, updated_at = now() WHERE id = ?;", [status, tid])

    def delete_task(self, tid: str) -> None:
        """Remove a task (no error if absent)."""
        assert tid, "task id required"
        self._conn.execute("DELETE FROM tasks WHERE id = ?;", [tid])

    @staticmethod
    def _advance(due_date: str, recur: str) -> str:
        """Next due date for a recurring task (YYYY-MM-DD).

        Rolls forward from ``max(parsed_due_date, today)`` so completing a task
        that is N days overdue jumps the due date to today-or-later (not just
        +1/+7 from the stale due_date — which would leave it overdue forever).
        """
        assert recur in ("daily", "weekly"), "recur must be daily/weekly"
        assert isinstance(due_date, str) and len(due_date) >= 10, "due_date must be YYYY-MM-DD"
        parsed = datetime.strptime(due_date[:10], "%Y-%m-%d").date()
        base = parsed if parsed >= date.today() else date.today()
        step = timedelta(days=1 if recur == "daily" else 7)
        nxt = base + step
        assert nxt >= date.today(), "next occurrence must be >= today"
        return nxt.strftime("%Y-%m-%d")

    def _seal(self, tid: str, body: dict) -> tuple[bytes, bytes]:
        """Encrypt {title, notes, tags} bound to ``task:`` + id; return (nonce, ciphertext)."""
        assert tid, "task id required"
        assert isinstance(body, dict), "body must be a dict"
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps(body).encode("utf-8")
        return nonce, self._aes.encrypt(nonce, plaintext, b"task:" + tid.encode("utf-8"))

    def _open(self, tid: str, nonce: bytes, ciphertext: bytes) -> dict:
        """Decrypt a stored task body for ``tid``."""
        assert tid, "task id required"
        assert len(nonce) == _NONCE_BYTES, "nonce must be 12 bytes"
        plaintext = self._aes.decrypt(nonce, ciphertext, b"task:" + tid.encode("utf-8"))
        body = json.loads(plaintext.decode("utf-8"))
        assert "title" in body and "notes" in body, "task body malformed"
        return body
