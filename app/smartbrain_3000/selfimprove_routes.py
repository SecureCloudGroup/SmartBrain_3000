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
