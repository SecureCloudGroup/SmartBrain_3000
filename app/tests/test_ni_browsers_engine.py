"""Round 20 (B3a, ni-format §35): a page-browser card in the engine and on the board — the sealed
``render`` shape and every refusal (packs, export, template install, tools, model-written), the
Activate consent and how it survives (only with an unchanged source), the draft → Activate round
trip, the switch-feed door, the single-flight render worker off the tick (busy is never a
failure, unavailable starts no streak, its own due window), the interval floor, the startup
sweep of stray engines, and the Status fields. Deterministic and offline."""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_ni import _basic_spec

from smartbrain_3000 import (
    browsers,
    ni_flow,
    ni_library,
    ni_routes,
    status_routes,
    tools,
)
from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000.browsers import proc, router, runner
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import SecretStore, gen_master_key

URL = "https://tides.example.org/creek"
TEXT_SCENE = {"type": "stack", "dir": "v", "gap": "sm", "children": [
    {"type": "text", "value": "{{text}}", "role": "title", "tone": "default", "size": "md"}]}
ELIGIBLE = {"eligible": True, "engine": "obscura", "version": "0.2.4", "reason": ""}
RENDERED_HTML = (b"<html><head><title>Creek</title></head><body><p>High tide at 7:12 AM and low "
                 b"tide at 1:33 PM on the creek today, posted by the harbor office.</p></body></html>")


def _source(sites: list[str] | None = None) -> dict:
    return {"type": "http_page", "url": URL, "render": "browser", "sites": sites or ["example.org"]}


def _spec(sites: list[str] | None = None, **over: object) -> dict:
    return _basic_spec(scene=copy.deepcopy(TEXT_SCENE), source=_source(sites), **over)


def _store() -> tuple[nimod.NIStore, duckdb.DuckDBPyConnection, bytes]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return nimod.NIStore(conn, key), conn, key


def _sealed(store: nimod.NIStore, sites: list[str] | None = None, state: str = "live", **over: object) -> str:
    item_id = store.add_item(_spec(sites, **over), {"text": "preview"})
    store.set_state(item_id, state)
    return item_id


# ---- the sealed shape -------------------------------------------------------------------------

@pytest.mark.parametrize("source", [
    {"type": "http_page", "url": URL, "render": "browser"},                       # render without sites
    {"type": "http_page", "url": URL, "sites": ["example.org"]},                  # sites without render
    {**_source(), "render": "chromium"},
    {**_source(), "url": "http://tides.example.org/creek"},
    {**_source(), "url": "https://tides.example.org/{{param:x}}"},
    {**_source(), "url": "https://203.0.113.9/creek", "sites": ["113.9"]},
    {**_source(), "headers": {"X-Trace": "1"}},
    {**_source(), "sites": ["other.org"]},                                       # sites[0] is not the url's site
    {**_source(), "sites": ["Example.org"]},
    {**_source(), "sites": ["example.org", "cdn.example.net"]},                  # not a registrable domain
    {**_source(), "sites": ["example.org", "example.org"]},
    {**_source(), "sites": ["example.org"] + [f"s{i}.com" for i in range(12)]},
    {**_source(), "sites": []},
])
def test_the_render_seal_is_a_closed_shape(source: dict) -> None:
    with pytest.raises(ValueError):
        nimod.validate_spec(_basic_spec(scene=copy.deepcopy(TEXT_SCENE), source=source))


def test_a_valid_seal_and_its_attestations_validate_and_a_legacy_page_spec_is_untouched() -> None:
    spec = _spec(["example.org", "tidecdn.net"], _render_ok=True,
                 _feed_candidates=[{"url": "https://api.tidecdn.net/v1/t.json", "site": "tidecdn.net"}])
    assert nimod.validate_spec(spec)["source"]["sites"] == ["example.org", "tidecdn.net"]
    legacy = _basic_spec(scene=copy.deepcopy(TEXT_SCENE), source={"type": "http_page", "url": URL})
    assert nimod.validate_spec(legacy)["source"] == {"type": "http_page", "url": URL}
    assert not nimod.spec_renders(legacy) and nimod.spec_renders(spec)
    for bad in ({"_render_ok": "yes"}, {"_feed_candidates": [{"url": "https://other.com/x.json",
                                                               "site": "other.com"}]},
                {"_feed_candidates": [{"url": "http://example.org/x.json", "site": "example.org"}]},
                {"_feed_candidates": [{"url": f"https://example.org/{i}.json", "site": "example.org"}
                                      for i in range(6)]}):
        with pytest.raises(ValueError):
            nimod.validate_spec({**spec, **bad})
    with pytest.raises(ValueError):  # a consent on a card that renders nothing
        nimod.validate_spec({**legacy, "_render_ok": True})


