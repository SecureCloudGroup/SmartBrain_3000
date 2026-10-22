"""S8 brief cards: headlines extracted by code from any feed / list / JSON items source, plus the opt-in summary
written by the owner's LOCAL model. Class tests on synthetic sources (no named site)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_ni import _FakeGateway, _store
from test_ni_routes import _create_via_tool, _unlock

from smartbrain_3000 import formats, ni_brief, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.ni_research import tools as rtools
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import SecretStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app
    with TestClient(create_app()) as test_client:
        yield test_client


NOW = datetime(2026, 10, 22, 10, tzinfo=UTC)
URL = "https://feeds.example.org/headlines"

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Alpha story</title><link>https://a.example/1</link><pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Beta story</title><link>https://b.example/2</link><pubDate>Tue, 20 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Gamma story</title><link>https://c.example/3</link><pubDate></pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>F</title>
<entry><title>One</title><link href="https://a.example/one"/><updated>2026-10-20T10:00:00Z</updated></entry>
<entry><title>Two</title><link href="https://a.example/two"/><updated>2026-10-19T10:00:00Z</updated></entry>
</feed>"""


def _items(n: int, *, title="title", link="url", time="published") -> list[dict]:
    return [{title: f"Headline {i}", link: f"https://h{i}.example/p/{i}",
             time: f"2026-10-{20 - i:02d}T08:00:00Z"} for i in range(n)]


def _build(parsed, kind="latest_items"):
    return ni_brief.build(parsed, title="News", ask="latest news", now=NOW, source_url=URL, cadence_s=3600,
                          viewer_tz="UTC", question_kind=kind, wants=[])


def _spec(built: dict, **over) -> dict:
    spec = {"version": 1, "title": "News", "goal": "latest news", "params": {},
            "source": {"type": "http_json", "url": URL, "format": "json"}, "pipeline": built["pipeline"],
            "scene": built["scene"], "display": {"size": "small"}, "contract": None, "model": None,
            "interval_minutes": 60, "brief": built["brief"]}
    spec.update(over)
    return spec


def _texts(bound: dict) -> list[str]:
    prims = bound["clir"]["desktop"]["prims"]
    out: list[str] = []
    for p in prims:  # bounded: one card
        if p["k"] == "text":
            out.extend(str(x) for x in (p.get("lines") or [p.get("text", "")]))
    return out


def _bind(spec: dict, payload: dict) -> dict:
    return nimod.bind_scene(spec["scene"], payload, form_ctx=nimod._form_bind_context(spec, NOW))


# ---- layout: any items source, no model -------------------------------------------------------------------

@pytest.mark.parametrize("parsed", [
    formats.parse_feed(RSS),
    formats.parse_feed(ATOM),
    {"items": _items(4)},
    {"data": {"stories": _items(4, title="headline", link="link", time="date")}},
    {"response": {"results": _items(6, title="webTitle", link="webUrl", time="webPublicationDate")}},
    _items(4),
], ids=["rss", "atom", "json-items", "nested", "camel-nested", "bare-list"])
def test_layout_and_build_from_any_items_source(parsed) -> None:
    built = _build(parsed)
    assert built is not None and built["brief"] == {"summary": False}
    assert 1 <= len(built["preview_payload"]["rows"]) <= ni_brief.MAX_HEADLINES
    assert not any(s.get("op") == "llm" for s in built["pipeline"]), "extractive by default: no model stage"
    spec = _spec(built)
    nimod.validate_spec(spec)
    assert any("tory" in t or "Headline" in t or "One" in t for t in _texts(_bind(spec, built["preview_payload"])))


def test_headlines_show_title_link_time_and_host() -> None:
    built = _build(formats.parse_feed(RSS))
    spec = _spec(built)
    bound = _bind(spec, built["preview_payload"])
    joined = " | ".join(_texts(bound))
    assert "Alpha story" in joined and "Beta story" in joined
    assert "a.example" in joined, "the source host rides the meta line"
    hrefs = json.dumps(bound["clir"]["desktop"]["hitmap"])
    assert "https://a.example/1" in hrefs and "https://b.example/2" in hrefs


def test_newest_first_and_bounded() -> None:
    rows = _build({"items": _items(30)})["preview_payload"]["rows"]
    assert len(rows) == ni_brief.MAX_HEADLINES
    assert rows[0]["title"] == "Headline 0"


