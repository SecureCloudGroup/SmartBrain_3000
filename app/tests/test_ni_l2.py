"""Neural Interface: §23 L2 frontier repair (park-only).

Eligibility matrix, tick-marks-only invariant, single-flight worker, the
worker's end-to-end proposal → carrier notice → board flag chain, GatewayError
403 (not-connected) quiet failure, invalid-reply / spec-changed audit rows,
apply / dismiss routes, and the D1 "user edit voids proposal" law extended to
_l2_proposal + the D1 shape-check refusal for smuggled system-only keys.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import scheduler as sched
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import gen_master_key

# --- helpers --------------------------------------------------------------

def _minimal_scene() -> dict:
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{note}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}


def _basic_spec(**overrides) -> dict:
    base: dict = {
        "version": 1,
        "title": "Watch",
        "goal": "show a note",
        "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [{"op": "extract", "paths": {"note": "text"}}],
        "scene": _minimal_scene(),
        "display": {"size": "small"},
        "contract": None,
        "repair_policy": {"l1": True, "l2_frontier": True},
        "model": None,
    }
    base.update(overrides)
    return base


def _store():
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return nimod.NIStore(conn, key), conn, key


def _make_failing_item(store, *, l1_attempted: bool = True,
                       l2_frontier: bool = True) -> tuple[str, dict]:
    """Land an item at state=failing with contract captured + L1 already tried
    this streak (unless overridden). Streak marker is 10 minutes ago; the L1
    stamp lands just after that so ``_l1_already_tried_this_streak`` is True.
    """
    assert store is not None, "store required"
    spec = _basic_spec()
    spec["repair_policy"] = {"l1": True, "l2_frontier": l2_frontier}
    iid = store.add_item(spec, {"note": "seed"})
    current = store.get_item(iid)["spec"]
    current["_c2_ok"] = True
    current["contract"] = {"shape": {"note": "string"}}
    if l1_attempted:
        current["_l1_last_attempt"] = (
            datetime.now(UTC) - timedelta(minutes=5)
        ).isoformat()
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ?, "
        "consecutive_failures = 3, first_failure_at = now() - INTERVAL '10 MINUTES', "
        "state = 'failing' WHERE id = ?;",
        [*store._seal_item(iid, current), iid],
    )
    return iid, current


def _install_gateway(monkeypatch: pytest.MonkeyPatch, *,
                      text: str | None = None,
                      raises: Exception | None = None) -> list[dict]:
    """Monkeypatch ``smartbrain_3000.gateway.chat`` for the worker's late-bound
    import (``from . import gateway as gateway_mod`` inside ``_run_l2_worker``).
    Returns a per-test capture list of {messages, model, timeout} call records.

    ``raises`` should be an already-constructed ``gateway.GatewayError`` (real
    class) so the worker's ``except gateway_mod.GatewayError`` matches.
    """
    from smartbrain_3000 import gateway as real_gw
    calls: list[dict] = []

    def _chat(messages, model, *, timeout: float | None = None, **_kw):
        assert model.startswith("claudecode/"), (
            "L2 must dial the claudecode provider — the frontier ladder's only v1 backend"
        )
        assert timeout is not None and timeout >= 300.0, (
            "L2 must ride the MIN_TIMEOUT floor (a frontier turn takes minutes)"
        )
        calls.append({"messages": messages, "model": model, "timeout": timeout})
        if raises is not None:
            raise raises
        return {"choices": [{"message": {"content": text or ""}}]}

    monkeypatch.setattr(real_gw, "chat", _chat)
    return calls


def _fake_app(conn, key: bytes):
    """Minimal app.state shim for the worker: master_key + db.cursor factory."""
    return SimpleNamespace(state=SimpleNamespace(
        master_key=key,
        db=SimpleNamespace(cursor=conn.cursor),
    ))


# --- eligibility matrix ---------------------------------------------------

def test_l2_eligible_happy_path() -> None:
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store)
    item = store.get_item(iid)
    assert nimod._l2_eligible(item, item["spec"]) is True


def test_l2_ineligible_when_policy_off() -> None:
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store, l2_frontier=False)
    item = store.get_item(iid)
    assert nimod._l2_eligible(item, item["spec"]) is False


def test_l2_ineligible_when_l1_not_attempted_this_streak() -> None:
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store, l1_attempted=False)
    item = store.get_item(iid)
    assert nimod._l2_eligible(item, item["spec"]) is False


def test_l2_ineligible_when_l1_trial_active() -> None:
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store)
    current = store.get_item(iid)["spec"]
    current["_l1_trial"] = {"rev_before": 1}
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;",
        [*store._seal_item(iid, current), iid],
    )
    item = store.get_item(iid)
    assert nimod._l2_eligible(item, item["spec"]) is False


def test_l2_ineligible_when_already_tried_this_streak() -> None:
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store)
    current = store.get_item(iid)["spec"]
    current["_l2_last_attempt"] = (
        datetime.now(UTC) - timedelta(minutes=3)
    ).isoformat()
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;",
        [*store._seal_item(iid, current), iid],
    )
    item = store.get_item(iid)
    assert nimod._l2_eligible(item, item["spec"]) is False


def test_l2_ineligible_when_proposal_pending() -> None:
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store)
    ok = store.set_l2_proposal(iid, {
        "stages": {"extract": {"note": "text"}},
        "created_at": datetime.now(UTC).isoformat(),
        "model": "claudecode/sonnet",
    }, expected_rev=store.get_item(iid)["spec_rev"])
    assert ok is True
    item = store.get_item(iid)
    assert nimod._l2_eligible(item, item["spec"]) is False


def test_l2_ineligible_when_state_not_failing() -> None:
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store)
    store.set_state(iid, "degraded")
    item = store.get_item(iid)
    assert nimod._l2_eligible(item, item["spec"]) is False


# --- tick marks only; NEVER dials the frontier model ----------------------

def test_tick_marks_l2_candidates_without_calling_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tick must NEVER call the frontier model itself (a claudecode turn
    takes minutes; the tick's budget is 20s). It just marks candidates.

    We install a loud ``gateway.chat`` that fails the test on any call — the
    tick's model-source items are covered by the breaker/local-availability
    gates in earlier tests, so a due failing item lands on the eligibility
    scan without dispatching to the model.
    """
    store, conn, key = _store()
    iid, _s = _make_failing_item(store)
    conn.execute("UPDATE ni_items SET last_checked = NULL;")

    from smartbrain_3000 import gateway as real_gw

    def _loud_chat(*_a, **_kw):
        raise AssertionError("tick must NEVER call gateway.chat for L2 eligibility")

    monkeypatch.setattr(real_gw, "chat", _loud_chat)
    monkeypatch.setattr(real_gw, "local_available", lambda: False)
    # Failing items skip due to the tick's local-availability gate; we still
    # want the eligibility scan to run, so drop the tick's fetch path entirely
    # via a fake_run_item that succeeds cheaply (leaves the item at 'failing'
    # since we don't touch state). What matters: the tick returns l2_candidates
    # containing iid and never dials the frontier model.
    monkeypatch.setattr(nimod, "run_item",
                        lambda *_a, **_kw: {"status": "ok", "duration_ms": 0,
                                             "alerts": [], "repaired": []})
    result = nimod.tick(_fake_app(conn, key), pass_budget_seconds=1.0)
    assert iid in result["l2_candidates"], result
    # Attempt is not stamped by the tick itself; only the worker does that.
    assert not store.get_item(iid)["spec"].get("_l2_last_attempt")