def test_packs_refuse_the_seal_and_install_export_strip_it() -> None:
    from test_ni_library import _template
    template = _template()
    template["spec_template"]["source"] = {"type": "http_page", "url": URL, "render": "browser",
                                           "sites": ["example.org"]}
    with pytest.raises(ni_library.LibraryError, match="source must not carry"):
        ni_library._validate_one_template(template, 0, set())
    for key in ("_render_ok", "_feed_candidates"):
        bad = _template()
        bad["spec_template"][key] = True
        with pytest.raises(ni_library.LibraryError, match="system-owned"):
            ni_library._validate_one_template(bad, 0, set())
    installed = ni_library.build_installed_spec(template, {"zip": "02101"})
    assert installed["source"] == {"type": "http_page", "url": URL}
    store, conn, key = _store()
    item = store.get_item(_sealed(store, ["example.org", "tidecdn.net"], _render_ok=True))
    exported = ni_routes._sanitize_spec_for_export(item, SecretStore(conn, key))
    assert exported["source"] == {"type": "http_page", "url": URL, "headers": {}} or \
        exported["source"] == {"type": "http_page", "url": URL}
    assert "_render_ok" not in exported and "_feed_candidates" not in exported


def test_tools_and_models_can_never_write_the_seal() -> None:
    args = {"title": "t", "goal": "g", "source": _source(), "pipeline": [], "scene": TEXT_SCENE,
            "display": {"size": "small"}, "interval_minutes": 30, "preview_payload": {"text": "x"}}
    with pytest.raises(ValueError, match="sealed by the card flow"):
        tools._validate_create_ni_args(args)
    with pytest.raises(ValueError, match="sealed by the card flow"):
        tools._validate_update_ni_patch({"item_id": "00000000-0000-4000-8000-000000000000", "source": _source()})
    assert "_render_ok" not in tools._assemble_spec({**args, "_render_ok": True})


# ---- the Activate consent and how it lasts ------------------------------------------------------

def test_the_consent_survives_only_an_unchanged_source_and_is_never_written_by_an_update() -> None:
    store, _conn, _key = _store()
    item_id = _sealed(store, ["example.org", "tidecdn.net"], state="draft")
    forged = {**store.get_item(item_id)["spec"], "_render_ok": True}
    store.update_spec(item_id, forged, origin="agent")
    assert "_render_ok" not in store.get_item(item_id)["spec"]  # an update can't grant it
    assert store.record_render_consent(item_id) == ["example.org", "tidecdn.net"]
    item = store.get_item(item_id)
    assert item["state"] == "commissioning" and item["spec"]["_render_ok"] is True
    store.update_spec(item_id, {**item["spec"], "title": "Creek tides"}, origin="agent")
    assert store.get_item(item_id)["spec"]["_render_ok"] is True  # same page, same sites
    repaired = {**store.get_item(item_id)["spec"], "goal": "repaired goal"}
    store.apply_repair(item_id, repaired, origin="repair_l1")
    assert store.get_item(item_id)["spec"]["_render_ok"] is True
    wider = copy.deepcopy(store.get_item(item_id)["spec"])
    wider["source"]["sites"] = ["example.org", "example.net", "tidecdn.net"]
    store.update_spec(item_id, wider, origin="agent")
    assert "_render_ok" not in store.get_item(item_id)["spec"]  # a wider site set asks again


def test_activate_is_refused_unless_a_draft_waits_for_its_sites() -> None:
    store, _conn, _key = _store()
    first_party = _sealed(store, ["example.org"], state="draft")
    live = _sealed(store, ["example.org", "tidecdn.net"], state="live")
    for item_id in (first_party, live):
        with pytest.raises(ValueError, match="not waiting"):
            store.record_render_consent(item_id)


