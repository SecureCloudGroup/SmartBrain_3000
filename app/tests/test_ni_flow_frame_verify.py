"""§29/§32 frame + verify (WP5): the flow carries the ask's frame (question kind, window, place) from the
intent to locate, pick and build, and a deterministic check stands between a build and its handoff.

Replay tests: the Library records and their declared answers are the v1.2 pack's own
(fixtures/ni_flow_verify/records.json), the responses are recorded samples (samples.json, fetched with
the app's honest identity 2026-09-29..10-03), the page readings are WP4's recorded pages. The clock is
frozen to each sample's fetch time. No network, no model: every model call is a stub.

Labeled sets (written before the code): windows_dev.json / windows_holdout.json (the window parse) and
verify_cases.json + verify_holdout.json (ask x recorded source -> accept / reject).
"""

from __future__ import annotations

import copy
import gzip
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import jail_extract, netguard, ni_flow, pagegraph
from smartbrain_3000 import ni as nimod
from smartbrain_3000.library_index import frame_kind_from_text
from smartbrain_3000.secrets import gen_master_key

_DIR = Path(__file__).parent / "fixtures" / "ni_flow_verify"
RECORDS = json.loads((_DIR / "records.json").read_text())
SAMPLES = json.loads((_DIR / "samples.json").read_text())
TAXONOMY = json.loads((_DIR / "taxonomy.json").read_text())
CASES = json.loads((_DIR / "verify_cases.json").read_text())
# written after the dev table, run once before any change (1 of 14 wrong cards shipped: "gas prices" on
# the CPI), then kept as a regression set
HOLDOUT = json.loads((_DIR / "verify_holdout.json").read_text())
_PAGES = Path(__file__).parent / "fixtures" / "pages"
_MANIFEST = json.loads((_PAGES / "manifest.json").read_text())
_READINGS = json.loads((_PAGES / "readings.json").read_text(encoding="utf-8"))
_NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _local_build_model(monkeypatch):
    from smartbrain_3000 import gateway as _gateway
    monkeypatch.setattr(_gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)


class _Lib:
    """The installed Library as the flow uses it: records with their declared answers, classify by the
    taxonomy's keywords, each subcategory's kinds / policy / expects, and candidates (recorded hint)."""

    def __init__(self, cands: list[dict] | None = None, taxonomy: dict | None = None) -> None:
        self.cands = cands or []
        self.tax = taxonomy if taxonomy is not None else TAXONOMY
        self.hints: list[dict | None] = []

    def get(self, sid: str) -> dict | None:
        return copy.deepcopy(RECORDS.get(sid))

    def answers(self, sid: str) -> list[dict]:
        return copy.deepcopy((RECORDS.get(sid) or {}).get("answers") or [])

    def classify(self, text: str, limit: int = 3) -> list[str]:
        low = " " + re.sub(r"[^a-z0-9 ]+", " ", text.lower()) + " "
        low += " " + " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in low.split()) + " "
        counts = {sub: sum(1 for kw in t["keywords"] if f" {kw} " in low) for sub, t in self.tax.items()}
        return [s for s, n in sorted(counts.items(), key=lambda x: -x[1]) if n][:limit]

    def taxonomy(self) -> list[dict]:
        cats: dict[str, dict] = {}
        for sub, t in self.tax.items():
            cat, _, sid = sub.partition("/")
            cats.setdefault(cat, {"id": cat, "subcategories": []})["subcategories"].append(
                {"id": sid, "keywords": t["keywords"]})
        return list(cats.values())

    def subcategory(self, sub: str) -> dict | None:
        t = self.tax.get(sub)
        return None if t is None else {"kinds": t["kinds"], "policy": t["policy"], "expects": t["expects"]}

    def candidates(self, ask: str, limit: int = 3, hint: dict | None = None):
        self.hints.append(hint)
        return list(self.cands), []


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


def _freeze(monkeypatch, iso: str) -> datetime:
    now = datetime.fromisoformat(iso).astimezone(_NY)
    monkeypatch.setattr(nimod, "_clock", lambda: now)
    return now


def _intent(ask: str, subject: str, wants: list[str], place: str | None = None,
            frame_kind: str | None = None) -> dict:
    """What stage 1 hands on: the model's blanks plus code's own parse of the kind and the window."""
    return {"kind": "external_data", "subject": subject, "cadence_minutes": 15, "wants": wants,
            "threshold": None, "place": place, "display_hint": "value",
            "frame_kind": frame_kind_from_text(ask) or frame_kind, "window": ni_flow._window_from_text(ask)}


