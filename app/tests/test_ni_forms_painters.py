"""Vector-painter tests ported from proto/tests/paint/test_painters.py.

Raster assertions are skipped (raster.py was not ported); the three CLIR
fixtures (`curve_tide`, `every_kind`, `stat_nvda`) live under
tests/fixtures/ni_forms/clir/.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from smartbrain_3000.ni_forms import text as TX
from smartbrain_3000.ni_forms import tokens as TK
from smartbrain_3000.ni_forms.paint import vector as VC

FIX = Path(__file__).parent / "fixtures" / "ni_forms" / "clir"
NOW = datetime(2026, 9, 24, 18, 52, tzinfo=UTC)
NAMES = ("stat_nvda", "curve_tide", "every_kind")


def fx(name):
    return json.loads((FIX / f"{name}.clir.json").read_text())


def test_vector_fragment_structure():
    c = fx("curve_tide")
    h = VC.card_html(c, width=440, theme="dark", now=NOW, viewer_tz="America/New_York")
    ids = {int(x) for x in re.findall(r'data-p="(\d+)"', h)}
    assert ids == {p["id"] for p in c["prims"]}
    assert 'data-ni-theme="dark"' in h and "http://" not in h and "https://" not in h
    hidden = {int(x) for x in re.findall(r'data-p="(\d+)"[^>]*data-hidden', h)}
    v2 = c["live"][2]["args"]["variants"][1]["prims"]
    assert set(v2) <= hidden
    h2 = VC.card_html(c, width=440, theme="dark",
                      now=datetime(2026, 9, 24, 23, 30, tzinfo=UTC),
                      viewer_tz="America/New_York")
    hidden2 = {int(x) for x in re.findall(r'data-p="(\d+)"[^>]*data-hidden', h2)}
    assert not (set(v2) & hidden2)


def test_vector_escapes_untrusted_text():
    c = fx("stat_nvda")
    c = copy.deepcopy(c)
    c["prims"][1]["lines"] = ['<script>alert(1)</script>"\u202e']
    h = VC.card_html(c, width=206, theme="light", now=NOW, viewer_tz="America/New_York")
    assert "<script>alert" not in h and "&lt;script&gt;" in h and "\u202e" not in h


def test_vector_page_is_standalone():
    c = fx("stat_nvda")
    page = VC.page([{"html": VC.card_html(c, width=206, theme="auto", now=NOW,
                                           viewer_tz="America/New_York")}],
                   title="t", now=NOW)
    assert page.count("@font-face") == 4 and "data:font/ttf;base64," in page
    assert not re.search(r'(src|href)="https?://', page)
    for t in ("dark", "light"):
        for tok in ("panel", "viz-line", "text"):
            assert f"--ni-{tok}: {TK.css(t, tok)};" in page


def test_vector_text_positions_match_layout_baselines():
    """The CSS line box is placed so the baseline lands on the CLIR y (Inter hhea metrics)."""
    c = fx("stat_nvda")
    h = VC.card_html(c, width=206, theme="dark", now=NOW, viewer_tz="America/New_York")
    hero = next(p for p in c["prims"] if p.get("role") == "hero")
    m = re.search(rf'data-p="{hero["id"]}"[^>]*style="[^"]*top:([\d.\-]+)px', h)
    top = float(m.group(1))
    px, lh = 31, 38
    mt = TX.metrics(px, 600)
    assert abs(top + (lh - mt["ascent"] - mt["descent"]) / 2 + mt["ascent"] - hero["y"]) < 0.02


def test_vector_style_attributes_are_well_formed():
    """Every style attribute survives HTML parsing whole."""
    for name in NAMES:
        h = VC.card_html(fx(name), width=424 if name != "stat_nvda" else 206, theme="dark",
                         now=NOW, viewer_tz="America/New_York")
        for st in re.findall(r'style="([^"]*)"', h):
            assert st.count("'") % 2 == 0
        assert re.search(r"font-feature-settings:'tnum' 1;z-index", h)


def test_vector_content_box_matches_raster():
    """The 1 px border sits inside the 16 px padding (content starts 16 px in)."""
    assert f"padding:{TK.SPACE['pad'] - TK.SPACE['border_w']}px" in VC.CARD_CSS
