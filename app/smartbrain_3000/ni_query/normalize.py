"""NORMALIZE (Round 20, Q3a): the model's Query IR checked against the declared answer and cut
down to what the model owns — the v1 contract the lead re-scored the Q2 replies under (scratch
query_ir/rescore_v1.py: 50/60 few-shot TEST, 54/82 zero-shot ALL), ported rule by rule, plus
the recognizer validator of plan §E.

Structure (``structural_errors``): the answer is declared; select names cells of a list /
columns answer or OTHER value answers of a value answer; where / order cols are cells of the
chosen answer, "*" only with ``names``; time tokens closed; limit 1..100; params ≤ 6 scalars.
Every message states the violated rule and never lists an answer name or a cell path — they
reach the model's one retry.

v1 rules:
- time: an explicit recognized time phrase decides it (``explicit_time``: the recognizer's
  token, read by the question kind's direction — "this week" on a forecast is today..+7 days,
  on a schedule today..end of week, on past events the last 7 days); else the model's token
  stands. Compared, null ≡ {now, now} ≡ {now, null} (``time_key``).
- where (``v1_where``): never on the axis or a time / date cell (time travels in ``time``);
  numeric ops only on number cells with a number or a list of numbers; contains / names only
  on a text cell or "*" with a value that is a substring of the ask, is not a parameter-
  consumed entity and is not a word of the source's own name. Compared, names ≡ contains over
  "*" (``where_key``). Every other filter is dropped.
- order (``v1_order``): an order on the axis cell is implicit (dropped); a non-axis order is
  kept only for kind ``ranking``.
- answer: a declared count value ≡ its list + count when nothing narrows (``count_equiv``).
Recognizer validator (plan §E — beyond v1, so the eval reports it apart):
- ``stated_numbers``: a numeric ``where`` value must be a recognized number of the ask or a
  literal number in it ("IncidentSize > 0", "weather_code in [95, 96, 99]" are dropped).
"""
from __future__ import annotations

import datetime as dt
import math

from .decl import answer_named, axis_of, cell_types, cells_of

NUM_OPS = frozenset({"<", "<=", ">", ">=", "=", "!=", "in"})
TEXT_OPS = frozenset({"contains", "names"})
FIXED_TOKENS = frozenset({"now", "today", "tonight", "tomorrow", "yesterday", "weekend", "this_week", "next_week",
                          "this_month"})
_COUNTED = {"next_days": 2, "past_days": 3, "next_hours": 3, "past_hours": 3}   # token → most digits
_DOW = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})
# question kind → which way a relative stretch runs: forward (forecasts), ahead (a calendar of
# coming events), back (dated past rows). Any other kind reads "this week" as the calendar week.
DIRECTIONS = {"forecast": "forward", "text_brief": "forward", "map": "forward",
              "schedule": "ahead", "next_event": "ahead", "lookup": "ahead",
              "count": "back", "latest_items": "back", "trend": "back", "result": "back", "compare": "back"}
_MAX_CLAUSE = 12
_STRIP = ",.;:!?()[]{}\"'+%$#"


def token_ok(token: object) -> bool:
    """A closed IR time token (ir.schema.json's pattern; a ``date:`` must also be a real day)."""
    assert token is None or isinstance(token, (str, int, float)), "a scalar token"
    if not isinstance(token, str):
        return False
    if token in FIXED_TOKENS:
        return True
    head, _, arg = token.partition(":")
    assert isinstance(arg, str), "arg is a str"
    if head in _COUNTED:
        return arg.isascii() and arg.isdigit() and 1 <= len(arg) <= _COUNTED[head]
    if head == "dow":
        return arg in _DOW
    if head == "year":
        return len(arg) == 4 and arg.isascii() and arg.isdigit()
    if head == "month":
        return len(arg) == 7 and arg[4] == "-" and arg.replace("-", "").isdigit() and 1 <= int(arg[5:]) <= 12
    if head == "date" and len(arg) == 10:
        try:
            dt.date.fromisoformat(arg)
            return True
        except ValueError:
            return False
    return False


def _row_errors(ir: dict, answer: dict, answers: list[dict]) -> list[str]:
    """select / where / order against the chosen answer's own ids."""
    assert isinstance(ir, dict) and isinstance(answer, dict), "ir + answer required"
    assert isinstance(answers, list), "answers must be a list"
    errs, cells = [], {c["path"] for c in cells_of(answer)}
    select = [s for s in ir.get("select") or []][:_MAX_CLAUSE]
    if answer.get("kind") == "value":
        others = {a.get("name") for a in answers if a.get("kind") == "value" and a is not answer}
        if any(s not in others for s in select):
            errs.append("select on a value answer names only OTHER value answers, else []")
    elif any(s not in cells for s in select):
        errs.append("select names only cell paths of the chosen answer, else []")
    for w in (ir.get("where") or [])[:_MAX_CLAUSE]:
        if w.get("col") == "*" and w.get("op") != "names":
            errs.append('where col "*" goes only with op "names"')
        elif w.get("col") != "*" and w.get("col") not in cells:
            errs.append("where col is a cell path of the chosen answer, or * with op names (a value answer has "
                        "no cells)")
    if any(o.get("col") not in cells for o in (ir.get("order") or [])[:_MAX_CLAUSE]):
        errs.append("order col is a cell path of the chosen answer (a value answer has no cells: order [])")
    return errs