# --- worker single-flight -------------------------------------------------

def test_spawn_l2_worker_drops_when_lock_held() -> None:
    """A worker already in flight (lock held) must cause spawn to drop silently."""
    store, conn, key = _store()
    iid, _s = _make_failing_item(store)
    acquired = nimod._L2_WORKER_LOCK.acquire(blocking=False)
    assert acquired, "test invariant: the lock must be free at test start"
    try:
        started = nimod.spawn_l2_worker(_fake_app(conn, key), [iid])
        assert started is False, "busy worker must drop the new call"
    finally:
        nimod._L2_WORKER_LOCK.release()
    # Sealed spec unchanged: no attempt stamped, no proposal, no runs recorded.
    fresh = store.get_item(iid)
    assert "_l2_last_attempt" not in fresh["spec"]
    assert "_l2_proposal" not in fresh["spec"]
    assert not store.list_runs(iid, limit=5)


# --- worker end-to-end ----------------------------------------------------

def test_worker_end_to_end_seals_proposal_and_posts_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: valid reply → proposal sealed + repair_l2_proposed row +
    carrier notice posted + board l2_proposal flag flips True.
    """
    store, conn, key = _store()
    iid, _s = _make_failing_item(store)
    _install_gateway(monkeypatch, text='{"extract": {"note": "text.value"}}')

    nimod._run_l2_worker(_fake_app(conn, key), [iid])

    after = store.get_item(iid)
    proposal = after["spec"].get("_l2_proposal")
    assert isinstance(proposal, dict), proposal
    assert proposal["stages"] == {"extract": {"note": "text.value"}}
    assert proposal["model"] == "claudecode/sonnet"
    datetime.fromisoformat(proposal["created_at"])  # ISO-8601 shape check

    # Attempt stamped so a re-fire this streak is blocked.
    assert after["spec"].get("_l2_last_attempt")

    # ni_runs row recorded.
    runs = store.list_runs(iid, limit=5)
    assert any(r["status"] == "repair_l2_proposed" for r in runs), runs

    # Carrier notice posted with the "proposal" status.
    schedules = ScheduleStore(conn, key)
    messages = [r for r in schedules.recent_runs()
                if r["schedule_title"] == "Neural Interface"]
    assert any(r["status"] == "proposal"
               and "a proposed fix is ready to review" in r["message"]
               for r in messages), messages


def test_worker_403_records_quiet_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§23 gate: a 403 (Claude Code not connected) records an ni_runs row with
    class "not_connected" and NO proposal. Attempt is stamped either way so a
    reconnected user gets a fresh attempt when the streak next resets."""
    store, conn, key = _store()
    iid, _s = _make_failing_item(store)
    from smartbrain_3000 import gateway as real_gw
    _install_gateway(monkeypatch, raises=real_gw.GatewayError(403, "not connected"))

    nimod._run_l2_worker(_fake_app(conn, key), [iid])

    after = store.get_item(iid)
    assert "_l2_proposal" not in after["spec"]
    assert after["spec"].get("_l2_last_attempt"), "one attempt per streak burns on 403 too"
    runs = store.list_runs(iid, limit=5)
    assert any(r["status"] == "repair_l2_failed" and r["error"] == "not_connected"
               for r in runs), runs
    # No carrier notice posted (403 is silent per §23).
    schedules = ScheduleStore(conn, key)
    messages = [r for r in schedules.recent_runs()
                if r["schedule_title"] == "Neural Interface"]
    assert not any(r["status"] == "proposal" for r in messages), messages


