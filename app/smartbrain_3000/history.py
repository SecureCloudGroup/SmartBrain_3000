"""Encrypted chat history: conversations + messages at rest in DuckDB.

A conversation's ``{title}`` and a message's ``{role, content}`` are encrypted
with AES-256-GCM under the master key, with the auth tag bound to the row id and
domain-separated (``conversation:`` / ``message:``) from documents/embeddings.
This mirrors ``KnowledgeBase``: encrypt at rest, decrypt in memory, bounded
scans. ``conversation_id`` and timestamps stay plaintext for querying/ordering.
"""

from __future__ import annotations

import json
import os
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import MASTER_KEY_BYTES

_NONCE_BYTES = 12
_LIST_LIMIT = 200  # max conversations listed (verifiable bound)
_MSG_LIMIT = 2000  # max messages loaded per conversation (verifiable bound)
_DEFAULT_LIST_PAGE = 50    # default page size for paginated conversation listing
_MAX_LIST_PAGE = 200       # hard max page size (mirrors _LIST_LIMIT)
_DEFAULT_MSG_PAGE = 100    # default page size for paginated message listing
_MAX_MSG_PAGE = 500        # hard max page size for messages
_ROLES = ("user", "assistant", "system")


def _clamp_page(limit: int | None, default: int, hard_max: int) -> int:
    """Return a bounded page size: ``None`` -> default; values are clamped to [1, hard_max]."""
    assert default >= 1, "default page size must be positive"
    assert hard_max >= default, "hard max must be >= default"
    if limit is None:
        return default
    assert isinstance(limit, int), "limit must be an int"
    if limit < 1:
        return 1
    if limit > hard_max:
        return hard_max
    return limit


def _split_cursor(cursor: str | None) -> tuple[str, str] | None:
    """Parse a ``"<timestamp>|<id>"`` keyset cursor; return ``None`` if absent."""
    assert cursor is None or isinstance(cursor, str), "cursor must be a string or None"
    if cursor is None or not cursor:
        return None
    sep = cursor.find("|")
    assert sep > 0, "cursor must be '<timestamp>|<id>'"
    return cursor[:sep], cursor[sep + 1 :]


