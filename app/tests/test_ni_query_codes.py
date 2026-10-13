"""Round 20, Q3d — Library answers spec v1.5: a text cell's code table is read, never dropped (docs/internal/
ni-format.md §32, §36). The three NWS alert answers declare ``codes: "nws_event"`` on their event cell; the app keeps
them with their status filter, adds no pipeline step for a text table, reports no sky from it, and the query layer
grounds an alert name in the table's values (rule 3, ``Grounds.codes``; note ``filter_grounded:codes``).

Fixtures (``fixtures/ni_query``), from a pack built from the Library's v1.5 tree (2026-10-12):
``library_codes.json`` — the pack's ``library_codes`` rows (nws_event: the 111 NWS event types + "Test Message");
``library_nws.json`` — the three NWS alert records' name, params and answers; ``nws_alerts_national.json`` — the
national active-alerts response the Library's probe recorded (GET /alerts/active, 2026-10-12 23:15Z), cut to at most
three alerts per event (50 of 216, the one test alert kept) and to the properties the answers read. Set A's Houston
and set C's California responses (``samples/``) are those asks' recorded samples."""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import json
import os
import pathlib
from zoneinfo import ZoneInfo

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.ni_query import plan as q_plan
from smartbrain_3000.ni_query import plan_query, recognize
from smartbrain_3000.ni_query.ground import Grounds, ground_where, grounded_by
from smartbrain_3000.secrets import gen_master_key

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ni_query"
_CODES = json.loads((_FIX / "library_codes.json").read_text())
_NWS = json.loads((_FIX / "library_nws.json").read_text())
_NATIONAL = json.loads((_FIX / "nws_alerts_national.json").read_text())
_ZONE = "America/New_York"
NY = ZoneInfo(_ZONE)
SET_A_NOW = dt.datetime(2026, 10, 9, 7, 30, tzinfo=NY)          # after the set A / C samples' fetch (11:27Z)
NATIONAL_NOW = dt.datetime(2026, 10, 12, 19, 20, tzinfo=NY)     # after the national fetch (23:15Z)
QUERY_MARK = "You turn a person's ask into one query object"
EVENT = "properties.event"
STATUS = {"path": "properties.status", "equals": "Actual"}
NWS_EVENT = frozenset(_CODES["nws_event"])
_PACK_DB = os.environ.get("SMARTBRAIN_TEST_LIBRARY_DB", "")


def _answer(sid: str, name: str) -> dict:
    """One declared NWS answer, as the flow cleans it."""
    raw = next(a for a in _NWS[sid]["answers"] if a["name"] == name)
    cleaned = ni_flow._clean_answer(raw)
    assert cleaned is not None, f"{sid}.{name} breaks the answers contract"
    return cleaned


def _payload(record: dict) -> object:
    return json.loads(record["body"])


def _events(rows: list[dict]) -> list[str]:
    return [r["properties"]["event"] for r in rows]


# --- the reader -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sid", sorted(_NWS))
def test_the_reader_keeps_the_nws_alert_answers_with_their_code_table(sid) -> None:
    """Until Q3d the reader skipped every answer whose ``codes`` was not "wmo_weather": these sources kept only their
    count, the tropical one nothing. Every declared answer stands — the event cell keeps its table and the list its
    status filter (a test alert is no alert)."""
    declared = _NWS[sid]["answers"]
    kept = [ni_flow._clean_answer(a) for a in declared]
    assert [a["name"] for a, k in zip(declared, kept, strict=True) if k is None] == []
    lists = [k for k in kept if k["kind"] == "list"]
    assert len(lists) == 1
    assert lists[0]["cells"][0] == {"path": EVENT, "label": "Alert", "type": "text", "codes": "nws_event"}
    assert lists[0]["filter"] == STATUS


_TEXT_CELL = {"path": EVENT, "label": "Alert", "type": "text"}
_NUMBER_CELL = {"path": "weather_code", "label": "Sky", "type": "number"}


def _list_with(cell: dict) -> dict:
    return {"name": "alerts", "label": "Active weather alerts", "kind": "list", "path": "features", "row": [cell]}


def _value_with(atype: str, codes: object) -> dict:
    return {"name": "event", "label": "Alert", "kind": "value", "path": "features[1].properties.event",
            "type": atype, "codes": codes}


