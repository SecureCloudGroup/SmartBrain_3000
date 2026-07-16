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

from . import gateway, identity, kb as kbmod, netguard, tools, vault_format
from .data_routes import _reauthorize, _require_desktop_local
from .vaults import IMPORT, IMPORTED

router = APIRouter()
log = logging.getLogger(__name__)

_MAX_IDS_PER_CALL = 1000  # bounded membership edit (P10 #2)


class VaultIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


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
        pubkey = (vault.get("source") or {}).get("publisher_pubkey")
        if isinstance(pubkey, str) and pubkey:
            vault["pinned_fingerprint"] = vault_format.fingerprint(pubkey)


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
    vault_id = store.create(body.name.strip(), body.description.strip())
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
    """Rename / re-describe a vault."""
    store = _vaults(request)
    _require(store, vault_id)
    store.update(vault_id, body.name.strip(), body.description.strip())
    return store.get(vault_id)


@router.delete("/api/vaults/{vault_id}")
def delete_vault(request: Request, vault_id: str) -> dict[str, bool]:
    """Delete the vault. Its DOCUMENTS are left alone — this removes a grouping, not your files."""
    store = _vaults(request)
    _require(store, vault_id)
    store.delete(vault_id)
    return {"ok": True}


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


@router.post("/api/vaults/{vault_id}/export")
def export_vault(request: Request, vault_id: str, body: ExportIn) -> Response:
    """Export a vault as a .sbvault file — SEALED (default; the KEY is fetched separately) or OPEN.

    Desktop-local AND re-authenticated in BOTH modes, exactly like /api/backup: a sealed file plus
    its key is plaintext-equivalent, and an open file IS the decrypted plaintext — the most
    sensitive egress in the app. (Reusing data_routes' helpers verbatim — "blocks a passer-by at an
    unattended-but-unlocked Desktop and a stale paired session from silently exfiltrating
    everything in one click".)
    """
    _require_desktop_local(request)
    _reauthorize(request, body)
    vaults, knowledge, secrets = _vaults(request), _kb(request), _secrets(request)
    vault = _require(vaults, vault_id)

    embed_model = gateway.embed_model(request.app.state.dbx)
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
        if body.include_vectors:
            vectors = knowledge.vectors_for(doc_id, embed_model)
            if vectors:
                entry["vectors"] = vectors  # so the recipient can search it the moment it lands
        docs.append(entry)

    seq = vaults.bump_version(vault_id)  # a publish IS a version (both modes share the counter)
    if body.mode == vault_format.OPEN:
        # Public == "there is no Vault Key": nothing is minted, nothing is remembered. The FIRST
        # open publish fixes K_name for the life of the vault (decision #6): seeded from the stored
        # Vault Key when one exists — the exact K_name every sealed export derived, so a
        # sealed->open flip keeps every uid/hash/object name and subscribers see an in-place mode
        # change — otherwise minted fresh for a born-open vault.
        name_key = vaults.get_name_key(vault_id)
        if name_key is None:
            stored = vaults.get_key(vault_id)
            name_key = (vault_format.derive_name_key(stored, vault_id)
                        if stored is not None else os.urandom(32))
        # Persist BEFORE packing: a file shipped under an unrecorded K_name would make the next
        # republish rename every object — a full re-download for every tree-host subscriber. The
        # inverse failure (recorded, never shipped) is harmless: the next export just reuses it.
        vaults.note_open_publish(vault_id, name_key)
        pack_args: dict = {"mode": vault_format.OPEN, "name_key": name_key}
    else:
        key = vault_format.new_vault_key()
        vaults.remember_key(vault_id, key)  # so the user can re-show it without re-exporting
        pack_args = {"vault_key": key}
    try:
        # No `label`: the publisher label sits in the PLAINTEXT manifest, and a vault's name ("Divorce
        # filings", "Acme acquisition") can reveal as much as its contents. Sealed keeps the real name
        # in the ENCRYPTED index; open publishes it in the manifest — the topic is public anyway.
        blob = vault_format.pack(
            store=secrets, vault_id=vault_id, name=vault["name"],
            description=vault["description"], seq=seq, docs=docs,
            embed_model=embed_model, **pack_args,
        )
    except vault_format.VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    safe = "".join(c for c in vault["name"] if c.isalnum() or c in " -_")[:60].strip() or "vault"
    return Response(
        content=blob,
        media_type="application/octet-stream",
        headers={"content-disposition": f'attachment; filename="{safe}.sbvault"'},
    )


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
    embed_model = gateway.embed_model(request.app.state.dbx)
    shipped = manifest.get("embeddings") or {}
    added = duplicates = 0
    for doc in docs:  # bounded by vault_format.MAX_VAULT_DOCS
        existing = knowledge.find_duplicate(doc["content"])
        if existing is not None:
            # The user already has this text. Keep THEIR document and just note the membership —
            # never overwrite something they authored with a stranger's copy.
            vaults.add_documents(local_id, [existing], origin="owner")
            vaults.note_member_source(local_id, existing, doc["uid"], doc["hash"])
            duplicates += 1
            continue
        doc_id = knowledge.add(doc["title"], doc["content"], doc["meta"])
        vaults.add_documents(local_id, [doc_id], origin="import")
        vaults.note_member_source(local_id, doc_id, doc["uid"], doc["hash"])
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


