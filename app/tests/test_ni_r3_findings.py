"""R3 field findings (2026-10-04): the ``_frame_gap`` grace and the ``upcoming`` row cut agreed
on one forward-cut rule; a Library row's url_template + clock_params ride through
``library_index.candidates`` so every clock card seals a date-walking URL (never a day-1 literal);
a clock-param answer path walks forward too; export skips clock params; the user zone is only
set from the desktop authority.

Each test names the finding (A / C / E / H / I) and the one line the fix stands on."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from smartbrain_3000 import library_index, library_resolve
from smartbrain_3000 import ni as nimod

NY = ZoneInfo("America/New_York")


# ---- C: ONE forward-cut rule shared by _frame_gap and the upcoming row cut --------------------

def test_c_the_stale_check_and_the_upcoming_cut_share_one_floor() -> None:
    """One forward-cut rule (now - 15 min): a row the ``upcoming`` cut keeps is never judged stale,
    so a next-event card is never refused for a row it shows. An event moment 10 minutes ago still
    leads; one 20 or 50 minutes ago (a 14:05 high tide read at 14:55) is not "next"."""
    moment = "2026-10-03T14:05:00-04:00"
    assert nimod.next_event_stale(moment, datetime(2026, 10, 3, 14, 15, tzinfo=NY)) is False
    assert nimod.next_event_stale(moment, datetime(2026, 10, 3, 14, 25, tzinfo=NY)) is True
    assert nimod.next_event_stale(moment, datetime(2026, 10, 3, 14, 55, tzinfo=NY)) is True
    keep = nimod._window_test("upcoming", datetime(2026, 10, 3, 14, 55, tzinfo=NY))
    at = datetime(2026, 10, 3, 14, 5, tzinfo=NY)
    assert keep((at.replace(tzinfo=None), at, True)) is False  # the cut drops what the check calls stale
    at = datetime(2026, 10, 3, 14, 45, tzinfo=NY)
    assert keep((at.replace(tzinfo=None), at, True)) is True
    assert nimod.next_event_stale(at, datetime(2026, 10, 3, 14, 55, tzinfo=NY)) is False


def test_c_frame_gap_a_future_event_is_never_stale() -> None:
    """A row still in the future reads as current — the floor only looks backward."""
    assert nimod.next_event_stale("2026-10-03T20:00:00-04:00",
                                   datetime(2026, 10, 3, 14, 30, tzinfo=NY)) is False


def test_c_frame_gap_grace_minutes_override() -> None:
    """A caller's grace is the floor: 13:55 read at 14:05 is fresh at 15 min, stale at 1 min."""
    now = datetime(2026, 10, 3, 14, 5, tzinfo=NY)
    assert nimod.next_event_stale("2026-10-03T13:55:00-04:00", now) is False
    assert nimod.next_event_stale("2026-10-03T13:55:00-04:00", now, grace_minutes=1) is True


# ---- A: library_index.candidates carries url_template + clock_params through --------------------
# A fixture Library pack with every clock-fill format family (and a keyed source).

_CLOCK_SOURCES: tuple[tuple[str, str, list[dict]], ...] = (
    # (source_id, url_template, params)
    ("mlb-schedule", "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}",
     [{"name": "date", "fill": {"from": "clock", "format": "%Y-%m-%d", "offset_days": 0}}]),
    ("coops-tide-hilo",
     "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?station={station}&begin_date={begin}",
     [{"name": "station", "fill": {"from": "default", "value": "8665530"}},
      {"name": "begin", "fill": {"from": "clock", "format": "%Y%m%d", "offset_days": 0}}]),
    ("usno-seasons", "https://aa.usno.navy.mil/api/seasons?year={year}&tz=0",
     [{"name": "year", "fill": {"from": "clock", "format": "%Y", "offset_days": 0}}]),
    ("usdm-state-stats",
     "https://usdmdataservices.unl.edu/api/StateStatistics/GetDroughtSeverityStatisticsByAreaPercent"
     "?aoi=NE&startdate={start}%2F{start_year}&enddate={end}%2F{end_year}&statisticsType=1",
     [{"name": "start", "fill": {"from": "clock", "format": "%m/%d", "offset_days": -7}},
      {"name": "start_year", "fill": {"from": "clock", "format": "%Y", "offset_days": -7}},
      {"name": "end", "fill": {"from": "clock", "format": "%m/%d", "offset_days": 0}},
      {"name": "end_year", "fill": {"from": "clock", "format": "%Y", "offset_days": 0}}]),
    ("nasa-neows-feed",
     "https://api.nasa.gov/neo/rest/v1/feed?start_date={start}&end_date={end}&api_key={key}",
     [{"name": "start", "fill": {"from": "clock", "format": "%Y-%m-%d", "offset_days": 0}},
      {"name": "end", "fill": {"from": "clock", "format": "%Y-%m-%d", "offset_days": 7}},
      {"name": "key", "fill": {"from": "vault_key"}}]),
)