def test_worker_invalid_reply_records_parse_or_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply with an extra key (e.g. "source") is refused by the L1 parser
    (closed {extract, transform}); records repair_l2_failed / parse_or_shape."""
    store, conn, key = _store()
    iid, _s = _make_failing_item(store)
    _install_gateway(
        monkeypatch,
        text='{"extract": {"note": "text"}, "source": {"type": "model"}}',
    )
    nimod._run_l2_worker(_fake_app(conn, key), [iid])
    after = store.get_item(iid)
    assert "_l2_proposal" not in after["spec"]
    runs = store.list_runs(iid, limit=5)
    assert any(r["status"] == "repair_l2_failed" and r["error"] == "parse_or_shape"
               for r in runs), runs


def test_worker_spec_changed_during_call_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§23 D3 lesson: a concurrent user update between the read and the write
    aborts the proposal (repair_l2_failed / spec_changed)."""
    store, conn, key = _store()
    iid, _s = _make_failing_item(store)
    from smartbrain_3000 import gateway as real_gw

    # Land a user update INSIDE the model call so the worker's expected_rev
    # captured before the call no longer matches at seal time (2b D3 lesson).
    def _racing_chat(_messages, _model, *, timeout=None, **_kw):
        edited = dict(store.get_item(iid)["spec"], title="User Won The Race")
        edited.pop("contract", None)  # update_spec strips it anyway
        store.update_spec(iid, edited, origin="user")
        return {"choices": [{"message": {
            "content": '{"extract": {"note": "text"}}'}}]}

    monkeypatch.setattr(real_gw, "chat", _racing_chat)
    nimod._run_l2_worker(_fake_app(conn, key), [iid])
    after = store.get_item(iid)
    assert "_l2_proposal" not in after["spec"], "race must not seal a proposal"
    runs = store.list_runs(iid, limit=5)
    assert any(r["status"] == "repair_l2_failed" and r["error"] == "spec_changed"
               for r in runs), runs


# --- validate_spec: system-only key shape ---------------------------------

def test_validate_spec_refuses_bad_l2_last_attempt_shapes() -> None:
    spec = _basic_spec()
    spec["_l2_last_attempt"] = 42
    with pytest.raises(ValueError, match="_l2_last_attempt"):
        nimod.validate_spec(spec)
    spec["_l2_last_attempt"] = "not-an-iso-date"
    with pytest.raises(ValueError, match="_l2_last_attempt"):
        nimod.validate_spec(spec)


def test_validate_spec_refuses_bad_l2_proposal_shapes() -> None:
    spec = _basic_spec()
    # not an object
    spec["_l2_proposal"] = "hello"
    with pytest.raises(ValueError, match="_l2_proposal"):
        nimod.validate_spec(spec)
    # unknown key
    spec["_l2_proposal"] = {"stages": {"extract": {"a": "b"}},
                             "created_at": datetime.now(UTC).isoformat(),
                             "model": "claudecode/sonnet",
                             "surprise": 1}
    with pytest.raises(ValueError, match="_l2_proposal"):
        nimod.validate_spec(spec)
    # bad inner shape (transform not a list)
    spec["_l2_proposal"] = {"stages": {"transform": {"not": "a list"}},
                             "created_at": datetime.now(UTC).isoformat(),
                             "model": "claudecode/sonnet"}
    with pytest.raises(ValueError, match="transform"):
        nimod.validate_spec(spec)
    # empty stages
    spec["_l2_proposal"] = {"stages": {},
                             "created_at": datetime.now(UTC).isoformat(),
                             "model": "claudecode/sonnet"}
    with pytest.raises(ValueError, match="stages"):
        nimod.validate_spec(spec)


def test_validate_spec_accepts_wellformed_l2_proposal() -> None:
    spec = _basic_spec()
    spec["_l2_proposal"] = {
        "stages": {"extract": {"note": "text"}},
        "created_at": datetime.now(UTC).isoformat(),
        "model": "claudecode/sonnet",
    }
    nimod.validate_spec(spec)  # must NOT raise


# --- user edit voids a pending L2 proposal (§23 D1 law extended) ----------

def test_update_spec_strips_pending_l2_proposal() -> None:
    """A user/agent edit must void any pending L2 proposal — carrying it would
    let a later Apply undo the edit (same rationale as §14 D1 for _l1_trial)."""
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store)
    ok = store.set_l2_proposal(iid, {
        "stages": {"extract": {"note": "text"}},
        "created_at": datetime.now(UTC).isoformat(),
        "model": "claudecode/sonnet",
    }, expected_rev=store.get_item(iid)["spec_rev"])
    assert ok
    edited = dict(store.get_item(iid)["spec"], title="User Rename")
    store.update_spec(iid, edited, origin="user")
    after = store.get_item(iid)
    assert "_l2_proposal" not in after["spec"], "user edit must void the proposal"
    assert after["spec"]["title"] == "User Rename"


# --- apply / dismiss routes ------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni_l2.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _unlock(client: TestClient) -> None:
    r = client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert r.status_code == 200, r.text


