"""Self-improving framework HTTP API (requires unlock).

Phase 2 exposes only the master kill-switch + review status — enough to turn the
reviewer on and see that it runs. The full settings surface (per-category toggles,
proposal review) arrives with the later phases.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import selfreview

router = APIRouter()


def _require_unlocked(request: Request) -> None:
    """Raise 423 unless the app has been unlocked (secret store loaded)."""
    if getattr(request.app.state, "secret_store", None) is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")


class SelfImproveIn(BaseModel):
    enabled: bool


@router.get("/api/selfimprove")
def get_selfimprove(request: Request) -> dict:
    """Kill-switch state + last review cadence stamp (reads are plaintext meta)."""
    _require_unlocked(request)
    conn = request.app.state.dbx
    return {"enabled": selfreview.enabled(conn), "last_run": selfreview.last_run(conn)}


@router.put("/api/selfimprove")
def put_selfimprove(request: Request, body: SelfImproveIn) -> dict:
    """Flip the master kill-switch. Off is the default; absent/corrupt config reads as off."""
    _require_unlocked(request)
    conn = request.app.state.dbx
    selfreview.set_enabled(conn, body.enabled)
    return {"enabled": selfreview.enabled(conn)}


@router.get("/api/selfimprove/improvements")
def list_improvements(request: Request) -> dict:
    """The change ledger: every improvement the reviewer proposed, applied, or reverted.

    An autonomous-within-bounds system owes the user an inspectable record — this is it
    (payload/body stay summarized; the applied fact itself is visible in Settings ->
    Memory under its "(learned) " prefix).
    """
    _require_unlocked(request)
    from .improvements import ImprovementStore

    store = ImprovementStore(request.app.state.dbx, request.app.state.master_key)
    return {"improvements": [
        {"id": r["id"], "created_at": r["created_at"], "category": r["category"],
         "component": r["component"], "lever_type": r["lever_type"], "status": r["status"],
         "confidence": r["confidence"], "description": r["description"],
         "applied_at": r["applied_at"], "reverted_at": r["reverted_at"]}
        for r in store.list(limit=50)
    ]}
