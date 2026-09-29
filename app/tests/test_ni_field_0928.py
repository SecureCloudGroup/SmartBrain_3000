"""Field round 2026-09-28 (v0.24.0 live tests): every class the operator's first tests and the live
end-to-end run exposed, pinned model-free. Each test names the ask that failed."""

from __future__ import annotations

import json

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import formats, netguard, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.library_resolve import _clock_offset
from smartbrain_3000.secrets import gen_master_key

NOAA_HILO = {"predictions": [{"t": "2026-09-28 03:37", "v": "0.194", "type": "L"},
                             {"t": "2026-09-28 09:48", "v": "6.904", "type": "H"}]}


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


# --- "tides for Charleston SC": numbers sent as text, and what a want's type is ---------------------

def test_a_want_with_a_text_word_in_any_form_is_text() -> None:
    assert ni_flow.infer_fields({"wants": ["tide times"]}) == {"tide_times": "string"}
    assert ni_flow.infer_fields({"wants": ["station name"]}) == {"station_name": "string"}
    assert ni_flow.infer_fields({"wants": ["tide height"]}) == {"tide_height": "number"}


@pytest.mark.parametrize(("example", "numeric"), [
    ('"6.904"', True), ('"-3"', True), ('"42"', True), ('"0.5"', True),
    ('"02134"', False), ('"N/A"', False), ('"2026-09-28"', False), ('"1e5"', False)])
def test_numeric_text(example, numeric) -> None:
    assert ni_flow._numeric_text(example) is numeric


def test_the_menu_offers_numbers_sent_as_text_for_number_fields() -> None:
    cands = ni_flow.derive_paths(NOAA_HILO)
    usable, menu = ni_flow.build_mapping_menu(cands, {"height": "number"})
    assert [c["path"] for c in usable if c["type"] == "number"] == ["predictions[0].v"]
    assert "sent as text" in menu and "count of items" not in menu  # not a how-many ask
    usable, menu = ni_flow.build_mapping_menu(cands, {"height": "number"}, count_words="how many tides today")
    assert "count of items" in menu
    usable, _ = ni_flow.build_mapping_menu(cands, {"predictions": "number"})  # the want names the list
    assert any(c.get("count") for c in usable)


def test_a_value_card_converts_a_number_sent_as_text_on_every_run() -> None:
    built = ni_flow.assemble_from_mapping({"height": "predictions[0].v"}, {"height": "number"},
                                          "value", NOAA_HILO)
    assert built["preview_payload"]["height"] == 0.194
    assert built["pipeline"][-1] == {"op": "transform", "apply": [{"fn": "number", "field": "height"}]}
    nimod.validate_spec({"version": 1, "title": "t", "goal": "g", "params": {},
                         "source": {"type": "http_json", "url": "https://a.example.org/x"},
                         "pipeline": built["pipeline"], "scene": built["scene"],
                         "display": {"size": "small"}, "interval_minutes": 15})


def test_the_number_transform() -> None:
    assert nimod._txf_number("6.904", None) == 6.904 and nimod._txf_number("42", None) == 42
    assert nimod._txf_number([{"v": "1.5"}], "v") == [{"v": 1.5}]
    with pytest.raises(nimod.NIError):
        nimod._txf_number("N/A", None)


def test_a_list_card_shows_the_items_fields_in_the_sources_order_under_the_subject() -> None:
    built = ni_flow.assemble_from_mapping({"tide_times": "predictions[0].v"}, {"tide_times": "string"},
                                          "list", NOAA_HILO, title="tides")
    scene = built["scene"]
    assert scene["children"][0]["value"] == "tides"
    assert scene["children"][1]["template"]["value"] == "{{item.t}} · {{item.v}} · {{item.type}}"


def test_row_padding_never_shows_ids_codes_links_or_epochs() -> None:
    rows = [{"type": "Feature", "id": f"us6000ty8{i}", "properties": {
        "mag": 4.6 + i, "place": f"near Kodiak {i}", "time": 1790636084809, "url": "https://x", "code": "6000ty8z",
        "ids": ",us6000ty8z,", "status": "reviewed"}} for i in range(2)]
    fields = ni_flow._row_fields("features", "item.properties.place", {"q": "features[0].properties.place"}, rows)
    assert fields == ["item.properties.mag", "item.properties.place"]


