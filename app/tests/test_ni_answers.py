"""Library ``answers``: a picked source that declares which response paths answer which questions
builds its card from them — deterministic selection, pipeline, scene and units, no model path-guessing
— and falls back to the model mapping path when the live response doesn't fit. Model-free, no network:
the Library pack is a REAL DuckDB file (``test_library_index`` style), only the fetch is replaced."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, library_resolve, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.secrets import gen_master_key

WEATHER_URL = "https://api.open-meteo.com/v1/forecast?latitude=40.71&longitude=-74.01"
_DAYS = [(datetime.now().date() + timedelta(days=i)).isoformat() for i in range(7)]
OPEN_METEO = {
    "current_units": {"temperature_2m": "°F", "relative_humidity_2m": "%", "wind_speed_10m": "mph"},
    "current": {"time": "2026-09-28T14:00", "temperature_2m": 71.3, "relative_humidity_2m": 62,
                "weather_code": 2, "wind_speed_10m": 8.1},
    "daily_units": {"temperature_2m_max": "°F", "precipitation_probability_max": "%"},
    "daily": {"time": _DAYS, "weather_code": [2, 61, 3, 0, 95, 71, 45],
              "temperature_2m_max": [78.1, 74.0, 70.2, 69.9, 72.5, 33.0, 60.1],
              "temperature_2m_min": [60.0, 58.2, 55.1, 54.0, 57.7, 25.3, 48.8],
              "precipitation_probability_max": [10, 80, 20, 0, 90, 60, 5]},
}


def _value(name, label, path, words, *, primary=False, **extra):
    return {"name": name, "label": label, "words": words, "primary": primary, "kind": "value",
            "path": path, "type": "number", **extra}


WEATHER_ANSWERS = [
    _value("temperature", "Temperature", "current.temperature_2m",
           ["temperature", "temp", "hot", "cold", "degrees"], primary=True,
           unit_path="current_units.temperature_2m"),
    _value("conditions", "Conditions", "current.weather_code",
           ["conditions", "sky", "sunny", "cloudy", "clear"], primary=True, codes="wmo_weather"),
    _value("high_today", "High today", "daily.temperature_2m_max[0]", ["high", "high today", "max"],
           primary=True, unit_path="daily_units.temperature_2m_max"),
    _value("low_today", "Low today", "daily.temperature_2m_min[0]", ["low", "low today", "min"],
           primary=True, unit="°F"),
    _value("humidity", "Humidity", "current.relative_humidity_2m", ["humidity", "humid", "muggy"],
           unit_path="current_units.relative_humidity_2m"),
    _value("rain_today", "Rain chance today", "daily.precipitation_probability_max[0]",
           ["rain", "rain today", "precipitation", "umbrella"], unit="%"),
    _value("rain_tomorrow", "Rain chance tomorrow", "daily.precipitation_probability_max[1]",
           ["rain tomorrow", "tomorrow", "precipitation tomorrow"], unit="%"),
    {"name": "daily_forecast", "label": "Daily forecast", "kind": "columns", "primary": False,
     "words": ["forecast", "week", "weekend", "this weekend", "daily", "next days"],
     "columns": [{"path": "daily.time", "label": "Day", "type": "date"},
                 {"path": "daily.weather_code", "label": "Conditions", "type": "number", "codes": "wmo_weather"},
                 {"path": "daily.temperature_2m_max", "label": "High", "type": "number",
                  "unit_path": "daily_units.temperature_2m_max"},
                 {"path": "daily.precipitation_probability_max", "label": "Rain", "type": "number",
                  "unit": "%"}]},
]
QUAKES = {"features": [
    {"properties": {"mag": "4.5", "place": "10 km N of Ridgecrest, CA", "time": 1790000000000}},
    {"properties": {"mag": "5.1", "place": "Off the coast of Oregon", "time": 1790000900000}}]}
QUAKE_ANSWERS = [{"name": "quakes", "label": "Recent earthquakes", "kind": "list", "primary": True,
                  "words": ["earthquake", "quakes", "latest", "recent"], "path": "features",
                  "newest_first": True,
                  "row": [{"path": "properties.mag", "label": "Magnitude", "type": "number"},
                          {"path": "properties.place", "label": "Place", "type": "text"},
                          {"path": "properties.time", "label": "When", "type": "time"}]}]
STORMS_ANSWERS = [{"name": "storms", "label": "Active storms", "kind": "value", "primary": True,
                   "words": ["storms", "hurricanes", "how many"], "path": "activeStorms", "type": "count"}]
RATES_ANSWERS = [_value("rate", "Exchange rate", "rates.{quote}", ["rate", "exchange"], primary=True)]
FAA_ANSWERS = [{"name": "delays", "label": "Delays", "kind": "list", "primary": True,
                "words": ["delay", "delays", "status"], "path": "items",
                "filter": {"path": "ARPT", "equals": "{airport}"},
                "row": [{"path": "ARPT", "label": "Airport", "type": "text"},
                        {"path": "Reason", "label": "Reason", "type": "text"}]}]


# --- the fixture pack: real DuckDB, schema as ``sourcetool build`` writes it -----------------------

def _record(sid: str, answers: list[dict]) -> dict:
    return {"id": sid, "name": sid, "description": f"{sid} description", "tier": "curated",
            "categories": ["weather/current"], "kinds": ["current_value"], "examples": [],
            "access": {"kind": "http_json", "url_template": "https://example.org/x", "params": [],
                       "auth": "none", "headers": {}},
            "provider": {"id": "p", "name": "Provider", "authority": "official"}, "answers": answers}


def _build_pack(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE library_sources(id VARCHAR PRIMARY KEY, record JSON)")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    for sid, answers in (("open-meteo-forecast", WEATHER_ANSWERS), ("usgs-quakes", QUAKE_ANSWERS),
                         ("nhc-storms", STORMS_ANSWERS), ("fx-rates", RATES_ANSWERS),
                         ("faa-status", FAA_ANSWERS), ("no-answers", [])):
        con.execute("INSERT INTO library_sources VALUES (?, ?)", (sid, json.dumps(_record(sid, answers))))
    con.execute("INSERT INTO library_meta VALUES ('records', '6')")
    con.close()


class _FakeNet:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


@pytest.fixture()
def lib(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_pack(src)
    raw = gzip.compress(src.read_bytes())
    pack = {"tag": "vtest", "url": "https://example.org/library.duckdb.gz",
            "sha256": hashlib.sha256(raw).hexdigest()}
    idx = library_index.LibraryIndex(tmp_path / "data", netguard_mod=_FakeNet(raw), pack=pack)
    idx.install()
    return idx


@pytest.fixture()
def weather(lib):
    return [ni_flow._clean_answer(a) for a in lib.answers("open-meteo-forecast")]


def _names(chosen: list[dict]) -> list[str]:
    return [a["name"] for a in chosen]


# --- the accessor ------------------------------------------------------------------------------------

def test_the_library_serves_a_records_answers(lib, monkeypatch) -> None:
    assert [a["name"] for a in lib.answers("open-meteo-forecast")][:2] == ["temperature", "conditions"]
    assert lib.answers("no-answers") == [] and lib.answers("missing") == []
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    cleaned = ni_flow._library_answers("open-meteo-forecast")
    assert len(cleaned) == len(WEATHER_ANSWERS) and cleaned[-1]["cells"][0]["type"] == "date"


def test_a_malformed_answer_is_skipped_not_fatal() -> None:
    good = WEATHER_ANSWERS[0]
    assert ni_flow._clean_answer(good) is not None
    assert ni_flow._clean_answer({**good, "extra": 1}) is None          # closed keys
    assert ni_flow._clean_answer({**good, "name": "Bad Name"}) is None
    assert ni_flow._clean_answer({**good, "codes": "made_up"}) is None  # only the shipped table
    assert ni_flow._clean_answer({**QUAKE_ANSWERS[0], "row": []}) is None


# --- selection -----------------------------------------------------------------------------------------

def test_a_general_ask_shows_the_primary_answers(weather) -> None:
    assert _names(ni_flow.select_answers(weather, "NYC weather", [])) == \
        ["temperature", "conditions", "high_today", "low_today"]
    # the model's wants lead, then the primaries fill in
    assert _names(ni_flow.select_answers(weather, "NYC weather", ["humidity"])) == \
        ["humidity", "temperature", "conditions", "high_today"]


def test_the_users_words_pick_the_answer(weather) -> None:
    assert _names(ni_flow.select_answers(weather, "will it rain tomorrow in Seattle", [])) == ["rain_tomorrow"]
    assert _names(ni_flow.select_answers(weather, "rain today in Boston", [])) == ["rain_today"]
    assert _names(ni_flow.select_answers(weather, "temps in Charleston SC", [])) == ["temperature"]
    assert _names(ni_flow.select_answers(weather, "how humid is Miami", ["temperature"])) == ["humidity"]


def test_a_forecast_ask_shows_the_columns_answer(weather) -> None:
    assert _names(ni_flow.select_answers(weather, "Philly forecast", [])) == ["daily_forecast"]
    assert _names(ni_flow.select_answers(weather, "weather this weekend in Austin", [])) == ["daily_forecast"]
    # an ask for many things that names no answer still takes the table
    assert _names(ni_flow.select_answers(weather, "Denver next few days", [])) == ["daily_forecast"]


# --- pipelines, scenes, previews ---------------------------------------------------------------------

def _bound_texts(built: dict) -> list[str]:
    bound = nimod.bind_scene(built["scene"], built["preview_payload"])
    nimod._enforce_bind_types(built["scene"], bound)
    out: list[str] = []
    stack = [bound]
    while stack:
        node = stack.pop(0)
        if node.get("type") in ("text", "number"):
            out.append(f"{node['value']}{node.get('unit') or ''}")
        stack.extend(node.get("children") or [])
    return out


def _valid(built: dict) -> None:
    nimod.validate_spec({"version": 1, "title": "t", "goal": "g", "params": {},
                         "source": {"type": "http_json", "url": WEATHER_URL},
                         "pipeline": built["pipeline"], "scene": built["scene"],
                         "display": {"size": "small"}, "interval_minutes": 15})


def test_value_answers_build_with_labels_units_and_words_for_codes(weather) -> None:
    chosen = ni_flow.select_answers(weather, "NYC weather", [])
    built = ni_flow.build_from_answers(chosen, OPEN_METEO, "NYC weather")
    _valid(built)
    assert built["pipeline"][0] == {"op": "extract", "paths": {
        "temperature": "current.temperature_2m", "conditions": "current.weather_code",
        "high_today": "daily.temperature_2m_max[0]", "low_today": "daily.temperature_2m_min[0]"}}
    prev = built["preview_payload"]
    assert prev["temperature"] == 71.3 and prev["conditions"] == "Partly cloudy" and prev["low_today"] == 60.0
    texts = _bound_texts(built)
    assert texts[:2] == ["Temperature", "71.3°F"]  # the first answer is the headline
    assert "Partly cloudy" in texts and "High today" in texts and "78.1°F" in texts and "60.0°F" in texts
    assert built["klass"] == "value" and built["fields"]["conditions"] == "string"


def test_the_unit_path_is_read_once_and_frozen(weather) -> None:
    built = ni_flow.build_from_answers(weather[:1], OPEN_METEO, "t")
    number = built["scene"]["children"][1]
    assert number["unit"] == "°F"  # a literal in the scene
    later = copy.deepcopy(OPEN_METEO)
    later["current"]["temperature_2m"] = "80.5"   # the next refresh, sent as text
    del later["current_units"]                    # the unit path is never read again
    out = nimod.run_pipeline(built["pipeline"], later)
    assert out["temperature"] == 80.5


def test_a_columns_answer_zips_a_daily_forecast(weather) -> None:
    chosen = ni_flow.select_answers(weather, "Philly forecast", [])
    built = ni_flow.build_from_answers(chosen, OPEN_METEO, "Philly forecast")
    _valid(built)
    apply = built["pipeline"][1]["apply"]
    assert apply[0] == {"fn": "zip", "field": "day", "with": ["conditions", "high", "rain"], "as": "rows"}
    assert apply[1] == {"fn": "top_n", "field": "rows", "n": 7}
    rows = built["preview_payload"]["rows"]
    assert len(rows) == 7 and rows[1]["conditions"] == "Light rain" and rows[4]["conditions"] == "Thunderstorm"
    assert "-" not in rows[0]["day"]  # a date, shown as a date
    template = built["scene"]["children"][1]["template"]["value"]
    assert template == "{{item.day}} · {{item.conditions}} · {{item.high}}°F · {{item.rain}}%"
    assert built["scene"]["children"][1]["max"] == 7
    texts = _bound_texts(built)
    assert texts[0] == "Philly forecast" and texts[2].endswith("Light rain · 74.0°F · 80%")


def test_a_list_answer_with_time_rows_newest_first(lib) -> None:
    chosen = [ni_flow._clean_answer(a) for a in lib.answers("usgs-quakes")]
    built = ni_flow.build_from_answers(chosen, QUAKES, "latest earthquakes")
    _valid(built)
    assert built["pipeline"][1]["apply"] == [
        {"fn": "reverse", "field": "rows"},
        {"fn": "number", "field": "rows", "key": "properties.mag"},
        {"fn": "time", "field": "rows", "key": "properties.time"}]
    first = built["preview_payload"]["rows"][0]["properties"]
    assert first["mag"] == 5.1 and first["place"] == "Off the coast of Oregon"  # newest first
    assert first["time"].endswith(("AM", "PM"))
    assert built["scene"]["children"][1]["template"]["value"] == \
        "{{item.properties.mag}} · {{item.properties.place}} · {{item.properties.time}}"


def test_a_count_answer_shows_zero_for_an_empty_list(lib) -> None:
    chosen = [ni_flow._clean_answer(a) for a in lib.answers("nhc-storms")]
    built = ni_flow.build_from_answers(chosen, {"activeStorms": []}, "hurricanes right now")
    _valid(built)
    assert built["pipeline"] == [{"op": "extract", "paths": {"storms_items": "activeStorms"}},
                                 {"op": "transform", "apply": [
                                     {"fn": "count", "field": "storms_items", "as": "storms"}]}]
    assert built["preview_payload"]["storms"] == 0
    assert _bound_texts(built)[:2] == ["Active storms", "0"]


def test_param_segments_and_row_filters_take_the_filled_values(lib) -> None:
    rates = [ni_flow._clean_answer(a) for a in lib.answers("fx-rates")]
    built = ni_flow.build_from_answers(rates, {"rates": {"EUR": "0.91", "JPY": 149.2}}, "euro",
                                       params={"quote": "EUR"})
    assert built["pipeline"][0]["paths"] == {"rate": 'rates["EUR"]'} and built["preview_payload"]["rate"] == 0.91
    with pytest.raises(ValueError, match="quote"):
        ni_flow.build_from_answers(rates, {"rates": {"EUR": 1}}, "euro", params={})
    faa = [ni_flow._clean_answer(a) for a in lib.answers("faa-status")]
    sample = [{"ARPT": "JFK", "Reason": "wind"}, {"ARPT": "ORD", "Reason": "volume"}]  # a bare list
    built = ni_flow.build_from_answers(faa, sample, "O'Hare delays", params={"airport": "ORD"})
    _valid(built)
    assert built["pipeline"][1]["apply"][0] == {"fn": "where", "field": "rows", "key": "ARPT", "op": "eq",
                                                "value": "ORD"}
    assert built["preview_payload"]["rows"] == [{"ARPT": "ORD", "Reason": "volume"}]
    # the engine runs the card over the same {"items": [...]} wrap on every refresh
    assert nimod.run_pipeline(built["pipeline"], {"items": sample})["rows"][0]["ARPT"] == "ORD"


def test_param_segments_are_whole_segments_filled_as_quoted_keys() -> None:
    assert ni_flow._fill_param_segments("{coin}.usd", {"coin": "bitcoin"}) == '["bitcoin"].usd'
    assert ni_flow._fill_param_segments("a.{x}[0].b", {"x": "5"}) == 'a["5"][0].b'
    assert ni_flow._fill_param_segments("rates.{q}", {"q": "a.b"}) == 'rates["a.b"]'  # never re-segments
    with pytest.raises(ValueError):
        ni_flow._fill_param_segments("rates.x{quote}", {"quote": "EUR"})
    with pytest.raises(ValueError):
        ni_flow._fill_param_segments("rates.{quote}", {"quote": 'EU"R'})


NEOWS = {"element_count": 2, "near_earth_objects": {"2026-09-27": [
    {"name": "(2026 AB)", "estimated_diameter": {"meters": {"estimated_diameter_max": 41.2}}},
    {"name": "(2019 XY)", "estimated_diameter": {"meters": {"estimated_diameter_max": 12.0}}}]}}


def test_a_neows_date_keyed_response_builds_through_a_quoted_key() -> None:
    answers = [ni_flow._clean_answer({
        "name": "asteroids", "label": "Asteroids today", "kind": "list", "primary": True,
        "words": ["asteroids", "near earth"], "path": "near_earth_objects.{date}",
        "row": [{"path": "name", "label": "Name", "type": "text"},
                {"path": "estimated_diameter.meters.estimated_diameter_max", "label": "Size",
                 "type": "number", "unit": "m"}]})]
    built = ni_flow.build_from_answers(answers, NEOWS, "asteroids", params={"date": "2026-09-27"})
    _valid(built)
    assert built["pipeline"][0] == {"op": "extract", "paths": {"rows": 'near_earth_objects["2026-09-27"]'}}
    assert built["preview_payload"]["rows"][0]["name"] == "(2026 AB)"
    assert _bound_texts(built)[1] == "(2026 AB) · 41.2 m"


@pytest.mark.parametrize(("path", "steps"), [
    ('near_earth_objects["2026-09-27"][0].name',
     [("key", "near_earth_objects"), ("key", "2026-09-27"), ("index", 0), ("key", "name")]),
    ('["bitcoin"].usd', [("key", "bitcoin"), ("key", "usd")]),
    ('lines["5"]["a b.c"]', [("key", "lines"), ("key", "5"), ("key", "a b.c")]),
])
def test_quoted_path_keys_parse_and_extract(path, steps) -> None:
    assert nimod.parse_path(path) == steps
    assert nimod.parse_path("a.b[0].c") == [("key", "a"), ("key", "b"), ("index", 0), ("key", "c")]


@pytest.mark.parametrize("path", [
    'a["x', 'a["x"y"]', 'a.["x"]', 'a["x"]b', 'a["x"].', 'a[""]', 'a["x\ny"]', 'a["__proto__"]',
    '"x"', 'a["x"][ 0]', 'a["' + "k" * 201 + '"]', '.["x"]',
])
def test_malformed_quoted_keys_are_refused(path) -> None:
    with pytest.raises(ValueError):
        nimod.parse_path(path)


# --- engine transforms ---------------------------------------------------------------------------------

def test_zip_label_reverse_and_date_transforms() -> None:
    out = nimod.run_pipeline([
        {"op": "extract", "paths": {"t": "d.t", "c": "d.c"}},
        {"op": "transform", "apply": [{"fn": "zip", "field": "t", "with": ["c"], "as": "rows"},
                                      {"fn": "label", "field": "rows", "table": "wmo_weather", "key": "c"},
                                      {"fn": "reverse", "field": "rows"}]}],
        {"d": {"t": ["a", "b"], "c": [0, 99]}})
    assert out["rows"] == [{"t": "b", "c": "Thunderstorm with heavy hail"}, {"t": "a", "c": "Clear sky"}]
    with pytest.raises(nimod.NIError):
        nimod._txf_zip({"a": [1, 2], "b": [1]}, "a", ["b"])
    with pytest.raises(nimod.NIError):
        nimod._txf_zip({"a": [1]}, "a", ["b"])
    with pytest.raises(nimod.NIError):
        nimod._txf_label(nimod.LABEL_TABLES["wmo_weather"], 42)
    assert nimod._txf_label(nimod.LABEL_TABLES["wmo_weather"], 3.0) == "Overcast"
    with pytest.raises(nimod.NIError):
        nimod.run_pipeline([{"op": "extract", "paths": {"x": "x"}},
                            {"op": "transform", "apply": [{"fn": "reverse", "field": "x"}]}], {"x": 1})
    today = datetime.now().astimezone().date()
    assert nimod.local_date(today.isoformat()) == f"{today.strftime('%a %b')} {today.day}"
    far = today + timedelta(days=40)
    assert nimod.local_date(far.isoformat()).startswith(far.strftime("%b"))
    # a date is never shifted by time zones, even written as a UTC midnight timestamp
    assert nimod.local_date("2030-01-02T00:00:00Z").startswith("Jan 2")
    with pytest.raises(nimod.NIError):
        nimod.local_date("2026-02-30")


@pytest.mark.parametrize("op", [
    {"fn": "zip", "field": "a", "with": ["b"], "as": "rows", "extra": 1},
    {"fn": "zip", "field": "a", "with": ["a"], "as": "rows"},
    {"fn": "zip", "field": "a", "with": [], "as": "rows"},
    {"fn": "zip", "field": "a", "with": ["b"], "as": "a"},
    {"fn": "label", "field": "a", "table": "made_up"},
    {"fn": "label", "field": "a", "table": "wmo_weather", "key": "a..b"},
    {"fn": "reverse", "field": "a", "n": 1},
    {"fn": "date", "field": "a", "format": "x"},
])
def test_new_transforms_are_closed(op) -> None:
    with pytest.raises(ValueError):
        nimod._validate_transform_op(op, 0, 0, {"a", "b"})


def test_the_wmo_table_is_the_open_meteo_code_set() -> None:
    table = nimod.LABEL_TABLES["wmo_weather"]
    assert sorted(table) == [0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77,
                             80, 81, 82, 85, 86, 95, 96, 99]
    assert table[45] == "Fog" and table[65] == "Heavy rain" and table[86] == "Heavy snow showers"


def test_number_with_a_nested_row_key() -> None:
    assert nimod._txf_number([{"p": {"m": "4.5"}}], "p.m") == [{"p": {"m": 4.5}}]


def test_the_engine_wraps_a_bare_list_response_like_the_flow() -> None:
    import inspect
    src = inspect.getsource(nimod.run_item)
    assert 'payload = {"items": payload}' in src


# --- the flow --------------------------------------------------------------------------------------

def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


_INTENT = {"kind": "external_data", "subject": "NYC weather", "cadence_minutes": 15, "wants": [],
           "threshold": None, "display_hint": "value"}


def _picked(store, lib, monkeypatch, request="NYC weather", source="open-meteo-forecast", params=None) -> str:
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    item_id = ni_flow.create_shell_item(store, request)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(request, "source"))
    ni_flow.seal_library_pick(store, item_id, WEATHER_URL,
                              {"source_id": source, "url": WEATHER_URL, "format": "json", "params": params or {}})
    return item_id


def test_a_tap_seals_the_library_source_for_that_url(lib, monkeypatch) -> None:
    store = _store()
    item_id = _picked(store, lib, monkeypatch, params={"lat": "40.71", "key": "x"})
    rec = ni_flow._flow_read(store, item_id)
    assert rec["_library_source"] == "open-meteo-forecast" and rec["_library_url"] == WEATHER_URL
    assert rec["_library_params"] == {"lat": "40.71", "key": "x"} and "_format" not in rec


def test_filled_params_ride_the_candidate_rows() -> None:
    urls = library_resolve._expand("https://x.example.org/r?base={base}&k={key}",
                                   {"base": [("USD", "")], "key": [(library_resolve._KEY_MARK, "")]}, {})
    assert urls[0]["params"] == {"base": "USD"}  # never the key slot


def test_the_flow_builds_from_answers_without_a_mapping_call(lib, monkeypatch) -> None:
    store = _store()
    item_id = _picked(store, lib, monkeypatch)
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        assert "Choose the best candidate path" not in prompt, "no model path-picking"
        return '{"serves": false, "gaps": [], "wrong": ["temperature: looks odd"]}'

    out = ni_flow._sample_and_map(store, item_id, "NYC weather", _INTENT, WEATHER_URL, model,
                                  lambda _u: copy.deepcopy(OPEN_METEO))
    assert out["state"] == "ready"
    assert len(prompts) == 1  # the judge read the card once; it never re-picked
    notes = " | ".join(out["notes"])
    assert "built from the Library's declared answers: Temperature, Conditions, High today, Low today" in notes
    assert "verification still doubts: temperature: looks odd" in notes
    spec = store.get_item(item_id)["spec"]
    assert spec["pipeline"][0]["paths"]["conditions"] == "current.weather_code"
    assert spec["source"] == {"type": "http_json", "url": WEATHER_URL}
    preview = store.read_snapshot(item_id, "preview_data")["payload"]
    assert preview["conditions"] == "Partly cloudy"
    journal = " | ".join(e["summary"] for e in store.read_journal(item_id))
    assert "declared answers" in journal


def _mapping_model(prompts: list[str]):
    def model(prompt: str) -> str:
        prompts.append(prompt)
        if "Choose the best candidate path" in prompt:
            return '{"temperature": "current.temperature_2m"}'
        return "{}"
    return model


def test_a_path_missing_from_the_live_response_falls_back_to_mapping(lib, monkeypatch) -> None:
    store = _store()
    item_id = _picked(store, lib, monkeypatch)
    sample = copy.deepcopy(OPEN_METEO)
    del sample["current"]["weather_code"]  # the declared "Conditions" path isn't in this response
    prompts: list[str] = []
    out = ni_flow._sample_and_map(store, item_id, "NYC weather", {**_INTENT, "wants": ["temperature"]},
                                  WEATHER_URL, _mapping_model(prompts), lambda _u: sample)
    assert out["state"] == "ready"
    assert any("Choose the best candidate path" in p for p in prompts)
    assert any("declared answers didn't fit" in n for n in out["notes"])
    assert store.get_item(item_id)["spec"]["pipeline"][0]["paths"] == {"temperature": "current.temperature_2m"}


def test_answers_build_only_for_the_sealed_url(lib, monkeypatch) -> None:
    store = _store()
    item_id = _picked(store, lib, monkeypatch)
    other = WEATHER_URL + "&x=1"  # the user pasted a different address
    assert ni_flow._try_answers_build(store, item_id, "NYC weather", _INTENT, other, OPEN_METEO) is None
    assert ni_flow._try_answers_build(store, item_id, "NYC weather", _INTENT, WEATHER_URL, OPEN_METEO)


def test_a_remap_keeps_the_frozen_spec_path_and_never_rebuilds_from_answers(lib, monkeypatch) -> None:
    store = _store()
    item_id = _picked(store, lib, monkeypatch)
    ni_flow._sample_and_map(store, item_id, "NYC weather", _INTENT, WEATHER_URL, lambda _p: "{}",
                            lambda _u: copy.deepcopy(OPEN_METEO))
    frozen = store.get_item(item_id)["spec"]
    # the card refreshes on its frozen pipeline: same labels, units and code words
    later = copy.deepcopy(OPEN_METEO)
    later["current"].update(temperature_2m=64.0, weather_code=63)
    out = nimod.run_pipeline(frozen["pipeline"], later)
    assert out["temperature"] == 64.0 and out["conditions"] == "Rain"
    # a Fix (remap) re-derives on the card's own frozen source; the answers are never consulted
    monkeypatch.setattr(ni_flow, "_library_answers", lambda _sid: pytest.fail("answers on a remap"))
    record = ni_flow._flow_read(store, item_id)
    record["_remap"] = True
    ni_flow._flow_write(store, item_id, record)
    prompts: list[str] = []
    out = ni_flow._sample_and_map(store, item_id, "NYC weather", {**_INTENT, "wants": ["temperature"]},
                                  WEATHER_URL, _mapping_model(prompts), lambda _u: copy.deepcopy(OPEN_METEO),
                                  remap=True, keep_source=frozen["source"], keep_params={})
    assert out["state"] == "ready" and any("Choose the best candidate path" in p for p in prompts)
    assert store.get_item(item_id)["spec"]["source"] == frozen["source"]


def test_no_library_or_no_answers_changes_nothing(lib, monkeypatch) -> None:
    store = _store()
    item_id = _picked(store, lib, monkeypatch, source="no-answers")
    assert ni_flow._try_answers_build(store, item_id, "NYC weather", _INTENT, WEATHER_URL, OPEN_METEO) is None
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", None)
    assert ni_flow._library_answers("open-meteo-forecast") == []