def structural_errors(ir: object, answers: list[dict]) -> list[str]:
    """Rule-only messages for a schema-valid IR that breaks the declared answer (≤ 4)."""
    assert isinstance(answers, list) and answers, "answers required"
    assert all(isinstance(a, dict) for a in answers), "answers must be dicts"
    if not isinstance(ir, dict):
        return ["the reply is one JSON object with the eight keys"]
    answer = answer_named(answers, ir.get("answer"))
    if answer is None:
        return ["answer is one of the declared answer names, copied exactly"]
    errs = _row_errors(ir, answer, answers)
    time = ir.get("time")
    if isinstance(time, dict) and any(v is not None and not token_ok(v) for v in (time.get("from"), time.get("to"))):
        errs.append("time from and to are tokens from the TOKENS line, or null")
    limit = ir.get("limit")
    if limit is not None and not (isinstance(limit, int) and 1 <= limit <= 100):
        errs.append("limit is an integer from 1 to 100, or null")
    params = ir.get("params") if isinstance(ir.get("params"), dict) else {}
    if len(params) > 6 or any(isinstance(v, bool) or not isinstance(v, (str, int, float)) for v in params.values()):
        errs.append("params holds at most 6 text values")
    return list(dict.fromkeys(errs))[:4]


def direction_of(kind: str | None) -> str | None:
    assert kind is None or isinstance(kind, str), "kind must be a str"
    direction = DIRECTIONS.get(kind or "")
    assert direction in (None, "forward", "ahead", "back"), "closed directions"
    return direction


def _pair(token: str, kind: str | None) -> dict:
    """One recognized token as an IR time object, read by the kind's direction."""
    assert isinstance(token, str) and token, "token required"
    assert token_ok(token), "a closed token"
    if token == "now":
        return {"from": "now", "to": "now"}
    if token == "this_week":
        by = {"forward": ("today", "next_days:7"), "ahead": ("today", "this_week"), "back": ("past_days:7", "today")}
        lo, hi = by.get(direction_of(kind) or "", ("this_week", "this_week"))
        return {"from": lo, "to": hi}
    head = token.split(":", 1)[0]
    if head in ("past_days", "past_hours"):
        return {"from": token, "to": "today" if head == "past_days" else "now"}
    if head in ("next_days", "next_hours"):
        return {"from": "today" if head == "next_days" else "now", "to": token}
    return {"from": token, "to": token}


def explicit_time(spans: dict, kind: str | None) -> dict | None:
    """The IR time an explicit recognized phrase states, or None (the model's token stands).
    A holiday beside another time phrase is the subject, not the stretch ("Thanksgiving in
    2027" → year:2027); two phrases make one stretch ("today and tomorrow")."""
    assert isinstance(spans, dict) and "time" in spans, "recognized spans required"
    assert kind is None or isinstance(kind, str), "kind must be a str"
    told = [s for s in spans["time"] if s.get("token") and s.get("kind") != "duration" and token_ok(s["token"])]
    plain = [s for s in told if s["kind"] != "holiday"] or told
    if not plain:
        return None
    first, last = _pair(plain[0]["token"], kind), _pair(plain[-1]["token"], kind)
    return {"from": first["from"], "to": last["to"]}


def time_key(time: dict | None) -> object:
    """The compared form of a time clause: null ≡ {now, now} ≡ {now, null}."""
    assert time is None or isinstance(time, dict), "time must be a dict or None"
    assert time is None or set(time) <= {"from", "to"}, "a closed time object"
    if time is None or (time.get("from") == "now" and time.get("to") in ("now", None)):
        return "NOW"
    return (time.get("from"), time.get("to"))


def _is_number(value: object) -> bool:
    assert value is None or isinstance(value, (str, int, float, list, dict, bool)), "a JSON value"
    assert not isinstance(value, bytes), "decoded values only"
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return ok and math.isfinite(float(value))


def consumed_value(value: str, consumed_words: frozenset[str]) -> bool:
    """Every word of ``value`` (whitespace-split, casefolded) is a parameter-consumed word."""
    assert isinstance(value, str), "value must be a str"
    assert isinstance(consumed_words, frozenset), "consumed_words must be a frozenset"
    parts = value.casefold().split()
    return bool(parts) and all(p in consumed_words for p in parts)


