"""The declined card state (plan G6, P6, 6.1, 6.2): "a decline is acceptable, a wrong card is the failure".

An ask the system cannot answer reliably ends as ONE terminal state, ``declined``: a closed reason code, one plain
line and up to three links. ``ni_master.ENDINGS`` is the table of every v2 ending that is not a card; these tests
drive each row through the real flow and check it lands where the table says, that a declined card never refreshes
or fetches, that it reopens, and that the no-dead-end law holds over it.
"""
from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import duckdb
import pytest
from test_ni_answers import _picked, lib  # noqa: F401  (lib is a fixture)
from test_ni_auto_pick import (  # noqa: F401  (client and the autouse v2 fixture are fixtures)
    _INTENT,
    _KEY,
    _QUAKE_URL,
    _RATES_URL,
    _REQUEST,
    WEATHER_URL,
    _fetcher,
    _model,
    _row,
    _run,
    _store,
    _v2_local,
    _Wrap,
    client,
)
from test_ni_compare import ONE_THING, _ask, _spec
from test_ni_research_loop import (  # noqa: F401  (_v2 is an autouse fixture)
    _ASK,
    Script,
    _flow,
    _v2,
    _wrap,
    call,
    reply,
    text_reply,
)
from test_ni_research_tools import _draft

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni, ni_flow, ni_master, ni_watch
from smartbrain_3000.ni_research import loop
from smartbrain_3000.secrets import gen_master_key

_REPO = pathlib.Path(__file__).resolve().parents[2]
_GEN = _REPO / "tools" / "gen-ni-schema.py"
_URL = "https://data.example.org/q.json"


# ---- the table -------------------------------------------------------------------------------------------

def test_the_endings_table_is_closed_and_every_code_has_a_line() -> None:
    declining = {e: code for e, (state, code) in ni_master.ENDINGS.items() if state == "declined"}
    assert all(code in ni_master.DECLINE_REASONS for code in declining.values())
    assert set(declining.values()) == ni_master.DECLINE_REASONS, "every code is the answer of some ending"
    assert set(ni_master.DECLINE_LINES) == ni_master.DECLINE_REASONS, "every code has its plain line"
    for ending, (state, code) in ni_master.ENDINGS.items():
        assert state in ni_flow.FLOW_STATES, ending
        assert (code is None) == (state != "declined"), f"{ending}: only a decline carries a code"
    assert ni_master.TERMINAL_STATES == ni_flow._TERMINAL_STATES >= {"declined"}


def test_a_research_outcome_names_a_declining_ending() -> None:
    ctx = SimpleNamespace(urls={})  # only reached through _decline's assert on the ending
    out = loop.Outcome("declined", "cloud")
    with pytest.raises(AssertionError):
        loop._decline(ctx, out, ["x"], "pick_has_untried_row")  # a stay is not a decline


def test_a_decline_view_is_total_and_bounded() -> None:
    seal = ni_master.seal_decline("compare_unbuildable", "x" * 400, [
        {"title": "ok", "url": "https://a.example/p"}, {"title": "dup", "url": "https://a.example/p"},
        {"title": "insecure", "url": "http://b.example/p"}, {"url": 5}, "junk",
        {"title": "", "url": "https://c.example/p"}, {"title": "d", "url": "https://d.example/p"},
        {"title": "e", "url": "https://e.example/p"}])
    assert len(seal["line"]) == 200 and seal["reason"] == "compare_unbuildable"
    assert [x["url"] for x in seal["links"]] == ["https://a.example/p", "https://c.example/p", "https://d.example/p"]
    assert seal["links"][1]["title"] == "c.example", "a link with no title reads as its host"
    blind = ni_master.decline_view({"state": "declined", "_decline_card": {"reason": "made-up", "line": ""}})
    assert blind["reason"] == "no_answering_source" and blind["line"] and blind["links"] == []
    assert ni_master.decline_view({})["headline"] == "I couldn't build a reliable card for this"


# ---- each ending lands where the table says --------------------------------------------------------------