# ---- the board, Activate and switch-feed routes ------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup",
                                json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _row(client: TestClient, item_id: str) -> dict:
    return next(r for r in client.get("/api/ni/board").json()["items"] if r["id"] == item_id)


def test_the_draft_activate_round_trip(client) -> None:
    store = client.app.state.ni
    feeds = [{"url": "https://api.tidecdn.net/v1/t.json", "site": "tidecdn.net"}]
    item_id = _sealed(store, ["example.org", "tidecdn.net"], state="draft", _feed_candidates=feeds)
    row = _row(client, item_id)
    assert row["rendered"] is True and row["state"] == "draft"
    assert row["draft"] == {"reason": "render_sites", "sites": ["example.org", "tidecdn.net"]}
    assert row["render"] == {"sites": ["example.org", "tidecdn.net"], "unavailable": False}
    assert row["preview"] == {"feed_candidates": feeds}
    refused = client.post(f"/api/ni/items/{item_id}/commission")
    assert refused.status_code == 409 and "Activate" in refused.json()["detail"]
    done = client.post(f"/api/ni/items/{item_id}/activate")
    assert done.status_code == 200 and done.json() == {"state": "commissioning",
                                                       "sites": ["example.org", "tidecdn.net"]}
    row = _row(client, item_id)
    assert row["draft"] is None and row["state"] == "commissioning"
    assert store.get_item(item_id)["spec"]["_render_ok"] is True
    assert client.post(f"/api/ni/items/{item_id}/activate").status_code == 409
    assert client.post("/api/ni/items/nope/activate").status_code == 404


def test_a_first_party_draft_commissions_as_before_and_legacy_rows_carry_no_render(client) -> None:
    store = client.app.state.ni
    item_id = _sealed(store, ["example.org"], state="draft")
    assert _row(client, item_id)["draft"] is None
    assert client.post(f"/api/ni/items/{item_id}/activate").status_code == 409
    assert client.post(f"/api/ni/items/{item_id}/commission").status_code == 200
    legacy = store.add_item(_basic_spec(scene=copy.deepcopy(TEXT_SCENE),
                                        source={"type": "http_page", "url": URL}), {"text": "p"})
    assert not {"rendered", "render", "draft", "preview"} & set(_row(client, legacy))  # unchanged rows


def test_switch_feed_accepts_only_a_sealed_candidate(client, monkeypatch) -> None:
    started: list[dict] = []
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda store, item_id, **kw: started.append({"id": item_id, **kw}) or True)
    store = client.app.state.ni
    feed = "https://api.tidecdn.net/v1/t.json"
    item_id = _sealed(store, ["example.org", "tidecdn.net"], state="draft",
                      _feed_candidates=[{"url": feed, "site": "tidecdn.net"}])
    other = client.post(f"/api/ni/items/{item_id}/switch-feed", json={"url": "https://api.tidecdn.net/other"})
    assert other.status_code == 409 and started == []
    ok = client.post(f"/api/ni/items/{item_id}/switch-feed", json={"url": feed})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "started": True}
    assert started == [{"id": item_id, "source_url": feed}]
    assert ni_flow._flow_read(store, item_id)["_pasted"] == feed


def test_a_manual_run_while_the_page_browser_is_busy_is_a_409_that_writes_nothing(client, monkeypatch) -> None:
    store = client.app.state.ni
    item_id = _sealed(store, ["example.org"], state="live")
    monkeypatch.setattr(runner, "slot_busy", lambda: True)
    got = client.post(f"/api/ni/items/{item_id}/run")
    assert got.status_code == 409
    assert got.json() == {"detail": "the local page browser is busy — try again in a moment",
                          "code": "render_busy"}  # the client matches the closed code
    item = store.get_item(item_id)
    assert item["last_checked"] is None and item["last_status"] in (None, "")


def test_a_render_that_finds_the_slot_taken_mid_run_is_a_409_with_the_busy_code(client, monkeypatch) -> None:
    store = client.app.state.ni
    item_id = _sealed(store, ["example.org"], state="live")
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    monkeypatch.setattr(router, "render_sealed", lambda url, **kw: _canned("busy"))
    got = client.post(f"/api/ni/items/{item_id}/run")
    assert got.status_code == 409
    assert got.json() == {"detail": "the local page browser is busy — try again in a moment",
                          "code": "render_busy"}  # top-level code beside detail (auth.py's no_session shape)
    item = store.get_item(item_id)
    assert item["consecutive_failures"] == 0 and store.list_runs(item_id) == []


