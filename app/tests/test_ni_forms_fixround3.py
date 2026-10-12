"""Round 19 post-1a-6 (2026-10-07, after the SET C live read): D2 the subject row filter
extended to alert-style lists (its tests left with the filter — Round 20, Q3b: the query layer's where),
P1 a one-row measure built from value answers never drops
the identity field before venue/meta, P2 a day_table window cut starts at the first day
WITH data, ALT the preview bind lays out the sealed runner-up as `alternatives`.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smartbrain_3000 import ni
from smartbrain_3000.ni_forms.enumerate import enumerate as enumerate_cands
from smartbrain_3000.ni_forms.form_scene import design
from smartbrain_3000.ni_forms.layout import layout_span
from smartbrain_3000.ni_forms.spans import Span
from smartbrain_3000.ni_forms.types import TimeProfile
from tests import _ni_forms_recs as recs

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
URL = "https://api.example.org/x"
SPEC = {"title": "t", "goal": "g", "interval_minutes": 15, "source": {"type": "http_json", "url": URL}}


def _red(lo) -> list[str]:
    return sorted({i.code for i in lo.lint.issues if i.sev == "red"})


def _texts(clir: dict) -> str:
    return " | ".join(ln for p in clir["prims"] if p["k"] == "text" for ln in p["lines"])


def test_a_percent_with_no_published_precision_shows_two_decimals() -> None:
    """Lead follow-up (board read 2026-10-07): an unrounded percent column painted "-0.7317%".
    A percent with no published precision caps at 2 decimals; a tiny one keeps 4; a
    published precision is kept as published (never fewer)."""
    from smartbrain_3000.ni_forms import fmt
    pct = recs.F("dp", "percent")                       # precision None
    assert fmt.decimals(pct, -0.7317) == 2
    assert fmt.decimals(pct, -0.7317, derived=True) == 2
    assert fmt.decimals(pct, 0.0042) == 4
    assert fmt.decimals(recs.F("dp", "percent", precision=4), -0.7317) == 4
    assert fmt.decimals(recs.F("dp", "percent", scale="0..1"), 0.007317) == 2


# ---------------------------------------------------------------------------- P1: identity vs venue

_NEXT_GAME_ANSWERS = [
    {"kind": "value", "name": "next_game", "label": "Next game", "type": "text",
     "path": "events[0].strEvent", "window": "latest",
     "words": ["next game", "next match", "who do they play", "opponent", "play next", "matchup", "next"]},
    {"kind": "value", "name": "start", "label": "Start", "type": "time", "utc": True,
     "path": "events[0].strTimestamp", "window": "latest",
     "words": ["when", "what time", "start time", "kickoff", "tip-off", "date", "game time",
               "next game", "play next"]},
    {"kind": "value", "name": "venue", "label": "Venue", "type": "text",
     "path": "events[0].strVenue", "window": "latest",
     "words": ["where", "venue", "stadium", "arena", "field", "next game", "play next"]},
]
_NEXT_GAME_OUTPUTS = {"next_game": "New York Knicks vs Washington Wizards",
                      "venue": "Madison Square Garden", "start": "2026-10-08T23:30:00Z"}


def test_identity_field_is_never_dropped_before_venue_in_a_next_matchup_card() -> None:
    """P1: the Library's 'next matchup' answer trio (name/start/venue) — the matchup
    text is a 38-char identity, not prose; it must ride with venue, not be dropped for
    it (the old passage rule only exempted a list demoted to a measure, not this)."""
    d = design(_NEXT_GAME_ANSWERS, _NEXT_GAME_OUTPUTS, title="next game",
              ask="Knicks schedule this month", now=NOW, source_url=URL, cadence_s=900,
              call_model=None, viewer_tz="UTC", question_kind="schedule", wants=["schedule"])
    lo = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(d.node["spans"]["desktop"]), NOW)
    texts = _texts(lo.clir)
    assert "New York Knicks vs Washington Wizards" in texts
    assert "Madison Square Garden" in texts
    assert not _red(lo)


# ---------------------------------------------------------------------------- P2: day_table window

def test_day_table_window_cut_starts_at_the_first_day_with_data() -> None:
    """P2: a window cut ("this weekend") already leaves the record's rows starting after
    today — day_rows must walk from the first day WITH data, never synthesize the empty
    days the window excluded (no "Tomorrow" / blank date row before the real ones)."""
    fields = [recs.F("when", type="datetime", role="time"),
             recs.F("temp", type="number", role="value", unit="°F")]
    rows = [[recs.iso(recs.NOW + timedelta(days=3, hours=2)), 68],
            [recs.iso(recs.NOW + timedelta(days=4, hours=2)), 70]]
    rec = recs.mk("events", fields, rows, host="api.weather.gov")
    prof = recs.prof(rec, ["events_with_kind"], ["each_day"])
    t = prof.time
    prof.time = TimeProfile(t.field, "day", t.span_h, t.covers_today, t.future_events, t.past_events, t.days)
    inp_ = recs.inp("weekend weather", ask="weather summary for Denver this weekend")
    c = next(x for x in enumerate_cands(rec, prof, inp_, recs.NOW) if x.form == "day_table")
    sp = Span.parse(next(k for k in ("d1x2", "d2x2", "d2x1", "d3x1") if k in c.plans))
    lo = layout_span(c, rec, prof, inp_, sp, recs.NOW)
    times = sorted(p["t"] for p in lo.clir["prims"] if p["k"] == "time" and p.get("role") != "footer")
    assert "Tomorrow" not in _texts(lo.clir)
    assert times[0].startswith("2026-09-27")  # the first row WITH data (NOW + 3 days), not d0 or +2
    assert not _red(lo)


# ---------------------------------------------------------------------------- ALT: runner-up preview

def _moon_case() -> tuple[list, dict]:
    cells = [{"path": "phase", "key": "phase", "label": "Phase", "type": "text", "unit": None},
             {"path": "month", "key": "month", "label": "Month", "type": "number", "unit": None},
             {"path": "day", "key": "day", "label": "Day", "type": "number", "unit": None},
             {"path": "time", "key": "time", "label": "Time (UT)", "type": "text", "unit": None}]
    answer = {"kind": "list", "name": "phases", "label": "Upcoming moon phases", "path": "phasedata",
              "words": ["moon phase", "new moon"], "primary": True, "cells": cells}
    rows = [{"phase": "New Moon", "month": 10, "day": 10, "time": "07:50"},
            {"phase": "First Quarter", "month": 10, "day": 18, "time": "16:13"},
            {"phase": "Full Moon", "month": 10, "day": 26, "time": "04:12"},
            {"phase": "Last Quarter", "month": 11, "day": 1, "time": "20:28"}]
    return [answer], {"rows": rows}


def _moon_design():
    answers, outputs = _moon_case()
    d = design(answers, outputs, title="new moon", ask="what day is the next new moon", now=NOW,
              source_url=URL, cadence_s=900, rows_output_name="rows", call_model=None,
              viewer_tz="UTC", question_kind="next_event", wants=[])
    assert d.node["design"]["second"] is not None, "fixture must seal a runner-up"
    return d, outputs


def test_preview_bind_lays_out_the_runner_up_as_alternatives() -> None:
    d, outputs = _moon_design()
    ctx = ni._form_bind_context(SPEC, NOW)
    preview = ni.bind_scene(d.node, outputs, form_ctx=ctx, alternatives=True)
    ni._enforce_form_shape(preview)
    assert len(preview["alternatives"]) == 1
    alt = preview["alternatives"][0]
    assert alt["form"] == d.node["design"]["second"]["form"]
    assert alt["id"] == d.node["design"]["second"]["id"]
    assert alt["clir"]["desktop"]["prims"] and alt["clir"]["phone"]["prims"]
    assert alt["summary"]


def test_a_run_bind_never_carries_alternatives() -> None:
    """The engine run path and the C2 swap are unchanged: no flag, no key."""
    d, outputs = _moon_design()
    ctx = ni._form_bind_context(SPEC, NOW)
    run = ni.bind_scene(d.node, outputs, form_ctx=ctx)
    assert "alternatives" not in run
    run2 = ni.bind_scene(d.node, outputs, form_ctx=ctx, alternatives=False)
    assert "alternatives" not in run2


def test_enforce_form_shape_rejects_a_malformed_alternatives_entry() -> None:
    d, outputs = _moon_design()
    ctx = ni._form_bind_context(SPEC, NOW)
    preview = ni.bind_scene(d.node, outputs, form_ctx=ctx, alternatives=True)
    one = preview["alternatives"][0]
    with pytest.raises(ni.NIError):
        ni._enforce_form_shape({**preview, "alternatives": [one, one]})
    missing_summary = {k: v for k, v in one.items() if k != "summary"}
    with pytest.raises(ni.NIError):
        ni._enforce_form_shape({**preview, "alternatives": [missing_summary]})
