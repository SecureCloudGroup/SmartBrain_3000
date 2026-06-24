"""Token-usage recording for the cost view.

Records per-call token counts as plaintext metadata (no message content) so the
cost view can sum spend by model. Cloud cost is computed from the live catalog
pricing at view time; local models have no pricing and cost $0. Recording is
best-effort: telemetry must never break a chat or agent turn.
"""

from __future__ import annotations

import logging
import uuid

log = logging.getLogger(__name__)


def record(conn, model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Insert one usage row (model + token counts)."""
    assert model, "model required to record usage"
    assert prompt_tokens >= 0 and completion_tokens >= 0, "token counts must be non-negative"
    conn.execute(
        "INSERT INTO usage_log (id, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?);",
        [uuid.uuid4().hex, model, int(prompt_tokens), int(completion_tokens)],
    )


def record_response(conn, model: str, response: object) -> None:
    """Best-effort: record token usage from an OpenAI-style chat response.

    A response without a ``usage`` block is ignored; any failure is swallowed
    (logged at debug) so usage logging can never fail a turn.
    """
    if conn is None or not model or not isinstance(response, dict):
        return
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return
    try:
        record(conn, model, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0)
    except Exception as exc:  # never fail a turn over telemetry
        log.debug("usage record skipped: %s", exc)


def summary(conn, since: str | None = None, until: str | None = None) -> list[dict]:
    """Per-model totals (call count + token sums), busiest model first.

    ``since`` (inclusive) / ``until`` (exclusive) are optional UTC datetime
    strings ('YYYY-MM-DD HH:MM:SS') bounding created_at — used by the cost
    view's time-range picker. An exclusive upper bound at the next local
    midnight avoids dropping rows in the final sub-second of a day.
    """
    assert conn is not None, "conn required for usage summary"
    assert since is None or isinstance(since, str), "since must be a string or None"
    assert until is None or isinstance(until, str), "until must be a string or None"
    where, params = [], []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at < ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT model, COUNT(*), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0) "
        f"FROM usage_log{clause} GROUP BY model ORDER BY 2 DESC;",
        params,
    ).fetchall()
    assert rows is not None, "query must return a result set"
    return [
        {"model": r[0], "calls": int(r[1]), "prompt_tokens": int(r[2]), "completion_tokens": int(r[3])}
        for r in rows
    ]
