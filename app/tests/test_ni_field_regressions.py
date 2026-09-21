"""The 2026-09-17 field round as permanent regressions (G1 exit bar).

Seven natural asks produced five effective failures; the root pattern was
cards dying silently. The G1 bar these tests hold forever: **every failure
states its true reason on the card with a next step** — a question or a
reopen affordance, never a blind dead end. Model-free: records are driven
directly (lifecycle-test pattern); each test names its field ask verbatim.

Coverage notes for later waves (asserted here only to their G1 truth):
- "latest earthquakes above magnitude 5" threshold-over-recipe → G2 (JUDGE).
- "top stories on Hacker News" suggestion RELEVANCE → G3 (LOCATE); G1 asserts
  the pick question renders.
- "should be in Fahrenheit" note → drives a redraft in G4 (SUSTAIN.refine);
  G1 asserts the honest journal + draft return.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import ni_flow

_LOCAL = {"X-SB-Local": "1"}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "field.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        c.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield c


@pytest.fixture(autouse=True)
def _no_worker(monkeypatch):
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda s, iid, **kw: True)


def _board_flow(client: TestClient, iid: str) -> dict:
    row = next(x for x in client.get("/api/ni/board").json()["items"]
               if x["id"] == iid)
    assert row["flow"] is not None, "the card must carry its flow truth"
    return row["flow"]


def test_field_1_bitcoin_decline_is_a_fork_not_a_death(client) -> None:
    """Field: "what's bitcoin worth right now" → [Not this source] → the card
    went dead. Now: decline re-enters the pick pause with suggestions."""
    from smartbrain_3000 import ni_catalog
    iid = client.post("/api/ni/intake",
                      json={"request": "what's bitcoin worth right now, keep it updated"},
                      headers=_LOCAL).json()["id"]
    store = client.app.state.ni
    ni_flow._pause_for_recipe_confirm(store, iid, {},
                                       ni_catalog.get_recipe("crypto-price-btc-usd"))
    r = client.post(f"/api/ni/items/{iid}/flow/decline-source", headers=_LOCAL)
    assert r.status_code == 200 and r.json()["state"] == "source"
    flow = _board_flow(client, iid)
    assert flow["state"] == "source"
    assert "suggestions" in flow, "the pick pause renders its affordances"


def test_field_2_countdown_without_date_asks_instead_of_dying(client) -> None:
    """Field: "countdown of days until US mid-term election" → generic
    'Creation didn't finish'. Now: the honest reason + a date question, and
    the typed answer resumes the build."""
    iid = client.post("/api/ni/intake",
                      json={"request": "Show me a countdown of days until US mid-term election"},
                      headers=_LOCAL).json()["id"]
    store = client.app.state.ni
    ni_flow._terminate_unsupported(
        store, iid,
        "computed-only requires an explicit YYYY-MM-DD date in the request",
        question={"kind": "supply_date",
                   "prompt": "When is it? Add the date as YYYY-MM-DD."})
    flow = _board_flow(client, iid)
    assert "date" in flow["reason"].lower()
    assert flow["question"]["kind"] == "supply_date"
    r = client.post(f"/api/ni/items/{iid}/flow/answer",
                    json={"kind": "supply_date", "value": "2026-11-03"},
                    headers=_LOCAL)
    assert r.status_code == 200 and r.json()["started"] is True


def test_field_3_tides_fetch_failure_names_itself_with_two_roads(client) -> None:
    """Field: "tides for Limehouse Boat Landing SC" → pasted URL fetch failed
    and the card showed nothing. Now: the failed shell names the fetch class
    and offers Retry AND pick-a-source."""
    iid = client.post("/api/ni/intake",
                      json={"request": "show me the tides for Limehouse Boat Landing SC"},
                      headers=_LOCAL).json()["id"]
    ni_flow._fail(client.app.state.ni, iid, "fetch",
                  "sample fetch failed: JSONDecodeError")
    flow = _board_flow(client, iid)
    assert flow["state"] == "failed"
    assert "fetched" in flow["reason"] or "fetch" in flow["reason"], flow
    assert set(flow["reopen"]) == {"retry", "pick_source"}
    # The pick_source road actually works.
    assert client.post(f"/api/ni/items/{iid}/flow/reopen",
                       headers=_LOCAL).json()["state"] == "source"


def test_field_4_hn_resolves_from_words_to_the_vetted_recipe(client) -> None:
    """Field: "top stories on Hacker News" failed as a words-path ask (the
    catalog had no HN recipe; suggestions were weather-and-stocks noise).
    G3: the promoted catalog resolves it deterministically to the vetted HN
    recipe — the flow's source stage lands the STANDARD confirm pause."""
    from smartbrain_3000 import ni_catalog
    iid = client.post("/api/ni/intake",
                      json={"request": "top stories on Hacker News"},
                      headers=_LOCAL).json()["id"]
    store = client.app.state.ni
    intent = {"kind": "external_data", "subject": "Hacker News",
              "cadence_minutes": 15, "wants": ["stories"], "threshold": None,
              "display_hint": "list"}
    match = ni_flow.match_recipe(ni_catalog.entries(),
                                  "top stories on Hacker News", intent)
    assert match is not None and match["id"] == "hn-front-page"
    ni_flow._pause_for_recipe_confirm(store, iid, intent, match)
    flow = _board_flow(client, iid)
    assert flow["state"] == "confirm_source"
    from urllib.parse import urlparse
    assert urlparse(flow.get("source_url") or "").hostname == "hn.algolia.com"
    # And a truly uncovered ask still gets the honest empty pick pause.
    iid2 = client.post("/api/ni/intake",
                       json={"request": "show me the tides for Limehouse Boat Landing SC"},
                       headers=_LOCAL).json()["id"]
    ni_flow.reenter_source_pick(store, iid2, "no recipe matched — user picks")
    flow2 = _board_flow(client, iid2)
    assert flow2["state"] == "source" and flow2.get("suggestions") == []


