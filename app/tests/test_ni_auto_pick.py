"""S3 BE-4/5: v2 auto-pick. A new card fetches the top buildable Library source once, before any tap,
falls through the remaining rows, prefers rows that need no key, and is held for the user's Keep.

Security posture (the three tests named ``test_security_*``): the only pre-Keep fetch is of an installed
Library row through the flow's own fetcher; a row needing a key or contact email is never fetched (no
secret is read) and goes last; and nothing recurs on cadence until Keep.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_ni_answers import (  # noqa: F401  (lib is a fixture)
    _INTENT,
    _QUERY_MARK,
    OPEN_METEO,
    WEATHER_URL,
    _ir_reply,
    lib,
)

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow, ni_routes, tools
from smartbrain_3000.secrets import gen_master_key

_QUAKE_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
_RATES_URL = "https://api.rates.example.org/latest?base=USD"
_KEY = {"in": "query", "name": "API_KEY", "prefix": "", "docs_url": "https://docs.rates.example.org/key"}
_REQUEST = "NYC temperature and conditions"


@pytest.fixture(autouse=True)
def _v2_local(monkeypatch):
    from smartbrain_3000 import gateway as _gateway
    monkeypatch.setattr(_gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v2")
    monkeypatch.setattr(ni_flow.pagegraph, "fetch_page_graph",
                        lambda url, **kw: (_ for _ in ()).throw(AssertionError("no page fetch")))


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


def _row(sid: str, url: str, **extra) -> dict:
    return {"source_id": sid, "title": f"{sid} title", "host": url.split("/")[2], "url": url,
            "provider": "Provider", "authority": "official", "label": "", "choice": False, **extra}


class _Wrap:
    """The real fixture Library for answers; locate offers ``cands`` (and records the limit asked)."""

    def __init__(self, real, cands: list[dict]) -> None:
        self.real, self.cands, self.limits = real, cands, []

    def candidates(self, request: str, limit: int = 3, hint: dict | None = None):
        self.limits.append(limit)
        return copy.deepcopy(self.cands[:limit]), []

    def answers(self, source_id: str) -> list[dict]:
        return self.real.answers(source_id)

    def get(self, source_id: str) -> dict | None:
        return self.real.get(source_id)


class _Search:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 10) -> dict:
        self.queries.append(query)
        return {"results": []}


def _model(prompt: str) -> str:
    if _QUERY_MARK in prompt:
        return _ir_reply("temperature")
    return '{"serves": true, "gaps": [], "wrong": []}'


def _run(store, wrap, monkeypatch, fetch, *, search=None, request: str = _REQUEST) -> tuple[str, dict]:
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: wrap)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", (lambda: search) if search else None)
    item_id = ni_flow.create_shell_item(store, request)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(request, "intent"))
    out = ni_flow._run_external_flow(store, item_id, request, {**_INTENT, "wants": []}, None, _model, fetch)
    return item_id, out


def _fetcher(fetched: list[str], by_url: dict[str, object]):
    def fetch(url: str) -> object:
        fetched.append(url)
        if url not in by_url:
            raise AssertionError(f"fetched an address that is no Library row: {url}")
        return copy.deepcopy(by_url[url])
    return fetch


def test_v2_builds_the_top_row_before_any_tap_and_holds_it_for_keep(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)])
    item_id, out = _run(store, wrap, monkeypatch, _fetcher(fetched, {WEATHER_URL: OPEN_METEO}))
    assert out["state"] == "ready" and fetched == [WEATHER_URL]
    assert wrap.limits == [5]  # v2 looks five rows deep
    item = store.get_item(item_id)
    assert item["spec"]["_built_from"]["path"] == "auto" and item["spec"]["_built_from"]["host"] == "api.open-meteo.com"
    assert item["state"] == "commissioning" and nimod.awaits_yes(item)  # the real card, as a preview, until Keep
    assert store.read_snapshot(item_id, "preview") is not None
    assert (ni_flow._flow_read(store, item_id) or {}).get("_try", {}).get("n") == 1


def test_the_first_row_that_does_not_fit_falls_through_to_the_second(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("usgs-quakes", _QUAKE_URL), _row("open-meteo-forecast", WEATHER_URL)])
    search = _Search()
    item_id, out = _run(store, wrap, monkeypatch,
                        _fetcher(fetched, {_QUAKE_URL: {"nothing": "here"}, WEATHER_URL: OPEN_METEO}), search=search)
    assert out["state"] == "ready" and fetched == [_QUAKE_URL, WEATHER_URL]
    assert search.queries == []  # a move-on with a row left never searches the web
    record = ni_flow._flow_read(store, item_id)
    assert record["_try"] == {"n": 2, "of": 2, "host": "api.open-meteo.com"}
    assert store.get_item(item_id)["spec"]["_built_from"]["path"] == "auto"


def test_exhausting_every_row_never_searches_the_web_and_stops_honestly(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("usgs-quakes", _QUAKE_URL), _row("open-meteo-forecast", WEATHER_URL)])
    search = _Search()
    item_id, out = _run(store, wrap, monkeypatch,
                        _fetcher(fetched, {_QUAKE_URL: {"x": 1}, WEATHER_URL: {"y": 2}}), search=search)
    assert fetched == [_QUAKE_URL, WEATHER_URL]  # only Library rows were fetched: no search result page
    assert search.queries == []  # v2 never searches the web before Keep
    assert out["state"] == "source" and out["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert store.get_item(item_id)["spec"].get("_shell") is True  # nothing was built
    assert not out.get("_auto_picked")


def test_only_link_rows_is_the_honest_stop_and_builds_nothing(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("no-answers", _QUAKE_URL)])  # a source that declares no answers is a link
    search = _Search()
    item_id, out = _run(store, wrap, monkeypatch, _fetcher(fetched, {}), search=search)
    assert fetched == [] and out["state"] == "source" and out["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert store.get_item(item_id)["spec"].get("_shell") is True
    assert search.queries == [] and not out.get("_ranked_search")  # no web search, no web pages


def test_a_keyless_row_is_tried_before_a_row_that_needs_a_key(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("fx-rates", _RATES_URL, needs_key=_KEY),
                       _row("open-meteo-forecast", WEATHER_URL)])
    item_id, out = _run(store, wrap, monkeypatch, _fetcher(fetched, {WEATHER_URL: OPEN_METEO}))
    assert out["state"] == "ready" and fetched == [WEATHER_URL]
    assert (ni_flow._flow_read(store, item_id) or {}).get("_access") is None  # never asked for a key


def test_v1_still_parks_on_the_pick(lib, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v1")
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)])
    wrap.candidates = lambda request, hint=None: (copy.deepcopy(wrap.cands), [])  # the v1 call carries no limit
    item_id, out = _run(store, wrap, monkeypatch, _fetcher(fetched, {}))
    assert fetched == [] and out["state"] == "source" and out["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert out["_ranked_library"][0]["url"] == WEATHER_URL and "_try" not in out


def test_no_to_a_kept_reading_moves_on_to_the_next_source_automatically(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL), _row("usgs-quakes", _QUAKE_URL)])
    fetch = _fetcher(fetched, {WEATHER_URL: OPEN_METEO, _QUAKE_URL: {"x": 1}})
    item_id, out = _run(store, wrap, monkeypatch, fetch)
    assert out["state"] == "ready" and fetched == [WEATHER_URL]
    started: list[str] = []
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda s, i, **kw: started.append(i) or True)
    assert ni_flow.decline_reading(store, item_id) == {"kind": "relocate"}  # never a pick pause
    record = ni_flow._flow_read(store, item_id)
    assert started == [item_id] and WEATHER_URL in record["_declined"] and record["state"] == "intent"
    ni_flow._auto_pick(store, item_id, _REQUEST, _INTENT, _model, fetch)  # what the restarted worker runs
    assert fetched == [WEATHER_URL, _QUAKE_URL]  # the declined row is not offered again


# --- the security posture ----------------------------------------------------------------------

def test_security_auto_fetch_only_reaches_library_rows_through_the_flow_fetcher(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("usgs-quakes", _QUAKE_URL), _row("open-meteo-forecast", WEATHER_URL)])
    # nothing reaches the network except through the fetcher the flow was given (netguard's own door)
    for name in ("safe_fetch_text", "safe_fetch_json"):
        monkeypatch.setattr(ni_flow._netguard_mod, name,
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("a side fetch")))
    _run(store, wrap, monkeypatch, _fetcher(fetched, {_QUAKE_URL: {"x": 1}, WEATHER_URL: OPEN_METEO}), search=_Search())
    assert set(fetched) <= {r["url"] for r in wrap.cands}  # each fetch is a Library row's own address
    assert len(fetched) == 2  # the page browser is guarded by the autouse fixture


class _NoSecrets:
    """A secret store that fails the test on any touch."""

    def __getattr__(self, name: str):
        raise AssertionError(f"a secret was touched before Keep: {name}")


def test_security_a_row_that_needs_a_key_is_never_fetched_and_no_secret_is_read(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    ni_flow.set_secrets_provider(lambda: _NoSecrets())
    monkeypatch.setattr(ni_flow, "_fetch_with_access",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("keyed fetch before Keep")))
    try:
        wrap = _Wrap(lib, [_row("fx-rates", _RATES_URL, needs_key=_KEY)])
        item_id, out = _run(store, wrap, monkeypatch, _fetcher(fetched, {}))
    finally:
        ni_flow.set_secrets_provider(None)
    assert fetched == [] and out["state"] == "awaiting_access"  # pauses for access exactly as a tap does
    assert out["source_url"] == _RATES_URL and (ni_flow._flow_read(store, item_id) or {})["_access"]["key"]
    # a contact-email row is the same: nothing is sent until the user gives it
    wrap = _Wrap(lib, [_row("fx-rates", _RATES_URL, needs_contact=True)])
    _item2, out2 = _run(store, wrap, monkeypatch, _fetcher(fetched, {}), request="SEC filings for Apple")
    assert fetched == [] and out2["state"] == "awaiting_access"


def test_security_nothing_recurs_before_keep_and_keep_releases_it(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    wrap = _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)])
    item_id, out = _run(store, wrap, monkeypatch, _fetcher(fetched, {WEATHER_URL: OPEN_METEO}))
    assert out["state"] == "ready"
    item = store.get_item(item_id)
    assert nimod.awaits_yes(item) and item["last_checked"] is None
    assert item_id not in [i["id"] for i in store.due_items()]  # the tick never picks it up
    assert store.read_snapshot(item_id, "latest") is None  # no cadence run happened
    store.record_validation(item_id, True)  # Keep
    assert not nimod.awaits_yes(store.get_item(item_id))
    assert item_id in [i["id"] for i in store.due_items()]


# --- Keep: the existing YES on the held card ----------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _auto_card(client, lib, monkeypatch) -> str:  # noqa: F811
    wrap = _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)])
    item_id, out = _run(client.app.state.ni, wrap, monkeypatch, _fetcher([], {WEATHER_URL: OPEN_METEO}))
    assert out["state"] == "ready"
    return item_id


def test_the_board_asks_keep_on_the_auto_built_card_with_its_preview(client, lib, monkeypatch) -> None:  # noqa: F811
    item_id = _auto_card(client, lib, monkeypatch)
    row = next(r for r in client.get("/api/ni/board").json()["items"] if r["id"] == item_id)
    assert row["awaiting_yes"] == {"from": "library", "host": "api.open-meteo.com", "title": "open-meteo-forecast title"}
    assert row["payload_slot"] == "preview" and row["payload"] is not None
    assert row["actions"] == ["keep", "dismiss"]


def test_keep_is_the_existing_yes_and_releases_the_card_to_cadence(client, lib, monkeypatch) -> None:  # noqa: F811
    item_id = _auto_card(client, lib, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(ni_routes, "_execute_manual_run", lambda r, s, item: calls.append(item["id"]) or {"status": "ok"})
    r = client.post(f"/api/ni/items/{item_id}/validate", json={"ok": True})
    assert r.status_code == 200 and calls == [item_id]
    store = client.app.state.ni
    item = store.get_item(item_id)
    assert item["spec"].get("_c2_ok") is True and not nimod.awaits_yes(item)
    assert item_id in [i["id"] for i in store.due_items()]


def test_the_marker_is_closed_stripped_on_export_and_named_in_chat_status(lib, monkeypatch) -> None:  # noqa: F811
    store = _store()
    item_id, _out = _run(store, _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)]), monkeypatch,
                         _fetcher([], {WEATHER_URL: OPEN_METEO}))
    spec = copy.deepcopy(store.get_item(item_id)["spec"])
    nimod.validate_spec(spec)  # "auto" is a member of the closed set
    spec["_built_from"]["path"] = "library"
    with pytest.raises(ValueError, match="_built_from"):
        nimod.validate_spec(spec)
    assert "_built_from" in ni_routes._EXPORT_STRIP_KEYS  # a template can neither claim nor shed it
    explanation, _next = tools._explain_state(store.get_item(item_id))
    assert "WAITING FOR THE USER'S YES" in explanation and "SmartBrain Library source" in explanation


def test_a_v1_declared_card_is_still_not_held(lib, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v1")
    store = _store()
    item_id = ni_flow.create_shell_item(store, _REQUEST)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(_REQUEST, "source"))
    ni_flow.seal_library_pick(store, item_id, WEATHER_URL,
                              {"source_id": "open-meteo-forecast", "url": WEATHER_URL, "format": "json", "params": {}})
    out = ni_flow._sample_and_map(store, item_id, _REQUEST, _INTENT, WEATHER_URL, _model,
                                  lambda _u: copy.deepcopy(OPEN_METEO))
    assert out["state"] == "ready"
    item = store.get_item(item_id)
    assert item["spec"]["_built_from"]["path"] == "declared" and not nimod.awaits_yes(item)


def _held(source: dict) -> dict:
    return {"state": "commissioning", "spec": {"source": source, "_built_from": {"path": "auto", "host": "h", "title": "t"}}}


def test_awaiting_yes_names_a_key_or_email_the_preview_fetch_used() -> None:
    plain = ni_routes._awaiting_yes_of(_held({"kind": "http_json", "url": "https://h/x"}))
    assert plain == {"from": "library", "host": "h", "title": "t"} and "used_key" not in plain
    keyed = ni_routes._awaiting_yes_of(_held({"url": "https://h/x", "secret_query": {"k": {"$secret": "ni:i:api_key"}}}))
    assert keyed["used_key"] is True
    header = ni_routes._awaiting_yes_of(_held({"url": "https://h/x", "headers": {"X-Key": {"$secret": "ni:i:api_key"}}}))
    assert header["used_key"] is True
    emailed = ni_routes._awaiting_yes_of(_held({"url": "https://h/x", "contact_ua": True}))
    assert emailed["used_key"] is True


def test_no_to_a_lookup_source_declines_the_row_address_not_only_the_resolved_one(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    row_url = "https://api.weather.example.gov/points/{{lat}},{{lon}}"  # a lookup row: the url before it is resolved
    wrap = _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)])
    item_id, out = _run(store, wrap, monkeypatch, _fetcher(fetched, {WEATHER_URL: OPEN_METEO}))
    assert out["_pick_url"] == WEATHER_URL  # _auto_continue remembers the row it tried
    ni_flow._flow_write(store, item_id, {**out, "_pick_url": row_url, "_library_url": "https://api.weather.example.gov/p/40,-74"})
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda s, i, **kw: True)
    ni_flow.decline_reading(store, item_id)
    declined = ni_flow._flow_read(store, item_id)["_declined"]
    assert row_url in declined and "https://api.weather.example.gov/p/40,-74" in declined
    assert ni_flow._without_declined(store, item_id, [_row("nws", row_url)]) == []  # not offered again


# --- a held card is not run until Keep ----------------------------------------------------------

def test_a_held_card_refuses_the_chat_tool_run_and_the_manual_run_route(client, lib, monkeypatch) -> None:  # noqa: F811
    item_id = _auto_card(client, lib, monkeypatch)
    store = client.app.state.ni
    assert nimod.awaits_yes(store.get_item(item_id))
    with pytest.raises(ValueError, match="Keep"):
        tools._run_ni_item_now(tools.ToolContext(ni=store), {"item_id": item_id})  # chat tool; MCP has no NI tools
    r = client.post(f"/api/ni/items/{item_id}/run")
    assert r.status_code == 409 and "Keep" in r.json()["detail"]
    assert store.get_item(item_id)["last_checked"] is None and store.read_snapshot(item_id, "latest") is None
    store.record_validation(item_id, True)  # Keep
    assert tools._run_ni_item_now(tools.ToolContext(ni=store), {"item_id": item_id})["ok"] is True


def test_a_flag_flip_mid_build_still_holds_the_auto_card(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    base = _fetcher(fetched, {WEATHER_URL: OPEN_METEO})

    def fetch(url: str) -> object:
        monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v1")  # the flag flips while the fetch is out
        return base(url)

    wrap = _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)])
    item_id, out = _run(store, wrap, monkeypatch, fetch)
    item = store.get_item(item_id)
    assert out["state"] == "ready" and item["spec"]["_built_from"]["path"] == "auto"
    assert nimod.awaits_yes(item)  # held for Keep: it does not run on cadence


# --- audit ----------------------------------------------------------------------------------------

class _AuditSpy:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def append(self, *a, **k) -> str:
        self.rows.append((a, k))
        return "id"


def test_each_auto_picked_fetch_is_audited_with_its_host(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched, spy = _store(), [], _AuditSpy()
    ni_flow.set_audit_sink(lambda: spy)
    try:
        wrap = _Wrap(lib, [_row("usgs-quakes", _QUAKE_URL), _row("open-meteo-forecast", WEATHER_URL)])
        _run(store, wrap, monkeypatch, _fetcher(fetched, {_QUAKE_URL: {"x": 1}, WEATHER_URL: OPEN_METEO}))
    finally:
        ni_flow.set_audit_sink(None)
    assert len(fetched) == 2 and [r[0][1] for r in spy.rows] == ["ni_auto_pick"] * 2
    assert [json.loads(r[1]["args_summary"])["host"] for r in spy.rows] == ["earthquake.usgs.gov", "api.open-meteo.com"]


def test_keep_on_an_auto_card_is_audited(client, lib, monkeypatch) -> None:  # noqa: F811
    item_id = _auto_card(client, lib, monkeypatch)
    before = {r["tool"] for r in client.app.state.audit.list(500)}
    assert "ni_auto_pick" in before and "ni_keep" not in before  # the worker's fetch reached the real log
    assert client.post(f"/api/ni/items/{item_id}/validate", json={"ok": True}).status_code == 200
    kept = [r for r in client.app.state.audit.list(500) if r["tool"] == "ni_keep"]
    assert len(kept) == 1 and "api.open-meteo.com" in kept[0]["args_summary"]
