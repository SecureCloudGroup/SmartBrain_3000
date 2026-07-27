"""Tests for the self-review scorecard pass (self-improving framework, Phase 2)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import metrics, scheduler, selfreview
from smartbrain_3000.audit import AuditLog
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.secrets import gen_master_key


def _conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return conn


def _seed_turns(conn, *, total: int, degraded: int = 0, max_steps: int = 0) -> None:
    for i in range(total):
        metrics.record_turn(
            conn, model="ollama/x", is_local=True, duration_ms=1000,
            degraded=i < degraded, hit_max_steps=i < max_steps,
            outcome="max_steps" if i < max_steps else "complete",
        )


# --- gates ------------------------------------------------------------------

def test_kill_switch_fails_closed() -> None:
    conn = _conn()
    assert selfreview.enabled(conn) is False  # absent -> off
    dbmod.meta_set(conn, selfreview.ENABLED_META_KEY, "yes please")  # corrupt -> off
    assert selfreview.enabled(conn) is False
    selfreview.set_enabled(conn, True)
    assert selfreview.enabled(conn) is True
    selfreview.set_enabled(conn, False)
    assert selfreview.enabled(conn) is False


def test_due_cadence() -> None:
    conn = _conn()
    assert selfreview.due(conn) is True  # never ran -> due
    row = conn.execute("SELECT strftime(now(), ?);", [selfreview._TS_FORMAT]).fetchone()
    dbmod.meta_set(conn, selfreview.LAST_RUN_META_KEY, str(row[0]))
    assert selfreview.due(conn) is False  # just ran -> not due
    old = conn.execute(
        "SELECT strftime(now() - to_seconds(?), ?);",
        [selfreview.REVIEW_INTERVAL_SECONDS + 60, selfreview._TS_FORMAT],
    ).fetchone()
    dbmod.meta_set(conn, selfreview.LAST_RUN_META_KEY, str(old[0]))
    assert selfreview.due(conn) is True  # a full interval ago -> due again
    dbmod.meta_set(conn, selfreview.LAST_RUN_META_KEY, "not a timestamp")
    assert selfreview.due(conn) is True  # corrupt cadence stamp -> run and self-heal


def test_disabled_run_review_is_a_noop() -> None:
    conn, key = _conn(), gen_master_key()
    calls: list = []
    assert selfreview.run_review(conn, key, notify=calls.append) is None
    assert selfreview.ReviewStore(conn, key).count() == 0
    assert not calls
    assert selfreview.last_run(conn) is None  # cadence untouched while disabled


# --- the review pass --------------------------------------------------------

def test_quiet_window_stores_review_but_stays_silent() -> None:
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    _seed_turns(conn, total=6)  # healthy: nothing degraded, nothing stopped
    notified: list = []
    out = selfreview.run_review(conn, key, notify=lambda s, m: notified.append((s, m)))
    assert out is not None and out["flags"] == 0
    store = selfreview.ReviewStore(conn, key)
    assert store.count() == 1
    latest = store.latest()
    assert latest["flags"] == 0 and latest["scorecard"]["chat"]["turns"] == 6
    assert not notified  # silence is the normal outcome
    assert selfreview.last_run(conn) == out["window_end"]  # cadence advanced
    assert selfreview.run_review(conn, key) is None  # immediately after: not due


def test_degraded_rate_flags_and_notifies() -> None:
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    _seed_turns(conn, total=10, degraded=3)  # 30% ≥ the 20% threshold
    notified: list = []
    out = selfreview.run_review(conn, key, notify=lambda s, m: notified.append((s, m)))
    assert out["flags"] >= 1
    assert len(notified) == 1
    status, message = notified[0]
    assert status == "complete" and "degraded" in message and "10 chat turns" in message


def test_small_samples_never_flag() -> None:
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    _seed_turns(conn, total=2, degraded=2)  # 100% degraded but n < min sample
    out = selfreview.run_review(conn, key)
    assert out is not None and out["flags"] == 0


def test_failing_tool_and_denials_flag() -> None:
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    audit = AuditLog(conn, key)
    for _ in range(3):  # a tool failing every call it gets
        audit.append("assistant", "web_fetch", "observe", "errored", False, error="boom")
    for _ in range(2):  # repeated denials = assistant proposing the wrong thing
        audit.append("user", "email_send", "irreversible", "denied", True)
    notified: list = []
    out = selfreview.run_review(conn, key, notify=lambda s, m: notified.append(m))
    assert out["flags"] == 2
    assert "web_fetch" in notified[0] and "denied" in notified[0]
    card = selfreview.ReviewStore(conn, key).latest()["scorecard"]
    assert card["denied"] == 2
    assert {"tool": "web_fetch", "calls": 3, "failures": 3} in card["tools"]


def test_review_failure_never_raises(monkeypatch) -> None:
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    monkeypatch.setattr(selfreview, "build_scorecard", lambda *a, **k: 1 / 0)
    assert selfreview.run_review(conn, key) is None  # swallowed, logged


def _stamp(conn, seconds_ago: int) -> str:
    row = conn.execute("SELECT strftime(now() - to_seconds(?), ?);",
                       [seconds_ago, selfreview._TS_FORMAT]).fetchone()
    return str(row[0])


def test_corrupt_last_run_self_heals() -> None:
    # Adversarial-review finding: a corrupt stamp made every pass throw BEFORE the
    # stamp rewrite, wedging the reviewer forever. Now it falls back to a bounded
    # window, completes, and rewrites a valid stamp — the self-heal is real.
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    _seed_turns(conn, total=6)
    for bad in ("not a timestamp", "2026-07-26 08:00:00"):  # garbage AND fraction-less
        dbmod.meta_set(conn, selfreview.LAST_RUN_META_KEY, bad)
        out = selfreview.run_review(conn, key)
        assert out is not None, f"review must survive stamp {bad!r}"
        healed = selfreview.last_run(conn)
        assert healed != bad
        assert selfreview.due(conn) is False  # healed stamp parses -> cadence works again


def test_stale_stamp_from_disabled_gap_is_clamped() -> None:
    # A valid stamp from weeks ago (disable/re-enable gap) must NOT produce an
    # unbounded catch-up window — the window falls back to the bounded first-window
    # shape instead of scoring the entire gap as one misleading period.
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    stale = _stamp(conn, 30 * 24 * 3600)  # a month ago
    dbmod.meta_set(conn, selfreview.LAST_RUN_META_KEY, stale)
    out = selfreview.run_review(conn, key)
    assert out is not None and out["window_start"] != stale
    row = conn.execute(  # the clamped start is within ~2 intervals of now
        "SELECT strptime(?, ?) >= now() - to_seconds(?);",
        [out["window_start"], selfreview._TS_FORMAT, 2 * selfreview.REVIEW_INTERVAL_SECONDS],
    ).fetchone()
    assert bool(row[0])


def test_slightly_late_stamp_is_kept() -> None:
    # Cadence jitter (a tick a few minutes late) must keep the real window start —
    # rows from the extra minutes would otherwise be silently dropped.
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    late = _stamp(conn, selfreview.REVIEW_INTERVAL_SECONDS + 300)  # 8h + 5min ago
    dbmod.meta_set(conn, selfreview.LAST_RUN_META_KEY, late)
    out = selfreview.run_review(conn, key)
    assert out is not None and out["window_start"] == late


def test_pending_embedding_flags_only_when_persistent() -> None:
    # Adversarial-review finding: an instantaneous backlog probe paged on a healthy
    # post-import drain. The flag now requires the backlog to persist across TWO
    # consecutive reviews (a whole interval) — the condition the comment always claimed.
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    KnowledgeBase(conn, key).add("Fresh import", "unembedded content")  # backlog of 1
    first = selfreview.run_review(conn, key)
    assert first is not None and first["flags"] == 0  # first sighting: report, don't page
    card = selfreview.ReviewStore(conn, key).latest()["scorecard"]
    assert card["knowledge"]["pending_embedding"] == 1
    dbmod.meta_set(conn, selfreview.LAST_RUN_META_KEY,
                   _stamp(conn, selfreview.REVIEW_INTERVAL_SECONDS + 60))  # force due again
    notified: list = []
    second = selfreview.run_review(conn, key, notify=lambda s, m: notified.append(m))
    assert second is not None and second["flags"] == 1  # still pending a whole interval later
    assert "waiting to be indexed" in notified[0]


def test_relock_mid_tick_stands_down() -> None:
    # The tick snapshots the key at entry; a mid-tick Lock must stop the review from
    # decrypting or writing with that stale snapshot (run_schedule's contract).
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    _seed_turns(conn, total=6)
    assert selfreview.run_review(conn, key, locked_check=lambda: True) is None
    assert selfreview.ReviewStore(conn, key).count() == 0
    assert selfreview.last_run(conn) is None  # nothing persisted after the relock


def test_notify_failure_keeps_cadence_unadvanced() -> None:
    # Adversarial-review finding: cadence used to advance BEFORE the digest, so a
    # notify failure silently dropped the phase's only user-visible output forever.
    # Now the stamp stays put and the next pass retries (duplicate review row is the
    # accepted cost).
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    _seed_turns(conn, total=10, degraded=10)  # guaranteed flag -> digest attempted
    def boom(status: str, msg: str) -> None:
        raise RuntimeError("feed write failed")
    assert selfreview.run_review(conn, key, notify=boom) is None
    assert selfreview.last_run(conn) is None  # cadence NOT advanced -> retry next tick
    delivered: list = []
    out = selfreview.run_review(conn, key, notify=lambda s, m: delivered.append(m))
    assert out is not None and len(delivered) == 1  # retried and delivered


# --- the carrier + tick integration ----------------------------------------

def test_selfreview_carrier_rides_feed_and_stays_hidden() -> None:
    conn, key = _conn(), gen_master_key()
    store = scheduler.ScheduleStore(conn, key)
    store.record_selfreview_run("complete", "digest text")
    assert store.unseen_count() == 1  # digest lights the badge like any scheduled run
    runs = store.recent_runs()
    assert runs[0]["schedule_title"] == "Self-review" and runs[0]["message"] == "digest text"
    # Hidden + protected exactly like the vault carrier:
    assert store.list_schedules() == []
    assert store.get_schedule(scheduler._SELFREVIEW_FEED_ID) is None
    store.delete_schedule(scheduler._SELFREVIEW_FEED_ID)  # no-op, never deletable
    assert store.unseen_count() == 1


def test_tick_runs_review_when_enabled_and_skips_when_locked() -> None:
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    _seed_turns(conn, total=10, degraded=10)  # guaranteed flag -> digest row
    app = SimpleNamespace(state=SimpleNamespace(master_key=key, db=conn, session_id="s1",
                                                last_interactive=0.0))
    scheduler.tick(app)
    assert selfreview.ReviewStore(conn, key).count() == 1
    assert scheduler.ScheduleStore(conn, key).unseen_count() == 1  # digest surfaced
    # Locked: nothing runs (cadence would allow it — reset the stamp to prove the gate).
    conn2, key2 = _conn(), gen_master_key()
    selfreview.set_enabled(conn2, True)
    locked = SimpleNamespace(state=SimpleNamespace(master_key=None, db=conn2, session_id=None))
    assert scheduler.tick(locked) == 0
    assert selfreview.ReviewStore(conn2, key2).count() == 0


# --- HTTP API ---------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_selfimprove_api_requires_unlock(client: TestClient) -> None:
    # The kill-switch is the phase's master safety control — it must be locked-gated.
    assert client.get("/api/selfimprove").status_code == 423
    assert client.put("/api/selfimprove", json={"enabled": True}).status_code == 423


def test_selfimprove_api_round_trip(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    state = client.get("/api/selfimprove").json()
    assert state == {"enabled": False, "last_run": None}  # default off, never ran
    assert client.put("/api/selfimprove", json={"enabled": True}).json()["enabled"] is True
    assert client.get("/api/selfimprove").json()["enabled"] is True
    assert client.put("/api/selfimprove", json={"enabled": False}).json()["enabled"] is False