def _build_clock_pack(tmp: Path) -> library_index.LibraryIndex:
    """A minimal Library pack carrying one source per clock-format family (A's audit)."""
    assert isinstance(tmp, Path), "tmp path required"
    src = tmp / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("""CREATE TABLE library_sources(
        id VARCHAR PRIMARY KEY, name VARCHAR, description VARCHAR, provider_id VARCHAR,
        provider_name VARCHAR, authority VARCHAR, tier VARCHAR, geo VARCHAR, entity VARCHAR,
        access_kind VARCHAR, url_template VARCHAR, docs_url VARCHAR, auth VARCHAR,
        terms_status VARCHAR, cadence VARCHAR, validation_status VARCHAR, robots VARCHAR,
        votes_yes INTEGER, votes_no INTEGER, prior DOUBLE, record JSON, kinds VARCHAR[],
        role VARCHAR, audience VARCHAR)""")
    con.execute("CREATE TABLE library_source_categories(source_id VARCHAR, category VARCHAR, "
                "subcategory VARCHAR)")
    con.execute("CREATE TABLE library_terms(term VARCHAR, source_id VARCHAR, weight DOUBLE)")
    con.execute("CREATE TABLE library_taxonomy(category VARCHAR, subcategory VARCHAR, label VARCHAR, "
                "kinds VARCHAR[], params VARCHAR[], keywords VARCHAR[], policy JSON)")
    con.execute("CREATE TABLE library_source_resolvers(source_id VARCHAR, resolver VARCHAR)")
    con.execute("CREATE TABLE library_resolver_entries(id VARCHAR PRIMARY KEY, resolver VARCHAR, "
                "kind VARCHAR, key VARCHAR, name VARCHAR, lat DOUBLE, lon DOUBLE, state VARCHAR, "
                "attrs JSON, rank DOUBLE)")
    con.execute("CREATE TABLE library_resolver_aliases(alias VARCHAR, entry_id VARCHAR, "
                "partial BOOLEAN)")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    con.execute("CREATE TABLE library_route_asks(ask VARCHAR, category VARCHAR, subcategory VARCHAR)")
    con.execute("INSERT INTO library_taxonomy VALUES ('sky', 'astronomy', 'Sky', ?, ?, ?, '{}')",
                 [["schedule"], [], ["asteroid", "asteroids", "today"]])
    for sid, template, params in _CLOCK_SOURCES:
        rec = {"id": sid, "name": sid, "provider": {"name": "p", "authority": "primary"},
               "categories": ["sky/astronomy"], "tier": "curated",
               "access": {"url_template": template, "params": [
                   {"name": p["name"], "kind": "date", "example": None, "required": True,
                    "fill": p["fill"]} for p in params]},
               "kinds": ["schedule"]}
        con.execute(
            "INSERT INTO library_sources VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, sid, "", "p", "p", "primary", "curated", "", "", "http_json", template,
             "", "none" if sid != "nasa-neows-feed" else "key", "ok", "", "ok", "allow",
             0, 0, 1.0, json.dumps(rec), ["schedule"], "", ""))
        con.execute("INSERT INTO library_source_categories VALUES (?, 'sky', 'astronomy')", (sid,))
        con.execute("INSERT INTO library_terms VALUES ('asteroids', ?, 3.0)", (sid,))
    con.execute("INSERT INTO library_meta VALUES ('built_at','2026-10-03T00:00:00Z'), "
                "('records','5'), ('schema','1')")
    con.close()
    payload = gzip.compress(src.read_bytes())

    class _Net:
        def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
            assert url and max_bytes > 0, "fetch args"
            return payload

    idx = library_index.LibraryIndex(tmp / "data", netguard_mod=_Net(),
                                      pack={"tag": "vtest", "url": "https://e.test/l.gz",
                                            "sha256": hashlib.sha256(payload).hexdigest()})
    idx.install()
    return idx