@pytest.mark.parametrize(("decl", "kept"), [
    (_list_with({**_TEXT_CELL, "codes": "nws_event"}), True),
    (_list_with({**_NUMBER_CELL, "codes": "wmo_weather"}), True),
    ({"name": "cols", "label": "Alerts", "kind": "columns", "columns": [{**_TEXT_CELL, "codes": "nws_event"}]}, True),
    (_value_with("text", "nws_event"), True),                      # spec v1.5: a text value, row or column
    (_value_with("number", "wmo_weather"), True),
    (_list_with({**_NUMBER_CELL, "codes": "nws_event"}), False),   # a text table on a number
    (_list_with({**_TEXT_CELL, "codes": "wmo_weather"}), False),   # the weather code on a text
    (_value_with("number", "nws_event"), False),
    (_value_with("count", "nws_event"), False),
    (_value_with("text", "wmo_weather"), False),
    (_list_with({**_TEXT_CELL, "codes": "NWS_Event"}), False),     # the Library's table slug only
    (_list_with({**_TEXT_CELL, "codes": "nws-event"}), False),
    (_list_with({**_TEXT_CELL, "codes": "1nws"}), False),
    (_list_with({**_TEXT_CELL, "codes": "n" * 41}), False),
    (_list_with({**_TEXT_CELL, "codes": "n" * 40}), True),
    (_list_with({**_TEXT_CELL, "codes": 5}), False),
])
def test_a_code_table_sits_on_its_cell_type(decl, kept) -> None:
    """``codes`` is "wmo_weather" on a number, or a Library table (its slug) on a text value / row / column cell;
    anything else breaks the contract and the answer is skipped, as an unknown table always was."""
    assert (ni_flow._clean_answer(decl) is not None) is kept


# --- the engine -----------------------------------------------------------------------------------------------------

def test_a_text_table_adds_no_pipeline_step() -> None:
    """``label`` turns a weather code into words (ni.LABEL_TABLES) and the pipeline validation refuses a table the
    app does not ship; a text cell already holds its words, so a Library table is never a step."""
    alerts = _answer("nws-alerts-point", "alerts")
    assert ni_flow._cell_ops(alerts["cells"][0], EVENT) == []
    assert ni_flow._cell_ops({**_NUMBER_CELL, "codes": "wmo_weather"}, "c") == [
        {"fn": "label", "field": "rows", "table": "wmo_weather", "key": "c"}]
    payload = _payload(_NATIONAL)
    built = ni_flow._build_value_answers([ni_flow._clean_answer(_value_with("text", "nws_event"))], payload)
    ops = [op for stage in built["pipeline"] if stage["op"] == "transform" for op in stage["apply"]]
    assert not any(op["fn"] == "label" for op in ops)
    assert built["preview_payload"]["event"] == "Marine Weather Statement"


def test_the_alerts_list_builds_on_a_recorded_response() -> None:
    """The declared build over the recorded national response: no label step, a spec the engine accepts, and the
    status filter keeps the test alert off the card."""
    built = ni_flow.build_from_answers([_answer("nws-alerts-state", "alerts")], copy.deepcopy(_payload(_NATIONAL)),
                                       "weather alerts")
    ops = [op for stage in built["pipeline"] if stage["op"] == "transform" for op in stage["apply"]]
    assert not any(op["fn"] == "label" for op in ops)
    events = _events(built["preview_payload"]["rows"])
    assert len(events) == 49 and "Test Message" not in events
    nimod.validate_spec({"version": 1, "title": "t", "goal": "g", "params": {},
                         "source": {"type": "http_json", "url": _NATIONAL["url"]}, "pipeline": built["pipeline"],
                         "scene": built["scene"], "display": {"size": "small"}, "interval_minutes": 15})


def test_only_the_weather_code_table_reports_the_sky() -> None:
    """A thunderstorm or snow is the sky's event: an answer reports it when it carries the WMO weather code. The NWS
    event names are a text table — the alerts list never claims to report snow (it would have, by its table)."""
    alerts = _answer("nws-alerts-point", "alerts")
    sky = ni_flow._clean_answer({"name": "hourly", "label": "Hourly", "kind": "list", "path": "hours",
                                 "row": [{**_NUMBER_CELL, "codes": "wmo_weather"}]})
    conditions = ni_flow._clean_answer(_value_with("number", "wmo_weather"))
    for measure in ("snow", "thunderstorm"):
        assert ni_flow._covers_measure(sky, measure) and ni_flow._covers_measure(conditions, measure)
        assert not ni_flow._covers_measure(alerts, measure)


