"""Self-review: the periodic scorecard pass (self-improving framework, Phase 2).

Every ~8 hours (while unlocked) this reads the Phase-1 telemetry plus the audit
log and quantifies how well each component — Chat, Knowledge, Tools — performed
over the window, entirely in SQL over PLAINTEXT metadata columns (no LLM in this
phase; the local-model critique pass arrives separately). The scorecard is stored
encrypted (AAD ``review:`` + id), and a digest is surfaced to the user ONLY when
a deterministic flag fires — silence is the normal outcome.

Safety posture (the plan's "autonomous within bounds"):
- Kill-switch: ``selfimprove:enabled`` meta flag, FAIL-CLOSED — absent or corrupt
  reads as disabled (mirrors consent.py's self-defending read).
- Unlocked-only: runs from the scheduler tick, which already bails while locked.
- This phase never changes behavior — it only measures and reports.

All timestamps come from the DATABASE clock (``now()``), never Python's, so the
window bounds compare consistently with every ``DEFAULT now()`` column write.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db, metrics
from .secrets import MASTER_KEY_BYTES

log = logging.getLogger(__name__)

_NONCE_BYTES = 12
# Microsecond precision matters: window bounds are compared against DEFAULT now() column
# values, and a seconds-truncated ``until`` would exclude every row written in the current
# second (the window end is exclusive) — seen as 0-turn scorecards in tests.
_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

ENABLED_META_KEY = "selfimprove:enabled"
LAST_RUN_META_KEY = "selfimprove:last_run"
REVIEW_INTERVAL_SECONDS = 8 * 3600  # operator-chosen cadence: every 8 hours
# A first-ever review (no last_run) still needs a bounded window — 8h, matching the
# cadence, so a years-old backlog can't produce a misleading "everything is on fire"
# first scorecard from ancient rows.
_FIRST_WINDOW_SECONDS = REVIEW_INTERVAL_SECONDS

# Deterministic flag thresholds. Every rate flag carries a MINIMUM SAMPLE so two bad
# turns on a quiet day can't page the user; the numbers are starting points the later
# phases will tune against their own measurements.
_MIN_TURNS = 5           # rate flags on chat need at least this many turns
_DEGRADED_RATE = 0.2     # ≥20% of answers fell back to a degraded/plain reply
_MAX_STEPS_RATE = 0.2    # ≥20% of turns exhausted the step budget
_DISSATISFACTION_RATE = 0.3  # ≥30% of turns were stopped or regenerated
_MIN_TOOL_CALLS = 3      # per-tool failure flag needs at least this many attempts
_TOOL_FAIL_RATE = 0.5    # ≥50% of a tool's attempts errored
_MIN_DENIALS = 2         # ≥2 denials in a window = the assistant repeatedly proposed wrong
_MAX_FLAGGED_TOOLS = 3   # cap per-review tool flags (verifiable bound, keeps digests short)


def enabled(conn: duckdb.DuckDBPyConnection) -> bool:
    """Kill-switch read. FAIL-CLOSED: only the exact stored value "true" enables —
    absent, corrupt, or anything else reads as disabled (a wiped/garbled config must
    disable the self-improver, never silently enable it)."""
    assert conn is not None, "conn required"
    return db.meta_get(conn, ENABLED_META_KEY) == "true"


def set_enabled(conn: duckdb.DuckDBPyConnection, on: bool) -> None:
    """Flip the kill-switch (stored as the literal strings enabled() checks)."""
    assert conn is not None, "conn required"
    db.meta_set(conn, ENABLED_META_KEY, "true" if on else "false")


def last_run(conn: duckdb.DuckDBPyConnection) -> str | None:
    """The previous review's window_end ('%Y-%m-%d %H:%M:%S', DB clock), or None."""
    return db.meta_get(conn, LAST_RUN_META_KEY)


