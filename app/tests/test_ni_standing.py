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
