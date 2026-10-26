"""P2 (release plan, T 2026-10-24): an ask answers in seconds, and when nothing is ready it says so at once and
offers Keep looking. Keep looking is a standing order: it looks now with research allowed, then on a backoff under
the shared daily ceiling and whenever the user keeps a new source (for free), and closes after 14 days."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_ni_answers import lib  # noqa: F401  (a fixture)
from test_ni_research_loop import (  # noqa: F401  (_v2 is an autouse fixture, client a fixture)
    _ASK,
    _INTENT,
    LOOKING,
    URL,
    Script,
    _flow,
    _v2,
    _wrap,
    client,
    good_run,
    stalls,
)

from smartbrain_3000 import (
    ni,
    ni_flow,
    ni_intake,
    ni_master,
    ni_refresh,
    ni_research_flow,
    ni_standing,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _declined(monkeypatch, lib_, **kw):
    """An ask with nothing ready: the flow as it ends without a standing order."""
    seam = Script()   # any model call would find no scripted reply
    store, item_id, out, _net = _flow(monkeypatch, _wrap(lib_), seam=seam, looking=False, cloud="anthropic/x",
                                      consented=True, **kw)
    out = {**out, "intent": dict(_INTENT)}   # sealed by the intent step, which ``_flow`` runs past
    ni_flow._flow_write(store, item_id, out)
    return store, item_id, out, seam


def _started(monkeypatch) -> list[str]:
    started: list[str] = []
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda store, item_id, **kw: started.append(item_id) or True)
    return started


def _order(store, item_id, **looking) -> None:
    record = ni_flow._flow_read(store, item_id)
    ni_flow._flow_write(store, item_id, {**record, "_looking": {**LOOKING, **looking}})


# --- the fast answer ------------------------------------------------------------------------------------------------

def test_nothing_ready_declines_at_once_with_no_model_and_offers_keep_looking(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, out, seam = _declined(monkeypatch, lib)
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "no_ready_source"
    assert seam.seen == [], "no research ran"
    field = ni_flow.board_flow_field(store, item_id)
    assert field["actions"] == ["keep_looking", "retry", "pick_source", "delete"]
    assert field["decline"]["looking"] is None
    assert field["decline"]["line"] == ni_master.DECLINE_LINES["no_ready_source"]


def test_a_standing_order_researches_and_lands_the_card_held_for_keep(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, out, _seam = _declined(monkeypatch, lib)
    seam = Script(*good_run())
    ni_research_flow.set_research_chat(lambda model, messages, tools: seam(messages, tools),
                                       cloud="anthropic/x", consented=True)
    assert ni_standing.seal(store, item_id, out)["state"] == "source", "the intent rides along: no model reads it again"
    landed = ni_flow.run_flow(store, item_id, gateway_call=lambda m, p: "{}", ni_route_model="mlx/test-local")
    assert landed["state"] == "ready" and ni.awaits_yes(store.get_item(item_id))
    assert len(seam.seen) == len(good_run())


def test_an_order_research_cannot_answer_stays_open_and_offers_to_stop(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, _out, _net = _flow(monkeypatch, _wrap(lib), seam=stalls(), cloud="anthropic/x", consented=True)
    record = ni_flow._flow_read(store, item_id)
    assert record["state"] == "declined" and record["_looking"] == LOOKING
    field = ni_flow.board_flow_field(store, item_id)
    assert field["actions"] == ["stop_looking", "pick_source", "delete"]
    assert field["decline"]["looking"] == {"since": LOOKING["since"], "next_at": None}


# --- the routes -----------------------------------------------------------------------------------------------------

def test_keep_looking_seals_the_order_and_looks_now(client, lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, _out, _ = _declined(monkeypatch, lib, store=client.app.state.ni)
    started = _started(monkeypatch)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    assert client.post(f"/api/ni/items/{item_id}/flow/keep-looking").json() == {"id": item_id, "started": True}
    record = ni_flow._flow_read(store, item_id)
    assert started == [item_id] and record["intent"] and record["request"] == _ASK
    assert record["_looking"] == {"since": NOW.isoformat(timespec="seconds"), "runs": 1,
                                  "next_at": (NOW + timedelta(days=1)).isoformat(timespec="seconds")}
    # the order is open now: a second tap is refused, and Stop looking closes it
    _order(store, item_id)
    ni_flow._flow_write(store, item_id, {**ni_flow._flow_read(store, item_id), "state": "declined",
                                         "_decline_card": ni_master.seal_decline("research_declined_budget", "", [])})
    assert client.post(f"/api/ni/items/{item_id}/flow/keep-looking").status_code == 409
    assert client.post(f"/api/ni/items/{item_id}/flow/stop-looking").json() == {"ok": True}
    record = ni_flow._flow_read(store, item_id)
    assert record["_looking"] is None and record["state"] == "declined"
    assert ni_master.decline_view(record)["reason"] == "research_gave_up", "the decline itself stays"
    assert "keep_looking" in ni_flow.board_flow_field(store, item_id)["actions"]


def test_keep_looking_is_refused_where_looking_could_not_help(client, lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, out, _ = _declined(monkeypatch, lib, store=client.app.state.ni)
    _started(monkeypatch)
    ni_flow._flow_write(store, item_id, {**out, "_decline_card": ni_master.seal_decline("source_cant_answer", "", [])})
    assert client.post(f"/api/ni/items/{item_id}/flow/keep-looking").status_code == 409
    assert client.post(f"/api/ni/items/{item_id}/flow/stop-looking").status_code == 409
    assert client.post("/api/ni/items/nope/flow/keep-looking").status_code == 404


# --- the scheduler pass ---------------------------------------------------------------------------------------------

def _open_order(monkeypatch, lib_, **looking):
    store, item_id, _out, _ = _declined(monkeypatch, lib_)
    _order(store, item_id, **looking)
    return store, item_id


def test_a_due_order_looks_again_under_the_daily_ceiling_with_a_longer_wait(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), runs=1,
                                 next_at=(NOW - timedelta(minutes=1)).isoformat())
    started = _started(monkeypatch)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    spent: list[datetime] = []
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: spent.append(now) or True)
    assert ni_standing.run_due(store) == item_id and started == [item_id] and spent == [NOW]
    looking = ni_flow._flow_read(store, item_id)["_looking"]
    assert looking["runs"] == 2 and looking["next_at"] == (NOW + timedelta(days=2)).isoformat(timespec="seconds")


def test_an_order_not_due_or_over_budget_waits(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), next_at=(NOW + timedelta(hours=1)).isoformat())
    started = _started(monkeypatch)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    assert ni_standing.run_due(store) is None and started == []
    _order(store, item_id, since=NOW.isoformat(), next_at=(NOW - timedelta(hours=1)).isoformat())
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: False)
    assert ni_standing.run_due(store) is None and started == [], "the day's research is spent"


def test_an_order_closes_after_fourteen_days(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id = _open_order(monkeypatch, lib, since=(NOW - timedelta(days=15)).isoformat(),
                                 next_at=(NOW - timedelta(days=1)).isoformat())
    started = _started(monkeypatch)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    assert ni_standing.run_due(store) is None and started == []
    record = ni_flow._flow_read(store, item_id)
    assert record["_looking"] is None and record["state"] == "declined"
    assert "keep_looking" in ni_flow.board_flow_field(store, item_id)["actions"]


def test_a_new_kept_source_makes_every_open_order_look_again_for_free(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), next_at=(NOW + timedelta(days=3)).isoformat())
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    assert ni_standing.rematch(store) == 1
    started = _started(monkeypatch)
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: (_ for _ in ()).throw(AssertionError("budget")))
    assert ni_standing.run_due(store) == item_id and started == [item_id]
    looking = ni_flow._flow_read(store, item_id)["_looking"]
    assert looking["free"] is True and "free_at" not in looking
    assert looking["next_at"] == (NOW + timedelta(days=3)).isoformat(), "the order's own schedule is kept"


def test_a_free_run_needs_the_sealed_intent_or_it_waits_for_the_orders_own_run(lib, monkeypatch) -> None:  # noqa: F811
    """A free run reads no model: a record without the intent it was understood under would ask one again."""
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), next_at=(NOW + timedelta(days=3)).isoformat())
    record = ni_flow._flow_read(store, item_id)
    ni_flow._flow_write(store, item_id, {k: v for k, v in record.items() if k != "intent"})
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    assert ni_standing.rematch(store) == 1
    started = _started(monkeypatch)
    assert ni_standing.run_due(store) is None and started == []


def test_a_free_run_never_researches(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id = _open_order(monkeypatch, lib, free=True)
    record = ni_flow._flow_read(store, item_id)
    ni_flow._flow_write(store, item_id, {**record, "state": "source"})
    assert ni_research_flow.fallback(store, item_id, _ASK, dict(_INTENT)) is None


def test_keeping_a_card_marks_the_open_orders(lib, monkeypatch) -> None:  # noqa: F811
    marked: list[object] = []
    monkeypatch.setattr(ni_standing, "rematch", lambda store: marked.append(store) or 0)
    store, first, out, _ = _flow(monkeypatch, _wrap(lib), seam=Script(*good_run()), cloud="anthropic/x",
                                 consented=True)
    assert out["state"] == "ready" and ni_intake.remember(store, store.get_item(first))
    assert marked == [store] and URL


# --- a run that cannot start, and an order that can never run (review of #548) ----------------------------------------

def _slots_full(monkeypatch) -> None:
    monkeypatch.setattr(ni_flow, "_INFLIGHT", {"other-a", "other-b"})   # two builds hold both worker slots


def test_with_every_slot_busy_a_due_order_waits_unchanged_and_nothing_is_charged(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), runs=1,
                                 next_at=(NOW - timedelta(minutes=1)).isoformat())
    before = ni_flow._flow_read(store, item_id)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: (_ for _ in ()).throw(AssertionError("charged")))
    _slots_full(monkeypatch)
    assert ni_standing.run_due(store) is None
    assert ni_flow._flow_read(store, item_id) == before, "the order is open, as it was: the next pass runs it"
    monkeypatch.setattr(ni_flow, "_INFLIGHT", set())
    started = _started(monkeypatch)
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: True)
    assert ni_standing.run_due(store) == item_id and started == [item_id]


def test_a_start_refused_after_the_check_puts_the_declined_card_back(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), runs=1,
                                 next_at=(NOW - timedelta(minutes=1)).isoformat())
    before = ni_flow._flow_read(store, item_id)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: True)

    def refused(store_, item_id_, **_kw) -> bool:   # the slot went between the check and the start
        ni_flow._fail(store_, item_id_, "busy", "another flow worker is already handling this item; retry later")
        return False
    monkeypatch.setattr(ni_flow, "start_flow_worker", refused)
    assert ni_standing.run_due(store) is None
    after = ni_flow._flow_read(store, item_id)
    assert after["state"] == "declined" and after["_looking"] == before["_looking"]
    assert after["_decline_card"] == before["_decline_card"], "the decline and its links stay"


def test_keep_looking_with_every_slot_busy_seals_a_first_look_the_next_pass_runs(client, lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, _out, _ = _declined(monkeypatch, lib, store=client.app.state.ni)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    _slots_full(monkeypatch)
    assert client.post(f"/api/ni/items/{item_id}/flow/keep-looking").json() == {"id": item_id, "started": False}
    record = ni_flow._flow_read(store, item_id)
    stamp = NOW.isoformat(timespec="seconds")
    assert record["state"] == "declined" and record["_looking"] == {"since": stamp, "runs": 0, "next_at": stamp}
    assert ni_flow.board_flow_field(store, item_id)["actions"] == ["stop_looking", "pick_source", "delete"]
    monkeypatch.setattr(ni_flow, "_INFLIGHT", set())
    started = _started(monkeypatch)
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: (_ for _ in ()).throw(AssertionError("charged")))
    assert ni_standing.run_due(store) == item_id and started == [item_id], "the user's own tap is not charged"
    looking = ni_flow._flow_read(store, item_id)["_looking"]
    assert looking["runs"] == 1 and looking["since"] == stamp


def test_an_order_that_can_never_run_closes(lib, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: (_ for _ in ()).throw(AssertionError("charged")))
    started = _started(monkeypatch)
    due = {"runs": 1, "next_at": (NOW - timedelta(minutes=1)).isoformat()}
    unreadable = _open_order(monkeypatch, lib, since="last tuesday", **due)
    lost = _open_order(monkeypatch, lib, since=NOW.isoformat(), **due)
    ni_flow._flow_write(lost[0], lost[1], {**ni_flow._flow_read(*lost), "request": ""})
    for store, item_id in (unreadable, lost):
        assert ni_standing.run_due(store) is None and started == []
        record = ni_flow._flow_read(store, item_id)
        assert record["state"] == "declined" and record["_looking"] is None
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), **due)
    monkeypatch.setattr(ni_research_flow, "can_research", lambda *_a: False)   # research was switched off since
    assert ni_standing.run_due(store) is None and started == []
    assert ni_flow._flow_read(store, item_id)["_looking"] is None


def test_free_marks_one_run_never_the_next_start(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), free=True)
    fresh = ni_flow._fresh_record(ni_flow._flow_read(store, item_id), _ASK, "source", "no: looking again")
    assert "free" not in fresh["_looking"] and fresh["_looking"]["since"] == NOW.isoformat()


def test_an_order_waits_while_the_library_is_not_there(lib, monkeypatch) -> None:  # noqa: F811
    """A Library fault at tick time is not the user's choice: the order waits, as for a busy slot, and is not closed."""
    store, item_id = _open_order(monkeypatch, lib, since=NOW.isoformat(), runs=1,
                                 next_at=(NOW - timedelta(minutes=1)).isoformat())
    before = ni_flow._flow_read(store, item_id)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    monkeypatch.setattr(ni_flow, "_resolve_library", lambda: None)
    monkeypatch.setattr(ni_refresh, "take_budget", lambda conn, now: (_ for _ in ()).throw(AssertionError("charged")))
    started = _started(monkeypatch)
    assert ni_standing.run_due(store) is None and started == []
    assert ni_flow._flow_read(store, item_id) == before