def _seed_failing_with_proposal(client: TestClient) -> str:
    """Create an item via the tool chokepoint, push it to failing with a
    contract captured + an L1 attempt this streak + an L2 opt-in + a pending
    L2 proposal, and return its id.

    ``repair_policy`` isn't a create_ni_item tool arg (tools default to
    ``l2_frontier: False``); the opt-in is stamped on the sealed spec inline
    alongside the other seed fields (same pattern as the contract stamp).
    """
    body = {
        "title": "Watch",
        "goal": "show a note",
        "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [{"op": "extract", "paths": {"note": "text"}}],
        "scene": _minimal_scene(),
        "display": {"size": "small"},
        "interval_minutes": 60,
        "preview_payload": {"note": "preview"},
        "draft": True,
    }
    r = client.post("/api/tools/invoke", json={"name": "create_ni_item",
                                                "args": body})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_approval", r.text
    pid = r.json()["pending_id"]
    approve = client.post(f"/api/agent/pending/{pid}/approve",
                          json={"confirm_tool": "create_ni_item"})
    assert approve.status_code == 200, approve.text
    iid = approve.json()["result"]["id"]

    store = client.app.state.ni
    current = store.get_item(iid)["spec"]
    current["_c2_ok"] = True
    current["contract"] = {"shape": {"note": "string"}}
    current["_l1_last_attempt"] = (
        datetime.now(UTC) - timedelta(minutes=5)
    ).isoformat()
    current["repair_policy"] = {"l1": True, "l2_frontier": True}
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ?, "
        "consecutive_failures = 3, first_failure_at = now() - INTERVAL '10 MINUTES', "
        "state = 'failing' WHERE id = ?;",
        [*store._seal_item(iid, current), iid],
    )
    ok = store.set_l2_proposal(iid, {
        "stages": {"extract": {"note": "text"}},
        "created_at": datetime.now(UTC).isoformat(),
        "model": "claudecode/sonnet",
    }, expected_rev=store.get_item(iid)["spec_rev"])
    assert ok, "proposal seed failed"
    return iid


def test_board_row_flags_pending_proposal(client: TestClient) -> None:
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    board = client.get("/api/ni/board").json()
    row = next(r for r in board["items"] if r["id"] == iid)
    assert row["l2_proposal"] is True


def test_apply_l2_proposal_applies_via_trial_and_clears(client: TestClient) -> None:
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    store = client.app.state.ni
    rev_before = store.get_item(iid)["spec_rev"]

    r = client.post(f"/api/ni/items/{iid}/l2-proposal/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["spec_rev"] == rev_before + 1

    after = store.get_item(iid)
    assert "_l2_proposal" not in after["spec"], "apply must clear the proposal"
    # Trial mechanics: apply_repair stamps _l1_trial{rev_before} + _l1_last_attempt.
    assert isinstance(after["spec"].get("_l1_trial"), dict)
    assert after["spec"]["_l1_trial"]["rev_before"] == rev_before
    # State/counters/contract preserved by apply_repair.
    assert after["state"] == "failing"
    assert after["spec"]["contract"] == {"shape": {"note": "string"}}
    # New revision recorded under origin repair_l2.
    row = store.conn.execute(
        "SELECT origin FROM ni_revisions WHERE item_id = ? AND rev = ?;",
        [iid, rev_before + 1],
    ).fetchone()
    assert row is not None and str(row[0]) == "repair_l2"
    # ni_runs row for the apply.
    runs = store.list_runs(iid, limit=10)
    assert any(r["status"] == "repair_l2_applied" for r in runs), runs


def test_apply_l2_proposal_409_when_absent(client: TestClient) -> None:
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    client.app.state.ni.clear_l2_proposal(iid)
    r = client.post(f"/api/ni/items/{iid}/l2-proposal/apply")
    assert r.status_code == 409, r.text


def test_dismiss_l2_proposal_clears_proposal(client: TestClient) -> None:
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    store = client.app.state.ni
    stamp_before = store.get_item(iid)["spec"].get("_l2_last_attempt")

    r = client.post(f"/api/ni/items/{iid}/l2-proposal/dismiss")
    assert r.status_code == 200, r.text
    after = store.get_item(iid)
    assert "_l2_proposal" not in after["spec"], "dismiss must strip the proposal"
    # _l2_last_attempt persists so no re-propose this streak.
    assert after["spec"].get("_l2_last_attempt") == stamp_before


def test_dismiss_l2_proposal_409_when_absent(client: TestClient) -> None:
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    client.app.state.ni.clear_l2_proposal(iid)
    r = client.post(f"/api/ni/items/{iid}/l2-proposal/dismiss")
    assert r.status_code == 409, r.text


def test_apply_and_dismiss_404_for_unknown_item(client: TestClient) -> None:
    _unlock(client)
    r = client.post("/api/ni/items/nope/l2-proposal/apply")
    assert r.status_code == 404, r.text
    r = client.post("/api/ni/items/nope/l2-proposal/dismiss")
    assert r.status_code == 404, r.text


# --- scheduler wiring: proposal status routes to kind "proposal" ----------

def test_carrier_proposal_status_maps_to_proposal_kind(client: TestClient) -> None:
    """post_ni_carrier_notices(..., proposed=[...]) writes status "proposal" ⇒
    /api/ni/notices renders kind "proposal" (§17 mapping)."""
    _unlock(client)
    store = ScheduleStore(client.app.state.dbx, client.app.state.master_key)
    sched.post_ni_carrier_notices(
        store, [], [], proposed=[{"item_id": "x", "title": "Watch"}],
    )
    rows = client.get("/api/ni/notices", headers={"X-SB-Local": "1"}).json()
    assert len(rows) == 1
    assert rows[0]["kind"] == "proposal"
    assert "a proposed fix is ready to review" in rows[0]["body"]


def test_auto_update_ni_hands_l2_candidates_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§23 wiring: scheduler._auto_update_ni forwards tick's l2_candidates to
    ni.spawn_l2_worker; a tick with no notices still fires the worker when
    candidates are present."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    app = SimpleNamespace(state=SimpleNamespace(
        master_key=key, db=SimpleNamespace(cursor=conn.cursor),
    ))

    def fake_tick(_app, pass_budget_seconds=20.0, breaker_open=None):
        return {"checked": 1, "alerts": [], "broken": [], "repaired": [],
                "l2_candidates": ["item-A", "item-B"]}

    seen: list = []

    def fake_spawn(app_arg, ids):
        seen.append({"app": app_arg, "ids": list(ids)})
        return True

    monkeypatch.setattr(sched.ni, "tick", fake_tick)
    monkeypatch.setattr(sched.ni, "spawn_l2_worker", fake_spawn)
    sched._auto_update_ni(app)
    assert seen == [{"app": app, "ids": ["item-A", "item-B"]}]


