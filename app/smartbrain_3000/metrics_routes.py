"""Feedback + per-turn metrics HTTP API (requires unlock).

``POST /api/feedback`` records an implicit-dissatisfaction event (the user stopped a
stream or regenerated a reply). ``GET /api/metrics/summary`` returns the plaintext
speed/quality aggregate the self-improving reviewer scores on. Both are content-free.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import metrics

router = APIRouter()


def _require_unlocked(request: Request) -> None:
    """Raise 423 unless the app has been unlocked (secret store loaded)."""
    if getattr(request.app.state, "secret_store", None) is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")


class FeedbackIn(BaseModel):
    kind: str
    conversation_id: str | None = None
    message_id: str | None = None


@router.post("/api/feedback")
def post_feedback(request: Request, body: FeedbackIn) -> dict:
    """Record a 'stop' or 'regenerate' event (best-effort telemetry)."""
    _require_unlocked(request)
    if body.kind not in metrics.FEEDBACK_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {metrics.FEEDBACK_KINDS}")
    metrics.record_feedback(
        request.app.state.dbx, kind=body.kind,
        conversation_id=body.conversation_id, message_id=body.message_id,
    )
    return {"ok": True}


@router.get("/api/metrics/summary")
def get_metrics_summary(request: Request, since: str | None = None, until: str | None = None) -> dict:
    """Speed/quality aggregate over a time window (turns, latency, degraded/stop rates)."""
    _require_unlocked(request)
    return metrics.summary(request.app.state.dbx, since, until)