# --- the pack's code tables -----------------------------------------------------------------------------------------

class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


def _pack(folder: pathlib.Path, *, codes: bool) -> library_index.LibraryIndex:
    """A real LibraryIndex over the three NWS records; ``codes``: with the pack's ``library_codes`` rows (without
    them, a pack older than spec v1.5)."""
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE library_sources(id VARCHAR PRIMARY KEY, record JSON)")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    for sid, rec in _NWS.items():
        record = {"id": sid, "name": rec["name"], "description": sid, "tier": "curated", "categories": [],
                  "kinds": [], "examples": [], "provider": {"id": "nws", "name": "NWS", "authority": "official"},
                  "access": {"kind": "http_json", "url_template": "https://api.weather.gov/alerts/active",
                             "params": rec["params"], "auth": "none", "headers": {}},
                  "answers": rec["answers"]}
        con.execute("INSERT INTO library_sources VALUES (?, ?)", (sid, json.dumps(record)))
    if codes:
        con.execute("CREATE TABLE library_codes(name VARCHAR, value VARCHAR)")
        con.executemany("INSERT INTO library_codes VALUES (?, ?)", [(n, v) for n, vs in _CODES.items() for v in vs])
    con.execute("INSERT INTO library_meta VALUES ('records', ?)", (str(len(_NWS)),))
    con.close()
    raw = gzip.compress(src.read_bytes())
    idx = library_index.LibraryIndex(folder / "data", netguard_mod=_Net(raw),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(raw).hexdigest()})
    idx.install()
    return idx


def test_code_values_reads_the_packs_table_once(tmp_path, monkeypatch) -> None:
    idx = _pack(tmp_path, codes=True)
    assert idx.code_values("nws_event") == NWS_EVENT and len(NWS_EVENT) == 112
    assert {"Flood Watch", "Coastal Flood Advisory", "Small Craft Advisory", "Test Message"} <= NWS_EVENT
    assert idx.code_values("no_such_table") == frozenset()

    def refused() -> None:
        raise AssertionError("read the pack again")
    monkeypatch.setattr(idx, "_conn", refused)
    assert idx.code_values("nws_event") == NWS_EVENT                  # cached per installed pack


def test_code_values_on_an_older_pack_is_empty_and_never_raises(tmp_path) -> None:
    assert _pack(tmp_path / "old", codes=False).code_values("nws_event") == frozenset()
    none = library_index.LibraryIndex(tmp_path / "none", pack={"tag": "v0", "url": "https://example.org/x.gz",
                                                               "sha256": "0" * 64})
    assert none.code_values("nws_event") == frozenset()               # not installed


def test_the_flow_reads_the_tables_its_answers_declare(tmp_path, monkeypatch) -> None:
    """``_code_values``: {table: values} for the text cells that declare one — {} on an older pack, without a
    Library, or from one that has no reader."""
    idx = _pack(tmp_path / "new", codes=True)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: idx)
    for sid, rec in _NWS.items():
        assert [a["name"] for a in ni_flow._library_answers(sid)] == [a["name"] for a in rec["answers"]]
    answers = ni_flow._library_answers("nws-alerts-point")
    assert ni_flow._code_values(answers) == {"nws_event": NWS_EVENT}
    assert ni_flow._code_values([_answer("nws-alerts-point", "alert_count")]) == {}   # no text cell declares one
    older = _pack(tmp_path / "old", codes=False)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: older)
    assert ni_flow._code_values(answers) == {}
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: object())
    assert ni_flow._code_values(answers) == {}
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", None)
    assert ni_flow._code_values(answers) == {}


# --- grounding ------------------------------------------------------------------------------------------------------

_GROUNDS = Grounds(codes={"nws_event": NWS_EVENT})