def test_oldest_first_source_is_reversed() -> None:
    asc = list(reversed(_items(5)))
    rows = _build({"items": asc})["preview_payload"]["rows"]
    assert rows[0]["title"] == "Headline 0" and rows[-1]["title"] == "Headline 4"


def test_missing_time_and_title_rows() -> None:
    items = _items(4)
    items[1]["published"] = ""
    items[2]["title"] = ""
    rows = _build({"items": items})["preview_payload"]["rows"]
    assert [r["title"] for r in rows] == ["Headline 0", "Headline 1", "Headline 3"]


@pytest.mark.parametrize("parsed", [
    {"temperature": 12, "unit": "c"},
    {"items": [{"a": 1}, {"a": 2}]},
    {"items": [{"title": "", "url": ""}] * 3},
    "plain text",
    [],
    None,
], ids=["scalar", "no-title", "empty-titles", "text", "empty", "none"])
def test_not_a_headline_list_builds_nothing(parsed) -> None:
    assert ni_brief.layout_of(parsed) is None
    assert _build(parsed) is None


def test_only_brief_questions() -> None:
    assert ni_brief.is_brief_question("latest_items") and ni_brief.is_brief_question("text_brief")
    assert not ni_brief.is_brief_question("current_value") and not ni_brief.is_brief_question(None)


# ---- validation ------------------------------------------------------------------------------------------

def _on_spec(model="ollama/x") -> dict:
    built = _build({"items": _items(4)})
    return ni_brief.with_summary(_spec(built), model)


def test_brief_key_is_closed_shape() -> None:
    spec = _on_spec()
    nimod.validate_spec(copy.deepcopy(spec))
    for bad in ({"summary": True, "extra": 1}, {"summary": "yes"}, {}, "on"):
        broken = copy.deepcopy(spec)
        broken["brief"] = bad
        with pytest.raises(ValueError, match="brief"):
            nimod.validate_spec(broken)


def test_summary_requirements() -> None:
    spec = _on_spec()
    no_stage = copy.deepcopy(spec)
    no_stage["pipeline"] = [s for s in no_stage["pipeline"] if s["op"] != "llm"]
    no_model = copy.deepcopy(spec)
    no_model["model"] = None
    unbounded = copy.deepcopy(spec)
    unbounded["pipeline"][-1]["max_chars"] = 5000
    loud = copy.deepcopy(spec)
    loud["pipeline"][-1]["output"] = {"headline": "string"}
    off_with_stage = copy.deepcopy(spec)
    off_with_stage["brief"] = {"summary": False}
    for bad in (no_stage, no_model, unbounded, loud, off_with_stage):
        with pytest.raises(ValueError):
            nimod.validate_spec(bad)


def test_researcher_cannot_draft_an_llm_stage_or_a_summary() -> None:
    spec = _on_spec()
    with pytest.raises(ValueError):
        nimod.validate_spec(copy.deepcopy(spec), researcher=True)
    off = ni_brief.without_summary(spec)
    llm_added = copy.deepcopy(off)
    llm_added["pipeline"].append(ni_brief.summary_stage())
    with pytest.raises(ValueError):
        nimod.validate_spec(llm_added, researcher=True)


def test_llm_stage_grammar_for_optional_and_max_chars() -> None:
    base = ni_brief.summary_stage()
    for key, bad in (("optional", False), ("max_chars", 0), ("max_chars", 5000), ("max_chars", True)):
        stage = {**base, key: bad}
        spec = _on_spec()
        spec["pipeline"][-1] = stage
        with pytest.raises(ValueError):
            nimod.validate_spec(spec)


def test_without_summary_round_trips() -> None:
    off = ni_brief.without_summary(_on_spec())
    assert off["brief"] == {"summary": False} and off["model"] is None
    assert not any(s.get("op") == "llm" for s in off["pipeline"])
    assert ni_brief.summary_on(_on_spec()) and not ni_brief.summary_on(off)


# ---- refresh: local only, headlines always render, summary labelled ---------------------------------------