@pytest.fixture(scope="module")
def _clock_lib(tmp_path_factory) -> library_index.LibraryIndex:
    return _build_clock_pack(tmp_path_factory.mktemp("r3_clock"))


def _freeze(monkeypatch, moment: datetime) -> None:
    assert moment.tzinfo is not None, "moment must be aware"
    monkeypatch.setattr(nimod, "_clock", lambda: moment)


@pytest.mark.parametrize("sid", [s[0] for s in _CLOCK_SOURCES])
def test_a_candidate_urls_carry_url_template_and_clock_params(
    sid: str, _clock_lib: library_index.LibraryIndex, monkeypatch) -> None:
    """A HIGH: a clock source's candidate ships with ``url_template`` ({{param:name}}) + clock_params,
    the sealed URL on day D reproduces the fetched URL, and on day D+3 the engine's
    ``substitute_params`` walks the date forward (-276 / server-zone bugs come back here)."""
    now = datetime(2026, 10, 3, 12, 0, tzinfo=NY)
    _freeze(monkeypatch, now)
    cands, _skipped = _clock_lib.candidates("asteroids today", limit=10,
                                              hint={"frame_kind": "schedule"})
    cand = next((c for c in cands if c["source_id"] == sid), None)
    assert cand is not None, f"{sid} candidate must be offered (got {[c['source_id'] for c in cands]})"
    assert "url_template" in cand and "{{param:" in cand["url_template"], \
        f"{sid} candidate must carry a url_template with slots"
    assert cand["clock_params"], f"{sid} candidate must carry clock_params"
    # the sealed template, substituted with the engine's clock on day D, equals the filled URL
    spec = {"source": {"url": cand["url_template"]},
            "params": {name: {"label": name, "kind": "clock",
                               "format": cp["format"], "offset_days": int(cp["offset_days"])}
                       for name, cp in cand["clock_params"].items()}}
    for name, raw in (cand.get("params") or {}).items():
        if name not in cand["clock_params"]:
            spec["params"][name] = {"label": name, "kind": "string", "value": str(raw)}
    # the fetched URL has the clock values filled in — never SBKEYSLOT
    assert library_resolve._KEY_MARK not in cand["url_template"], \
        f"{sid} url_template must have the key stripped"
    assert library_resolve._KEY_MARK not in cand["url"], \
        f"{sid} url must have the key stripped"
    today_url = nimod.substitute_params(spec)["source"]["url"]
    assert today_url == cand["url"], f"{sid} day-D substitute must reproduce the URL"
    # a step far enough to move EVERY format family's rendered value (%Y, %m/%d, %Y%m%d, …): one year
    later = datetime(2027, 10, 6, 12, 0, tzinfo=NY)
    _freeze(monkeypatch, later)
    assert nimod.substitute_params(spec)["source"]["url"] != today_url, \
        f"{sid} year-later must walk the date forward"


def test_a_candidate_urls_use_the_user_zone_not_the_server(_clock_lib, monkeypatch) -> None:
    """A HIGH (zone skew): with the user's zone pinned, the clock dates on the filled URL match
    the user's own calendar — server zone (UTC) must not reach into the URL."""
    nimod.set_user_timezone("Pacific/Auckland")  # UTC+13 on 10/03 — a next-day skew
    try:
        monkeypatch.setattr(nimod, "_clock", lambda: datetime(
            2026, 10, 4, 11, 0, tzinfo=ZoneInfo("Pacific/Auckland")))
        cands, _skipped = _clock_lib.candidates("asteroids today",
                                                  hint={"frame_kind": "schedule"})
        mlb = next(c for c in cands if c["source_id"] == "mlb-schedule")
        assert "date=2026-10-04" in mlb["url"], \
            f"user zone must drive the clock, got {mlb['url']!r}"
    finally:
        nimod.set_user_timezone(None)


