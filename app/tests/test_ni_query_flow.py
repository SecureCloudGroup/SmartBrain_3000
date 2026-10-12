"""Round 20, Q3b — the query layer wired into the declared build (docs/internal/ni-format.md §36): the flow's
model transport, the consumed spans, the answer the plan reads and its honest stops, the time binding (the
answer's direction, spans, the clock-filled date parameters and the rebound fetch), the where ops with the
Library's ``param_bounds`` push-down, the sealed ``query`` + ``interpretation``, and the page path's window.

The Library is a REAL LibraryIndex over a small DuckDB pack (``test_ni_answers`` style); models are scripted
(the query turn answers with the Query IR a test stands for); no network."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, ni_flow, ni_library, ni_routes
from smartbrain_3000 import ni as nimod
from smartbrain_3000.secrets import gen_master_key

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=NY)          # a Friday morning
QUERY_MARK = "You turn a person's ask into one query object"


def _ir(answer: str, **kw) -> str:
    return json.dumps({"answer": answer, "params": {}, "select": [], "where": [], "time": None, "order": [],
                       "limit": None, "agg": "none", **kw})


def _model(reply: str, seen: list | None = None):
    """The query turn reads ``reply``; every other turn (PRESENT, FIT) has no model (the rules)."""
    def model(prompt: str) -> str:
        if seen is not None:
            seen.append(prompt)
        if QUERY_MARK in prompt:
            return reply
        raise RuntimeError("no model for this turn")
    return model


# --- the pack: sources whose records carry params, templates and answers ----------------------------------------

QUAKES = {"name": "quakes", "label": "Earthquakes", "kind": "list", "primary": True, "path": "features",
          "words": ["earthquakes", "quakes", "magnitude"], "may_be_empty": True,
          "row": [{"path": "properties.mag", "label": "Magnitude", "type": "number"},
                  {"path": "properties.place", "label": "Place", "type": "text"},
                  {"path": "properties.time", "label": "Time", "type": "time"}],
          "param_bounds": {"properties.mag": {"min": "min_mag"}}}
HOLIDAYS = {"name": "holidays", "label": "Public holidays", "kind": "list", "primary": True, "path": "items",
            "words": ["holidays", "holiday", "when is"],
            "row": [{"path": "date", "label": "Date", "type": "date"}, {"path": "name", "label": "Holiday", "type": "text"}],
            "axis": {"cell": "date", "step": "day"}}
GAMES = {"name": "games", "label": "Games", "kind": "list", "primary": True, "path": "games",
         "words": ["games", "scores", "result"], "may_be_empty": False,
         "row": [{"path": "away", "label": "Away", "type": "text"}, {"path": "home", "label": "Home", "type": "text"},
                 {"path": "score", "label": "Score", "type": "text"}]}
WEATHER = [{"name": "temperature", "label": "Temperature", "kind": "value", "primary": True, "type": "number",
            "path": "current.t", "unit": "°F", "words": ["temperature", "temp", "degrees"], "window": "now",
            "measure": "temperature"},
           {"name": "conditions", "label": "Conditions", "kind": "value", "primary": True, "type": "text",
            "path": "current.c", "words": ["conditions", "sky"], "window": "now", "measure": "conditions"}]


def _record(sid: str, answers: list[dict], template: str, params: list[dict]) -> dict:
    return {"id": sid, "name": sid.replace("-", " "), "description": f"{sid} description", "tier": "curated",
            "categories": ["weather/current"], "kinds": ["current_value"], "examples": [],
            "access": {"kind": "http_json", "url_template": template, "params": params, "auth": "none", "headers": {}},
            "provider": {"id": "p", "name": "Provider", "authority": "official"}, "answers": answers}


RECORDS = [
    _record("quakes-state", [QUAKES], "https://example.org/q?minmagnitude={min_mag}&starttime={start}&orderby=time",
            [{"name": "min_mag", "kind": "none", "example": "2.5", "fill": {"from": "default", "value": "2.5"}},
             {"name": "start", "kind": "date", "example": "2026-09-09",
              "fill": {"from": "clock", "format": "%Y-%m-%d", "offset_days": -30}}]),
    _record("holidays-year", [HOLIDAYS], "https://example.org/h/{year}/US",
            [{"name": "year", "kind": "date", "example": "2026", "fill": {"from": "clock", "format": "%Y", "offset_days": 0}}]),
    _record("nhl-scores", [GAMES], "https://example.org/s", []),
    _record("weather-now", WEATHER, "https://example.org/w", []),
]


class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE library_sources(id VARCHAR PRIMARY KEY, record JSON)")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    for rec in RECORDS:
        con.execute("INSERT INTO library_sources VALUES (?, ?)", (rec["id"], json.dumps(rec)))
    con.execute("INSERT INTO library_meta VALUES ('records', ?)", (str(len(RECORDS)),))
    con.close()
    raw = gzip.compress(src.read_bytes())
    idx = library_index.LibraryIndex(tmp_path / "data", netguard_mod=_Net(raw),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(raw).hexdigest()})
    idx.install()
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: idx)
    monkeypatch.setattr(nimod, "_clock", lambda: NOW)
    monkeypatch.setattr(nimod, "_user_timezone", NY)
    return idx


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


def _intent(ask: str, kind: str | None = None, names: tuple = ()) -> dict:
    return {"kind": "external_data", "subject": ask, "cadence_minutes": 15, "wants": [], "threshold": None,
            "place": None, "display_hint": "value", "names": list(names), "frame_kind": kind}


def _picked(store, sid: str, ask: str, url: str, params: dict | None = None) -> str:
    item_id = ni_flow.create_shell_item(store, ask)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(ask, "source"))
    ni_flow.seal_library_pick(store, item_id, url, {"source_id": sid, "url": url, "format": "json",
                                                    "params": params or {}})
    return item_id


def _ops(spec: dict) -> list[dict]:
    return [op for st in spec["pipeline"] if st.get("op") == "transform" for op in st["apply"]]


# --- the transport and the consumed spans ---------------------------------------------------------------------

def test_the_query_turn_goes_through_the_flow_models_messages_seam() -> None:
    """Measured (§36): the flattened prompt lost 3 of 60 rows live — the flow model's ``chat`` carries the
    system + user messages end to end; a prompt-only seam (a caller's own) gets them flattened as FIT does."""
    sent: list = []
    chat = sent.append
    flow = ni_flow.FlowModel(prompt=lambda p: "", chat=chat)
    assert ni_flow._query_transport(flow) is chat
    assert flow("x") == ""                        # the prompt stages call it as before
    prompts: list = []
    flat = ni_flow._query_transport(lambda p: prompts.append(p) or "{}")
    flat([{"role": "system", "content": "RULES"}, {"role": "user", "content": "ASK"}])
    assert prompts == ["system:\nRULES\n\nuser:\nASK"]
    assert ni_flow._query_transport(None) is None


def test_consumed_spans_are_the_ask_words_the_address_took() -> None:
    ask = "current wind speed at Chicago O'Hare"
    spans = ni_flow._consumed_spans(ask, ["Chicago O'Hare International Airport"])
    assert [ask[a:b] for a, b in spans] == ["Chicago O'Hare"]
    assert ni_flow._consumed_spans("weather at the lake", ["The Lake"]) == [(15, 19)]   # never the filler word


# --- the answer and the honest stops -------------------------------------------------------------------------------

def test_chosen_answers_are_the_irs_answer_and_its_value_companions() -> None:
    answers = [ni_flow._clean_answer(a) for a in WEATHER]
    chosen = ni_flow._chosen_answers({"answer": "temperature", "select": ["conditions"]}, answers)
    assert [a["name"] for a in chosen] == ["temperature", "conditions"]
    assert [a["name"] for a in ni_flow._chosen_answers({"answer": "quakes", "select": ["properties.mag"]},
                                                       [ni_flow._clean_answer(QUAKES)])] == ["quakes"]


def test_a_measure_no_answer_reports_is_the_sources_honest_nothing(lib) -> None:
    answers = ni_flow._library_answers("weather-now")
    assert ni_flow._unbound_measures("wind in Denver right now", answers, frozenset()) == (["wind"], ["wind"])
    assert ni_flow._unbound_measures("temperature in Denver", answers, frozenset()) == (["temperature"], [])
    # a place that is a measure word names no measure ("Wind River" was taken by the address)
    assert ni_flow._unbound_measures("weather in Wind River", answers, frozenset({"wind", "river"})) == ([], [])
    store = _store()
    url = "https://example.org/w"
    item_id = _picked(store, "weather-now", "wind in Denver right now", url)
    built = ni_flow._try_answers_build(store, item_id, "wind in Denver right now", _intent("wind in Denver right now"),
                                       url, {"current": {"t": 61, "c": "Clear"}},
                                       _model(_ir("temperature", time={"from": "now", "to": "now"})), NOW)
    assert built["nothing"] and built["why"] == "doesn't report wind"
    assert "quantity_unbound" in built["query"]["query"]["notes"]


def test_a_day_ahead_the_answer_is_not_about_is_the_honest_nothing(lib) -> None:
    store = _store()
    url = "https://example.org/w"
    ask = "temperature in Denver tomorrow"
    item_id = _picked(store, "weather-now", ask, url)
    built = ni_flow._try_answers_build(store, item_id, ask, _intent(ask), url, {"current": {"t": 61, "c": "Clear"}},
                                       _model(_ir("temperature", time={"from": "tomorrow", "to": "tomorrow"})), NOW)
    assert built["nothing"] and built["why"] == "doesn't give tomorrow"


# --- time: direction, spans, the legacy windows ------------------------------------------------------------------

PAST = {"features": [{"properties": {"mag": 4.6, "place": "A", "time": "2026-10-08T09:00:00-04:00"}},
                     {"properties": {"mag": 2.7, "place": "B", "time": "2026-09-20T09:00:00-04:00"}}]}


def test_the_rows_say_which_way_a_stretch_reads() -> None:
    quakes = ni_flow._clean_answer(QUAKES)
    assert ni_flow._rows_direction(quakes, PAST, NOW, None) == "backward"
    ahead = {"features": [{"properties": {"mag": 1, "place": "A", "time": "2026-10-10T09:00:00-04:00"}}]}
    assert ni_flow._rows_direction(quakes, ahead, NOW, None) == "forward"
    assert ni_flow._rows_direction(quakes, {"features": []}, NOW, "trend") == "backward"   # no rows: the kind's way


@pytest.mark.parametrize(("time", "direction", "binding", "coverage"), [
    (None, "backward", None, "bound"),
    ({"from": "now", "to": "now"}, "backward", None, "bound"),                # an instant list as it stands
    ({"from": "now", "to": None}, "forward", {"window": "upcoming", "span": None, "forward": True}, "bound"),
    ({"from": "past_days:7", "to": "today"}, "backward",
     {"window": "past_days:7", "span": {"to": "today", "back": True, "forward": False}}, "bound"),
    ({"from": "dow:tue", "to": "dow:tue"}, "backward",
     {"window": "dow:tue", "span": {"to": "dow:tue", "back": True, "forward": False}}, "bound"),
    ({"from": "today", "to": "next_days:7"}, "backward", None, "approximated"),   # a stretch ahead over past rows
    ({"from": "today", "to": "next_days:30"}, "forward",
     {"window": "next_days:16", "span": None, "forward": True}, "approximated"),  # clamped to the engine's 16
])
def test_the_irs_time_binds_to_the_answers_rows(time, direction, binding, coverage) -> None:
    quakes = ni_flow._clean_answer(QUAKES)
    assert ni_flow._list_time_binding(time, quakes, direction) == (binding, coverage)


def test_a_list_with_no_time_cell_never_stands_for_a_day_ahead() -> None:
    games = ni_flow._clean_answer(GAMES)
    assert ni_flow._list_time_binding({"from": "tomorrow", "to": "tomorrow"}, games, "forward") == (None, "unbound")
    assert ni_flow._list_time_binding({"from": "today", "to": "today"}, games, "forward") == (None, "approximated")


def test_a_count_always_cuts_its_stretch() -> None:
    quakes = ni_flow._clean_answer(QUAKES)
    got, coverage = ni_flow._list_time_binding({"from": "today", "to": "next_days:7"}, quakes, "backward", count=True)
    assert got["window"] == "next_days:7" and coverage == "bound"


@pytest.mark.parametrize(("time", "start", "fixed"), [
    ({"from": "past_days:90", "to": "today"}, -90, None),
    ({"from": "this_week", "to": "this_week"}, -6, None),        # covers the Monday on any weekday
    ({"from": "date:2026-11-26", "to": "date:2026-11-26"}, -30, "2026-11-26"),
    ({"from": "now", "to": None}, -30, None),                     # from now on: the source's own default
])
def test_the_clock_filled_start_takes_the_stretchs_first_day(time, start, fixed) -> None:
    clock = {"start": {"format": "%Y-%m-%d", "offset_days": -30, "label": "start"}}
    got = ni_flow._clock_rebind(clock, time, False, NOW)["start"]
    assert got["offset_days"] == start and got.get("value") == fixed


def test_an_asked_year_is_a_fixed_year_parameter() -> None:
    clock = {"year": {"format": "%Y", "offset_days": 0, "label": "year"}}
    assert ni_flow._clock_rebind(clock, {"from": "year:2027", "to": "year:2027"}, False, NOW)["year"]["value"] == "2027"
    assert ni_flow._clock_spec_params({"year": {"format": "%Y", "offset_days": 0, "label": "y", "value": "2027"}}) \
        == {"year": {"label": "y", "kind": "string", "value": "2027"}}


# --- where: sealed ops, the names over every text cell, the push-down ----------------------------------------------

def test_names_over_every_text_cell_keep_the_teams_own_games() -> None:
    """The SET D W3 class: "the latest Sharks game" on a league scoreboard — the team is home in one game and
    away in another; the filter keeps both and nothing else (the removed subject filter kept the whole list)."""
    games = ni_flow._clean_answer(GAMES)
    keys = {c["path"]: c["path"] for c in games["cells"]}
    ops, dropped = ni_flow._where_ops([{"col": "*", "op": "names", "value": "Sharks"}], games, keys)
    assert dropped == [] and ops == [{"fn": "where", "field": "rows", "key": "away", "op": "contains",
                                      "value": "Sharks", "or_keys": ["home", "score"]}]
    rows = [{"away": "San Jose Sharks", "home": "Blues", "score": "2-1"},
            {"away": "Stars", "home": "San Jose Sharks", "score": "0-3"},
            {"away": "Bruins", "home": "Sabres", "score": "4-4"}]
    out = nimod.run_pipeline([{"op": "extract", "paths": {"rows": "games"}}, {"op": "transform", "apply": ops}],
                             {"games": rows})
    assert [r["score"] for r in out["rows"]] == ["2-1", "0-3"]


def test_a_comparator_reads_the_cell_as_a_number_and_pushes_into_its_bound() -> None:
    quakes = ni_flow._clean_answer(QUAKES)
    where = [{"col": "properties.mag", "op": ">", "value": 4}]
    ops, _ = ni_flow._where_ops(where, quakes, {c["path"]: c["path"] for c in quakes["cells"]})
    assert ops == [{"fn": "number", "field": "rows", "key": "properties.mag"},
                   {"fn": "where", "field": "rows", "key": "properties.mag", "op": "gt", "value": 4}]
    assert ni_flow._bound_overrides(where, quakes) == {"min_mag": "4"}
    assert ni_flow._bound_overrides([{"col": "properties.mag", "op": ">=", "value": 2.5}], quakes) == {"min_mag": "2.5"}
    assert ni_flow._bound_overrides([{"col": "properties.mag", "op": "<", "value": 3}], quakes) == {}   # no max


def test_a_name_neither_the_address_nor_a_filter_takes_leaves_the_card_unbound() -> None:
    intent = _intent("result of the latest Sharks game", names=("Sharks", "NHL"))
    assert ni_flow._names_unbound(intent, "result of the latest Sharks game", frozenset(), "nhl scores", []) \
        == ["Sharks"]
    assert ni_flow._names_unbound(intent, "…", frozenset(), "nhl scores",
                                  [{"col": "*", "op": "names", "value": "Sharks"}]) == []
    assert ni_flow._names_unbound(intent, "…", frozenset({"sharks"}), "nhl scores", []) == []


# --- order and limit: the line's "top N" / "the next one" is what the card executes ---------------------------------

def test_the_order_and_limit_are_sealed_sorts_then_a_cut() -> None:
    keys = {"properties.mag": "properties.mag", "properties.place": "properties.place"}
    ops = ni_flow._rank_ops([{"col": "properties.mag", "dir": "desc"}, {"col": "properties.place", "dir": "asc"}],
                            3, keys)
    assert ops == [{"fn": "sort_by", "field": "rows", "key": "properties.place", "dir": "asc", "missing": "last"},
                   {"fn": "sort_by", "field": "rows", "key": "properties.mag", "dir": "desc", "missing": "last"},
                   {"fn": "top_n", "field": "rows", "n": 3}]       # stable sorts, the last key first, then the cut
    assert ni_flow._rank_ops([{"col": "nope", "dir": "asc"}], None, keys) == []
    assert ni_flow._rank_ops([], 500, keys) == [{"fn": "top_n", "field": "rows", "n": nimod._MAX_TOP_N}]


def test_a_ranked_ask_keeps_its_top_rows_in_order() -> None:
    quakes = ni_flow._clean_answer(QUAKES)
    sample = {"features": [{"properties": {"mag": m, "place": p, "time": "2026-10-08T09:00:00-04:00"}}
                           for m, p in ((2.7, "A"), (None, "B"), (4.6, "C"), (3.1, "D"))]}
    built = ni_flow.build_from_answers([quakes], sample, "Earthquakes",
                                       order=[{"col": "properties.mag", "dir": "desc"}], row_limit=2)
    assert [r["properties"]["place"] for r in built["preview_payload"]["rows"]] == ["C", "D"]
    assert built["preview_payload"]["rows_count"] == 2          # the may_be_empty count reads the kept rows


def test_the_next_one_is_the_one_row_the_card_keeps(lib) -> None:
    store = _store()
    url = "https://example.org/h/2026/US"
    ask = "when is the next holiday"
    item_id = _picked(store, "holidays-year", ask, url, {"year": "2026"})
    sample = {"items": [{"date": "2026-12-25", "name": "Christmas Day"},
                        {"date": "2026-07-04", "name": "Independence Day"},
                        {"date": "2026-11-26", "name": "Thanksgiving Day"}]}
    out = ni_flow._sample_and_map(store, item_id, ask, _intent(ask, "next_event"), url, _model(_ir("holidays")),
                                  lambda _u: copy.deepcopy(sample))
    assert out["state"] == "ready", out["notes"]
    spec = store.get_item(item_id)["spec"]
    assert spec["query"]["ir"]["limit"] == 1 and spec["query"]["ir"]["order"] == [{"col": "date", "dir": "asc"}]
    fns = [op["fn"] for op in _ops(spec)]
    assert fns.index("window") < fns.index("sort_by") < fns.index("top_n")
    assert spec["interpretation"] == "Public holidays · from now · the next one"
    rows = store.read_snapshot(item_id, "preview_data")["payload"]["rows"]
    assert [r["name"] for r in rows] == ["Thanksgiving Day"]     # past July 4 cut, the source's order sorted


def test_the_intents_names_are_masked_for_the_recognizers(lib) -> None:
    """Round 20, Q3b fix round: the proper names stage 1 copied from the ask reach the recognizers as masks —
    "Saturday Night Live" is no Saturday; a holiday the table names stays a date."""
    holidays = [ni_flow._clean_answer(HOLIDAYS)]
    live = {"_library_source": "holidays-year"}
    ask = "Saturday Night Live schedule"
    named = ni_flow._query_plan(ask, _intent(ask, names=("Saturday Night Live",)), live, holidays, NOW, None)
    bare = ni_flow._query_plan(ask, _intent(ask), live, holidays, NOW, None)
    assert named.ir["time"] is None and bare.ir["time"] == {"from": "dow:sat", "to": "dow:sat"}
    ask = "when is Thanksgiving"
    kept = ni_flow._query_plan(ask, _intent(ask, names=("Thanksgiving",)), live, holidays, NOW, None)
    assert kept.ir["time"] == {"from": "date:2026-11-26", "to": "date:2026-11-26"}


# --- end to end through _sample_and_map: the sealed card ------------------------------------------------------------

def test_a_team_named_in_no_row_is_the_honest_nothing(lib) -> None:
    store = _store()
    url = "https://example.org/s"
    ask = "result of the latest Sharks game"
    item_id = _picked(store, "nhl-scores", ask, url)
    sample = {"games": [{"away": "Bruins", "home": "Sabres", "score": "4-4"}]}
    out = ni_flow._sample_and_map(store, item_id, ask, _intent(ask, "result"), url,
                                  _model(_ir("games", where=[{"col": "*", "op": "names", "value": "Sharks"}])),
                                  lambda _u: copy.deepcopy(sample))
    assert out["state"] == "unsupported" and "has nothing for this right now" in out["error"]


def test_a_pushed_bound_and_a_stretch_refetch_and_seal_the_card(lib) -> None:
    store = _store()
    url = "https://example.org/q?minmagnitude=2.5&starttime=2026-09-09&orderby=time"
    ask = "how many earthquakes above magnitude 4 in the past 90 days"
    item_id = _picked(store, "quakes-state", ask, url, {"min_mag": "2.5", "start": "2026-09-09"})
    fetched: list[str] = []

    def fetch(u: str) -> dict:
        fetched.append(u)
        return copy.deepcopy(PAST)
    reply = _ir("quakes", where=[{"col": "properties.mag", "op": ">", "value": 4}],
                time={"from": "past_days:90", "to": "today"}, agg="count")
    out = ni_flow._sample_and_map(store, item_id, ask, _intent(ask, "count"), url, _model(reply), fetch)
    assert out["state"] == "ready", out["notes"]
    assert fetched == [url, "https://example.org/q?minmagnitude=4&starttime=2026-07-11&orderby=time"]
    spec = store.get_item(item_id)["spec"]
    assert spec["source"]["url"] == "https://example.org/q?minmagnitude=4&starttime={{param:start}}&orderby=time"
    assert spec["params"]["start"]["offset_days"] == -90
    ops = _ops(spec)
    assert {"fn": "where", "field": "rows", "key": "properties.mag", "op": "gt", "value": 4} in ops
    assert any(op["fn"] == "window" and op["window"] == "past_days:90" and op.get("back") for op in ops)
    assert {"fn": "count", "field": "rows", "as": "rows_count"} in ops
    assert spec["query"]["ir"]["agg"] == "count"
    assert {"bound_pushed", "clock_bound", "refetched"} <= set(spec["query"]["notes"])
    assert spec["interpretation"] == "Earthquakes · Magnitude > 4 · past 90 days · count"
    nimod.validate_spec(spec)
    preview = store.read_snapshot(item_id, "preview_data")["payload"]
    assert preview["rows_count"] == 1                       # the 4.6 inside 90 days; the 2.7 filtered


def test_an_asked_year_refetches_that_year_and_seals_it_fixed(lib) -> None:
    store = _store()
    url = "https://example.org/h/2026/US"
    ask = "when is Thanksgiving in 2027"
    item_id = _picked(store, "holidays-year", ask, url, {"year": "2026"})
    sample = {"items": [{"date": "2027-11-25", "name": "Thanksgiving Day"},
                        {"date": "2027-12-25", "name": "Christmas Day"}]}
    fetched: list[str] = []
    reply = _ir("holidays", where=[{"col": "name", "op": "contains", "value": "Thanksgiving"}],
                time={"from": "year:2027", "to": "year:2027"})
    out = ni_flow._sample_and_map(store, item_id, ask, _intent(ask, "lookup"), url, _model(reply),
                                  lambda u: fetched.append(u) or copy.deepcopy(sample))
    assert out["state"] == "ready", out["notes"]
    assert fetched[-1] == "https://example.org/h/2027/US"
    spec = store.get_item(item_id)["spec"]
    assert spec["params"]["year"] == {"label": "year", "kind": "string", "value": "2027"}
    assert nimod.substitute_params(spec)["source"]["url"] == "https://example.org/h/2027/US"
    assert spec["interpretation"] == "Public holidays · Holiday contains Thanksgiving · in 2027"
    rows = store.read_snapshot(item_id, "preview_data")["payload"]["rows"]
    assert [r["name"] for r in rows] == ["Thanksgiving Day"]


def test_without_a_model_the_rules_floor_reads_the_ask_and_says_so(lib) -> None:
    store = _store()
    url = "https://example.org/w"
    ask = "temperature in Denver right now"
    item_id = _picked(store, "weather-now", ask, url)
    built = ni_flow._try_answers_build(store, item_id, ask, _intent(ask), url, {"current": {"t": 61, "c": "Clear"}},
                                       None, NOW)
    q = built["query"]["query"]
    assert built["labels"] == ["Temperature"]
    assert {"query_rules", "model_off"} <= set(q["notes"]) and q["coverage"]["answer"] == "rules"
    assert q["model"] == {"attempts": 0, "used_fewshot": True}
    assert built["query"]["interpretation"] == "Temperature · now"


# --- the sealed fields travel like _fit -----------------------------------------------------------------------------

def test_the_reading_is_stripped_on_export_and_never_installed_from_a_template() -> None:
    for key in ("query", "interpretation"):
        assert key in ni_routes._EXPORT_STRIP_KEYS and key in ni_library._TEMPLATE_STRIP_KEYS


# --- the page path reads the recognizers -------------------------------------------------------------------------------

@pytest.mark.parametrize(("ask", "window"), [
    ("weather this weekend in Austin", "weekend"), ("temperature right now in Boise", "now"),
    ("rain tomorrow in Seattle", "tomorrow"), ("gas prices over the past month", None),
])
def test_the_page_window_is_the_recognizers_explicit_phrase(lib, ask, window) -> None:
    assert ni_flow._ask_window(ask, None) == window