@pytest.mark.parametrize(("value", "col", "rule", "kept"), [
    ("coastal flood advisory", EVENT, "codes", "coastal flood advisory"),     # a value of the table, case ignored
    ("High Wind Warning", EVENT, "codes", "High Wind Warning"),              # kept as written
    ("small craft advisory", EVENT, "codes", "small craft advisory"),
    ("flood", EVENT, "codes", "flood"),                                      # whole words of Flood Watch, …
    ("flood watches", EVENT, "codes", "flood watch"),                        # the plural folded: the table's spelling
    ("winter weather advisories", EVENT, "codes", "winter weather advisory"),
    ("warning", EVENT, "codes", "warning"),                                  # narrows the alerts to warnings
    ("warnings", "*", "codes", "warning"),                                   # "*" reads every text cell
    ("fog advisories", EVENT, "codes", "fog advisory"),                      # two words inside Dense Fog Advisory
    ("thunderstorm warnings", EVENT, "codes", "thunderstorm warning"),       # inside Severe Thunderstorm Warning
    ("tornado", EVENT, "codes", "tornado"),                                  # the subject (first word) of a name
    ("anywhere", EVENT, None, None),
    ("weather", EVENT, None, None),                                          # a lone middle word: Winter Weather Advisory
    ("current", EVENT, None, None),                                          # Rip Current Statement
    ("craft", EVENT, None, None),                                            # Small Craft Advisory
    ("fog", EVENT, None, None),                                              # Dense Fog Advisory: alone it is a modifier
    ("flo", EVENT, None, None),                                              # whole words only
    ("flood", "properties.areaDesc", None, None),                            # the area cell declares no table
])
def test_an_alert_name_stands_on_the_code_table(value, col, rule, kept) -> None:
    alerts = _answer("nws-alerts-point", "alerts")
    spans = recognize(f"any {value} near me", now=NATIONAL_NOW, zone=_ZONE)
    assert grounded_by(value, col, alerts, phrases=[], spans=spans, grounds=_GROUNDS) == rule
    where, dropped, coded = ground_where([{"col": col, "op": "contains", "value": value}], alerts, phrases=[],
                                         spans=spans, grounds=_GROUNDS)
    assert [w["value"] for w in where] == ([kept] if kept else [])
    assert dropped == ([] if kept else [value]) and coded is (rule == "codes")


def test_the_code_values_come_from_the_caller() -> None:
    """ground.py does no I/O: without the caller's values the table grounds nothing (the Q3c reading), and values of
    a table the filtered cell does not declare are no evidence."""
    alerts = _answer("nws-alerts-point", "alerts")
    spans = recognize("any flood warnings in Houston", now=SET_A_NOW, zone=_ZONE)
    for grounds in (Grounds(), Grounds(codes={"other_table": NWS_EVENT})):
        assert grounded_by("flood", EVENT, alerts, phrases=[], spans=spans, grounds=grounds) is None


_A30_REPLY = next(json.loads(x) for x in (_FIX / "replies_zero.jsonl").read_text().splitlines()
                  if '"id": "A30"' in x)["reply_raw"]


def test_the_plan_notes_a_filter_the_code_table_grounded() -> None:
    """A30 "any flood warnings in Houston", the recorded zero-shot reply (contains "flood"; names "Houston", which the
    address took): in Q3c "flood" stood on no evidence and was dropped; the NWS event names ground it now."""
    rec = _NWS["nws-alerts-point"]
    args = {"kind": "alerts", "wants": [], "source": {"id": "nws-alerts-point", "name": rec["name"]},
            "answers": rec["answers"], "params": rec["params"], "consumed": ["Houston"], "now": SET_A_NOW,
            "zone": _ZONE, "call_model": lambda _messages: _A30_REPLY, "pool": []}
    plan = plan_query("any flood warnings in Houston", grounds=_GROUNDS, **args)
    assert plan.ir["where"] == [{"col": EVENT, "op": "contains", "value": "flood"}]
    assert "filter_grounded:codes" in plan.notes and plan.coverage["where"] == "bound"
    assert plan.interpretation == "Active weather alerts · Alert contains flood"
    before = plan_query("any flood warnings in Houston", grounds=Grounds(), **args)
    assert before.ir["where"] == [] and "filter_ungrounded:flood" in before.notes
    assert "filter_grounded:codes" not in before.notes and before.coverage["where"] == "approximated"


def test_the_note_is_one_closed_code() -> None:
    assert q_plan.note_ok("filter_grounded:codes") and nimod._query_note_ok("filter_grounded:codes")
    for bad in ("filter_grounded:keyword", "filter_grounded:", "filter_grounded"):
        assert not q_plan.note_ok(bad) and not nimod._query_note_ok(bad), bad


# --- the alert cards through the declared build ---------------------------------------------------------------------

def _model(reply: str):
    """The query turn reads ``reply``; every other turn (PRESENT, FIT) has no model (the rules)."""
    def model(prompt: str) -> str:
        if QUERY_MARK in prompt:
            return reply
        raise RuntimeError("no model for this turn")
    return model