def _row(key: str) -> dict:
    s = SAMPLES[key]
    rec = RECORDS[s["source_id"]]
    takes_place = any((p.get("fill") or {}).get("resolver") in ("place", "zip") for p in rec["access"]["params"])
    return {"source_id": s["source_id"], "url": s["url"], "title": rec["name"], "host": "x",
            "provider": rec["provider"]["name"], "authority": rec["provider"].get("authority", ""),
            "categories": rec["categories"], "params": s.get("params") or {},
            "format": "csv" if "fredgraph" in s["url"] else "json",
            "scope": "place" if takes_place else "global"}


def _pick(store, monkeypatch, key: str, ask: str, intent: dict, rest: tuple = (),
          lib: _Lib | None = None) -> tuple[str, str]:
    """A card paused at the pick with ``key``'s source first (and ``rest`` after it), tapped."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib or _Lib())
    item_id = ni_flow.create_shell_item(store, ask)
    rows = [_row(key), *rest]
    rec = ni_flow._make_record(ask, "source", intent=intent)
    rec["_ranked_library"] = rows
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, rows[0]["url"], rows[0])
    return item_id, rows[0]["url"]


def _no_model(prompt: str) -> str:
    raise AssertionError("a declared-answers build makes no model call")


def _run(store, monkeypatch, key: str, ask: str, intent: dict, **kw) -> dict:
    _freeze(monkeypatch, SAMPLES[key]["fetched"])
    item_id, url = _pick(store, monkeypatch, key, ask, intent, **kw)
    out = ni_flow._sample_and_map(store, item_id, ask, intent, url, _no_model,
                                  lambda _u: copy.deepcopy(SAMPLES[key]["sample"]))
    out["_item"] = item_id
    return out


def _shown(store, item_id: str) -> list[str]:
    """The card's texts as it renders (the preview bound into the frozen scene)."""
    spec = store.get_item(item_id)["spec"]
    preview = store.read_snapshot(item_id, "preview_data")["payload"]
    bound = nimod.bind_scene(spec["scene"], preview)
    out, stack = [], [bound]
    while stack:
        node = stack.pop(0)
        if node.get("type") in ("text", "number"):
            out.append(f"{node['value']}{node.get('unit') or ''}")
        stack.extend(node.get("children") or [])
    return out


def _ops(store, item_id: str) -> list[dict]:
    spec = store.get_item(item_id)["spec"]
    return [op for st in spec["pipeline"] if st.get("op") == "transform" for op in st["apply"]]


# ---- stage 1: the frame ------------------------------------------------------------------------------

def _window_rate(rows: list) -> tuple[float, list]:
    misses = [(ask, want, ni_flow._window_from_text(ask)) for ask, want in rows
              if ni_flow._window_from_text(ask) != want]
    return 1 - len(misses) / len(rows), misses


def test_the_window_parse_on_the_dev_set() -> None:
    rows = json.loads((_DIR / "windows_dev.json").read_text())
    rate, misses = _window_rate(rows)
    assert len(rows) >= 100 and rate >= 0.98, misses


def test_the_window_parse_on_the_holdout_set() -> None:
    """Written with the dev set, never used to shape the parse."""
    rows = json.loads((_DIR / "windows_holdout.json").read_text())
    rate, misses = _window_rate(rows)
    assert len(rows) >= 60 and rate >= 0.95, misses


def test_the_window_is_a_closed_enum() -> None:
    for ask, _ in json.loads((_DIR / "windows_dev.json").read_text()):
        w = ni_flow._window_from_text(ask)
        assert w is None or nimod._WINDOW_RE.fullmatch(w), (ask, w)


def test_code_owns_the_frame_kind_and_the_window() -> None:
    def model(reply: dict):
        return lambda _p: json.dumps(reply)
    base = {"kind": "external_data", "subject": "Buffalo Bills", "cadence_minutes": 15, "wants": ["score"],
            "threshold": None, "place": None, "display_hint": "value"}
    # the model says current_value; the words say a result: code wins (as with the cadence)
    intent = ni_flow.stage_intent("Bills score", model({**base, "frame_kind": "current_value"}))
    assert intent["frame_kind"] == "result" and intent["window"] is None
    # no cue in the words: the model's kind stands when it is one of the closed kinds
    intent = ni_flow.stage_intent("Buffalo Bills", model({**base, "frame_kind": "next_event"}))
    assert intent["frame_kind"] == "next_event"
    intent = ni_flow.stage_intent("Buffalo Bills", model({**base, "frame_kind": "a vibe"}))
    assert intent["frame_kind"] is None
    intent = ni_flow.stage_intent("weather this weekend in Austin", model({**base, "frame_kind": None}))
    assert intent["window"] == "weekend"
    assert "frame_kind" in ni_flow._INTENT_PROMPT