def _run_once(model: str, reply: str = '{"brief_summary": "Three stories today."}', monkeypatch=None,
              payload=None) -> tuple:
    store, conn, key = _store()
    secrets, schedules = SecretStore(conn, key), ScheduleStore(conn, key)
    built = _build({"items": _items(4)})
    spec = ni_brief.with_summary(_spec(built), model)
    iid = store.add_item(spec, built["preview_payload"])
    store.set_state(iid, "live")
    gw = _FakeGateway(text=reply, model=model)
    monkeypatch.setattr(nimod, "_fetch_http_json", lambda *a, **k: copy.deepcopy(payload or {"items": _items(4)}))
    result = nimod.run_item(store, iid, gateway_mod=gw, secrets_store=secrets, schedules_store=schedules)
    return store, iid, gw, result


def test_refresh_with_summary_on_uses_the_local_model_and_labels_it(monkeypatch) -> None:
    store, iid, gw, result = _run_once("ollama/x", monkeypatch=monkeypatch)
    assert result["status"] == "ok" and gw.calls == 1
    snap = store.read_snapshot(iid, "latest")
    assert snap["ok"] is True and "Headline 0" in json.dumps(snap["payload"]), "headlines render with the summary on"
    field = ni_brief.board_field(store.get_item(iid)["spec"], store.read_snapshot(iid, "outputs"), local=True)
    assert field["state"] == "ready" and field["text"] == "Three stories today."
    assert field["label"] == "Summary written by your local model"


def test_refresh_never_calls_a_non_local_model(monkeypatch) -> None:
    store, iid, gw, result = _run_once("openai/gpt-4o", monkeypatch=monkeypatch)
    assert gw.calls == 0, "no cloud call, ever"
    assert result["status"] == "ok", "the headlines still refresh"
    snap = store.read_snapshot(iid, "latest")
    assert snap["ok"] is True and "Headline 0" in json.dumps(snap["payload"])
    field = ni_brief.board_field(store.get_item(iid)["spec"], store.read_snapshot(iid, "outputs"), local=False)
    assert field["state"] in {"unavailable", "pending"} and field["text"] == ""


def test_a_failing_local_model_leaves_headlines(monkeypatch) -> None:
    store, iid, gw, result = _run_once("ollama/x", reply="not json at all", monkeypatch=monkeypatch)
    assert result["status"] == "ok"
    assert "Headline 0" in json.dumps(store.read_snapshot(iid, "latest")["payload"])


def test_summary_is_clipped_to_its_bound(monkeypatch) -> None:
    long = json.dumps({"brief_summary": "x" * 3000})
    store, iid, _gw, _r = _run_once("ollama/x", reply=long, monkeypatch=monkeypatch)
    field = ni_brief.board_field(store.get_item(iid)["spec"], store.read_snapshot(iid, "outputs"), local=True)
    assert len(field["text"]) <= ni_brief.SUMMARY_MAX_CHARS


def test_off_card_has_no_text_and_no_model_call(monkeypatch) -> None:
    store, conn, key = _store()
    secrets, schedules = SecretStore(conn, key), ScheduleStore(conn, key)
    built = _build({"items": _items(4)})
    iid = store.add_item(_spec(built), built["preview_payload"])
    store.set_state(iid, "live")
    gw = _FakeGateway(model="ollama/x")
    monkeypatch.setattr(nimod, "_fetch_http_json", lambda *a, **k: {"items": _items(4)})
    nimod.run_item(store, iid, gateway_mod=gw, secrets_store=secrets, schedules_store=schedules)
    assert gw.calls == 0
    field = ni_brief.board_field(store.get_item(iid)["spec"], None, local=True)
    assert field["state"] == "off" and field["text"] == ""


# ---- the toggle route -------------------------------------------------------------------------------------

def _brief_item(client, state="live", *, kept_preview: bool = False) -> str:
    built = _build({"items": _items(4)})
    # a card kept through the flow stores its preview with the instants as text (ni_flow does the same)
    preview = nimod.json_instants(built["preview_payload"]) if kept_preview else built["preview_payload"]
    iid = client.app.state.ni.add_item(_spec(built), preview)
    client.app.state.ni.set_state(iid, state)
    return iid


def _row(client, iid):
    return next(i for i in client.get("/api/ni/board").json()["items"] if i["id"] == iid)


def test_toggle_refused_when_no_local_model(client, monkeypatch) -> None:
    _unlock(client)
    iid = _brief_item(client)
    monkeypatch.setattr(ni_flow, "_local_flow_model", lambda store: None)
    got = client.post(f"/api/ni/items/{iid}/brief-summary", json={"on": True})
    assert got.status_code == 409 and "local model" in got.json()["detail"]
    row = _row(client, iid)
    assert row["brief"]["summary"] is False and row["brief"]["local"] is False
    spec = client.app.state.ni.get_item(iid)["spec"]
    assert not any(s.get("op") == "llm" for s in spec["pipeline"]) and spec["model"] is None


