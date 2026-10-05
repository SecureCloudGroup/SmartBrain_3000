"""Operator ruling 2026-10-04 — "Hold open paths for a YES".

A card built from a WEB PAGE (compiled or interpreted) or by the MODEL-MAPPING path (a harvested
dataset, any source without declared answers) shows the reading it found and where it came from,
and goes live only on the user's YES. NO sends it back to the source pick without that source.
Cards built from a Library source's DECLARED ANSWERS go live as before.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_ni import _FakeGateway, _fetching_preview, _fetching_scene_spec
from test_ni_answers import (  # noqa: F401  (lib is a fixture)
    OPEN_METEO,
    WEATHER_URL,
    _picked,
    lib,
)

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow, ni_library, ni_routes
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import SecretStore, gen_master_key


@pytest.fixture(autouse=True)
def _local_build_model(monkeypatch):
    from smartbrain_3000 import gateway as _gateway
    monkeypatch.setattr(_gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})


@pytest.fixture(autouse=True)
def _no_threaded_worker(monkeypatch):
    """A real worker thread on a shared in-memory DuckDB corrupts cursors (test_ni_flow lesson)."""
    started: list[dict] = []
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda store, item_id, **kw: started.append({"id": item_id, **kw}) or True)
    return started


def _store() -> tuple[nimod.NIStore, duckdb.DuckDBPyConnection, bytes]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return nimod.NIStore(conn, key), conn, key


_PAGE_PROSE = ("\nThis page is updated through the day by the office that publishes it. Readings are "
               "posted as they come in, and the times shown are local. Check back later for the next "
               "update to these readings, or contact the office with questions about them.")
_PAGE_URL = "https://www.nhc.noaa.gov/gtwo.php"
_STORM_INTENT = {"kind": "external_data", "subject": "storms", "cadence_minutes": 1440,
                 "wants": ["tropical storms"], "threshold": None, "display_hint": "value"}


def _not_json(_url: str) -> object:
    raise json.JSONDecodeError("Expecting value", "<html>", 0)


def _page_card(monkeypatch, store: nimod.NIStore, *, ranked_search: list | None = None) -> str:
    """An interpreted page card built through the real flow (the llm + judge replies scripted)."""
    monkeypatch.setattr(nimod, "_fetch_http_page", lambda source, item_id, secrets, **kw: {
        "text": "Tropical storms: Tropical Storm Fay, 40 kt." + _PAGE_PROSE, "title": "NHC Outlook"})
    item_id = ni_flow.create_shell_item(store, "daily tropical storms")
    ni_flow._transition(store, item_id, "intent", intent=_STORM_INTENT,
                        _ranked_search=ranked_search)
    replies = [json.dumps({"tropical_storms": "Tropical Storm Fay (40 kt)"}),
               json.dumps({"serves": True, "gaps": [], "wrong": []})]
    out = ni_flow._sample_and_map(store, item_id, "daily tropical storms", _STORM_INTENT, _PAGE_URL,
                                  lambda _p: replies.pop(0), _not_json)
    assert out["state"] == "ready", out.get("error")
    return item_id


_P2_GRAPH = {
    "text": "Tide tables for the creek. High tide at 7:12 AM, low at 1:33 PM.",
    "title": "Creek Tides",
    "entities": [], "feeds": [], "meta": {}, "outline": ["h1: Tide Tables"],
    "tables": [{"caption": "", "headers": ["Time", "Height", "Tide"],
                "rows": [["7:12 AM", "5.8 ft", "High"], ["1:33 PM", "0.4 ft", "Low"]]}],
}
_P2_INTENT = {"kind": "external_data", "subject": "creek tides", "cadence_minutes": 720,
              "wants": ["high tide time"], "threshold": None, "display_hint": "value"}

_AAPL_URL = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
_AAPL = {"chart": {"result": [{"meta": {"regularMarketPrice": 227.5, "previousClose": 225.1}}]}}
_AAPL_INTENT = {"kind": "external_data", "subject": "AAPL", "cadence_minutes": 5,
                "wants": ["price"], "threshold": None, "display_hint": "value"}


def _mapping_card(store: nimod.NIStore, *, ranked_search: list | None = None) -> str:
    """A model-mapped card over a JSON dataset (no declared answers)."""
    item_id = ni_flow.create_shell_item(store, "AAPL price")
    ni_flow._transition(store, item_id, "intent", intent=_AAPL_INTENT, _ranked_search=ranked_search)

    def model(prompt: str) -> str:
        if "Choose the best candidate path" in prompt:
            return json.dumps({"price": "chart.result[0].meta.regularMarketPrice"})
        return json.dumps({"serves": True, "gaps": [], "wrong": []})

    out = ni_flow._sample_and_map(store, item_id, "AAPL price", _AAPL_INTENT, _AAPL_URL, model,
                                  lambda _u: copy.deepcopy(_AAPL))
    assert out["state"] == "ready", out.get("error")
    return item_id


# --- the flow marks where a card's reading came from ------------------------------------------

def test_an_interpreted_page_card_waits_for_a_yes(monkeypatch) -> None:
    store, _conn, _key = _store()
    item_id = _page_card(monkeypatch, store)
    item = store.get_item(item_id)
    assert item["state"] == "commissioning"
    assert item["spec"]["_built_from"] == {"path": "page", "host": "www.nhc.noaa.gov",
                                           "title": "NHC Outlook"}
    assert nimod.awaits_yes(item)


def test_a_compiled_page_card_waits_for_a_yes(monkeypatch) -> None:
    from smartbrain_3000 import pagegraph
    store, _conn, _key = _store()
    monkeypatch.setattr(nimod, "_fetch_http_page",
                        lambda source, item_id, secrets, **kw: dict(_P2_GRAPH))
    pick = next(m["id"] for m in pagegraph.enumerate_menu(_P2_GRAPH, list(_P2_INTENT["wants"]))
                if "Time where Tide=High" in m["label"])
    item_id = ni_flow.create_shell_item(store, "creek tide times")
    ni_flow._transition(store, item_id, "intent", intent=_P2_INTENT)
    queue = [json.dumps({"picks": {"high_tide_time": pick}}),
             json.dumps({"serves": True, "gaps": [], "wrong": []})]
    out = ni_flow._build_page_card(store, item_id, "creek tide times", _P2_INTENT,
                                   "https://tides.example.org/c", lambda _p: queue.pop(0))
    assert out["state"] == "ready", out
    item = store.get_item(item_id)
    assert [st["op"] for st in item["spec"]["pipeline"]] == ["graph_extract"]
    assert item["spec"]["_built_from"] == {"path": "page", "host": "tides.example.org",
                                           "title": "Creek Tides"}
    assert nimod.awaits_yes(item)


def test_a_model_mapped_dataset_card_waits_for_a_yes_and_names_the_dataset() -> None:
    store, _conn, _key = _store()
    rows = [{"title": "Apple Inc. (AAPL) chart data", "host": "query1.finance.yahoo.com",
             "url": _AAPL_URL, "evidence": []}]
    item_id = _mapping_card(store, ranked_search=rows)
    item = store.get_item(item_id)
    assert item["state"] == "commissioning"
    assert item["spec"]["_built_from"] == {"path": "mapping", "host": "query1.finance.yahoo.com",
                                           "title": "Apple Inc. (AAPL) chart data"}
    assert nimod.awaits_yes(item)


def test_a_pasted_dataset_link_has_no_title_but_still_waits() -> None:
    store, _conn, _key = _store()
    item = store.get_item(_mapping_card(store))
    assert item["spec"]["_built_from"]["title"] == ""
    assert nimod.awaits_yes(item)


def test_a_declared_answers_card_goes_live_as_before(lib, monkeypatch) -> None:  # noqa: F811
    store, _conn, _key = _store()
    item_id = _picked(store, lib, monkeypatch)
    out = ni_flow._sample_and_map(store, item_id, "NYC weather",
                                  {"kind": "external_data", "subject": "NYC weather",
                                   "cadence_minutes": 15, "wants": [], "threshold": None,
                                   "display_hint": "value"},
                                  WEATHER_URL, lambda _p: "{}", lambda _u: copy.deepcopy(OPEN_METEO))
    assert out["state"] == "ready"
    item = store.get_item(item_id)
    assert item["spec"]["_built_from"]["path"] == "declared"
    assert not nimod.awaits_yes(item)
    assert item_id in [i["id"] for i in store.due_items()]


def test_a_fix_of_a_declared_card_is_a_mapping_build_and_waits(lib, monkeypatch) -> None:  # noqa: F811
    store, _conn, _key = _store()
    intent = {"kind": "external_data", "subject": "NYC weather", "cadence_minutes": 15,
              "wants": ["temperature"], "threshold": None, "display_hint": "value"}
    item_id = _picked(store, lib, monkeypatch)
    ni_flow._sample_and_map(store, item_id, "NYC weather", intent, WEATHER_URL, lambda _p: "{}",
                            lambda _u: copy.deepcopy(OPEN_METEO))
    store.record_validation(item_id, True)
    store.set_state(item_id, "live")
    frozen = store.get_item(item_id)["spec"]
    record = ni_flow._flow_read(store, item_id)
    record["_remap"] = True
    ni_flow._flow_write(store, item_id, record)

    def model(prompt: str) -> str:
        if "Choose the best candidate path" in prompt:
            return '{"temperature": "current.temperature_2m"}'
        return '{"serves": true, "gaps": [], "wrong": []}'

    out = ni_flow._sample_and_map(store, item_id, "NYC weather", intent, WEATHER_URL, model,
                                  lambda _u: copy.deepcopy(OPEN_METEO), remap=True,
                                  keep_source=frozen["source"], keep_params={})
    assert out["state"] == "ready"
    item = store.get_item(item_id)
    assert item["state"] == "commissioning" and item["spec"]["_built_from"]["path"] == "mapping"
    assert nimod.awaits_yes(item)


# --- the engine never treats a held card as live on its own ------------------------------------

def _held_engine_item(store: nimod.NIStore) -> str:
    spec = _fetching_scene_spec()
    spec["_built_from"] = {"path": "page", "host": "example.org", "title": "Example"}
    iid = store.add_item(spec, _fetching_preview())
    store.set_state(iid, "commissioning")
    return iid


def test_a_held_card_is_never_due_until_the_yes() -> None:
    store, _conn, _key = _store()
    held = _held_engine_item(store)
    legacy = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(legacy, "commissioning")
    due = [i["id"] for i in store.due_items()]
    assert held not in due and legacy in due  # an existing card without the marker is untouched
    store.record_validation(held, True)
    assert held in [i["id"] for i in store.due_items()]


def test_runs_without_the_yes_never_promote_to_live() -> None:
    store, conn, key = _store()
    iid = _held_engine_item(store)
    secrets, schedules = SecretStore(conn, key), ScheduleStore(conn, key)
    for _ in range(3):  # a manual refresh / any run: C1 only, never live
        nimod.run_item(store, iid, gateway_mod=_FakeGateway(text="sunny"), secrets_store=secrets,
                       schedules_store=schedules)
    item = store.get_item(iid)
    assert item["state"] == "commissioning" and nimod.awaits_yes(item)
    store.record_validation(iid, True)
    nimod.run_item(store, iid, gateway_mod=_FakeGateway(text="sunny"), secrets_store=secrets,
                   schedules_store=schedules)
    assert store.get_item(iid)["state"] == "live"


def test_a_repair_keeps_the_card_held() -> None:
    store, _conn, _key = _store()
    iid = _held_engine_item(store)
    spec = copy.deepcopy(store.get_item(iid)["spec"])
    spec["goal"] = "show the temperature (repaired)"
    store.apply_repair(iid, spec, origin="repair_l1")
    item = store.get_item(iid)
    assert item["state"] == "commissioning" and nimod.awaits_yes(item)


def test_an_edit_of_a_confirmed_live_card_never_re_holds_it() -> None:
    store, _conn, _key = _store()
    iid = _held_engine_item(store)
    store.record_validation(iid, True)
    store.set_state(iid, "live")
    spec = copy.deepcopy(store.get_item(iid)["spec"])
    spec["title"] = "Renamed"
    store.update_spec(iid, spec, origin="user")  # strips _c2_ok; the state stays live
    item = store.get_item(iid)
    assert item["state"] == "live" and not nimod.awaits_yes(item)


def test_the_marker_is_closed_and_never_travels_in_a_template() -> None:
    spec = _fetching_scene_spec()
    for bad in ({"path": "library", "host": "x", "title": ""}, {"path": "page"},
                {"path": "page", "host": "x", "title": "", "ok": True}, "page"):
        spec["_built_from"] = bad
        with pytest.raises(ValueError, match="_built_from"):
            nimod.validate_spec(spec)
    assert "_built_from" in ni_routes._EXPORT_STRIP_KEYS
    assert "_built_from" in ni_library._TEMPLATE_STRIP_KEYS
    template = {"spec_template": {**_fetching_scene_spec(),
                                  "_built_from": {"path": "declared", "host": "x", "title": ""}},
                "preview_payload": _fetching_preview()}
    with pytest.raises(ni_library.LibraryError, match="_built_from"):
        ni_library._validate_template_spec_and_preview(template, "t")


# --- NO: back to the source pick without that source -------------------------------------------

_ROWS = [{"title": "NHC Outlook", "host": "www.nhc.noaa.gov", "url": _PAGE_URL, "evidence": []},
         {"title": "Storm tracker", "host": "storms.example.org",
          "url": "https://storms.example.org/now", "evidence": []}]


def test_no_re_lands_the_pick_without_the_declined_source(monkeypatch) -> None:
    store, _conn, _key = _store()
    item_id = _page_card(monkeypatch, store, ranked_search=copy.deepcopy(_ROWS))
    store.record_validation(item_id, False)
    out = ni_flow.decline_reading(store, item_id)
    assert out["kind"] == "repick"
    field = ni_flow.board_flow_field(store, item_id)
    assert field["state"] == "source"
    assert [s["url"] for s in field["suggestions"]] == ["https://storms.example.org/now"]
    assert store.get_item(item_id)["state"] == "draft"


def test_no_with_nothing_left_searches_again_without_it(monkeypatch, _no_threaded_worker) -> None:
    store, _conn, _key = _store()
    item_id = _page_card(monkeypatch, store)  # a pasted link: no row to drop
    store.record_validation(item_id, False)
    out = ni_flow.decline_reading(store, item_id)
    assert out["kind"] == "relocate"
    assert _no_threaded_worker and _no_threaded_worker[-1]["id"] == item_id
    record = ni_flow._flow_read(store, item_id)
    assert record["state"] == "intent" and _PAGE_URL in record["_declined"]
    # the re-located pick never offers the declined address again — Library or web
    monkeypatch.setattr(ni_flow, "_library_candidates", lambda request, intent: [
        {"url": _PAGE_URL, "title": "same"}, {"url": "https://other.example.org/x", "title": "o"}])
    ni_flow._pause_source_pick(store, item_id, "daily tropical storms", _STORM_INTENT, lambda _p: "{}")
    assert [r["url"] for r in ni_flow._flow_read(store, item_id)["_ranked_library"]] == [
        "https://other.example.org/x"]


def test_no_drops_the_declined_web_row_from_a_fresh_search(monkeypatch) -> None:
    store, _conn, _key = _store()
    item_id = _page_card(monkeypatch, store)
    store.record_validation(item_id, False)
    ni_flow.decline_reading(store, item_id)

    class _Search:
        pass

    monkeypatch.setattr(ni_flow, "_resolve_search_service", lambda: _Search())
    monkeypatch.setattr(ni_flow, "_s2_search_candidates", lambda service, request, intent: copy.deepcopy(_ROWS))
    monkeypatch.setattr(ni_flow, "_s2_evaluate", lambda web, intent, request: web)
    ni_flow._pause_with_web(store, item_id, "daily tropical storms", _STORM_INTENT, lambda _p: "{}")
    assert [r["url"] for r in ni_flow._flow_read(store, item_id)["_ranked_search"]] == [
        "https://storms.example.org/now"]


# --- routes: the board shows what the user judges; YES / NO -------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup",
                                json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _route_page_card(client: TestClient, monkeypatch, rows: list | None = None) -> str:
    return _page_card(monkeypatch, client.app.state.ni, ranked_search=rows)


def test_the_board_shows_the_reading_and_where_it_came_from(client, monkeypatch) -> None:
    item_id = _route_page_card(client, monkeypatch)
    row = next(r for r in client.get("/api/ni/board").json()["items"] if r["id"] == item_id)
    assert row["state"] == "commissioning"
    assert row["awaiting_yes"] == {"from": "page", "host": "www.nhc.noaa.gov", "title": "NHC Outlook"}
    assert row["payload_slot"] == "preview" and row["payload"] is not None
    assert "Tropical Storm Fay (40 kt)" in json.dumps(row["payload"])


def test_a_legacy_commissioning_card_is_not_asked_for_a_yes(client) -> None:
    store = client.app.state.ni
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(iid, "commissioning")
    row = next(r for r in client.get("/api/ni/board").json()["items"] if r["id"] == iid)
    assert row["awaiting_yes"] is None and row["payload_slot"] is None


def test_yes_confirms_and_the_next_tick_may_promote(client, monkeypatch) -> None:
    item_id = _route_page_card(client, monkeypatch)
    calls: list[str] = []

    def fake_run(request, store, item):
        calls.append(item["id"])
        return {"status": "ok"}

    monkeypatch.setattr(ni_routes, "_execute_manual_run", fake_run)
    r = client.post(f"/api/ni/items/{item_id}/validate", json={"ok": True})
    assert r.status_code == 200 and calls == [item_id]
    store = client.app.state.ni
    item = store.get_item(item_id)
    assert item["spec"].get("_c2_ok") is True and not nimod.awaits_yes(item)
    assert item_id in [i["id"] for i in store.due_items()]  # the C3 proof run is due right away
    row = next(r for r in client.get("/api/ni/board").json()["items"] if r["id"] == item_id)
    assert row["awaiting_yes"] is None and row["c2_ok"] is True


def test_no_journals_and_re_lands_the_pick(client, monkeypatch) -> None:
    item_id = _route_page_card(client, monkeypatch, rows=copy.deepcopy(_ROWS))
    r = client.post(f"/api/ni/items/{item_id}/validate", json={"ok": False})
    assert r.status_code == 200 and r.json()["repick"] == "repick"
    store = client.app.state.ni
    assert store.get_item(item_id)["state"] == "draft"
    kinds = [e["kind"] for e in store.read_journal(item_id)]
    assert "c2_wrong" in kinds
    row = next(x for x in client.get("/api/ni/board").json()["items"] if x["id"] == item_id)
    assert row["flow"]["state"] == "source"
    assert [s["url"] for s in row["flow"]["suggestions"]] == ["https://storms.example.org/now"]
    # the next pick builds and waits for its own YES
    assert row["awaiting_yes"] is None


def test_the_chat_status_says_the_card_waits_for_the_yes() -> None:
    from smartbrain_3000 import tools
    store, _conn, _key = _store()
    iid = _held_engine_item(store)
    explanation, next_action = tools._explain_state(store.get_item(iid))
    assert "WAITING FOR THE USER'S YES" in explanation and "web page" in explanation
    assert "Yes, that's it" in next_action