def test_locate_gets_the_intents_frame_at_every_call_site(monkeypatch) -> None:
    lib = _Lib(cands=[{**_row("open_meteo_charleston"), "label": "", "choice": False, "status": "ok",
                       "tier": "curated", "needs_key": None, "needs_contact": False, "frame_kind": None}])
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    store = _store()
    item_id = ni_flow.create_shell_item(store, "high in Denver on Saturday")
    intent = _intent("high in Denver on Saturday", "weather", ["high temperature"], "Denver")
    ni_flow._flow_write(store, item_id, ni_flow._make_record("high in Denver on Saturday", "source", intent=intent))
    out = ni_flow._pause_source_pick(store, item_id, "high in Denver on Saturday", intent, _no_model)
    assert out["_ranked_library"][0]["scope"] == "place"
    ni_flow.reenter_source_pick(store, item_id, "pick again")
    want = {"subject": "weather", "wants": ["high temperature"], "place": "Denver", "frame_kind": None,
            "window": "dow:sat"}
    assert lib.hints == [want, want]


def test_no_library_row_goes_to_the_web(monkeypatch) -> None:
    """Locate offering no row (whatever its skip reasons say) sends the pick to the web stage."""
    class Unsure(_Lib):
        def candidates(self, ask, limit=3, hint=None):
            return [], ["not about what was asked"]
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: Unsure())
    went: list[str] = []
    monkeypatch.setattr(ni_flow, "_pause_with_web", lambda *a, **k: went.append("web") or None)
    store = _store()
    item_id = ni_flow.create_shell_item(store, "zork status")
    ni_flow._pause_source_pick(store, item_id, "zork status", _intent("zork status", "zork", ["status"]), _no_model)
    assert went == ["web"]


# ---- §32 pick: window, measure, question form ----------------------------------------------------------

def _answers(sid: str) -> list[dict]:
    return [a for a in (ni_flow._clean_answer(x) for x in RECORDS[sid]["answers"]) if a is not None]


def test_v12_answers_are_clean_with_their_window_axis_measure_and_tbd() -> None:
    for sid, rec in RECORDS.items():
        cleaned = _answers(sid)
        assert len(cleaned) == len(rec["answers"]), sid
    by = {a["name"]: a for a in _answers("open-meteo-forecast")}
    assert by["high_today"]["window"] == "today" and by["temperature"]["measure"] == "temperature"
    assert by["daily_forecast"]["axis"] == {"cell": "daily.time", "step": "day"}
    sched = {a["name"]: a for a in _answers("mlb-team-schedule")}
    assert sched["next_game_time"]["tbd_if"] == {"path": "dates[0].games[0].status.startTimeTBD", "equals": True}
    assert sched["upcoming"]["cells"][0]["tbd_if"]["path"] == "games[0].status.startTimeTBD"
    good = RECORDS["open-meteo-forecast"]["answers"][0]
    assert ni_flow._clean_answer({**good, "window": "someday"}) is None
    assert ni_flow._clean_answer({**good, "measure": "vibes"}) is None
    daily = next(a for a in RECORDS["open-meteo-forecast"]["answers"] if a["name"] == "daily_forecast")
    assert ni_flow._clean_answer({**daily, "axis": {"cell": "daily.nope", "step": "day"}}) is None
    assert ni_flow._clean_answer({**daily, "axis": {"cell": "daily.time", "step": "week"}}) is None


@pytest.mark.parametrize(("ask", "window", "picked"), [
    ("weather this weekend in Austin", "weekend", ["daily_forecast"]),
    ("high in Denver on Saturday", "dow:sat", ["daily_forecast"]),
    ("weather in Denver tomorrow", "tomorrow", ["daily_forecast"]),
    ("will it rain tomorrow in Seattle", "tomorrow", ["rain_tomorrow"]),
    ("is it gonna storm in Tulsa tonight", "tonight", ["tonight"]),
    ("frost tonight in Boise", "tonight", ["tonight"]),
    ("snow this weekend in Tahoe", "weekend", ["snow_forecast"]),
    ("NYC weather", None, ["temperature", "conditions", "high_today", "low_today"]),
    ("Houston weather today", "today", ["temperature", "conditions", "high_today", "low_today"]),
])
def test_the_pick_serves_the_asked_window(ask, window, picked) -> None:
    assert ni_flow._window_from_text(ask) == window
    chosen = ni_flow.select_answers(_answers("open-meteo-forecast"), ask, [], window=window)
    assert [a["name"] for a in chosen] == picked
    assert "high_today" not in [a["name"] for a in chosen] or window in (None, "today")


def test_no_answer_for_the_window_or_the_named_measure_is_nothing() -> None:
    marine = _answers("open-meteo-marine")
    assert ni_flow.select_answers(marine, "wind at Virginia Beach", ["wind speed"]) == []
    assert ni_flow.select_answers(marine, "surf this weekend at Virginia Beach", [], window="weekend") \
        == [next(a for a in marine if a["name"] == "waves_hourly")]
    iss = _answers("wheretheiss-now")
    assert ni_flow.select_answers(iss, "ISS position tomorrow", [], window="tomorrow") == []


