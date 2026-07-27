"""Self-review: the periodic scorecard + critique pass (self-improving framework).

Every ~8 hours (while unlocked) this quantifies how well each component — Chat,
Knowledge, Tools — performed over the window, entirely in SQL over PLAINTEXT
metadata columns. When something is flagged, a LOCAL model critiques a bounded
sample of the evidence and may propose ONE improvement; a high-confidence learned
preference is applied through the reversible memory-fact lever and put ON TRIAL —
the next review with enough evidence keeps it or auto-reverts it against the
baseline captured at apply time. The scorecard is stored encrypted (AAD
``review:`` + id), and a digest surfaces ONLY when a flag fired or behavior
actually changed — silence is the normal outcome.

Safety posture (the plan's "autonomous within bounds"):
- Kill-switch: ``selfimprove:enabled`` meta flag, FAIL-CLOSED — absent or corrupt
  reads as disabled (mirrors consent.py's self-defending read).
- Unlocked-only: runs from the scheduler tick, which already bails while locked,
  and stands down mid-pass via ``locked_check`` before anything is written.
- Privacy: the critique reads private chats, so it runs on a LOCAL model or not
  at all — never a cloud fallback. Metrics-only cycles need no model.
- Change discipline: reversible data-level levers only (see improvements.py),
  one change on trial at a time, measured against its baseline, auto-reverted on
  regression, and ALWAYS announced in the digest.

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

# --- Phase 3: critique + closed loop ---------------------------------------
# The critique pass reads PRIVATE activity, so it runs on a LOCAL model or not at all
# (hard privacy gate — never "prefer local, fall back to cloud").
_CRITIQUE_TIMEOUT = 120.0   # one bounded local call per review; the tick tolerates it
_MAX_EVIDENCE_MESSAGES = 6  # user messages sampled (USER-authored only — see _evidence)
_EVIDENCE_CHARS = 240       # per-item truncation
_MAX_FINDINGS = 3           # findings accepted from one critique (bounded)
_MIN_APPLY_CONFIDENCE = 0.7  # below this a finding is recorded but never self-applied

# Auto-revert: a trial must beat the baseline it was applied against, judged ONLY on
# post-apply evidence (the trial window is clipped to applied_at).
_MIN_TRIAL_TURNS = 5        # post-apply turns needed before judging a trial either way
_REGRESSION_DELTA = 0.15    # dissatisfaction rate rising this much = revert...
_MIN_TRIAL_UNHAPPY = 2      # ...AND at least this many unhappy events: at the minimum
#                             sample a SINGLE ordinary stop (1/5 = 0.20) would otherwise
#                             always clear the delta and force a false "made things worse".
# A trial that can't gather evidence must not stay live unmeasured forever — an unverified
# change is REVERTED after this many intervals (reversible-by-default: the very harm a bad
# fact causes can be what keeps the user away from chat, so "no evidence" is not "no harm").
_MAX_TRIAL_INTERVALS = 3
# Digest lines that could not be delivered (notify failed / relock) queue durably here and
# ride the next digest — a behavior change announcement must never be silently lost.
_PENDING_CHANGES_META_KEY = "selfimprove:pending_changes"


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


def _dissatisfaction(chat: dict) -> float:
    """(stops + regenerations) / turns — the closed loop's target metric.

    Stopping or regenerating an answer is the strongest implicit "that wasn't what I
    wanted" signal the app records, so it is what an applied improvement must not worsen.
    """
    turns = chat.get("turns") or 0
    return 0.0 if turns <= 0 else (chat.get("stops", 0) + chat.get("regenerations", 0)) / turns


def _local_model(conn: duckdb.DuckDBPyConnection) -> str | None:
    """Resolve a model for the critique pass, or None to skip it.

    HARD privacy gate: the critique reads private chat content, so a cloud model is
    refused outright rather than silently used — the promise is that this content never
    leaves the machine. Also yields when the single local slot is busy with a foreground
    request (the pass is a nicety; the user's chat is not).
    """
    from . import gateway
    routes = gateway.load_routes(conn)
    model = (gateway.resolve_model("self_review", routes)
             or gateway.resolve_model("agent", routes)
             or gateway.resolve_model("chat", routes))
    if not model or not gateway.is_local(model):
        log.debug("self-review: critique skipped (no local model routed)")
        return None
    if not gateway.local_available():
        log.debug("self-review: critique skipped (local model busy)")
        return None
    return model


def _evidence(conn: duckdb.DuckDBPyConnection, key: bytes, since: str, until: str) -> dict:
    """Sample bounded, decrypted evidence for the critique prompt.

    PROMPT-INJECTION BOUNDARY: evidence is restricted to messages the USER wrote —
    nothing else. Tool results, fetched web pages, document text, AND tool-error
    strings are all excluded: a learned preference becomes standing instruction text
    in every future system prompt, and error strings can embed third-party bytes (an
    upstream server's header, a hostile document quoted by a parser), so hostile
    content the assistant merely *read* must never be able to author one. Trashed
    conversations are excluded too — the user deleted them; they are not evidence.
    """
    # Conversations the user stopped or regenerated in — where dissatisfaction actually happened.
    cids = conn.execute(
        "SELECT DISTINCT conversation_id FROM feedback_events WHERE conversation_id IS NOT NULL"
        " AND created_at >= strptime(?, ?) AND created_at < strptime(?, ?) LIMIT ?;",
        [since, _TS_FORMAT, until, _TS_FORMAT, _MAX_EVIDENCE_MESSAGES],
    ).fetchall()
    asks: list[str] = []
    if cids:
        from .history import ChatHistory
        history = ChatHistory(conn, key)
        for (cid,) in cids:  # bounded by the LIMIT above
            if len(asks) >= _MAX_EVIDENCE_MESSAGES:
                break
            try:
                if history.get_conversation(str(cid)) is None:
                    continue  # trashed (or gone): the user deleted it — not evidence
                msgs = history.get_messages(str(cid))
            except Exception:  # an undecryptable conversation is simply not evidence
                continue
            user_asks = [m["content"] for m in msgs if m.get("role") == "user"]
            if user_asks:
                asks.append(str(user_asks[-1])[:_EVIDENCE_CHARS])
    return {"asks": asks}


_CRITIQUE_SYSTEM = (
    "You review an AI assistant's own recent performance and propose ONE improvement at most. "
    "You reply with JSON only — no prose, no code fences.\n\n"
    "Reply with a JSON array (possibly empty) of at most 1 object:\n"
    '[{"category":"preference","component":"chat","description":"<what you observed, one sentence>",'
    '"payload":"<a single durable preference about how the user likes answers>","confidence":0.0}]\n\n'
    "Rules:\n"
    "- Only propose a 'preference' when the USER'S OWN messages show a durable, repeated pattern "
    "in how they want answers (length, format, tone, level of detail).\n"
    "- 'payload' must be one short sentence stating that preference, e.g. "
    "'Prefers concise answers with the conclusion first.' Never restate a single request, "
    "never include specifics of any document, and never write an instruction to take actions.\n"
    "- confidence is 0.0-1.0: how sure you are this is a durable preference, not a one-off.\n"
    "- If the evidence does not clearly show such a pattern, reply exactly: []"
)


def _critique_prompt(scorecard: dict, evidence: dict) -> str:
    """Compose the (bounded) critique user message from metrics + sampled evidence."""
    chat = scorecard["chat"]
    parts = [
        f"Period: {chat['turns']} chat turns, median answer {chat['median_ms'] / 1000:.1f}s.",
        f"Answers stopped or regenerated by the user: {chat['stops'] + chat['regenerations']}.",
        f"Degraded answers: {chat['degraded']}. Turns that ran out of steps: {chat['hit_max_steps']}.",
    ]
    if scorecard["flags"]:
        parts.append("Observed problems:\n" + "\n".join(f"- {f}" for f in scorecard["flags"]))
    if evidence["asks"]:
        parts.append("Recent requests the user was not satisfied with:\n"
                     + "\n".join(f"- {a}" for a in evidence["asks"]))
    return "\n\n".join(parts)


def _parse_findings(text: str) -> list[dict]:
    """Parse a local model's critique reply into validated findings.

    Local models are unreliable JSON emitters, so this is deliberately forgiving about
    WRAPPING (code fences, leading prose) and strict about CONTENT: every field is
    validated against the allowlists and anything unrecognized is dropped rather than
    coerced. A garbled reply yields no findings — never a malformed improvement.
    """
    from . import improvements as imp

    raw = str(text or "").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(raw[start:end + 1])
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict] = []
    for item in parsed[:_MAX_FINDINGS]:  # bounded
        if not isinstance(item, dict):
            continue
        category, component = item.get("category"), item.get("component")
        description, payload = item.get("description"), item.get("payload")
        if category not in imp.CATEGORIES or component not in imp.COMPONENTS:
            continue
        if not isinstance(description, str) or not isinstance(payload, str):
            continue
        if not description.strip() or not imp._clean_fact(payload):
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        out.append({"category": category, "component": component,
                    "description": description.strip()[:_EVIDENCE_CHARS],
                    "payload": payload, "confidence": max(0.0, min(1.0, confidence))})
    return out


def _critique(conn: duckdb.DuckDBPyConnection, key: bytes, scorecard: dict,
              since: str, until: str) -> list[dict]:
    """Run the local-model critique for a flagged window. Never raises; [] when skipped."""
    model = _local_model(conn)
    if model is None:
        return []
    try:
        from . import gateway
        evidence = _evidence(conn, key, since, until)
        if not evidence["asks"]:
            return []  # metrics alone flagged; no user-authored evidence to reason over
        response = gateway.chat(
            [{"role": "system", "content": _CRITIQUE_SYSTEM},
             {"role": "user", "content": _critique_prompt(scorecard, evidence)}],
            model, timeout=_CRITIQUE_TIMEOUT,
        )
        choices = (response or {}).get("choices") or []
        content = (choices[0].get("message") or {}).get("content", "") if choices else ""
        findings = _parse_findings(content)
        log.info("self-review: critique produced %d finding(s)", len(findings))
        return findings
    except Exception as exc:  # gateway down / model confused — the review still stands
        log.debug("self-review: critique failed: %s", exc)
        return []


def _settle_trial(conn: duckdb.DuckDBPyConnection, key: bytes, since: str, until: str) -> str | None:
    """Judge the improvement currently on trial against the baseline it was applied on.

    Judged ONLY on post-apply evidence: the trial window is clipped to ``applied_at``,
    so a re-run of the applying window (notify failure, relock retry) can never settle
    a trial against the very pre-apply rows its baseline came from. A trial with too
    little evidence stays open — until the interval cap, when the UNMEASURED change is
    reverted (reversible-by-default: a bad fact can itself be why the user stopped
    chatting, so absence of evidence must not immortalize it as "kept").

    Returns a digest line when the trial resolved with something to say, else None.
    """
    from . import improvements as imp

    store = imp.ImprovementStore(conn, key)
    store.reconcile()  # facts the user hand-deleted -> rejected rows; frees ceiling slots
    trial = store.on_trial()
    if trial is None:
        return None
    # Clip the evidence window to the apply moment (string compare is safe: both are
    # zero-padded 'YYYY-MM-DD HH:MM:SS[.ffffff]' from the same DB clock).
    trial_since = max(since, trial["applied_at"] or since)
    chat = metrics.summary(conn, trial_since, until)
    if chat["turns"] < _MIN_TRIAL_TURNS:
        stale = conn.execute(
            "SELECT CAST(? AS TIMESTAMP) < now() - to_seconds(?);",
            [trial["applied_at"], _MAX_TRIAL_INTERVALS * REVIEW_INTERVAL_SECONDS],
        ).fetchone()
        if stale and stale[0]:  # never enough evidence — undo the unverified change
            if store.revert(trial["id"]):
                return ("removed an unverified change (not enough evidence to judge it): "
                        f"{trial['description']}")
            return None
        return None  # keep waiting for post-apply evidence
    baseline = (trial["body"].get("baseline") or {})
    before = float(baseline.get("dissatisfaction", 0.0))
    after = _dissatisfaction(chat)
    unhappy = chat["stops"] + chat["regenerations"]
    if after > before + _REGRESSION_DELTA and unhappy >= _MIN_TRIAL_UNHAPPY:
        if store.revert(trial["id"]):
            return (f"reverted a change that made things worse: {trial['description']} "
                    f"(unhappy answers {before:.0%} → {after:.0%})")
        return None
    store.mark_evaluated(trial["id"])
    return None  # kept: a change that quietly worked needs no announcement


def _maybe_apply(conn: duckdb.DuckDBPyConnection, key: bytes, findings: list[dict],
                 chat: dict) -> str | None:
    """Apply at most ONE high-confidence finding; return a digest line when something changed.

    Blocked while another improvement is on trial (attribution) and below the confidence
    floor. The baseline recorded here is what the next review judges the change against.
    """
    from . import improvements as imp

    # v1 trusts exactly one finding shape to the memory-fact lever: a learned PREFERENCE.
    # Other categories the parser admits are future lever work — drop them here so a
    # mis-labeled finding can never ride the wrong lever into the system prompt.
    findings = [f for f in findings if f["category"] == "preference"]
    if not findings:
        return None
    store = imp.ImprovementStore(conn, key)
    if store.on_trial() is not None:
        return None  # one change at a time, so the next window's metrics stay attributable
    best = max(findings, key=lambda f: f["confidence"])
    if store.find_by_payload(best["payload"]) is not None:
        # This exact payload already has a ledger row. Covers two failure modes found by
        # adversarial review: a REVERTED fact flapping back in forever (measured harmful
        # once is refused for good), and identical low-confidence proposals piling up
        # window after window.
        return None
    iid = store.add(category=best["category"], component=best["component"],
                    lever_type="memory_fact", description=best["description"],
                    payload=best["payload"], confidence=best["confidence"])
    if best["confidence"] < _MIN_APPLY_CONFIDENCE:
        return None  # recorded as a proposal for the record; not trusted enough to self-apply
    baseline = {"turns": chat["turns"], "dissatisfaction": _dissatisfaction(chat)}
    if not store.apply(iid, baseline=baseline):
        return None
    return f"learned a preference: {imp._clean_fact(best['payload'])}"


def _queued_changes(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Undelivered behavior-change announcements (bounded; corrupt config reads empty)."""
    raw = db.meta_get(conn, _PENDING_CHANGES_META_KEY)
    if not raw:
        return []
    try:
        vals = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(vals, list):
        return []
    return [str(v) for v in vals if isinstance(v, str)][:20]  # bounded (P10 #2)


def _queue_changes(conn: duckdb.DuckDBPyConnection, lines: list[str]) -> None:
    """Durably queue announcement lines (append + dedup) until a digest delivers them.

    Written the moment a mutation happens, BEFORE notify/persist can fail — so an
    applied/reverted change can never go unannounced, whatever fails afterwards.
    """
    merged = _queued_changes(conn)
    for line in lines:  # bounded: callers pass at most a few lines per pass
        if line not in merged:
            merged.append(line)
    db.meta_set(conn, _PENDING_CHANGES_META_KEY, json.dumps(merged[:20]))


def _digest(chat: dict, flags: list[str], changes: list[str]) -> str:
    """The user-facing digest — emitted when a flag fired OR the assistant changed itself.

    A behavior change is always worth telling the user about, even in an otherwise clean
    period: silent self-modification is exactly what the approval-gate philosophy rejects.
    """
    assert flags or changes, "digest is only built when something is worth saying"
    head = (f"Self-review of the last period: {chat['turns']} chat turns"
            + (f", median answer {chat['median_ms'] / 1000:.1f}s" if chat["turns"] else "")
            + ".")
    sections = []
    if changes:
        sections.append("What changed:\n" + "\n".join(f"- {c}" for c in changes))
    if flags:
        sections.append("Needs attention:\n" + "\n".join(f"- {f}" for f in flags))
    return head + "\n\n" + "\n\n".join(sections)


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
            return None  # re-check before the closed loop: it mutates memory + improvements
        # Closed loop. Order matters: settle the outstanding trial FIRST (it may revert and
        # free the one-at-a-time slot), then critique + apply at most one new change. Every
        # change line is queued durably the moment it exists — announcements must survive a
        # notify failure or a relock (they are merged into the next digest otherwise).
        changes: list[str] = []
        settled = _settle_trial(conn, key, since, until)
        if settled:
            changes.append(settled)
            _queue_changes(conn, changes)
        if flags:  # nothing flagged -> nothing to critique; the model stays untouched
            findings = _critique(conn, key, scorecard, since, until)
            if locked_check is not None and locked_check():
                return None  # the critique can run ~2 min: never apply after a mid-call relock
            applied = _maybe_apply(conn, key, findings, scorecard["chat"])
            if applied:
                changes.append(applied)
                _queue_changes(conn, changes)
        scorecard["changes"] = changes
        if locked_check is not None and locked_check():
            return None  # re-check before the review write (queued changes survive for later)
        pending = _queued_changes(conn)  # this pass's lines + any undelivered from before
        rid = store.add(since, until, scorecard, len(flags))
        # Digest BEFORE the cadence advance: if notify fails, the stamp stays put and the
        # next tick re-runs the window — a duplicate review row and a retried digest.
        # The digest is this phase's only user-visible surface, so a harmless duplicate
        # beats silently losing it forever (the original order did exactly that).
        # A behavior change (applied or reverted) ALWAYS surfaces, flags or not.
        if (flags or pending) and notify is not None:
            notify("complete", _digest(scorecard["chat"], flags, pending))
            if pending:
                db.meta_set(conn, _PENDING_CHANGES_META_KEY, "[]")  # delivered
        db.meta_set(conn, LAST_RUN_META_KEY, until)
        log.info("self-review complete: window %s..%s, %d flag(s)", since, until, len(flags))
        return {"id": rid, "window_start": since, "window_end": until, "flags": len(flags)}
    except Exception as exc:  # the reviewer is a background nicety — never break a tick
        log.warning("self-review failed: %s", exc)
        return None
