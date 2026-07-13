"""Vaults HTTP API: create a named subset of the knowledge base, and scope a search to it.

A vault is the unit you collect documents into, search within, and (next) export and share. This is
the collection primitive only — the portable ``.sbvault`` artifact is built on top of it.

Deleting a vault never deletes its documents: the same document may sit in other vaults, and
"remove this grouping" is not "shred my files".
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

_MAX_IDS_PER_CALL = 1000  # bounded membership edit (P10 #2)


class VaultIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class DocIdsIn(BaseModel):
    doc_ids: list[str] = Field(default_factory=list)


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
