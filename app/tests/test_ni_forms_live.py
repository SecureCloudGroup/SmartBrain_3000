"""live.py: time formatting, countdown words, live-binding semantics, clock boundaries.

Ported from proto/tests/paint/test_live.py (12 tests). CLIR fixtures live under
tests/fixtures/ni_forms/clir/.
"""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from smartbrain_3000.ni_forms import canon
from smartbrain_3000.ni_forms.paint import live as LV

FIX = Path(__file__).parent / "fixtures" / "ni_forms" / "clir"


def fx(name):
    return json.loads((FIX / f"{name}.clir.json").read_text())


@pytest.mark.parametrize("fmt,zone,exp", [
    ("h:mm a", "America/New_York", "7:10 pm"), ("h a", "America/New_York", "7 pm"),
    ("ha_short", "America/New_York", "7p"), ("EEE", "America/New_York", "Thu"),
    ("EEE d", "America/New_York", "Thu 24"), ("MMM d", "America/New_York", "Sep 24"),
    ("EEE MMM d", "America/New_York", "Thu Sep 24"), ("yyyy", "America/New_York", "2026"),
    ("MMM d, h:mm a", "America/New_York", "Sep 24, 7:10 pm"), ("HH:mm", "America/New_York", "19:10"),
    ("h:mm a", "Australia/Sydney", "9:10 am"), ("EEE", "Australia/Sydney", "Fri"),
])
def test_format_time(fmt, zone, exp):
    assert LV.format_time("2026-09-24T23:10:00Z", fmt, zone, False) == exp


def test_format_time_zone_suffix_and_midnight():
    assert LV.format_time("2026-09-25T04:00:00Z", "h:mm a", "America/New_York", True) == "12:00 am EDT"
    assert LV.format_time("2026-09-25T04:00:00Z", "ha_short", "America/New_York", False) == "12a"
    assert LV.format_time("2026-09-24T12:00:00Z", "h:mm a", "Asia/Kathmandu", True) == "5:45 pm UTC+5:45"


def test_date_only_is_floating():
    assert LV.format_time("2026-12-25", "EEE MMM d", "Pacific/Kiritimati", True) == "Fri Dec 25"
    assert LV.format_time("2026-12-25", "EEE MMM d", "Pacific/Pago_Pago", True) == "Fri Dec 25"


def test_time_prim_uses_card_zone_not_viewer():
    p = {"t": "2026-09-24T23:10:00Z", "fmt": "h:mm a", "tz": "card", "zone": "America/New_York",
         "show_zone": True}
    assert LV.time_prim_text(p, "America/New_York") == "7:10 pm"
    assert LV.time_prim_text(p, "America/Los_Angeles") == "7:10 pm EDT"
    q = dict(p, tz="viewer")
    assert LV.time_prim_text(q, "America/Los_Angeles") == "4:10 pm"


@pytest.mark.parametrize("s,fmt,exp", [
    (4 * 3600 + 18 * 60, "in_hm", "in 4 h 18 m"), (4 * 3600 + 17 * 60 + 1, "in_hm", "in 4 h 18 m"),
    (59, "in_hm", "in 1 min"), (0, "in_hm", "now"), (-5, "hm", "now"), (3600, "hm", "1 h"),
    (3 * 86400 + 60, "in_dhm", "in 3 days"), (86400 + 7200, "in_dhm", "in 26 h"),
    (86400 + 7200, "rel", "in 1 d 2 h"), (86400, "rel", "in 1 day"),
])
def test_countdown(s, fmt, exp):
    assert LV.countdown_text(s, fmt) == exp


def test_count_up_age_number():
    assert LV.count_up_text(12 * 86400 + 5, "auto") == "12 days"
    assert LV.count_up_text(3600, "hour") == "1 hour"
    assert LV.age_text(125 * 60) == "2 h old"
    assert LV.age_text(30) == "1 min old"
    assert LV.age_text(3 * 86400) == "3 d old"
    assert LV.number_text(8_123_456_789.4, ",.0f") == "8,123,456,789"
    assert LV.number_text(0.1234, ".1%") == "12.3%"
    assert LV.number_text(5, "bogus{0}") == "5"


def test_apply_live_variants_marker_dim():
    c = fx("curve_tide")
    before = LV.apply_live(c, datetime(2026, 9, 24, 18, 52, tzinfo=UTC), "America/New_York")
    after = LV.apply_live(c, datetime(2026, 9, 24, 23, 30, tzinfo=UTC), "America/New_York")

    def tx(cl):
        return [" ".join(p["lines"]) for p in cl["prims"] if p.get("role") == "headline"]
    assert tx(before) == ["High 6.0 ft at 7:10 pm"]
    assert tx(after) == ["Low 1.1 ft at 1:27 am"]
    cd = [p for p in before["prims"] if p.get("lines") and p["lines"][0].startswith("in ")]
    assert cd and cd[0]["lines"] == ["in 4 h 18 m"]
    dim = [" ".join(p["lines"]) for p in before["prims"] if p["k"] == "text" and p.get("alpha")]
    assert any("12:48 pm" in d for d in dim) and not any("7:10 pm ·" in d for d in dim)
    line = next(p for p in before["prims"] if p["k"] == "line" and p["tok"] == "viz-now-line")
    assert line["x0"][0] == "f" and abs(line["x0"][1] - (14 + 52 / 60) / 24) < 1e-6
    assert before["live"] == [] and c["live"]


def test_apply_live_is_pure_and_deterministic():
    c = fx("curve_tide")
    snap = copy.deepcopy(c)
    now = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    a, b = LV.apply_live(c, now, "America/New_York"), LV.apply_live(c, now, "America/New_York")
    assert c == snap and canon.clir_hash(a) == canon.clir_hash(b)


def test_now_marker_hidden_outside_window():
    c = fx("curve_tide")
    out = LV.apply_live(c, datetime(2026, 9, 25, 5, 0, tzinfo=UTC), "America/New_York")
    ids = {p["id"] for p in out["prims"]}
    marker = c["live"][0]["args"]["prims"]
    assert not (set(marker) & ids)


def test_lerp_anchor():
    assert LV.lerp_anchor(["l", 0], ["r", 0], 0.25) == ["f", 0.25]
    assert LV.lerp_anchor(["l", 10], ["l", 110], 0.5) == ["l", 60.0]
    m = LV.lerp_anchor(["l", 20], ["r", 20], 0.5)
    assert m[0] == "m" and LV.resolve_x(m, 0, 440) == 220


def test_dst_and_midnight():
    tr = LV.dst_transitions(datetime(2026, 10, 2, 12, 0, tzinfo=UTC), "Australia/Sydney", 48)
    assert tr == [datetime(2026, 10, 3, 16, 0, tzinfo=UTC)]
    assert LV.next_midnight(datetime(2026, 9, 24, 18, 52, tzinfo=UTC), "America/New_York") == \
        datetime(2026, 9, 25, 4, 0, tzinfo=UTC)
    m1 = LV.next_midnight(datetime(2026, 10, 3, 12, 0, tzinfo=UTC), "Australia/Sydney")
    m2 = LV.next_midnight(m1, "Australia/Sydney")
    assert (m2 - m1).total_seconds() == 23 * 3600


def test_live_boundaries():
    c = fx("curve_tide")
    b = LV.live_boundaries(c, datetime(2026, 9, 24, 18, 52, tzinfo=UTC))
    assert datetime(2026, 9, 24, 23, 10, tzinfo=UTC) in b