def test_a_manual_run_on_an_unavailable_engine_keeps_its_shape_and_gains_the_code(client) -> None:
    store = client.app.state.ni
    item_id = _sealed(store, ["example.org"], state="live")  # the suite's opt-out: disabled
    got = client.post(f"/api/ni/items/{item_id}/run")
    assert got.status_code == 200
    body = got.json()
    assert isinstance(body.pop("duration_ms"), int)  # the 200 reply keeps its shape; code at its top level
    assert body == {"status": "error", "kind": "render_unavailable", "code": "render_unavailable"}
    item = store.get_item(item_id)
    assert item["last_status"] == "render_unavailable" and item["consecutive_failures"] == 0


@pytest.mark.parametrize(("render", "run", "code"), [("busy", "busy", "render_busy"),
                                                       ("disabled", "error", "render_unavailable")])
def test_the_c2_yes_carries_the_runs_capacity_code(client, monkeypatch, render, run, code) -> None:
    store = client.app.state.ni
    item_id = _sealed(store, ["example.org"], state="commissioning",
                      _built_from={"path": "page", "host": "tides.example.org", "title": "Creek"})
    if render == "busy":
        monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
        monkeypatch.setattr(router, "render_sealed", lambda url, **kw: _canned("busy"))
    got = client.post(f"/api/ni/items/{item_id}/validate", json={"ok": True})
    assert got.status_code == 200
    assert got.json() == {"ok": True, "state": "commissioning", "run": run, "code": code}
    assert store.get_item(item_id)["spec"]["_c2_ok"] is True  # the YES stands; the C3 proof is due


def test_a_legacy_c2_yes_response_carries_no_code(client) -> None:
    from test_ni import _fetching_preview, _fetching_scene_spec
    store = client.app.state.ni
    item_id = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(item_id, "commissioning")
    got = client.post(f"/api/ni/items/{item_id}/validate", json={"ok": True})
    assert got.status_code == 200 and "code" not in got.json()


def test_status_reports_the_page_browser_in_the_flow(client) -> None:
    store = client.app.state.ni
    _sealed(store, ["example.org", "tidecdn.net"], state="draft")
    _sealed(store, ["example.org"], state="live")
    body = client.get("/api/status/overview").json()
    assert body["browsers_flow"] == {"rendered_cards": 2, "drafts": 1, "worker": "idle"}
    with nimod._RENDER_WORKER_LOCK:
        assert status_routes._browsers_flow_status(SimpleNamespace(ni=store))["worker"] == "busy"


# ---- the render worker and the tick ------------------------------------------------------------

def _app(conn: duckdb.DuckDBPyConnection, key: bytes) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(master_key=key, db=conn, kb=None))


def _canned(status: str, html: bytes | None = None) -> router.BrowseResult:
    return router.BrowseResult(ok=status == "ok", tier="obscura", status=status,
                               reason="" if status == "ok" else "canned", signals=(), html=html,
                               graph=None, final_url=URL, engine="obscura", version="0.2.4",
                               identity={"mode": "mimic", "profile": 5, "user_agent": None, "consistent": True},
                               timings={"total_ms": 7},
                               census=[{"site": "example.org", "tunnels": 3, "up": 1, "down": 9, "verdict": "ok"}])


def _run_worker_once(store, conn, key, item_id: str, result: router.BrowseResult, monkeypatch) -> None:
    from test_ni import _FakeGateway
    sealed: list[dict] = []
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    monkeypatch.setattr(router, "render_sealed", lambda url, **kw: sealed.append({"url": url, **kw}) or result)
    nimod._render_run(store, store.get_item(item_id), _FakeGateway(), SecretStore(conn, key),
                      ScheduleStore(conn, key), None, None)
    return sealed


def test_busy_is_never_a_failure(monkeypatch) -> None:
    store, conn, key = _store()
    item_id = _sealed(store)
    _run_worker_once(store, conn, key, item_id, _canned("busy"), monkeypatch)
    item = store.get_item(item_id)
    assert (item["consecutive_failures"], item["last_checked"], item["state"]) == (0, None, "live")
    assert store.list_runs(item_id) == [] and store.read_snapshot(item_id, "latest") is None
    assert store.read_snapshot(item_id, "last_failure") is None and item_id in [i["id"] for i in store.due_items()]