def _ir(answer: str, **kw) -> str:
    return json.dumps({"answer": answer, "params": {}, "select": [], "where": [], "time": None, "order": [],
                       "limit": None, "agg": "none", **kw})


def _where(value: str) -> list[dict]:
    return [{"col": EVENT, "op": "contains", "value": value}]


@pytest.fixture()
def flow(tmp_path, monkeypatch):
    """The flow over a real LibraryIndex holding the three NWS records and the pack's code table."""
    idx = _pack(tmp_path, codes=True)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: idx)
    monkeypatch.setattr(nimod, "_user_timezone", NY)
    return monkeypatch


def _build(sid: str, ask: str, kind: str, reply: str, record: dict, now: dt.datetime, *, label: str, params: dict,
           place: str) -> dict:
    """The declared build of one alerts ask on a recorded response (the run's pick: label + params)."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    url = record["url"]
    item_id = ni_flow.create_shell_item(store, ask)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(ask, "source"))
    ni_flow.seal_library_pick(store, item_id, url, {"source_id": sid, "url": url, "format": "json", "params": params,
                                                    "label": label, "scope": "place"})
    intent = {"kind": "external_data", "subject": ask, "cadence_minutes": 15, "wants": [], "threshold": None,
              "place": place, "display_hint": "value", "names": [], "frame_kind": kind}
    payload = _payload(record)
    built = ni_flow._try_answers_build(store, item_id, ask, intent, url, copy.deepcopy(payload), _model(reply), now,
                                       do_fetch=lambda _u: copy.deepcopy(payload))
    assert built is not None and not built.get("nothing"), built
    nimod._validate_query(built["query"]["query"])
    return built


_HOUSTON = {"label": "Houston (TX)", "params": {"lat": "29.785743", "lon": "-95.388806"}, "place": "Houston"}
_NASHVILLE = {"label": "Nashville-Davidson metropolitan government (TN)",
              "params": {"lat": "36.1718", "lon": "-86.785002"}, "place": "Nashville"}


def test_a30_the_bare_flood_stands_and_houston_has_none(flow) -> None:
    """A30, the recorded zero-shot reply on the recorded Houston response (no alert that morning): Q3c dropped
    "flood" and the card stood for every Houston alert under an approximated filter; now the filter stands and the
    card is the honest empty under a line that says it."""
    flow.setattr(nimod, "_clock", lambda: SET_A_NOW)
    record = json.loads((_FIX / "samples" / "nws-alerts-point__9181481d.json").read_text())
    built = _build("nws-alerts-point", "any flood warnings in Houston", "alerts", _A30_REPLY, record, SET_A_NOW,
                   **_HOUSTON)
    q = built["query"]["query"]
    assert q["ir"]["where"] == _where("flood")
    assert "filter_grounded:codes" in q["notes"] and q["coverage"]["where"] == "bound"
    assert built["query"]["interpretation"] == "Active weather alerts · Alert contains flood"
    assert built["preview_payload"]["rows"] == []


def test_a_coastal_flood_advisory_ask_keeps_those_alerts_of_a_recorded_day(flow) -> None:
    """The said plural of an NWS name on the recorded California alerts (set C, 20 alerts): the five coastal flood
    advisories, in the table's spelling (``contains`` is a substring — the plural would match no row)."""
    flow.setattr(nimod, "_clock", lambda: SET_A_NOW)
    record = json.loads((_FIX / "samples" / "nws-alerts-state__6578e8aa.json").read_text())
    built = _build("nws-alerts-state", "coastal flood advisories in California", "alerts",
                   _ir("alerts", where=_where("coastal flood advisories")), record, SET_A_NOW,
                   label="California (CA)", params={"state": "CA"}, place="California")
    assert built["query"]["query"]["ir"]["where"] == _where("coastal flood advisory")
    assert _events(built["preview_payload"]["rows"]) == ["Coastal Flood Advisory"] * 5


