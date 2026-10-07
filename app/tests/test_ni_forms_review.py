"""Regression tests ported from proto/tests/forms/test_review_fixes.py.

Skipped: `test_card_zone_from_coordinates` (needs the data layer, not ported),
`test_float_noise_is_not_precision` (needs pipeline.data.assemble, not ported),
`test_day_table_one_column_per_kind` (reads out/ build artifacts, not ported).
"""
from __future__ import annotations

import types

from smartbrain_3000.ni_forms import fmt
from smartbrain_3000.ni_forms.lint import lint
from smartbrain_3000.ni_forms.paint import live as LV
from smartbrain_3000.ni_forms.spans import Span, bucket


def _txt(pid, y, s, role="row", x=("l", 0), px=16):
    return {"k": "text", "id": pid, "x": list(x), "y": y, "max_w": 180, "lines": [s], "role": role, "px": px,
            "tok": "text", "anchor": "start", "dir": "ltr", "src": "code"}


def _clir(prims, span="d1x2", live=()):
    b = bucket(Span.parse(span))
    clir = {"v": 1, "form": "x", "variant": "d", "cand": "c0", "plan": "p", "span": span,
            "bucket": {"min_w": b.min_w, "max_w": b.max_w, "h": b.h}, "state": "ok", "prims": prims,
            "reading_order": [p["id"] for p in prims], "summary": "s", "live": list(live), "hitmap": []}
    return clir, b


def _codes(res):
    return {i.code for i in res.issues if i.sev == "red"}


def test_body_empty_is_red():
    clir, b = _clir([_txt(1, 14, "Title", "title", px=14), _txt(2, 300, "footer", "footer", px=12)])
    res = lint(clir, None, None, None, None, None, b, meta={"body": {"y0": 30, "y1": 280}})
    assert "L-BODY-EMPTY" in _codes(res)


def test_balanced_hollow_is_red():
    """Equal top and bottom gaps no longer hide a hollow card (review 1c)."""
    prims = [_txt(1, 14, "Title", "title", px=14), _txt(3, 150, "one row"), _txt(4, 172, "two row")]
    clir, b = _clir(prims)
    res = lint(clir, None, None, None, None, None, b, meta={"body": {"y0": 30, "y1": 300}})
    assert "L-HOLLOW" in _codes(res)
    res2 = lint(clir, None, None, None, None, None, b, meta={"body": {"y0": 30, "y1": 300}, "forced": "one"})
    assert "L-HOLLOW" not in _codes(res2)


def test_now_line_must_gap_labels():
    """A now-marker line sweeps its box: every label in the sweep is listed in gaps (review 7)."""
    prims = [_txt(1, 14, "Title", "title", px=14),
             _txt(2, 80, "7:10 pm · 6.0 ft", "label", x=("f", 0.5), px=11),
             {"k": "line", "id": 3, "x0": ["f", 0.2], "y0": 60, "x1": ["f", 0.2], "y1": 200,
              "tok": "viz-now-line", "w": 1, "dash": None}]
    box = {"x0": ["f", 0], "x1": ["f", 1], "y0": 60, "y1": 200}
    live = [{"k": "now_marker", "args": {"prims": [3], "box": box, "t0": "2026-09-24T00:00:00Z",
                                         "t1": "2026-09-25T00:00:00Z"}}]
    clir, b = _clir(prims, live=live)
    assert any(i.code == "overlap" and "sweeps" in i.detail for i in
               lint(clir, None, None, None, None, None, b, meta={"body": {"y0": 30, "y1": 300}}).issues)
    live[0]["args"]["gaps"] = [2]
    assert not any("sweeps" in i.detail for i in
                   lint(clir, None, None, None, None, None, b, meta={"body": {"y0": 30, "y1": 300}}).issues)


def test_line_gaps_painted():
    lab = _txt(2, 100, "8:48 pm · 6.64 ft", "label", x=("f", 0.5), px=11)
    line = {"k": "line", "id": 3, "x0": ["f", 0.52], "y0": 60, "x1": ["f", 0.52], "y1": 200, "gaps": [2]}
    segs = LV.line_segments(line, {2: lab}, 400)
    assert len(segs) == 2 and segs[0][1] < 95 and segs[1][0] > 100


def test_unrounded_floats_and_compact():
    f = types.SimpleNamespace(type="number", precision=None, unrounded=True, currency=None, unit=None, scale=None)
    assert fmt.value_text(-0.12432868, f) == "−0.124"
    g = types.SimpleNamespace(type="currency", precision=4, unrounded=False, currency="USD", unit=None, scale=None)
    assert fmt.value_text(1692113715423.4297, g, compact=True) == "$1.69T"
