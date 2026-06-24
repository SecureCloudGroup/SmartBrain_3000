"""Scheduler HTTP API (requires unlock).

Schedules are encrypted at rest (title/prompt/model); cadence metadata is
plaintext. A schedule fires an agent turn — OBSERVE tools auto-complete,
dangerous ones park as approval tiles. The background runner fires due
schedules on a timer (only while unlocked); ``/run`` fires one synchronously.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import scheduler, tools

router = APIRouter()

_MAX_INTERVAL = 525600  # one year in minutes (verifiable upper bound)


class ScheduleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    interval_minutes: int = Field(default=0, ge=0, le=_MAX_INTERVAL)
    start_in_minutes: int = Field(default=0, ge=0, le=_MAX_INTERVAL)
    model: str | None = Field(default=None, max_length=200)


class EnabledIn(BaseModel):
    enabled: bool


def _store(request: Request) -> scheduler.ScheduleStore:
    """Return the unlocked ScheduleStore, or raise 423 if locked."""
    store = getattr(request.app.state, "schedules", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


@router.get("/api/schedules")
def list_schedules(request: Request) -> dict:
    """List all schedules (soonest first)."""
    return {"schedules": _store(request).list_schedules()}


@router.post("/api/schedules")
def add_schedule(request: Request, body: ScheduleIn) -> dict[str, str]:
    """Create a schedule; return its id."""
    sid = _store(request).add_schedule(
        body.title.strip(), body.prompt, body.interval_minutes, body.start_in_minutes, body.model
    )
    return {"id": sid}


@router.put("/api/schedules/{sid}")
def update_schedule(request: Request, sid: str, body: ScheduleIn) -> dict[str, bool]:
    """Replace a schedule's content + interval."""
    store = _store(request)
    if store.get_schedule(sid) is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    store.update_schedule(sid, body.title.strip(), body.prompt, body.interval_minutes, body.model)
    return {"ok": True}


@router.patch("/api/schedules/{sid}")
def set_enabled(request: Request, sid: str, body: EnabledIn) -> dict[str, bool]:
    """Enable or disable a schedule."""
    store = _store(request)
    if store.get_schedule(sid) is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    store.set_enabled(sid, body.enabled)
    return {"ok": True}


@router.delete("/api/schedules/{sid}")
def delete_schedule(request: Request, sid: str) -> dict[str, bool]:
    """Delete a schedule."""
    _store(request).delete_schedule(sid)
    return {"ok": True}


@router.post("/api/schedules/{sid}/run")
def run_now(request: Request, sid: str) -> dict:
    """Fire a schedule immediately (synchronous); return the agent result."""
    store = _store(request)
    schedule = store.get_schedule(sid)
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    state = request.app.state
    # email= must match the timer path (scheduler._run_one), or a "Run now" on a schedule
    # that sends email fails with "no email account connected" while the timer works.
    ctx = tools.ToolContext(
        kb=state.kb, planner=state.planner, memory=state.memory,
        email=getattr(state, "email", None),
    )
    return scheduler.run_schedule(ctx, state.audit, state.approvals, store, schedule)


@router.get("/api/schedules/{sid}/runs")
def list_runs(request: Request, sid: str) -> dict:
    """Return recent run results for a schedule (newest first) so output is readable."""
    store = _store(request)
    if store.get_schedule(sid) is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"runs": store.list_runs(sid)}