def test_existence_takes_the_list_and_only_how_many_takes_the_count() -> None:
    storms = _answers("nhc-current-storms")
    assert [a["name"] for a in ni_flow.select_answers(storms, "any hurricanes headed for Tampa?", [])] == ["storms"]
    assert [a["name"] for a in ni_flow.select_answers(storms, "any hurricanes", ["hurricanes"])] == ["storms"]
    assert [a["name"] for a in ni_flow.select_answers(storms, "how many hurricanes are active", [])] \
        == ["storm_count"]
    quakes = _answers("usgs-quakes-near")
    assert [a["name"] for a in ni_flow.select_answers(quakes, "any earthquakes near LA today", [],
                                                      window="today")] == ["quakes"]


@pytest.mark.parametrize(("ask", "days"), [
    ("weather this weekend in Austin", ["2026-10-03", "2026-10-04"]),
    ("high in Denver on Saturday", ["2026-10-03"]),
    ("weather in Denver tomorrow", ["2026-09-30"]),
])
def test_a_windowed_build_keeps_only_the_asked_days_on_every_run(monkeypatch, ask, days) -> None:
    store = _store()
    out = _run(store, monkeypatch, "open_meteo_charleston", ask, _intent(ask, "weather", ["forecast"], "Austin"))
    assert out["state"] == "ready", out["notes"]
    ops = _ops(store, out["_item"])
    assert {"fn": "top_n", "field": "rows", "n": 7} not in ops
    win = next(op for op in ops if op["fn"] == "window")
    assert win["window"] == ni_flow._window_from_text(ask) and win["zone"] == "zone"
    raw = nimod.run_pipeline(store.get_item(out["_item"])["spec"]["pipeline"][:1] + [
        {"op": "transform", "apply": [op for op in ops if op["fn"] in ("zip", "window")]}],
        copy.deepcopy(SAMPLES["open_meteo_charleston"]["sample"]))
    assert [r[next(iter(r))] for r in raw["rows"]] == days
    assert not any("High today" in t for t in _shown(store, out["_item"]))


def test_tonight_cuts_the_hours_to_tonight(monkeypatch) -> None:
    store = _store()
    ask = "is it gonna storm in Tulsa tonight"
    out = _run(store, monkeypatch, "open_meteo_charleston", ask, _intent(ask, "weather", ["storm chance"], "Tulsa"))
    assert out["state"] == "ready", out["notes"]
    preview = store.read_snapshot(out["_item"], "preview_data")["payload"]
    assert len(preview["rows"]) == 12  # 6 PM .. 5 AM, the source's own clock


# D5 (review 2026-10-03): a forward window never cuts rows dated in the past — "gas prices this week" on FRED's
# recent observations left nothing ("the list is empty here"); the card shows the latest rows instead.

@pytest.mark.parametrize("window", ["next_days:7", "tomorrow", "tonight", "weekend", "dow:fri", "next_days:3"])
def test_a_forward_window_on_past_dated_rows_shows_the_latest_rows(monkeypatch, window) -> None:
    _freeze(monkeypatch, SAMPLES["cpi"]["fetched"])
    recent = next(a for a in _answers("fred-cpiaucsl") if a["name"] == "recent")
    built = ni_flow.build_from_answers([recent], copy.deepcopy(SAMPLES["cpi"]["sample"]), "CPI", window=window)
    ops = [op for st in built["pipeline"] if st.get("op") == "transform" for op in st["apply"]]
    assert not any(op["fn"] == "window" for op in ops)
    dates = [r["observation_date"] for r in built["preview_payload"]["rows"]]
    assert dates[0] == "Aug 1" and SAMPLES["cpi"]["sample"]["rows"][0]["observation_date"] == "2026-08-01"


def test_a_forward_window_through_the_flow_on_past_dated_rows(monkeypatch) -> None:
    store = _store()
    ask = "consumer price index this week"
    out = _run(store, monkeypatch, "cpi", ask, _intent(ask, "CPI", ["index value"]))
    assert out["state"] == "ready", out["notes"]


def test_a_past_window_still_cuts_past_dated_rows(monkeypatch) -> None:
    """"today" isn't forward: it still cuts (a monthly series has no row for today)."""
    _freeze(monkeypatch, SAMPLES["cpi"]["fetched"])
    recent = next(a for a in _answers("fred-cpiaucsl") if a["name"] == "recent")
    with pytest.raises(ValueError, match="the list is empty here"):
        ni_flow.build_from_answers([recent], copy.deepcopy(SAMPLES["cpi"]["sample"]), "CPI", window="today")