# --- Phase 4b audit (2026-09-11): D1-D8 regressions ---------------------------

def test_D1_revert_carries_l2_last_attempt_and_pops_proposal(client: TestClient) -> None:
    """D1 (Phase 4b): after a full proposal → Apply → trial-fail → revert cycle,
    ``_l2_last_attempt`` MUST survive the revert (rev-preserving system marker,
    never in the revision snapshot) and ``_l2_proposal`` MUST be stripped. Without
    the fix, L2 re-fires the same streak — unbounded per Apply.
    """
    from smartbrain_3000 import ni as nimod_local
    from smartbrain_3000 import secrets as sec_mod
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    store = client.app.state.ni

    # Stamp _l2_last_attempt at proposal time (mirrors the real worker path).
    current = store.get_item(iid)["spec"]
    current["_l2_last_attempt"] = datetime.now(UTC).isoformat()
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;",
        [*store._seal_item(iid, current), iid],
    )
    l2_stamp_before = store.get_item(iid)["spec"]["_l2_last_attempt"]

    # Apply the parked proposal via the route (§14 trial machinery).
    r = client.post(f"/api/ni/items/{iid}/l2-proposal/apply")
    assert r.status_code == 200, r.text
    after_apply = store.get_item(iid)
    assert isinstance(after_apply["spec"].get("_l1_trial"), dict)
    # Sanity: the applied spec still carries the L2 attempt marker + no proposal.
    assert after_apply["spec"].get("_l2_last_attempt") == l2_stamp_before
    assert "_l2_proposal" not in after_apply["spec"]

    # Force a trial failure through the full run pipeline: rewrite the extract
    # stage to a missing path so the trial run raises extract_miss (spec-shape).
    broken = dict(store.get_item(iid)["spec"])
    broken["pipeline"] = [{"op": "extract", "paths": {"note": "nope-not-there"}}]
    # Seal directly (bypassing update_spec which would strip _l1_trial + streak).
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;",
        [*store._seal_item(iid, broken), iid],
    )

    secrets = sec_mod.SecretStore(store.conn, client.app.state.master_key)
    schedules = ScheduleStore(store.conn, client.app.state.master_key)

    class _FakeGW:
        class GatewayError(Exception):
            def __init__(self, status_code: int, message: str) -> None:
                super().__init__(message)
                self.status_code = status_code

        def load_routes(self, _c) -> dict:
            return {"ni": "ollama/x"}

        def resolve_model(self, _cap, routes) -> str | None:
            return routes.get("ni")

        def is_local(self, model: str) -> bool:
            return True

        def local_available(self) -> bool:
            return False  # keep L1 from firing again — we're testing the L2 marker

        def chat(self, _m, _model, **_k) -> dict:
            return {"choices": [{"message": {"content": "hi"}}]}

        def completion_text(self, data: dict) -> str:
            return data["choices"][0]["message"]["content"]

    gw = _FakeGW()
    with pytest.raises(nimod_local.NIError):
        nimod_local.run_item(store, iid, gateway_mod=gw, secrets_store=secrets,
                              schedules_store=schedules)

    after = store.get_item(iid)
    # D1: _l2_last_attempt survived the revert.
    assert after["spec"].get("_l2_last_attempt") == l2_stamp_before, (
        "D1: _l2_last_attempt must survive the trial-failed revert"
    )
    # Trial cleared + proposal gone.
    assert "_l1_trial" not in after["spec"]
    assert "_l2_proposal" not in after["spec"]

    # Eligibility gate closes (one-attempt-per-streak marker present).
    assert nimod_local._l2_eligible(after, after["spec"]) is False, (
        "D1: L2 must NOT re-fire while _l2_last_attempt >= first_failure_at"
    )

    # Simulate the next tick's _collect_l2_candidate — no candidate yielded.
    candidates: list = []
    nimod_local._collect_l2_candidate(store, iid, candidates)
    assert iid not in candidates, (
        "D1: subsequent tick must NOT yield an l2_candidate for this item"
    )


def test_D2c_pack_carrying_repair_policy_refused_at_parse() -> None:
    """D2c: a template pack that ships ``repair_policy`` inside spec_template is
    refused by ``parse_pack`` — repair policy is always the installer's local choice."""
    from smartbrain_3000 import ni_library

    spec = _basic_spec()
    spec.pop("_l2_proposal", None)
    spec.pop("_l2_last_attempt", None)
    # spec_template ships an aggressive repair policy — must be refused.
    template = {
        "id": "watch-1",
        "title": "Watch",
        "goal": "show a note",
        "category": "misc",
        "tags": [],
        "notes": "n",
        "spec_template": spec,  # already carries repair_policy
        "preview_payload": {"note": "hi"},
    }
    with pytest.raises(ni_library.LibraryError, match="repair_policy"):
        ni_library._validate_one_template(template, 0, set())


