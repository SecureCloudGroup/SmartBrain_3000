"""Phase 1a-2 glyph policy: `glyph_missing` is AMBER (hybrid: browser paints the CLIR).

A codepoint absent from every face in the fallback chain used to make the whole layout
red; it now reports as amber so the layout can still land. Measurement already uses the
font's .notdef advance (tofu fallback)."""

from __future__ import annotations

from smartbrain_3000.ni_forms import text as TX
from smartbrain_3000.ni_forms.lint import AMBER, RED, lint
from smartbrain_3000.ni_forms.spans import Span, bucket


def _txt(pid: int, y: float, s: str, role: str = "row", px: int = 16) -> dict:
    return {"k": "text", "id": pid, "x": ["l", 0], "y": y, "max_w": 180, "lines": [s], "role": role,
            "px": px, "tok": "text", "anchor": "start", "dir": "ltr", "src": "code"}


def test_coverage_reports_missing_codepoints() -> None:
    """The measurer separates inter, fallback and missing — missing stays a list of chars."""
    # Zero-width non-joiner is stripped from measurement; use a Private Use Area code
    # point that no shipped face covers.
    cov = TX.coverage("hello \ue000", weight=400)
    assert cov["missing"] == ["\ue000"]
    assert isinstance(cov["inter"], int)


def test_lint_reports_a_missing_glyph_as_amber() -> None:
    """A text prim carrying U+E000 (no face has it) lints ``glyph_missing`` AMBER, never red."""
    span = "d1x2"
    b = bucket(Span.parse(span))
    prims = [_txt(1, 14, "Title", "title", px=14), _txt(2, 60, "ok \ue000 row"),
             _txt(3, 82, "two row"), _txt(4, 104, "three row"), _txt(5, 126, "four row"),
             _txt(6, 148, "five row"), _txt(7, 170, "six row"), _txt(8, 192, "seven row"),
             _txt(9, 214, "eight row"), _txt(10, 236, "nine row"), _txt(11, 258, "ten row")]
    clir = {"v": 1, "form": "x", "variant": "d", "cand": "c0", "plan": "p", "span": span,
            "bucket": {"min_w": b.min_w, "max_w": b.max_w, "h": b.h}, "state": "ok", "prims": prims,
            "reading_order": [p["id"] for p in prims], "summary": "s", "live": [], "hitmap": []}
    res = lint(clir, None, None, None, None, None, b, meta={"body": {"y0": 30, "y1": 280}})
    glyph = [i for i in res.issues if i.code == "glyph_missing"]
    assert glyph and all(i.sev == AMBER for i in glyph), [(i.code, i.sev) for i in res.issues]
    assert glyph[0].prim == 2 and "\ue000" in (glyph[0].detail or "")
    assert not any(i.code == "glyph_missing" and i.sev == RED for i in res.issues)