class ChatHistory:
    """AES-256-GCM conversation + message store over DuckDB."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    def create_conversation(self, title: str) -> str:
        """Create a conversation with an encrypted title; return its new id."""
        assert title, "title must be non-empty"
        cid = str(uuid.uuid4())
        nonce, ciphertext = self._seal(b"conversation:", cid, {"title": title})
        self._conn.execute(
            "INSERT INTO conversations (id, nonce, ciphertext) VALUES (?, ?, ?);",
            [cid, nonce, ciphertext],
        )
        return cid

    def list_conversations(self) -> list[dict]:
        """Return id/title/timestamps for conversations, most-recent first."""
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext, created_at, updated_at FROM conversations "
            "ORDER BY updated_at DESC LIMIT ?;",
            [_LIST_LIMIT],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for row in rows:  # bounded by _LIST_LIMIT
            body = self._open(b"conversation:", str(row[0]), bytes(row[1]), bytes(row[2]))
            out.append(
                {
                    "id": str(row[0]),
                    "title": body["title"],
                    "created_at": str(row[3]),
                    "updated_at": str(row[4]),
                }
            )
        return out

    def list_conversations_page(
        self, before: str | None = None, limit: int | None = None
    ) -> dict:
        """Return one keyset-paginated page of conversations (newest first).

        ``before`` is a ``"<updated_at>|<id>"`` cursor returned by a prior call;
        omit it for the first (newest) page. ``limit`` defaults to
        ``_DEFAULT_LIST_PAGE`` and is clamped to ``_MAX_LIST_PAGE``. Returns
        ``{items, next_cursor, has_more}`` where ``next_cursor`` is the cursor
        for the page AFTER this one (``None`` when ``has_more`` is false).
        """
        page = _clamp_page(limit, _DEFAULT_LIST_PAGE, _MAX_LIST_PAGE)
        assert 1 <= page <= _MAX_LIST_PAGE, "page size out of bounds"
        cursor = _split_cursor(before)
        # Fetch one extra row to detect a next page without a second query.
        if cursor is None:
            rows = self._conn.execute(
                "SELECT id, nonce, ciphertext, created_at, updated_at FROM conversations "
                "ORDER BY updated_at DESC, id DESC LIMIT ?;",
                [page + 1],
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, nonce, ciphertext, created_at, updated_at FROM conversations "
                "WHERE (updated_at, id) < (CAST(? AS TIMESTAMP), ?) "
                "ORDER BY updated_at DESC, id DESC LIMIT ?;",
                [cursor[0], cursor[1], page + 1],
            ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        has_more = len(rows) > page
        rows = rows[:page]
        items: list[dict] = []
        for row in rows:  # bounded by page (<= _MAX_LIST_PAGE)
            body = self._open(b"conversation:", str(row[0]), bytes(row[1]), bytes(row[2]))
            items.append(
                {
                    "id": str(row[0]),
                    "title": body["title"],
                    "created_at": str(row[3]),
                    "updated_at": str(row[4]),
                }
            )
        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = f"{last['updated_at']}|{last['id']}"
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    def get_conversation(self, cid: str) -> dict | None:
        """Return a conversation's id/title/timestamps, or None if absent."""
        assert cid, "conversation id required"
        row = self._conn.execute(
            "SELECT nonce, ciphertext, created_at, updated_at FROM conversations WHERE id = ?;",
            [cid],
        ).fetchone()
        if row is None:
            return None
        assert len(row) == 4, "unexpected conversations row shape"
        body = self._open(b"conversation:", cid, bytes(row[0]), bytes(row[1]))
        return {"id": cid, "title": body["title"], "created_at": str(row[2]), "updated_at": str(row[3])}

    def rename_conversation(self, cid: str, title: str) -> None:
        """Replace a conversation's encrypted title."""
        assert cid, "conversation id required"
        assert title, "title must be non-empty"
        nonce, ciphertext = self._seal(b"conversation:", cid, {"title": title})
        self._conn.execute(
            "UPDATE conversations SET nonce = ?, ciphertext = ?, updated_at = now() WHERE id = ?;",
            [nonce, ciphertext, cid],
        )

    def delete_conversation(self, cid: str) -> None:
        """Delete a conversation and its messages (no error if absent)."""
        assert cid, "conversation id required"
        self._conn.execute("DELETE FROM messages WHERE conversation_id = ?;", [cid])
        self._conn.execute("DELETE FROM conversations WHERE id = ?;", [cid])
        assert self.get_conversation(cid) is None, "conversation must be absent after delete"

    def add_message(self, cid: str, role: str, content: str) -> str:
        """Append an encrypted message to a conversation; bump its updated_at."""
        assert cid, "conversation id required"
        assert role in _ROLES, "role must be user/assistant/system"
        assert content is not None, "content must not be None"
        if self.get_conversation(cid) is None:
            raise ValueError("conversation not found")
        mid = str(uuid.uuid4())
        nonce, ciphertext = self._seal(b"message:", mid, {"role": role, "content": content})
        self._conn.execute(
            "INSERT INTO messages (id, conversation_id, nonce, ciphertext) VALUES (?, ?, ?, ?);",
            [mid, cid, nonce, ciphertext],
        )
        self._conn.execute("UPDATE conversations SET updated_at = now() WHERE id = ?;", [cid])
        return mid

    def get_messages(self, cid: str) -> list[dict]:
        """Return a conversation's messages in order (oldest first)."""
        assert cid, "conversation id required"
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext, created_at FROM messages WHERE conversation_id = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?;",
            [cid, _MSG_LIMIT],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for row in rows:  # bounded by _MSG_LIMIT
            body = self._open(b"message:", str(row[0]), bytes(row[1]), bytes(row[2]))
            out.append(
                {
                    "id": str(row[0]),
                    "role": body["role"],
                    "content": body["content"],
                    "created_at": str(row[3]),
                }
            )
        return out

    def get_messages_page(
        self, cid: str, before: str | None = None, limit: int | None = None
    ) -> dict:
        """Return one keyset-paginated page of messages (newest page first).

        With no ``before``, the page is the most-recent ``limit`` messages. With
        ``before`` (a ``"<created_at>|<id>"`` cursor), the page is the next
        older slice — strictly older than the cursor, no overlap. Items inside
        each page are returned oldest-first so the UI renders top-to-bottom;
        ``next_cursor`` points at the oldest item on this page (use it as
        ``before`` to fetch the next older page).
        """
        assert cid, "conversation id required"
        page = _clamp_page(limit, _DEFAULT_MSG_PAGE, _MAX_MSG_PAGE)
        assert 1 <= page <= _MAX_MSG_PAGE, "page size out of bounds"
        cursor = _split_cursor(before)
        if cursor is None:
            rows = self._conn.execute(
                "SELECT id, nonce, ciphertext, created_at FROM messages WHERE conversation_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?;",
                [cid, page + 1],
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, nonce, ciphertext, created_at FROM messages WHERE conversation_id = ? "
                "AND (created_at, id) < (CAST(? AS TIMESTAMP), ?) "
                "ORDER BY created_at DESC, id DESC LIMIT ?;",
                [cid, cursor[0], cursor[1], page + 1],
            ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        has_more = len(rows) > page
        rows = rows[:page]
        items: list[dict] = []
        for row in rows:  # bounded by page (<= _MAX_MSG_PAGE)
            body = self._open(b"message:", str(row[0]), bytes(row[1]), bytes(row[2]))
            items.append(
                {
                    "id": str(row[0]),
                    "role": body["role"],
                    "content": body["content"],
                    "created_at": str(row[3]),
                }
            )
        # Cursor points at the OLDEST item on this page (the next page is older still).
        next_cursor: str | None = None
        if has_more and items:
            oldest = items[-1]
            next_cursor = f"{oldest['created_at']}|{oldest['id']}"
        items.reverse()  # return oldest-first within the page for natural rendering
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    def _seal(self, domain: bytes, row_id: str, body: dict) -> tuple[bytes, bytes]:
        """Encrypt ``body`` bound to ``domain + row_id``; return (nonce, ciphertext)."""
        assert row_id, "row id required"
        assert isinstance(body, dict), "body must be a dict"
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps(body).encode("utf-8")
        return nonce, self._aes.encrypt(nonce, plaintext, domain + row_id.encode("utf-8"))

    def _open(self, domain: bytes, row_id: str, nonce: bytes, ciphertext: bytes) -> dict:
        """Decrypt a stored row body bound to ``domain + row_id``."""
        assert row_id, "row id required"
        assert len(nonce) == _NONCE_BYTES, "nonce must be 12 bytes"
        plaintext = self._aes.decrypt(nonce, ciphertext, domain + row_id.encode("utf-8"))
        body = json.loads(plaintext.decode("utf-8"))
        assert isinstance(body, dict), "decrypted body must be a dict"
        return body