def test_D2c_install_forces_default_repair_policy() -> None:
    """D2c: ``build_installed_spec`` REPLACES any repair_policy with the safe default
    ``{l1: True, l2_frontier: False}`` — a template MUST NOT invisibly opt items in."""
    from smartbrain_3000 import ni_library

    # Build a valid template via the private helper (parse-time forbids repair_policy,
    # but a corrupt pack path or a direct call must still enforce the default).
    spec = _basic_spec()
    spec["repair_policy"] = {"l1": False, "l2_frontier": True}  # hostile shape
    template = {
        "id": "watch-1", "title": "Watch", "goal": "show a note",
        "category": "misc", "tags": [], "notes": "n",
        "spec_template": spec, "preview_payload": {"note": "hi"},
    }
    installed = ni_library.build_installed_spec(template, {})
    assert installed["repair_policy"] == {"l1": True, "l2_frontier": False}, (
        "D2c: install-time repair_policy MUST be forced to the safe default"
    )


def test_D2c_export_strips_repair_policy_and_l2_state(client: TestClient) -> None:
    """D2c: /export-template strips ``repair_policy`` + `_l2_*` state — a subscriber
    installing the exported template must land on the safe default, not the exporter's
    opt-in."""
    _unlock(client)
    iid = _seed_failing_with_proposal(client)  # ships l2_frontier: True on the sealed spec
    r = client.get(f"/api/ni/items/{iid}/export-template",
                   headers={"X-SB-Local": "1"})
    assert r.status_code == 200, r.text
    exported_spec = r.json()["spec_template"]
    assert "repair_policy" not in exported_spec, (
        "D2c: export must strip repair_policy"
    )
    assert "_l2_proposal" not in exported_spec
    assert "_l2_last_attempt" not in exported_spec


