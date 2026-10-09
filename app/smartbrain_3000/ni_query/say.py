"""SAY (Round 20, Q3a): the interpretation line — the final Query IR rendered by code, never by a
model, for the C2 VerifyPanel and the card footer (Q3b seals it as ``spec.interpretation``):

    <Answer label>[ (<selected labels>)] · <where phrases> · <time phrase> · <order / limit phrase> · count

Cells by their LABELS (never a path), tokens in plain words: "Earthquakes · Magnitude > 4 ·
past 7 days · count", "Daily forecast (Rain chance) · next 7 days", "Public holidays · Holiday
contains Thanksgiving · in 2027". A line that misstates the ask is the release gate's "silently
wrong" — so every part comes from the IR the card executes.
"""
from __future__ import annotations

import datetime as dt

from .decl import answer_named, axis_of, cells_of

_DAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
         "sat": "Saturday", "sun": "Sunday"}
_FIXED = {"now": "now", "today": "today", "tonight": "tonight", "tomorrow": "tomorrow", "yesterday": "yesterday",
          "weekend": "this weekend", "this_week": "this week", "next_week": "next week", "this_month": "this month"}
_COUNTED = ("next_days", "past_days", "next_hours", "past_hours")
_SEP = " · "


def token_words(token: str | None) -> str:
    """One IR time token in plain words ("dow:fri" → "on Friday", "year:2027" → "in 2027")."""
    assert token is None or isinstance(token, str), "token must be a str or None"
    if not token:
        return ""
    if token in _FIXED:
        return _FIXED[token]
    head, _, arg = token.partition(":")
    assert isinstance(arg, str), "arg is a str"
    if head in _COUNTED:
        side, unit = head.split("_")
        return f"{side} {unit[:-1]}" if arg == "1" else f"{side} {int(arg)} {unit}"
    if head == "dow":
        return f"on {_DAYS.get(arg, arg)}"
    if head == "date":
        day = dt.date.fromisoformat(arg)
        return f"on {day.strftime('%B')} {day.day}, {day.year}"
    if head == "month":
        return f"in {dt.date(int(arg[:4]), int(arg[5:]), 1).strftime('%B %Y')}"
    return f"in {arg}" if head == "year" else token


def time_words(time: dict | None) -> str:
    """The time clause in plain words; "" when it is null."""
    assert time is None or isinstance(time, dict), "time must be a dict or None"
    assert time is None or set(time) <= {"from", "to"}, "a closed time object"
    if not time:
        return ""
    lo, hi = time.get("from"), time.get("to")
    if lo == hi:
        return token_words(lo)
    if hi is None or lo is None:
        return f"from {token_words(lo)}" if lo else f"until {token_words(hi)}"
    if lo in ("now", "today") and (str(hi).startswith("next_") or hi == "this_week"):
        return token_words(hi)
    if str(lo).startswith("past_") and hi in ("now", "today"):
        return token_words(lo)
    return f"{token_words(lo)} to {token_words(hi)}"


def _number(value: object) -> str:
    assert value is None or isinstance(value, (int, float, str)), "a scalar"
    assert not isinstance(value, bool), "not a bool"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _where_words(w: dict, labels: dict[str, str]) -> str:
    assert isinstance(w, dict), "a where entry"
    assert isinstance(labels, dict), "labels must be a dict"
    col, op, val = w.get("col"), w.get("op"), w.get("value")
    if op == "names":
        return f"mentions {val}" if col == "*" else f"{labels.get(col, 'a field')} mentions {val}"
    label = labels.get(col, "a field")
    if op == "contains":
        return f"{label} contains {val}"
    if op == "in":
        return f"{label} in " + ", ".join(_number(v) for v in (val if isinstance(val, list) else [val]))
    return f"{label} {op} {_number(val)}" if op != "=" else f"{label} is {_number(val)}"


def _order_words(ir: dict, answer: dict, labels: dict[str, str]) -> str:
    """Order + limit in words: top 5 by Price, the next one, newest first, the latest one, 3 shown."""
    assert isinstance(ir, dict) and isinstance(answer, dict), "ir + answer required"
    assert isinstance(labels, dict), "labels must be a dict"
    order, limit = list(ir.get("order") or []), ir.get("limit")
    if not order:
        if limit == 1:
            return "the latest one" if answer.get("newest_first") else "the first one"
        return f"{limit} shown" if limit else ""
    col, desc = order[0].get("col"), order[0].get("dir") == "desc"
    timed = col == axis_of(answer) or any(c["path"] == col and c.get("type") in ("time", "date")
                                          for c in cells_of(answer))
    if timed:
        if limit == 1:
            return "the latest one" if desc else "the next one"
        return ("newest first" if desc else "soonest first") + (f", {limit} shown" if limit else "")
    label = labels.get(col, "a field")
    if limit:
        return f"{'top' if desc else 'bottom'} {limit} by {label}"
    return f"{'highest' if desc else 'lowest'} {label} first"


def say(ir: dict, answers: list[dict]) -> str:
    """The interpretation line of ``ir`` over its declared ``answers``."""
    assert isinstance(ir, dict) and isinstance(answers, list), "ir + answers required"
    answer = answer_named(answers, ir.get("answer"))
    assert answer is not None, "the IR's answer is declared"
    labels = {c["path"]: str(c.get("label") or c["path"].rsplit(".", 1)[-1]) for c in cells_of(answer)}
    names = {str(a.get("name")): str(a.get("label") or a.get("name")) for a in answers}
    head = str(answer.get("label") or answer.get("name"))
    picked = [labels.get(s) or names.get(s) or "" for s in ir.get("select") or []]
    if any(picked):
        head += " (" + ", ".join(p for p in picked if p) + ")"
    parts = [head] + [_where_words(w, labels) for w in ir.get("where") or []]
    parts += [time_words(ir.get("time")), _order_words(ir, answer, labels)]
    parts.append("count" if ir.get("agg") == "count" else "")
    return _SEP.join(p for p in parts if p)
