"""Round 20 Q3a: ``ni_query.apply_query`` — the Q2 harness's execution semantics, ported. The gold
IRs over the recorded JSON samples reproduce the harness's own rows; the unit cases pin the
direction-placed tokens, the point cut, the where ops, order / limit / agg."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from zoneinfo import ZoneInfo

from smartbrain_3000.ni_query import apply_query
from smartbrain_3000.ni_query.apply import dig, token_interval
from smartbrain_3000.ni_query.decl import answer_named, axis_of, cells_of

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ni_query"
_ZONE = "America/New_York"
_TZ = ZoneInfo(_ZONE)


def _harness_ids(answer: dict, res: dict, payload: object) -> list[str]:
    path = str(answer.get("path") or "")
    if answer["kind"] == "value":
        counted = answer.get("type") == "count" and isinstance(dig(payload, path), list)
        return [f"{path}#{i}" if counted else f"value:{path}" for i in res["indices"]]
    if answer["kind"] == "columns":
        base = (axis_of(answer) or cells_of(answer)[0]["path"]).rsplit(".", 1)[0]
        return [f"{base}#{i}" for i in res["indices"]]
    return [f"{path}#{i}" for i in res["indices"]]


def test_gold_rows_match_the_harness_on_json_samples():
    """Every gold IR over its recorded JSON sample: the harness's rows (first 12), row count, count."""
    gold = {json.loads(x)["id"]: json.loads(x) for x in (_FIX / "gold.jsonl").read_text().splitlines() if x}
    lib = json.loads((_FIX / "library.json").read_text())
    index = json.loads((_FIX / "samples" / "index.json").read_text())
    want = json.loads((_FIX / "exec_gold.json").read_text())
    checked = 0
    for rid, row in gold.items():
        entry = index.get(rid) or {}
        sample = json.loads((_FIX / "samples" / entry["file"]).read_text()) if entry.get("file") else {}
        body = str(sample.get("body") or "").lstrip()
        if not body[:1] in ("{", "[") or rid not in want:
            continue   # CSV / RSS samples go through the eval tool's parser (test_ni_query_eval)
        payload = json.loads(body)
        payload = {"items": payload} if isinstance(payload, list) else payload
        answer = dict(answer_named(lib[row["source_id"]]["answers"], row["ir"]["answer"]))
        if answer.get("filter"):
            equals = str(answer["filter"]["equals"])
            for k, v in row["params_used"].items():
                equals = equals.replace("{" + k + "}", str(v))
            answer["filter"] = {**answer["filter"], "equals": equals}
        res = apply_query(row["ir"], payload, answer, now=dt.datetime.fromisoformat(entry["fetched_at"]),
                          zone=_ZONE, direction="forward")
        ids = _harness_ids(answer, res, payload)
        assert (len(ids), ids[:12], res["count"]) == (want[rid]["n"], want[rid]["ids"], want[rid]["count"]), rid
        checked += 1
    assert checked >= 90, f"only {checked} JSON samples checked"


def test_dig_paths():
    doc = {"games": [{"teams": {"away": {"team": {"name": "Sharks"}}}}], "a": {"b": [1, 2, 3]}}
    assert dig(doc, "games[0].teams.away.team.name") == "Sharks"
    assert dig(doc, "a.b[2]") == 3 and dig(doc, "a.b[9]") is None and dig(doc, "a.x.y") is None
    assert dig(doc, "a.b[x]") is None and dig(doc, "") == doc


def test_direction_places_relative_tokens():
    thu = dt.datetime(2026, 10, 8, 9, 0, tzinfo=_TZ)
    assert token_interval("dow:fri", thu, _TZ, "forward")[0].date() == dt.date(2026, 10, 9)
    assert token_interval("dow:fri", thu, _TZ, "backward")[0].date() == dt.date(2026, 10, 2)
    assert token_interval("dow:thu", thu, _TZ, "backward")[0].date() == dt.date(2026, 10, 8)
    assert token_interval("weekend", thu, _TZ, "forward")[0].date() == dt.date(2026, 10, 10)
    assert token_interval("weekend", thu, _TZ, "backward")[0].date() == dt.date(2026, 10, 3)
    sun = dt.datetime(2026, 10, 11, 9, 0, tzinfo=_TZ)
    assert token_interval("weekend", sun, _TZ, "backward")[0].date() == dt.date(2026, 10, 10)
    lo, hi = token_interval("past_days:7", thu, _TZ, "forward")
    assert lo.date() == dt.date(2026, 10, 1) and hi == thu


_LIST = {"name": "quakes", "kind": "list", "path": "features", "row": [
    {"path": "properties.mag", "type": "number", "label": "Magnitude"},
    {"path": "properties.place", "type": "text", "label": "Place"},
    {"path": "properties.time", "type": "time", "label": "Time"}]}


def _quakes(now: dt.datetime) -> dict:
    stamps = [now - dt.timedelta(days=d) for d in (0.1, 2, 9)]
    return {"features": [{"properties": {"mag": m, "place": p, "time": int(t.timestamp() * 1000)}}
                         for m, p, t in zip((4.6, 2.1, 5.2), ("10 km N of Nome, Alaska", "Reno, Nevada",
                                                              "Anchorage, Alaska"), stamps, strict=True)]}


def test_where_time_order_limit_count():
    now = dt.datetime(2026, 10, 8, 9, 0, tzinfo=_TZ)
    base = {"answer": "quakes", "params": {}, "select": [], "where": [], "time": None, "order": [], "limit": None,
            "agg": "none"}

    def run(ir: dict) -> dict:
        return apply_query({**base, **ir}, _quakes(now), _LIST, now=now, zone=_ZONE, direction="backward")

    assert run({"where": [{"col": "properties.mag", "op": ">", "value": 4}]})["indices"] == [0, 2]
    assert run({"where": [{"col": "*", "op": "names", "value": "alaska"}]})["indices"] == [0, 2]
    assert run({"time": {"from": "past_days:7", "to": "today"}})["indices"] == [0, 1]
    assert run({"time": {"from": "now", "to": "now"}})["indices"] == [0, 1, 2], "an instant list stays whole at now"
    ordered = run({"order": [{"col": "properties.mag", "dir": "desc"}], "limit": 2})
    assert ordered["indices"] == [2, 0] and ordered["count"] is None
    assert run({"agg": "count", "time": {"from": "past_days:7", "to": "today"}})["count"] == 2


def test_hourly_columns_point_cut_keeps_the_current_hour():
    cols = {"name": "waves", "kind": "columns", "axis": {"cell": "hourly.time", "step": "hour"}, "columns": [
        {"path": "hourly.time", "type": "time", "label": "Time"},
        {"path": "hourly.wave_height", "type": "number", "label": "Wave height"}]}
    payload = {"utc_offset_seconds": -14400,
               "hourly": {"time": ["2026-10-08T08:00", "2026-10-08T09:00", "2026-10-08T10:00"],
                          "wave_height": [1.1, 1.2, 1.3]}}
    now = dt.datetime(2026, 10, 8, 9, 30, tzinfo=_TZ)
    ir = {"answer": "waves", "params": {}, "select": [], "where": [], "time": {"from": "now", "to": "now"},
          "order": [], "limit": None, "agg": "none"}
    res = apply_query(ir, payload, cols, now=now, zone=_ZONE, direction="forward")
    assert res["indices"] == [1] and res["rows"][0]["hourly.wave_height"] == 1.2
