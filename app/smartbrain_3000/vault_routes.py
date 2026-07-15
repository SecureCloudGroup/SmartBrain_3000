"""Vaults HTTP API: create a named subset of the knowledge base, and scope a search to it.

A vault is the unit you collect documents into, search within, and (next) export and share. This is
the collection primitive only — the portable ``.sbvault`` artifact is built on top of it.

Deleting a vault never deletes its documents: the same document may sit in other vaults, and
"remove this grouping" is not "shred my files".
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import gateway, kb as kbmod, vault_format
from .data_routes import _reauthorize, _require_desktop_local
from .vaults import IMPORTED

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
    # is plaintext-equivalent to whoever holds the key.
    passphrase: str | None = None
    recovery_key: str | None = None
    include_vectors: bool = True


class ImportIn(BaseModel):
    key: str = Field(min_length=1)  # the SBVK1-... vault key


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


@router.get("/api/vaults")
def list_vaults(request: Request) -> dict:
    """All vaults, with how many documents each holds."""
    return {"vaults": _vaults(request).list_vaults()}


@router.post("/api/vaults")
def create_vault(request: Request, body: VaultIn) -> dict:
    """Create an empty vault."""
    store = _vaults(request)
    vault_id = store.create(body.name.strip(), body.description.strip())
    return store.get(vault_id)


@router.get("/api/vaults/{vault_id}")
def get_vault(request: Request, vault_id: str) -> dict:
    """One vault, plus the ids of the documents in it."""
    store = _vaults(request)
    vault = _require(store, vault_id)
    return {**vault, "doc_ids": store.document_ids(vault_id)}


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
    """Export a vault as a SEALED .sbvault file. Returns the file; the KEY is fetched separately.

    Desktop-local AND re-authenticated, exactly like /api/backup: whoever holds the file and its key
    holds the plaintext, so this is a sensitive egress. (Reusing data_routes' helpers verbatim —
    "blocks a passer-by at an unattended-but-unlocked Desktop and a stale paired session from
    silently exfiltrating everything in one click".)
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

    seq = vaults.bump_version(vault_id)  # a publish IS a version
    key = vault_format.new_vault_key()
    vaults.remember_key(vault_id, key)  # so the user can re-show it without re-exporting
    try:
        # No `label`: the publisher label sits in the PLAINTEXT manifest, and a vault's name ("Divorce
        # filings", "Acme acquisition") can reveal as much as its contents. The real name travels in
        # the ENCRYPTED index (pack's `name=`) and the importer restores it from there.
        blob = vault_format.pack(
            store=secrets, vault_id=vault_id, name=vault["name"],
            description=vault["description"], seq=seq, docs=docs, vault_key=key,
            embed_model=embed_model,
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
    _require(vaults, vault_id)
    key = vaults.get_key(vault_id)
    if key is None:
        raise HTTPException(status_code=409, detail="export this vault first — it has no key yet")
    return {"key": vault_format.encode_vault_key(key)}


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

    embed_model = gateway.embed_model(request.app.state.dbx)
    shipped = manifest.get("embeddings") or {}
    added = duplicates = 0
    for doc in docs:  # bounded by vault_format.MAX_VAULT_DOCS
        existing = knowledge.find_duplicate(doc["content"])
        if existing is not None:
            # The user already has this text. Keep THEIR document and just note the membership —
            # never overwrite something they authored with a stranger's copy.
            vaults.add_documents(local_id, [existing], origin="owner")
            duplicates += 1
            continue
        doc_id = knowledge.add(doc["title"], doc["content"], doc["meta"])
        vaults.add_documents(local_id, [doc_id], origin="import")
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

    log.info("imported vault %s: %d added, %d already present", manifest["vault_id"], added, duplicates)
    return {
        "id": local_id,
        "name": vaults.get(local_id)["name"],
        "publisher": vault_format.fingerprint(publisher["pubkey"]),
        "added": added,
        "duplicates": duplicates,
        "vectors_used": bool(shipped.get("model") == embed_model),
    }
