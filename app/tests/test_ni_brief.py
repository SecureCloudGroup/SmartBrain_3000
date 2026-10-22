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

def _brief_item(client, state="live") -> str:
    built = _build({"items": _items(4)})
    iid = client.app.state.ni.add_item(_spec(built), built["preview_payload"])
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
    assert client.app.state.ni.get_item(iid)["spec"]["model"] == "ollama/x"
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
    iid = _brief_item(client)
    monkeypatch.setattr(ni_flow, "_local_flow_model", lambda store: "ollama/x")
    client.post(f"/api/ni/items/{iid}/brief-summary", json={"on": True})
    spec = client.app.state.ni.get_item(iid)["spec"]
    from smartbrain_3000 import ni_routes
    assert "brief" in ni_routes._EXPORT_STRIP_KEYS
    assert ni_brief.summary_on(spec)


# ---- researcher and flow integration ---------------------------------------------------------------------

def test_sample_payload_wraps_a_bare_list() -> None:
    assert rtools.sample_payload([{"title": "a"}]) == {"items": [{"title": "a"}]}
    assert rtools.sample_payload({"x": 1}) == {"x": 1}