def test_an_unavailable_engine_marks_the_card_and_starts_no_streak(monkeypatch) -> None:
    store, conn, key = _store()
    item_id = _sealed(store)
    from test_ni import _FakeGateway
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": {**ELIGIBLE, "eligible": False,
                                                                         "reason": "sandbox"})
    nimod._render_run(store, store.get_item(item_id), _FakeGateway(), SecretStore(conn, key),
                      ScheduleStore(conn, key), None, None)
    item = store.get_item(item_id)
    assert item["last_status"] == "render_unavailable" and item["consecutive_failures"] == 0
    assert store.list_runs(item_id) == [] and item["state"] == "live"
    assert ni_routes._render_fields(item)["render"]["unavailable"] is True


def test_the_worker_renders_the_sealed_tier_and_records_closed_run_notes(monkeypatch) -> None:
    store, conn, key = _store()
    item_id = _sealed(store, ["example.org", "tidecdn.net"], state="draft")
    store.record_render_consent(item_id)
    store.record_validation(item_id, True)
    sealed = _run_worker_once(store, conn, key, item_id, _canned("ok", RENDERED_HTML), monkeypatch)
    assert sealed == [{"url": URL, "sites": ["example.org", "tidecdn.net"], "identity": {"card": item_id},
                       "timezone": nimod.user_timezone_name()}]
    runs = store.list_runs(item_id)
    assert runs and runs[0]["status"] == "ok"
    notes = store.read_snapshot(item_id, "render")["payload"]
    assert set(notes) == {"engine", "version", "identity", "status", "total_ms", "census"}
    assert notes["identity"] == {"mode": "mimic", "profile": 5, "consistent": True}
    assert notes["census"] == [{"site": "example.org", "tunnels": 3, "verdict": "ok"}]
    assert "example.org" not in str(store.get_item(item_id)["last_status"])
    assert "7:12 AM" in str(store.read_snapshot(item_id, "latest")["payload"])


def test_a_render_failure_is_a_failure_with_a_host_free_class(monkeypatch) -> None:
    store, conn, key = _store()
    item_id = _sealed(store)
    _run_worker_once(store, conn, key, item_id, _canned("blocked"), monkeypatch)
    item = store.get_item(item_id)
    assert item["consecutive_failures"] == 1 and item["last_status"] == "render_blocked"
    assert store.list_runs(item_id)[0]["error"] == "render_blocked"


def test_a_render_never_runs_on_the_tick_thread() -> None:
    store, _conn, _key = _store()
    item_id = _sealed(store)
    nimod._TICK_THREAD.active = True
    try:
        with pytest.raises(nimod.NIError) as exc:
            nimod._fetch_http_page_rendered(store.get_item(item_id)["spec"], item_id, store, full=False)
    finally:
        nimod._TICK_THREAD.active = False
    assert exc.value.kind == "render_busy"