# --- the phone ceiling (2026-10-26) -----------------------------------------------------------------------------------

def test_keep_looking_from_a_phone_draws_the_daily_ceiling(client, lib, monkeypatch) -> None:  # noqa: F811
    """A tap from a paired phone starts research with model spend from the signaling node's origin, so it draws
    one unit of the shared daily ceiling; the Desktop's own tap is not counted (the owner at the desk)."""
    from smartbrain_3000.auth import relay_headers
    phone = relay_headers("phone-under-test")
    store, item_id, _out, _ = _declined(monkeypatch, lib, store=client.app.state.ni)
    _started(monkeypatch)
    monkeypatch.setattr(ni, "_clock", lambda: NOW)
    monkeypatch.setenv(ni_refresh.ENV_DAILY, "1")
    assert client.post(f"/api/ni/items/{item_id}/flow/keep-looking", headers=phone).json()["started"] is True
    assert not ni_refresh.budget_left(store.conn, datetime.now(UTC))  # the one unit went to the phone's tap
    # a second declined card: the phone is refused (429, nothing sealed), the Desktop is not
    store2, item2, _o, _ = _declined(monkeypatch, lib, store=client.app.state.ni, ask="rain in Lisbon tomorrow")
    r = client.post(f"/api/ni/items/{item2}/flow/keep-looking", headers=phone)
    assert r.status_code == 429 and "from your Mac" in r.json()["detail"], r.text
    assert "_looking" not in (ni_flow._flow_read(store2, item2) or {})
    assert client.post(f"/api/ni/items/{item2}/flow/keep-looking").json()["started"] is True
