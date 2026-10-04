"""Review-2 fixes (2026-10-04): a correct live card or an honest gap — never a confidently wrong card.

Each test names the finding (F2 F4 F5-gap F6 F7 F10 F12 F14 D13) and the one line the fix stands on."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.secrets import gen_master_key

_PACK = Path(__file__).parent / "fixtures" / "ni_flow_verify" / "pack_subjects.json"
_SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "ni_flow_verify" / "samples.json").read_text())
_NOW = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)


class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


def _build_pack_index(tmp: Path) -> library_index.LibraryIndex:
    pack = json.loads(_PACK.read_text())
    src = tmp / "src.duckdb"
    con = duckdb.connect(str(src))
    for table, cols in pack["schema"].items():
        con.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in cols)})")
        for row in pack["rows"][table]:
            con.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in cols)})", row)
    con.close()
    payload = gzip.compress(src.read_bytes())
    idx = library_index.LibraryIndex(tmp / "data", netguard_mod=_Net(payload),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(payload).hexdigest()})
    idx.install()
    return idx


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> library_index.LibraryIndex:
    return _build_pack_index(tmp_path_factory.mktemp("review2"))


@pytest.fixture(autouse=True)
def _wired(lib, monkeypatch):
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


def _intent(ask: str, subject: str, wants: list[str], place: str | None = None) -> dict:
    return {"kind": "external_data", "subject": subject, "cadence_minutes": 15, "wants": wants, "threshold": None,
            "place": place, "display_hint": "value", "frame_kind": library_index.frame_kind_from_text(ask),
            "window": ni_flow._window_from_text(ask)}


# ---- F2: _other_subject must subtract only readings this source took / covers --------------------

def test_f2_a_leagues_source_still_refuses_a_soccer_team_though_another_rows_reading_names_marlins() -> None:
    """mlb-schedule covers MLB; the pick also has an mlb-team-schedule row whose reading is "Miami Marlins
    (mlb)". _other_subject used to subtract readings from EVERY row of the pick, so "Inter Miami" slipped
    past (its "miami" was excused by the Marlins reading). The subtraction must stay local to this source."""
    ask = "Inter Miami games today"
    intent = _intent(ask, "Inter Miami", ["games"])
    answers = ni_flow._library_answers("mlb-schedule")
    assert answers, "mlb-schedule has answers"
    chosen = ni_flow.select_answers(answers, ask, intent["wants"], intent["window"], intent["frame_kind"])
    assert chosen, "something is chosen for the ask"
    # another row of the SAME pick reads the subject itself ("Inter Miami CF" from a soccer-league
    # source); its reading used to be subtracted globally and excused mlb-schedule.
    rows = [{"source_id": "mlb-schedule", "url": "https://example.org/a", "label": "", "params": {},
             "scope": "global"},
            {"source_id": "mlb-team-schedule", "url": "https://example.org/b",
             "label": "Miami Marlins (mlb)", "params": {"team": "146"}, "scope": "global"},
            {"source_id": "thesportsdb-team-next", "url": "https://example.org/c",
             "label": "Inter Miami CF (usa.1)", "params": {"team": "137699"}, "scope": "global"}]
    store = _store()
    item_id = ni_flow.create_shell_item(store, ask)
    rec = ni_flow._make_record(ask, "source", intent=intent)
    rec["_ranked_library"] = rows
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, rows[0]["url"], rows[0])
    live = {**ni_flow._flow_read(store, item_id), "_library_source": "mlb-schedule"}
    built = {"chosen": chosen, "answers": answers, "preview_payload": {},
             "unanswered": ni_flow._unanswered_wants(answers, ask, intent["wants"], [])}
    reasons, _notes = ni_flow._verify_frame(ni_flow._frame_of(ask, intent), ni_flow._picked_source(live),
                                            {}, built, ask, intent, _NOW)
    assert reasons, f"mlb-schedule shipped for Inter Miami: {reasons}"


# ---- F4: the "any …?" exists rule may not override an answer that strictly serves the window -----

@pytest.mark.parametrize("ask, window", [("any aurora tonight?", "tonight"),
                                          ("any chance of northern lights tomorrow", "tomorrow")])
def test_f4_any_aurora_takes_the_forecast_not_a_may_be_empty_list(ask, window) -> None:
    """swpc-kp-forecast's "today" column answer is may_be_empty; its "forecast" rows strictly serve
    tonight / tomorrow. Without the fix, the exists rule picked may_be_empty regardless of score."""
    answers = ni_flow._library_answers("swpc-kp-forecast")
    assert answers, "swpc-kp-forecast has answers"
    chosen = ni_flow.select_answers(answers, ask, ["kp"], window, None)
    assert chosen, "something is chosen"
    assert chosen[0]["name"] == "forecast", (ask, [a["name"] for a in chosen])


# ---- F5-gap: _frame_gap must judge the FIRST SHOWN moment, not require ALL to be past -----------

def test_f5_frame_gap_refuses_when_the_first_shown_time_is_past_even_with_future_rows() -> None:
    """A "next game" list whose first row is a past game is wrong; the test used to require ALL rows
    past to refuse. Fix: a stale FIRST moment refuses."""
    answer = {"name": "games", "label": "Games", "words": ["game"], "kind": "list", "path": "games",
              "cells": [{"path": "when", "label": "When", "type": "time"}], "axis": {"cell": "when", "step": "day"}}
    now = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
    first = nimod.run_pipeline([{"op": "extract", "paths": {"when": "when"}},
                                 {"op": "transform", "apply": [{"fn": "time", "field": "when"}]}],
                                {"when": "2026-09-27T19:10:00Z"})
    rest = nimod.run_pipeline([{"op": "extract", "paths": {"when": "when"}},
                                {"op": "transform", "apply": [{"fn": "time", "field": "when"}]}],
                               {"when": "2026-10-01T18:00:00Z"})
    preview = {"rows": [first, rest]}
    reason = ni_flow._frame_gap("next_event", [answer], preview, now)
    assert reason and "passed" in reason, f"expected refusal on first-row past, got {reason!r}"


# ---- F6: source-level checks run before ANY build path (mapping included) ------------------------

def test_f6_source_mismatch_refuses_before_the_mapping_path_when_answers_dont_fit() -> None:
    """When declared answers don't fit the response (misfit), _try_answers_build returns None and the
    flow used to fall to model mapping — skipping _verify_frame. Fix: source-level verify (category,
    other subject, place, stray topic) still runs; a mismatched source refuses before mapping."""
    ask = "next Yankees game"
    intent = _intent(ask, "New York Yankees", ["next game"])
    sample = copy.deepcopy(_SAMPLES["mets_schedule"])
    # strip every game's venue so declared answers that require it misfit (every row loses a cell)
    for d in sample["sample"]["dates"]:
        for g in d["games"]:
            g.pop("venue", None)
    row = {"source_id": "mlb-team-schedule", "url": sample["url"], "title": "MLB",
           "host": "statsapi.mlb.com", "provider": "MLB", "authority": "official",
           "label": "Atlanta Braves (mlb)", "choice": False, "format": "json", "needs_key": None,
           "needs_contact": False, "params": sample["params"], "categories": ["sports/schedules"],
           "scope": "global", "lookup": None}
    now = datetime.fromisoformat(sample["fetched"]).astimezone(UTC)
    store = _store()
    item_id = ni_flow.create_shell_item(store, ask)
    rec = ni_flow._make_record(ask, "source", intent=intent)
    rec["_ranked_library"] = [row]
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, row["url"], row)

    def _no_model(prompt: str) -> str:
        raise AssertionError("model called: source-level verify should have refused first")

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(nimod, "_clock", lambda: now)
        out = ni_flow._sample_and_map(store, item_id, ask, intent, row["url"], _no_model,
                                       lambda _u: copy.deepcopy(sample["sample"]))
    finally:
        monkey.undo()
    assert out["state"] in {"unsupported", "source"}, out


# ---- F7: a naming word the user said that no part of the source takes refuses --------------------

def test_f7_ukraine_news_refuses_a_general_news_feed_that_takes_no_filter_for_it() -> None:
    """abc-top is a general US headline feed; "Ukraine" is a naming word the user said and no part of
    the source takes it (not entity, not params, not readings, not a filter the card applies). Fix
    (class): refuse."""
    ask = "latest news on Ukraine"
    intent = _intent(ask, "Ukraine", ["news"])
    answers = ni_flow._library_answers("abc-top")
    assert answers, "abc-top has answers"
    chosen = ni_flow.select_answers(answers, ask, intent["wants"], intent["window"], intent["frame_kind"])
    assert chosen, "something is chosen"
    row = {"source_id": "abc-top", "url": "https://abcnews.go.com/abcnews/topstories", "label": "",
           "params": {}, "scope": "global"}
    store = _store()
    item_id = ni_flow.create_shell_item(store, ask)
    rec = ni_flow._make_record(ask, "source", intent=intent)
    rec["_ranked_library"] = [row]
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, row["url"], row)
    live = {**ni_flow._flow_read(store, item_id), "_library_source": "abc-top"}
    built = {"chosen": chosen, "answers": answers, "preview_payload": {},
             "unanswered": ni_flow._unanswered_wants(answers, ask, intent["wants"], [])}
    reasons, _notes = ni_flow._verify_frame(ni_flow._frame_of(ask, intent), ni_flow._picked_source(live),
                                            {}, built, ask, intent, _NOW)
    assert reasons, f"abc-top shipped for 'Ukraine news': {reasons}"


# positive regressions for F7: these must still ship (named-by-own-words / general)
_F7_SHIPS = [
    ("latest headlines", "news headlines", ["headlines"], "abc-top"),
    ("abc news headlines", "ABC News", ["headlines"], "abc-top"),
    ("baseball games tonight", "baseball", ["games"], "mlb-schedule"),
    ("latest world headlines", "world news", ["headlines"], "bbc-world"),
]


@pytest.mark.parametrize("ask, subject, wants, sid", _F7_SHIPS)
def test_f7_right_asks_still_ship(ask, subject, wants, sid) -> None:
    answers = ni_flow._library_answers(sid)
    assert answers, sid
    intent = _intent(ask, subject, wants)
    chosen = ni_flow.select_answers(answers, ask, wants, intent["window"], intent["frame_kind"])
    assert chosen, f"nothing chosen for {ask!r}"
    row = {"source_id": sid, "url": "https://example.org/x", "label": "", "params": {}, "scope": "global"}
    store = _store()
    item_id = ni_flow.create_shell_item(store, ask)
    rec = ni_flow._make_record(ask, "source", intent=intent)
    rec["_ranked_library"] = [row]
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, row["url"], row)
    live = {**ni_flow._flow_read(store, item_id), "_library_source": sid}
    built = {"chosen": chosen, "answers": answers, "preview_payload": {},
             "unanswered": ni_flow._unanswered_wants(answers, ask, wants, [])}
    reasons, _notes = ni_flow._verify_frame(ni_flow._frame_of(ask, intent), ni_flow._picked_source(live),
                                            {}, built, ask, intent, _NOW)
    assert reasons == [], f"{sid} refused for {ask!r}: {reasons}"


# ---- F10: an hour-axis list cut to a day window must size the cap to the window -----------------

def test_f10_hour_axis_list_cut_to_a_day_window_renders_more_than_five_rows(monkeypatch) -> None:
    """A nws-forecast-hourly-shaped list axis=hour has limit=5 by default; a Saturday ask then shows only
    12-4 AM. The cap must size to the asked window when the axis is finer than the window."""
    answer = {"name": "hours", "label": "Hourly forecast", "words": ["hourly"], "kind": "list",
              "path": "periods",
              "cells": [{"path": "startTime", "label": "Time", "type": "time"},
                        {"path": "temperature", "label": "Temperature", "type": "number", "unit": "°F"}],
              "axis": {"cell": "startTime", "step": "hour"}}
    # Oct 10 2026 is a Saturday; mock the clock so dow:sat lands on these rows
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(nimod, "_clock", lambda: now)
    periods = [{"startTime": f"2026-10-10T{h:02d}:00:00Z", "temperature": 40 + h} for h in range(24)]
    built = ni_flow._build_rows_answer(answer, {"periods": periods}, "Denver", window="dow:sat")
    scene = built["scene"]
    repeats = [c for c in scene["children"] if isinstance(c, dict) and c.get("type") == "repeat"]
    assert repeats, scene
    assert repeats[0].get("max", 0) > 5, repeats[0]


# ---- F12: when every want the user said is unanswered, refuse -----------------------------------

def test_f12_a_source_whose_overlap_is_only_the_subject_word_refuses_when_all_wants_are_unanswered() -> None:
    """"gas inventories this week" shipped fred-gasregw ("retail gas prices"), scored on "gas" alone,
    with "inventories" unanswered. Fix: every asked want unanswered → refuse."""
    ask = "gas inventories this week"
    intent = _intent(ask, "natural gas storage", ["inventories"])
    answers = ni_flow._library_answers("fred-gasregw")
    assert answers, "fred-gasregw has answers"
    chosen = ni_flow.select_answers(answers, ask, intent["wants"], intent["window"], intent["frame_kind"])
    assert chosen, "something is chosen"
    row = {"source_id": "fred-gasregw", "url": "https://example.org/f", "label": "", "params": {},
           "scope": "global"}
    store = _store()
    item_id = ni_flow.create_shell_item(store, ask)
    rec = ni_flow._make_record(ask, "source", intent=intent)
    rec["_ranked_library"] = [row]
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, row["url"], row)
    live = {**ni_flow._flow_read(store, item_id), "_library_source": "fred-gasregw"}
    built = {"chosen": chosen, "answers": answers, "preview_payload": {},
             "unanswered": ni_flow._unanswered_wants(answers, ask, intent["wants"], [])}
    reasons, _notes = ni_flow._verify_frame(ni_flow._frame_of(ask, intent), ni_flow._picked_source(live),
                                            {}, built, ask, intent, _NOW)
    assert reasons, f"fred-gasregw shipped for 'gas inventories this week': {reasons}"


# ---- F14: _clean_answer accepts whole {param} segments in row cells ------------------------------

def test_f14_clean_answer_accepts_a_whole_param_segment_in_a_row_cell() -> None:
    """SCHEMA allows a whole {param} segment in a cell path; _clean_answer dropped any cell with "{"
    in its path, so fred-gas-region's row-level date column was lost."""
    raw = {"name": "recent", "label": "Recent", "kind": "list", "primary": True, "words": ["recent"],
           "path": "observations",
           "row": [{"path": "{date}.value", "label": "Value", "type": "number"},
                   {"path": "date", "label": "Date", "type": "text"}]}
    cleaned = ni_flow._clean_answer(raw)
    assert cleaned is not None, "a whole-segment {param} in a cell path is allowed"
    assert cleaned["cells"][0]["path"] == "{date}.value"


