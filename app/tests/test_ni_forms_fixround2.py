"""Fix round 1a-6 (2026-10-07, after the second live read of the forms engine): one
labeled case set per class — H a one-row list reads like a measure (record.py's own
tests cover role assignment; this file covers the end-to-end red-free design), I a unit
too long to ride inline with the hero, J undeclared precision follows the magnitude
rule, L red lint never ships from a build, M PRESENT's prompt ends with a literal JSON
skeleton of the menu.
"""
from __future__ import annotations

import types
from datetime import UTC, datetime

from smartbrain_3000.ni_forms import fmt
from smartbrain_3000.ni_forms.enumerate import enumerate as enumerate_cands
from smartbrain_3000.ni_forms.form_scene import design
from smartbrain_3000.ni_forms.layout import layout_span
from smartbrain_3000.ni_forms.present import build_messages
from smartbrain_3000.ni_forms.record import from_spec
from smartbrain_3000.ni_forms.spans import Span

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
URL = "https://api.example.org/x"
CTX = {"source_url": URL, "fetched_at": "2026-10-07T12:00:00Z", "viewer_tz": "UTC"}

_CPI_ANSWER = {"kind": "value", "name": "latest", "label": "Consumer Price Index (all items)",
              "type": "number", "path": "latest", "unit": "index 1982-84=100", "window": "latest"}
_CPI_OUTPUTS = {"latest": 334.131}
_SUNRISE_ANSWER = {"kind": "columns", "label": "Sunrise and sunset by day",
                   "cells": [{"path": "daily.time", "key": "day", "type": "date", "label": "Day"},
                             {"path": "daily.sunrise", "key": "sunrise", "type": "time", "label": "Sunrise"},
                             {"path": "daily.sunset", "key": "sunset", "type": "time", "label": "Sunset"}],
                   "axis": {"cell": "daily.time", "step": "day"}}
_SUNRISE_OUTPUTS = {"rows": [{"day": "2026-10-08", "sunrise": "2026-10-08T10:49:00Z",
                             "sunset": "2026-10-08T22:13:00Z"}]}
_TWO_FIELD_ANSWERS = [{"kind": "value", "name": "a", "label": "A", "type": "number", "path": "a"},
                     {"kind": "value", "name": "b", "label": "B", "type": "number", "path": "b"}]
_TWO_FIELD_OUTPUTS = {"a": 1.0, "b": 2.0}


def _red(lo) -> list[str]:
    return sorted({i.code for i in lo.lint.issues if i.sev == "red"})


# ---- I: a unit too long to ride inline with the hero ----------------------------------

def test_unit_rides_inline_only_for_short_symbols_without_a_space_or_equals() -> None:
    long_unit = types.SimpleNamespace(unit="index 1982-84=100")
    assert fmt.unit_rides_inline(long_unit) is False
    for u in ("mph", "degF", "USD/oz"):
        assert fmt.unit_rides_inline(types.SimpleNamespace(unit=u)) is True
    assert fmt.unit_rides_inline(types.SimpleNamespace(unit=None)) is True
    assert fmt.unit_rides_inline(None) is True


def test_cpi_hero_carries_the_number_alone_the_unit_rides_below() -> None:
    d = design([_CPI_ANSWER], _CPI_OUTPUTS, title="consumer price index",
              ask="consumer price index over the past 12 months", now=NOW, source_url=URL,
              cadence_s=900, call_model=None, viewer_tz="UTC", question_kind="trend", wants=[])
    lo = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(d.node["spans"]["desktop"]), NOW)
    hero = next(p for p in lo.clir["prims"] if p["k"] == "text" and p["role"] == "hero")
    assert " ".join(hero["lines"]) == "334.13"          # class J: capped at the magnitude rule
    unit_line = next(p for p in lo.clir["prims"] if p["k"] == "text" and p.get("src") == "lexicon")
    assert "index 1982-84=100" in " ".join(unit_line["lines"])
    assert _red(lo) == []


# ---- J: undeclared precision follows the magnitude rule --------------------------------

def test_undeclared_precision_caps_at_two_decimals_for_values_at_or_above_one() -> None:
    assert fmt.decimals(None, 4121.299805) == 2
    assert fmt.decimals(None, 334.131) == 2
    assert fmt.decimals(None, 86.8) == 1              # never adds decimals the source lacked
    assert fmt.decimals(None, 0.12432868) == 3
    assert fmt.decimals(None, 0.0012345678) == 4


def test_declared_precision_still_wins_over_the_magnitude_rule() -> None:
    f = types.SimpleNamespace(type="number", precision=4, scale=None)
    assert fmt.decimals(f, 4121.299805) == 4


def test_gold_price_shows_at_most_two_decimals_unit_stays_inline() -> None:
    answer = {"kind": "value", "name": "price", "label": "Spot price", "type": "number",
             "path": "price", "unit": "USD/oz", "window": "now"}
    d = design([answer], {"price": 4121.299805}, title="gold price", ask="price of gold today",
              now=NOW, source_url=URL, cadence_s=900, call_model=None, viewer_tz="UTC",
              question_kind="current_value", wants=[])
    lo = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(d.node["spans"]["desktop"]), NOW)
    hero = next(p for p in lo.clir["prims"] if p["k"] == "text" and p["role"] == "hero")
    assert " ".join(hero["lines"]) == "4,121.30 USD/oz"   # inline: a short unit stays with the hero
    assert _red(lo) == []


