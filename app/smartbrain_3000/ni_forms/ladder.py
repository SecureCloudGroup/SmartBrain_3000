"""The deterministic relayout ladder (CONTRACTS.md 6.3).

hero_step -> drop_tick -> flip_label -> label_policy -> drop_items -> sibling.
`layout_span` loops layout -> lint -> next rung on red until clean or exhausted.
Forms read the rung state from ctx.rung; the hero also auto-fits its ladder
(31 -> 28 -> 24 -> 20) before anything is dropped, so hero_step only starts lower.
"""
from __future__ import annotations

LADDER = ("hero_step", "drop_tick", "flip_label", "label_policy", "drop_items", "sibling")
MAX_DROP = 8


def start() -> dict:
    return {"hero": 0, "drop_tick": 0, "flip": 0, "label_policy": 0, "drop": 0, "applied": []}


def step(rung: dict, issues: list, prims_by_id: dict) -> bool:
    """Advance one rung given the red issues. False when the ladder is exhausted
    (the span is then rejected at ENUMERATE with `lint_red`)."""
    red = [i for i in issues if i.sev == "red"]
    if not red:
        return False
    codes = {i.code for i in red}
    roles = {prims_by_id.get(i.prim, {}).get("role") for i in red if i.prim is not None}
    if codes & {"truncated_hero", "truncated_headline"} and rung["hero"] < 3:
        rung["hero"] += 1
        rung["applied"].append("hero_step")
        return True
    if "overlap" in codes or "out_of_box" in codes:
        if "tick" in roles and not rung["drop_tick"]:
            rung["drop_tick"] = 1
            rung["applied"].append("drop_tick")
            return True
        if "label" in roles and not rung["flip"]:
            rung["flip"] = 1
            rung["applied"].append("flip_label")
            return True
        if "label" in roles and rung["label_policy"] < 2:
            rung["label_policy"] += 1
            rung["applied"].append("label_policy")
            return True
    if rung["drop"] < MAX_DROP:
        rung["drop"] += 1
        rung["applied"].append("drop_items")
        return True
    return False