def test_f14_clean_answer_still_rejects_a_param_inside_a_cell_segment() -> None:
    """Only WHOLE segments are allowed; a {param} spliced into another word is still refused."""
    raw = {"name": "x", "label": "X", "kind": "list", "primary": True, "words": ["x"],
           "path": "items", "row": [{"path": "pre{bad}suf", "label": "Y", "type": "text"}]}
    assert ni_flow._clean_answer(raw) is None


# ---- FETCH-F6: _repick_without drops the whole host only for host-wide signals -------------------

def _mk_rows(host_a: str, host_b: str) -> list[dict]:
    return [{"source_id": "a1", "url": f"https://{host_a}/p1", "label": "", "params": {}, "scope": "global",
             "host": host_a, "provider": "P-A1"},
            {"source_id": "a2", "url": f"https://{host_a}/p2", "label": "", "params": {}, "scope": "global",
             "host": host_a, "provider": "P-A2"},
            {"source_id": "b1", "url": f"https://{host_b}/p1", "label": "", "params": {}, "scope": "global",
             "host": host_b, "provider": "P-B"}]


def test_fetch_f6_plain_url_refusal_drops_only_the_url_not_the_whole_host() -> None:
    """401 / 403 on one URL of a multi-tenant host (services.arcgis.com, s3.amazonaws.com) must leave
    the host's other tenants in the pick."""
    rows = _mk_rows("services.arcgis.com", "api.other.example.org")
    store = _store()
    item_id = ni_flow.create_shell_item(store, "ask")
    rec = ni_flow._make_record("ask", "source", intent=_intent("ask", "x", []))
    rec["_ranked_library"] = rows
    ni_flow._flow_write(store, item_id, rec)
    moved = ni_flow._repick_without(store, item_id, rows[0]["url"], why="refused SmartBrain's request")
    assert moved is not None
    kept = [r["url"] for r in moved["_ranked_library"]]
    assert rows[1]["url"] in kept, "the OTHER tenant on the same host must stay"
    assert rows[2]["url"] in kept