def _research_flow(monkeypatch, lib_fixture, script, *, ask=_ASK, local=False):
    kw = {"cloud": None, "local": "mlx/test-local", "consented": True} if local else \
        {"cloud": "anthropic/claude-sonnet-5-5", "local": "mlx/test-local", "consented": True}
    if local:
        monkeypatch.setenv("SMARTBRAIN_NI_RESEARCH", "local")
    _store_, _id, out, _net = _flow(monkeypatch, _wrap(lib_fixture), seam=script, ask=ask, **kw)
    return out


def _bad_proposals(monkeypatch, lib_fixture):
    bad = _draft(_URL, {"title": "metadata.title", "rows": "features"})
    steps = [reply(call("library_search", query="q")), reply(call("fetch_sample", url=_URL)),
             reply(call("propose_card", draft_spec=bad)), reply(call("propose_card", draft_spec=bad))]
    return _research_flow(monkeypatch, lib_fixture, Script(*steps), ask="latest earthquakes above 6")


def _drive_research_declined_checks(lib_fixture, monkeypatch):
    return _bad_proposals(monkeypatch, lib_fixture)


def _drive_research_declined_budget(lib_fixture, monkeypatch):
    return _research_flow(monkeypatch, lib_fixture, Script(reply(call("library_search", query="q"))))


def _drive_research_declined_no_source(lib_fixture, monkeypatch):
    return _research_flow(monkeypatch, lib_fixture, Script(text_reply('{"pick": null}')), local=True)


def _drive_no_source_research_off(lib_fixture, monkeypatch):
    wrap = _Wrap(lib_fixture, [_row("no-answers", _QUAKE_URL)])  # a source that declares no answers is only a link
    return _run(_store(), wrap, monkeypatch, _fetcher([], {}))[1]


def _drive_no_source_rows_exhausted(lib_fixture, monkeypatch):
    wrap = _Wrap(lib_fixture, [_row("usgs-quakes", _QUAKE_URL), _row("open-meteo-forecast", WEATHER_URL)])
    return _run(_store(), wrap, monkeypatch, _fetcher([], {_QUAKE_URL: {"x": 1}, WEATHER_URL: {"y": 2}}))[1]


def _drive_compare_unbuildable(lib_fixture, monkeypatch):
    wrap = _Wrap(lib_fixture, [])
    return _run(_store(), wrap, monkeypatch, _fetcher([], {}), request="compare EUR and Zorblax exchange rates")[1]


def _drive_compare_refused_at_build(lib_fixture, monkeypatch):
    store = _store()
    item_id = ni_flow.create_shell_item(store, _ask(["Nvidia", "AMD"]))
    return ni_flow._finalize(store, item_id, _spec(["Nvidia", "AMD"]), ONE_THING, note="n", born="flow",
                             fetched_at=ni._clock())


def _drive_source_cant_answer(lib_fixture, monkeypatch):
    store = _store()
    item_id = _picked(store, lib_fixture, monkeypatch, source="no-answers")  # a source that declares no answers
    return ni_flow._sample_and_map(store, item_id, "NYC weather", {"kind": "external_data", "wants": ["temp"]},
                                   WEATHER_URL, _model, lambda _u: {"current": {"temperature_2m": 70}})


def _drive_ask_unsupported(lib_fixture, monkeypatch):
    store = _store()
    item_id = ni_flow.create_shell_item(store, "days until 2027-01-01")
    monkeypatch.setattr(ni_flow, "_has_source_type", lambda name: False)
    return ni_flow._handle_computed(store, item_id, "days until 2027-01-01", {"kind": "computed_only"})


def _drive_pick_has_untried_row(lib_fixture, monkeypatch):
    store = _store()
    item_id = ni_flow.create_shell_item(store, _REQUEST)
    row = {**_row("open-meteo-forecast", WEATHER_URL), "answers": True}
    return ni_flow._pause_source_pick(store, item_id, _REQUEST, _INTENT, _model, rows=[row], web=False)