def test_toggle_on_then_off(client, monkeypatch) -> None:
    _unlock(client)
    iid = _brief_item(client)
    monkeypatch.setattr(ni_flow, "_local_flow_model", lambda store: "ollama/x")
    got = client.post(f"/api/ni/items/{iid}/brief-summary", json={"on": True})
    assert got.status_code == 200 and got.json()["brief"]["summary"] is True
    row = _row(client, iid)
    assert row["brief"]["state"] == "pending" and row["brief"]["label"] == ni_brief.SUMMARY_LABEL
    assert client.app.state.ni.get_item(iid)["spec"]["model"] == ni_brief.LOCAL_MODEL
    off = client.post(f"/api/ni/items/{iid}/brief-summary", json={"on": False})
    assert off.status_code == 200 and off.json()["brief"]["state"] == "off"
    assert client.app.state.ni.get_item(iid)["spec"]["model"] is None


def test_toggle_refusals_for_non_brief_and_unkept(client, monkeypatch) -> None:
    _unlock(client)
    monkeypatch.setattr(ni_flow, "_local_flow_model", lambda store: "ollama/x")
    plain = _create_via_tool(client)
    assert client.post(f"/api/ni/items/{plain}/brief-summary", json={"on": True}).status_code == 409
    assert "brief" not in _row(client, plain)
    draft = _brief_item(client, state="draft")
    assert client.post(f"/api/ni/items/{draft}/brief-summary", json={"on": True}).status_code == 409
    assert client.post("/api/ni/items/missing/brief-summary", json={"on": True}).status_code == 404


def test_export_strips_the_summary(client, monkeypatch) -> None:
    _unlock(client)
    iid = _brief_item(client, kept_preview=True)
    monkeypatch.setattr(ni_flow, "_local_flow_model", lambda store: "ollama/x")
    assert client.post(f"/api/ni/items/{iid}/brief-summary", json={"on": True}).status_code == 200
    assert ni_brief.summary_on(client.app.state.ni.get_item(iid)["spec"]), "the summary is on before the export"
    got = client.get(f"/api/ni/items/{iid}/export-template")
    assert got.status_code == 200, got.text
    template = got.json()["spec_template"]
    assert "brief" not in template
    assert not any(s.get("op") == "llm" for s in template["pipeline"])
    assert template["model"] is None
    assert ni_brief.summary_on(client.app.state.ni.get_item(iid)["spec"]), "the export leaves the kept card alone"


# ---- researcher and flow integration ---------------------------------------------------------------------

def test_sample_payload_wraps_a_bare_list() -> None:
    assert rtools.sample_payload([{"title": "a"}]) == {"items": [{"title": "a"}]}
    assert rtools.sample_payload({"x": 1}) == {"x": 1}


# ---- review fixes: the monitor hash follows the revision, the local model is resolved at run ----------------

class _RoutedGateway(_FakeGateway):
    """A fake gateway with its own route table; records the model each chat asked for."""

    def __init__(self, routes: dict, text: str = '{"brief_summary": "Stories."}') -> None:
        super().__init__(text=text)
        self._routes = routes
        self.models: list[str] = []
        self.kwargs: list[dict] = []

    def load_routes(self, _conn) -> dict:
        return dict(self._routes)

    def chat(self, messages, model, **kwargs) -> dict:
        self.models.append(model)
        self.kwargs.append(kwargs)
        return super().chat(messages, model, **kwargs)


def _live_brief(gw, *, summary: bool = True, model: str = ni_brief.LOCAL_MODEL, monkeypatch=None):
    store, conn, key = _store()
    built = _build({"items": _items(4)})
    spec = ni_brief.with_summary(_spec(built), model) if summary else _spec(built)
    iid = store.add_item(spec, built["preview_payload"])
    store.set_state(iid, "live")
    monkeypatch.setattr(nimod, "_fetch_http_json", lambda *a, **k: copy.deepcopy({"items": _items(4)}))
    refresh = lambda: nimod.run_item(store, iid, gateway_mod=gw, secrets_store=SecretStore(conn, key),
                                     schedules_store=ScheduleStore(conn, key))
    return store, iid, refresh


