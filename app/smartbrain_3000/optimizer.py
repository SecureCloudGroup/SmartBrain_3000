"""Prompt Optimizer (self-improving framework, Phase 5) — SHADOW mode.

The end goal (next phase): interpret what the user is really asking for and steer the
model toward a better answer via a trailing directive — without slowing turns down or
hijacking the user's words. THIS phase ships the measurement half only:

- A **deterministic classifier** tags each incoming turn's request type. Rule-based on
  purpose: the hot path adds zero model latency (a pre-pass model call would hold the
  single local slot hostage before every turn) and no new privacy surface.
- The reviewer's critique may learn a per-request-type **strategy** (a directive
  sentence), stored SHADOW: it never touches a live prompt. Shadow strategies count
  the turns they WOULD have fired on; go-live gating (next phase) compares those
  turns' measured outcomes against baseline before any strategy activates.
- ``observe_turn`` is the hot-path hook: classify, record one content-free event row,
  bump the would-have-fired counter. Fail-open and silent — it can never affect,
  delay, or fail a turn, and it does nothing at all unless the operator enabled the
  optimizer (fail-closed kill-switch, like the reviewer's).

Privacy: events and strategy metadata (type/status/counter) are plaintext and
content-free, exactly like turn_metrics; the directive text is learned from private
activity and lives in the encrypted body (AAD ``optimizer:`` + id).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db
from .secrets import MASTER_KEY_BYTES

log = logging.getLogger(__name__)

_NONCE_BYTES = 12

ENABLED_META_KEY = "optimizer:enabled"

REQUEST_TYPES = ("factual", "multi_step", "code", "retrieval", "ambiguous")
STRATEGY_STATUSES = ("shadow", "active", "disabled")

_MAX_STRATEGIES_TOTAL = 10  # ceiling on stored strategies (verifiable bound)
_MAX_PER_TYPE = 2           # per request type — two competing ideas at most
_DIRECTIVE_MAX_CHARS = 300  # a steering sentence, not an essay

# Classifier rules (ordered; first match wins). Deliberately coarse — the classifier
# only buckets turns for measurement, and go-live gating judges strategies on measured
# outcomes, so a misbucketed turn costs accuracy of accounting, never correctness.
_CODE_RE = re.compile(
    r"```|\btraceback\b|\bstack trace\b|\bexception\b|\berror:\s|\bdef \w+\(|\bfunction\s*\(|[{};]\s*$",
    re.IGNORECASE | re.MULTILINE)
_RETRIEVAL_RE = re.compile(
    r"\bmy (knowledge|notes?|documents?|files?)\b|\bsearch (for|my)\b|\blook up\b"
    r"|\bwhat does .{1,60}\b(say|mention)\b|\bfind (the|my|any)\b",
    re.IGNORECASE)
_MULTI_STEP_RE = re.compile(
    r"^\s*\d+[.)]\s|\bthen\b.+\bthen\b|\bstep[- ]by[- ]step\b|\bfirst\b.+\b(then|second|after that)\b"
    r"|\bplan\b.+\b(and|then)\b",
    re.IGNORECASE | re.MULTILINE)
_FACTUAL_RE = re.compile(
    r"^\s*(what|who|when|where|which|why|how (many|much|old|far|long))\b", re.IGNORECASE)
_FACTUAL_MAX_WORDS = 20  # long interrogatives are usually tasks in disguise


def enabled(conn: duckdb.DuckDBPyConnection) -> bool:
    """Kill-switch read. FAIL-CLOSED: only the exact stored value "true" enables."""
    assert conn is not None, "conn required"
    return db.meta_get(conn, ENABLED_META_KEY) == "true"


def set_enabled(conn: duckdb.DuckDBPyConnection, on: bool) -> None:
    """Flip the optimizer kill-switch."""
    assert conn is not None, "conn required"
    db.meta_set(conn, ENABLED_META_KEY, "true" if on else "false")


def classify(text: str) -> str:
    """Bucket one user ask into a request type. Pure, ordered rules; total function."""
    ask = str(text or "").strip()
    if not ask:
        return "ambiguous"
    if _CODE_RE.search(ask):
        return "code"
    if _RETRIEVAL_RE.search(ask):
        return "retrieval"
    if _MULTI_STEP_RE.search(ask):
        return "multi_step"
    if _FACTUAL_RE.search(ask) and len(ask.split()) <= _FACTUAL_MAX_WORDS:
        return "factual"
    return "ambiguous"


class StrategyStore:
    """AES-256-GCM strategy store over DuckDB's ``optimizer_strategies`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    def add(self, request_type: str, directive: str, rationale: str = "") -> str | None:
        """Store a SHADOW strategy; returns its id, or None when refused.

        Refusals (never raises): unknown type, empty/oversized directive after
        normalization, a similar directive already stored for the type, or a cap hit.
        """
        if request_type not in REQUEST_TYPES:
            return None
        text = " ".join(str(directive or "").split())[:_DIRECTIVE_MAX_CHARS]
        if len(text) < 10:
            return None  # not a directive
        rows = self.list()
        if len(rows) >= _MAX_STRATEGIES_TOTAL:
            return None
        same_type = [r for r in rows if r["request_type"] == request_type]
        if len(same_type) >= _MAX_PER_TYPE:
            return None
        lowered = text.lower()
        if any(r["directive"].lower() == lowered for r in same_type):
            return None  # exact duplicate idea
        sid = str(uuid.uuid4())
        nonce = os.urandom(_NONCE_BYTES)
        body = json.dumps({"directive": text, "rationale": str(rationale or "")[:_DIRECTIVE_MAX_CHARS]})
        ciphertext = self._aes.encrypt(nonce, body.encode("utf-8"), b"optimizer:" + sid.encode("utf-8"))
        self._conn.execute(
            "INSERT INTO optimizer_strategies (id, request_type, status, nonce, ciphertext) "
            "VALUES (?, ?, 'shadow', ?, ?);",
            [sid, request_type, nonce, ciphertext],
        )
        return sid

    def list(self) -> list[dict]:
        """All strategies (decrypted directives), newest first. Bounded by the caps."""
        rows = self._conn.execute(
            "SELECT id, created_at, request_type, status, fired, activated_at, evaluated_at,"
            " baseline_bad, nonce, ciphertext "
            "FROM optimizer_strategies ORDER BY created_at DESC, id DESC LIMIT ?;",
            [_MAX_STRATEGIES_TOTAL * 2],  # slack for rows predating a cap change
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for r in rows:  # bounded by the LIMIT
            body = json.loads(self._aes.decrypt(
                bytes(r[8]), bytes(r[9]), b"optimizer:" + str(r[0]).encode("utf-8")).decode("utf-8"))
            out.append({"id": str(r[0]), "created_at": str(r[1]), "request_type": str(r[2]),
                        "status": str(r[3]), "fired": int(r[4]),
                        "activated_at": None if r[5] is None else str(r[5]),
                        "evaluated_at": None if r[6] is None else str(r[6]),
                        "baseline_bad": None if r[7] is None else float(r[7]),
                        "directive": body.get("directive", ""),
                        "rationale": body.get("rationale", "")})
        return out

    def set_status(self, sid: str, status: str) -> None:
        """Move a strategy through its lifecycle (shadow -> active -> disabled)."""
        assert sid, "strategy id required"
        assert status in STRATEGY_STATUSES, "unknown strategy status"
        self._conn.execute("UPDATE optimizer_strategies SET status = ? WHERE id = ?;", [status, sid])


def _match_strategy(conn: duckdb.DuckDBPyConnection, request_type: str) -> str | None:
    """Oldest live strategy id for the type — PLAINTEXT columns only (hot path: no key,
    no decryption). Oldest-first keeps the match stable while its evidence accumulates."""
    row = conn.execute(
        "SELECT id FROM optimizer_strategies WHERE request_type = ? AND status IN ('shadow', 'active') "
        "ORDER BY created_at ASC, id ASC LIMIT 1;",
        [request_type],
    ).fetchone()
    return None if row is None else str(row[0])


# --- Phase 6: go-live gating + live directives ------------------------------
# Honest gating: SHADOW data can prove a strategy is RELEVANT (it keeps firing) and
# that the problem it targets PERSISTS (its cohort keeps going badly) — it cannot
# prove the strategy helps, because a shadow strategy never touches a turn. So
# promotion needs relevance + persistent problem, and HELP is judged on a measured
# ACTIVE trial against the baseline captured at promotion — kept on improvement,
# auto-disabled on no-improvement or regression. One trial at a time across the
# whole framework (strategy or memory-fact) so metrics stay attributable.
_PROMOTE_MIN_FIRED = 8       # shadow matches before a strategy is even considered
_PROMOTE_MIN_BAD = 0.25      # cohort bad-rate proving the problem persists
_TRIAL_MIN_TURNS = 6         # post-activation cohort turns before judging
_TRIAL_IMPROVE_DELTA = 0.10  # bad-rate must drop this much to be kept
_TRIAL_REGRESS_DELTA = 0.10  # bad-rate rising this much disables early (>=4 turns)
_TRIAL_REGRESS_MIN_TURNS = 4
_COHORT_MATCH_MINUTES = 30   # an event's turn_metrics row lands within this window


def cohort_metrics(conn: duckdb.DuckDBPyConnection, strategy_id: str,
                   since: str | None = None) -> dict:
    """Measured outcomes of the turns a strategy fired on (shadow or active).

    A cohort turn is a turn_metrics row in the same conversation landing within
    ``_COHORT_MATCH_MINUTES`` after one of the strategy's events (events insert at
    turn start, metrics at turn end). ``bad`` = degraded or step-budget-exhausted.
    Plaintext SQL only. ``since`` (DB-clock string) restricts to events after it —
    the active-trial window.
    """
    assert strategy_id, "strategy id required"
    clause = "AND e.created_at >= CAST(? AS TIMESTAMP)" if since else ""
    params = [strategy_id, *([since] if since else [])]
    row = conn.execute(
        f"""
        SELECT COUNT(*),
               COALESCE(SUM(CASE WHEN m.degraded OR m.hit_max_steps THEN 1 ELSE 0 END), 0)
        FROM optimizer_events e
        JOIN turn_metrics m
          ON m.conversation_id = e.conversation_id
         AND m.created_at >= e.created_at
         AND m.created_at <= e.created_at + to_minutes(?)
        WHERE e.strategy_id = ? {clause};
        """,
        [_COHORT_MATCH_MINUTES, *params],
    ).fetchone()
    turns = int(row[0]) if row else 0
    bad = int(row[1]) if row else 0
    return {"turns": turns, "bad": bad, "bad_rate": (bad / turns) if turns else 0.0}


def open_trial(conn: duckdb.DuckDBPyConnection) -> bool:
    """True while ANY strategy is active-but-unjudged (blocks other framework trials)."""
    row = conn.execute(
        "SELECT 1 FROM optimizer_strategies WHERE status = 'active' AND evaluated_at IS NULL LIMIT 1;"
    ).fetchone()
    return row is not None


def promote_and_judge(conn: duckdb.DuckDBPyConnection, key: bytes, *,
                      other_trial_open: bool = False) -> list[str]:
    """The review-time gate: settle any active trial, then maybe promote ONE shadow.

    Returns digest lines for every behavior change (activation / kept is silent /
    disabled), in the same announce-always discipline as the memory-fact loop.
    Never raises past the boundary.
    """
    lines: list[str] = []
    try:
        store = StrategyStore(conn, key)
        rows = store.list()
        # 1) Judge the open trial, if any.
        for s in rows:  # bounded by the store cap
            if s["status"] != "active" or s["evaluated_at"] is not None:
                continue
            trial = cohort_metrics(conn, s["id"], since=s["activated_at"])
            baseline = float(s.get("baseline_bad") or 0.0)
            if (trial["turns"] >= _TRIAL_REGRESS_MIN_TURNS
                    and trial["bad_rate"] > baseline + _TRIAL_REGRESS_DELTA):
                store.set_status(s["id"], "disabled")
                conn.execute("UPDATE optimizer_strategies SET evaluated_at = now() WHERE id = ?;", [s["id"]])
                lines.append(f"turned off guidance for {s['request_type']} requests — it made them worse "
                             f"({baseline:.0%} → {trial['bad_rate']:.0%} going badly)")
            elif trial["turns"] >= _TRIAL_MIN_TURNS:
                if trial["bad_rate"] <= baseline - _TRIAL_IMPROVE_DELTA:
                    conn.execute("UPDATE optimizer_strategies SET evaluated_at = now() WHERE id = ?;",
                                 [s["id"]])  # kept: quiet success needs no announcement
                else:
                    store.set_status(s["id"], "disabled")
                    conn.execute("UPDATE optimizer_strategies SET evaluated_at = now() WHERE id = ?;",
                                 [s["id"]])
                    lines.append(f"turned off guidance for {s['request_type']} requests — "
                                 "it measurably didn't help")
        # 2) Maybe promote ONE shadow strategy (attribution: nothing else on trial).
        if other_trial_open or open_trial(conn):
            return lines
        for s in rows:  # bounded; oldest-last order not important — one per pass
            if s["status"] != "shadow" or s["fired"] < _PROMOTE_MIN_FIRED:
                continue
            cohort = cohort_metrics(conn, s["id"])
            if cohort["turns"] < _PROMOTE_MIN_FIRED or cohort["bad_rate"] < _PROMOTE_MIN_BAD:
                continue  # not enough evidence, or the problem resolved itself
            store.set_status(s["id"], "active")
            conn.execute(
                "UPDATE optimizer_strategies SET activated_at = now(), baseline_bad = ?,"
                " evaluated_at = NULL WHERE id = ?;",
                [cohort["bad_rate"], s["id"]],
            )
            lines.append(
                f"activated guidance for {s['request_type']} requests (on trial: "
                f"{cohort['bad_rate']:.0%} of them were going badly): “{s['directive']}”")
            break  # one activation per review, ever
    except Exception as exc:  # gating is part of the review nicety — never break it
        log.debug("optimizer gating skipped: %s", exc)
    return lines


def apply_directive(conn: duckdb.DuckDBPyConnection, key: bytes | None,
                    messages: list) -> dict | None:
    """Hot-path live steering: the ACTIVE strategy's directive for this ask, if any.

    Returns {"request_type", "directive", "note"} or None. Plaintext match first;
    ONE row's body is decrypted only when an active strategy actually matches — the
    common no-strategy case does no crypto at all. Fail-open like observe_turn.
    The caller appends ``note`` as a TRAILING system message (the _time_line slot),
    so the static prompt head and the conversation prefix stay byte-stable — live
    steering must never cost the prompt cache (the whole point of the design).
    """
    try:
        if conn is None or not enabled(conn):
            return None
        if not isinstance(key, (bytes, bytearray)) or len(key) != MASTER_KEY_BYTES:
            return None  # locked / no key -> baseline behavior
        ask = next((str(m.get("content", "")) for m in reversed(list(messages or []))
                    if isinstance(m, dict) and m.get("role") == "user"), "")
        if not ask.strip():
            return None
        request_type = classify(ask)
        row = conn.execute(
            "SELECT id, nonce, ciphertext FROM optimizer_strategies "
            "WHERE request_type = ? AND status = 'active' ORDER BY created_at ASC LIMIT 1;",
            [request_type],
        ).fetchone()
        if row is None:
            return None
        body = json.loads(AESGCM(bytes(key)).decrypt(
            bytes(row[1]), bytes(row[2]), b"optimizer:" + str(row[0]).encode("utf-8")).decode("utf-8"))
        directive = str(body.get("directive", "")).strip()
        if not directive:
            return None
        return {"request_type": request_type, "directive": directive,
                "note": {"role": "system", "content": f"Guidance for this request: {directive}"}}
    except Exception as exc:  # steering is additive — a failure means baseline, never an error
        log.debug("optimizer directive skipped: %s", exc)
        return None


def observe_turn(conn: duckdb.DuckDBPyConnection, messages: list, conversation_id: str | None) -> None:
    """Hot-path shadow hook: classify the incoming ask, record one content-free event.

    MUST stay cheap and harmless: plaintext SQL only, no model, no decryption, and a
    blanket except — a telemetry hiccup can never affect the user's turn. Does nothing
    while the optimizer kill-switch is off.
    """
    try:
        if conn is None or not enabled(conn):
            return
        ask = next((str(m.get("content", "")) for m in reversed(list(messages or []))
                    if isinstance(m, dict) and m.get("role") == "user"), "")
        if not ask.strip():
            return
        request_type = classify(ask)
        sid = _match_strategy(conn, request_type)
        conn.execute(
            "INSERT INTO optimizer_events (id, conversation_id, request_type, strategy_id) "
            "VALUES (?, ?, ?, ?);",
            [uuid.uuid4().hex, conversation_id, request_type, sid],
        )
        if sid is not None:  # the strategy WOULD have fired here — count it
            conn.execute("UPDATE optimizer_strategies SET fired = fired + 1 WHERE id = ?;", [sid])
    except Exception as exc:  # never let telemetry near a live turn's failure path
        log.debug("optimizer observation skipped: %s", exc)
