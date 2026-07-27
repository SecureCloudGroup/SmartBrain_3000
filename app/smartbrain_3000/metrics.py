"""Per-turn speed & quality telemetry (self-improving framework, Phase 1).

Records content-free, plaintext metadata about how well and how fast a turn went —
the signals the app never captured before: latency, time-to-first-token, step count,
and the degraded / step-budget-exhausted outcomes, plus a per-turn token total (the
usage_log records tokens but has no turn linkage). No message content is stored, so
rows are readable without the master key and the reviewer does its rate/latency math
directly in SQL.

Recording is best-effort, exactly like usage recording: telemetry must never break a
chat or agent turn, so every write swallows its own failure.
"""

from __future__ import annotations

import logging
import uuid

log = logging.getLogger(__name__)

FEEDBACK_KINDS = ("stop", "regenerate")


def record_turn(
    conn,
    *,
    model: str,
    is_local: bool,
    duration_ms: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    conversation_id: str | None = None,
    ttft_ms: int | None = None,
    steps: int | None = None,
    degraded: bool = False,
    hit_max_steps: bool = False,
    outcome: str = "",
) -> None:
    """Insert one turn-metric row. Best-effort — never raises into a turn."""
    if conn is None or not model:
        return
    try:
        conn.execute(
            "INSERT INTO turn_metrics (id, conversation_id, model, is_local, duration_ms, ttft_ms,"
            " steps, prompt_tokens, completion_tokens, degraded, hit_max_steps, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            [
                uuid.uuid4().hex,
                conversation_id,
                model,
                bool(is_local),
                max(0, int(duration_ms)),
                None if ttft_ms is None else max(0, int(ttft_ms)),
                None if steps is None else int(steps),
                max(0, int(prompt_tokens)),
                max(0, int(completion_tokens)),
                bool(degraded),
                bool(hit_max_steps),
                str(outcome or ""),
            ],
        )
    except Exception as exc:  # never fail a turn over telemetry
        log.debug("turn metric skipped: %s", exc)


def record_feedback(conn, *, kind: str, conversation_id: str | None = None, message_id: str | None = None) -> None:
    """Insert one implicit-feedback row (kind = 'stop' | 'regenerate'). Best-effort."""
    if conn is None or kind not in FEEDBACK_KINDS:
        return
    try:
        conn.execute(
            "INSERT INTO feedback_events (id, conversation_id, message_id, kind) VALUES (?, ?, ?, ?);",
            [uuid.uuid4().hex, conversation_id, message_id, kind],
        )
    except Exception as exc:  # never fail a request over telemetry
        log.debug("feedback event skipped: %s", exc)


class _TokenTally:
    """Wraps a turn's usage_sink to also accumulate per-turn token totals.

    ``run_turn`` calls the sink once per model round-trip with the OpenAI-style
    response; we forward to the real sink (usage_log spend) and, on the side, sum the
    ``usage`` block so the caller can record ONE turn_metrics row with the turn total.
    """

    __slots__ = ("_inner", "prompt_tokens", "completion_tokens")

    def __init__(self, inner):
        self._inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def __call__(self, used_model: str, response: object) -> None:
        if isinstance(response, dict):
            usage = response.get("usage")
            if isinstance(usage, dict):
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self._inner(used_model, response)


def summary(conn, since: str | None = None, until: str | None = None) -> dict:
    """Aggregate turn quality/speed for a time window (plaintext, no key needed).

    ``since`` (inclusive) / ``until`` (exclusive) are optional UTC datetime strings
    bounding created_at. Returns turn counts, median/p90 duration, and the quality-flag
    rates the reviewer scores on. Empty window -> zeros, never an error.
    """
    assert conn is not None, "conn required for metrics summary"
    where, params = [], []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at < ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    row = conn.execute(
        "SELECT COUNT(*),"
        " median(duration_ms),"
        " quantile_cont(duration_ms, 0.9),"
        " COALESCE(SUM(CASE WHEN degraded THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN hit_max_steps THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(prompt_tokens + completion_tokens), 0),"
        " COALESCE(SUM(CASE WHEN is_local THEN 1 ELSE 0 END), 0)"
        f" FROM turn_metrics{clause};",
        params,
    ).fetchone()
    turns = int(row[0]) if row and row[0] is not None else 0
    fb = conn.execute(
        "SELECT kind, COUNT(*) FROM feedback_events"
        + clause
        + " GROUP BY kind;",
        params,
    ).fetchall()
    feedback = {k: int(c) for k, c in (fb or [])}
    return {
        "turns": turns,
        "median_ms": float(row[1]) if turns and row[1] is not None else 0.0,
        "p90_ms": float(row[2]) if turns and row[2] is not None else 0.0,
        "degraded": int(row[3]) if turns else 0,
        "hit_max_steps": int(row[4]) if turns else 0,
        "total_tokens": int(row[5]) if turns else 0,
        "local_turns": int(row[6]) if turns else 0,
        "stops": feedback.get("stop", 0),
        "regenerations": feedback.get("regenerate", 0),
    }