def test_day_rows_show_tonight_as_their_today(monkeypatch) -> None:
    """Rows indexed by day answer "tonight" with today's row when nothing hourly serves it."""
    _freeze(monkeypatch, SAMPLES["open_meteo_charleston"]["fetched"])
    daily = next(a for a in _answers("open-meteo-forecast") if (a.get("axis") or {}).get("step") == "day")
    built = ni_flow.build_from_answers([daily], copy.deepcopy(SAMPLES["open_meteo_charleston"]["sample"]), "Weather",
                                       window="tonight")
    win = [op for st in built["pipeline"] if st.get("op") == "transform" for op in st["apply"] if op["fn"] == "window"]
    assert [op["window"] for op in win] == ["today"]
    assert [r["day"] for r in built["preview_payload"]["rows"]] == ["Tue Sep 29"]  # the fetch day, Charleston


def test_a_tbd_start_shows_time_tbd(monkeypatch) -> None:
    _freeze(monkeypatch, SAMPLES["mets_schedule"]["fetched"])
    sample = copy.deepcopy(SAMPLES["mets_schedule"]["sample"])
    sample["dates"][1]["games"][0]["status"]["startTimeTBD"] = True
    upcoming = next(a for a in _answers("mlb-team-schedule") if a["name"] == "upcoming")
    built = ni_flow.build_from_answers([upcoming], sample, "Braves")
    time_op = next(op for st in built["pipeline"] if st["op"] == "transform" for op in st["apply"]
                   if op["fn"] == "time")
    assert time_op["unless"] == "games[0].status.startTimeTBD"
    shown = [r["games"][0]["gameDate"] for r in built["preview_payload"]["rows"]]
    assert shown[1].endswith("time TBD") and not shown[0].endswith("time TBD")
    value = next(a for a in _answers("mlb-team-schedule") if a["name"] == "next_game_time")
    sample["dates"][0]["games"][0]["status"]["startTimeTBD"] = True
    built = ni_flow.build_from_answers([value], sample, "Braves")
    assert built["preview_payload"]["next_game_time"].endswith("time TBD")


# ---- the verify step (C8) ------------------------------------------------------------------------------

def _case_intent(case: dict) -> dict:
    return _intent(case["ask"], case["subject"], case["wants"], case["place"])


@pytest.mark.parametrize("case", CASES + HOLDOUT, ids=lambda c: f"{c['expect']}:{c['ask']}:{c['sample']}")
def test_the_verify_table(monkeypatch, case) -> None:
    store = _store()
    out = _run(store, monkeypatch, case["sample"], case["ask"], _case_intent(case))
    notes = " | ".join(out["notes"])
    if case["expect"] == "accept":
        assert out["state"] == "ready", notes
    else:
        assert out["state"] != "ready", f"handed off: {notes}"
        assert store.get_item(out["_item"])["spec"].get("_shell") is True
    if case["note"]:
        assert case["note"] in notes, notes


def test_verify_table_rates(monkeypatch) -> None:
    """Every wrong card refused, every right card built (the C8 gate: 0 wrong accepted, >= 95% right)."""
    wrong_accepted, right_refused, rights = [], [], 0
    for case in CASES + HOLDOUT:
        store = _store()
        with monkeypatch.context() as m:
            out = _run(store, m, case["sample"], case["ask"], _case_intent(case))
        if case["expect"] == "reject" and out["state"] == "ready":
            wrong_accepted.append(case["ask"])
        if case["expect"] == "accept":
            rights += 1
            if out["state"] != "ready":
                right_refused.append(case["ask"])
    assert wrong_accepted == [] and len(right_refused) <= 0.05 * rights, (wrong_accepted, right_refused)


def test_a_refused_build_moves_to_the_next_source_with_the_reason(monkeypatch) -> None:
    store = _store()
    ask = "Bills score"
    nxt = {**_row("chiefs"), "url": "https://example.org/other", "provider": "Other"}
    out = _run(store, monkeypatch, "bills", ask, _intent(ask, "Buffalo Bills", ["score"]), rest=(nxt,))
    assert out["state"] == "source"
    assert [r["url"] for r in out["_ranked_library"]] == ["https://example.org/other"]
    assert "TheSportsDB doesn't answer this" in " | ".join(out["notes"])


def test_a_place_the_source_ignores_under_a_place_free_policy_is_disclosed(monkeypatch) -> None:
    tax = copy.deepcopy(TAXONOMY)
    tax["hazards/tropical_storms"]["policy"]["match"] = "none"
    store = _store()
    ask = "any hurricanes headed for Tampa?"
    out = _run(store, monkeypatch, "storms", ask, _intent(ask, "hurricanes", ["hurricanes"], "Tampa"),
               lib=_Lib(taxonomy=tax))
    assert out["state"] == "ready"
    notes = " | ".join(out["notes"])
    assert "isn't specific to Tampa" in notes
    assert "rows" in store.read_snapshot(out["_item"], "preview_data")["payload"]  # the list, not a bare count