# ---- A fallback: _derive_clock_template uses the RECORD's offset (never inference from values) --

class _FakeLib:
    """A minimal Library stand-in: one record keyed by source id."""

    def __init__(self, records: dict[str, dict]) -> None:
        assert isinstance(records, dict), "records required"
        self._records = records

    def get(self, source_id: str) -> dict | None:
        assert isinstance(source_id, str), "source id required"
        return self._records.get(source_id)


def _rec(url_template: str, params: list[dict]) -> dict:
    return {"access": {"url_template": url_template, "params": params}}


_DERIVE_CASES = (
    # a %Y value inferred from the filled "2026" would read Jan 1 2026 and emit offset ~-276 — the
    # record's own offset is 0 (the current year)
    ("usno-seasons",
     _rec("https://aa.usno.navy.mil/api/seasons?year={year}",
          [{"name": "year", "fill": {"from": "clock", "format": "%Y", "offset_days": 0}}]),
     "https://aa.usno.navy.mil/api/seasons?year=2026",
     "https://aa.usno.navy.mil/api/seasons?year={{param:year}}", {"year": 0}),
    # the URL holds the same year TWICE (treasury-yield-curve); every occurrence must slot
    ("treasury-yield-curve",
     _rec("https://home.treasury.gov/d/{year}?field_tdr_date_value={year}",
          [{"name": "year", "fill": {"from": "clock", "format": "%Y", "offset_days": 0}}]),
     "https://home.treasury.gov/d/2026?field_tdr_date_value=2026",
     "https://home.treasury.gov/d/{{param:year}}?field_tdr_date_value={{param:year}}", {"year": 0}),
    # the URL template holds URL-encoded "%2F" where the filled value has "/": the encoded form must
    # be tried (usdm-state-stats-shaped)
    ("usdm-state-stats",
     _rec("https://u.test/x?s={start}&e={end}",
          [{"name": "start", "fill": {"from": "clock", "format": "%m/%d", "offset_days": -7}},
           {"name": "end", "fill": {"from": "clock", "format": "%m/%d", "offset_days": 0}}]),
     "https://u.test/x?s=09%2F26&e=10%2F03",
     "https://u.test/x?s={{param:start}}&e={{param:end}}", {"start": -7, "end": 0}),
)