def test_field_5_wrong_value_note_is_journaled_and_rewinds_honestly(client) -> None:
    """Field: weather card, "should be in Fahrenheit degrees" note. This card
    is MODEL-sourced, so the rebuild path refuses with guidance — the note
    still rides the journal verbatim and the card returns to draft honestly.
    (The http_json rebuild path is field_5b below — G4a made the promise
    true for the class the field failure was actually in.)"""
    from smartbrain_3000 import tools
    ctx = tools.ToolContext(ni=client.app.state.ni)
    body = {
        "title": "Weather in Charleston, SC", "goal": "show the weather",
        "params": {}, "source": {"type": "model", "instruction": "weather"},
        "pipeline": [],
        "scene": {"type": "stack", "dir": "v", "gap": "sm", "children": [
            {"type": "text", "value": "{{text}}", "role": "title",
             "tone": "default", "size": "md"}]},
        "display": {"size": "small"}, "interval_minutes": 15,
        "preview_payload": {"text": "29.2 C"},
    }
    iid = tools.INTERNAL_NI_TOOLS["create_ni_item"](ctx, body)["id"]
    store = client.app.state.ni
    assert store.get_item(iid)["state"] == "commissioning"
    r = client.post(f"/api/ni/items/{iid}/validate",
                    json={"ok": False, "note": "should be in Fahrenheit degrees."})
    assert r.status_code == 200
    assert store.get_item(iid)["state"] == "draft"
    journal = store.read_journal(iid)
    assert any("should be in Fahrenheit degrees." in e["summary"]
               for e in journal), "the user's words ride the journal verbatim"
    assert any("could not drive a rebuild" in e["summary"] for e in journal), \
        "the refusal is honest, never silent"


def test_field_5b_fahrenheit_note_drives_the_rebuild(client) -> None:
    """G4a closes the °F field failure for its real class: on an http_json
    card, Something's-wrong + the note re-enters sampling with the note
    sealed as part of the goal (the rebuild authors the conversion and the
    judge verifies it — pinned end-to-end in test_ni_flow)."""
    from smartbrain_3000 import tools
    ctx = tools.ToolContext(ni=client.app.state.ni)
    body = {
        "title": "Weather in Charleston (http)", "goal": "track the weather",
        "params": {},
        "source": {"type": "model", "instruction": "placeholder"},
        "pipeline": [],
        "scene": {"type": "stack", "dir": "v", "gap": "sm", "children": [
            {"type": "text", "value": "{{temperature}}", "role": "title",
             "tone": "default", "size": "md"}]},
        "display": {"size": "small"}, "interval_minutes": 15,
        "preview_payload": {"temperature": "29.2"},
    }
    iid = tools.INTERNAL_NI_TOOLS["create_ni_item"](ctx, body)["id"]
    store = client.app.state.ni
    spec = dict(store.get_item(iid)["spec"])
    spec["source"] = {"type": "http_json",
                       "url": "https://api.open-meteo.com/v1/forecast?latitude=32.7&longitude=-79.9&current_weather=true"}
    spec["pipeline"] = [{"op": "extract",
                          "paths": {"temperature": "current_weather.temperature"}}]
    store.update_spec(iid, spec, origin="user")
    r = client.post(f"/api/ni/items/{iid}/validate",
                    json={"ok": False, "note": "should be in Fahrenheit degrees."})
    assert r.status_code == 200 and r.json()["refine"] == "rebuild", r.text
    record = ni_flow._flow_read(store, iid)
    assert record["_refine_note"] == "should be in Fahrenheit degrees."
    assert record["state"] == "sampling"


