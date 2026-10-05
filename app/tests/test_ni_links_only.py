"""Operator ruling 2026-10-05 — "Library cards; web as links."

Cards are built only from SmartBrain Library sources that DECLARE their answers. Web search results and
Library sources without declared answers (harvested datasets) are offered as LINKS — named for what they
are, nothing read off them, never built from (the pick route refuses them, the flow never builds them).
A link the user PASTES still builds a page card that waits for their YES (§33).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_ni_answers import WEATHER_URL, _picked, lib  # noqa: F401  (lib is a fixture)

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow
from smartbrain_3000.secrets import gen_master_key


@pytest.fixture(autouse=True)
def _local_build_model(monkeypatch):
    from smartbrain_3000 import gateway as _gateway
    monkeypatch.setattr(_gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


_ANSWER = {"name": "level", "label": "Level", "kind": "value", "primary": True, "words": ["level"],
           "path": "v", "type": "number"}
_DATASET_API = "https://data.example.gov/resource/abcd-1234.json?$limit=50"
_DATASET_PAGE = "https://data.example.gov/d/abcd-1234"
_DECLARED_URL = "https://api.example.org/level?site=1"
_WEB = [{"title": "River levels today", "host": "rivers.example.org", "url": "https://rivers.example.org/now"},
        {"title": "Gauge map", "host": "gauges.example.net", "url": "https://gauges.example.net/map"}]


def _record(sid: str, *, answers: list | None = None, docs: str = "", home: str = "",
            template: str = "") -> dict:
    return {"id": sid, "name": f"{sid} name", "answers": answers or [],
            "access": {"kind": "http_json", "url_template": template, "docs_url": docs},
            "provider": {"name": "Provider", "url": home}}


def _cand(sid: str, url: str) -> dict:
    return {"source_id": sid, "title": f"{sid} name", "host": url.split("/")[2], "url": url,
            "provider": "Provider", "authority": "official", "label": "", "choice": False}


class _Lib:
    """A Library double: locate offers ``cands``; ``records`` hold their answers and pages."""

    def __init__(self, cands: list[dict], records: dict[str, dict]) -> None:
        self.cands, self.records = cands, records

    def candidates(self, request: str, hint: dict | None = None) -> tuple[list[dict], list[str]]:
        return copy.deepcopy(self.cands), []

    def answers(self, source_id: str) -> list[dict]:
        return list(self.records[source_id].get("answers") or [])

    def get(self, source_id: str) -> dict | None:
        return self.records.get(source_id)


class _Search:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows, self.queries = rows or [], []

    def search(self, query: str, limit: int = 10) -> dict:
        self.queries.append(query)
        return {"results": [{"title": r["title"], "url": r["url"], "snippet": "72°F right now"}
                            for r in self.rows]}


def _wire(monkeypatch, lib_double: _Lib | None, search: _Search | None) -> None:
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", (lambda: lib_double) if lib_double else None)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", (lambda: search) if search else None)
    monkeypatch.setattr(ni_flow.pagegraph, "fetch_page_graph",
                        lambda url, **kw: (_ for _ in ()).throw(RuntimeError("no fetch")))


def _dataset_lib() -> _Lib:
    return _Lib([_cand("harvested-gauges", _DATASET_API)],
                {"harvested-gauges": _record("harvested-gauges", docs=_DATASET_PAGE, home="https://data.example.gov",
                                             template=_DATASET_API)})


def _pause(store: nimod.NIStore, request: str = "river level in Boise") -> str:
    item_id = ni_flow.create_shell_item(store, request)
    ni_flow._pause_source_pick(store, item_id, request, {"kind": "external_data", "subject": "river level"},
                               call_model=lambda _p: "{}")
    return item_id


# --- the rows: which build, which are links --------------------------------------------------------

def test_a_library_source_without_declared_answers_is_a_link_to_its_page(monkeypatch) -> None:
    lib_double = _Lib([], {
        "with": _record("with", answers=[_ANSWER]),
        "docs": _record("docs", docs=_DATASET_PAGE, template=_DATASET_API),
        "api-docs": _record("api-docs", docs=_DATASET_API, home="https://data.example.gov", template=_DATASET_API),
        "spec": _record("spec", docs="https://api.example.org/openapi.json", home="https://example.org"),
        "terms": _record("terms", docs="https://example.org/terms_of_service/", home="https://example.org"),
        "none": _record("none", docs="https://x.example.org/gtfs.zip", home="https://x.example.org/gtfs.zip",
                        template="https://x.example.org/gtfs.zip")})
    rows = [{"source_id": sid, "url": _DATASET_API, "url_template": _DATASET_API}
            for sid in ("docs", "with", "api-docs", "spec", "terms", "none")]
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib_double)
    out = ni_flow._mark_links(lib_double, rows)
    # the rows that build come first; a page is a person's page — never the API, a spec file or a terms page
    assert [(r["source_id"], r["answers"], r.get("page")) for r in out] == [
        ("with", True, None),
        ("docs", False, _DATASET_PAGE),
        ("api-docs", False, "https://data.example.gov"),
        ("spec", False, "https://example.org"),
        ("terms", False, "https://example.org")]  # "none" has no page a person reads: left off


def test_no_answering_source_offers_the_dataset_and_the_web_pages_as_links(monkeypatch) -> None:
    search = _Search(_WEB)
    _wire(monkeypatch, _dataset_lib(), search)
    store = _store()
    item_id = _pause(store)
    record = ni_flow._flow_read(store, item_id)
    assert record["error"] == ni_flow.AWAITING_SOURCE_PICK and search.queries  # the web was searched
    assert "no SmartBrain Library source answers this yet" in record["notes"][-1]
    assert [set(r) for r in record["_ranked_search"]] == [{"title", "host", "url"}] * 2  # nothing read off a page
    sugs = ni_flow.board_flow_field(store, item_id)["suggestions"]
    assert sugs == [
        {"kind": "link", "found": "dataset", "title": "harvested-gauges name", "host": "data.example.gov",
         "url": _DATASET_PAGE},  # its page, never its API address
        {"kind": "link", "found": "page", "title": "River levels today", "host": "rivers.example.org",
         "url": "https://rivers.example.org/now"},
        {"kind": "link", "found": "page", "title": "Gauge map", "host": "gauges.example.net",
         "url": "https://gauges.example.net/map"}]
    assert _DATASET_API not in json.dumps(sugs)


def test_two_datasets_of_one_provider_list_its_page_once(monkeypatch) -> None:
    home = "https://www.example-agency.gov"
    lib_double = _Lib([_cand("a", "https://api.example-agency.gov/a.json"),
                       _cand("b", "https://api.example-agency.gov/b.json")],
                      {"a": _record("a", docs="https://example-agency.gov/terms", home=home),
                       "b": _record("b", docs="https://example-agency.gov/legal", home=home)})
    _wire(monkeypatch, lib_double, None)
    store = _store()
    item_id = _pause(store)
    assert [s["url"] for s in ni_flow.board_flow_field(store, item_id)["suggestions"]] == [home]


def test_a_declared_source_is_offered_to_tap_and_the_web_is_never_searched(monkeypatch) -> None:
    lib_double = _Lib([_cand("harvested-gauges", _DATASET_API), _cand("gauge-api", _DECLARED_URL)],
                      {**_dataset_lib().records, "gauge-api": _record("gauge-api", answers=[_ANSWER])})
    search = _Search(_WEB)
    _wire(monkeypatch, lib_double, search)
    store = _store()
    item_id = _pause(store)
    assert search.queries == []
    sugs = ni_flow.board_flow_field(store, item_id)["suggestions"]
    assert [(s["kind"], s["url"]) for s in sugs] == [("library", _DECLARED_URL), ("link", _DATASET_PAGE)]


def test_nothing_found_is_an_honest_pause_with_paste_a_link(monkeypatch) -> None:
    _wire(monkeypatch, None, None)
    store = _store()
    item_id = _pause(store)
    record = ni_flow._flow_read(store, item_id)
    assert record["state"] == "source" and record["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert record["notes"][-1] == ("paused: no SmartBrain Library source answers this yet — paste a link to "
                                   "the data on the card")
    assert ni_flow.board_flow_field(store, item_id)["suggestions"] == []


# --- the flow never builds from a link ------------------------------------------------------------

def _never(*_a, **_k):
    raise AssertionError("must not run")


def test_the_flow_never_builds_from_a_link_row(monkeypatch) -> None:
    _wire(monkeypatch, _dataset_lib(), _Search(_WEB))
    store = _store()
    item_id = _pause(store)
    for url in (_WEB[0]["url"], _DATASET_PAGE, _DATASET_API):  # a web page, a dataset's page, its address
        out = ni_flow.run_flow(store, item_id, gateway_call=_never, fetcher=_never, ni_route_model="m",
                               source_url=url)
        assert out["state"] == "source" and out["error"] == ni_flow.AWAITING_SOURCE_PICK
        assert "is offered as a link" in out["notes"][-1]
    assert store.get_item(item_id)["spec"].get("_shell") is True  # nothing was built


def test_a_tapped_library_row_without_declared_answers_never_reaches_the_mapping(lib, monkeypatch) -> None:  # noqa: F811
    store = _store()
    item_id = _picked(store, lib, monkeypatch, source="no-answers")  # sealed as a legacy tap would be
    out = ni_flow._sample_and_map(store, item_id, "NYC weather", {"kind": "external_data", "wants": ["temp"]},
                                  WEATHER_URL, _never, lambda _u: {"current": {"temperature_2m": 70}})
    assert out["state"] == "unsupported" and "doesn't declare its answers" in out["error"]


def test_declared_answers_that_dont_fit_move_on_never_mapping(lib, monkeypatch) -> None:  # noqa: F811
    store = _store()
    item_id = _picked(store, lib, monkeypatch)
    rows = [{"source_id": "open-meteo-forecast", "url": WEATHER_URL, "title": "Open-Meteo", "answers": True},
            {"source_id": "other", "url": "https://other.example.org/w", "title": "Other", "answers": True}]
    record = ni_flow._flow_read(store, item_id)
    ni_flow._flow_write(store, item_id, {**record, "_ranked_library": rows})
    monkeypatch.setattr(ni_flow, "build_from_answers",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("a cell is missing from every row")))
    out = ni_flow._sample_and_map(store, item_id, "NYC weather", {"kind": "external_data", "wants": ["temperature"]},
                                  WEATHER_URL, _never, lambda _u: {"current": {"temperature_2m": 70}})
    assert out["state"] == "source" and [r["url"] for r in out["_ranked_library"]] == [
        "https://other.example.org/w"]
    assert "didn't fit its declared answers" in out["notes"][-1]


# --- the pick route ------------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup",
                                json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _route_pause(client: TestClient, monkeypatch) -> tuple[str, list[dict]]:
    lib_double = _Lib([_cand("harvested-gauges", _DATASET_API), _cand("gauge-api", _DECLARED_URL)],
                      {**_dataset_lib().records, "gauge-api": _record("gauge-api", answers=[_ANSWER])})
    _wire(monkeypatch, lib_double, None)
    store = client.app.state.ni
    item_id = _pause(store)
    # and the web pages a search found (the pause offers both when no declared source fits)
    record = ni_flow._flow_read(store, item_id)
    ni_flow._flow_write(store, item_id, {**record, "_ranked_search": copy.deepcopy(_WEB)})
    started: list[dict] = []
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda s, i, **kw: started.append({"id": i, **kw}) or True)
    return item_id, started


def test_the_pick_route_refuses_a_link_row(client, monkeypatch) -> None:
    item_id, started = _route_pause(client, monkeypatch)
    for url in (_WEB[0]["url"], _DATASET_PAGE, _DATASET_API):
        r = client.post(f"/api/ni/items/{item_id}/flow/pick-source", json={"url": url})
        assert r.status_code == 409 and "offered as a link" in r.json()["detail"], r.text
    assert started == []


def test_the_pick_route_builds_a_declared_library_row(client, monkeypatch) -> None:
    item_id, started = _route_pause(client, monkeypatch)
    r = client.post(f"/api/ni/items/{item_id}/flow/pick-source", json={"url": _DECLARED_URL})
    assert r.status_code == 200 and r.json()["started"] is True
    assert started == [{"id": item_id, "source_url": _DECLARED_URL}]
    assert ni_flow._flow_read(client.app.state.ni, item_id)["_library_source"] == "gauge-api"


def test_a_pasted_link_builds_even_when_it_was_offered_as_a_link(client, monkeypatch) -> None:
    item_id, started = _route_pause(client, monkeypatch)
    url = _WEB[0]["url"]
    r = client.post(f"/api/ni/items/{item_id}/flow/pick-source", json={"url": url, "pasted": True})
    assert r.status_code == 200 and started == [{"id": item_id, "source_url": url}]
    store = client.app.state.ni
    record = ni_flow._flow_read(store, item_id)
    assert record["_pasted"] == url and not record.get("_library_source")
    # the worker builds the page card from it, and the card waits for the user's YES
    monkeypatch.setattr(nimod, "_fetch_http_page", lambda source, item_id, secrets, **kw: {
        "text": "River level: 4.2 ft at the Boise gauge." + _PROSE, "title": "River levels today"})
    intent = json.dumps({"kind": "external_data", "subject": "river level", "cadence_minutes": 60,
                         "wants": ["river level"], "threshold": None, "display_hint": "value"})
    replies = [intent, json.dumps({"river_level": "4.2 ft"}), json.dumps({"serves": True, "gaps": [], "wrong": []})]

    def not_json(_url: str) -> object:
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    out = ni_flow.run_flow(store, item_id, gateway_call=lambda _m, _p: replies.pop(0), fetcher=not_json,
                           ni_route_model="m", source_url=url)
    assert out["state"] == "ready", out
    item = store.get_item(item_id)
    assert item["spec"]["_built_from"]["path"] == "page" and nimod.awaits_yes(item)


_PROSE = ("\nThis page is updated through the day by the office that publishes it. Readings are "
          "posted as they come in, and the times shown are local. Check back later for the next "
          "update to these readings, or contact the office with questions about them.")