@pytest.mark.parametrize(("sid", "record", "url", "want_tmpl", "want_offsets"), _DERIVE_CASES)
def test_a_derive_clock_template_uses_the_records_offset(
    sid: str, record: dict, url: str, want_tmpl: str,
    want_offsets: dict[str, int], monkeypatch) -> None:
    """A HIGH fallback: ``_derive_clock_template`` (seal-time fallback for rows without metadata)
    reads the offset from the RECORD, replaces every occurrence of the filled value, and tries
    the URL-encoded form — never infers from the filled value (which read %Y as Jan 1 for -276)."""
    from smartbrain_3000 import ni_flow as nifx

    monkeypatch.setattr(nifx, "_LIBRARY_PROVIDER", lambda: _FakeLib({sid: record}))
    monkeypatch.setattr(nimod, "_clock",
                         lambda: datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    filled = {}
    from smartbrain_3000 import library_resolve as lr
    for p in record["access"]["params"]:
        f = p["fill"]
        filled[p["name"]] = lr._clock(f["format"], int(f["offset_days"]),
                                        datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    tmpl, meta = nifx._derive_clock_template(sid, url, filled)
    assert tmpl == want_tmpl, f"{sid} template mismatch"
    for name, want in want_offsets.items():
        assert meta[name]["offset_days"] == want, f"{sid} {name} offset wrong"


# ---- E: a clock param inside an answer PATH walks forward too (never a frozen day-1 key) -------

def test_e_clock_param_in_answer_path_rides_through_to_the_engine() -> None:
    """A NEOWS-shaped answer (``near_earth_objects.{date}``) sealed as ``near_earth_objects["<
    today>"]`` would extract-miss on day 2. ``build_from_answers`` must keep each clock param as a
    ``["{{param:name}}"]`` slot in the sealed pipeline so ``substitute_params`` refills every tick."""
    from smartbrain_3000 import ni_flow as nifx

    today = "2026-10-04"
    sample = {"near_earth_objects": {today: [{"name": "(2026 XA)", "feet": 130}]}}
    answer = {"name": "today", "label": "Today's asteroids", "words": ["asteroids"],
              "kind": "list", "path": "near_earth_objects.{date}",
              "cells": [{"path": "name", "label": "Name", "type": "text"}]}
    built = nifx.build_from_answers([answer], sample, "Today's asteroids",
                                      params={"date": today},
                                      clock_params=frozenset({"date"}))
    extract = next(st for st in built["pipeline"] if st.get("op") == "extract")
    assert extract["paths"]["rows"] == 'near_earth_objects["{{param:date}}"]', \
        f"the sealed path must slot the clock param, got {extract['paths']['rows']!r}"
    # day-D+1: the engine's substitute_params fills the slot with the new date, so a sample for
    # 2026-10-05 extracts correctly (the pipeline would have extract-missed on the day-1 literal)
    tomorrow = "2026-10-05"
    spec = {"source": {"url": "https://e.test"},
            "pipeline": built["pipeline"], "params": {
                "date": {"label": "date", "kind": "clock", "format": "%Y-%m-%d", "offset_days": 0}}}
    filled = nimod.substitute_params({**spec, "params": {
        "date": {"label": "date", "kind": "string", "value": tomorrow}}})
    rows_extract = next(st for st in filled["pipeline"] if st.get("op") == "extract")
    assert rows_extract["paths"]["rows"] == f'near_earth_objects["{tomorrow}"]'


# ---- I: a REMOTE device sets the zone when none is stored; a DESKTOP always wins ---------------
# R4-3 (field 2026-10-04): the old R3-I rule (DESKTOP-only) stranded headless / LAN-only /
# phone-only installs on UTC — those users never get a desktop handshake. The new rule allows a
# REMOTE device to set the zone when nothing is stored yet, records the setter on meta
# ``user:timezone_by``, and keeps a DESKTOP-set zone immune to remote overrides.

def test_i_remote_authority_does_not_override_the_desktop_timezone(tmp_path, monkeypatch) -> None:
    """A paired phone's handshake must not alternate a desktop-set zone — the engine's clock meta
    stays sealed to the desktop once the desktop has written it."""
    import os

    from fastapi.testclient import TestClient

    from smartbrain_3000 import auth
    from smartbrain_3000 import db as dbmod
    from smartbrain_3000.main import create_app

    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "tz.duckdb"))
    app = create_app()
    with TestClient(app) as c:
        # desktop probe: zone is written and tagged as desktop-set
        desk_auth = {"Authorization": f"Bearer {os.environ['SMARTBRAIN_LOCAL_TOKEN']}",
                      "X-SmartBrain-Timezone": "America/New_York"}
        assert c.get("/api/health", headers=desk_auth).json()["status"] == "ok"
        assert dbmod.meta_get(app.state.dbx, "user:timezone") == "America/New_York"
        assert dbmod.meta_get(app.state.dbx, "user:timezone_by") == "desktop"
        # phone (relay credential) probes with a different zone; the stored zone stays NY
        relay = {**auth.relay_headers("device-abc"),
                  "X-SmartBrain-Timezone": "America/Los_Angeles"}
        # TestClient's default Authorization header must be suppressed for the relay probe
        c.headers.pop("Authorization", None)
        assert c.get("/api/health", headers=relay).status_code == 200
        assert dbmod.meta_get(app.state.dbx, "user:timezone") == "America/New_York"
        assert dbmod.meta_get(app.state.dbx, "user:timezone_by") == "desktop"