def test_D2b_repair_policy_route_sets_flag(client: TestClient) -> None:
    """D2b: POST /repair-policy is the desktop-local setter — flips l2_frontier
    end-to-end + refuses without the X-SB-Local marker."""
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    store = client.app.state.ni

    # 403 without desktop-local.
    r = client.post(f"/api/ni/items/{iid}/repair-policy",
                    json={"l2_frontier": False})
    assert r.status_code == 403, r.text

    r = client.post(f"/api/ni/items/{iid}/repair-policy",
                    json={"l2_frontier": False},
                    headers={"X-SB-Local": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["repair_policy"] == {"l1": True, "l2_frontier": False}
    after = store.get_item(iid)
    assert (after["spec"].get("repair_policy") or {}).get("l2_frontier") is False


def test_D2b_repair_policy_route_404_for_unknown_item(client: TestClient) -> None:
    _unlock(client)
    r = client.post("/api/ni/items/nope/repair-policy",
                    json={"l2_frontier": True},
                    headers={"X-SB-Local": "1"})
    assert r.status_code == 404, r.text


def test_D2a_update_ni_item_tool_lands_repair_policy_through_handler() -> None:
    """D2a: the update_ni_item tool's copy loop now includes ``repair_policy``, so an
    approved edit lands the flag through the tool chokepoint (approval card renders
    the whole dict via fmtArgs = consent)."""
    from smartbrain_3000 import ni as nimod_local
    from smartbrain_3000 import tools

    # Fresh tool context — an item is created, then updated via the handler.
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    ctx = tools.ToolContext(ni=nimod_local.NIStore(conn, key))

    create_args = {
        "title": "Watch", "goal": "show a note", "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [{"op": "extract", "paths": {"note": "text"}}],
        "scene": _minimal_scene(),
        "display": {"size": "small"},
        "interval_minutes": 60,
        "preview_payload": {"note": "preview"},
        "draft": True,
    }
    create_tool = tools.get_tool("create_ni_item")
    created = create_tool.handler(ctx, tools.validate_args(create_tool, create_args))
    iid = created["id"]

    update_tool = tools.get_tool("update_ni_item")
    validated = tools.validate_args(update_tool, {
        "item_id": iid,
        "repair_policy": {"l1": True, "l2_frontier": True},
    })
    update_tool.handler(ctx, validated)

    after = ctx.ni.get_item(iid)
    assert after["spec"]["repair_policy"] == {"l1": True, "l2_frontier": True}


def test_D3_pack_carrying_l2_proposal_refused_at_parse() -> None:
    """D3: a template pack that ships a forged ``_l2_proposal`` (or ``_l2_last_attempt``)
    is refused by ``parse_pack``'s forbidden-keys guard — a subscriber MUST NOT install
    an item with an attacker-shaped proposal ready to Apply."""
    from smartbrain_3000 import ni_library

    spec = _basic_spec()
    spec.pop("repair_policy", None)
    spec["_l2_proposal"] = {
        "stages": {"extract": {"note": "text"}},
        "created_at": datetime.now(UTC).isoformat(),
        "model": "claudecode/sonnet",
    }
    template = {
        "id": "watch-2", "title": "Watch", "goal": "show a note",
        "category": "misc", "tags": [], "notes": "n",
        "spec_template": spec, "preview_payload": {"note": "hi"},
    }
    with pytest.raises(ni_library.LibraryError, match="_l2_proposal"):
        ni_library._validate_one_template(template, 0, set())


def test_D4_apply_on_broken_refuses_and_leaves_trial_unstamped(
    client: TestClient,
) -> None:
    """D4: Apply on a broken item refuses (409) — a broken item's fix path is
    edit → re-commission, and stamping ``_l1_trial`` on a spec whose engine path
    is stopped wedges the item forever."""
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    store = client.app.state.ni
    store.set_state(iid, "broken")

    r = client.post(f"/api/ni/items/{iid}/l2-proposal/apply")
    assert r.status_code == 409, r.text
    assert "re-commission" in r.json()["detail"]
    # No trial stamped.
    assert "_l1_trial" not in store.get_item(iid)["spec"]


def test_D4_broken_transition_clears_l2_proposal() -> None:
    """D4: when an item transitions to broken (secret_host_mismatch permanent-refusal
    branch), a parked ``_l2_proposal`` MUST be stripped — a stale proposal on a broken
    card is misleading (Apply would 409 anyway)."""
    store, _c, _k = _store()
    iid, _s = _make_failing_item(store)
    ok = store.set_l2_proposal(iid, {
        "stages": {"extract": {"note": "text"}},
        "created_at": datetime.now(UTC).isoformat(),
        "model": "claudecode/sonnet",
    }, expected_rev=store.get_item(iid)["spec_rev"])
    assert ok

    # Force a broken transition via the permanent-refusal class.
    exc = nimod.NIError("secret_host_mismatch", "wrong host")
    nimod._transition_on_failure(store, store.get_item(iid), exc, count=1)

    after = store.get_item(iid)
    assert after["state"] == "broken"
    assert "_l2_proposal" not in after["spec"], (
        "D4: broken transition must strip parked L2 proposals"
    )


def test_D5_failure_seals_last_failure_slot_with_excerpt_and_class() -> None:
    """D5: a spec-shape failure seals ``last_failure`` with excerpt + class + detail
    + ts — the L2 worker later reads REAL context (matching §23's envelope promise)."""
    store, conn, key = _store()
    from smartbrain_3000 import secrets as sec_mod
    secrets = sec_mod.SecretStore(conn, key)
    schedules = ScheduleStore(conn, key)

    # Build an item that reaches contract_violation on the run: the model source
    # returns text but the pipeline captures a contract keyed on the wrong field.
    iid, current = _make_failing_item(store)

    class _GW:
        class GatewayError(Exception):
            def __init__(self, sc, msg):
                super().__init__(msg)
                self.status_code = sc

        def load_routes(self, _c):
            return {"ni": "ollama/x"}

        def resolve_model(self, _cap, routes):
            return routes.get(_cap)

        def is_local(self, m):
            return True

        def local_available(self):
            # True so the model source fetches successfully; L1 stays gated
            # because ``_l1_last_attempt`` is stamped by _make_failing_item.
            return True

        def chat(self, _m, _mod, **_k):
            return {"choices": [{"message": {"content": "hello"}}]}

        def completion_text(self, d):
            return d["choices"][0]["message"]["content"]

    # Break the pipeline so the run raises extract_miss (a spec-shape class).
    broken = dict(current)
    broken["pipeline"] = [{"op": "extract", "paths": {"note": "not.there"}}]
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;",
        [*store._seal_item(iid, broken), iid],
    )

    with pytest.raises(nimod.NIError):
        nimod.run_item(store, iid, gateway_mod=_GW(), secrets_store=secrets,
                        schedules_store=schedules)

    snap = store.read_snapshot(iid, "last_failure")
    assert snap is not None and isinstance(snap["payload"], dict), snap
    body = snap["payload"]
    assert body.get("class") == "extract_miss"
    assert "excerpt" in body and isinstance(body["excerpt"], str)
    assert body["excerpt"], "excerpt must not be empty (payload was non-empty)"
    datetime.fromisoformat(body["ts"])  # ISO-8601 shape check


def test_D5_l2_prompt_contains_excerpt_and_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5: ``_attempt_l2_repair`` reads the sealed ``last_failure`` snapshot and passes
    real class + detail + excerpt to ``_build_l1_repair_prompt`` — the L2 prompt now
    matches §23's envelope promise."""
    store, conn, key = _store()
    iid, _s = _make_failing_item(store)
    # Pre-seal a last_failure snapshot with a distinctive excerpt.
    store.write_snapshot(iid, "last_failure", {
        "excerpt": '{"upstream": "REAL-EXCERPT-MARKER"}',
        "class": "extract_miss",
        "detail": "note not.there",
        "ts": datetime.now(UTC).isoformat(),
    }, ok=False)

    captured: list[list[dict]] = []

    def _capture(messages, _model, *, timeout=None, **_kw):
        captured.append(list(messages))
        return {"choices": [{"message": {"content": '{"extract": {"note": "text"}}'}}]}

    from smartbrain_3000 import gateway as real_gw
    monkeypatch.setattr(real_gw, "chat", _capture)

    nimod._run_l2_worker(_fake_app(conn, key), [iid])
    assert captured, "L2 chat must have been called"
    prompt_text = captured[0][0]["content"]
    assert "REAL-EXCERPT-MARKER" in prompt_text, (
        "D5: L2 prompt MUST carry the sealed last_failure excerpt"
    )
    assert "extract_miss" in prompt_text
    assert "note not.there" in prompt_text


def test_D5_success_clears_last_failure_slot() -> None:
    """D5: a clean run (success path) clears the sealed ``last_failure`` slot — the
    next L2 attempt on a fresh streak sees no stale context."""
    from smartbrain_3000 import secrets as sec_mod

    store, conn, key = _store()
    secrets = sec_mod.SecretStore(conn, key)
    schedules = ScheduleStore(conn, key)
    iid, _s = _make_failing_item(store)
    # Seed a last_failure slot as if a prior failure sealed one.
    store.write_snapshot(iid, "last_failure", {
        "excerpt": "old", "class": "extract_miss", "detail": "",
        "ts": datetime.now(UTC).isoformat(),
    }, ok=False)
    assert store.read_snapshot(iid, "last_failure") is not None

    # Push to a state that permits transition on success, then run cleanly.
    store.set_state(iid, "live")

    class _GW:
        class GatewayError(Exception):
            pass

        def chat(self, _m, _mod, **_k):
            return {"choices": [{"message": {"content": "hi"}}]}

        def completion_text(self, d):
            return d["choices"][0]["message"]["content"]

        def load_routes(self, _c):
            return {}

        def resolve_model(self, _c, _r):
            return "ollama/x"

        def is_local(self, _m):
            return True

        def local_available(self):
            # True so a model-source item can fetch; L1 still won't re-fire this
            # streak because the seed has `_l1_last_attempt` stamped.
            return True

    nimod.run_item(store, iid, gateway_mod=_GW(), secrets_store=secrets,
                    schedules_store=schedules)
    assert store.read_snapshot(iid, "last_failure") is None, (
        "D5: success must clear the last_failure slot"
    )


def test_D6_l2_reachable_when_l1_disabled_and_l2_on() -> None:
    """D6 (Phase 4b): an item with ``repair_policy = {l1: False, l2_frontier: True}``
    IS L2-eligible even without a stamped _l1_last_attempt — the L1 ladder is
    exhausted by definition when disabled (a policy opt-out, not a race). Without
    the fix the L2 gate was unreachable in this configuration."""
    store, _c, _k = _store()
    # Make an item that never had L1 fire; only L2 is enabled.
    iid, spec = _make_failing_item(store, l1_attempted=False)
    # Flip repair_policy: L1 disabled + L2 on.
    current = store.get_item(iid)["spec"]
    current["repair_policy"] = {"l1": False, "l2_frontier": True}
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;",
        [*store._seal_item(iid, current), iid],
    )
    item = store.get_item(iid)
    assert nimod._l2_eligible(item, item["spec"]) is True


