"""Token-usage recording for the cost view.

Records per-call token counts as plaintext metadata (no message content) so the
cost view can sum spend by model. Cloud cost is computed from the live catalog
pricing at view time; local models have no pricing and cost $0. Recording is
best-effort: telemetry must never break a chat or agent turn.
"""

from __future__ import annotations

import logging
import time
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
    _warn_if_server_is_reloading(model, usage)


# A local model server that reports spending seconds LOADING the model on a request is
# misconfigured, not slow: it is throwing the weights away between requests. This cost a
# real install 4.5s on EVERY turn for five days — three times the rest of the turn — and
# nothing in the app said so, because the app records tokens and never looked at the
# server's own timings. Say it out loud, rarely enough to stay readable.
_RELOAD_WARN_SECONDS = 1.0
_RELOAD_WARN_INTERVAL = 900.0  # at most once every 15 minutes per process
# None means "not yet warned" — NOT 0.0. time.monotonic() counts from an arbitrary origin
# that is near zero on a freshly booted machine, so a 0.0 sentinel reads as "warned at
# startup" and would swallow the first report for fifteen minutes. Startup is exactly when
# a user is most likely to be watching. (CI caught this: its runners boot seconds before
# the tests run, where a dev machine's clock is hours in.)
_last_reload_warning: float | None = None


def _warn_if_server_is_reloading(model: str, usage: dict) -> None:
    """Surface a model server that reloads its model per request (best-effort, throttled)."""
    global _last_reload_warning
    try:
        seconds = usage.get("model_load_duration")
        if not isinstance(seconds, (int, float)) or seconds < _RELOAD_WARN_SECONDS:
            return
        now = time.monotonic()
        if _last_reload_warning is not None and now - _last_reload_warning < _RELOAD_WARN_INTERVAL:
            return
        _last_reload_warning = now
        log.warning(
            "%s: the model server spent %.1fs LOADING the model for this request. If that "
            "happens every request it is a server setting, not the model being slow — check "
            "for a draft/speculative-decoding option pointed at an incompatible model, or an "
            "idle-unload setting.", model, float(seconds),
        )
    except Exception as exc:  # telemetry must never break a turn
        log.debug("reload check skipped: %s", exc)


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