def test_monitor_hash_is_bound_to_the_spec_revision(monkeypatch) -> None:
    gw = _RoutedGateway({"ni": "ollama/x"})
    store, iid, refresh = _live_brief(gw, monkeypatch=monkeypatch)
    assert refresh()["status"] == "ok" and gw.calls == 1
    assert refresh()["status"] == "unchanged" and gw.calls == 1, "same payload at the same revision skips the model"
    store.update_spec(iid, ni_brief.without_summary(store.get_item(iid)["spec"]), origin="user")
    assert refresh()["status"] == "ok" and gw.calls == 1, "off: no model"
    store.update_spec(iid, ni_brief.with_summary(store.get_item(iid)["spec"]), origin="user")
    assert refresh()["status"] == "ok" and gw.calls == 2, "on again over the same bytes runs the model"
    assert refresh()["status"] == "unchanged" and gw.calls == 2
    state = store.read_snapshot(iid, "llm_state")["payload"]
    assert state["spec_rev"] == store.get_item(iid)["spec_rev"]


def test_a_revert_or_repair_revision_runs_the_model_too(monkeypatch) -> None:
    gw = _RoutedGateway({"ni": "ollama/x"})
    store, iid, refresh = _live_brief(gw, monkeypatch=monkeypatch)
    refresh()
    rev = store.get_item(iid)["spec_rev"]
    store.apply_repair(iid, copy.deepcopy(store.get_item(iid)["spec"]), origin="repair_l1", expected_rev=rev)
    assert store.get_item(iid)["spec_rev"] == rev + 1
    refresh()
    assert gw.calls == 2, "any revision writer, not only update_spec, re-arms the model"


def test_an_llm_state_row_without_a_revision_runs_once_then_records_it(monkeypatch) -> None:
    gw = _RoutedGateway({"ni": "ollama/x"})
    store, iid, refresh = _live_brief(gw, monkeypatch=monkeypatch)
    refresh()
    old = store.read_snapshot(iid, "llm_state")["payload"]
    store.write_snapshot(iid, "llm_state", {k: v for k, v in old.items() if k != "spec_rev"}, ok=True)
    assert refresh()["status"] == "ok" and gw.calls == 2
    assert store.read_snapshot(iid, "llm_state")["payload"]["spec_rev"] == store.get_item(iid)["spec_rev"]
    assert refresh()["status"] == "unchanged" and gw.calls == 2


def test_the_local_model_is_resolved_when_the_card_runs(monkeypatch) -> None:
    gw = _RoutedGateway({"ni": "ollama/a"})
    _store_, _iid, refresh = _live_brief(gw, monkeypatch=monkeypatch)
    refresh()
    gw._routes = {"ni": "openai/gpt-4o", "chat": "mlx/b"}   # the user switched local servers
    _store_.update_spec(_iid, _store_.get_item(_iid)["spec"], origin="user")
    refresh()
    assert gw.models == ["ollama/a", "mlx/b"], "the sentinel follows the routes, never a stale id"
    assert _store_.get_item(_iid)["spec"]["model"] == ni_brief.LOCAL_MODEL


def test_no_cloud_model_is_ever_chosen_for_the_summary(monkeypatch) -> None:
    gw = _RoutedGateway({"ni": "openai/gpt-4o", "chat": "anthropic/claude", "agent": "google/gemini"})
    store, iid, refresh = _live_brief(gw, monkeypatch=monkeypatch)
    assert refresh()["status"] == "ok"
    assert gw.calls == 0 and gw.models == []
    assert "Headline 0" in json.dumps(store.read_snapshot(iid, "latest")["payload"])
    with pytest.raises(nimod.NIError) as err:
        nimod._make_llm_call(store, store.get_item(iid)["spec"], gw)
    assert err.value.kind == "llm_unrouted"
    # a concrete cloud pin is still refused by the same gate
    with pytest.raises(nimod.NIError) as err2:
        nimod._make_llm_call(store, {**store.get_item(iid)["spec"], "model": "openai/gpt-4o"}, gw)
    assert err2.value.kind == "llm_requires_local"