def _drive_needs_key_or_email(lib_fixture, monkeypatch):
    ni_flow.set_secrets_provider(lambda: SimpleNamespace(has=lambda *a, **k: False, get=lambda *a, **k: None))
    try:
        wrap = _Wrap(lib_fixture, [_row("fx-rates", _RATES_URL, needs_key=_KEY)])
        return _run(_store(), wrap, monkeypatch, _fetcher([], {}))[1]
    finally:
        ni_flow.set_secrets_provider(None)


def _drive_model_consent_question(lib_fixture, monkeypatch):
    store = _store()
    item_id = ni_flow.create_shell_item(store, _ASK)
    return ni_flow._ask_model_consent(store, item_id, "anthropic/x", "mlx/y")


def _drive_date_question(lib_fixture, monkeypatch):
    store = _store()
    item_id = ni_flow.create_shell_item(store, "my countdown to Christmas")
    return ni_flow._handle_computed(store, item_id, "my countdown to Christmas", {"kind": "computed_only"})


def _drive_fault(lib_fixture, monkeypatch):
    store = _store()
    item_id = _picked(store, lib_fixture, monkeypatch)
    ni_flow._flow_write(store, item_id, {**ni_flow._flow_read(store, item_id), "_remap": True})

    def boom(_u: str) -> object:
        raise OSError("down")

    return ni_flow._sample_and_map(store, item_id, "NYC weather", {"kind": "external_data", "wants": ["temp"]},
                                   WEATHER_URL, _model, boom, remap=True)


DRIVERS = {name.removeprefix("_drive_"): fn for name, fn in globals().items() if name.startswith("_drive_")}


def test_every_row_of_the_table_has_a_driver_and_every_driver_a_row() -> None:
    assert set(DRIVERS) == set(ni_master.ENDINGS)


@pytest.mark.parametrize("ending", sorted(ni_master.ENDINGS))
def test_each_ending_lands_in_the_state_the_table_names(ending, lib, monkeypatch) -> None:  # noqa: F811
    state, code = ni_master.ENDINGS[ending]
    out = DRIVERS[ending](lib, monkeypatch)
    assert out["state"] == state, (ending, out["state"], out.get("error"))
    if code is None:
        assert "_decline_card" not in out, "only a decline is sealed as one"
        return
    card = out["_decline_card"]
    assert card["reason"] == code and card["line"] and len(card["links"]) <= ni_master.DECLINE_LINKS
    assert out["error"].startswith(f"{code}:"), "the error's class is the code"
    assert not out.get("_ranked_library") and not out.get("_ranked_search"), "no pick is left to tap"
    assert out.get("_auto_picked") is None


def test_a_decline_takes_its_links_from_the_pages_the_run_saw(lib, monkeypatch) -> None:  # noqa: F811
    out = _drive_research_declined_budget(lib, monkeypatch)
    links = out["_decline_card"]["links"]
    assert [(x["host"], x["url"]) for x in links] == [("data.example.org", _URL)] and links[0]["title"]
    assert out["_decline_card"]["reason"] == "research_gave_up"


def test_a_compare_the_researcher_could_not_build_says_so(lib, monkeypatch) -> None:  # noqa: F811
    out = _research_flow(monkeypatch, lib, Script(reply(call("library_search", query="q"))),
                         ask="compare Nvidia and AMD stock prices")
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "compare_unbuildable"


