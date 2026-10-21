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


def record(conn, model: str, prompt_tokens: int, completion_tokens: int,
           cost_usd: float | None = None) -> None:
    """Insert one usage row (model + token counts).

    ``cost_usd`` is the API-equivalent cost a plan-covered call reports for itself
    (Claude Code's ``total_cost_usd``); None for providers whose cost is computed
    from catalog pricing at display time."""
    assert model, "model required to record usage"
    assert prompt_tokens >= 0 and completion_tokens >= 0, "token counts must be non-negative"
    conn.execute(
        "INSERT INTO usage_log (id, model, prompt_tokens, completion_tokens, cost_usd)"
        " VALUES (?, ?, ?, ?, ?);",
        [uuid.uuid4().hex, model, int(prompt_tokens), int(completion_tokens),
         float(cost_usd) if cost_usd is not None else None],
    )


def _count(val: object) -> int:
    """A positive int token count, else 0 (bools and other types are not counts)."""
    return val if isinstance(val, int) and not isinstance(val, bool) and val > 0 else 0


def cache_tokens(usage: object) -> tuple[int, int]:
    """(cache write, cache read) token counts from a usage block; 0 when absent/invalid.

    Two shapes reach us. Anthropic-native usage (claude CLI) has top-level
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens``. Bifrost's OpenAI-compatible
    /v1/chat/completions moves Anthropic's cache counts into ``prompt_tokens_details``:
    reads as ``cached_read_tokens`` (mirrored to OpenAI's ``cached_tokens``) and writes as
    ``cached_write_tokens`` (mirrored to ``cache_write_tokens``), with the top-level keys absent.
    Top-level keys win when present; otherwise the details block is read."""
    if not isinstance(usage, dict):
        return 0, 0
    created = _count(usage.get("cache_creation_input_tokens"))
    read = _count(usage.get("cache_read_input_tokens"))
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        created = created or _count(details.get("cached_write_tokens")) or _count(details.get("cache_write_tokens"))
        read = read or _count(details.get("cached_read_tokens")) or _count(details.get("cached_tokens"))
    return created, read


def prompt_total(usage: dict) -> int:
    """Total prompt tokens for a usage block, cache included.

    Anthropic-native usage reports ``input_tokens`` EXCLUDING cached tokens, so the total is
    input_tokens + cache_creation_input_tokens + cache_read_input_tokens. OpenAI-style usage
    (``prompt_tokens``) is already a total and is used as-is."""
    assert isinstance(usage, dict), "usage must be a dict"
    created, read = cache_tokens(usage)
    native = usage.get("input_tokens")
    if "prompt_tokens" not in usage and isinstance(native, int) and not isinstance(native, bool):
        return max(native, 0) + created + read
    return usage.get("prompt_tokens") or 0


def record_response(conn, model: str, response: object, purpose: str | None = None) -> None:
    """Best-effort: record token usage from an OpenAI-style chat response.

    A response without a ``usage`` block is ignored; any failure is swallowed
    (logged at debug) so usage logging can never fail a turn. Prompt-cache tokens
    (either usage shape, see ``cache_tokens``) count toward the prompt total.
    ``purpose`` is a caller label (e.g. "ni.fit") kept in the debug log:
    usage_log has no purpose column, so rows aggregate by model in /api/usage.
    """
    if conn is None or not model or not isinstance(response, dict):
        return
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return
    cost = usage.get("cost_usd")
    try:
        created, read = cache_tokens(usage)
        record(conn, model, prompt_total(usage), usage.get("completion_tokens") or 0,
               cost_usd=float(cost) if isinstance(cost, (int, float)) else None)
        if purpose:
            log.debug("usage recorded purpose=%s model=%s cache_creation=%d cache_read=%d",
                      purpose, model, created, read)
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
        "SELECT model, COUNT(*), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0), "
        "SUM(cost_usd) "
        f"FROM usage_log{clause} GROUP BY model ORDER BY 2 DESC;",
        params,
    ).fetchall()
    assert rows is not None, "query must return a result set"
    return [
        {"model": r[0], "calls": int(r[1]), "prompt_tokens": int(r[2]), "completion_tokens": int(r[3]),
         # API-equivalent value self-reported by plan-covered calls; None when never reported.
         "recorded_cost": float(r[4]) if r[4] is not None else None}
        for r in rows
    ]
