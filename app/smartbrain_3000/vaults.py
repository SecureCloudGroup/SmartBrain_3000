"""Vaults: a named, selectable subset of the knowledge base.

A vault is the unit you scope a search to ("only search my Property vault"), and — next — the unit
you EXPORT and share with someone else. This module is only the collection primitive: membership,
naming, and scoping. The portable ``.sbvault`` artifact is built on top of it.

Encrypted at rest like every other store. The vault's NAME and DESCRIPTION are inside the ciphertext
because what you called a collection ("Divorce", "Cancer treatment", "Acme acquisition") can reveal
as much as the documents inside it. ``kind`` and ``version`` stay plaintext — low-sensitivity, and
the UI filters on them without decrypting, exactly as ``tasks.status`` and ``schedule_runs.seen`` do.

Membership is many-to-many: a lease belongs in both "Property" and "2026 taxes", so it cannot be a
column on the document.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import MASTER_KEY_BYTES

_NONCE_BYTES = 12
_MAX_VAULTS = 500  # verifiable bound on the vault list (P10 #2)
_MAX_DOCS_PER_VAULT = 10_000  # bound on one vault's membership
_MAX_NAME = 200
_MAX_DESCRIPTION = 2000

LOCAL = "local"  # you authored it: yours to edit and export
IMPORTED = "imported"  # it came from someone else: an update from source may replace its documents
_KINDS = (LOCAL, IMPORTED)

# Who owns a MEMBER of a vault (vs. who owns the vault).
OWNER = "owner"  # the user's own document, which merely also sits in this vault — never clobber it
IMPORT = "import"  # came from a vault: vault-owned, and a later update may replace it
_ORIGINS = (OWNER, IMPORT)


class VaultStore:
    """AES-256-GCM vaults + their document membership, over DuckDB."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    # --- crypto (domain-separated from documents/memories/embeddings) --------------------------

    def _aad(self, vault_id: str) -> bytes:
        assert vault_id, "vault id required"
        return b"vault:" + vault_id.encode("utf-8")

    def _seal(self, vault_id: str, body: dict) -> tuple[bytes, bytes]:
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps(body).encode("utf-8")
        return nonce, self._aes.encrypt(nonce, plaintext, self._aad(vault_id))

    def _open(self, vault_id: str, nonce: bytes, ciphertext: bytes) -> dict:
        plaintext = self._aes.decrypt(nonce, ciphertext, self._aad(vault_id))
        body = json.loads(plaintext.decode("utf-8"))
        assert "name" in body, "vault body malformed"
        body.setdefault("description", "")
        return body

    # Every write to an EXISTING body must go through _load_body → mutate → _store_body. A
    # rebuild-from-known-fields would silently destroy anything a future writer put in the body
    # (e.g. a publisher pin) — inside the ciphertext, where no log or diff would show the loss.

    def _load_body(self, vault_id: str) -> dict | None:
        """The decrypted body of one vault, or None — the read half of read-modify-write."""
        row = self._conn.execute(
            "SELECT nonce, ciphertext FROM vaults WHERE id = ?;", [vault_id]
        ).fetchone()
        if row is None:
            return None
        return self._open(vault_id, bytes(row[0]), bytes(row[1]))

    def _store_body(self, vault_id: str, body: dict) -> None:
        """Re-seal a body read via _load_body — the write half of read-modify-write."""
        nonce, ciphertext = self._seal(vault_id, body)
        self._conn.execute(
            "UPDATE vaults SET nonce = ?, ciphertext = ?, updated_at = now() WHERE id = ?;",
            [nonce, ciphertext, vault_id],
        )

    # --- vaults ---------------------------------------------------------------------------------

    def create(self, name: str, description: str = "", *, kind: str = LOCAL, source: dict | None = None) -> str:
        """Create a vault; return its id. ``source`` carries import provenance (set by an import)."""
        assert name, "vault name required"
        assert kind in _KINDS, "unknown vault kind"
        vault_id = str(uuid.uuid4())
        body = {"name": name[:_MAX_NAME], "description": description[:_MAX_DESCRIPTION]}
        if source:
            body["source"] = source  # e.g. {url, publisher_pubkey} — pinned at import time
        nonce, ciphertext = self._seal(vault_id, body)
        self._conn.execute(
            "INSERT INTO vaults (id, kind, version, nonce, ciphertext) VALUES (?, ?, 1, ?, ?);",
            [vault_id, kind, nonce, ciphertext],
        )
        return vault_id

    def get(self, vault_id: str) -> dict | None:
        """Return one vault (decrypted) with its document count, or None."""
        assert vault_id, "vault id required"
        row = self._conn.execute(
            "SELECT id, kind, version, nonce, ciphertext, created_at, updated_at "
            "FROM vaults WHERE id = ?;",
            [vault_id],
        ).fetchone()
        if row is None:
            return None
        return self._row(row)

    def list_vaults(self) -> list[dict]:
        """All vaults (newest first, bounded)."""
        rows = self._conn.execute(
            "SELECT id, kind, version, nonce, ciphertext, created_at, updated_at FROM vaults "
            f"ORDER BY created_at DESC LIMIT {_MAX_VAULTS};"
        ).fetchall()
        return [self._row(r) for r in rows]  # bounded by _MAX_VAULTS

    def _row(self, row) -> dict:
        vault_id = str(row[0])
        body = self._open(vault_id, bytes(row[3]), bytes(row[4]))
        return {
            "id": vault_id,
            "kind": str(row[1]),
            "version": int(row[2]),
            "name": body["name"],
            "description": body.get("description", ""),
            "source": body.get("source"),
            "doc_count": self.count_documents(vault_id),
            "created_at": str(row[5]),
            "updated_at": str(row[6]),
        }

    def update(self, vault_id: str, name: str, description: str = "") -> bool:
        """Rename / re-describe a vault. False if it doesn't exist."""
        assert vault_id and name, "vault id + name required"
        body = self._load_body(vault_id)
        if body is None:
            return False
        # Change ONLY the fields this method writes; everything else (source, key, fields owned
        # by future writers) rides along verbatim — see the read-modify-write note above.
        body["name"] = name[:_MAX_NAME]
        body["description"] = description[:_MAX_DESCRIPTION]
        self._store_body(vault_id, body)
        return True

    def delete(self, vault_id: str) -> None:
        """Delete a vault and its membership rows. The DOCUMENTS are left alone.

        Deleting a collection must never delete its contents: the same document may sit in other
        vaults, and even if it doesn't, "remove this grouping" is not "shred my files".
        """
        assert vault_id, "vault id required"
        self._conn.execute("DELETE FROM vault_documents WHERE vault_id = ?;", [vault_id])
        self._conn.execute("DELETE FROM vaults WHERE id = ?;", [vault_id])

    def bump_version(self, vault_id: str) -> int:
        """Increment the vault's monotonic version (an export publishes a version)."""
        assert vault_id, "vault id required"
        self._conn.execute(
            "UPDATE vaults SET version = version + 1, updated_at = now() WHERE id = ?;", [vault_id]
        )
        row = self._conn.execute("SELECT version FROM vaults WHERE id = ?;", [vault_id]).fetchone()
        return int(row[0]) if row else 0

    # --- membership -----------------------------------------------------------------------------

    def add_documents(self, vault_id: str, doc_ids: list[str], origin: str = OWNER) -> int:
        """Add documents to a vault (idempotent); return how many were newly added.

        ``origin`` records WHO owns the member. 'import' = it came from someone else's vault, so a
        later update from that vault may replace it. 'owner' = the user's own document, which merely
        also sits in this vault — a vault update must NEVER clobber it.
        """
        assert vault_id, "vault id required"
        assert origin in _ORIGINS, "unknown membership origin"
        assert len(doc_ids) <= _MAX_DOCS_PER_VAULT, "too many documents in one call"
        added = 0
        for doc_id in doc_ids:  # bounded by _MAX_DOCS_PER_VAULT
            if not doc_id:
                continue
            existing = self._conn.execute(
                "SELECT 1 FROM vault_documents WHERE vault_id = ? AND doc_id = ?;", [vault_id, doc_id]
            ).fetchone()
            if existing is not None:
                continue  # already a member — adding twice is a no-op, not an error
            self._conn.execute(
                "INSERT INTO vault_documents (vault_id, doc_id, origin) VALUES (?, ?, ?);",
                [vault_id, doc_id, origin],
            )
            added += 1
        return added

    def origin_of(self, vault_id: str, doc_id: str) -> str | None:
        """Who owns this membership — 'import' (vault-owned) or 'owner' (the user's own document)."""
        row = self._conn.execute(
            "SELECT origin FROM vault_documents WHERE vault_id = ? AND doc_id = ?;", [vault_id, doc_id]
        ).fetchone()
        return str(row[0]) if row else None

    def remember_key(self, vault_id: str, key: bytes) -> None:
        """Store the Vault Key of a vault we exported, so the user can re-show it to a friend
        without re-exporting (which would mint a NEW key and orphan the file already sent)."""
        assert len(key) == 32, "vault key must be 32 bytes"
        body = self._load_body(vault_id)
        assert body is not None, "vault must exist"
        # Only the key changes; every other body field rides along verbatim (see above).
        body["key"] = base64.b64encode(key).decode("ascii")
        self._store_body(vault_id, body)

    def get_key(self, vault_id: str) -> bytes | None:
        """The stored Vault Key, or None if this vault has never been exported."""
        body = self._load_body(vault_id)
        if body is None:
            return None
        raw = body.get("key")
        return base64.b64decode(raw) if raw else None

    def remove_documents(self, vault_id: str, doc_ids: list[str]) -> int:
        """Remove documents from a vault. The documents themselves are NOT deleted."""
        assert vault_id, "vault id required"
        removed = 0
        for doc_id in doc_ids[:_MAX_DOCS_PER_VAULT]:  # bounded
            cur = self._conn.execute(
                "DELETE FROM vault_documents WHERE vault_id = ? AND doc_id = ?;", [vault_id, doc_id]
            )
            removed += 1 if cur else 0
        return removed

    def document_ids(self, vault_id: str) -> list[str]:
        """The document ids in a vault — the SCOPE a search is restricted to."""
        assert vault_id, "vault id required"
        rows = self._conn.execute(
            "SELECT doc_id FROM vault_documents WHERE vault_id = ? "
            f"ORDER BY added_at DESC LIMIT {_MAX_DOCS_PER_VAULT};",
            [vault_id],
        ).fetchall()
        return [str(r[0]) for r in rows]

    def count_documents(self, vault_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM vault_documents WHERE vault_id = ?;", [vault_id]
        ).fetchone()
        return int(row[0]) if row else 0

    def vaults_for_document(self, doc_id: str) -> list[str]:
        """Which vaults a document belongs to (it can be in several)."""
        assert doc_id, "doc id required"
        rows = self._conn.execute(
            f"SELECT vault_id FROM vault_documents WHERE doc_id = ? LIMIT {_MAX_VAULTS};", [doc_id]
        ).fetchall()
        return [str(r[0]) for r in rows]

    def forget_document(self, doc_id: str) -> None:
        """Drop a deleted document from every vault, so no vault points at a ghost."""
        assert doc_id, "doc id required"
        self._conn.execute("DELETE FROM vault_documents WHERE doc_id = ?;", [doc_id])