def test_field_6_stalled_build_is_swept_and_named(client) -> None:
    """Field class: a worker dies and the card says "Preparing card…" forever.
    The sweep fails it honestly; the card then names the stall and offers the
    ways out (never the blind didn't-finish copy)."""
    iid = client.post("/api/ni/intake", json={"request": "a build that stalls"},
                      headers=_LOCAL).json()["id"]
    ni_flow._fail(client.app.state.ni, iid, "stale",
                  "flow record stranded >1h; failed by sweep")
    flow = _board_flow(client, iid)
    assert "stalled" in flow["reason"], flow
    assert set(flow["reopen"]) == {"retry", "pick_source"}


def test_field_7_every_terminal_on_the_board_obeys_the_law(client) -> None:
    """The law itself, board-level: drive one shell into EVERY terminal error
    class seen in the field; each must expose reason + (question|reopen)."""
    classes = [("fetch", "sample fetch failed: HTTPError"),
               ("mapping", "no candidate matched the menu"),
               ("assembling", "scene bind failed"),
               ("intent", "reply unparseable after retry")]
    store = client.app.state.ni
    for klass, detail in classes:
        iid = client.post("/api/ni/intake",
                          json={"request": f"law case {klass}"},
                          headers=_LOCAL).json()["id"]
        ni_flow._fail(store, iid, klass, detail)
        flow = _board_flow(client, iid)
        assert flow["reason"], f"{klass}: no reason"
        assert flow.get("question") or flow.get("reopen"), f"{klass}: dead end"


def test_field_8_quakes_threshold_routes_instead_of_shipping_m25(client) -> None:
    """G2 upgrade of the quakes field failure: approving the vetted USGS feed
    for an above-magnitude-5 ask must NOT hand off the fixed M2.5 template —
    the continuation re-dispatches the approved URL into freeform sampling
    (where the where-filter is authored from the sealed intent)."""
    from smartbrain_3000 import ni_catalog
    iid = client.post("/api/ni/intake",
                      json={"request": "latest earthquakes above magnitude 5"},
                      headers=_LOCAL).json()["id"]
    store = client.app.state.ni
    intent = {"kind": "external_data", "subject": "earthquakes",
              "cadence_minutes": 15, "wants": ["magnitude"], "threshold": 5,
              "display_hint": "list"}
    ni_flow._transition(store, iid, "intent", intent=intent)
    ni_flow._pause_for_recipe_confirm(store, iid, intent,
                                       ni_catalog.get_recipe("quakes-day-25"))
    r = client.post(f"/api/ni/items/{iid}/flow/confirm-source", headers=_LOCAL)
    assert r.status_code == 200, r.text
    rec2 = ni_flow._flow_read(store, iid)
    assert rec2["state"] == "sampling", "routed to freeform, not template handoff"
    assert rec2.get("_reuse_intent") is True
    item = store.get_item(iid)
    assert item["spec"].get("_shell"), "no verbatim M2.5 card was sealed"


def test_field_9_quakes_disclosure_no_longer_lies(client) -> None:
    """G2: the confirm card's coverage line for the quakes recipe must not
    claim magnitude/location are missing (top_mag/top_place serve them)."""
    from smartbrain_3000 import ni_catalog
    iid = client.post("/api/ni/intake",
                      json={"request": "latest earthquakes"},
                      headers=_LOCAL).json()["id"]
    store = client.app.state.ni
    ni_flow._pause_for_recipe_confirm(
        store, iid, {"wants": ["location", "magnitude", "depth", "time"]},
        ni_catalog.get_recipe("quakes-day-25"))
    row = next(x for x in client.get("/api/ni/board").json()["items"]
               if x["id"] == iid)
    not_covered = row["flow"].get("not_covered") or []
    assert "magnitude" not in not_covered and "location" not in not_covered
    assert set(not_covered) == {"depth", "time"}


def test_field_10_us_weather_defaults_to_fahrenheit_at_source(client) -> None:
    """G2 upgrade of the °C-for-Charleston failure: the consent pause seals
    fahrenheit/mph unit fills for a US place — what the card shows is what
    will run."""
    from smartbrain_3000 import ni_catalog
    iid = client.post("/api/ni/intake",
                      json={"request": "track the weather in Charleston, SC"},
                      headers=_LOCAL).json()["id"]
    store = client.app.state.ni
    ni_flow._pause_for_recipe_confirm(
        store, iid,
        {"wants": ["temperature"], "place": "Charleston, SC"},
        ni_catalog.get_recipe("weather-open-meteo"))
    row = next(x for x in client.get("/api/ni/board").json()["items"]
               if x["id"] == iid)
    fills = row["flow"].get("fills") or {}
    assert fills.get("temperature_unit") == "fahrenheit"
    assert fills.get("wind_speed_unit") == "mph"
    assert "temperature_unit=fahrenheit" in (row["flow"].get("filled_url") or "")
