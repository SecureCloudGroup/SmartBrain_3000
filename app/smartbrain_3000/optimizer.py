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
            "SELECT id, created_at, request_type, status, fired, nonce, ciphertext "
            "FROM optimizer_strategies ORDER BY created_at DESC, id DESC LIMIT ?;",
            [_MAX_STRATEGIES_TOTAL * 2],  # slack for rows predating a cap change
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for r in rows:  # bounded by the LIMIT
            body = json.loads(self._aes.decrypt(
                bytes(r[5]), bytes(r[6]), b"optimizer:" + str(r[0]).encode("utf-8")).decode("utf-8"))
            out.append({"id": str(r[0]), "created_at": str(r[1]), "request_type": str(r[2]),
                        "status": str(r[3]), "fired": int(r[4]),
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