@router.post("/api/vaults/import")
async def import_vault(request: Request, key: str) -> dict:
    """Import a .sbvault (raw body) with its SBVK1- key. Verifies, decrypts, and RE-SEALS locally.

    Imported documents are re-sealed under THIS user's master key with fresh local ids: the GCM tag
    is bound to the doc_id, so there is no such thing as importing a ciphertext — and a malicious
    vault naming a document with an id that already exists locally could otherwise clobber it.
    Minting locally makes that attack structurally impossible.
    """
    vaults, knowledge = _vaults(request), _kb(request)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    try:
        vault_key = vault_format.decode_vault_key(key)
        manifest, docs = vault_format.open_vault(data, vault_key)
    except vault_format.VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    publisher = manifest["publisher"]
    # The vault's real name comes from the ENCRYPTED index (surfaced by open_vault as _sealed) — the
    # plaintext manifest deliberately carries no topic, so a host never learns what a vault is about.
    sealed = manifest.get("_sealed") or {}
    local_id = vaults.create(
        (sealed.get("name") or "Imported vault")[:200],
        f"Imported vault · publisher {vault_format.fingerprint(publisher['pubkey'])}",
        kind=IMPORTED,
        source={"vault_id": manifest["vault_id"], "publisher_pubkey": publisher["pubkey"],
                "seq": manifest["seq"]},
    )

    applied = _apply_docs(request, vaults, knowledge, local_id, manifest, docs)

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
        data = netguard.safe_fetch_vault(url)
    except netguard.FetchError as exc:
        raise HTTPException(status_code=400, detail=_explain_fetch_refusal(str(exc))) from None

    try:
        if vault_format.read_manifest(data)["mode"] != vault_format.OPEN:
            # v1 URL-subscribe is open-only (the plan's explicit deferral): a sealed vault's key
            # must never ride a URL, so file + separately-sent key stays the sealed channel.
            raise HTTPException(status_code=400, detail=(
                "that vault is sealed — import its file with the key instead"))
        manifest, docs = vault_format.open_vault(data)
    except vault_format.VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    publisher = manifest["publisher"]
    # Resolve the claimed vault_id against every existing pin (bounded decrypt-scan: list_vaults
    # is capped at vaults._MAX_VAULTS). Refusing BEFORE anything lands keeps this all-or-nothing.
    for vault in vaults.list_vaults():
        pin = vault.get("source") or {}
        if pin.get("vault_id") != manifest["vault_id"]:
            continue
        if pin.get("publisher_pubkey") == publisher["pubkey"]:
            raise HTTPException(status_code=409, detail=(
                f"you already have this vault (“{vault['name']}”) — "
                "check it for updates instead of subscribing again"))
        # Same vault_id, different key: either an impersonation of a vault this user already
        # trusts, or a publisher key change — and a key change must NEVER silently succeed (§5).
        raise HTTPException(status_code=409, detail=(
            "a vault with this identity is already pinned to a different publisher — refusing "
            "to add it (if the publisher really changed keys, remove the old vault first)"))

    fp = vault_format.fingerprint(publisher["pubkey"])
    sealed = manifest.get("_sealed") or {}
    local_id = vaults.create(
        (sealed.get("name") or "Subscribed vault")[:200],
        f"Public vault · publisher {fp}",
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
    try:
        applied = _apply_docs(request, vaults, knowledge, local_id, manifest, docs)
    except Exception:
        # All-or-nothing, for real: the vault row + pin land BEFORE the documents, so a mid-apply
        # failure (KB write, embed, disk) would otherwise strand a partial vault whose pin makes
        # every RETRY hit the duplicate-409 above — self-blocking until a manual delete. Roll back
        # everything this call minted: the vault row and its memberships, then the import-origin
        # documents (a deduped member is the USER's own doc, origin owner — kept).
        minted = [m["id"] for m in vaults.members(local_id) if m["origin"] == IMPORT]
        vaults.delete(local_id)
        for doc_id in minted:  # bounded by _MAX_DOCS_PER_VAULT
            knowledge.delete(doc_id)
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