def test_a_partial_card_says_what_its_kind_of_answer_lacks(monkeypatch) -> None:
    store = _store()
    ask = "how's the surf at Virginia Beach"
    out = _run(store, monkeypatch, "marine_charleston", ask, _intent(ask, "surf", ["wave height"], "Virginia Beach"))
    assert out["state"] == "ready"
    assert "this source doesn't report: wind" in " | ".join(out["notes"])


def test_a_next_event_that_already_passed_is_refused(monkeypatch) -> None:
    store = _store()
    ask = "Chiefs next game"
    sample = copy.deepcopy(SAMPLES["chiefs"]["sample"])
    sample["events"][0]["strTimestamp"] = "2026-09-20T17:00:00"
    _freeze(monkeypatch, SAMPLES["chiefs"]["fetched"])
    item_id, url = _pick(store, monkeypatch, "chiefs", ask, _intent(ask, "Kansas City Chiefs", ["next game"]))
    out = ni_flow._sample_and_map(store, item_id, ask, _intent(ask, "Kansas City Chiefs", ["next game"]),
                                  url, _no_model, lambda _u: sample)
    assert out["state"] != "ready" and "already passed" in " | ".join(out["notes"])


def test_a_next_event_time_is_marked_next_on_the_card(monkeypatch) -> None:
    store = _store()
    ask = "Chiefs next game"
    out = _run(store, monkeypatch, "chiefs", ask, _intent(ask, "Kansas City Chiefs", ["next game"]))
    assert out["state"] == "ready"
    spec = store.get_item(out["_item"])["spec"]
    nodes = [c for c in spec["scene"]["children"] if c.get("next")]
    assert [n["value"]["$bind"] for n in nodes] == ["start"]


# ---- the fetch contract (C12) and the sealed lookup (C13) ------------------------------------------------

def test_a_library_json_row_that_serves_html_moves_on_never_to_the_page_reader(monkeypatch) -> None:
    store = _store()
    ask = "NYC weather"
    _freeze(monkeypatch, SAMPLES["open_meteo_charleston"]["fetched"])
    nxt = {**_row("chiefs"), "url": "https://example.org/other", "provider": "Other"}
    item_id, url = _pick(store, monkeypatch, "open_meteo_charleston", ask,
                         _intent(ask, "weather", ["temperature"], "New York"), rest=(nxt,))
    monkeypatch.setattr(ni_flow, "_build_page_card", lambda *a, **k: pytest.fail("a Library row read as a page"))

    def html(_u):
        raise netguard.FetchError("an HTML page", kind="not_json")
    out = ni_flow._sample_and_map(store, item_id, ask, _intent(ask, "weather", ["temperature"]), url, _no_model, html)
    assert out["state"] == "source" and "did not return its data" in " | ".join(out["notes"])


POINTS = {"properties": {"gridId": "CHS", "gridX": 87, "gridY": 77}}
POINTS_URL = "https://api.weather.gov/points/32.7765,-79.9311"
TEMPLATE = "https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast"
LOOKUP = [{"url": POINTS_URL, "path": "properties.gridId", "param": "office"},
          {"url": POINTS_URL, "path": "properties.gridX", "param": "grid_x"},
          {"url": POINTS_URL, "path": "properties.gridY", "param": "grid_y"}]


def _nws_pick(store, monkeypatch, lookup):
    _freeze(monkeypatch, SAMPLES["nws_forecast_chs"]["fetched"])
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Lib())
    ask = "Charleston forecast tonight"
    item_id = ni_flow.create_shell_item(store, ask)
    row = {**_row("nws_forecast_chs"), "url": TEMPLATE, "lookup": lookup, "scope": "place"}
    nxt = {**_row("open_meteo_charleston")}
    rec = ni_flow._make_record(ask, "source", intent=_intent(ask, "weather", ["forecast"], "Charleston"))
    rec["_ranked_library"] = [row, nxt]
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, TEMPLATE, rec["_ranked_library"][0])
    return item_id, ask


def test_a_sealed_same_host_lookup_runs_before_the_sample_and_freezes_the_address(monkeypatch) -> None:
    store = _store()
    item_id, ask = _nws_pick(store, monkeypatch, LOOKUP)
    assert ni_flow._flow_read(store, item_id)["_library_lookup"] == LOOKUP
    fetched: list[str] = []

    def fetch(u):
        fetched.append(u)
        return copy.deepcopy(POINTS if u == POINTS_URL else SAMPLES["nws_forecast_chs"]["sample"])
    out = ni_flow._sample_and_map(store, item_id, ask, _intent(ask, "weather", ["forecast"], "Charleston"),
                                  TEMPLATE, _no_model, fetch)
    final = "https://api.weather.gov/gridpoints/CHS/87,77/forecast"
    assert fetched == [POINTS_URL, final]
    assert out["state"] == "ready", out["notes"]
    assert store.get_item(item_id)["spec"]["source"]["url"] == final