# ---- L: red lint never ships from a build -----------------------------------------------

def test_cpi_and_sunrise_designs_seal_red_free_spans_and_bind_red_free() -> None:
    """The oracle already asserts red 0 at the sealed spans for recorded cases; this
    checks it directly on the CPI (class I/J) and sunrise (class H) shapes, build and
    bind alike, at the same fetch instant."""
    cpi = design([_CPI_ANSWER], _CPI_OUTPUTS, title="consumer price index",
                 ask="consumer price index over the past 12 months", now=NOW, source_url=URL,
                 cadence_s=900, call_model=None, viewer_tz="UTC", question_kind="trend", wants=[])
    sunrise = design([_SUNRISE_ANSWER], _SUNRISE_OUTPUTS, title="sunrise",
                     ask="what time is sunrise in Boston tomorrow", now=NOW, source_url=URL,
                     cadence_s=900, rows_output_name="rows", call_model=None, viewer_tz="UTC",
                     question_kind="lookup", wants=[])
    for d, outputs in ((cpi, _CPI_OUTPUTS), (sunrise, _SUNRISE_OUTPUTS)):
        assert not d.node["design"].get("gates"), d.node["design"].get("gates")
        for side in ("desktop", "phone"):
            lo = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(d.node["spans"][side]), NOW)
            assert _red(lo) == [], f"{d.node['form']} {side} build: {_red(lo)}"
        bind_ctx = {**CTX, "title": d.inp.title, "ask": d.inp.ask, "cadence_s": 900}
        rec2, inp2 = from_spec(d.node["record"], outputs, history=None, context=bind_ctx)
        for side in ("desktop", "phone"):
            lo2 = layout_span(d.cand, rec2, d.prof, inp2, Span.parse(d.node["spans"][side]), NOW)
            assert _red(lo2) == [], f"{d.node['form']} {side} bind: {_red(lo2)}"


def test_design_skips_a_red_candidate_for_the_next_one_in_floor_order(monkeypatch) -> None:
    """``form_scene._layout_once`` used to smoke-test the pick and discard the result;
    ``design()`` now moves to the next candidate in floor order when the pick is red."""
    from smartbrain_3000.ni_forms import form_scene as fs_mod
    real_layout_span = fs_mod.layout_span
    probe = design(_TWO_FIELD_ANSWERS, _TWO_FIELD_OUTPUTS, title="t", ask="a and b", now=NOW,
                  source_url=URL, cadence_s=900, call_model=None, viewer_tz="UTC")
    assert len(probe.cands) >= 2, "need >= 2 candidates to prove the fallback-through order"
    floor_first = probe.cands[0].id

    def fake_layout_span(cand, rec, prof, inp, span, now, **kw):
        out = real_layout_span(cand, rec, prof, inp, span, now, **kw)
        if cand.id == floor_first:
            out.lint.ok = False   # force the floor's top pick red at every span
        return out
    monkeypatch.setattr(fs_mod, "layout_span", fake_layout_span)
    d = design(_TWO_FIELD_ANSWERS, _TWO_FIELD_OUTPUTS, title="t", ask="a and b", now=NOW,
              source_url=URL, cadence_s=900, call_model=None, viewer_tz="UTC")
    assert d.cand.id != floor_first
    assert any(g.startswith(f"red:{floor_first}:") for g in d.node["design"]["gates"])
    assert d.node["design"]["designer"] == "rules"


def test_design_falls_to_the_universal_fallback_when_every_candidate_is_red(monkeypatch) -> None:
    from smartbrain_3000.ni_forms import form_scene as fs_mod
    real_layout_span = fs_mod.layout_span

    def always_red(cand, rec, prof, inp, span, now, **kw):
        out = real_layout_span(cand, rec, prof, inp, span, now, **kw)
        out.lint.ok = False
        return out
    monkeypatch.setattr(fs_mod, "layout_span", always_red)
    d = design(_TWO_FIELD_ANSWERS, _TWO_FIELD_OUTPUTS, title="t", ask="a and b", now=NOW,
              source_url=URL, cadence_s=900, call_model=None, viewer_tz="UTC")
    assert d.cand.fallback is True
    assert d.node["design"].get("fallback") is True
    assert any(g.startswith("fallback:") for g in d.node["design"]["gates"])


# ---- M: PRESENT's prompt ends with a literal JSON skeleton of the menu -------------------

def test_present_build_messages_ends_with_a_literal_json_skeleton_of_the_menu() -> None:
    """Fix round 1a-6, class M: the user message ends with a literal JSON shape naming
    THIS menu's candidate ids — the intent stage's prompt validates on the local 9B this
    way; a schema described only in words never did."""
    d = design(_TWO_FIELD_ANSWERS, _TWO_FIELD_OUTPUTS, title="t", ask="a and b", now=NOW,
              source_url=URL, cadence_s=900, call_model=None, viewer_tz="UTC")
    cands = enumerate_cands(d.rec, d.prof, d.inp, NOW)
    assert len(cands) >= 2, "need a real menu to prove the skeleton names its ids"
    msgs, ids, _schema = build_messages(cands, d.rec, d.prof, d.inp)
    content = msgs[-1]["content"]
    assert content.rstrip().endswith("Reply with ONLY that JSON.")
    assert '"pick": "<one of' in content and '"fits": [' in content
    for cid in ids:
        assert f'"cand": "{cid}"' in content
