"""The closed set of code sentences (CONTRACTS.md 5.1). Every on-screen string that
is not a formatted cell, a label, the ask or the title comes from here. No model
text ever reaches the screen.
"""
from __future__ import annotations

TEMPLATES: dict[str, str] = {
    # counts and overflow
    "more": "+{n} more",
    "more_status": "+{n} more · {status}",
    "n_items": "{n} active",
    "n_of": "{n} of {total}",
    "all_status": "All {n} · {status}",
    "value_label": "{value} {label}",
    "not_returned": "{name} · not returned",
    "filtered_out": "{n} more not shown (filtered by your words)",
    "filtered_inactive": "{n} more not in effect now",
    "filtered_request": "{n} more not matching the request",
    "other": "Other",
    # empty / failing / waiting states
    "none_now": "Nothing active right now",
    "no_rows": "No data in this update",
    "no_items": "Nothing here right now",
    "checked": "Checked",
    "not_ready": "Source is still preparing this data · retrying",
    "update_failed": "Update failed · showing last good data",
    "needs_update": "Needs an update · last data {date}",
    "not_published": "{day}: not published yet",
    "no_upcoming": "Nothing upcoming in this data",
    # honesty markers
    "sample": "Sample data from {date}",
    "sample_short": "Sample · {date}",
    "dates_inferred": "dates inferred",
    # what the data cannot answer (round 1: designed honesty, from record flags set by the data stage)
    "gap_latest": "Latest data {date}",
    "gap_none_latest": "None {frame} · latest {date}",
    "gap_none_next": "None {frame} · next {date}",
    "gap_none": "None {frame} in this data",
    "gap_none_active_latest": "None in effect now · latest {date}",
    "gap_none_active_next": "None in effect now · next {date}",
    "gap_no_match": "No “{q}” in this data",
    "gap_ids_only": "Only IDs in this data",
    "gap_unnamed": "Fields unnamed in this data",
    "gap_few": "Only {n} readings in this data",
    "gap_capped": "First {n} results only",
    "gap_none_of": "None of {n} in this data",
    "gap_unit_unstated": "Unit not stated in this data",
    "gap_unit_other": "In {has}, not {unit}",
    "gap_capped_total": "{n} of {total} shown",
    "gap_short": "{text}",
    # Port note (2026-10-06): pg_* templates served pipeline/program (not ported); dropped.
    "interp_legend": "Curve estimated between published times",
    "loop": "loop · {n} frames",
    # sentences with data
    "kind_value_at": "{kind} {value} at",
    "kind_at": "{kind} at",
    "next_kind": "Next {kind}",
    "vs": "vs {label}",
    "label_value": "{label} {value}",
    "since": "since",
    "of_goal": "of {goal}",
    "over_by": "over by {x}",
    "min_max": "Low {lo} · High {hi}",
    "range": "{lo} – {hi}",
    "lowest_at": "Lowest {time}",
    "highest_at": "Highest {time}",
    "read_more": "Read more",
    # words
    "rising": "Rising",
    "falling": "Falling",
    "high": "High",
    "low": "Low",
    "latest": "Latest",
    "min": "Min",
    "max": "Max",
    "avg": "Avg",
    "now": "Now",
    "next": "Next",
    "yes": "Yes",
    "no": "No",
}


def render(tid: str, **args) -> str:
    return TEMPLATES[tid].format(**args)
