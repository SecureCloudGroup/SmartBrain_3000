"""Scheduled agent prompts + the background runner (H5).

A schedule stores an encrypted prompt and a cadence; when due it runs an agent
turn (H4c) — OBSERVE tools auto-complete, dangerous ones park as approval tiles
for the user to resolve later. Cadence metadata (enabled / interval / next_run)
is plaintext so the runner finds due rows without decrypting.

Honest constraint: schedules fire ONLY while the vault is unlocked — a locked
vault has no master key and cannot decrypt or act. The runner skips when locked.
The background loop ticks on a worker thread using a per-thread DuckDB cursor
(DuckDB connections are not shared across threads).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import duckdb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import agent, consent, gateway, ingest, tools, usage
from .approvals import ApprovalStore
from .audit import AuditLog
from .kb import KnowledgeBase
from .memory import MemoryStore
from .planner import Planner
from .secrets import MASTER_KEY_BYTES
from .vaults import VaultStore

log = logging.getLogger(__name__)

_NONCE_BYTES = 12
_LIST_LIMIT = 200  # max schedules listed (verifiable bound)
_MAX_PER_TICK = 10  # max schedules fired per tick (verifiable bound)
# The background indexer works to a TIME budget, not a document count: 5-per-tick meant a 100-file
# drop took ~10 minutes to index. 20s of a 30s tick drains a backlog steadily while still leaving
# the single-threaded local model free most of the time (and it yields entirely to a live chat).
_AUTO_REINDEX_SECONDS = 20.0
_AUTO_REINDEX_MAX_DOCS = 500  # verifiable per-tick ceiling (P10 #2); the budget usually binds first
_EAGER_REINDEX_MAX = 50_000  # one-shot post-unlock backfill cap (matches kb._REINDEX_SCAN_LIMIT)
_SCHEDULE_WORKERS = 4  # H1: parallel agent turns per tick (bounded thread pool)
# Background turns get a generous per-request budget: a scheduled turn is often the first
# request to hit a local model since boot, so it eats the cold-load time. Matched to the
# gateway's local-provider timeout so bifrost — not the app — decides the final deadline.
_AGENT_TURN_TIMEOUT = 300.0
# Gateway circuit breaker (B11): when embeddings or scheduled turns repeatedly
# fail (Ollama/Bifrost down), suppress further attempts for a cooldown window
# instead of retrying every 30s tick and flooding the logs.
_BREAKER_TRIP_AFTER = 3       # consecutive failures before tripping
_BREAKER_COOLDOWN_SECS = 300  # skip-attempts window once tripped (5 minutes)


def _close_cursor(cursor) -> None:
    """Close a per-thread DuckDB cursor, releasing its file handle (so the DB file /
    test temp dir can be cleaned). Best-effort: a double-close must never raise."""
    if cursor is None:
        return
    try:
        cursor.close()
    except Exception:  # already closed / DB torn down — nothing to release
        pass


class _Breaker:
    """Module-singleton breaker state (avoids ``global`` for mutation)."""

    __slots__ = ("fails", "until", "warned")

    def __init__(self) -> None:
        self.fails = 0           # consecutive failure count (mutated under _BREAKER_LOCK; the tick runs workers in parallel)
        self.until = 0.0         # monotonic-time when the breaker stops suppressing
        self.warned = False      # debounce: only log the "tripped" warning once per trip


_BREAKER = _Breaker()
# Guards the breaker's read-modify-write: run_schedule calls _breaker_record OUTSIDE
# _RUN_LOCK, and a tick runs up to _SCHEDULE_WORKERS turns in parallel, so without this
# lock failure increments race (breaker trips late) and the debounced warning can double-fire.
_BREAKER_LOCK = threading.Lock()
# Serializes the claim-and-advance step across the background tick and a manual
# /run (both run in the same process, on different threads) so a schedule can
# never fire twice. The long agent turn runs OUTSIDE this lock.
_RUN_LOCK = threading.Lock()


class ScheduleStore:
    """AES-256-GCM schedule store over DuckDB's ``schedules`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, master_key: bytes) -> None:
        assert conn is not None, "connection must be open"
        assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
        self._conn = conn
        self._aes = AESGCM(master_key)

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """The underlying cursor. Invariant: all stores for one agent turn share ONE cursor."""
        return self._conn

    def add_schedule(self, title: str, prompt: str, interval_minutes: int, start_in_minutes: int, model: str | None) -> str:
        """Create a schedule; next run is ``start_in_minutes`` from now."""
        assert title and prompt, "title + prompt required"
        assert interval_minutes >= 0 and start_in_minutes >= 0, "minutes must be non-negative"
        sid = str(uuid.uuid4())
        nonce, ciphertext = self._seal(sid, {"title": title, "prompt": prompt, "model": model})
        self._conn.execute(
            "INSERT INTO schedules (id, nonce, ciphertext, interval_minutes, next_run) "
            "VALUES (?, ?, ?, ?, now() + to_minutes(?));",
            [sid, nonce, ciphertext, interval_minutes, start_in_minutes],
        )
        return sid

    def list_schedules(self) -> list[dict]:
        """Return all schedules (decrypted), soonest first."""
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext, enabled, interval_minutes, next_run, last_run "
            "FROM schedules ORDER BY next_run ASC LIMIT ?;",
            [_LIST_LIMIT],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        return [self._row(r) for r in rows]  # bounded by _LIST_LIMIT

    def get_schedule(self, sid: str) -> dict | None:
        """Return one schedule, or None if absent."""
        assert sid, "schedule id required"
        row = self._conn.execute(
            "SELECT id, nonce, ciphertext, enabled, interval_minutes, next_run, last_run "
            "FROM schedules WHERE id = ?;",
            [sid],
        ).fetchone()
        return None if row is None else self._row(row)

    def due_schedules(self) -> list[dict]:
        """Return enabled schedules whose next_run has passed (bounded)."""
        rows = self._conn.execute(
            "SELECT id, nonce, ciphertext, enabled, interval_minutes, next_run, last_run "
            "FROM schedules WHERE enabled AND next_run <= now() ORDER BY next_run ASC LIMIT ?;",
            [_MAX_PER_TICK],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        return [self._row(r) for r in rows]

    def is_due(self, sid: str) -> bool:
        """True if the schedule still exists, is enabled, and next_run has passed."""
        assert sid, "schedule id required"
        row = self._conn.execute(
            "SELECT 1 FROM schedules WHERE id = ? AND enabled AND next_run <= now();", [sid]
        ).fetchone()
        return row is not None

    def update_schedule(self, sid: str, title: str, prompt: str, interval_minutes: int, model: str | None) -> None:
        """Replace a schedule's content + interval."""
        assert sid and title and prompt, "id + title + prompt required"
        nonce, ciphertext = self._seal(sid, {"title": title, "prompt": prompt, "model": model})
        self._conn.execute(
            "UPDATE schedules SET nonce = ?, ciphertext = ?, interval_minutes = ? WHERE id = ?;",
            [nonce, ciphertext, interval_minutes, sid],
        )

    def set_enabled(self, sid: str, enabled: bool) -> None:
        """Enable or disable a schedule."""
        assert sid, "schedule id required"
        self._conn.execute("UPDATE schedules SET enabled = ? WHERE id = ?;", [enabled, sid])

    def delete_schedule(self, sid: str) -> None:
        """Delete a schedule and its run history (no error if absent).

        schedule_runs has no DB-level FK to schedules, so cascade in code (children
        first) — matching kb.delete / history.delete_conversation — otherwise orphaned
        encrypted run rows accumulate forever and ride into every backup.
        """
        assert sid, "schedule id required"
        self._conn.execute("DELETE FROM schedule_runs WHERE schedule_id = ?;", [sid])
        self._conn.execute("DELETE FROM schedules WHERE id = ?;", [sid])

    def mark_ran(self, sid: str, interval_minutes: int) -> None:
        """Advance after a run: reschedule by the interval, or disable a one-shot."""
        assert sid, "schedule id required"
        if interval_minutes > 0:
            self._conn.execute(
                "UPDATE schedules SET last_run = now(), next_run = now() + to_minutes(?) WHERE id = ?;",
                [interval_minutes, sid],
            )
        else:
            self._conn.execute(
                "UPDATE schedules SET last_run = now(), enabled = false WHERE id = ?;", [sid]
            )

    def record_run(self, sid: str, status: str, message: str = "", error: str | None = None) -> str:
        """Persist a run's outcome so the user can read scheduled output (and see failures)."""
        assert sid and status, "schedule id + status required"
        rid = str(uuid.uuid4())
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps({"message": message or "", "error": error}).encode("utf-8")
        ciphertext = self._aes.encrypt(nonce, plaintext, b"schedule_run:" + rid.encode("utf-8"))
        self._conn.execute(
            # seen=false: a freshly-fired run is unseen until the user opens the Scheduled updates feed.
            "INSERT INTO schedule_runs (id, schedule_id, status, nonce, ciphertext, seen) VALUES (?, ?, ?, ?, ?, false);",
            [rid, sid, status, nonce, ciphertext],
        )
        return rid

    def list_runs(self, sid: str, limit: int = 20) -> list[dict]:
        """Return recent runs for a schedule (decrypted message/error), newest first."""
        assert sid, "schedule id required"
        capped = min(max(int(limit), 1), 100)
        rows = self._conn.execute(
            "SELECT id, ran_at, status, nonce, ciphertext FROM schedule_runs "
            "WHERE schedule_id = ? ORDER BY ran_at DESC LIMIT ?;",
            [sid, capped],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for r in rows:  # bounded by capped
            body = json.loads(
                self._aes.decrypt(bytes(r[3]), bytes(r[4]), b"schedule_run:" + str(r[0]).encode("utf-8")).decode("utf-8")
            )
            out.append({"id": str(r[0]), "ran_at": str(r[1]), "status": str(r[2]),
                        "message": body.get("message", ""), "error": body.get("error")})
        return out

    def recent_runs(self, limit: int = 50) -> list[dict]:
        """Recent runs across ALL schedules (decrypted), newest first, each tagged with its
        schedule title — the feed behind the Schedules 'Output' tab. The JOIN drops any orphaned
        run whose schedule was deleted (delete_schedule cascades, so orphans shouldn't exist;
        the JOIN is the belt-and-suspenders and also supplies the still-encrypted title)."""
        capped = min(max(int(limit), 1), 200)
        rows = self._conn.execute(
            "SELECT r.id, r.ran_at, r.status, r.nonce, r.ciphertext, r.schedule_id, s.nonce, s.ciphertext, r.seen "
            "FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id "
            "ORDER BY r.ran_at DESC LIMIT ?;",
            [capped],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        out: list[dict] = []
        for r in rows:  # bounded by capped
            rid, sid = str(r[0]), str(r[5])
            run = json.loads(
                self._aes.decrypt(bytes(r[3]), bytes(r[4]), b"schedule_run:" + rid.encode("utf-8")).decode("utf-8")
            )
            sched = self._open(sid, bytes(r[6]), bytes(r[7]))
            out.append({"id": rid, "schedule_id": sid, "schedule_title": sched["title"],
                        "ran_at": str(r[1]), "status": str(r[2]),
                        "message": run.get("message", ""), "error": run.get("error"), "seen": bool(r[8])})
        return out

    def unseen_count(self) -> int:
        """Count run outputs the user hasn't opened in the Scheduled-updates feed (nav badge).

        Plaintext-only query (no decrypt). JOINs to schedules so a cascade-deleted schedule's
        orphan runs (which shouldn't exist) can never inflate the badge — mirrors recent_runs.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id WHERE NOT r.seen;"
        ).fetchone()
        return 0 if row is None else int(row[0])

    def mark_all_seen(self) -> int:
        """Mark every unseen run as seen (the user opened the feed). Returns how many were unseen."""
        n = self.unseen_count()
        self._conn.execute("UPDATE schedule_runs SET seen = true WHERE NOT seen;")
        return n

    def _row(self, row: tuple) -> dict:
        body = self._open(str(row[0]), bytes(row[1]), bytes(row[2]))
        return {
            "id": str(row[0]),
            "title": body["title"],
            "prompt": body["prompt"],
            "model": body.get("model"),
            "enabled": bool(row[3]),
            "interval_minutes": int(row[4]),
            "next_run": str(row[5]),
            "last_run": None if row[6] is None else str(row[6]),
        }

    def _seal(self, sid: str, body: dict) -> tuple[bytes, bytes]:
        assert sid, "schedule id required"
        assert isinstance(body, dict), "body must be a dict"
        nonce = os.urandom(_NONCE_BYTES)
        return nonce, self._aes.encrypt(nonce, json.dumps(body).encode("utf-8"), b"schedule:" + sid.encode("utf-8"))

    def _open(self, sid: str, nonce: bytes, ciphertext: bytes) -> dict:
        assert len(nonce) == _NONCE_BYTES, "nonce must be 12 bytes"
        body = json.loads(self._aes.decrypt(nonce, ciphertext, b"schedule:" + sid.encode("utf-8")).decode("utf-8"))
        assert "title" in body and "prompt" in body, "schedule body malformed"
        return body


def _grounded_messages(ctx, prompt: str) -> list[dict]:
    """Wrap a scheduled prompt with the same grounding the chat path injects.

    Scheduled runs have no Request, so build the system message directly: the base
    grounding (current time + the trust-critical "only a tool performs an action")
    plus the user's profile/facts. Without it the model can log a status string as a
    fact or claim work no tool performed — exactly what an ungrounded schedule did.
    """
    from .chat_routes import _base_system_prompt  # lazy: keep the route<-domain edge off import time
    assert ctx is not None, "tool context required"
    assert prompt, "prompt required"
    parts = [_base_system_prompt()]
    profile = ctx.memory.system_prompt() if ctx.memory is not None else None
    if profile:
        parts.append(profile)
    return [{"role": "system", "content": "\n\n".join(parts)}, {"role": "user", "content": prompt}]


def _record_run_safe(store: ScheduleStore, sid: str, status: str, message: str, error: str | None) -> None:
    """Persist a run outcome; a recording failure must never crash the runner."""
    assert store is not None, "schedule store required"
    try:
        store.record_run(sid, status, message=message, error=error)
    except Exception as exc:  # recording is best-effort — never mask the real result
        log.warning("failed to record scheduled run %s: %s", sid, exc)


def run_schedule(ctx, audit, approvals, store: ScheduleStore, schedule: dict, *,
                 require_due: bool = False, locked_check=None) -> dict:
    """Atomically claim + advance a schedule, then run its agent turn.

    The claim (re-read of the live row + ``mark_ran``) is serialized under
    ``_RUN_LOCK`` so the background tick and a manual /run can never fire the
    same schedule twice; the long agent turn runs OUTSIDE the lock. ``require_due``
    (the tick) skips a row that is no longer due; a manual /run leaves it False.
    ``locked_check`` (optional, used by the parallel tick) is consulted inside the
    same lock window as the claim; if it returns True, the schedule is skipped
    without advancing — this is what stops further fires once the vault re-locks.
    """
    assert schedule and schedule.get("id"), "schedule required"
    assert store is not None, "schedule store required"
    sid = schedule["id"]
    with _RUN_LOCK:  # claim window only — never held across the agent turn
        if locked_check is not None and locked_check():
            return {"status": "skipped", "detail": "vault locked"}
        fresh = store.get_schedule(sid)
        if fresh is None:
            return {"status": "skipped", "detail": "schedule deleted"}
        if require_due and not store.is_due(sid):
            return {"status": "skipped", "detail": "no longer due"}
        store.mark_ran(sid, fresh["interval_minutes"])  # advance with the FRESH interval
    routes = gateway.load_routes(store.conn)
    # Prefer the "agent" route (a user may point background tasks at a tool-capable local
    # model, e.g. MLX gemma-4); fall back to the Chat model when it's unset.
    model = fresh.get("model") or gateway.resolve_model("agent", routes) or gateway.resolve_model("chat", routes)
    if not model:
        return {"status": "error", "detail": "no model configured for scheduled run"}
    conn = store.conn

    def sink(used_model: str, response: object) -> None:  # record scheduled-run spend
        usage.record_response(conn, used_model, response)

    try:
        result = agent.run_turn(
            ctx, audit, approvals,
            messages=_grounded_messages(ctx, fresh["prompt"]),
            model=model, conversation_id=None, turn_id=uuid.uuid4().hex, usage_sink=sink,
            # Honor remembered writes (no user at the tile) — EXCEPT schedule-mutating tools,
            # which must never auto-run in an autonomous turn (they'd let an injected prompt
            # spawn/rewrite self-perpetuating schedules); those always park for human approval.
            auto_approve=consent.remembered(conn) - tools.SCHEDULE_WRITE_TOOLS,
            timeout=_AGENT_TURN_TIMEOUT,  # tolerate a cold local-model load (see constant)
            result_cap=gateway.result_cap_for(conn, model),
        )
        _breaker_record(success=True)  # B11: a successful turn resets the breaker
        _record_run_safe(store, sid, str(result.get("status", "complete")), str(result.get("message", "")), None)
        return result
    except Exception as exc:  # a scheduled run must never crash the runner
        log.warning("scheduled run %s failed: %s", sid, exc)
        _breaker_record(success=False)  # B11: count gateway/agent failures
        _record_run_safe(store, sid, "error", "", str(exc))
        return {"status": "error", "detail": str(exc)}


def _breaker_open() -> bool:
    """B11: True while the gateway-down cooldown is suppressing attempts."""
    assert _BREAKER_COOLDOWN_SECS > 0, "cooldown must be positive"
    assert _BREAKER_TRIP_AFTER > 0, "trip threshold must be positive"
    return time.monotonic() < _BREAKER.until


def _breaker_record(success: bool) -> None:
    """B11: count failures; trip + warn (debounced) once threshold is hit; reset on success.

    Parallel tick workers call this concurrently, so the whole read-modify-write runs
    under _BREAKER_LOCK — otherwise increments are lost (late trip) and the debounced
    warning can fire more than once.
    """
    assert isinstance(success, bool), "success must be bool"
    assert _BREAKER_TRIP_AFTER > 0, "trip threshold must be positive"
    with _BREAKER_LOCK:
        if success:
            if _BREAKER.fails or _BREAKER.warned:
                log.info("gateway breaker reset")
            _BREAKER.fails = 0
            _BREAKER.until = 0.0
            _BREAKER.warned = False
            return
        _BREAKER.fails += 1
        if _BREAKER.fails >= _BREAKER_TRIP_AFTER:
            _BREAKER.until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            if not _BREAKER.warned:  # debounce: one warning per trip
                log.warning("gateway breaker tripped after %d failures; suppressing for %ds",
                            _BREAKER.fails, _BREAKER_COOLDOWN_SECS)
                _BREAKER.warned = True


def _auto_reindex(cursor, key: bytes) -> None:
    """Background indexer: drain the embedding backlog so uploaded documents become semantically
    searchable on their own. Bounded; never raises. Honors the B11 breaker.

    Works to a TIME budget rather than a document count. It used to do 5 documents per 30-second
    tick, so a 100-file drop took ~10 minutes to finish indexing and 1,000 files took over an hour —
    while the upload path embedded inline and blocked. Now uploads return immediately and this
    drains steadily, spending at most ``_AUTO_REINDEX_SECONDS`` of each tick so the single-threaded
    local model is never held away from chat for long.
    """
    if _breaker_open():
        log.debug("auto-reindex skipped: breaker open")
        return
    try:
        model = gateway.embed_model(cursor)
        # Yield to any in-flight foreground chat: a local model server serves one request at a
        # time, so starting a backfill while the user is chatting would make them wait behind the
        # embed. Skip this tick if a chat holds the model — a later tick (or a manual Reindex)
        # backfills. (Each embed below still serializes through gateway._serialized, so even if a
        # chat starts mid-backfill nothing collides; this peek just avoids the common-case wait.)
        if not gateway.local_available():
            log.debug("auto-reindex skipped: local model busy with a foreground request")
            return
        embedded, _skipped, failed, _err = ingest.reindex_pending(
            KnowledgeBase(cursor, key), model,
            limit=_AUTO_REINDEX_MAX_DOCS, budget_seconds=_AUTO_REINDEX_SECONDS,
        )
        if embedded or failed:
            log.info("auto-reindex: %d embedded, %d failed", embedded, failed)
        _breaker_record(success=failed == 0)
    except Exception as exc:  # embeddings optional — never let it break a tick
        log.debug("auto-reindex skipped: %s", exc)
        _breaker_record(success=False)


def eager_reindex(cursor, key: bytes) -> None:
    """One-shot full backfill when the embeddings table is empty but documents exist.

    The destructive migration 13->14 drops + recreates ``embeddings``, so after an
    upgrade the trickle backfill (``_AUTO_REINDEX_PER_TICK`` per tick) takes
    ~minutes-per-doc to recover semantic search. Runs on the scheduler's FIRST tick
    after unlock (using the tick's managed cursor) — not a per-unlock daemon thread,
    so it can't leak a cursor past teardown. Naturally one-shot: once embeddings
    exist, the empty-check below returns early. Gateway outage is logged, not fatal.
    """
    assert cursor is not None and key, "cursor + key required"
    try:
        doc_count = cursor.execute("SELECT COUNT(*) FROM documents;").fetchone()[0]
        embed_count = cursor.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0]
        if doc_count == 0 or embed_count > 0:
            return  # nothing to backfill, or trickle is already covering it
        model = gateway.embed_model(cursor)
        bound = min(int(doc_count), _EAGER_REINDEX_MAX)
        assert bound >= 1, "eager bound must be at least one doc"
        embedded, _skipped, failed, _err = ingest.reindex_pending(
            KnowledgeBase(cursor, key), model, limit=bound
        )
        log.info("eager-reindex: %d embedded, %d failed (of %d docs)", embedded, failed, doc_count)
    except Exception as exc:  # gateway down / model missing — best-effort
        log.warning("eager-reindex skipped: %s", exc)


def _run_one(app, key: bytes, session: str, schedule: dict) -> dict:
    """H1 worker: build per-thread cursor + stores, then run one due schedule.

    DuckDB cursors are NOT thread-safe, so each worker owns its own cursor and
    its own per-turn stores (the invariant: all stores for one agent turn share
    ONE cursor). The agent turn's own errors are caught inside ``run_schedule``;
    infrastructure errors here propagate to the caller (matches pre-H1 behavior).
    """
    assert app is not None and key is not None and session, "worker args required"
    assert schedule and schedule.get("id"), "schedule required"
    cursor = app.state.db.cursor()  # per-thread cursor (DuckDB cursors are not shared across threads)
    assert cursor is not None, "per-thread cursor required"
    try:
        store = ScheduleStore(cursor, key)
        ctx = tools.ToolContext(
            kb=KnowledgeBase(cursor, key), planner=Planner(cursor, key),
            memory=MemoryStore(cursor, key), email=getattr(app.state, "email", None),
            schedules=store,  # same per-thread cursor as the other stores (turn-cursor invariant)
            vaults=VaultStore(cursor, key),  # so KB tools can tag imported-vault content
        )
        audit = AuditLog(cursor, key)
        approvals = ApprovalStore(cursor, key, session)
        return run_schedule(
            ctx, audit, approvals, store, schedule, require_due=True,
            locked_check=lambda: getattr(app.state, "master_key", None) is None,
        )
    finally:
        _close_cursor(cursor)  # release the worker's per-thread cursor


def tick(app) -> int:
    """Fire all due schedules (only while unlocked); return how many actually ran.

    Snapshots master_key + session_id together and bails if either is missing
    (the vault is locked). Independent due schedules run in parallel on a bounded
    pool (``_SCHEDULE_WORKERS``); each worker builds its OWN per-thread cursor
    (DuckDB cursors are not thread-safe). Claim + ``mark_ran`` stay serialized
    under ``_RUN_LOCK`` inside ``run_schedule`` so a schedule cannot double-fire,
    and ``locked_check`` inside that lock stops further fires once the vault
    re-locks. B11: skip schedule firing while the gateway breaker is open.
    """
    assert app is not None, "app required"
    key = getattr(app.state, "master_key", None)
    session = getattr(app.state, "session_id", None)
    if key is None or session is None:
        return 0  # locked — nothing can decrypt or act
    cursor = app.state.db.cursor()
    assert cursor is not None, "per-thread cursor required"
    try:
        eager_reindex(cursor, key)  # one-shot post-upgrade catch-up (empty embeddings + docs)
        _auto_reindex(cursor, key)  # keep semantic search current without a manual Reindex
        if _breaker_open():  # B11: gateway is known-bad — don't pile turns onto a dead model
            return 0
        store = ScheduleStore(cursor, key)
        due = store.due_schedules()
        assert len(due) <= _MAX_PER_TICK, "due set must respect the per-tick bound"
        if not due:
            return 0
        fired = 0
        workers = min(_SCHEDULE_WORKERS, len(due))  # bounded pool; never larger than work
        assert 1 <= workers <= _SCHEDULE_WORKERS, "worker count must be within bounds"
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sched") as pool:
            futures = [pool.submit(_run_one, app, key, session, s) for s in due]
            assert len(futures) == len(due), "must submit exactly one future per due schedule"
            for fut in futures:  # bounded by _MAX_PER_TICK
                result = fut.result()  # run_schedule wraps agent errors; infra errors propagate
                if result.get("status") != "skipped":
                    fired += 1
        return fired
    finally:
        _close_cursor(cursor)  # release the tick's per-thread cursor
