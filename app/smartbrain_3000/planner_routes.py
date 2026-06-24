"""Planner HTTP API (requires unlock).

Tasks are encrypted at rest; status + due_date are plaintext. Today/Week
grouping is done client-side (timezone-aware) over the returned due dates.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .planner import Planner

router = APIRouter()

_DATE = r"^\d{4}-\d{2}-\d{2}$"
_TIME = r"^\d{2}:\d{2}$"


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=8000)
    due_date: str | None = Field(default=None, pattern=_DATE)
    due_time: str | None = Field(default=None, pattern=_TIME)
    priority: str = Field(default="medium", pattern="^(low|medium|high)$")
    recur: str = Field(default="none", pattern="^(none|daily|weekly)$")
    tags: list[str] = Field(default_factory=list, max_length=20)


class StatusIn(BaseModel):
    status: str = Field(pattern="^(open|done)$")


def _planner(request: Request) -> Planner:
    """Return the unlocked Planner, or raise 423 if locked."""
    planner = getattr(request.app.state, "planner", None)
    if planner is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return planner


@router.get("/api/tasks")
def list_tasks(request: Request) -> dict:
    """List all tasks (open first, by due date)."""
    return {"tasks": _planner(request).list_tasks()}


@router.post("/api/tasks")
def add_task(request: Request, body: TaskIn) -> dict[str, str]:
    """Create a task; return its id."""
    tid = _planner(request).add_task(
        body.title.strip(), body.notes, body.due_date,
        due_time=body.due_time, priority=body.priority, recur=body.recur, tags=body.tags,
    )
    return {"id": tid}


@router.put("/api/tasks/{tid}")
def update_task(request: Request, tid: str, body: TaskIn) -> dict[str, bool]:
    """Replace a task's editable fields (title / notes / due / priority / recur / tags)."""
    planner = _planner(request)
    if planner.get_task(tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner.update_task(
        tid, body.title.strip(), body.notes, body.due_date,
        due_time=body.due_time, priority=body.priority, recur=body.recur, tags=body.tags,
    )
    return {"ok": True}


@router.patch("/api/tasks/{tid}")
def set_status(request: Request, tid: str, body: StatusIn) -> dict[str, bool]:
    """Mark a task open or done."""
    planner = _planner(request)
    if planner.get_task(tid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    planner.set_status(tid, body.status)
    return {"ok": True}


@router.delete("/api/tasks/{tid}")
def delete_task(request: Request, tid: str) -> dict[str, bool]:
    """Delete a task."""
    _planner(request).delete_task(tid)
    return {"ok": True}
