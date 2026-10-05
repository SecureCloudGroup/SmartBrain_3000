"""The fill step (§29 stage 2): a Library record's parameters -> a concrete, consentable source URL.

Labeled sets, recorded 2026-09-29 (fixtures/library_resolve_fill/pack.json says how):
  * C13 same-host helper chains: the official NWS forecast fills office/grid from the nws-points helper on
    the same keyless host. 65 US places (cities, ZIPs, small towns, AK/HI/PR) -> a sealed lookup whose
    recorded /points payload yields NWS's own forecast link.
  * C14 station capability: a station fill takes the nearest station that REPORTS the asked measure
    (NDBC latest_obs), keeps the provider's exact-case id, and a marine source refuses an inland place.
  * C4 a state named inside a water body ("Lake Michigan") is not the state.
The matcher reads the pinned pack's own resolver rows for every n-gram of the asks, so it sees exactly what
the pinned pack gives it. No network.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from smartbrain_3000 import library_resolve as lr

_PACK = json.loads((Path(__file__).parent / "fixtures" / "library_resolve_fill" / "pack.json").read_text())
_NWS_TEMPLATE = "https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast"
_POLICY_WATER = {"prefer": ["official"], "match": "geo", "resolvers": ["place", "buoy"], "max_km": 80,
                 "differ_on": [], "max_age": "1h"}
_WTMP = {**_POLICY_WATER, "measure": {"buoy": "WTMP"}}
_SURF = {**_POLICY_WATER, "measure": {"buoy": ["WVHT", "DPD"]}}  # surf is height AND period
# reviewed water bodies (the Library's water_body resolver): a state inside one of these names is no state
_WATER_BODIES = ["Lake Michigan", "Lake Erie", "Lake Superior", "Lake Ontario", "Lake Champlain", "Lake Tahoe",
                 "Great Salt Lake", "Mississippi River", "Missouri River", "Ohio River", "Tennessee River",
                 "Delaware River", "Delaware Bay", "Connecticut River", "Columbia River", "Hudson River",
                 "Chesapeake Bay", "Puget Sound", "Gulf of Mexico"]
_COASTAL = {("South Lake Tahoe", "CA"): False, ("Virginia Beach", "VA"): True}


def _con(*, water: bool = True, measures: bool = True, drop_aliases: tuple[tuple[str, str], ...] = ()):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE library_resolver_entries(id VARCHAR PRIMARY KEY, resolver VARCHAR, kind VARCHAR, "
                "key VARCHAR, name VARCHAR, lat DOUBLE, lon DOUBLE, state VARCHAR, attrs JSON, rank DOUBLE)")
    con.execute("CREATE TABLE library_resolver_aliases(alias VARCHAR, entry_id VARCHAR, partial BOOLEAN)")
    con.execute("CREATE TABLE library_sources(id VARCHAR PRIMARY KEY, record JSON)")
    for eid, res, kind, key, name, lat, lon, st, attrs, rank in _PACK["entries"]:
        attrs = dict(attrs)
        if res == "place" and (name, st) in _COASTAL:
            attrs["coastal"] = _COASTAL[(name, st)]
        if res == "buoy" and not measures:
            attrs.pop("measures", None)
        con.execute("INSERT INTO library_resolver_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (eid, res, kind, key, name, lat, lon, st, json.dumps(attrs), rank))
    for alias, eid, partial in _PACK["aliases"]:
        if (alias, eid) not in drop_aliases:
            con.execute("INSERT INTO library_resolver_aliases VALUES (?,?,?)", (alias, eid, partial))
    if water:
        for i, name in enumerate(_WATER_BODIES):
            con.execute("INSERT INTO library_resolver_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f"water_body:{i}", "water_body", "water", str(i), name, None, None, "", "{}", 0.0))
            con.execute("INSERT INTO library_resolver_aliases VALUES (?,?,?)", (lr.norm(name), f"water_body:{i}", False))
    for sid, rec in _PACK["records"].items():
        con.execute("INSERT INTO library_sources VALUES (?,?)", (sid, json.dumps(rec)))
    return con


@pytest.fixture(scope="module")
def res():
    con = _con()
    yield lr.Resolver(con)
    con.close()


def _entry(con, name: str, state: str) -> dict:
    row = con.execute(f"SELECT {lr._ENTRY_COLS} FROM library_resolver_entries e WHERE e.name = ? AND e.state = ? "
                      "AND e.resolver IN ('place', 'zip') ORDER BY json_extract(e.attrs, '$.pop')::DOUBLE DESC "
                      "NULLS LAST LIMIT 1", [name, state]).fetchone()
    return lr._row(row)


def _fetch(docs: dict, seen: list | None = None):
    def fetch_json(url: str) -> dict:
        if seen is not None:
            seen.append(url)
        return docs[url]
    return fetch_json


# --- C13: same-host helper chains --------------------------------------------------------------------

@pytest.mark.parametrize(("ask", "kind", "name", "state"), [tuple(x) for x in _PACK["nws_labels"]],
                         ids=[x[0] for x in _PACK["nws_labels"]])
def test_nws_forecast_offers_a_sealed_same_host_lookup(res, ask, kind, name, state) -> None:
    urls, why = lr.candidate_urls(_PACK["records"]["nws-forecast"], ask, {}, res)
    assert urls, why
    e = _entry(res._con, name, state)
    points = f"https://api.weather.gov/points/{e['lat']},{e['lon']}"
    [cand] = [u for u in urls if u["lookup"][0]["url"] == points]  # the labeled place is one of the readings
    assert cand["url"] == _NWS_TEMPLATE  # the office/grid stay placeholders until the consented lookup runs
    assert [(s["param"], s["path"]) for s in cand["lookup"]] == [
        ("office", "properties.gridId"), ("grid_x", "properties.gridX"), ("grid_y", "properties.gridY")]
    assert {s["url"] for s in cand["lookup"]} == {points}
    assert (cand["params"]["lat"], cand["params"]["lon"]) == (str(e["lat"]), str(e["lon"]))  # the place is consumed
    doc = _PACK["points"][points]
    # NWS's own forecast link for that point is the independent label
    assert lr.resolve_lookup(cand, _fetch(_PACK["points"])) == doc["properties"]["forecast"]
    assert doc["properties"]["relativeLocation"]["properties"]["state"] == (state or doc["properties"][
        "relativeLocation"]["properties"]["state"])


def test_nws_hourly_and_the_chained_forecast_reads_today_and_tonight(res) -> None:
    urls, _ = lr.candidate_urls(_PACK["records"]["nws-forecast-hourly"], "Tulsa forecast tonight", {}, res)
    [cand] = urls
    assert lr.resolve_lookup(cand, _fetch(_PACK["points"])) == \
        "https://api.weather.gov/gridpoints/TSA/44,103/forecast/hourly"
    for ask in ("Tulsa forecast tonight", "Anchorage forecast", "San Juan PR forecast", "10001 forecast"):
        [cand] = lr.candidate_urls(_PACK["records"]["nws-forecast"], ask, {}, res)[0]
        final = lr.resolve_lookup(cand, _fetch(_PACK["points"]))
        names = [p["name"] for p in _PACK["forecast"][final]["properties"]["periods"]]
        assert names == ["Today", "Tonight"], (ask, names)


def test_an_ambiguous_place_is_one_lookup_per_reading(res) -> None:
    urls, _ = lr.candidate_urls(_PACK["records"]["nws-forecast"], "Des Moines weather", {}, res)
    assert [u["label"] for u in urls] == ["Des Moines (IA)", "Des Moines (WA)", "Des Moines (NM)"]
    assert all(u["choice"] for u in urls)
    finals = [lr.resolve_lookup(u, _fetch(_PACK["points"])) for u in urls]
    assert finals == ["https://api.weather.gov/gridpoints/DMX/74,48/forecast",
                      "https://api.weather.gov/gridpoints/SEW/123,58/forecast",
                      "https://api.weather.gov/gridpoints/ABQ/205,188/forecast"]


def test_resolve_lookup_fetches_each_helper_once_and_refuses_a_missing_path(res) -> None:
    [cand] = lr.candidate_urls(_PACK["records"]["nws-forecast"], "Tulsa forecast tonight", {}, res)[0]
    seen: list = []
    lr.resolve_lookup(cand, _fetch(_PACK["points"], seen))
    assert seen == ["https://api.weather.gov/points/36.127949,-95.902316"]
    for doc in ({"properties": {"gridId": "TSA", "gridX": 44}},                      # gridY missing
                {"properties": {"gridId": None, "gridX": 44, "gridY": 103}},         # a null is not a value
                {"properties": {"gridId": {"x": 1}, "gridX": 44, "gridY": 103}},     # nor an object
                {"type": "Feature"}):
        with pytest.raises(ValueError):
            lr.resolve_lookup(cand, lambda url, doc=doc: doc)
    moved = {**cand, "lookup": [{**s, "url": "https://evil.example.org/points/1,2"} for s in cand["lookup"]]}
    with pytest.raises(ValueError):  # a lookup never leaves the source's own host
        lr.resolve_lookup(moved, _fetch({"https://evil.example.org/points/1,2": _PACK["points"][cand["lookup"][0]["url"]]}))


def _chained(helper_access: dict, helper_id: str = "helper-x", template: str = "https://api.example.org/f/{g}"):
    helper = {"id": helper_id, "name": "Helper", "role": "helper", "kinds": ["lookup"],
              "access": {"kind": "http_json", "auth": "none", "headers": {}, **helper_access}}
    rec = {"id": "rec-x", "name": "Forecast", "kinds": ["forecast"],
           "access": {"kind": "http_json", "auth": "none", "headers": {}, "url_template": template,
                      "params": [{"name": "g", "fill": {"from": "source", "source": helper_id, "path": "p.g"}}]}}
    return rec, helper


_PLACE_PARAMS = [{"name": "lat", "fill": {"from": "resolver", "resolver": "place", "field": "lat"}},
                 {"name": "lon", "fill": {"from": "resolver", "resolver": "place", "field": "lon"}}]


@pytest.mark.parametrize(("case", "access", "template"), [
    ("cross-host", {"url_template": "https://other.example.net/p/{lat},{lon}", "params": _PLACE_PARAMS},
     "https://api.example.org/f/{g}"),
    ("keyed", {"url_template": "https://api.example.org/p/{lat},{lon}?key={key}",
               "params": [*_PLACE_PARAMS, {"name": "key", "fill": {"from": "vault_key"}}]},
     "https://api.example.org/f/{g}"),
    ("contact", {"url_template": "https://api.example.org/p/{lat},{lon}", "params": _PLACE_PARAMS,
                 "contact_ua": True}, "https://api.example.org/f/{g}"),
    ("chain-of-chains", {"url_template": "https://api.example.org/p/{z}",
                         "params": [{"name": "z", "fill": {"from": "source", "source": "helper-y", "path": "a"}}]},
     "https://api.example.org/f/{g}"),
    ("host-parameter", {"url_template": "https://{h}/p", "params": [
        {"name": "h", "fill": {"from": "resolver", "resolver": "statuspage", "field": "key"}}]},
     "https://{h}/f/{g}"),
])
def test_a_helper_off_host_keyed_or_itself_chained_is_refused(case, access, template) -> None:
    rec, helper = _chained(access, template=template)
    con = _con()
    con.execute("INSERT INTO library_sources VALUES (?,?)", (helper["id"], json.dumps(helper)))
    urls, why = lr.candidate_urls(rec, "Tulsa forecast tonight", {}, lr.Resolver(con))
    assert urls == [] and "another lookup before your consent" in why, (case, urls, why)


def test_a_same_host_helper_chain_works_for_any_record_and_an_unknown_helper_is_refused() -> None:
    rec, helper = _chained({"url_template": "https://api.example.org/p/{lat},{lon}", "params": _PLACE_PARAMS})
    con = _con()
    r = lr.Resolver(con)
    assert "another lookup" in lr.candidate_urls(rec, "Tulsa forecast", {}, r)[1]  # not in the pack yet
    con.execute("INSERT INTO library_sources VALUES (?,?)", (helper["id"], json.dumps(helper)))
    [cand] = lr.candidate_urls(rec, "Tulsa forecast", {}, r)[0]
    assert cand["url"] == "https://api.example.org/f/{g}"
    assert cand["lookup"] == [{"url": "https://api.example.org/p/36.127949,-95.902316", "path": "p.g", "param": "g"}]
    # FETCH-F5 (2026-10-04): a helper's value is a bounded safe id — the quote step still runs,
    # but the untrusted JSON can't rewrite path segments or deliver a surprise URL.
    assert lr.resolve_lookup(cand, lambda url: {"p": {"g": "ABC-1,2"}}) == "https://api.example.org/f/ABC-1,2"
    with pytest.raises(ValueError):
        lr.resolve_lookup(cand, lambda url: {"p": {"g": "a b/c"}})
    # the helper's own refusal is the honest reason when the ask names no place
    assert lr.candidate_urls(rec, "forecast tonight", {}, r) == ([], "the ask doesn't name a place")


# --- C14: a station fill takes a station that reports the asked measure ---------------------------

def _station_rows():
    rows = []
    for lab in _PACK["station_labels"]:
        wtmp = lab["measure"] == ["WTMP"]
        ask = f"water temperature {lab['phrase']}" if wtmp else f"wave height {lab['phrase']}"
        rows.append(pytest.param(ask, _WTMP if wtmp else _SURF, lab["expect"], id=ask))
    return rows


@pytest.mark.parametrize(("ask", "policy", "expect"), _station_rows())
def test_station_fill_takes_the_nearest_station_that_reports_the_measure(res, ask, policy, expect) -> None:
    urls, why = lr.candidate_urls(_PACK["records"]["ndbc-buoy-realtime"], ask, policy, res)
    if expect is None:
        assert urls == [] and why, urls
        return
    assert [u["params"]["buoy"] for u in urls] == [expect], why
    assert urls[0]["url"] == f"https://www.ndbc.noaa.gov/data/realtime2/{expect}.txt"  # the provider's exact case


def test_station_seeds_milwaukee_water_temp_and_virginia_beach_surf(res) -> None:
    ndbc = _PACK["records"]["ndbc-buoy-realtime"]
    assert lr.candidate_urls(ndbc, "Milwaukee water temperature", _WTMP, res)[0][0]["params"] == {"buoy": "45013"}
    assert [u["params"] for u in lr.candidate_urls(ndbc, "how's the surf at Virginia Beach", _SURF, res)[0]] == \
        [{"buoy": "44099"}]
    urls, why = lr.candidate_urls(ndbc, "wave height Galveston", _SURF, res)
    assert urls == [] and "WVHT" in why and "80 km" in why  # honest: nothing near reports it


def test_the_lake_michigan_ask_reaches_the_lake_buoy_once_the_state_and_alias_are_fixed() -> None:
    # the regenerated pack (Library WP6) has no 'lake michigan' alias on Lake City MI; with a water_body
    # resolver, 'Michigan' inside 'Lake Michigan' no longer reads as the state, so Milwaukee WI stands
    bad = tuple((a, eid) for a, eid, _ in _PACK["aliases"] if a == "lake michigan")
    con = _con(drop_aliases=bad)
    urls, why = lr.candidate_urls(_PACK["records"]["ndbc-buoy-realtime"], "Lake Michigan water temp Milwaukee",
                                  _WTMP, lr.Resolver(con))
    assert [u["params"]["buoy"] for u in urls] == ["45013"], why


def test_without_measures_data_the_station_fill_behaves_as_before_and_keeps_the_ids_case() -> None:
    con = _con(measures=False)
    ndbc = _PACK["records"]["ndbc-buoy-realtime"]
    urls, _ = lr.candidate_urls(ndbc, "Milwaukee water temperature", _WTMP, lr.Resolver(con))
    assert [u["url"] for u in urls] == ["https://www.ndbc.noaa.gov/data/realtime2/MLWW3.txt"]


def test_a_marine_source_refuses_an_inland_place(res) -> None:
    marine = {**_PACK["records"]["open-meteo-marine"], "coverage": {"geo": "ocean/coastal", "entity": ""}}
    urls, why = lr.candidate_urls(marine, "water temperature South Lake Tahoe", _WTMP, res)
    assert urls == [] and "coast" in why
    assert lr.candidate_urls(marine, "waves at Virginia Beach", _SURF, res)[0]       # declared coastal
    assert lr.candidate_urls(marine, "waves at Santa Cruz CA", _SURF, res)[0]        # nothing declared: as today
    plain = _PACK["records"]["open-meteo-marine"]                                     # no marine coverage: as today
    assert lr.candidate_urls(plain, "water temperature South Lake Tahoe", _WTMP, res)[0]


def test_the_librarys_closed_water_key_marks_a_marine_source(res) -> None:
    """The Library declares marine coverage as `coverage.water: "ocean_coastal"` (geo stays "global"); the
    app read only geo wording, so an inland place was still offered ocean data (review D12)."""
    marine = {**_PACK["records"]["open-meteo-marine"], "coverage": {"geo": "global", "water": "ocean_coastal"}}
    urls, why = lr.candidate_urls(marine, "water temperature South Lake Tahoe", _WTMP, res)
    assert urls == [] and "coast" in why
    assert lr.candidate_urls(marine, "waves at Virginia Beach", _SURF, res)[0]


# --- C4: a state inside a water body's name is not the state ------------------------------------------

@pytest.mark.parametrize(("ask", "states"), [tuple(x) for x in _PACK["water_labels"]],
                         ids=[x[0] for x in _PACK["water_labels"]])
def test_states_in_ignores_a_state_inside_a_water_body(res, ask, states) -> None:
    assert lr.states_in(ask, res) == set(states)


def test_states_in_without_a_water_body_resolver_is_unchanged() -> None:
    con = _con(water=False)
    assert lr.states_in("Lake Michigan water temp Milwaukee", lr.Resolver(con)) == {"MI"}
    assert lr.states_in("Lake Michigan water temp Milwaukee") == {"MI"}


# --- fix13-state (2026-10-05): a US state name eclipses its same-named small towns --------------------

def _state_pack_con():
    """A tiny pack with California towns (all small), Washington DC (big) + Washington UT (small),
    New York (big), Kansas City (big), and the matching us_state entries. Enough to exercise the
    eclipse rule across the hard cases."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE library_resolver_entries(id VARCHAR PRIMARY KEY, resolver VARCHAR, kind VARCHAR, "
                "key VARCHAR, name VARCHAR, lat DOUBLE, lon DOUBLE, state VARCHAR, attrs JSON, rank DOUBLE)")
    con.execute("CREATE TABLE library_resolver_aliases(alias VARCHAR, entry_id VARCHAR, partial BOOLEAN)")
    rows = [
        ("place:ca-pa", "place", "place", "p1", "California", 40.0, -79.9, "PA", {"pop": 4628}, 3.67, ["california"]),
        ("place:ca-mo", "place", "place", "p2", "California", 38.6, -92.5, "MO", {"pop": 4522}, 3.66, ["california"]),
        ("place:ca-ky", "place", "place", "p3", "California", 38.9, -84.2, "KY", {"pop": 85}, 1.93, ["california"]),
        ("place:fl-ny", "place", "place", "p4", "Florida", 41.3, -74.3, "NY", {"pop": 2868}, 3.0, ["florida"]),
        ("place:wa-dc", "place", "place", "p5", "Washington", 38.9, -77.0, "DC", {"pop": 702250}, 5.0,
         ["washington", "washington dc", "dc"]),
        ("place:wa-ut", "place", "place", "p6", "Washington", 37.1, -113.5, "UT", {"pop": 35501}, 4.0, ["washington"]),
        ("place:ny", "place", "place", "p7", "New York", 40.7, -74.0, "NY", {"pop": 8478072}, 6.0,
         ["new york", "new york city", "nyc"]),
        ("place:kc-mo", "place", "place", "p8", "Kansas City", 39.1, -94.6, "MO", {"pop": 516000}, 5.0,
         ["kansas city"]),
        ("place:kc-ks", "place", "place", "p9", "Kansas City", 39.1, -94.6, "KS", {"pop": 156000}, 5.0,
         ["kansas city"]),
        ("us_state:CA", "us_state", "state", "CA", "California", None, None, "CA", {}, 0.0,
         ["california", "ca"]),
        ("us_state:FL", "us_state", "state", "FL", "Florida", None, None, "FL", {}, 0.0, ["florida", "fl"]),
        ("us_state:WA", "us_state", "state", "WA", "Washington", None, None, "WA", {}, 0.0,
         ["washington", "wa"]),
        ("us_state:KS", "us_state", "state", "KS", "Kansas", None, None, "KS", {}, 0.0, ["kansas", "ks"]),
        ("us_state:NY", "us_state", "state", "NY", "New York", None, None, "NY", {}, 0.0, ["new york", "ny"]),
        ("us_state:PA", "us_state", "state", "PA", "Pennsylvania", None, None, "PA", {}, 0.0,
         ["pennsylvania", "pa"]),
    ]
    for eid, resv, kind, key, name, lat, lon, st, attrs, rank, aliases in rows:
        con.execute("INSERT INTO library_resolver_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (eid, resv, kind, key, name, lat, lon, st, json.dumps(attrs), rank))
        for a in aliases:
            con.execute("INSERT INTO library_resolver_aliases VALUES (?,?,?)", (a, eid, False))
    return con


@pytest.mark.parametrize(("ask", "expect"), [
    ("earthquakes in california last 24 hrs", "none"),    # all California towns are small -> eclipse
    ("earthquakes in florida", "none"),                   # all Florida towns are small -> eclipse
    ("weather in Washington", "ambiguous"),               # Washington DC is big -> keep (unchanged)
    ("weather in Washington state", "none"),              # "{state} state" cue -> eclipse DC + UT
    ("weather in Kansas City", "ambiguous"),              # states_in excludes "Kansas City" -> keep
    ("weather in New York", "resolved"),                  # big same-named city -> keep (unchanged)
    ("weather in California PA", "resolved"),             # two states named -> keep (California PA town)
    ("weather in California, PA", "resolved"),            # same, with punctuation
    ("earthquakes in CA", "none"),                        # bare code; no "california" word to eclipse
])
def test_state_eclipses_place_rule(ask, expect) -> None:
    """fix13-state: an ask whose place-words are exactly a US state name reads as the state (so
    sources that take ``us_state`` lead); a big same-named city or a second named state keeps the
    town reading. ``_state_eclipses_place`` returns status ``none`` when the eclipse fires."""
    r = lr.Resolver(_state_pack_con())
    pl = r.by_name("zip", ask) if any(c.isdigit() for c in ask) else {"status": "none"}
    pl = pl if pl["status"] == "resolved" else r.by_name("place", ask)
    assert lr._state_eclipses_place(ask, pl, r)["status"] == expect, ask


def test_state_eclipses_place_leaves_a_non_same_named_place() -> None:
    """A reading whose name differs from the state name is the ask's place; the eclipse must not drop
    it ("earthquakes in Alaska Fairbanks" → Fairbanks)."""
    r = lr.Resolver(_state_pack_con())
    pl = {"status": "resolved", "best": {"name": "Fairbanks", "attrs": {}}, "candidates": [
        {"name": "Fairbanks", "attrs": {}}]}
    assert lr._state_eclipses_place("earthquakes in Alaska Fairbanks", pl, r)["status"] == "resolved"
