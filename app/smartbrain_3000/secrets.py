"""Local encrypted secret store for SmartBrain_3000.

Secrets (provider API keys, tokens) are encrypted at rest with AES-256-GCM and
stored in the embedded DuckDB. The caller supplies the 32-byte master key; how
that key is unlocked (passphrase, recovery key) is layered on top of this
module in a later step.

Each row's authentication tag is bound, via AES-GCM associated data (AAD), to
the secret's key name — so a stored ciphertext cannot be silently moved to a
different key.
"""

from __future__ import annotations

import os

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MASTER_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12      # 96-bit GCM nonce, random per write


def gen_master_key() -> bytes:
    """Return a fresh 32-byte (AES-256) master key from the OS CSPRNG."""
    key = os.urandom(MASTER_KEY_BYTES)
    assert isinstance(key, bytes), "master key must be bytes"
    assert len(key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
    return key


class SecretStore:
    """AES-256-GCM secret store backed by a DuckDB ``secrets`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS secrets "
            "(key TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL);"
        )

    def put(self, key: str, value: str) -> None:
        """Encrypt and store ``value`` under ``key`` (insert or update)."""
        assert key, "secret key must be non-empty"
        assert value is not None, "secret value must not be None"
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aes.encrypt(nonce, value.encode("utf-8"), key.encode("utf-8"))
        self._conn.execute(
            "INSERT INTO secrets (key, nonce, ciphertext) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET nonce = excluded.nonce, "
            "ciphertext = excluded.ciphertext;",
            [key, nonce, ciphertext],
        )

    def get(self, key: str) -> str | None:
        """Return the decrypted value for ``key``, or None if absent.

        Raises ``cryptography.exceptions.InvalidTag`` if the ciphertext fails
        authentication (wrong master key, tampering, or AAD/key mismatch).
        """
        assert key, "secret key must be non-empty"
        row = self._conn.execute(
            "SELECT nonce, ciphertext FROM secrets WHERE key = ?;", [key]
        ).fetchone()
        if row is None:
            return None
        assert len(row) == 2, "unexpected secrets row shape"
        nonce, ciphertext = bytes(row[0]), bytes(row[1])
        plaintext = self._aes.decrypt(nonce, ciphertext, key.encode("utf-8"))
        return plaintext.decode("utf-8")

    def delete(self, key: str) -> None:
        """Remove the secret stored under ``key`` (no error if absent)."""
        assert key, "secret key must be non-empty"
        self._conn.execute("DELETE FROM secrets WHERE key = ?;", [key])
        assert self.get(key) is None, "secret must be absent after delete"

    def list_keys(self) -> list[str]:
        """Return all secret key names (never values), sorted."""
        rows = self._conn.execute("SELECT key FROM secrets ORDER BY key;").fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        keys = [str(r[0]) for r in rows]
        assert len(keys) == len(rows), "key extraction must preserve count"
        return keys
