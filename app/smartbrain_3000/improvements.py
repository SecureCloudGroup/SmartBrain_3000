"""Learned improvements: apply, measure, revert (self-improving framework, Phase 3).

This is the first place the framework CHANGES behavior rather than just measuring it,
so every bound the plan promised is enforced here rather than assumed:

- **Reversible-only.** A lever may auto-apply only if it registers an undo. Applying
  captures the undo reference BEFORE it mutates anything; reverting replays it.
- **Data-level only.** ``_AUTO_LEVERS`` is the allowlist — today just ``memory_fact``
  (one learned preference, undone by deleting that row). Everything else can be
  PROPOSED but never self-applied; code-level behavior (tool steering, tiers, the
  cache-critical system-prompt head) is out of reach by construction.
- **One trial at a time.** At most one applied-but-unevaluated improvement exists.
  Without that, two overlapping changes make the next window's metrics unattributable
  and the auto-revert loop would be guessing which one to blame.
- **Measured, then kept or reverted.** Applying snapshots the baseline it must beat;
  the next review with enough evidence compares and reverts on regression.
- **Bounded footprint.** At most ``_MAX_ACTIVE_FACTS`` learned facts alive at once, each
  length-capped and single-line, so the system prompt can never be flooded.

Prompt-injection note: a learned fact is model-written text that reaches every future
system prompt. The critique pass that produces it is fed the USER's own messages only —
never tool results, documents, or web content — so hostile text the assistant merely
*read* cannot propose a permanent instruction. Sanitizing here is the second layer.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .memory import MemoryStore
from .secrets import MASTER_KEY_BYTES

log = logging.getLogger(__name__)

_NONCE_BYTES = 12

CATEGORIES = ("preference", "workflow", "knowledge", "prompt")
COMPONENTS = ("chat", "knowledge", "tools")
LEVER_TYPES = ("memory_fact", "instructions", "schedule", "document")
# The allowlist: levers the reviewer may apply on its own. Everything else is proposal-only.
_AUTO_LEVERS = ("memory_fact",)
STATUSES = ("proposed", "active", "reverted", "rejected", "superseded")

# A learned fact is prefixed so it is identifiable in Settings -> Memory, countable
# without a schema change, and honest about its origin.
LEARNED_PREFIX = "(learned) "
_MAX_FACT_CHARS = 200    # a preference, not an essay — bounds prompt growth
_MIN_FACT_CHARS = 10     # anything shorter is noise, not a preference
_MAX_ACTIVE_FACTS = 10   # ceiling on auto-added facts alive at once (P10 #2)

_LIST_LIMIT = 200  # max improvements returned by a listing (verifiable bound)


def _clean_fact(text: str) -> str:
    """Normalize model-written preference text: single line, whitespace-collapsed, bounded.

    Collapsing whitespace also strips newlines/tabs, so a payload cannot smuggle extra
    pseudo-sections ("\\n\\nSystem: ...") into the composed system prompt.
    """
    return " ".join(str(text or "").split())[:_MAX_FACT_CHARS]


class ImprovementStore:
    """AES-256-GCM improvement store over DuckDB's ``improvements`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._key = master_key
        self._aes = AESGCM(master_key)

    # --- persistence --------------------------------------------------------

    def add(self, *, category: str, component: str, lever_type: str, description: str,
            payload: str, confidence: float, status: str = "proposed") -> str:
        """Record an improvement (default: merely proposed); return its id."""
        assert category in CATEGORIES, "unknown improvement category"
        assert component in COMPONENTS, "unknown component"
        assert lever_type in LEVER_TYPES, "unknown lever type"
        assert status in STATUSES, "unknown status"
        assert description, "description required"
        iid = str(uuid.uuid4())
        body = {"description": description, "payload": payload,
                "prior_state": None, "applied_ref": None, "baseline": None}
        nonce, ciphertext = self._sealed(iid, body)
        self._conn.execute(
            "INSERT INTO improvements (id, category, component, lever_type, status, confidence,"
            " nonce, ciphertext) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            [iid, category, component, lever_type, status, float(confidence), nonce, ciphertext],
        )
        return iid

    def get(self, iid: str) -> dict | None:
        """One improvement with its decrypted body, or None."""
        assert iid, "improvement id required"
        row = self._conn.execute(
            "SELECT id, created_at, category, component, lever_type, status, confidence,"
            " applied_at, evaluated_at, reverted_at, nonce, ciphertext FROM improvements WHERE id = ?;",
            [iid],
        ).fetchone()
        return None if row is None else self._row(row)

    def list(self, limit: int = 50) -> list[dict]:
        """Recent improvements, newest first (bounded)."""
        capped = min(max(int(limit), 1), _LIST_LIMIT)
        rows = self._conn.execute(
            "SELECT id, created_at, category, component, lever_type, status, confidence,"
            " applied_at, evaluated_at, reverted_at, nonce, ciphertext FROM improvements"
            " ORDER BY created_at DESC, id DESC LIMIT ?;",
            [capped],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        return [self._row(r) for r in rows]  # bounded by capped

    def on_trial(self) -> dict | None:
        """The single applied-but-unevaluated improvement, if one exists.

        Its existence is what blocks a further auto-apply: overlapping changes would
        make the next window's metrics unattributable.
        """
        row = self._conn.execute(
            "SELECT id, created_at, category, component, lever_type, status, confidence,"
            " applied_at, evaluated_at, reverted_at, nonce, ciphertext FROM improvements"
            " WHERE status = 'active' AND evaluated_at IS NULL ORDER BY applied_at ASC LIMIT 1;"
        ).fetchone()
        return None if row is None else self._row(row)

    def count_active_facts(self) -> int:
        """How many learned facts are LIVE IN MEMORY right now (the footprint ceiling).

        Counts actual "(learned) " memory rows, not ledger rows: the user can hand-delete
        a learned fact in Settings -> Memory, and a ledger-based count would keep those
        phantom slots occupied forever, silently bricking the auto-apply ceiling.
        """
        memory = MemoryStore(self._conn, self._key)
        return sum(1 for m in memory.list_memories() if m["text"].startswith(LEARNED_PREFIX))

    def find_by_payload(self, payload: str) -> dict | None:
        """The most recent ledger row whose payload matches (cleaned, case-insensitive).

        The lookback is bounded (newest ``_LIST_LIMIT`` rows) — enough that a payload
        measured harmful and reverted cannot flap back within any realistic horizon.
        """
        want = _clean_fact(payload).lower()
        if not want:
            return None
        for row in self.list(_LIST_LIMIT):  # bounded
            if _clean_fact(row["body"].get("payload", "")).lower() == want:
                return row
        return None

    def reconcile(self) -> int:
        """Settle ledger rows whose applied fact the user has hand-deleted.

        A missing "(learned) " memory row means the user rejected the change themselves
        (Settings -> Memory is exactly where the ledger points them). The row becomes
        ``rejected`` — settled, off trial, and its ceiling slot freed. Returns how many
        rows were reconciled.
        """
        memory = MemoryStore(self._conn, self._key)
        live_ids = {m["id"] for m in memory.list_memories()}
        fixed = 0
        for row in self.list(_LIST_LIMIT):  # bounded
            if row["status"] != "active" or row["lever_type"] != "memory_fact":
                continue
            ref = row["body"].get("applied_ref") or {}
            mid = ref.get("memory_id")
            if mid and mid not in live_ids:
                self._conn.execute(
                    "UPDATE improvements SET status = 'rejected',"
                    " evaluated_at = COALESCE(evaluated_at, now()) WHERE id = ?;",
                    [row["id"]],
                )
                fixed += 1
        return fixed

    # --- the closed loop ----------------------------------------------------

    def apply(self, iid: str, *, baseline: dict) -> bool:
        """Execute a proposed improvement's lever and mark it active + on trial.

        ``baseline`` is the metric snapshot the change must not make worse. Returns False
        (leaving the row proposed) when the lever is not auto-applicable, its payload fails
        sanitizing, or the footprint ceiling is reached — never raises for those cases.
        """
        assert iid, "improvement id required"
        assert isinstance(baseline, dict), "baseline snapshot required"
        row = self.get(iid)
        if row is None or row["status"] != "proposed":
            return False
        if row["lever_type"] not in _AUTO_LEVERS:
            return False  # proposal-only lever: a human decides
        # The lever mutation and the ledger UPDATE must land together: a fact that exists
        # with no active ledger row would be live, untracked, and outside the auto-revert
        # loop forever. All-or-nothing via explicit transaction (the vault_sync pattern).
        self._conn.execute("BEGIN TRANSACTION;")
        try:
            applied_ref, prior_state = self._do_apply(row)
            if applied_ref is None:
                self._conn.execute("ROLLBACK;")
                return False
            body = dict(row["body"])
            body.update({"applied_ref": applied_ref, "prior_state": prior_state, "baseline": baseline})
            nonce, ciphertext = self._sealed(iid, body)
            self._conn.execute(
                "UPDATE improvements SET status = 'active', applied_at = now(), nonce = ?, ciphertext = ?"
                " WHERE id = ?;",
                [nonce, ciphertext, iid],
            )
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")  # no half-applied state, whatever failed
            raise
        log.info("self-improve: applied %s (%s)", row["lever_type"], iid)
        return True

    def revert(self, iid: str) -> bool:
        """Undo an applied improvement using its captured reference; mark it reverted."""
        assert iid, "improvement id required"
        row = self.get(iid)
        if row is None or row["status"] != "active":
            return False
        self._do_revert(row)
        self._conn.execute(
            "UPDATE improvements SET status = 'reverted', reverted_at = now(),"
            " evaluated_at = COALESCE(evaluated_at, now()) WHERE id = ?;",
            [iid],
        )
        log.info("self-improve: reverted %s (%s)", row["lever_type"], iid)
        return True

    def mark_evaluated(self, iid: str) -> None:
        """Settle a trial without reverting (the change is kept)."""
        assert iid, "improvement id required"
        self._conn.execute("UPDATE improvements SET evaluated_at = now() WHERE id = ?;", [iid])

    def mark_rejected(self, iid: str) -> None:
        """Settle a proposal the user turned down — never offered or re-parked again."""
        assert iid, "improvement id required"
        self._conn.execute(
            "UPDATE improvements SET status = 'rejected',"
            " evaluated_at = COALESCE(evaluated_at, now()) WHERE id = ? AND status = 'proposed';",
            [iid],
        )

    # --- levers -------------------------------------------------------------

    def _do_apply(self, row: dict) -> tuple[dict | None, dict | None]:
        """Run the lever. Returns ``(applied_ref, prior_state)``; ``(None, None)`` on refusal.

        ``applied_ref`` is what revert needs to undo a CREATE; ``prior_state`` is what
        revert needs to restore an OVERWRITE. Every auto-lever must supply one of them.
        """
        lever = row["lever_type"]
        if lever == "memory_fact":
            text = _clean_fact(row["body"].get("payload", ""))
            if len(text) < _MIN_FACT_CHARS:
                log.debug("self-improve: fact payload too short after cleaning; refused")
                return None, None
            if self.count_active_facts() >= _MAX_ACTIVE_FACTS:
                log.info("self-improve: learned-fact ceiling reached; refusing to add another")
                return None, None
            memory = MemoryStore(self._conn, self._key)
            fact = LEARNED_PREFIX + text
            existing = memory.list_memories()
            if any(m["text"].strip().lower() == fact.strip().lower() for m in existing):
                return None, None  # already known — adding it twice only bloats the prompt
            mid = memory.add_memory(fact)
            return {"memory_id": mid}, None
        return None, None  # unreachable for _AUTO_LEVERS; belt-and-suspenders

    def _do_revert(self, row: dict) -> None:
        """Undo a lever. Best-effort per lever: a missing target is already 'undone'."""
        lever = row["lever_type"]
        ref = row["body"].get("applied_ref") or {}
        if lever == "memory_fact" and ref.get("memory_id"):
            MemoryStore(self._conn, self._key).delete_memory(str(ref["memory_id"]))

    # --- encryption ---------------------------------------------------------

    def _sealed(self, iid: str, body: dict) -> tuple[bytes, bytes]:
        assert iid, "improvement id required"
        assert isinstance(body, dict), "body must be a dict"
        nonce = os.urandom(_NONCE_BYTES)
        return nonce, self._aes.encrypt(
            nonce, json.dumps(body).encode("utf-8"), b"improvement:" + iid.encode("utf-8"))

    def _row(self, row: tuple) -> dict:
        iid = str(row[0])
        body = json.loads(
            self._aes.decrypt(bytes(row[10]), bytes(row[11]), b"improvement:" + iid.encode("utf-8")).decode("utf-8")
        )
        return {"id": iid, "created_at": str(row[1]), "category": str(row[2]),
                "component": str(row[3]), "lever_type": str(row[4]), "status": str(row[5]),
                "confidence": float(row[6]),
                "applied_at": None if row[7] is None else str(row[7]),
                "evaluated_at": None if row[8] is None else str(row[8]),
                "reverted_at": None if row[9] is None else str(row[9]),
                "description": body.get("description", ""), "body": body}