def test_D7_spawn_worker_releases_lock_on_thread_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D7: if ``threading.Thread.start()`` raises (e.g. OS refuses to spawn a thread),
    ``_L2_WORKER_LOCK`` MUST be released so a later tick can retry — without the fix
    the lock leaks permanently and L2 stops firing process-wide."""
    store, conn, key = _store()
    iid, _s = _make_failing_item(store)

    class _RaisingThread:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(nimod.threading, "Thread", _RaisingThread)

    started = nimod.spawn_l2_worker(_fake_app(conn, key), [iid])
    assert started is False, "spawn must report failure when start() raises"
    # The lock is FREE — a second acquire in the test proves it (non-blocking).
    acquired = nimod._L2_WORKER_LOCK.acquire(blocking=False)
    try:
        assert acquired, "D7: lock must be released when Thread.start() raises"
    finally:
        if acquired:
            nimod._L2_WORKER_LOCK.release()


def test_D8_clean_run_after_proposal_voids_proposal_and_board_flag(
    client: TestClient,
) -> None:
    """D8: a clean run resets the streak AND voids the parked ``_l2_proposal`` — the
    card's l2_proposal flag flips back to False (a resolved item must not keep the
    "Fix proposed — review" chip forever)."""
    from smartbrain_3000 import secrets as sec_mod
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    store = client.app.state.ni

    # Sanity: board reports the proposal chip on.
    board = client.get("/api/ni/board").json()
    row = next(r for r in board["items"] if r["id"] == iid)
    assert row["l2_proposal"] is True

    # Drop back to a runnable state (failing has back-off; live keeps this simple).
    store.set_state(iid, "live")
    store.clear_last_checked(iid)

    class _GW:
        class GatewayError(Exception):
            pass

        def chat(self, _m, _mod, **_k):
            return {"choices": [{"message": {"content": "hi"}}]}

        def completion_text(self, d):
            return d["choices"][0]["message"]["content"]

        def load_routes(self, _c):
            return {}

        def resolve_model(self, _c, _r):
            return "ollama/x"

        def is_local(self, _m):
            return True

        def local_available(self):
            # True so a model-source item can fetch; L1 still won't re-fire this
            # streak because the seed has `_l1_last_attempt` stamped.
            return True

    secrets = sec_mod.SecretStore(store.conn, client.app.state.master_key)
    schedules = ScheduleStore(store.conn, client.app.state.master_key)
    nimod.run_item(store, iid, gateway_mod=_GW(), secrets_store=secrets,
                    schedules_store=schedules)

    after = store.get_item(iid)
    assert "_l2_proposal" not in after["spec"], (
        "D8: clean run must strip the sealed L2 proposal"
    )
    board = client.get("/api/ni/board").json()
    row = next(r for r in board["items"] if r["id"] == iid)
    assert row["l2_proposal"] is False, (
        "D8: board l2_proposal flag must flip False after streak resets"
    )


def test_cosmetic_validate_l1_trial_accepts_optional_origin() -> None:
    """Cosmetic: ``_l1_trial`` may OPTIONALLY carry an ``origin`` from _REVISION_ORIGINS."""
    spec = _basic_spec()
    spec["_l1_trial"] = {"rev_before": 1, "origin": "repair_l2"}
    nimod.validate_spec(spec)  # accepted
    spec["_l1_trial"] = {"rev_before": 1, "origin": "not-a-real-origin"}
    with pytest.raises(ValueError, match="origin"):
        nimod.validate_spec(spec)


def test_cosmetic_apply_l2_stamps_trial_origin_repair_l2(client: TestClient) -> None:
    """Cosmetic: ``apply_repair(origin='repair_l2')`` writes ``_l1_trial.origin`` so a
    later revert records the honest revision origin (``repair_l2``) instead of
    hardcoded ``repair_l1``."""
    _unlock(client)
    iid = _seed_failing_with_proposal(client)
    store = client.app.state.ni

    r = client.post(f"/api/ni/items/{iid}/l2-proposal/apply")
    assert r.status_code == 200, r.text
    trial = store.get_item(iid)["spec"].get("_l1_trial")
    assert isinstance(trial, dict), trial
    assert trial.get("origin") == "repair_l2", (
        "cosmetic: L2-applied trial must carry origin='repair_l2'"
    )