def test_a_busy_local_model_leaves_headlines_and_does_not_wait(monkeypatch) -> None:
    class _Busy(_RoutedGateway):
        class LocalBusy(Exception):
            pass

        def chat(self, messages, model, **kwargs):
            self.kwargs.append(kwargs)
            self.calls += 1
            raise self.LocalBusy("busy")

    gw = _Busy({"ni": "ollama/x"})
    store, iid, refresh = _live_brief(gw, monkeypatch=monkeypatch)
    assert refresh()["status"] == "ok"
    assert gw.kwargs and gw.kwargs[0].get("acquire_timeout") == nimod._OPTIONAL_LLM_ACQUIRE_S
    assert gw.calls == 1, "busy is a soft failure: no retry"
    assert "Headline 0" in json.dumps(store.read_snapshot(iid, "latest")["payload"])
    field = ni_brief.board_field(store.get_item(iid)["spec"], store.read_snapshot(iid, "outputs"), local=True)
    assert field["text"] == "" and field["state"] == "unavailable"


def test_gateway_chat_gives_up_on_a_held_slot() -> None:
    from smartbrain_3000 import gateway
    with gateway.serialized_local(), pytest.raises(gateway.LocalBusy):
        gateway.chat([{"role": "user", "content": "hi"}], "ollama/x", acquire_timeout=0.05)


# ---- grammar: the time transform's lenient flag ------------------------------------------------------------

def _time_stage(spec: dict) -> dict:
    return next(a for s in spec["pipeline"] if s.get("op") == "transform" for a in s["apply"] if a["fn"] == "time")


def test_time_lenient_grammar() -> None:
    spec = ni_brief.without_summary(_on_spec())
    nimod.validate_spec(copy.deepcopy(spec))
    assert _time_stage(spec)["lenient"] is True
    no_key = copy.deepcopy(spec)
    del _time_stage(no_key)["key"]
    with_unless = copy.deepcopy(spec)
    _time_stage(with_unless)["unless"] = {"key": "published", "op": "eq", "value": ""}
    for bad in (no_key, with_unless):
        with pytest.raises(ValueError, match="lenient"):
            nimod.validate_spec(bad)
    for bad_value in ("yes", 1, None, [True]):
        broken = copy.deepcopy(spec)
        _time_stage(broken)["lenient"] = bad_value
        with pytest.raises(ValueError):
            nimod.validate_spec(broken)


# ---- a re-research proposal applied to a brief with the summary on --------------------------------------

def _proposal_card(client, *, replacement_is_brief: bool):
    store = client.app.state.ni
    built = _build({"items": _items(4)})
    iid = store.add_item(ni_brief.with_summary(_spec(built)), built["preview_payload"])
    new = _spec(_build({"items": _items(5)}))
    new["source"] = {"type": "http_json", "url": "https://other.example.org/headlines", "format": "json"}
    if not replacement_is_brief:
        new.pop("brief")
    store.conn.execute("UPDATE ni_items SET state='degraded', consecutive_failures = 1, first_failure_at = now() "
                       "WHERE id = ?;", [iid])
    proposal = {"spec": new, "origin": "research", "reason": "source_dead",
                "created_at": datetime.now(UTC).isoformat(), "model": "research"}
    assert store.set_l2_proposal(iid, proposal, expected_rev=store.get_item(iid)["spec_rev"],
                                 states=("degraded", "failing"))
    return store, iid


def test_applied_reresearch_carries_the_summary_to_a_brief(client) -> None:
    _unlock(client)
    store, iid = _proposal_card(client, replacement_is_brief=True)
    assert client.post(f"/api/ni/items/{iid}/l2-proposal/apply").status_code == 200
    spec = store.get_item(iid)["spec"]
    assert spec["source"]["url"].startswith("https://other.example.org")
    assert spec["brief"] == {"summary": True} and ni_brief.summary_on(spec)
    assert sum(1 for s in spec["pipeline"] if s.get("op") == "llm") == 1
    assert spec["model"] == ni_brief.LOCAL_MODEL


def test_applied_reresearch_does_not_carry_a_summary_to_a_non_brief(client) -> None:
    _unlock(client)
    store, iid = _proposal_card(client, replacement_is_brief=False)
    assert client.post(f"/api/ni/items/{iid}/l2-proposal/apply").status_code == 200
    spec = store.get_item(iid)["spec"]
    assert "brief" not in spec and not any(s.get("op") == "llm" for s in spec["pipeline"])
    assert not spec.get("model")


def test_carry_summary_leaves_an_off_summary_off() -> None:
    off = ni_brief.without_summary(_on_spec())
    new = copy.deepcopy(off)
    assert ni_brief.carry_summary(off, new) == new
