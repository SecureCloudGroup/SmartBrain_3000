"""Tests for the scheduler store + runner (H5). The agent turn is mocked."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import duckdb
import pytest

from smartbrain_3000 import agent, consent, gateway, scheduler, tools
from smartbrain_3000 import db as dbmod
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import gen_master_key


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    # B11: each test starts with a fresh breaker so state doesn't bleed across tests.
    scheduler._BREAKER.fails = 0
    scheduler._BREAKER.until = 0.0
    scheduler._BREAKER.warned = False


def _store():
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return ScheduleStore(conn, key), conn, key


# --- store CRUD -----------------------------------------------------------

def test_add_list_get_roundtrip() -> None:
    store, _, _ = _store()
    sid = store.add_schedule("Daily digest", "summarize my tasks", interval_minutes=1440, start_in_minutes=0, model="ollama/x")
    got = store.get_schedule(sid)
    assert got["title"] == "Daily digest" and got["prompt"] == "summarize my tasks"
    assert got["model"] == "ollama/x" and got["interval_minutes"] == 1440 and got["enabled"]
    assert [s["id"] for s in store.list_schedules()] == [sid]


def test_get_missing_is_none() -> None:
    store, _, _ = _store()
    assert store.get_schedule("nope") is None


def test_update_and_set_enabled_and_delete() -> None:
    store, _, _ = _store()
    sid = store.add_schedule("t", "p", interval_minutes=0, start_in_minutes=0, model=None)
    store.update_schedule(sid, "t2", "p2", interval_minutes=60, model="m")
    got = store.get_schedule(sid)
    assert got["title"] == "t2" and got["prompt"] == "p2" and got["interval_minutes"] == 60
    store.set_enabled(sid, False)
    assert store.get_schedule(sid)["enabled"] is False
    store.delete_schedule(sid)
    assert store.get_schedule(sid) is None


def test_delete_schedule_cascades_run_history() -> None:
    # schedule_runs has no DB-level FK, so delete must cascade in code — otherwise orphaned
    # encrypted run rows accumulate forever and ride into every backup.
    store, conn, _ = _store()
    sid = store.add_schedule("o", "p", interval_minutes=0, start_in_minutes=0, model=None)
    store.record_run(sid, "complete", message="run body", error=None)
    store.record_run(sid, "error", message="", error="boom")
    assert conn.execute("SELECT COUNT(*) FROM schedule_runs WHERE schedule_id = ?;", [sid]).fetchone()[0] == 2
    store.delete_schedule(sid)
    assert store.get_schedule(sid) is None
    assert conn.execute("SELECT COUNT(*) FROM schedule_runs WHERE schedule_id = ?;", [sid]).fetchone()[0] == 0


def test_delete_schedule_cannot_remove_the_reserved_vault_carrier() -> None:
    # get/list already hide the vault-updates carrier; delete_schedule must refuse it too, or a
    # DELETE by that id would drop the carrier + cascade its runs and break the feed's INNER JOIN.
    store, conn, _ = _store()
    store.record_vault_run("complete", "Vault X updated to v3")  # lazily creates carrier + one run
    assert conn.execute("SELECT COUNT(*) FROM schedule_runs WHERE schedule_id = ?;",
                        [scheduler._VAULT_FEED_ID]).fetchone()[0] == 1

    store.delete_schedule(scheduler._VAULT_FEED_ID)  # refused — the carrier is not a user schedule

    assert conn.execute("SELECT COUNT(*) FROM schedules WHERE id = ?;",
                        [scheduler._VAULT_FEED_ID]).fetchone()[0] == 1, "the carrier row survives"
    assert conn.execute("SELECT COUNT(*) FROM schedule_runs WHERE schedule_id = ?;",
                        [scheduler._VAULT_FEED_ID]).fetchone()[0] == 1, "its run history survives"

    sid = store.add_schedule("t", "p", interval_minutes=0, start_in_minutes=0, model=None)
    store.delete_schedule(sid)  # a normal schedule still deletes
    assert store.get_schedule(sid) is None


def test_content_encrypted_at_rest() -> None:
    store, conn, _ = _store()
    store.add_schedule("secret-title", "secret-prompt", interval_minutes=0, start_in_minutes=0, model=None)
    raw = bytes(conn.execute("SELECT ciphertext FROM schedules;").fetchone()[0])
    assert b"secret-title" not in raw and b"secret-prompt" not in raw


# --- due / cadence --------------------------------------------------------

def test_due_only_when_next_run_passed() -> None:
    store, _, _ = _store()
    store.add_schedule("now", "p", interval_minutes=0, start_in_minutes=0, model=None)
    future = store.add_schedule("later", "p", interval_minutes=0, start_in_minutes=60, model=None)
    due_ids = [s["id"] for s in store.due_schedules()]
    assert future not in due_ids and len(due_ids) == 1


def test_due_excludes_disabled() -> None:
    store, _, _ = _store()
    sid = store.add_schedule("now", "p", interval_minutes=0, start_in_minutes=0, model=None)
    store.set_enabled(sid, False)
    assert store.due_schedules() == []


def test_mark_ran_recurring_reschedules() -> None:
    store, _, _ = _store()
    sid = store.add_schedule("r", "p", interval_minutes=60, start_in_minutes=0, model=None)
    store.mark_ran(sid, 60)
    got = store.get_schedule(sid)
    assert got["enabled"] and got["last_run"] is not None  # still on, advanced
    assert store.due_schedules() == []  # next_run pushed into the future


def test_mark_ran_one_shot_disables() -> None:
    store, _, _ = _store()
    sid = store.add_schedule("o", "p", interval_minutes=0, start_in_minutes=0, model=None)
    store.mark_ran(sid, 0)
    assert store.get_schedule(sid)["enabled"] is False


# --- run_schedule ---------------------------------------------------------

def test_run_schedule_advances_then_runs(monkeypatch) -> None:
    store, conn, key = _store()
    sid = store.add_schedule("o", "do it", interval_minutes=0, start_in_minutes=0, model="m")
    seen = {}

    def fake(*a, **k):
        seen["ran"] = k
        return {"status": "complete"}

    monkeypatch.setattr(agent, "run_turn", fake)
    sched = store.get_schedule(sid)
    result = scheduler.run_schedule(tools.ToolContext(), None, None, store, sched)
    assert result["status"] == "complete"
    msgs = seen["ran"]["messages"]
    assert msgs[0]["role"] == "system" and msgs[-1]["content"] == "do it"  # grounded + prompt preserved
    assert store.get_schedule(sid)["enabled"] is False  # one-shot advanced (disabled)


def test_run_schedule_grounds_the_prompt(monkeypatch) -> None:
    # A scheduled run must carry the same anti-"pretend-done" grounding as chat, so the
    # model doesn't log a status string as a fact (the remember_fact "briefing sent" bug).
    store, _, _ = _store()
    sid = store.add_schedule("o", "send a briefing", interval_minutes=0, start_in_minutes=0, model="m")
    seen = {}
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: seen.update(k) or {"status": "complete"})
    scheduler.run_schedule(tools.ToolContext(), None, None, store, store.get_schedule(sid))
    system = seen["messages"][0]
    assert system["role"] == "system"
    assert "describing the change in words does not perform it" in system["content"].lower()


def test_run_schedule_honors_remembered_consent(monkeypatch) -> None:
    # A scheduled run must honor remembered writes (no user at the approval tile).
    store, conn, _ = _store()
    consent.remember(conn, "remember_fact")  # a remembered REVIEWED write
    sid = store.add_schedule("o", "do it", interval_minutes=0, start_in_minutes=0, model="m")
    seen = {}

    def fake(*a, **k):
        seen["ran"] = k
        return {"status": "complete"}

    monkeypatch.setattr(agent, "run_turn", fake)
    scheduler.run_schedule(tools.ToolContext(), None, None, store, store.get_schedule(sid))
    assert "remember_fact" in seen["ran"]["auto_approve"]


def test_run_schedule_never_auto_approves_schedule_writes(monkeypatch) -> None:
    # Security: even if the user remembered create/update/set_enabled in interactive chat, an
    # AUTONOMOUS scheduled turn must NOT auto-run them (an injected prompt could otherwise spawn
    # self-perpetuating schedules). They're stripped from auto_approve so they always park; other
    # remembered writes (remember_fact) still auto-run.
    store, conn, _ = _store()
    for name in ("remember_fact", "create_schedule", "update_schedule", "set_schedule_enabled"):
        consent.remember(conn, name)
    sid = store.add_schedule("o", "do it", interval_minutes=0, start_in_minutes=0, model="m")
    seen = {}
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: seen.update(k) or {"status": "complete"})
    scheduler.run_schedule(tools.ToolContext(), None, None, store, store.get_schedule(sid))
    auto = seen["auto_approve"]
    assert "remember_fact" in auto  # ordinary remembered write still honored
    assert auto.isdisjoint(tools.SCHEDULE_WRITE_TOOLS)  # every schedule-mutating tool stripped


def test_run_schedule_no_model_errors(monkeypatch) -> None:
    store, _, _ = _store()
    sid = store.add_schedule("o", "p", interval_minutes=0, start_in_minutes=0, model=None)
    monkeypatch.setattr(gateway, "resolve_model", lambda *_a: None)
    result = scheduler.run_schedule(None, None, None, store, store.get_schedule(sid))
    assert result["status"] == "error" and "model" in result["detail"]


def test_run_schedule_swallows_agent_error(monkeypatch) -> None:
    store, _, _ = _store()
    sid = store.add_schedule("o", "p", interval_minutes=0, start_in_minutes=0, model="m")

    def boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(agent, "run_turn", boom)
    result = scheduler.run_schedule(tools.ToolContext(), None, None, store, store.get_schedule(sid))
    assert result["status"] == "error" and "gateway down" in result["detail"]
    assert store.get_schedule(sid)["enabled"] is False  # advanced despite the failure


# --- run history (results visibility) -------------------------------------

def test_new_run_is_unseen_and_recent_runs_reports_it() -> None:
    # A freshly-fired run is unseen (drives the Chat nav badge); recent_runs surfaces the flag.
    store, _, _ = _store()
    sid = store.add_schedule("o", "p", interval_minutes=0, start_in_minutes=0, model="m")
    store.record_run(sid, "complete", message="out")
    assert store.unseen_count() == 1
    assert store.recent_runs()[0]["seen"] is False


def test_mark_all_seen_clears_unseen_count() -> None:
    store, _, _ = _store()
    a = store.add_schedule("a", "p", interval_minutes=0, start_in_minutes=0, model="m")
    b = store.add_schedule("b", "p", interval_minutes=0, start_in_minutes=0, model="m")
    store.record_run(a, "complete", message="x")
    store.record_run(b, "error", error="boom")
    assert store.unseen_count() == 2
    assert store.mark_all_seen() == 2  # returns how many were unseen
    assert store.unseen_count() == 0
    assert all(r["seen"] for r in store.recent_runs())
    assert store.mark_all_seen() == 0  # idempotent — nothing left to mark


def test_new_run_after_mark_seen_re_raises_the_badge() -> None:
    # The core feed loop: opening the feed clears the badge, then the NEXT scheduled run lights it again.
    store, _, _ = _store()
    sid = store.add_schedule("o", "p", interval_minutes=0, start_in_minutes=0, model="m")
    store.record_run(sid, "complete", message="first")
    store.mark_all_seen()
    assert store.unseen_count() == 0
    store.record_run(sid, "complete", message="second")  # a fresh run fires after the user caught up
    assert store.unseen_count() == 1  # badge re-appears for the new output only


def test_unseen_count_excludes_deleted_schedule_runs() -> None:
    # delete_schedule cascades its runs; the JOIN keeps a deleted schedule's runs out of the badge.
    store, _, _ = _store()
    sid = store.add_schedule("gone", "p", interval_minutes=0, start_in_minutes=0, model="m")
    store.record_run(sid, "complete", message="x")
    assert store.unseen_count() == 1
    store.delete_schedule(sid)
    assert store.unseen_count() == 0


def test_record_run_roundtrip_and_encrypted_at_rest() -> None:
    store, conn, _ = _store()
    sid = store.add_schedule("o", "p", interval_minutes=0, start_in_minutes=0, model="m")
    store.record_run(sid, "complete", message="secret briefing body", error=None)
    runs = store.list_runs(sid)
    assert len(runs) == 1 and runs[0]["status"] == "complete" and runs[0]["message"] == "secret briefing body"
    raw = bytes(conn.execute("SELECT ciphertext FROM schedule_runs;").fetchone()[0])
    assert b"secret briefing body" not in raw  # output encrypted at rest


def test_run_schedule_persists_result_so_user_can_read_it(monkeypatch) -> None:
    # The Critical bug: timer-fired briefing output was discarded. Now every run is recorded.
    store, _, _ = _store()
    sid = store.add_schedule("o", "brief me", interval_minutes=60, start_in_minutes=0, model="m")
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: {"status": "complete", "message": "Here is your briefing."})
    scheduler.run_schedule(tools.ToolContext(), None, None, store, store.get_schedule(sid))
    runs = store.list_runs(sid)
    assert runs[0]["status"] == "complete" and runs[0]["message"] == "Here is your briefing."


def test_run_schedule_records_failure_distinctly(monkeypatch) -> None:
    # A silently-failing nightly run must be distinguishable from a success.
    store, _, _ = _store()
    sid = store.add_schedule("o", "p", interval_minutes=60, start_in_minutes=0, model="m")
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")))
    scheduler.run_schedule(tools.ToolContext(), None, None, store, store.get_schedule(sid))
    runs = store.list_runs(sid)
    assert runs[0]["status"] == "error" and "model down" in (runs[0]["error"] or "")


def test_run_schedule_require_due_skips_when_not_due(monkeypatch) -> None:
    # The tick re-checks due-ness under the run lock: a not-due (future) row is skipped,
    # never advanced or run — this is what prevents tick/{run} double-fire.
    store, _, _ = _store()
    sid = store.add_schedule("r", "p", interval_minutes=60, start_in_minutes=60, model="m")
    ran = []
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: ran.append(1) or {"status": "complete"})
    result = scheduler.run_schedule(None, None, None, store, store.get_schedule(sid), require_due=True)
    assert result["status"] == "skipped" and ran == []
    assert store.get_schedule(sid)["last_run"] is None  # not advanced


# --- tick -----------------------------------------------------------------

def _app(conn, key):
    return SimpleNamespace(state=SimpleNamespace(master_key=key, db=conn, session_id="sess1"))


def test_tick_skips_when_locked() -> None:
    store, conn, _ = _store()
    store.add_schedule("now", "p", interval_minutes=0, start_in_minutes=0, model="m")
    app = SimpleNamespace(state=SimpleNamespace(master_key=None, db=conn, session_id=None))
    assert scheduler.tick(app) == 0  # locked: nothing runs


def test_tick_fires_due_and_advances(monkeypatch) -> None:
    store, conn, key = _store()
    store.add_schedule("now", "p", interval_minutes=0, start_in_minutes=0, model="m")
    ran = []
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: ran.append(1) or {"status": "complete"})
    assert scheduler.tick(_app(conn, key)) == 1
    assert ran == [1]
    assert scheduler.tick(_app(conn, key)) == 0  # one-shot disabled itself; nothing due now


def test_tick_no_due_returns_zero(monkeypatch) -> None:
    store, conn, key = _store()
    store.add_schedule("later", "p", interval_minutes=0, start_in_minutes=60, model="m")
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: {"status": "complete"})
    assert scheduler.tick(_app(conn, key)) == 0


def test_tick_stops_when_locked_mid_tick(monkeypatch) -> None:
    # Two due schedules; the vault is locked while the first runs. With workers=1
    # the claims serialize so the second worker's locked_check (inside _RUN_LOCK)
    # sees the lock set by the first turn and skips — preserving the original
    # "only while unlocked" invariant under the parallel tick model.
    monkeypatch.setattr(scheduler, "_SCHEDULE_WORKERS", 1)
    store, conn, key = _store()
    store.add_schedule("a", "p", interval_minutes=0, start_in_minutes=0, model="m")
    store.add_schedule("b", "p", interval_minutes=0, start_in_minutes=0, model="m")
    app = _app(conn, key)
    calls = []

    def fake(*a, **k):
        calls.append(1)
        app.state.master_key = None  # user locks the vault mid-tick
        return {"status": "complete"}

    monkeypatch.setattr(agent, "run_turn", fake)
    scheduler.tick(app)
    assert len(calls) == 1  # second due schedule not fired after the lock


def test_eager_reindex_backfills(monkeypatch) -> None:
    # R3: after the destructive embeddings migration (docs present, embeddings empty),
    # the scheduler's eager pass does a one-shot full backfill (not the 5/tick trickle).
    from smartbrain_3000.kb import KnowledgeBase

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    KnowledgeBase(conn, key).add("Doc", "hello world")  # add() stores the doc, not embeddings
    assert conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0] == 0
    monkeypatch.setattr(gateway, "embed_model", lambda c=None: "ollama/test")
    monkeypatch.setattr(gateway, "embed", lambda text, model, **k: [1.0, 0.0, 0.0])
    scheduler.eager_reindex(conn.cursor(), key)
    assert conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0] >= 1


def test_eager_reindex_noop_when_already_embedded(monkeypatch) -> None:
    # Once embeddings exist, the eager pass is a no-op (naturally one-shot).
    from smartbrain_3000.kb import KnowledgeBase

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    kb = KnowledgeBase(conn, key)
    doc = kb.add("Doc", "hi")
    kb.put_embeddings(doc, [[1.0, 0.0, 0.0]], "ollama/test")

    def boom(*a, **k):  # must not be called when embeddings already present
        raise AssertionError("eager reindex must not run when embeddings exist")

    monkeypatch.setattr(gateway, "embed", boom)
    scheduler.eager_reindex(conn.cursor(), key)  # returns early, no embed calls


# --- H1: parallel schedule execution --------------------------------------


def test_tick_runs_due_schedules_in_parallel(monkeypatch) -> None:
    # H1: with N>1 due schedules the agent turns must overlap (not strictly serial).
    # A barrier proves overlap: each turn waits for the others to arrive; if turns
    # ran sequentially the barrier would time out and `calls` would be empty
    # (the BrokenBarrierError raises before the append).
    store, conn, key = _store()
    n = 3
    for i in range(n):
        store.add_schedule(f"s{i}", "p", interval_minutes=0, start_in_minutes=0, model="m")
    barrier = threading.Barrier(n, timeout=5.0)  # bounded wait — never hangs the suite
    calls = []
    calls_lock = threading.Lock()

    def fake(*a, **k):
        barrier.wait()  # raises BrokenBarrierError if N turns don't arrive together
        with calls_lock:
            calls.append(1)
        return {"status": "complete"}

    monkeypatch.setattr(agent, "run_turn", fake)
    scheduler.tick(_app(conn, key))
    assert len(calls) == n, "all N turns must overlap at the barrier (proves parallel)"


def test_tick_claim_is_serialized(monkeypatch) -> None:
    # H1: even with parallel agent turns, the claim (_RUN_LOCK) must serialize so
    # no two workers are inside the claim window at once. We observe this by
    # asserting only one thread is in the claim region at a time.
    store, conn, key = _store()
    for i in range(3):
        store.add_schedule(f"s{i}", "p", interval_minutes=0, start_in_minutes=0, model="m")
    inside = [0]
    max_inside = [0]
    inside_lock = threading.Lock()
    real_mark_ran = scheduler.ScheduleStore.mark_ran

    def traced_mark_ran(self, sid, interval_minutes):
        # mark_ran runs INSIDE the _RUN_LOCK claim window — count overlap there.
        with inside_lock:
            inside[0] += 1
            if inside[0] > max_inside[0]:
                max_inside[0] = inside[0]
        time.sleep(0.01)  # widen the window so any race would be visible
        try:
            real_mark_ran(self, sid, interval_minutes)
        finally:
            with inside_lock:
                inside[0] -= 1

    monkeypatch.setattr(scheduler.ScheduleStore, "mark_ran", traced_mark_ran)
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: {"status": "complete"})
    scheduler.tick(_app(conn, key))
    assert max_inside[0] == 1, f"claim must be serialized, saw {max_inside[0]} concurrent"


def test_tick_uses_per_thread_cursor(monkeypatch) -> None:
    # H1 safety: each worker must build its OWN cursor (no shared cursor across
    # threads). We capture the cursor each worker uses and assert they are distinct
    # objects and distinct from the tick's own cursor.
    store, conn, key = _store()
    for i in range(2):
        store.add_schedule(f"s{i}", "p", interval_minutes=0, start_in_minutes=0, model="m")
    seen_cursors = []
    seen_lock = threading.Lock()
    real_run_schedule = scheduler.run_schedule

    def traced(ctx, audit, approvals, store_arg, schedule, **kwargs):
        with seen_lock:
            seen_cursors.append(id(store_arg.conn))  # B18: public property, no _conn
        return real_run_schedule(ctx, audit, approvals, store_arg, schedule, **kwargs)

    monkeypatch.setattr(scheduler, "run_schedule", traced)
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: {"status": "complete"})
    scheduler.tick(_app(conn, key))
    assert len(seen_cursors) == 2 and seen_cursors[0] != seen_cursors[1], \
        "each worker must own a distinct per-thread cursor"


# --- B11: gateway circuit breaker ----------------------------------------


def test_auto_reindex_breaker_trips_and_suppresses(monkeypatch) -> None:
    # B11: repeated embed failures trip the breaker; subsequent ticks must skip
    # the reindex attempt entirely (no per-tick retry storm) until cooldown.
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    calls = [0]

    def boom(_c=None):
        calls[0] += 1
        raise RuntimeError("gateway down")

    monkeypatch.setattr(gateway, "embed_model", boom)
    # Drive enough failures to trip the breaker.
    for _ in range(scheduler._BREAKER_TRIP_AFTER):
        scheduler._auto_reindex(conn, key)
    assert calls[0] == scheduler._BREAKER_TRIP_AFTER  # each attempt ran the gateway
    assert scheduler._breaker_open(), "breaker must be open after threshold failures"
    # Next several ticks must be suppressed — calls counter does NOT advance.
    pre = calls[0]
    for _ in range(5):
        scheduler._auto_reindex(conn, key)
    assert calls[0] == pre, "breaker must suppress attempts during cooldown"


def test_breaker_recovers_after_cooldown(monkeypatch) -> None:
    # B11: once the cooldown window elapses, the next attempt probes again and
    # a success resets the breaker.
    scheduler._BREAKER.fails = scheduler._BREAKER_TRIP_AFTER
    scheduler._BREAKER.until = time.monotonic() - 1.0  # cooldown already elapsed
    scheduler._BREAKER.warned = True
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    monkeypatch.setattr(gateway, "embed_model", lambda _c=None: "ollama/test")
    # No pending docs → reindex_pending returns (0, 0, 0, None) — counts as success.
    scheduler._auto_reindex(conn, key)
    assert not scheduler._breaker_open()
    assert scheduler._BREAKER.fails == 0 and scheduler._BREAKER.warned is False


def test_breaker_blocks_schedule_firing(monkeypatch) -> None:
    # B11: when the breaker is open, tick must skip schedule firing too — not
    # just auto-reindex — so we don't pile turns onto a known-dead model.
    store, conn, key = _store()
    store.add_schedule("s", "p", interval_minutes=0, start_in_minutes=0, model="m")
    scheduler._BREAKER.fails = scheduler._BREAKER_TRIP_AFTER
    scheduler._BREAKER.until = time.monotonic() + 60.0  # breaker open
    ran = []
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: ran.append(1) or {"status": "complete"})
    monkeypatch.setattr(gateway, "embed_model", lambda _c=None: "ollama/test")
    assert scheduler.tick(_app(conn, key)) == 0
    assert ran == []  # breaker open → no agent turn fired


def test_breaker_warning_is_debounced(caplog) -> None:
    # B11: the "breaker tripped" warning logs ONCE per trip, not every failure
    # after the threshold. Drive failures via _breaker_record directly so the
    # open-breaker short-circuit in _auto_reindex doesn't suppress the count.
    caplog.set_level("WARNING", logger=scheduler.log.name)
    for _ in range(scheduler._BREAKER_TRIP_AFTER + 5):
        scheduler._breaker_record(success=False)
    tripped = [r for r in caplog.records if "tripped" in r.getMessage()]
    assert len(tripped) == 1, f"expected 1 trip-warning, saw {len(tripped)}"


def test_breaker_record_no_lost_increments_under_parallel_failures(caplog) -> None:
    # The tick runs up to _SCHEDULE_WORKERS agent turns in parallel, each calling
    # _breaker_record OUTSIDE _RUN_LOCK. Without _BREAKER_LOCK the read-modify-write
    # races: increments are lost (late trip) and the debounced warning double-fires.
    caplog.set_level("WARNING", logger=scheduler.log.name)
    from concurrent.futures import ThreadPoolExecutor

    n = 200
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: scheduler._breaker_record(success=False), range(n)))
    assert scheduler._BREAKER.fails == n  # every failure counted; none lost to the race
    tripped = [r for r in caplog.records if "tripped" in r.getMessage()]
    assert len(tripped) == 1, f"expected exactly 1 trip-warning, saw {len(tripped)}"


# --- B18: ScheduleStore.conn property ------------------------------------


def test_schedule_store_exposes_conn_property() -> None:
    # B18: callers should use store.conn, not store._conn. The property must
    # return the same connection passed in.
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = ScheduleStore(conn, gen_master_key())
    assert store.conn is conn  # public property mirrors KnowledgeBase.conn
    # And the scheduler module itself uses the property — no ._conn reach-through.
    import inspect

    src = inspect.getsource(scheduler)
    assert "store._conn" not in src, "scheduler.py must not reach into ScheduleStore._conn"


# --- Stage E: the vault-updates carrier row (plan decision #2) -------------------------------------


def test_record_vault_run_surfaces_in_the_feed_and_badge() -> None:
    # Vault auto-update results have no schedule, but the reserved carrier keeps the feed's INNER
    # JOIN valid so they ride recent_runs + the unseen badge exactly like a scheduled-prompt run.
    store, _, _ = _store()
    store.record_vault_run("complete", "Vault X updated to v3 — 1 document changed")
    runs = [r for r in store.recent_runs() if r["schedule_title"] == "Vault updates"]
    assert len(runs) == 1 and runs[0]["message"].startswith("Vault X updated")
    assert runs[0]["status"] == "complete" and runs[0]["seen"] is False
    assert store.unseen_count() == 1
    store.record_vault_run("blocked", "another one")  # carrier is created once, then reused
    assert store.unseen_count() == 2


def test_vault_carrier_is_hidden_from_the_schedule_list_and_get() -> None:
    # The carrier is not a user schedule: it must never appear in the Schedules list, and get_schedule
    # reads it as absent so no route can run, edit, or delete it.
    store, conn, _ = _store()
    store.add_schedule("real", "p", interval_minutes=0, start_in_minutes=0, model="m")
    store.record_vault_run("complete", "x")  # lazily creates the carrier row
    assert conn.execute("SELECT COUNT(*) FROM schedules WHERE id = ?;",
                        [scheduler._VAULT_FEED_ID]).fetchone()[0] == 1, "the carrier row exists"
    titles = [s["title"] for s in store.list_schedules()]
    assert titles == ["real"], "the carrier is filtered out of the user's schedule list"
    assert store.get_schedule(scheduler._VAULT_FEED_ID) is None
    assert store.due_schedules() == [] or all(
        s["id"] != scheduler._VAULT_FEED_ID for s in store.due_schedules()), "carrier never fires"