# --- "hurricanes right now": how many items a list holds -----------------------------------------

def test_a_number_field_that_picks_a_list_shows_its_count() -> None:
    built = ni_flow.assemble_from_mapping({"storms": "activeStorms"}, {"storms": "number"}, "value",
                                          {"activeStorms": [{"name": "Fay"}, {"name": "Gil"}]})
    assert built["preview_payload"]["storms"] == 2
    empty = ni_flow.assemble_from_mapping({"storms": "activeStorms"}, {"storms": "number"}, "value",
                                          {"activeStorms": []})
    assert empty["preview_payload"]["storms"] == 0


# --- "unemployment rate": a series stored oldest first ----------------------------------------------

def test_a_csv_series_leads_with_the_latest_value_and_keeps_the_newest_rows(monkeypatch) -> None:
    body = "observation_date,UNRATE\n" + "\n".join(f"19{y:02d}-01-01,{y / 10:.1f}" for y in range(48, 99)) \
        + "\n2026-08-01,4.3\n"
    monkeypatch.setattr(formats, "MAX_CSV_ROWS", 10)
    out = formats.parse_csv(body)
    assert out["rows"][0] == {"observation_date": "2026-08-01", "UNRATE": "4.3"}
    assert len(out["rows"]) == 10


def test_a_csv_that_is_not_a_date_series_keeps_its_order() -> None:
    out = formats.parse_csv("name,count\nb,2\na,1\n")
    assert [r["name"] for r in out["rows"]] == ["b", "a"]


# --- "wave height in Santa Cruz": a whitespace table and a long text body ---------------------------

NDBC = """#YY  MM DD hh mm WDIR WSPD WVHT
#yr  mo dy hr mn degT m/s  m
2026 09 28 23 40 290  5.0  1.2
2026 09 28 23 10 280  4.0  1.3
2026 09 28 22 40 270  4.5  1.1
"""


def test_a_whitespace_table_becomes_rows() -> None:
    out = formats.parse_text(NDBC)
    assert out["columns"][:3] == ["YY", "MM", "DD"] and out["rows"][0]["WVHT"] == "1.2"


def test_a_huge_text_body_still_derives_paths() -> None:
    cands = ni_flow.derive_paths({"text": "x" * 200_000})
    assert cands and len(cands[0].get("example", "")) < 1000


# --- "next Dodgers game": a schedule looks forward ----------------------------------------------------

def test_a_schedule_window_looks_forward() -> None:
    params = [{"name": "startDate", "fill": {"from": "clock", "offset_days": -30}},
              {"name": "endDate", "fill": {"from": "clock", "offset_days": 0}}]
    forward = {"kinds": ["schedule", "next_event"]}
    history = {"kinds": ["trend"]}
    assert [_clock_offset(forward, p["fill"], params) for p in params] == [0, 30]
    assert [_clock_offset(history, p["fill"], params) for p in params] == [-30, 0]


# --- "top news headlines" / "Hacker News": a web page reaches the page reader -------------------------

def test_an_html_page_goes_to_the_page_reader(monkeypatch) -> None:
    def not_json(url, headers=None, allow_redirects=True):
        raise netguard.FetchError("not json", kind="not_json")

    def html(url, fmt, headers=None, allow_redirects=True):
        return {"text": "<html><head><title>x</title></head><body>hi</body></html>", "content_type": "text/html"}

    monkeypatch.setattr(netguard, "safe_fetch_json", not_json)
    monkeypatch.setattr(netguard, "safe_fetch_text", html)
    with pytest.raises(netguard.FetchError) as exc:
        ni_flow._sniffed_fetch("https://news.example.org/")
    assert exc.value.kind == "not_json"  # the flow's page door takes it from here


# --- "temp in Charleston SC": a refused web page is never offered; a refused Library source re-picks ---