def test_the_worker_is_single_flight(monkeypatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(nimod, "_run_render_worker", lambda app, item_id, breaker=None: ran.append(item_id))
    with nimod._RENDER_WORKER_LOCK:
        assert nimod.spawn_render_worker(object(), "a") is False
    assert nimod.spawn_render_worker(object(), "b") is True
    for _ in range(200):  # bounded wait for the daemon thread
        if ran and not nimod._RENDER_WORKER_LOCK.locked():
            break
        time.sleep(0.01)
    assert ran == ["b"] and not nimod._RENDER_WORKER_LOCK.locked()


def test_rendered_cards_keep_their_own_due_window_and_the_tick_hands_off_one(monkeypatch) -> None:
    from test_ni import _fetching_preview, _fetching_scene_spec
    store, conn, key = _store()
    rendered = [_sealed(store) for _ in range(60)]
    static = [store.add_item(_fetching_scene_spec(), _fetching_preview()) for _ in range(60)]
    for item_id in static:
        store.set_state(item_id, "live")
    due = store.due_items()
    assert [i["id"] for i in due[:3]] == rendered[:3] or {i["id"] for i in due[:3]} <= set(rendered)
    assert len([i for i in due if i["id"] in set(static)]) == 50  # never starved by a render backlog
    handed: list[str] = []
    ran: list[str] = []
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    monkeypatch.setattr(nimod, "spawn_render_worker", lambda app, item_id, **kw: handed.append(item_id) or True)
    monkeypatch.setattr(nimod, "run_item", lambda store, item_id, **kw: ran.append(item_id) or {})
    result = nimod.tick(_app(conn, key))
    assert len(handed) == 1 and handed[0] in set(rendered)
    assert len(ran) == 3 and set(ran) <= set(static) and result["checked"] == 3


def test_an_unavailable_engine_is_marked_on_the_tick_and_a_preparable_one_stays_due(monkeypatch) -> None:
    store, conn, key = _store()
    item_id = _sealed(store)
    prepared: list[str] = []
    monkeypatch.setattr(browsers, "prepare", lambda engine="obscura": prepared.append(engine) or True)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": {**ELIGIBLE, "eligible": False,
                                                                         "reason": "absent"})
    monkeypatch.setattr(nimod, "run_item", lambda *a, **kw: pytest.fail("the tick ran a rendered card"))
    nimod.tick(_app(conn, key))
    assert prepared == ["obscura"] and store.get_item(item_id)["last_checked"] is None
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": {**ELIGIBLE, "eligible": False,
                                                                         "reason": "disabled"})
    nimod.tick(_app(conn, key))
    item = store.get_item(item_id)
    assert item["last_status"] == "render_unavailable" and item["consecutive_failures"] == 0


def test_a_draft_or_a_card_waiting_for_its_yes_is_never_rendered() -> None:
    store, _conn, _key = _store()
    draft = _sealed(store, ["example.org", "tidecdn.net"], state="draft")
    held = _sealed(store, state="commissioning",
                   _built_from={"path": "page", "host": "tides.example.org", "title": "Creek"})
    due = {i["id"] for i in store.due_items()}
    assert draft not in due and held not in due
    from test_ni import _FakeGateway
    assert not nimod._render_run_ok(store.get_item(draft), None, _FakeGateway())


def test_a_rendered_card_has_its_own_interval_floor() -> None:
    store, _conn, _key = _store()
    item_id = store.add_item(_spec(interval_minutes=1), {"text": "p"})
    assert store.get_item(item_id)["interval_minutes"] == 15


# ---- the startup sweep -------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix" or not shutil.which("sleep"), reason="POSIX processes")
def test_the_startup_sweep_ends_a_stray_engine_by_its_executable_path(tmp_path) -> None:
    engine_dir = tmp_path / "browsers" / "obscura" / "9.9.9"
    engine_dir.mkdir(parents=True)
    stray_exe = engine_dir / "obscura"
    shutil.copy2(os.path.realpath(shutil.which("sleep")), stray_exe)
    stray = subprocess.Popen([str(stray_exe), "30"])
    bystander = subprocess.Popen([shutil.which("sleep"), "30"])
    try:
        for _ in range(100):  # bounded: the exec lands
            if stray.pid in proc.stray_engines(str(tmp_path / "browsers")):
                break
            time.sleep(0.02)
        assert proc.sweep_strays(str(tmp_path / "browsers")) == 1
        assert stray.wait(timeout=5) == -9
        assert bystander.poll() is None
        assert proc.sweep_strays(str(tmp_path / "missing")) == 0
    finally:
        for child in (stray, bystander):
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


# ---- the launcher's renderer signal: {wanted, sidecar} -------------------------------------------

@pytest.fixture()
def fresh_wanted() -> Iterator[None]:
    browsers._WANTED.clear()  # process-wide and sticky by design: each test starts from a boot
    yield
    browsers._WANTED.clear()


def _ready(build: str | None, *, ok: bool = True) -> dict:
    facts = {"v": 1, "boot": "b3aboot", "at": "2026-10-12T00:00:00+00:00", "uid": 65532, "gid": 10001,
             "interfaces": ["lo"], "netns": "net:[4026531840]", "readonly_root": True,
             "canaries": [{"target": t, "outcome": "Network is unreachable"} for t in ("a", "b", "c")],
             "engine": {"name": "obscura", "version": "0.2.4", "platform": "linux-aarch64",
                        "sha256": "0" * 64, "verified": True}, "ok": ok}
    if build is not None:
        facts["build"] = build
    return facts