def test_fetch_f6_a_host_wide_signal_drops_every_tenant() -> None:
    """429 / challenge / rate_limited is a host-level wall; dropping just the URL would retry the next
    tenant into the same wall."""
    rows = _mk_rows("services.arcgis.com", "api.other.example.org")
    store = _store()
    item_id = ni_flow.create_shell_item(store, "ask")
    rec = ni_flow._make_record("ask", "source", intent=_intent("ask", "x", []))
    rec["_ranked_library"] = rows
    ni_flow._flow_write(store, item_id, rec)
    moved = ni_flow._repick_without(store, item_id, rows[0]["url"],
                                     why="refused SmartBrain's request", host_wide=True)
    assert moved is not None
    kept = [r["url"] for r in moved["_ranked_library"]]
    assert rows[1]["url"] not in kept
    assert rows[2]["url"] in kept


# ---- D13: unexpected exception from _library_candidates must not be swallowed silently -----------

def test_d13_library_candidates_logs_an_unexpected_exception_and_still_returns_empty() -> None:
    """A broken Library degrades to the web stage (silent fallback), but an unexpected class is
    logged (host-free) so the operator sees it."""
    class _Broken:
        def candidates(self, *_a, **_kw):
            raise RuntimeError("bang")

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Broken())
        logs: list[tuple[str, str]] = []

        class _LogSink:
            def warning(self, msg: str, *args: object) -> None:
                logs.append((msg, " ".join(str(a) for a in args)))

            def info(self, *_a: object, **_kw: object) -> None:
                pass

            def debug(self, *_a: object, **_kw: object) -> None:
                pass

            def error(self, *_a: object, **_kw: object) -> None:
                pass

        monkey.setattr(ni_flow, "log", _LogSink())
        got = ni_flow._library_candidates("test", {})
    finally:
        monkey.undo()
    assert got == []
    assert logs, "an unexpected exception must be logged (host-free)"
    assert "RuntimeError" in logs[0][1]