def test_a_page_that_refuses_us_is_never_offered(monkeypatch) -> None:
    def refuse(url, **kw):
        raise netguard.FetchError("upstream returned HTTP 403", status=403)

    monkeypatch.setattr(ni_flow.pagegraph, "fetch_page_graph", refuse)
    rows = [{"title": "Blocked", "host": "b.example.org", "url": "https://b.example.org/", "snippet": ""}]
    assert ni_flow._s2_evaluate(rows, {"subject": "weather", "wants": ["temp"]}) == []


def test_a_library_source_that_refuses_us_goes_back_to_the_pick() -> None:
    store = _store()
    item_id = ni_flow.create_shell_item(store, "Yankees score")
    record = ni_flow._make_record("Yankees score", "sampling")
    record["_ranked_library"] = [
        {"source_id": "espn", "title": "ESPN scoreboard", "host": "site.api.espn.com", "provider": "ESPN",
         "url": "https://site.api.espn.com/x", "authority": "aggregator", "label": "", "choice": False},
        {"source_id": "mlb", "title": "MLB schedule", "host": "statsapi.mlb.com", "provider": "MLB",
         "url": "https://statsapi.mlb.com/y", "authority": "official", "label": "", "choice": False}]
    ni_flow._flow_write(store, item_id, record)
    out = ni_flow._repick_without(store, item_id, "https://site.api.espn.com/x")
    assert out["state"] == "source" and out["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert [r["source_id"] for r in out["_ranked_library"]] == ["mlb"]
    assert "ESPN refused" in out["notes"][-1]
    assert ni_flow._repick_without(store, item_id, "https://elsewhere.example.org/") is None


def test_the_card_json_is_unchanged_for_plain_numbers() -> None:
    built = ni_flow.assemble_from_mapping({"price": "p"}, {"price": "number"}, "value", {"p": 3.5})
    assert built["pipeline"] == [{"op": "extract", "paths": {"price": "p"}}]
    assert json.dumps(built["preview_payload"]) == '{"price": 3.5}'


# --- "when is sunset in Denver": a timestamp reads as local time --------------------------------------

def test_a_timestamp_value_reads_as_local_time_on_every_run() -> None:
    built = ni_flow.assemble_from_mapping({"sunset": "results.sunset"}, {"sunset": "string"}, "value",
                                          {"results": {"sunset": "2026-09-29T00:48:17+00:00"}})
    assert built["pipeline"][-1] == {"op": "transform", "apply": [{"fn": "time", "field": "sunset"}]}
    shown = built["preview_payload"]["sunset"]
    assert "2026" not in shown and ("AM" in shown or "PM" in shown)


def test_local_time_formats() -> None:
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()
    assert nimod.local_time(now.isoformat()).endswith(("AM", "PM"))
    assert nimod.local_time(int(now.timestamp() * 1000)).endswith(("AM", "PM"))
    far = (now + timedelta(days=30)).isoformat()
    assert "," in nimod.local_time(far)
    with pytest.raises(nimod.NIError):
        nimod.local_time("soon")


def test_tide_rows_show_local_times() -> None:
    built = ni_flow.assemble_from_mapping({"t": "predictions[0].t"}, {"t": "string"}, "list", NOAA_HILO)
    assert built["pipeline"][-1]["apply"] == [{"fn": "time", "field": "rows", "key": "t"}]
    assert "2026-" not in built["preview_payload"]["rows"][0]["t"]


def test_count_spellings_resolve_to_the_offered_count() -> None:
    offered = {"activeStorms": {"type": "number", "count": True}}
    assert ni_flow._count_spellings({"n": "activeStorms.length"}, offered) == {"n": "activeStorms"}
    assert ni_flow._count_spellings({"n": "len(activeStorms)"}, offered) == {"n": "activeStorms"}
    assert ni_flow._count_spellings({"n": "other.length"}, offered) == {"n": "other.length"}


def test_nested_row_timestamps_convert() -> None:
    sample = {"dates": [{"games": {"gameDate": "2026-10-03T23:10:00Z", "venue": "Dodger Stadium"}},
                        {"games": {"gameDate": "2026-10-04T23:10:00Z", "venue": "Dodger Stadium"}}]}
    built = ni_flow.assemble_from_mapping({"game": "dates[0].games.gameDate"}, {"game": "string"}, "list", sample)
    assert {"fn": "time", "field": "rows", "key": "games.gameDate"} in built["pipeline"][-1]["apply"]
    assert "2026-" not in built["preview_payload"]["rows"][0]["games"]["gameDate"]


def test_a_count_the_model_spells_out_for_a_list_in_the_sample_is_accepted() -> None:
    offered: dict = {}
    reply = ni_flow._count_spellings({"storms": "activeStorms.length"}, offered, {"activeStorms"})
    assert reply == {"storms": "activeStorms"} and offered["activeStorms"]["count"] is True


def test_when_every_library_source_refuses_the_card_searches_the_web(monkeypatch) -> None:
    store = _store()
    item_id = ni_flow.create_shell_item(store, "Yankees score")
    record = ni_flow._make_record("Yankees score", "sampling")
    record["_ranked_library"] = [{"source_id": "espn", "title": "ESPN", "host": "site.api.espn.com",
                                  "provider": "ESPN", "url": "https://site.api.espn.com/x", "authority": "",
                                  "label": "", "choice": False}]
    ni_flow._flow_write(store, item_id, record)

    def refused(url):
        raise netguard.FetchError("upstream returned HTTP 403", status=403)

    calls = []
    monkeypatch.setattr(ni_flow, "_pause_with_web",
                        lambda s, i, r, it, cm: calls.append(r) or ni_flow._transition(
                            s, i, "source", error=ni_flow.AWAITING_SOURCE_PICK, _ranked_search=[
                                {"title": "MLB", "host": "mlb.com", "url": "https://www.mlb.com/yankees", "evidence": []}]))
    out = ni_flow._sample_and_map(store, item_id, "Yankees score", {"wants": ["score"]},
                                  "https://site.api.espn.com/x", lambda p: "{}", refused)
    assert calls == ["Yankees score"] and out["_ranked_search"][0]["host"] == "mlb.com"


def test_a_card_with_nothing_to_show_is_not_built() -> None:
    with pytest.raises(ValueError, match="empty"):
        ni_flow.assemble_from_mapping({"score": "s"}, {"score": "string"}, "value", {"s": ""})


def test_a_path_off_the_menu_that_resolves_in_the_sample_is_accepted() -> None:
    offered: dict = {}
    sample = {"dates": [{"officialDate": "2026-10-03"}]}
    ni_flow._offer_resolving_paths({"date": "dates[0].officialDate", "x": "nope.path"}, offered,
                                   {"date": "string", "x": "string"}, sample)
    assert list(offered) == ["dates[0].officialDate"]


def test_a_list_card_with_blank_rows_is_not_built() -> None:
    with pytest.raises(ValueError, match="empty"):
        ni_flow.assemble_from_mapping({"d": "rows[0].level"}, {"d": "string"}, "list",
                                      {"rows": [{"level": ""}, {"level": ""}]})


def test_dig_follows_list_positions() -> None:
    row = {"games": [{"gameDate": "2026-10-03T23:10:00Z"}], "teams": {"home": {"name": "LAD"}}}
    assert ni_flow._dig(row, "games[0].gameDate") == "2026-10-03T23:10:00Z"
    assert ni_flow._dig(row, "teams.home.name") == "LAD" and ni_flow._dig(row, "games[3].x") is None


def test_a_list_picked_for_a_number_is_its_count() -> None:
    offered: dict = {}
    ni_flow._offer_resolving_paths({"storms": "activeStorms"}, offered, {"storms": "number"},
                                   {"activeStorms": []})
    assert offered["activeStorms"]["count"] is True


def test_time_step_keys_follow_list_positions() -> None:
    rows = [{"games": [{"gameDate": "2026-10-03T23:10:00Z"}]}]
    out = nimod._txf_rows(rows, "games[0].gameDate", nimod.local_time)
    assert "2026-" not in out[0]["games"][0]["gameDate"]
    assert nimod._ROW_KEY_RE.fullmatch("games[0].gameDate") and not nimod._ROW_KEY_RE.fullmatch("a..b")


def test_a_count_want_stays_a_number_when_the_sample_has_no_numbers() -> None:
    cands = ni_flow.derive_paths({"activeStorms": [], "note": "none"})
    assert ni_flow.reconcile_field_types({"count": "number"}, cands) == {"count": "number"}
