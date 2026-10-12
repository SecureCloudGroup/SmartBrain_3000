"""Round 19, data-layer fix round 2 (datalayer-r2): one labeled test per class, over the
functions ni_flow.py / ni_forms/record.py gained or changed. Each test names its class in
its own docstring; the plan's rule freeze applies — these are the labeled-set deltas for
the classes this round measured and shipped.
"""
from __future__ import annotations

from smartbrain_3000 import ni_flow
from smartbrain_3000.ni_forms import record as ni_record
from smartbrain_3000.ni_forms.types import Field

# ---- class W1: the card never claims a quantity it does not show -------------------------

def test_five_columns_are_accepted_and_six_refused() -> None:
    """datalayer-r2: the per-answer cell cap is 5 (Open-Meteo's hourly tonight answer carries time,
    conditions, temperature, rain chance and snowfall); a sixth is still refused. A columns answer
    declares its cells under ``columns`` (a list answer under ``row``)."""
    time_col = {"path": "hourly.time", "label": "Time", "type": "time"}
    col = lambda i: {"path": f"hourly.c{i}", "label": f"C{i}", "type": "number"}
    base = {"kind": "columns", "name": "tonight", "label": "Tonight, hour by hour", "words": ["tonight"], "primary": True,
            "limit": 24, "axis": {"cell": "hourly.time", "step": "hour"}}
    assert ni_flow._clean_answer(dict(base, columns=[time_col, *(col(i) for i in range(4))])) is not None   # 5
    assert ni_flow._clean_answer(dict(base, columns=[time_col, col(0)])) is not None                       # 2
    assert ni_flow._clean_answer(dict(base, columns=[time_col, *(col(i) for i in range(5))])) is None       # 6


def test_w4_count_frame_kind_always_cuts_the_window() -> None:
    """class W4a: a count always cuts, even over rows that lie entirely in the past (the "recent
    observations" leniency _cuts otherwise applies) -- a stale row outside the asked stretch must not
    silently survive into a tally. Round 20 (Q3b): the count is the query layer's ``agg``."""
    assert ni_flow._cuts("next_days:7", [], [], {}, "t", count=True) is True


def test_w4_count_kind_skips_the_one_row_measure_demotion() -> None:
    """class W4b: _shape_rows's one-row -> "measure" demotion (fix round 1a-6, class H) must
    not fire for a count ask -- one surviving row is still the tally's one entity, not a
    stand-in reading."""
    outputs = {"rows": [{"mag": 2.7, "place": "Louisiana"}]}
    answer = {"cells": [{"path": "mag", "label": "Magnitude", "type": "number", "key": "mag"},
                        {"path": "place", "label": "Place", "type": "text", "key": "place"}],
              "kind": "list"}
    kind, fields, rows, _long = ni_record._shape_rows(answer, outputs, "rows", ask="earthquakes",
                                                      question_kind="count")
    assert kind != "measure"
    assert len(rows) == 1


def test_w4_count_kind_one_row_demotion_still_fires_for_other_kinds() -> None:
    """class W4b regression guard: the SAME one-row answer, asked without a count frame
    (e.g. a lookup), keeps the fix round 1a-6 demotion -- this round narrows the rule, it
    does not remove it."""
    outputs = {"rows": [{"mag": 2.7, "place": "Louisiana"}]}
    answer = {"cells": [{"path": "mag", "label": "Magnitude", "type": "number", "key": "mag"},
                        {"path": "place", "label": "Place", "type": "text", "key": "place"}],
              "kind": "list"}
    kind, fields, rows, _long = ni_record._shape_rows(answer, outputs, "rows", ask="earthquake",
                                                      question_kind="lookup")
    assert kind == "measure"
    assert isinstance(fields[0], Field)