def test_a_candidate_row_keeps_its_lookup_only_on_its_own_host() -> None:
    row = {**_row("nws_forecast_chs"), "url": TEMPLATE, "title": "NWS", "authority": "official"}
    assert ni_flow._library_rows([{**row, "lookup": LOOKUP}])[0]["lookup"] == LOOKUP
    assert ni_flow._library_rows([{**row, "lookup": [{**LOOKUP[0], "url": "https://evil.example.org/p"}]}]) == []
    assert ni_flow._library_rows([row])[0]["lookup"] is None


def test_a_lookup_that_leaves_the_host_moves_on(monkeypatch) -> None:
    store = _store()
    bad = [{**LOOKUP[0], "url": "https://evil.example.org/points"}]
    item_id, ask = _nws_pick(store, monkeypatch, bad)
    out = ni_flow._sample_and_map(store, item_id, ask, _intent(ask, "weather", ["forecast"]), TEMPLATE, _no_model,
                                  lambda _u: pytest.fail("nothing is fetched off the source's host"))
    assert out["state"] == "source" and "pick another source" in " | ".join(out["notes"])


# ---- the page path (C9-C11) ------------------------------------------------------------------------------

_EXTRACTS: dict[str, dict] = {}


def _extract(name: str) -> dict:
    if name not in _EXTRACTS:
        raw = gzip.decompress((_PAGES / f"{name}.html.gz").read_bytes())
        _EXTRACTS[name] = jail_extract.extract(raw, _MANIFEST[name]["url"])
    return copy.deepcopy(_EXTRACTS[name])


def _page_model(reading: dict, serves: bool = True, wrong: list | None = None):
    def model(prompt: str) -> str:
        if "You transform an untrusted JSON value" in prompt:
            return json.dumps(reading)
        if "You are verifying a data card" in prompt:
            return json.dumps({"serves": serves, "gaps": [], "wrong": wrong or []})
        return "{}"  # the compiler: no pick, so the interpreted tier reads the page
    return model


def _web_pick(store, monkeypatch, case: dict, pages: list[str]) -> tuple[str, dict]:
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", None)
    monkeypatch.setattr(nimod, "_clock", lambda: datetime.fromisoformat(case["now"]))
    by_url = {_MANIFEST[p]["url"]: p for p in pages}
    monkeypatch.setattr(nimod, "_fetch_http_page",
                        lambda source, item_id, secrets, full=False: _extract(by_url[source["url"]]))
    item_id = ni_flow.create_shell_item(store, case["ask"])
    intent = _intent(case["ask"], case["subject"], case["wants"], frame_kind=case["frame_kind"])
    intent["frame_kind"] = case["frame_kind"]
    rec = ni_flow._make_record(case["ask"], "source", intent=intent)
    rec["_ranked_search"] = [{"title": p, "host": p, "url": _MANIFEST[p]["url"], "evidence": []} for p in pages]
    ni_flow._flow_write(store, item_id, rec)
    return item_id, intent


@pytest.mark.parametrize("case", [c for c in _READINGS if all(isinstance(v, str) for v in c["preview"].values())],
                         ids=lambda c: c["id"])
def test_page_readings_through_the_flow(monkeypatch, case) -> None:
    """WP4's labeled readings, now through the flow's own page build: a refused reading never ships."""
    store = _store()
    item_id, intent = _web_pick(store, monkeypatch, case, [case["page"]])
    url = _MANIFEST[case["page"]]["url"]
    out = ni_flow._build_page_card(store, item_id, case["ask"], intent, url, _page_model(case["preview"]))
    if case["expect"] == "reject":
        assert out["state"] != "ready", f"{case['id']} shipped: {case['preview']}"
    else:
        assert out["state"] == "ready", f"{case['id']}: {out['notes']} {out.get('error')}"


@pytest.mark.parametrize(("rid", "reason"), [("reddit_title", "couldn't be read"),
                                             ("flightaware_modal", "couldn't be read"),
                                             ("aws_docs_heading", "")])
def test_a_refused_page_reading_moves_to_the_next_page(monkeypatch, rid, reason) -> None:
    case = next(c for c in _READINGS if c["id"] == rid)
    store = _store()
    item_id, intent = _web_pick(store, monkeypatch, case, [case["page"], "slack_status"])
    url = _MANIFEST[case["page"]]["url"]
    out = ni_flow._build_page_card(store, item_id, case["ask"], intent, url, _page_model(case["preview"]))
    assert out["state"] == "source"
    assert [r["url"] for r in out["_ranked_search"]] == [_MANIFEST["slack_status"]["url"]]
    assert "didn't show what you asked" in " | ".join(out["notes"]) and reason in " | ".join(out["notes"])