def test_v1_keeps_its_unsupported_endings(lib, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v1")
    store = _store()
    item_id = _picked(store, lib, monkeypatch, source="no-answers")
    out = ni_flow._sample_and_map(store, item_id, "NYC weather", {"kind": "external_data", "wants": ["temp"]},
                                  WEATHER_URL, _model, lambda _u: {"current": {"temperature_2m": 70}})
    assert out["state"] == "unsupported" and "doesn't declare its answers" in out["error"]
    assert "_decline_card" not in out


# ---- the card the board shows ----------------------------------------------------------------------------

def test_the_board_field_carries_the_reason_the_line_the_links_and_the_way_back(lib, monkeypatch) -> None:  # noqa: F811
    out = _drive_research_declined_budget(lib, monkeypatch)
    store = _store()
    item_id = ni_flow.create_shell_item(store, _ASK)
    ni_flow._flow_write(store, item_id, {**out, "state": "declined"})
    field = ni_flow.board_flow_field(store, item_id)
    assert field["state"] == "declined" and field["reason"] == field["decline"]["line"]
    assert field["decline"]["headline"] == "I couldn't build a reliable card for this"
    assert field["decline"]["reason"] == "research_gave_up" and len(field["decline"]["links"]) <= 3
    assert field["reopen"] == ["retry", "pick_source"] and "question" not in field and "suggestions" not in field
    assert field["actions"] == ["retry", "delete"], "a shell reopens by retry; Fix and Refine are for finished cards"
    finished = ni_master.actions_for("declined", out, shell=False, awaiting_yes=False, item_state="live",
                                     has_payload=True)
    assert finished == ["retry", "fix", "refine", "delete"] and set(finished) <= ni_master.ACTION_KINDS


@pytest.mark.skipif(not _GEN.exists(), reason="the schema generator is not shipped in the app image")
def test_the_declined_flow_field_validates_against_the_generated_schema(lib, monkeypatch) -> None:  # noqa: F811
    spec = importlib.util.spec_from_file_location("_gen_ni_schema_declined", _GEN)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    schema = gen.build_schema()
    out = _drive_no_source_research_off(lib, monkeypatch)
    store = _store()
    item_id = ni_flow.create_shell_item(store, _REQUEST)
    ni_flow._flow_write(store, item_id, {**out, "state": "declined"})
    field = ni_flow.board_flow_field(store, item_id)
    assert gen.validate(field, schema["$defs"]["Flow"], schema) == []
    assert "declined" in schema["$defs"]["FlowState"]["enum"]
    assert schema["$defs"]["DeclineReason"]["enum"] == sorted(ni_master.DECLINE_REASONS)
    bad = {**field, "decline": {**field["decline"], "reason": "because"}}
    assert gen.validate(bad, schema["$defs"]["Flow"], schema), "an unknown reason code is not the wire shape"


# ---- no dead end -----------------------------------------------------------------------------------------

def test_no_declining_ending_is_a_dead_end(lib, monkeypatch) -> None:  # noqa: F811
    for ending, (state, _code) in sorted(ni_master.ENDINGS.items()):
        if state != "declined":
            continue
        out = DRIVERS[ending](lib, monkeypatch)
        surface = ni_master.terminal_surface("declined", out, shell=True)
        assert surface["reason"] and surface["decline"]["line"], ending
        assert surface.get("question") or surface.get("reopen"), ending


def test_the_watcher_files_nothing_for_declines_and_flags_a_declined_record_with_no_way_out(lib, monkeypatch) -> None:  # noqa: F811
    store = _store()
    for n, ending in enumerate(sorted(e for e, (s, _c) in ni_master.ENDINGS.items() if s == "declined")):
        item_id = ni_flow.create_shell_item(store, f"ask number {n} about things")
        ni_flow._flow_write(store, item_id, {**DRIVERS[ending](lib, monkeypatch), "request": f"ask {n}"})
    summary = ni_watch.watch_create(store)
    assert summary["high"] == 0, "every live declined record shows a reason and a way back"
    # the invariant is real: a declined view with no reopen and no question would be flagged
    monkeypatch.setattr(ni_master, "DECLINE_REOPEN", ())
    monkeypatch.setattr(ni_master, "terminal_surface",
                        lambda state, record, *, shell: {"reason": "x", "decline": {}})
    item_id = ni_flow.create_shell_item(store, "an ask with no way out")
    ni_flow._flow_write(store, item_id, ni_flow._make_record("an ask with no way out", "declined"))
    assert ni_watch._is_dead_end(store, item_id, True) is True


# ---- never refreshes, never fetches ----------------------------------------------------------------------

def test_a_declined_card_is_never_due_and_the_tick_fetches_nothing(lib, monkeypatch) -> None:  # noqa: F811
    from smartbrain_3000 import netguard
    out = _drive_no_source_research_off(lib, monkeypatch)
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    store = ni.NIStore(conn, key)
    item_id = ni_flow.create_shell_item(store, _REQUEST)
    ni_flow._flow_write(store, item_id, {**out, "state": "declined"})
    spied: list[str] = []
    for name in ("safe_fetch_json", "safe_fetch_text", "safe_fetch", "safe_fetch_feed", "safe_post_json"):
        monkeypatch.setattr(netguard, name, lambda *a, _n=name, **k: spied.append(_n))
    assert store.due_items() == [] and store.get_item(item_id)["state"] == "draft"
    app = SimpleNamespace(state=SimpleNamespace(master_key=key, db=conn))
    result = ni.tick(app)
    assert result["checked"] == 0 and spied == []
    assert ni_flow.sweep_stranded_flows(store) == 0, "a terminal record is not swept"
    assert ni_flow._flow_read(store, item_id)["state"] == "declined"


def test_the_manual_run_and_activate_routes_refuse_a_declined_shell(client, lib, monkeypatch) -> None:  # noqa: F811
    store = client.app.state.ni
    item_id = ni_flow.create_shell_item(store, _REQUEST)
    ni_flow._flow_write(store, item_id, {**_drive_no_source_research_off(lib, monkeypatch), "state": "declined"})
    assert client.post(f"/api/ni/items/{item_id}/run").status_code == 409
    assert client.post(f"/api/ni/items/{item_id}/commission").status_code == 409
    row = next(r for r in client.get("/api/ni/board").json()["items"] if r["id"] == item_id)
    assert row["flow"]["state"] == "declined" and row["flow"]["decline"]["reason"] == "no_answering_source"
    assert row["actions"] == ["retry", "delete"] and "run" not in row["actions"]


# ---- reopen ----------------------------------------------------------------------------------------------

def _declined_shell(client, lib, monkeypatch) -> str:  # noqa: F811
    store = client.app.state.ni
    item_id = ni_flow.create_shell_item(store, _REQUEST)
    ni_flow._flow_write(store, item_id, {**_drive_no_source_research_off(lib, monkeypatch), "state": "declined"})
    return item_id


def test_retry_reopens_a_declined_shell_and_drops_its_seal(client, lib, monkeypatch) -> None:  # noqa: F811
    item_id = _declined_shell(client, lib, monkeypatch)
    started: list[str] = []
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda s, i, **kw: started.append(i) or True)
    r = client.post(f"/api/ni/items/{item_id}/flow/retry")
    assert r.status_code == 200 and started == [item_id]
    record = ni_flow._flow_read(client.app.state.ni, item_id)
    assert record["state"] == "intent" and "_decline_card" not in record