def v1_where(where: list[dict], answer: dict | None, ask: str, consumed_words: frozenset[str],
             src_words: frozenset[str]) -> list[dict]:
    """The v1 where rules (module docstring); the surviving filters, the model's own shape."""
    assert isinstance(where, list) and isinstance(ask, str), "where + ask required"
    assert isinstance(src_words, frozenset), "src_words must be a frozenset"
    types, axis, askc = cell_types(answer), axis_of(answer), ask.casefold()
    out = []
    for w in where[:_MAX_CLAUSE]:
        col, op, val = w.get("col"), w.get("op"), w.get("value")
        if col == axis or types.get(col) in ("time", "date"):
            continue
        if op in NUM_OPS and types.get(col) == "number":
            if _is_number(val) or (isinstance(val, list) and all(_is_number(v) for v in val)):
                out.append(dict(w))
            continue
        if op in TEXT_OPS and isinstance(val, str) and (col == "*" or types.get(col) == "text"):
            v = val.casefold().strip()
            if v and v in askc and not consumed_value(v, consumed_words) and v not in src_words:
                out.append(dict(w))
    return out


def literal_numbers(ask: str) -> frozenset[float]:
    """Numbers written in the ask as plain tokens ("4.0", "4.5+", "2,500")."""
    assert isinstance(ask, str), "ask must be a str"
    out = set()
    for tok in ask.split()[:400]:  # bounded
        try:
            x = float(tok.strip(_STRIP).replace(",", ""))
        except ValueError:
            continue
        if math.isfinite(x):
            out.add(x)
    assert all(math.isfinite(x) for x in out), "finite numbers only"
    return frozenset(out)


def stated_numbers(where: list[dict], spans: dict, ask: str) -> list[dict]:
    """The recognizer validator: a numeric filter survives only with numbers the ask states."""
    assert isinstance(where, list) and isinstance(spans, dict), "where + spans required"
    assert isinstance(ask, str), "ask must be a str"
    known = {float(n["value"]) for n in spans.get("numbers") or []} | literal_numbers(ask)
    out = []
    for w in where[:_MAX_CLAUSE]:
        vals = w.get("value") if isinstance(w.get("value"), list) else [w.get("value")]
        numeric = w.get("op") in NUM_OPS and any(_is_number(v) for v in vals)
        if numeric and not all(_is_number(v) and float(v) in known for v in vals):
            continue
        out.append(w)
    return out


def v1_order(order: list[dict], answer: dict | None, kind: str | None) -> list[dict]:
    """The axis order is implicit; a non-axis order only ranks a ``ranking`` ask."""
    assert isinstance(order, list), "order must be a list"
    assert kind is None or isinstance(kind, str), "kind must be a str"
    axis = axis_of(answer)
    kept = [dict(o) for o in order[:_MAX_CLAUSE] if o.get("col") != axis]
    return kept if kind == "ranking" else []


def where_key(where: list[dict]) -> frozenset:
    """The compared form of where: numbers as floats, lists sorted, names ≡ contains over "*"."""
    assert isinstance(where, list), "where must be a list"
    assert len(where) <= _MAX_CLAUSE * 2, "where bounded"
    out = set()
    for w in where:
        col, op, val = w.get("col"), w.get("op"), w.get("value")
        if op in TEXT_OPS and isinstance(val, str):
            out.add(("*", "contains", val.casefold().strip()))
        elif isinstance(val, list):
            out.add((col, op, tuple(sorted(float(v) for v in val if _is_number(v)))))
        elif _is_number(val):
            out.add((col, op, float(val)))
        else:
            out.add((col, op, str(val).casefold().strip()))
    return frozenset(out)


def order_key(order: list[dict], answer: dict | None, kind: str | None) -> tuple:
    """The compared form of order (``v1_order``'s survivors)."""
    assert isinstance(order, list), "order must be a list"
    assert kind is None or isinstance(kind, str), "kind must be a str"
    return tuple((o.get("col"), o.get("dir")) for o in v1_order(order, answer, kind))


def narrows(ir: dict) -> bool:
    """Does a filter or a time cut (other than now) narrow the rows? (rescore's count rule)"""
    assert isinstance(ir, dict), "ir must be a dict"
    time = ir.get("time")
    assert time is None or isinstance(time, dict), "time must be a dict or None"
    return bool(ir.get("where")) or (time is not None and not (time.get("from") == "now" and time.get("to") == "now"))


def count_equiv(model_answer: object, gold: dict, answers: list[dict]) -> bool:
    """A declared count value answers a count ask as its list + count does when nothing narrows."""
    assert isinstance(gold, dict) and "answer" in gold, "a gold IR"
    assert isinstance(answers, list), "answers must be a list"
    picked = answer_named(answers, model_answer) or {}
    listed = answer_named(answers, gold["answer"]) or {}
    return gold.get("agg") == "count" and picked.get("type") == "count" and \
        picked.get("path") == listed.get("path") and not narrows(gold)