@pytest.mark.parametrize(("value", "line"), [
    ("Flood Watch", "Active weather alerts · Alert contains Flood Watch"),     # the live set F reply
    ("flood watches", "Active weather alerts · Alert contains flood watch"),   # the said plural, in the table's words
])
def test_f33_flood_watches_keep_the_flood_watches(flow, value, line) -> None:
    """F33 "any flood watches around Nashville" on the recorded national alerts (three flood watches among 50)."""
    flow.setattr(nimod, "_clock", lambda: NATIONAL_NOW)
    built = _build("nws-alerts-point", "any flood watches around Nashville", "alerts", _ir("alerts", where=_where(value)),
                   _NATIONAL, NATIONAL_NOW, **_NASHVILLE)
    q = built["query"]["query"]
    assert "filter_grounded:codes" in q["notes"] and built["query"]["interpretation"] == line
    assert _events(built["preview_payload"]["rows"]) == ["Flood Watch"] * 3


def test_f35_winter_weather_advisories(flow) -> None:
    """F35 "winter weather advisories for Vermont". The live reply ("Winter Weather Advisory") never reaches the
    grounding — the ask says "advisories", and the v1 rule takes a plural s / es only — so on the run's own Vermont
    response (no alert) the card is the honest empty under a line that claims no filter. A reply in the ask's words
    stands on the table and keeps the three winter weather advisories of the national response."""
    flow.setattr(nimod, "_clock", lambda: NATIONAL_NOW)
    vermont = {"url": "https://api.weather.gov/alerts/active?area=VT",
               "body": json.dumps({"type": "FeatureCollection", "features": []})}
    ask, pick = "winter weather advisories for Vermont", {"label": "Vermont (VT)", "params": {"state": "VT"},
                                                          "place": "Vermont"}
    live = _build("nws-alerts-state", ask, "alerts", _ir("alerts", where=_where("Winter Weather Advisory")), vermont,
                  NATIONAL_NOW, **pick)
    assert live["query"]["query"]["ir"]["where"] == [] and live["preview_payload"]["rows"] == []
    assert live["query"]["interpretation"] == "Active weather alerts"
    said = _build("nws-alerts-state", ask, "alerts", _ir("alerts", where=_where("winter weather advisories")),
                  _NATIONAL, NATIONAL_NOW, **pick)
    assert said["query"]["query"]["ir"]["where"] == _where("winter weather advisory")
    assert _events(said["preview_payload"]["rows"]) == ["Winter Weather Advisory"] * 3


def test_f37_warnings_count_the_warnings(flow) -> None:
    """F37 "number of active NWS warnings in Texas right now" with a reply that filters on the said "warnings": the
    bare type word stands on the table (every "… Warning") and the count is the warnings' — 12 of the national
    response's 49 actual alerts."""
    flow.setattr(nimod, "_clock", lambda: NATIONAL_NOW)
    built = _build("nws-alerts-state", "number of active NWS warnings in Texas right now", "count",
                   _ir("alerts", where=_where("warnings")), _NATIONAL, NATIONAL_NOW,
                   label="Texas (TX)", params={"state": "TX"}, place="Texas")
    q = built["query"]["query"]
    assert q["ir"]["where"] == _where("warning") and q["ir"]["agg"] == "count"
    assert "filter_grounded:codes" in q["notes"]
    assert built["preview_payload"]["rows_count"] == 12


# --- a built pack (optional) ----------------------------------------------------------------------------------------

@pytest.mark.skipif(not (_PACK_DB and pathlib.Path(_PACK_DB).is_file()),
                    reason="set SMARTBRAIN_TEST_LIBRARY_DB to a library.duckdb built from the Library's v1.5 tree")
def test_the_fixtures_match_a_built_v15_pack(tmp_path, monkeypatch) -> None:
    """Against a pack built from the Library's v1.5 tree: its code table and the three NWS records are the fixtures'
    (no drift), and the reader keeps every NWS answer with its table and status filter."""
    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "library.duckdb").symlink_to(pathlib.Path(_PACK_DB).resolve())
    pin = {"tag": "v1.5-test", "url": "https://example.org/library.duckdb.gz", "sha256": "0" * 64}
    (folder / "installed.json").write_text(json.dumps({"tag": pin["tag"], "sha256": pin["sha256"]}))
    idx = library_index.LibraryIndex(tmp_path, pack=pin)
    assert idx.code_values("nws_event") == NWS_EVENT
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: idx)
    for sid, rec in _NWS.items():
        assert idx.answers(sid) == rec["answers"], sid
        kept = ni_flow._library_answers(sid)
        assert [a["name"] for a in kept] == [a["name"] for a in rec["answers"]], sid
        assert ni_flow._code_values(kept) == {"nws_event": NWS_EVENT}, sid