def test_reopen_returns_a_declined_shell_to_the_source_pick_with_paste_a_link(client, lib, monkeypatch) -> None:  # noqa: F811
    item_id = _declined_shell(client, lib, monkeypatch)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    r = client.post(f"/api/ni/items/{item_id}/flow/reopen")
    assert r.status_code == 200 and r.json()["state"] == "source"
    record = ni_flow._flow_read(client.app.state.ni, item_id)
    assert record["state"] == "source" and record["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert "_decline_card" not in record, "the seal never outlives the declined state"
    assert "decline" not in ni_flow.board_flow_field(client.app.state.ni, item_id)


def test_retry_and_reopen_refuse_a_flow_that_is_not_over(client, lib, monkeypatch) -> None:  # noqa: F811
    store = client.app.state.ni
    item_id = ni_flow.create_shell_item(store, _REQUEST)  # a running shell
    assert client.post(f"/api/ni/items/{item_id}/flow/retry").status_code == 409
    assert client.post(f"/api/ni/items/{item_id}/flow/reopen").status_code == 409


def test_a_finished_card_hides_a_declined_remap_and_keeps_its_working_payload(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)])
    from test_ni_auto_pick import OPEN_METEO
    item_id, out = _run(store, wrap, monkeypatch, _fetcher(fetched, {WEATHER_URL: OPEN_METEO}))
    assert out["state"] == "ready"
    ni_flow._transition(store, item_id, "declined", error="source_cant_answer: x",
                        _decline_card=ni_master.seal_decline("source_cant_answer", "That source can't answer this.", []))
    assert ni_flow.board_flow_field(store, item_id) is None, "the working card keeps rendering (Fix still reopens it)"
