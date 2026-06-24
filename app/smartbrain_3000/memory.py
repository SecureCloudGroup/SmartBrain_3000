"""Encrypted memory + identity: facts the assistant should remember, plus a
profile (assistant name / user name / custom instructions).

Both are AES-256-GCM encrypted at rest under the master key, domain-separated
(``memory:`` / ``profile:``) from documents/embeddings/history. ``system_prompt``
composes them into a single system message the chat route prepends server-side.
"""

from __future__ import annotations

import json
import os
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import MASTER_KEY_BYTES

_NONCE_BYTES = 12
_LIST_LIMIT = 500  # max facts loaded / injected (verifiable bound)
_PROFILE_AAD = b"profile:1"
_DEFAULT_ASSISTANT = "SmartBrain"


class MemoryStore:
    """AES-256-GCM memory facts + a singleton profile over DuckDB."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    def add_memory(self, text: str) -> str:
        """Store an encrypted fact; return its new id."""
        assert text, "memory text must be non-empty"
        mid = str(uuid.uuid4())
        nonce, ciphertext = self._seal(b"memory:" + mid.encode("utf-8"), {"text": text})
        self._conn.execute(
            "INSERT INTO memories (id, nonce, ciphertext) VALUES (?, ?, ?);",
            [mid, nonce, ciphertext],
        )
        return mid

    def list_memories(self) -> list[dict]:
        """Return id/text/timestamps for all facts (newest first, bounded)."""
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext, created_at, updated_at FROM memories "
            "ORDER BY created_at DESC LIMIT ?;",
            [_LIST_LIMIT],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for row in rows:  # bounded by _LIST_LIMIT
            body = self._open(b"memory:" + str(row[0]).encode("utf-8"), bytes(row[1]), bytes(row[2]))
            out.append(
                {
                    "id": str(row[0]),
                    "text": body["text"],
                    "created_at": str(row[3]),
                    "updated_at": str(row[4]),
                }
            )
        return out

    def delete_memory(self, mid: str) -> None:
        """Remove a fact (no error if absent)."""
        assert mid, "memory id required"
        self._conn.execute("DELETE FROM memories WHERE id = ?;", [mid])

    def get_profile(self) -> dict:
        """Return {assistant_name, user_name, instructions}; defaults if unset."""
        row = self._conn.execute("SELECT nonce, ciphertext FROM profile WHERE id = 1;").fetchone()
        if row is None:
            return {"assistant_name": "", "user_name": "", "instructions": ""}
        body = self._open(_PROFILE_AAD, bytes(row[0]), bytes(row[1]))
        return {
            "assistant_name": body.get("assistant_name", ""),
            "user_name": body.get("user_name", ""),
            "instructions": body.get("instructions", ""),
        }

    def set_profile(self, assistant_name: str, user_name: str, instructions: str) -> None:
        """Store the singleton profile (encrypted)."""
        assert assistant_name is not None, "assistant_name must not be None"
        body = {
            "assistant_name": assistant_name,
            "user_name": user_name,
            "instructions": instructions,
        }
        nonce, ciphertext = self._seal(_PROFILE_AAD, body)
        self._conn.execute(
            "INSERT INTO profile (id, nonce, ciphertext) VALUES (1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET nonce = excluded.nonce, "
            "ciphertext = excluded.ciphertext, updated_at = now();",
            [nonce, ciphertext],
        )

    def system_prompt(self) -> str | None:
        """Compose a system message from the profile + facts, or None if empty."""
        profile = self.get_profile()
        facts = [m["text"] for m in self.list_memories()]
        configured = profile["user_name"] or profile["instructions"] or profile["assistant_name"]
        if not configured and not facts:
            return None
        name = profile["assistant_name"] or _DEFAULT_ASSISTANT
        intro = f"You are {name}, a personal assistant"
        if profile["user_name"]:
            intro += f" for {profile['user_name']}"
        parts = [intro + "."]
        if profile["instructions"]:
            parts.append(profile["instructions"])
        if facts:
            parts.append("Known facts about the user:\n" + "\n".join(f"- {f}" for f in facts))
        return "\n\n".join(parts)

    def _seal(self, aad: bytes, body: dict) -> tuple[bytes, bytes]:
        """Encrypt ``body`` bound to ``aad``; return (nonce, ciphertext)."""
        assert aad, "aad required"
        assert isinstance(body, dict), "body must be a dict"
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps(body).encode("utf-8")
        return nonce, self._aes.encrypt(nonce, plaintext, aad)

    def _open(self, aad: bytes, nonce: bytes, ciphertext: bytes) -> dict:
        """Decrypt a stored row body bound to ``aad``."""
        assert aad, "aad required"
        assert len(nonce) == _NONCE_BYTES, "nonce must be 12 bytes"
        plaintext = self._aes.decrypt(nonce, ciphertext, aad)
        body = json.loads(plaintext.decode("utf-8"))
        assert isinstance(body, dict), "decrypted body must be a dict"
        return body