def due(conn: duckdb.DuckDBPyConnection) -> bool:
    """True when a full interval has passed since the last review (DB clock).

    A malformed stored timestamp reads as due — the review then runs and rewrites a
    valid one, self-healing the cadence state.
    """
    assert conn is not None, "conn required"
    prev = last_run(conn)
    if not prev:
        return True
    try:
        row = conn.execute(
            "SELECT strptime(?, ?) + to_seconds(?) <= now();",
            [prev, _TS_FORMAT, REVIEW_INTERVAL_SECONDS],
        ).fetchone()
    except duckdb.Error:
        return True  # corrupt timestamp -> run now and self-heal
    return bool(row and row[0])


class ReviewStore:
    """AES-256-GCM review store over DuckDB's ``reviews`` table (Phase-2 shape:
    append + latest; later phases read history for before/after comparisons)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    def add(self, window_start: str, window_end: str, scorecard: dict, flags: int) -> str:
        """Persist one review cycle; returns its id. ``flags`` is the plaintext count."""
        assert window_start and window_end, "window bounds required"
        assert isinstance(scorecard, dict), "scorecard must be a dict"
        assert flags >= 0, "flag count must be non-negative"
        rid = str(uuid.uuid4())
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aes.encrypt(
            nonce, json.dumps({"scorecard": scorecard}).encode("utf-8"),
            b"review:" + rid.encode("utf-8"),
        )
        self._conn.execute(
            "INSERT INTO reviews (id, window_start, window_end, status, flags, nonce, ciphertext) "
            "VALUES (?, strptime(?, ?), strptime(?, ?), 'complete', ?, ?, ?);",
            [rid, window_start, _TS_FORMAT, window_end, _TS_FORMAT, flags, nonce, ciphertext],
        )
        return rid

    def latest(self) -> dict | None:
        """The most recent review (decrypted scorecard), or None on a fresh install."""
        row = self._conn.execute(
            "SELECT id, created_at, window_start, window_end, status, flags, nonce, ciphertext "
            "FROM reviews ORDER BY created_at DESC, id DESC LIMIT 1;"
        ).fetchone()
        if row is None:
            return None
        body = json.loads(
            self._aes.decrypt(bytes(row[6]), bytes(row[7]), b"review:" + str(row[0]).encode("utf-8")).decode("utf-8")
        )
        return {"id": str(row[0]), "created_at": str(row[1]), "window_start": str(row[2]),
                "window_end": str(row[3]), "status": str(row[4]), "flags": int(row[5]),
                "scorecard": body["scorecard"]}

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM reviews;").fetchone()
        return 0 if row is None else int(row[0])


def _window(conn: duckdb.DuckDBPyConnection) -> tuple[str, str]:
    """Window bounds from the DB clock: [validated previous window_end, now].

    The stored stamp is used ONLY when it parses and is recent (within twice the
    review interval). A corrupt or fraction-less stamp must not wedge the pass —
    feeding it into the window SQL would throw on every run BEFORE the successful
    run's meta_set could rewrite it, so the advertised self-heal would never happen
    (found by adversarial review, reproduced live). A stale-but-valid stamp from a
    long disabled gap falls back too: an unbounded catch-up window is exactly the
    misleading everything-on-fire scorecard _FIRST_WINDOW_SECONDS exists to prevent.
    Cadence jitter (a tick landing minutes late) stays within the 2x bound, so a
    normal, slightly-long window is still covered edge to edge.
    """
    row = conn.execute(
        "SELECT strftime(now() - to_seconds(?), ?), strftime(now(), ?);",
        [_FIRST_WINDOW_SECONDS, _TS_FORMAT, _TS_FORMAT],
    ).fetchone()
    assert row is not None, "clock query must return a row"
    fallback, until = str(row[0]), str(row[1])
    prev = last_run(conn)
    if not prev:
        return fallback, until
    try:
        recent = conn.execute(
            "SELECT strptime(?, ?) >= now() - to_seconds(?);",
            [prev, _TS_FORMAT, 2 * _FIRST_WINDOW_SECONDS],
        ).fetchone()
    except duckdb.Error:
        return fallback, until  # corrupt stamp -> bounded window; meta_set then heals it
    return (prev, until) if (recent and recent[0]) else (fallback, until)


def _tool_stats(conn: duckdb.DuckDBPyConnection, since: str, until: str) -> tuple[list[dict], int]:
    """Per-tool execution outcomes + denial count from audit_log plaintext columns.

    Executions are the decisions where a handler actually ran (or raised): auto /
    executed / errored — proposed/approved/denied rows are decisions, not runs.
    """
    rows = conn.execute(
        "SELECT tool_name, COUNT(*), SUM(CASE WHEN ok THEN 0 ELSE 1 END) "
        "FROM audit_log WHERE ts >= strptime(?, ?) AND ts < strptime(?, ?) "
        "AND decision IN ('auto', 'executed', 'errored') "
        "GROUP BY tool_name ORDER BY 3 DESC, 2 DESC;",
        [since, _TS_FORMAT, until, _TS_FORMAT],
    ).fetchall()
    tools = [{"tool": str(r[0]), "calls": int(r[1]), "failures": int(r[2] or 0)} for r in rows]
    denied = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE ts >= strptime(?, ?) AND ts < strptime(?, ?) "
        "AND decision = 'denied';",
        [since, _TS_FORMAT, until, _TS_FORMAT],
    ).fetchone()
    return tools, int(denied[0]) if denied else 0


def _knowledge_stats(conn: duckdb.DuckDBPyConnection, key: bytes, tools: list[dict]) -> dict:
    """Knowledge health: search activity (from the tool stats) + embedding backlog.

    The backlog probe needs the gateway only to name the embed model; if it is
    unreachable the backlog reads as unknown (None) rather than failing the review.
    """
    search = next((t for t in tools if t["tool"] == "kb_search"), None)
    pending: int | None = None
    try:  # gateway down / no embed model routed -> backlog unknown, never fatal
        from . import gateway
        from .kb import KnowledgeBase
        pending = KnowledgeBase(conn, key).docs_pending_embedding(gateway.embed_model(conn))
    except Exception as exc:
        log.debug("self-review: embedding backlog unknown: %s", exc)
    return {
        "searches": 0 if search is None else search["calls"],
        "search_failures": 0 if search is None else search["failures"],
        "pending_embedding": pending,
    }


def build_scorecard(conn: duckdb.DuckDBPyConnection, key: bytes,
                    since: str, until: str, *, prior_pending: int | None = None) -> tuple[dict, list[str]]:
    """Quantify the window per component (pure SQL) and derive deterministic flags.

    Returns ``(scorecard, flag_lines)`` — flag_lines are the human-readable findings
    that, when non-empty, justify surfacing a digest to the user. ``prior_pending``
    is the previous review's embedding backlog (None on a first review) — the
    backlog flag needs it to distinguish "stuck" from "normally draining".
    """
    assert conn is not None and key, "conn + key required"
    assert since and until, "window bounds required"
    chat = metrics.summary(conn, since, until)  # turn_metrics + feedback_events aggregate
    tools, denied = _tool_stats(conn, since, until)
    knowledge = _knowledge_stats(conn, key, tools)
    flags: list[str] = []

    turns = chat["turns"]
    if turns >= _MIN_TURNS:  # rate flags need a real sample, not two bad turns
        if chat["degraded"] / turns >= _DEGRADED_RATE:
            flags.append(f"{chat['degraded']} of {turns} answers were degraded (model fell back without tools)")
        if chat["hit_max_steps"] / turns >= _MAX_STEPS_RATE:
            flags.append(f"{chat['hit_max_steps']} of {turns} turns exhausted the step budget before finishing")
        unhappy = chat["stops"] + chat["regenerations"]
        if unhappy / turns >= _DISSATISFACTION_RATE:
            flags.append(f"{unhappy} of {turns} answers were stopped or regenerated")

    flagged_tools = [t for t in tools
                     if t["calls"] >= _MIN_TOOL_CALLS and t["failures"] / t["calls"] >= _TOOL_FAIL_RATE]
    for t in flagged_tools[:_MAX_FLAGGED_TOOLS]:  # bounded (P10 #2): digests stay short
        flags.append(f"tool '{t['tool']}' failed {t['failures']} of {t['calls']} calls")
    if denied >= _MIN_DENIALS:
        flags.append(f"{denied} proposed actions were denied (the assistant proposed the wrong thing)")
    if knowledge["pending_embedding"] and prior_pending:
        # Flag only when the backlog was ALSO nonzero at the PREVIOUS review — i.e. it has
        # persisted across a whole interval, which means embedding is genuinely stuck
        # (gateway/model trouble). An instantaneous nonzero probe alone is normal life: a
        # bulk import minutes before a due review is still draining (the auto-reindexer
        # works a 20s budget per tick and yields to foreground chat), and paging the user
        # for a healthy drain was a false alarm (found by adversarial review).
        flags.append(f"{knowledge['pending_embedding']} documents are still waiting to be indexed for meaning-search")

    scorecard = {"window": {"start": since, "end": until},
                 "chat": chat, "tools": tools,
                 "denied": denied, "knowledge": knowledge, "flags": flags}
    return scorecard, flags


def _digest(chat: dict, flags: list[str]) -> str:
    """The user-facing digest — emitted only when at least one flag fired."""
    assert flags, "digest is only built when something is worth saying"
    head = (f"Self-review of the last period: {chat['turns']} chat turns"
            + (f", median answer {chat['median_ms'] / 1000:.1f}s" if chat["turns"] else "")
            + ".")
    lines = "\n".join(f"- {f}" for f in flags)
    return f"{head}\n\nNeeds attention:\n{lines}"


def run_review(conn: duckdb.DuckDBPyConnection, key: bytes, *, notify=None,
               locked_check=None) -> dict | None:
    """One reviewer pass: gate on kill-switch + cadence, score the window, persist,
    and surface a digest via ``notify(status, message)`` ONLY when a flag fired.

    ``locked_check`` (the tick passes one) is consulted at entry AND again before any
    write: the tick snapshots the master key at its start, so without this re-check a
    mid-tick Lock would let the review keep decrypting and writing after the vault
    re-locked — the same stand-down contract run_schedule's locked_check enforces.

    Returns the stored scorecard summary, or None when gated off / not yet due.
    Never raises past the boundary — a review failure must never hurt the tick.
    """
    assert conn is not None and key, "conn + key required"
    try:
        if not enabled(conn) or not due(conn):
            return None
        if locked_check is not None and locked_check():
            return None  # vault re-locked since the tick snapshot — stand down
        since, until = _window(conn)
        store = ReviewStore(conn, key)
        prior = store.latest()
        prior_pending = (prior or {}).get("scorecard", {}).get("knowledge", {}).get("pending_embedding")
        scorecard, flags = build_scorecard(conn, key, since, until, prior_pending=prior_pending)
        if locked_check is not None and locked_check():
            return None  # re-check before the first write: never persist after a relock
        rid = store.add(since, until, scorecard, len(flags))
        # Digest BEFORE the cadence advance: if notify fails, the stamp stays put and the
        # next tick re-runs the window — a duplicate review row and a retried digest.
        # The digest is this phase's only user-visible surface, so a harmless duplicate
        # beats silently losing it forever (the original order did exactly that).
        if flags and notify is not None:
            notify("complete", _digest(scorecard["chat"], flags))
        db.meta_set(conn, LAST_RUN_META_KEY, until)
        log.info("self-review complete: window %s..%s, %d flag(s)", since, until, len(flags))
        return {"id": rid, "window_start": since, "window_end": until, "flags": len(flags)}
    except Exception as exc:  # the reviewer is a background nicety — never break a tick
        log.warning("self-review failed: %s", exc)
        return None