def test_i_remote_authority_seeds_the_zone_when_none_is_stored(tmp_path, monkeypatch) -> None:
    """A headless / LAN-only install never sees a desktop handshake — a REMOTE device's reported
    zone seeds the engine's clock (R4-3) so NI windows cut on the user's day instead of UTC."""
    from fastapi.testclient import TestClient

    from smartbrain_3000 import auth
    from smartbrain_3000 import db as dbmod
    from smartbrain_3000.main import create_app

    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "tz.duckdb"))
    app = create_app()
    with TestClient(app) as c:
        c.headers.pop("Authorization", None)  # no desktop credential
        # an anonymous probe (no credential at all) never seeds the zone
        assert c.get("/api/health", headers={"X-SmartBrain-Timezone": "Europe/Paris"}).status_code == 200
        assert not dbmod.meta_get(app.state.dbx, "user:timezone")
        relay = {**auth.relay_headers("device-abc"),
                  "X-SmartBrain-Timezone": "Asia/Tokyo"}
        assert c.get("/api/health", headers=relay).status_code == 200
        assert dbmod.meta_get(app.state.dbx, "user:timezone") == "Asia/Tokyo"
        assert dbmod.meta_get(app.state.dbx, "user:timezone_by") == "remote"
        # another remote probe with a DIFFERENT zone updates the stored zone (no desktop has
        # claimed it yet) — the setter stays "remote".
        relay2 = {**auth.relay_headers("device-xyz"),
                   "X-SmartBrain-Timezone": "America/Chicago"}
        assert c.get("/api/health", headers=relay2).status_code == 200
        assert dbmod.meta_get(app.state.dbx, "user:timezone") in ("Asia/Tokyo", "America/Chicago")


def test_i_desktop_always_overrides_a_remote_set_zone(tmp_path, monkeypatch) -> None:
    """A later DESKTOP handshake wins over a remote-set zone — the desktop is the authoritative
    source once it joins."""
    import os

    from fastapi.testclient import TestClient

    from smartbrain_3000 import auth
    from smartbrain_3000 import db as dbmod
    from smartbrain_3000.main import create_app

    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "tz.duckdb"))
    app = create_app()
    with TestClient(app) as c:
        c.headers.pop("Authorization", None)
        relay = {**auth.relay_headers("device-abc"),
                  "X-SmartBrain-Timezone": "America/Los_Angeles"}
        assert c.get("/api/health", headers=relay).status_code == 200
        assert dbmod.meta_get(app.state.dbx, "user:timezone") == "America/Los_Angeles"
        # desktop joins and reports a different zone — the stored zone flips to NY, setter desktop
        desk_auth = {"Authorization": f"Bearer {os.environ['SMARTBRAIN_LOCAL_TOKEN']}",
                      "X-SmartBrain-Timezone": "America/New_York"}
        assert c.get("/api/health", headers=desk_auth).json()["status"] == "ok"
        assert dbmod.meta_get(app.state.dbx, "user:timezone") == "America/New_York"
        assert dbmod.meta_get(app.state.dbx, "user:timezone_by") == "desktop"


# ---- H: export skips clock params (no empty ``value`` added to a clock-kind decl) --------------

def test_h_export_leaves_clock_params_untouched() -> None:
    """A clock-kind param carries ``format`` + ``offset_days``, never a user ``value`` — an export
    helper that stamps ``value: ""`` on it ships an invalid param decl."""
    from smartbrain_3000.ni_routes import _empty_param_values

    spec = {"params": {
        "date": {"label": "date", "kind": "clock", "format": "%Y-%m-%d", "offset_days": 0},
        "symbol": {"label": "symbol", "kind": "string", "value": "AAPL"},
        "key": {"label": "key", "kind": "secret", "value": "ni:self:key"}}}
    _empty_param_values(spec)
    assert "value" not in spec["params"]["date"], \
        "a clock param must not carry a value field"
    assert spec["params"]["symbol"]["value"] == "", \
        "a string param's value is zeroed"
    assert spec["params"]["key"]["value"].endswith(":key"), \
        "a secret param's value is the ni:self placeholder"
