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

from .kb import _clean_tags
from .secrets import MASTER_KEY_BYTES

_NONCE_BYTES = 12
_MAX_VAULTS = 500  # verifiable bound on the vault list (P10 #2)
_MAX_DOCS_PER_VAULT = 10_000  # bound on one vault's membership
_MAX_SOURCES_PER_MEMBER = 32  # bound on upstream {uid, hash} entries sharing one deduped doc
_MAX_NAME = 200
_MAX_DESCRIPTION = 2000

LOCAL = "local"  # you authored it: yours to edit and export
IMPORTED = "imported"  # it came from someone else: an update from source may replace its documents
_KINDS = (LOCAL, IMPORTED)

# Who owns a MEMBER of a vault (vs. who owns the vault).
OWNER = "owner"  # the user's own document, which merely also sits in this vault — never clobber it
IMPORT = "import"  # came from a vault: vault-owned, and a later update may replace it
FEED = "feed"  # pulled from a website's feed: the open internet, unattended — never the user's words
_ORIGINS = (OWNER, IMPORT, FEED)
_UNTRUSTED_ORIGINS = (IMPORT, FEED)


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

    def create(
        self, name: str, description: str = "", *, kind: str = LOCAL,
        source: dict | None = None, tags: list[str] | None = None,
    ) -> str:
        """Create a vault; return its id. ``source`` carries import provenance (set by an import)."""
        assert name, "vault name required"
        assert kind in _KINDS, "unknown vault kind"
        vault_id = str(uuid.uuid4())
        body = {"name": name[:_MAX_NAME], "description": description[:_MAX_DESCRIPTION]}
        if tags:
            body["tags"] = _clean_tags(tags)
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
        version = int(row[2])
        published_open = bool(body.get("published_open", False))
        # published_seq = the last seq an OPEN export was published under (the truthful "public
        # version"). Legacy vaults published before this field existed had no counter, so fall
        # back to ``version`` — the value the export handler bumped just before pack() saw it.
        published_seq_raw = body.get("published_seq")
        if isinstance(published_seq_raw, int) and published_seq_raw >= 0:
            published_seq: int | None = published_seq_raw
        else:
            published_seq = version if published_open else None
        sealed_seq_raw = body.get("sealed_seq")
        sealed_seq = sealed_seq_raw if isinstance(sealed_seq_raw, int) and sealed_seq_raw >= 0 else None
        return {
            "id": vault_id,
            "kind": str(row[1]),
            "version": version,
            # ``internal_seq`` == the counter every export bumps (public + private share this).
            # The UI compares ``internal_seq > published_seq`` to say "changed since last publish".
            "internal_seq": version,
            "name": body["name"],
            "description": body.get("description", ""),
            "tags": body.get("tags", []),
            "source": body.get("source"),
            # Once true, forever true: an open publish put the plaintext in the world, and no later
            # action can take it back — the UI badge must outlive re-seals, renames, everything.
            "published_open": published_open,
            "published_seq": published_seq,
            "published_at": body.get("published_at") or None,
            # Retired-published is a publisher-side one-way marker: this vault has been retired to
            # every subscriber. A later normal open export un-retires it (see vault_sync).
            "retired_published": bool(body.get("retired_published", False)),
            # Sealed-share side of the same story: has this vault ever been sealed-shared, and at
            # what seq. The UI shows a re-key warning when a sealed re-export mints a fresh key.
            "shared_sealed": bool(body.get("shared_sealed", False)),
            "sealed_seq": sealed_seq,
            # Publisher meta (imported/subscribed vaults): the publisher's own name/description as
            # signed by them; local ``name``/``description`` is what THIS user renamed the vault to.
            "publisher_name": body.get("publisher_name") or "",
            "publisher_description": body.get("publisher_description") or "",
            # The publisher's own hosted-URL note (open-published vaults): where the user uploaded
            # the .sbvault so a friend could subscribe. Local metadata only — never travels in the
            # export — and the verify-hosted route reads it to check the hosted file against this
            # install's published_seq. Empty string when unset.
            "hosted_url": body.get("hosted_url") or "",
            "doc_count": self.count_documents(vault_id),
            "created_at": str(row[5]),
            "updated_at": str(row[6]),
        }

    def update(self, vault_id: str, name: str, description: str = "",
               tags: list[str] | None = None, hosted_url: str | None = None) -> bool:
        """Rename / re-describe / re-tag / re-note-hosted-URL a vault. False if it doesn't exist.

        ``tags=None`` means UNTOUCHED (a rename-only PATCH must not wipe them); pass ``[]``
        to clear. Tags never travel in exports — they're the local user's organization.

        ``hosted_url`` follows the same absent/empty rule: ``None`` = untouched, ``""`` = clear,
        any other string is stored verbatim (the caller validates its shape). Local metadata:
        never rides an export — it's where THIS install remembers the publisher uploaded the file.
        """
        assert vault_id and name, "vault id + name required"
        assert tags is None or isinstance(tags, list), "tags must be None or a list"
        body = self._load_body(vault_id)
        if body is None:
            return False
        # Change ONLY the fields this method writes; everything else (source, key, fields owned
        # by future writers) rides along verbatim — see the read-modify-write note above.
        body["name"] = name[:_MAX_NAME]
        body["description"] = description[:_MAX_DESCRIPTION]
        if tags is not None:
            body["tags"] = _clean_tags(tags)
        if hosted_url is not None:
            if hosted_url:
                body["hosted_url"] = hosted_url
            else:
                body.pop("hosted_url", None)
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

    def note_imported_doc(self, doc_id: str) -> None:
        """Record that ``doc_id`` was minted from a vault import. One-way, permanent, plaintext.

        This is what distinguishes a user-authored doc that HAPPENS to match a subscribed vault's
        content from an ex-import orphan whose vault was deleted (both look identical otherwise:
        find_duplicate matches, doc has no memberships). The distinction lets a re-subscribe
        re-adopt the orphan as vault-owned (so updates apply again — the freeze-trap fix) while
        keeping the user-authored duplicate owner-origin (their rename/delete keeps working, and
        an upstream delete never takes their authored copy).
        """
        assert doc_id, "doc id required"
        # ON CONFLICT DO NOTHING is idempotent — a re-import (Bob imports Alice's vault twice,
        # deduped both times) writes the row once; a re-adopted orphan writes it again as a no-op.
        self._conn.execute(
            "INSERT INTO vault_import_traces (doc_id) VALUES (?) ON CONFLICT DO NOTHING;",
            [doc_id],
        )

    def was_ever_imported(self, doc_id: str) -> bool:
        """True iff ``doc_id`` was minted by a vault import at any point in its history — the
        signal a re-subscribe uses to un-freeze orphans without ever converting a user-authored
        duplicate into vault-owned content (see note_imported_doc)."""
        assert doc_id, "doc id required"
        row = self._conn.execute(
            "SELECT 1 FROM vault_import_traces WHERE doc_id = ? LIMIT 1;", [doc_id]
        ).fetchone()
        return row is not None

    def import_origin_doc_ids(self, vault_id: str) -> list[str]:
        """The doc ids in a vault whose membership is import-origin — the docs a caller may opt
        to delete alongside the vault (``DELETE /api/vaults/{id}?remove_docs=1``). Owner-origin
        members are the user's own documents and are never included, even when the caller asks
        to remove everything (the "delete grouping vs shred files" invariant still holds for
        owner-origin members).
        """
        assert vault_id, "vault id required"
        rows = self._conn.execute(
            "SELECT doc_id FROM vault_documents WHERE vault_id = ? AND origin = ? "
            f"LIMIT {_MAX_DOCS_PER_VAULT};",
            [vault_id, IMPORT],
        ).fetchall()
        return [str(r[0]) for r in rows]

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

    def detach(self, vault_id: str, doc_id: str) -> bool:
        """Flip one import-origin membership to 'owner' — the user claims this copy as theirs.

        After a detach the document behaves like anything the user authored: rename/delete work
        again, and a future vault update must skip it instead of replacing it. Only the ONE
        membership named here flips; the same document's rows in other vaults are untouched.
        Returns False when there was no import-origin membership to flip (idempotent, like add).
        """
        assert vault_id and doc_id, "vault id + doc id required"
        if self.origin_of(vault_id, doc_id) != IMPORT:
            return False
        self._conn.execute(
            "UPDATE vault_documents SET origin = ? WHERE vault_id = ? AND doc_id = ?;",
            [OWNER, vault_id, doc_id],
        )
        return True

    # --- member provenance (migration 23): the upstream {uid, hash} an update diffs against -------

    def _member_aad(self, vault_id: str, doc_id: str) -> bytes:
        """AAD for one membership's encrypted body — domain-separated from the vault body's."""
        assert vault_id and doc_id, "vault id + doc id required"
        return b"vault_doc:" + vault_id.encode("utf-8") + b":" + doc_id.encode("utf-8")

    def _open_member(self, vault_id: str, doc_id: str, nonce: bytes, ciphertext: bytes) -> list[dict]:
        """Decrypt one membership body as a LIST of {uid, hash} entries.

        Backward compat: a body written before multi-source support is a single dict — read it as
        a one-entry list (the AAD is unchanged, so old rows decrypt as-is).
        """
        body = json.loads(
            self._aes.decrypt(nonce, ciphertext, self._member_aad(vault_id, doc_id)).decode("utf-8"))
        return [body] if isinstance(body, dict) else body

    def note_member_source(self, vault_id: str, doc_id: str, uid: str, content_hash: str,
                           landed_hash: str) -> None:
        """Record where a member CAME FROM: the publisher's ``uid`` (the update key — which local
        document is upstream's X?), the SIGNED content ``hash`` (tree-delta detection: did the
        publisher change this doc?), and the ``landed_hash`` (owner-edit detection: did the USER
        change their local copy?). Set by import/subscribe/update; owner-added rows never get one.

        Why TWO hashes: the signed ``hash`` covers the publisher's RAW title/meta, but landing
        NORMALIZES (title[:MAX_TITLE] or "Untitled", _clean_meta drops unknown keys / clips a long
        source_url), so the local copy legitimately hashes differently. Comparing a normalized-on-
        landing doc against the signed hash would misread it as a user edit and detach it — killing
        every future update for a class of real docs. ``landed_hash`` records what we ACTUALLY
        landed, so the owner-edit guard compares like with like.

        The body is a LIST of {uid, hash, landed_hash} entries, appended-or-replaced by uid: two
        upstream docs with identical content dedupe to ONE local doc, and BOTH uids must survive on
        that shared row — overwriting would silently lose the first, and a future update would
        re-add it as "new" or mis-diff. Bounded by _MAX_SOURCES_PER_MEMBER.

        Encrypted, not plaintext columns: a stored content hash is a plaintext fingerprint of
        encrypted content — exactly what we don't keep (kbindex.content_hash's rule) — and where a
        document came from is as sensitive as what it says (kb._seal's rule).
        """
        assert vault_id and doc_id and uid and content_hash and landed_hash, \
            "vault/doc/uid/hash/landed_hash required"
        row = self._conn.execute(
            "SELECT nonce, ciphertext FROM vault_documents WHERE vault_id = ? AND doc_id = ?;",
            [vault_id, doc_id],
        ).fetchone()
        entries: list[dict] = []
        if row is not None and row[0] is not None and row[1] is not None:
            entries = self._open_member(vault_id, doc_id, bytes(row[0]), bytes(row[1]))
        entries = [e for e in entries if e["uid"] != uid]  # replace-by-uid: re-noting updates the hashes
        entries.append({"uid": uid, "hash": content_hash, "landed_hash": landed_hash})
        # A real refusal, not an assert: asserts vanish under `python -O`, so a publisher shipping
        # >32 identical-content docs would otherwise grow this list unbounded instead of refusing.
        if len(entries) > _MAX_SOURCES_PER_MEMBER:
            raise ValueError("too many upstream sources for one member")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aes.encrypt(
            nonce, json.dumps(entries).encode("utf-8"), self._member_aad(vault_id, doc_id))
        self._conn.execute(
            "UPDATE vault_documents SET nonce = ?, ciphertext = ? WHERE vault_id = ? AND doc_id = ?;",
            [nonce, ciphertext, vault_id, doc_id],
        )

    def member_map(self, vault_id: str) -> dict[str, dict]:
        """{uid: {doc_id, hash, landed_hash, origin}} for every member with a recorded upstream
        source. ``landed_hash`` is None for entries written before it existed (#77) — the caller
        gives those the benefit of the doubt rather than a false-positive detach.

        The lookup table a vault update applies against: uid present -> update/skip decision;
        uid absent -> a new document. Each row's entries fan out to their own uid keys — several
        uids mapping to the SAME doc_id is the dedupe case, not an error. Bounded decrypt-scan
        (<= _MAX_DOCS_PER_VAULT rows x _MAX_SOURCES_PER_MEMBER entries). Rows without a body —
        documents the user added to the vault themselves — have no upstream uid and are
        skipped: they are not errors, they are simply not the publisher's to touch.
        """
        assert vault_id, "vault id required"
        rows = self._conn.execute(
            "SELECT doc_id, origin, nonce, ciphertext FROM vault_documents WHERE vault_id = ? "
            f"LIMIT {_MAX_DOCS_PER_VAULT};",
            [vault_id],
        ).fetchall()
        out: dict[str, dict] = {}
        for row in rows:  # bounded by _MAX_DOCS_PER_VAULT
            if row[2] is None or row[3] is None:
                continue  # owner-added row: no upstream source to map
            doc_id = str(row[0])
            for entry in self._open_member(vault_id, doc_id, bytes(row[2]), bytes(row[3])):
                out[entry["uid"]] = {"doc_id": doc_id, "hash": entry["hash"],
                                     "landed_hash": entry.get("landed_hash"), "origin": str(row[1])}
        return out

    def import_provenance(self, doc_id: str) -> dict | None:
        """Where this document CAME FROM, if any of its memberships is import-origin; else None.

        Returns {vault_id, name, publisher_pubkey|None}. This runs on every rename/delete attempt
        and on every tool read of a document, so it is one bounded indexed lookup plus at most one
        vault-body decrypt — never a scan.
        """
        assert doc_id, "doc id required"
        # ORDER BY so the SAME vault is named every time a doc sits import-origin in several
        # vaults — an unordered LIMIT 1 would let the 409 detail / provenance line flip between calls.
        row = self._conn.execute(
            "SELECT vault_id, origin FROM vault_documents WHERE doc_id = ? AND origin IN (?, ?) "
            "ORDER BY added_at ASC LIMIT 1;",
            [doc_id, IMPORT, FEED],
        ).fetchone()
        if row is None:
            return None
        vault = self.get(str(row[0]))
        if vault is None:
            return None  # membership outlived its vault (delete() clears rows, so only a race)
        source = vault.get("source") or {}
        return {
            "vault_id": vault["id"],
            "origin": str(row[1]),
            "name": vault["name"],
            "publisher_pubkey": source.get("publisher_pubkey"),
        }

    def update_source(self, vault_id: str, changes: dict) -> None:
        """Read-modify-write the pin (the encrypted body's ``source``): merge ``changes`` in.

        A value of None REMOVES that key (how a cleared ``blocked`` marker goes away). Everything
        else in the body — and every source field not named — rides along verbatim: the pin is the
        trust anchor every update is verified against, and losing a field of it silently (inside
        the ciphertext, where no diff would show it) is the exact failure the read-modify-write
        rule above exists to prevent.
        """
        assert vault_id, "vault id required"
        assert isinstance(changes, dict) and changes, "changes required"
        body = self._load_body(vault_id)
        assert body is not None, "vault must exist"
        source = body.get("source") or {}
        for key, value in changes.items():  # bounded by the caller's literal dict
            if value is None:
                source.pop(key, None)
            else:
                source[key] = value
        body["source"] = source
        self._store_body(vault_id, body)

    def forget_member_source(self, vault_id: str, doc_id: str, uid: str) -> None:
        """Drop ONE upstream {uid, hash} entry from a membership body (the publisher deleted it).

        The membership row itself stays — whether the document leaves the vault is the caller's
        decision (an owner-origin copy never does), not a side effect of pruning provenance. A row
        with no body, or without this uid, is left untouched (idempotent, like add/detach).
        """
        assert vault_id and doc_id and uid, "vault/doc/uid required"
        row = self._conn.execute(
            "SELECT nonce, ciphertext FROM vault_documents WHERE vault_id = ? AND doc_id = ?;",
            [vault_id, doc_id],
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return
        entries = [e for e in self._open_member(vault_id, doc_id, bytes(row[0]), bytes(row[1]))
                   if e["uid"] != uid]
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aes.encrypt(
            nonce, json.dumps(entries).encode("utf-8"), self._member_aad(vault_id, doc_id))
        self._conn.execute(
            "UPDATE vault_documents SET nonce = ?, ciphertext = ? WHERE vault_id = ? AND doc_id = ?;",
            [nonce, ciphertext, vault_id, doc_id],
        )

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

    def note_open_publish(self, vault_id: str, name_key: bytes, *, seq: int,
                          published_at: str, index_hash: str, retired: bool = False) -> bool:
        """Record an OPEN publish in the encrypted body: the ``published_open`` marker (drives the
        UI's "Public" badge — publishing is irreversible, so the flag never clears), and, on the
        FIRST open publish only, the object-naming key.

        Also records ``published_seq`` (the truthful "public version" the UI shows — distinct from
        ``version``, which every export bumps), ``published_at`` (the plaintext calendar date the
        manifest carries), ``retired_published`` (marker for a publisher-retired vault; cleared by
        the next normal open publish per the un-retire rule), and the last export's INDEX HASH
        (per mode) — the caller compares against it to surface "you republished with no changes".
        Returns True when the index hash equals the previously-stored one (unchanged republish).

        K_name must be fixed once and persisted: object names are HMAC(K_name, ...), so a
        republish under a fresh key would rename every object and turn a tree-host delta update
        into a full re-download (plan decision #6). A later publish never overwrites it — the
        first publish is the one subscribers pinned their tree against.
        """
        assert len(name_key) == 32, "name key must be 32 bytes"
        assert isinstance(seq, int) and seq > 0, "seq must be a positive integer"
        body = self._load_body(vault_id)
        assert body is not None, "vault must exist"
        body["published_open"] = True
        if "name_key" not in body:
            body["name_key"] = base64.b64encode(name_key).decode("ascii")
        body["published_seq"] = int(seq)
        body["published_at"] = str(published_at)
        # Un-retire on a normal open publish: the publisher came back. A retire-export sets the
        # flag explicitly (retired=True), so a later normal publish clearing it here is exactly
        # the spec's un-retire rule.
        body["retired_published"] = bool(retired)
        previous = body.get("last_open_index_hash")
        body["last_open_index_hash"] = str(index_hash)
        self._store_body(vault_id, body)
        return isinstance(previous, str) and previous == str(index_hash)

    def note_sealed_publish(self, vault_id: str, *, seq: int, index_hash: str) -> tuple[bool, bool]:
        """Record a SEALED export in the encrypted body: ``shared_sealed`` (has this vault ever
        been sealed-shared) + ``sealed_seq`` (the seq of the LAST sealed export — the UI compares
        against ``internal_seq`` to note "changed since last sealed share") + the last export's
        index hash. Returns ``(rotated_key, unchanged)``:

        * ``rotated_key`` is True whenever ``shared_sealed`` was already set — every sealed export
          mints a fresh Vault Key (see vault_routes.export_vault), and a re-export orphans every
          recipient of the previous file. That's the UI's re-key warning.
        * ``unchanged`` mirrors the open-side signal: the produced index hash equals the previous
          sealed export's — the caller warns "you re-shared with no changes to the documents".
        """
        assert isinstance(seq, int) and seq > 0, "seq must be a positive integer"
        body = self._load_body(vault_id)
        assert body is not None, "vault must exist"
        rotated = bool(body.get("shared_sealed", False))
        previous = body.get("last_sealed_index_hash")
        body["shared_sealed"] = True
        body["sealed_seq"] = int(seq)
        body["last_sealed_index_hash"] = str(index_hash)
        self._store_body(vault_id, body)
        return rotated, (isinstance(previous, str) and previous == str(index_hash))

    def note_publisher_meta(self, vault_id: str, *, name: str, description: str) -> str | None:
        """Record the publisher's own name/description into the imported vault's body (the values
        signed by the publisher, distinct from the local ``name``/``description`` this user can
        rename freely). Called by subscribe and by every applied update, so a publisher's rename
        propagates to subscribers without clobbering a local rename the user chose.

        Returns the PREVIOUS ``publisher_name`` when it changed (so the update result can note
        "the publisher renamed this to X"), else None. Blank inputs are stored as empty strings —
        never as absent — so a publisher who clears a description propagates that clear too.
        """
        assert vault_id, "vault id required"
        body = self._load_body(vault_id)
        assert body is not None, "vault must exist"
        prev = body.get("publisher_name")
        body["publisher_name"] = str(name)[:_MAX_NAME]
        body["publisher_description"] = str(description)[:_MAX_DESCRIPTION]
        self._store_body(vault_id, body)
        prev_str = str(prev) if isinstance(prev, str) else ""
        if prev_str and prev_str != body["publisher_name"]:
            return prev_str
        return None

    def get_name_key(self, vault_id: str) -> bytes | None:
        """The persisted object-naming key, or None if this vault was never published open."""
        body = self._load_body(vault_id)
        if body is None:
            return None
        raw = body.get("name_key")
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

    def members(self, vault_id: str) -> list[dict]:
        """The documents in a vault WITH each membership's origin.

        The UI needs the origin per row to show Detach only on vault-owned (import-origin) members
        — an owner-origin document has nothing to detach from.
        """
        assert vault_id, "vault id required"
        rows = self._conn.execute(
            "SELECT doc_id, origin FROM vault_documents WHERE vault_id = ? "
            f"ORDER BY added_at DESC LIMIT {_MAX_DOCS_PER_VAULT};",
            [vault_id],
        ).fetchall()
        return [{"id": str(r[0]), "origin": str(r[1])} for r in rows]

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
