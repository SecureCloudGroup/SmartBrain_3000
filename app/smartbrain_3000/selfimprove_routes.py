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
    # Both optional: PUT can change JUST enabled, JUST interval, or both — and
    # setting one must never flip the other (see set_interval_hours' docstring).
    enabled: bool | None = None
    interval_hours: int | None = None


@router.get("/api/selfimprove")
def get_selfimprove(request: Request) -> dict:
    """Kill-switch state + configured cadence + last review stamp (plaintext meta)."""
    _require_unlocked(request)
    conn = request.app.state.dbx
    return {"enabled": selfreview.enabled(conn),
            "interval_hours": selfreview.interval_hours(conn),
            "last_run": selfreview.last_run(conn)}


@router.put("/api/selfimprove")
def put_selfimprove(request: Request, body: SelfImproveIn) -> dict:
    """Update kill-switch and/or cadence. Off is the default; absent/corrupt config reads
    as off. Cadence outside the allowed set → 422 with the allowed values in the message."""
    _require_unlocked(request)
    conn = request.app.state.dbx
    if body.interval_hours is not None:
        try:
            selfreview.set_interval_hours(conn, body.interval_hours)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.enabled is not None:
        selfreview.set_enabled(conn, body.enabled)
    return {"enabled": selfreview.enabled(conn),
            "interval_hours": selfreview.interval_hours(conn),
            "last_run": selfreview.last_run(conn)}


class OptimizerIn(BaseModel):
    enabled: bool


@router.get("/api/selfimprove/optimizer")
def get_optimizer(request: Request) -> dict:
    """Optimizer state: kill-switch + every learned strategy (all shadow in this phase)."""
    _require_unlocked(request)
    from . import optimizer

    conn = request.app.state.dbx
    strategies = optimizer.StrategyStore(conn, request.app.state.master_key).list()
    return {"enabled": optimizer.enabled(conn),
            "strategies": [{"id": s["id"], "request_type": s["request_type"],
                            "status": s["status"], "fired": s["fired"],
                            "directive": s["directive"], "created_at": s["created_at"]}
                           for s in strategies]}


@router.put("/api/selfimprove/optimizer")
def put_optimizer(request: Request, body: OptimizerIn) -> dict:
    """Flip the optimizer kill-switch (default off; absent/corrupt reads as off)."""
    _require_unlocked(request)
    from . import optimizer

    conn = request.app.state.dbx
    optimizer.set_enabled(conn, body.enabled)
    return {"enabled": optimizer.enabled(conn)}


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