def test_an_unreadable_model_reply_tries_the_next_page(monkeypatch) -> None:
    case = next(c for c in _READINGS if c["id"] == "slack_ok")
    store = _store()
    item_id, intent = _web_pick(store, monkeypatch, case, [case["page"], "githubstatus"])
    out = ni_flow._build_page_card(store, item_id, case["ask"], intent, _MANIFEST[case["page"]]["url"],
                                   lambda _p: "not json at all")
    assert out["state"] == "source" and len(out["_ranked_search"]) == 1


def test_the_judge_saying_it_does_not_serve_is_binding(monkeypatch) -> None:
    case = next(c for c in _READINGS if c["id"] == "slack_ok")
    store = _store()
    item_id, intent = _web_pick(store, monkeypatch, case, [case["page"], "githubstatus"])
    out = ni_flow._build_page_card(store, item_id, case["ask"], intent, _MANIFEST[case["page"]]["url"],
                                   _page_model(case["preview"], serves=False))
    assert out["state"] == "source"


def _graph(name: str) -> dict:
    return pagegraph.graph_from_extract(_MANIFEST[name]["url"], _extract(name))


def test_the_web_rank_drops_unreadable_pages_and_marks_the_subjects_own_site(monkeypatch) -> None:
    """The subject's own site is marked official (the rank prompt shows it); it sorts first only with
    evidence that its page serves the ask (D10) — the recorded status page shows none for "status"."""
    pages = ["isitdown_slack", "reddit_worldnews_new", "lagcheck_slack", "slack_status"]
    graphs = {_MANIFEST[p]["url"]: _graph(p) for p in pages}
    monkeypatch.setattr(pagegraph, "fetch_page_graph", lambda url: graphs[url])
    rows = [{"title": p, "host": netguard_host(_MANIFEST[p]["url"]), "url": _MANIFEST[p]["url"], "snippet": ""}
            for p in pages]
    out = ni_flow._s2_evaluate(rows, {"subject": "Slack", "wants": ["status"]}, "is Slack down")
    assert _MANIFEST["reddit_worldnews_new"]["url"] not in [r["url"] for r in out]
    own = next(r for r in out if r["url"] == _MANIFEST["slack_status"]["url"])
    assert own["authority"] == "official" and not own["evidence"]
    assert out[0]["evidence"] and out[0]["authority"] == ""


def netguard_host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""


def test_the_rank_prompt_shows_authority() -> None:
    seen: list[str] = []
    rows = [{"title": "Slack Status", "host": "slack-status.com", "url": "https://slack-status.com/",
             "snippet": "", "authority": "official"},
            {"title": "Is Slack down?", "host": "isitdown.now", "url": "https://isitdown.now/x", "snippet": ""}]
    ni_flow.rank_web_rows(rows, "is Slack down", {"subject": "Slack"}, lambda p: seen.append(p) or "{}")
    assert "authority" in seen[0] and "| official |" in seen[0]


def test_an_official_page_without_evidence_does_not_lead_a_page_with_evidence(monkeypatch) -> None:
    """D10: the subject's own site leads only when its page shows it serves the ask; a zero-evidence
    official page ranks by fitness like any other row. A capital the user typed counts as the name."""
    prose = " The page has more words here so it reads as a real page with content on it." * 4
    graphs = {
        "https://slack.com/": {"url": "https://slack.com/", "title": "Slack", "entities": [], "tables": [],
                               "feeds": [], "meta": {}, "outline": [], "text": "Work happens here." + prose},
        "https://checker.example.org/slack": {
            "url": "https://checker.example.org/slack", "title": "Slack status",
            "entities": [{"type": "Event", "name": "Slack status: all systems operational", "time": "7:12"}],
            "tables": [], "feeds": [], "meta": {}, "outline": [], "text": "status" + prose},
    }
    monkeypatch.setattr(pagegraph, "fetch_page_graph", lambda url: graphs[url])
    rows = [{"title": "Slack", "host": "slack.com", "url": "https://slack.com/", "snippet": ""},
            {"title": "Checker", "host": "checker.example.org", "url": "https://checker.example.org/slack",
             "snippet": ""}]
    out = ni_flow._s2_evaluate(rows, {"subject": "slack", "wants": ["status"]}, "is Slack down")
    assert out[0]["host"] == "checker.example.org" and out[0]["evidence"]
    assert out[1]["authority"] == "official" and not out[1].get("evidence")