def _volume(tmp_path, monkeypatch, ready: dict | None, *, beat_age_s: float = 0.0):
    from smartbrain_3000.browsers import walls
    from smartbrain_3000.browsers.walls import docker as dw
    volume = tmp_path / "render"
    volume.mkdir()
    monkeypatch.setattr(dw, "RENDER_ROOT", volume)
    monkeypatch.setattr(walls, "host_platform", lambda: "docker")
    if ready is not None:
        dw.write_json(volume, dw.READY_NAME, ready)
        dw.write_json(volume, dw.HEARTBEAT_NAME, {"v": 1, "boot": "b3aboot", "ts": time.time() - beat_age_s})
    return volume


def test_the_renderer_signal_is_readable_while_locked_and_n_a_outside_docker(tmp_path, monkeypatch,
                                                                             fresh_wanted) -> None:
    from smartbrain_3000.browsers import walls
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "locked.duckdb"))
    monkeypatch.setattr(walls, "host_platform", lambda: "darwin")
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as locked:  # never set up: the vault is locked
        body = locked.get("/api/status/overview").json()
    assert body["unlocked"] is False
    assert body["renderer"] == {"wanted": False, "sidecar": "n/a"}


@pytest.mark.parametrize(("ready", "beat_age_s", "state"), [
    (None, 0.0, "missing"),                          # nothing answers on the volume
    ("this", 0.0, "ready"),                          # READY names this app build
    ("0.0.1-other", 0.0, "stale"),                   # another build: rebuild / pull, not start
    ("legacy", 0.0, "stale"),                        # a B2 READY without the build field
    ("this", 3600.0, "missing"),                     # the agent stopped (no fresh heartbeat)
    ("not-ok", 0.0, "missing"),                      # its own canary failed
])
def test_the_sidecar_state_follows_the_ready_facts(tmp_path, monkeypatch, ready, beat_age_s, state) -> None:
    from smartbrain_3000 import __version__
    facts = {None: None, "this": _ready(__version__), "0.0.1-other": _ready("0.0.1-other"),
             "legacy": _ready(None), "not-ok": _ready(__version__, ok=False)}[ready]
    _volume(tmp_path, monkeypatch, facts, beat_age_s=beat_age_s)
    assert browsers.renderer_status()["sidecar"] == state


def test_the_agent_names_its_build_and_a_legacy_ready_still_proves_the_wall(tmp_path, monkeypatch) -> None:
    from smartbrain_3000 import __version__
    from smartbrain_3000.browsers import walls
    from smartbrain_3000.browsers.walls import docker as dw
    assert "build" in dw.READY_KEYS and dw.LEGACY_READY_KEYS == dw.READY_KEYS - {"build"}
    _volume(tmp_path, monkeypatch, _ready(None))
    assert walls.check("docker", None) is None  # eligibility is unchanged by the build field
    assert dw.valid_ready(_ready(__version__)) and not dw.valid_ready(_ready(""))
    assert not dw.valid_ready(_ready("x" * 65))


def test_wanted_turns_on_when_a_rendered_card_is_due_even_without_an_engine(monkeypatch, fresh_wanted) -> None:
    store, conn, key = _store()
    _sealed(store)
    monkeypatch.setattr(nimod, "run_item", lambda *a, **kw: pytest.fail("the tick ran a rendered card"))
    assert browsers.renderer_status()["wanted"] is False
    nimod.tick(_app(conn, key))  # the suite's opt-out: the engine is disabled
    assert browsers.renderer_status()["wanted"] is True
    browsers._WANTED.clear()
    nimod.tick(_app(conn, key))  # marked render_unavailable on the first pass: not due again
    assert browsers.renderer_status()["wanted"] is False


def test_wanted_stays_off_for_cards_that_never_render(monkeypatch, fresh_wanted) -> None:
    from test_ni import _fetching_preview, _fetching_scene_spec
    store, conn, key = _store()
    store.set_state(store.add_item(_fetching_scene_spec(), _fetching_preview()), "live")
    monkeypatch.setattr(nimod, "run_item", lambda *a, **kw: {})
    nimod.tick(_app(conn, key))
    assert browsers.renderer_status()["wanted"] is False
