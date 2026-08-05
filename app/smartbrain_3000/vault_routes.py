"""Vaults HTTP API: create a named subset of the knowledge base, and scope a search to it.

A vault is the unit you collect documents into, search within, and (next) export and share. This is
the collection primitive only — the portable ``.sbvault`` artifact is built on top of it.

Deleting a vault never deletes its documents: the same document may sit in other vaults, and
"remove this grouping" is not "shred my files".
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urldefrag, urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import gateway, identity, kb as kbmod, netguard, tools, vault_format, vault_sync
from .data_routes import _reauthorize, _require_desktop_local
from .vaults import IMPORT, IMPORTED

router = APIRouter()
log = logging.getLogger(__name__)

_MAX_IDS_PER_CALL = 1000  # bounded membership edit (P10 #2)


class VaultIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=20)  # None = untouched; [] = clear


class DocIdsIn(BaseModel):
    doc_ids: list[str] = Field(default_factory=list)


class ExportIn(BaseModel):
    # Re-auth, exactly as /api/backup and /api/export require: a vault export hands out content that
    # is plaintext-equivalent to whoever holds the key — and in open mode IS the plaintext.
    passphrase: str | None = None
    recovery_key: str | None = None
    include_vectors: bool = True
    mode: Literal["sealed", "open"] = "sealed"  # private share stays the default


class ImportIn(BaseModel):
    key: str = Field(min_length=1)  # the SBVK1-... vault key


class SubscribeIn(BaseModel):
    # 2048 is the classic practical URL ceiling; a longer one is not a vault link.
    url: str = Field(min_length=1, max_length=2048)


def _vaults(request: Request):
    """Return the unlocked VaultStore, or raise 423."""
    store = getattr(request.app.state, "vaults", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


def _require(store, vault_id: str) -> dict:
    vault = store.get(vault_id)
    if vault is None:
        raise HTTPException(status_code=404, detail="vault not found")
    return vault


def _attach_publisher_fp(request: Request, vaults: list[dict]) -> None:
    """Attach the publisher fingerprint (in place) to every published-open vault.

    Hard UI rule: a "Public" label never appears without the identity behind it — the fingerprint
    is what a subscriber actually pins. Every open publish from this Desktop is signed by the same
    vault:publisher_ed25519 key, so one derivation covers the whole list.
    """
    if not any(v.get("published_open") for v in vaults):
        return  # don't touch (or lazily create) the publisher key for users who never publish
    fp = vault_format.fingerprint(
        identity.public_key_b64(_secrets(request), identity.VAULT_PUBLISHER_SECRET))
    for vault in vaults:  # bounded by vaults._MAX_VAULTS
        if vault.get("published_open"):
            vault["publisher_fingerprint"] = fp


def _attach_pinned_fp(vaults: list[dict]) -> None:
    """Attach the PINNED publisher's fingerprint (in place) to every imported/subscribed vault.

    Same hard UI rule as _attach_publisher_fp, pointing the other way: a "Subscribed" badge never
    appears without the identity it is pinned to — the fingerprint every future update must match.
    """
    for vault in vaults:  # bounded by vaults._MAX_VAULTS
        source = vault.get("source") or {}
        pubkey = source.get("publisher_pubkey")
        if isinstance(pubkey, str) and pubkey:
            vault["pinned_fingerprint"] = vault_format.fingerprint(pubkey)
        offered = (source.get("blocked") or {}).get("offered_pubkey")
        if isinstance(offered, str) and offered:
            # A blocked subscription's OTHER identity: the key-change warning must show both
            # fingerprints side by side, so the offered one rides the list response too.
            vault["blocked_fingerprint"] = vault_format.fingerprint(offered)


@router.get("/api/vaults")
def list_vaults(request: Request) -> dict:
    """All vaults, with how many documents each holds."""
    vaults = _vaults(request).list_vaults()
    _attach_publisher_fp(request, vaults)
    _attach_pinned_fp(vaults)
    return {"vaults": vaults}


@router.post("/api/vaults")
def create_vault(request: Request, body: VaultIn) -> dict:
    """Create an empty vault."""
    store = _vaults(request)
    vault_id = store.create(body.name.strip(), body.description.strip(), tags=body.tags)
    return store.get(vault_id)


@router.get("/api/vaults/{vault_id}")
def get_vault(request: Request, vault_id: str) -> dict:
    """One vault, plus the documents in it (with each membership's origin).

    ``members`` carries {id, origin} so the UI can offer Detach only on vault-owned
    (import-origin) rows; ``doc_ids`` stays for existing callers.
    """
    store = _vaults(request)
    vault = _require(store, vault_id)
    _attach_publisher_fp(request, [vault])
    _attach_pinned_fp([vault])
    members = store.members(vault_id)
    return {**vault, "doc_ids": [m["id"] for m in members], "members": members}


@router.patch("/api/vaults/{vault_id}")
def update_vault(request: Request, vault_id: str, body: VaultIn) -> dict:
    """Rename / re-describe / re-tag a vault (tags absent = untouched)."""
    store = _vaults(request)
    _require(store, vault_id)
    store.update(vault_id, body.name.strip(), body.description.strip(), tags=body.tags)
    return store.get(vault_id)


@router.delete("/api/vaults/{vault_id}")
def delete_vault(request: Request, vault_id: str, remove_docs: bool = False) -> dict:
    """Delete the vault. By default its DOCUMENTS are left alone — this removes a grouping, not
    your files. Pass ``?remove_docs=1`` to also delete the vault's import-origin documents (owner-
    origin copies always stay — the "delete grouping vs shred files" invariant applies to the
    user's OWN documents, whatever they asked for).

    The default keeps the historical behavior. The opt-in exists because delete-keeping-docs on a
    URL subscription used to leave orphaned import-origin docs behind, and re-subscribing then
    matched them as ``owner`` — silently freezing every future update for them. The re-subscribe
    path now handles no-membership orphans directly (see subscribe/_apply_docs), so this flag is
    the deliberate way to *actually shred* an imported vault when that is what the user wanted.
    """
    store = _vaults(request)
    _require(store, vault_id)
    removed_docs = 0
    if remove_docs:
        knowledge = _kb(request)
        # Import-origin only — owner-origin copies are the user's own documents that merely also
        # sat in this vault (dedupe / manual add). They survive every "yes really delete it".
        for doc_id in store.import_origin_doc_ids(vault_id):  # bounded by _MAX_DOCS_PER_VAULT
            knowledge.delete(doc_id)
            removed_docs += 1
    store.delete(vault_id)
    return {"ok": True, "removed_docs": removed_docs}


@router.post("/api/vaults/{vault_id}/documents")
def add_documents(request: Request, vault_id: str, body: DocIdsIn) -> dict:
    """Add documents to a vault (idempotent — adding twice is a no-op, not an error)."""
    store = _vaults(request)
    _require(store, vault_id)
    if len(body.doc_ids) > _MAX_IDS_PER_CALL:
        raise HTTPException(status_code=400, detail=f"at most {_MAX_IDS_PER_CALL} documents per call")
    added = store.add_documents(vault_id, body.doc_ids)
    return {"added": added, "doc_count": store.count_documents(vault_id)}


@router.delete("/api/vaults/{vault_id}/documents/{doc_id}")
def remove_document(request: Request, vault_id: str, doc_id: str) -> dict:
    """Remove one document from a vault. The document itself is NOT deleted."""
    store = _vaults(request)
    _require(store, vault_id)
    store.remove_documents(vault_id, [doc_id])
    return {"ok": True, "doc_count": store.count_documents(vault_id)}


@router.post("/api/vaults/{vault_id}/documents/{doc_id}/detach")
def detach_document(request: Request, vault_id: str, doc_id: str) -> dict:
    """Make an imported copy the user's own: flip this membership's origin to 'owner'.

    A vault-owned (import-origin) document is read-only and a vault update may replace it.
    Detaching is the user saying "this copy is mine now" — rename/delete work again and any
    future update from the publisher skips it. Idempotent on an already-owner membership,
    matching add_documents' no-op philosophy.
    """
    store = _vaults(request)
    _require(store, vault_id)
    if store.origin_of(vault_id, doc_id) is None:
        raise HTTPException(status_code=404, detail="document is not in this vault")
    store.detach(vault_id, doc_id)
    return {"ok": True, "origin": "owner"}


# --- export / import ----------------------------------------------------------------------------

def _kb(request: Request):
    store = getattr(request.app.state, "kb", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


def _secrets(request: Request):
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


def _build_export_docs(vaults, knowledge, vault_id: str, include_vectors: bool,
                        embed_model: str) -> list[dict]:
    """Materialise a vault's docs into pack()-ready entries (title/content/meta/uid/chunks+vectors)."""
    docs: list[dict] = []
    for doc_id in vaults.document_ids(vault_id):  # bounded by _MAX_DOCS_PER_VAULT
        doc = knowledge.get(doc_id)
        if doc is None:
            continue  # deleted under us — a vault must never export a missing file
        entry = {
            "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"sbvault:{vault_id}:{doc_id}")),
            "title": doc["title"],
            "content": doc["content"],
            "meta": doc.get("meta") or {},
            "chunks": len(kbmod.chunk_text(doc["title"], doc["content"])),
        }
        if include_vectors:
            vectors = knowledge.vectors_for(doc_id, embed_model)
            if vectors:
                entry["vectors"] = vectors  # so the recipient can search it the moment it lands
        docs.append(entry)
    return docs


def _pack_args_for(vaults, vault_id: str, mode: str) -> tuple[dict, bool]:
    """Prepare pack() args for ``mode`` and return (pack_args, rotated_key).

    Open: reuse the persisted K_name (or seed one — decision #6 detailed above).
    Sealed: mint a fresh Vault Key and remember it; ``rotated_key`` is True iff this vault has
    previously been sealed-shared — every fresh-key export orphans past recipients.
    """
    assert mode in (vault_format.SEALED, vault_format.OPEN), "unknown mode"
    if mode == vault_format.OPEN:
        name_key = vaults.get_name_key(vault_id)
        if name_key is None:
            stored = vaults.get_key(vault_id)
            name_key = (vault_format.derive_name_key(stored, vault_id)
                        if stored is not None else os.urandom(32))
        return {"mode": vault_format.OPEN, "name_key": name_key}, False
    key = vault_format.new_vault_key()
    rotated_key = bool(vaults.get(vault_id).get("shared_sealed", False))
    vaults.remember_key(vault_id, key)  # so the user can re-show it without re-exporting
    return {"vault_key": key}, rotated_key


def _export_headers(vault_name: str, *, seq: int, mode: str, unchanged: bool,
                    rotated_key: bool, retired: bool) -> dict[str, str]:
    """Response headers for every export response. The file is the body; these headers carry the
    metadata a UI cannot recover from the download alone (unchanged republish, sealed re-key)."""
    safe = "".join(c for c in vault_name if c.isalnum() or c in " -_")[:60].strip() or "vault"
    headers = {
        "content-disposition": f'attachment; filename="{safe}.sbvault"',
        "x-sb-export-seq": str(int(seq)),
        "x-sb-export-mode": mode,
    }
    if unchanged:
        headers["x-sb-export-unchanged"] = "1"
    if rotated_key:
        headers["x-sb-export-rotated-key"] = "1"
    if retired:
        headers["x-sb-export-retired"] = "1"
    return headers


def _do_export(request: Request, vault_id: str, body: ExportIn, *, retired: bool) -> Response:
    """The shared export path — Desktop-local + re-auth, pack, persist publish state, respond.

    ``retired`` forces open mode and stamps ``retired: true`` in the manifest; the on-disk vault
    is marked ``retired_published: true`` so a later normal open publish un-retires it.
    """
    _require_desktop_local(request)
    _reauthorize(request, body)
    vaults, knowledge, secrets = _vaults(request), _kb(request), _secrets(request)
    vault = _require(vaults, vault_id)
    mode = vault_format.OPEN if retired else body.mode

    embed_model = gateway.embedding_scheme(gateway.embed_model(request.app.state.dbx))
    docs = _build_export_docs(vaults, knowledge, vault_id, body.include_vectors, embed_model)
    seq = vaults.bump_version(vault_id)  # a publish IS a version (both modes share the counter)
    pack_args, rotated_key = _pack_args_for(vaults, vault_id, mode)
    published_at = vault_format._today_utc()
    try:
        # No `label`: the publisher label sits in the PLAINTEXT manifest, and a vault's name
        # ("Divorce filings", "Acme acquisition") can reveal as much as its contents.
        blob = vault_format.pack(
            store=secrets, vault_id=vault_id, name=vault["name"],
            description=vault["description"], seq=seq, docs=docs,
            embed_model=embed_model, published_at=published_at, retired=retired, **pack_args,
        )
    except vault_format.VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # The signed ``index`` hash embeds ``seq`` and therefore always changes; ``docs_fingerprint``
    # is mode-agnostic and content-only, so it detects an unchanged republish (same set of
    # {uid, doc-hash} rows) regardless of the seq bump between the two exports.
    content_hash = vault_format.docs_fingerprint(docs)
    if mode == vault_format.OPEN:
        # Persist BEFORE responding: a file shipped under an unrecorded K_name would make the next
        # republish rename every object — a full re-download for every tree-host subscriber.
        unchanged = vaults.note_open_publish(
            vault_id, pack_args["name_key"], seq=seq, published_at=published_at,
            index_hash=content_hash, retired=retired)
    else:
        _rotated, unchanged = vaults.note_sealed_publish(
            vault_id, seq=seq, index_hash=content_hash)
    return Response(
        content=blob,
        media_type="application/octet-stream",
        headers=_export_headers(vault["name"], seq=seq, mode=mode, unchanged=unchanged,
                                rotated_key=rotated_key, retired=retired),
    )


@router.post("/api/vaults/{vault_id}/export")
def export_vault(request: Request, vault_id: str, body: ExportIn) -> Response:
    """Export a vault as a .sbvault file — SEALED (default; the KEY is fetched separately) or OPEN.

    Desktop-local AND re-authenticated in BOTH modes, exactly like /api/backup: a sealed file plus
    its key is plaintext-equivalent, and an open file IS the decrypted plaintext — the most
    sensitive egress in the app. (Reusing data_routes' helpers verbatim — "blocks a passer-by at an
    unattended-but-unlocked Desktop and a stale paired session from silently exfiltrating
    everything in one click".)

    Response headers a UI reads: ``x-sb-export-seq`` (the seq this export took), ``x-sb-export-mode``,
    optional ``x-sb-export-unchanged`` (content identical to the previous export — the UI warns),
    and optional ``x-sb-export-rotated-key`` (sealed re-export minted a fresh Vault Key — old
    recipients are orphaned; the UI warns before the user distributes the new file).
    """
    return _do_export(request, vault_id, body, retired=False)


@router.post("/api/vaults/{vault_id}/retire")
def retire_vault(request: Request, vault_id: str, body: ExportIn) -> Response:
    """Retire a public vault: produce a final open export with content INTACT and ``retired: true``.

    Same gate as /export (Desktop-local + re-auth): the produced file is decrypted plaintext, and
    "retire" is a consequential publisher action — the user distributes this last blob so their
    subscribers see the retirement. Body ``mode`` is ignored; retirement is always an open publish
    (subscribers only follow URL-shared vaults, which are open by construction).

    The publisher-side flag ``retired_published`` is set on the local vault body; a later normal
    open export un-retires it (see spec §5). Subscribers, on their next check, apply the FINAL
    content update and stop auto-updating — see vault_sync.check + _apply for the subscriber half.
    """
    return _do_export(request, vault_id, body, retired=True)


@router.post("/api/vaults/{vault_id}/key")
def vault_key(request: Request, vault_id: str, body: ExportIn) -> dict:
    """The SBVK1-... key for a vault you exported. Send it to your friend by a DIFFERENT channel."""
    _require_desktop_local(request)
    _reauthorize(request, body)
    vaults = _vaults(request)
    vault = _require(vaults, vault_id)
    key = vaults.get_key(vault_id)
    if key is None:
        if vault.get("published_open"):
            # Not "try again": an open publish deliberately has no key, and saying so plainly is
            # part of making the irreversibility impossible to miss.
            raise HTTPException(status_code=409, detail=(
                "this vault has only been published open — there is no key; "
                "anyone with the file can read it"))
        raise HTTPException(status_code=409, detail="export this vault first — it has no key yet")
    return {"key": vault_format.encode_vault_key(key)}


def _audit_import(request: Request, name: str, fp: str, seq: int, added: int, duplicates: int,
                  *, tool: str = "vault_import", host: str | None = None) -> None:
    """Audit one vault import/subscribe — INGRESS of someone else's content into the knowledge base.

    As security-relevant as any tool action, so it gets a row: what arrived (name), who signed it
    (fingerprint — the identity a human is asked to trust), and how much landed. Same
    user-initiated pattern as email_routes' send: the click is the consent, the row is the record.
    A subscribe adds the URL's HOST only — never the full URL: its path can name the topic as
    plainly as a vault name would, and a fragment could carry key material.
    """
    args = {"vault": name, "publisher": fp, "seq": seq}
    if host is not None:
        args["host"] = host
    request.app.state.audit.append(
        "user", tool, "reviewed", "executed", True,
        args_summary=tools.summarize(args),
        result_summary=tools.summarize({"added": added, "duplicates": duplicates}),
    )


def _audit_update(request: Request, name: str, fp: str, seq: int, result: dict,
                  *, host: str | None = None) -> None:
    """Audit one applied vault update — new INGRESS under an existing pin.

    Same rules as _audit_import: the pinned fingerprint is the identity, and only the URL's HOST
    ever reaches the row (a file update has none).
    """
    args = {"vault": name, "publisher": fp, "seq": seq}
    if host is not None:
        args["host"] = host
    request.app.state.audit.append(
        "user", "vault_update", "reviewed", "executed", True,
        args_summary=tools.summarize(args),
        result_summary=tools.summarize(
            {k: result[k] for k in ("added", "updated", "deleted", "kept_yours")}),
    )


def _apply_docs(request: Request, vaults, knowledge, local_id: str, manifest: dict,
                docs: list[dict]) -> dict:
    """Apply a VERIFIED vault's documents to local vault ``local_id``; return what happened.

    Shared by file import and URL subscribe — how trust was established differs, what lands must
    not. Dedupe keeps the USER's copy (never overwrite something they authored with a stranger's);
    every landed document gets a FRESH local id and is re-sealed under this user's master key (the
    GCM tag binds to doc_id, so importing a ciphertext — or clobbering an existing id — is
    structurally impossible). Both membership rows record the upstream {uid, hash}: the map a
    future update diffs against (an owner-origin row with a uid = "this uid is the user's — skip").
    Returns {added, duplicates, vectors_used}.
    """
    # Storage identity for locally-adopted vectors — same scheme the publisher wrote under,
    # so nomic vectors keep the '#tp1' marker on import and match subscriber queries.
    embed_model = gateway.embedding_scheme(gateway.embed_model(request.app.state.dbx))
    shipped = manifest.get("embeddings") or {}
    added = duplicates = 0
    for doc in docs:  # bounded by vault_format.MAX_VAULT_DOCS
        existing = knowledge.find_duplicate(doc["content"])
        if existing is not None:
            # Dedupe decision. The default is OWNER — never overwrite something the user authored
            # with a stranger's copy. The one exception is the RE-SUBSCRIBE FREEZE fix (spec §7):
            # a matching doc with no current memberships AND a permanent import trace is an ex-
            # import orphan whose vault was deleted; re-adopt it as vault-owned so updates apply
            # again. Without the trace (user-authored duplicate), keep it owner-origin so the
            # user's rename/delete stays available AND an upstream delete never takes their doc.
            is_orphan = (not vaults.vaults_for_document(existing)
                         and vaults.was_ever_imported(existing))
            origin = "import" if is_orphan else "owner"
            vaults.add_documents(local_id, [existing], origin=origin)
            vaults.note_member_source(local_id, existing, doc["uid"], doc["hash"],
                                      vault_sync._landed_hash(knowledge.get(existing)))
            duplicates += 1
            continue
        doc_id = knowledge.add(doc["title"], doc["content"], doc["meta"])
        # Permanent one-way marker: this doc was minted from a vault import. Survives a vault
        # delete that keeps docs, so a re-subscribe can spot the orphan (see dedupe branch above).
        vaults.note_imported_doc(doc_id)
        vaults.add_documents(local_id, [doc_id], origin="import")
        # landed_hash = the doc AS STORED locally (normalized) — separate from the publisher's signed
        # hash, so a doc that normalized on the way in isn't later misread as a user edit.
        vaults.note_member_source(local_id, doc_id, doc["uid"], doc["hash"],
                                  vault_sync._landed_hash(doc))
        added += 1
        vectors = doc.get("vectors")
        # Use the shipped vectors ONLY if they were made by the same model, at the same dim, with
        # the same chunker. Vectors chunked differently would give WRONG page citations, not merely
        # worse ranking — kb.chunk_span is the inverse of chunk_text and is what cuts the snippet.
        if (
            vectors
            and shipped.get("model") == embed_model
            and len(vectors) == len(kbmod.chunk_text(doc["title"], doc["content"]))
        ):
            knowledge.put_embeddings(doc_id, vectors, embed_model)
    # One bulk write, then drop the index: rebuilding it per-document is the O(n^2) path kbindex
    # warns about (19s for 10k docs). The next search rebuilds in a single pass.
    knowledge.reset_index()
    return {"added": added, "duplicates": duplicates,
            "vectors_used": bool(shipped.get("model") == embed_model)}


def _pin_for(vaults, vault_id: str) -> dict | None:
    """The existing local vault whose pin names ``vault_id``, or None.

    Bounded decrypt-scan (list_vaults is capped at vaults._MAX_VAULTS) — the resolution step both
    ingress paths run BEFORE anything lands, so a vault identity can exist here at most once.
    """
    for vault in vaults.list_vaults():  # bounded by vaults._MAX_VAULTS
        if (vault.get("source") or {}).get("vault_id") == vault_id:
            return vault
    return None


def _rollback_import(vaults, knowledge, local_id: str) -> None:
    """Undo a failed import/subscribe: the vault row, its memberships, and the docs it minted.

    All-or-nothing, for real: the vault row + pin land BEFORE the documents, so a mid-apply
    failure (KB write, embed, disk) would otherwise strand a partial vault whose pin makes every
    RETRY hit the duplicate-409 — self-blocking until a manual delete. Only import-origin docs are
    deleted: a deduped member is the USER's own doc, origin owner — kept.

    (Compensating cleanup, not a transaction: everything an import creates is freshly minted, so
    deleting it IS the undo. The update path can't say that — it overwrites bodies in place — so
    vault_sync._write wraps its writes in a real transaction instead.)
    """
    minted = [m["id"] for m in vaults.members(local_id) if m["origin"] == IMPORT]
    vaults.delete(local_id)
    for doc_id in minted:  # bounded by _MAX_DOCS_PER_VAULT
        knowledge.delete(doc_id)


def _update_from_file(request: Request, vaults, knowledge, vault: dict, manifest: dict,
                      docs: list[dict], data: bytes) -> dict:
    """Route a re-imported FILE of an already-pinned vault into the update core (§7: "imported
    from this vault — it's an update, not an import").

    The PIN — never the file — is the authority, so §5 runs here exactly as a URL check would:
    (2) the vault_id matched to reach this function; (3) the signature is verified against the
    PINNED key over the exact manifest bytes in the file (open_vault's self-check is not this
    check); (4) the seq must move forward. A sealed file updates a file-import pin fine (uids and
    hashes are mode-independent), but a URL subscription is pinned to the vault's OPEN edition —
    a sealed file there is a clear refusal, not a guess.
    """
    pin = vault.get("source") or {}
    pinned_key = pin.get("publisher_pubkey") or ""
    if not pinned_key:  # a pin without an identity can't verify anything — refuse, don't guess
        raise HTTPException(status_code=409, detail=(
            f"a vault with this identity already exists (“{vault['name']}”) but its pin is "
            "incomplete — remove it and import the file fresh"))
    fp = vault_format.fingerprint(pinned_key)
    if not vault_format.manifest_signed_by(vault_format.manifest_entry(data), pinned_key):
        raise HTTPException(status_code=409, detail=(
            f"this file names a vault you already have (“{vault['name']}”) but is signed by a "
            f"different publisher — pinned {fp}, file "
            f"{vault_format.fingerprint(manifest['publisher']['pubkey'])} — refusing"))
    if pin.get("mode") == vault_format.OPEN and manifest["mode"] == vault_format.SEALED:
        raise HTTPException(status_code=409, detail=(
            "you are subscribed to this vault's public edition, but this file is a sealed "
            "export — check the subscription for updates instead, or remove it and import "
            "the file fresh"))
    seq, pinned_seq = manifest["seq"], int(pin.get("seq") or 0)
    if seq < pinned_seq:
        raise HTTPException(status_code=409, detail=(
            f"this file is OLDER (v{seq}) than what you already have (v{pinned_seq}) — "
            "refusing to roll back"))
    base = {"id": vault["id"], "name": vault["name"], "publisher": fp, "update": True}
    if seq == pinned_seq:
        return {**base, "added": 0, "updated": 0, "deleted": 0, "kept_yours": 0,
                "seq": seq, "retired": False, "renamed_from": None}
    result = vault_sync.apply_from_docs(
        vaults, knowledge, vault["id"], manifest, docs, gateway.embed_model(request.app.state.dbx))
    log.info("updated vault %s from file to seq %d: %s", manifest["vault_id"], seq, result)
    _audit_update(request, vault["name"], fp, seq, result)
    return {**base, **result, "seq": seq}


@router.post("/api/vaults/import")
async def import_vault(request: Request, key: str = "") -> dict:
    """Import a .sbvault (raw body). Sealed needs its SBVK1- key; a PUBLIC (open) file needs none.
    Verifies, decrypts when sealed, and RE-SEALS locally.

    Imported documents are re-sealed under THIS user's master key with fresh local ids: the GCM tag
    is bound to the doc_id, so there is no such thing as importing a ciphertext — and a malicious
    vault naming a document with an id that already exists locally could otherwise clobber it.
    Minting locally makes that attack structurally impossible.

    A file whose vault_id is already pinned here routes into the UPDATE core instead of minting a
    duplicate vault (§7) — see _update_from_file.
    """
    vaults, knowledge = _vaults(request), _kb(request)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    try:
        vault_key = vault_format.decode_vault_key(key) if key.strip() else None
        manifest, docs = vault_format.open_vault(data, vault_key)
    except vault_format.VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    publisher = manifest["publisher"]
    existing = _pin_for(vaults, manifest["vault_id"])
    if existing is not None:
        return _update_from_file(request, vaults, knowledge, existing, manifest, docs, data)

    # The vault's real name comes from the ENCRYPTED index (surfaced by open_vault as _sealed) — the
    # plaintext manifest deliberately carries no topic, so a host never learns what a vault is about.
    sealed = manifest.get("_sealed") or {}
    publisher_name = sealed.get("name") or ""
    publisher_description = sealed.get("description") or ""
    local_id = vaults.create(
        (publisher_name or "Imported vault")[:200],
        publisher_description[:2000],
        kind=IMPORTED,
        source={"vault_id": manifest["vault_id"], "publisher_pubkey": publisher["pubkey"],
                "seq": manifest["seq"]},
    )
    vaults.note_publisher_meta(local_id, name=publisher_name, description=publisher_description)
    try:
        applied = _apply_docs(request, vaults, knowledge, local_id, manifest, docs)
    except Exception:
        # Same atomicity subscribe got (#77): nothing kept, retry works. vault_id only — a file
        # import has no URL, and the vault's NAME is the topic, which never reaches a log.
        _rollback_import(vaults, knowledge, local_id)
        log.exception("import failed applying vault %s; rolled back", manifest["vault_id"])
        raise HTTPException(status_code=500, detail=(
            "import failed part-way — nothing was kept; try again")) from None

    log.info("imported vault %s: %d added, %d already present",
             manifest["vault_id"], applied["added"], applied["duplicates"])
    imported_name = vaults.get(local_id)["name"]
    fp = vault_format.fingerprint(publisher["pubkey"])
    _audit_import(request, imported_name, fp, manifest["seq"], applied["added"], applied["duplicates"])
    return {"id": local_id, "name": imported_name, "publisher": fp, **applied}


def _explain_fetch_refusal(msg: str) -> str:
    """netguard's refusals, in the words of a person pasting a URL into a field.

    The two cases a normal user can actually hit get plain language; anything else keeps the
    guard's own message (it names the mechanism, which is what a bug report needs).
    """
    if "non-global" in msg:
        return ("that address is not on the public internet — subscribing works with public "
                "internet hosts only (not localhost or LAN addresses)")
    if "content-type" in msg:
        return "that URL doesn't serve a vault file — point it at the .sbvault file itself"
    return f"could not fetch that vault: {msg}"


@router.post("/api/vaults/subscribe")
def subscribe_vault(request: Request, body: SubscribeIn) -> dict:
    """Subscribe to a PUBLIC (open) vault by URL: fetch, verify, re-seal locally, PIN the publisher.

    First contact IS the trust decision (TOFU, vault-format §5): the publisher key seen now is
    pinned in the new vault's encrypted body, and every later update must verify against that pin —
    never against whatever key a future download claims. Ingress, not egress, so it gates exactly
    like file import (unlock only, no desktop-local): nothing leaves the machine, and everything
    arriving is verified, bounded, re-sealed under this user's master key, and audited.
    """
    vaults, knowledge = _vaults(request), _kb(request)
    # Fragment hygiene BEFORE the URL is fetched, pinned, parsed, or audited: a sealed-share link
    # carries key material in its fragment (#k=...), and nothing downstream may ever see it.
    # (netguard.safe_fetch_vault strips again — belt and suspenders, one rule.)
    url = urldefrag(body.url.strip()).url
    if not url:
        # A whitespace- or fragment-only "URL" survives the model's min_length; refuse it here
        # rather than let netguard's internal assert turn it into a 500.
        raise HTTPException(status_code=400, detail="enter the vault's URL")
    host = urlparse(url).hostname or ""
    try:
        # Both host shapes (§1): a .sbvault URL fetches the zip; a /manifest.json URL walks the
        # unzipped tree (manifest -> index -> every object), each piece verified the same way.
        manifest, docs = vault_sync.fetch_open_vault(url)
    except netguard.FetchError as exc:
        raise HTTPException(status_code=400, detail=_explain_fetch_refusal(str(exc))) from None
    except (vault_format.VaultError, vault_sync.SyncError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    publisher = manifest["publisher"]
    # Resolve the claimed vault_id against every existing pin. Refusing BEFORE anything lands
    # keeps this all-or-nothing.
    existing = _pin_for(vaults, manifest["vault_id"])
    if existing is not None:
        if (existing.get("source") or {}).get("publisher_pubkey") == publisher["pubkey"]:
            raise HTTPException(status_code=409, detail=(
                f"you already have this vault (“{existing['name']}”) — "
                "check it for updates instead of subscribing again"))
        # Same vault_id, different key: either an impersonation of a vault this user already
        # trusts, or a publisher key change — and a key change must NEVER silently succeed (§5).
        raise HTTPException(status_code=409, detail=(
            "a vault with this identity is already pinned to a different publisher — refusing "
            "to add it (if the publisher really changed keys, remove the old vault first)"))

    fp = vault_format.fingerprint(publisher["pubkey"])
    sealed = manifest.get("_sealed") or {}
    publisher_name = sealed.get("name") or ""
    publisher_description = sealed.get("description") or ""
    # Use the publisher's own name/description as the initial local values (the user can rename
    # them later — the local ``name``/``description`` is theirs to edit). The old code overwrote
    # ``description`` with "Public vault · publisher {fp}", clobbering something the publisher
    # deliberately wrote and offering the user nothing to distinguish two similarly-named vaults.
    # The fingerprint the badge needs already rides list responses (see _attach_pinned_fp).
    local_id = vaults.create(
        (publisher_name or "Subscribed vault")[:200],
        publisher_description[:2000],
        kind=IMPORTED,
        # THE pin. Everything a future update is verified against lives here, inside the
        # ciphertext: the pinned key (the identity), seq (rollback floor), and the fetch URL
        # (fragment-stripped above). last_checked is null until check-for-updates exists.
        source={"url": url, "publisher_pubkey": publisher["pubkey"],
                "vault_id": manifest["vault_id"], "seq": manifest["seq"],
                "mode": vault_format.OPEN,
                "added_at": datetime.now(timezone.utc).date().isoformat(),
                "last_checked": None},
    )
    # Record the publisher's own name/description in the body BEFORE apply, so a mid-apply
    # rollback throws it out with the rest — the meta and the docs live and die together.
    vaults.note_publisher_meta(local_id, name=publisher_name, description=publisher_description)
    try:
        applied = _apply_docs(request, vaults, knowledge, local_id, manifest, docs)
    except Exception:
        _rollback_import(vaults, knowledge, local_id)
        # Traceback for the bug report; vault_id + host only — never the URL (its path names the topic).
        log.exception("subscribe failed applying vault %s (host %s); rolled back",
                      manifest["vault_id"], host)
        raise HTTPException(status_code=500, detail=(
            "subscribe failed part-way — nothing was kept; try again")) from None

    log.info("subscribed to vault %s (host %s): %d added, %d already present",
             manifest["vault_id"], host, applied["added"], applied["duplicates"])
    name = vaults.get(local_id)["name"]
    _audit_import(request, name, fp, manifest["seq"], applied["added"], applied["duplicates"],
                  tool="vault_subscribe", host=host)
    return {"id": local_id, "name": name, "publisher": fp, "url_host": host, **applied}


# --- check for updates / apply / trust a changed key ----------------------------------------------

class TrustIn(BaseModel):
    """Re-auth + the EXACT key being blessed. Echoing the offered pubkey back is what stops a
    racing rotation from being trusted blind: the server compares it to the blocked record, so a
    key that changed again since the user confirmed out-of-band is refused, never re-pinned."""

    passphrase: str | None = None
    recovery_key: str | None = None
    offered_pubkey: str = Field(min_length=1, max_length=100)


def _subscription(request: Request, vault_id: str) -> tuple:
    """(vaults, vault, pin) for a URL subscription — 400 for anything else (all three routes)."""
    vaults = _vaults(request)
    vault = _require(vaults, vault_id)
    pin = vault.get("source") or {}
    if not pin.get("url") or not pin.get("publisher_pubkey") or not pin.get("vault_id"):
        raise HTTPException(status_code=400, detail=(
            "this vault is not a URL subscription — there is nothing to check"))
    return vaults, vault, pin


def _key_change_detail(pinned_pubkey: str, offered_pubkey: str) -> str:
    """The one interruption the design allows itself — so it must say everything: BOTH identities,
    side by side, and what unblocks it."""
    return (f"the publisher's key CHANGED — pinned {vault_format.fingerprint(pinned_pubkey)}, "
            f"offered {vault_format.fingerprint(offered_pubkey)}. Updates are blocked until you "
            "confirm the new key with the publisher out-of-band and choose Trust new key")


def _refuse_if_blocked(pin: dict) -> None:
    """While a key change is pending, check/update short-circuit to the SAME 409 — no fetch, no
    re-verify: nothing a hostile host serves may move the pin, and there is nothing new to learn
    until the human decides."""
    offered = (pin.get("blocked") or {}).get("offered_pubkey")
    if offered:
        raise HTTPException(status_code=409,
                            detail=_key_change_detail(pin["publisher_pubkey"], offered))


def _block_key_change(vaults, vault_id: str, pin: dict, offered_pubkey: str) -> HTTPException:
    """Record the offered key in the pin (read-modify-write) and build the 409 to raise."""
    vaults.update_source(vault_id, {"blocked": {"offered_pubkey": offered_pubkey}})
    return HTTPException(status_code=409,
                         detail=_key_change_detail(pin["publisher_pubkey"], offered_pubkey))


def _checked(vaults, vault_id: str, pin: dict) -> dict:
    """Run vault_sync.check for a route, mapping every failure to its HTTP shape."""
    try:
        return vault_sync.check(pin)
    except vault_sync.KeyChanged as exc:
        raise _block_key_change(vaults, vault_id, pin, exc.offered_pubkey) from None
    except netguard.FetchError as exc:
        raise HTTPException(status_code=400, detail=_explain_fetch_refusal(str(exc))) from None
    except (vault_format.VaultError, vault_sync.SyncError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/api/vaults/{vault_id}/check-updates")
def check_updates(request: Request, vault_id: str) -> dict:
    """Ask the pinned URL whether a newer version exists. Writes NOTHING except last_checked
    (and the blocked marker, when the answer is a key change).

    Response includes ``retired`` (the remote manifest carries the retire marker) and ``kind``, a
    one-word disposition for the UI: ``update`` | ``up-to-date`` | ``rollback`` | ``retired``.
    ``retired`` short-circuits over ``update`` because applying a retire-export leaves the vault
    in the ``retired`` state — a UI must not say "click Update" and mean "stop following this".
    """
    vaults, _vault, pin = _subscription(request, vault_id)
    _refuse_if_blocked(pin)
    chk = _checked(vaults, vault_id, pin)
    # A successful check to the host clears the failure counter (the host IS up). The retired-
    # flag itself is only PERSISTED on apply — check leaves it to the caller to decide.
    vaults.update_source(vault_id,
                         {**vault_sync._clear_failure_state(),
                          "last_checked": vault_sync._now_iso()})
    if chk["retired"] and chk["behind"]:
        kind = "retired"
    elif chk["rollback"]:
        kind = "rollback"
    elif chk["behind"]:
        kind = "update"
    else:
        kind = "up-to-date"
    return {"behind": chk["behind"], "remote_seq": chk["remote_seq"],
            "seq": chk["pinned_seq"], "rollback": chk["rollback"],
            "retired": chk["retired"], "kind": kind}


@router.post("/api/vaults/{vault_id}/update")
def update_vault_from_source(request: Request, vault_id: str) -> dict:
    """Check and, when a newer version exists, APPLY it — all-or-nothing (vault-format §5).

    Response gains ``retired`` (the applied update was a retire-export — the subscription is now
    in the retired state and no longer auto-checked) and ``renamed_from`` (the publisher's
    previous name, when the update carried a rename — else null); everything else is unchanged.
    """
    vaults, vault, pin = _subscription(request, vault_id)
    _refuse_if_blocked(pin)
    knowledge = _kb(request)
    chk = _checked(vaults, vault_id, pin)
    # A successful check clears the failure counter (the host IS up). last_checked advances on
    # every verified answer (update, up-to-date, even a refused rollback).
    vaults.update_source(vault_id,
                         {**vault_sync._clear_failure_state(),
                          "last_checked": vault_sync._now_iso()})
    if chk["rollback"]:
        raise HTTPException(status_code=409, detail=(
            f"the host is serving an OLDER version (v{chk['remote_seq']}) than you already have "
            f"(v{chk['pinned_seq']}) — refusing to roll back"))
    if not chk["behind"]:
        return {"added": 0, "updated": 0, "deleted": 0, "kept_yours": 0,
                "seq": chk["pinned_seq"], "retired": False, "renamed_from": None}
    host = urlparse(pin["url"]).hostname or ""
    try:
        result = vault_sync.apply(vaults, knowledge, vault_id, pin, chk,
                                  gateway.embed_model(request.app.state.dbx))
    except netguard.FetchError as exc:
        raise HTTPException(status_code=400, detail=_explain_fetch_refusal(str(exc))) from None
    except (vault_format.VaultError, vault_sync.SyncError) as exc:
        # A tampered/malformed piece surfaced AFTER the manifest verified: nothing was applied
        # (verification precedes the first write, and the write phase rolls back whole).
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        log.exception("update failed applying vault %s (host %s); rolled back",
                      pin["vault_id"], host)
        raise HTTPException(status_code=500, detail=(
            "update failed part-way — nothing was changed; try again")) from None
    log.info("updated vault %s (host %s) to seq %d: %s",
             pin["vault_id"], host, chk["remote_seq"], result)
    _audit_update(request, vault["name"], vault_format.fingerprint(pin["publisher_pubkey"]),
                  chk["remote_seq"], result, host=host)
    return {**result, "seq": chk["remote_seq"]}


@router.post("/api/vaults/{vault_id}/trust-publisher")
def trust_publisher(request: Request, vault_id: str, body: TrustIn) -> dict:
    """Re-pin a subscription to the NEW key the user confirmed out-of-band; clear the block.

    Trusting a new publisher key is the single most consequential act in the vault system — it
    hands every future update to whoever holds that key — so it gates exactly like export
    (Desktop-local + passphrase re-entry), and the body must name the exact key it blesses. The
    seq floor deliberately survives the re-pin: a new key is not a license to roll back.
    """
    _require_desktop_local(request)
    _reauthorize(request, body)
    vaults, vault, pin = _subscription(request, vault_id)
    offered = (pin.get("blocked") or {}).get("offered_pubkey")
    if not offered:
        raise HTTPException(status_code=409, detail=(
            "there is no pending key change on this vault — check for updates first"))
    if body.offered_pubkey != offered:
        # The host rotated AGAIN after the user confirmed: what they verified out-of-band is not
        # what would be pinned. Refuse — a stale confirmation must never bless a newer stranger.
        raise HTTPException(status_code=409, detail=(
            "the offered key has changed since you confirmed it — check for updates and confirm "
            "the new fingerprint with the publisher again"))
    vaults.update_source(vault_id, {"publisher_pubkey": offered, "blocked": None})
    fp = vault_format.fingerprint(offered)
    request.app.state.audit.append(
        "user", "vault_trust_publisher", "reviewed", "executed", True,
        args_summary=tools.summarize({"vault": vault["name"], "publisher": fp}),
        result_summary=tools.summarize({"repinned": True}),
    )
    log.info("re-pinned vault %s to a new publisher key", pin["vault_id"])
    return {"ok": True, "pinned_fingerprint": fp}


# --- scheduled auto-update (opt-in) ---------------------------------------------------------------

class SubscriptionIn(BaseModel):
    """Auto-update preferences for a URL subscription. Both optional: a PATCH sets only what it
    names (read-modify-write through update_source), so toggling auto-update never disturbs the
    interval and vice versa. ``check_interval_seconds`` is bounded on input and floored to 1h on
    apply — a background timer must never be able to hammer a host."""

    auto_update: bool | None = None
    check_interval_seconds: int | None = Field(default=None, ge=0, le=31_536_000)


@router.patch("/api/vaults/{vault_id}/subscription")
def set_subscription(request: Request, vault_id: str, body: SubscriptionIn) -> dict:
    """Set opt-in scheduled auto-update on a URL subscription (default OFF). Unlock-gated; 400 on a
    vault that isn't a URL subscription. The interval is clamped to a 1h floor.

    Auto-update runs only on the Desktop, only while unlocked, and NEVER applies a publisher key
    change on its own (the background pass blocks and reports it instead — see vault_sync.tick).
    """
    vaults, _vault, _pin = _subscription(request, vault_id)  # 400 unless a URL subscription
    changes: dict = {}
    if body.auto_update is not None:
        changes["auto_update"] = bool(body.auto_update)
    if body.check_interval_seconds is not None:
        changes["check_interval_seconds"] = max(
            int(body.check_interval_seconds), vault_sync._MIN_CHECK_INTERVAL_SECONDS)
    if changes:
        vaults.update_source(vault_id, changes)
    updated = _require(vaults, vault_id)
    _attach_pinned_fp([updated])
    return updated
