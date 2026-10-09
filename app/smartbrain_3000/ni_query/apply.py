"""APPLY (Round 20, Q3a): a Query IR executed over one fetched payload — the Q2 measurement's
``apply_ir`` ported, deterministic and pure Python (no DuckDB). The binding into the engine's
pipeline is Q3b; this is the reference semantics the eval scores execution agreement with.

1. Rows: a list answer reads the array at ``answer.path`` (a dict there is one row); a columns
   answer zips its parallel arrays along the axis cell; a value answer is one pseudo-row; a
   ``count`` value reads the rows of the array it counts. Then the declared ``filter`` (the
   caller fills ``{param}`` first, as ``ni_flow._fill_answer`` does) and ``newest_first``.
2. ``where``: ``=`` / ``!=`` compare numbers as numbers and text casefolded; ``<``..``>=``
   numeric; ``in`` membership; ``contains`` a casefolded substring; ``names`` the same over
   every text cell (``col: "*"``) or one cell.
3. ``time``: the cut [start(from), end(to)) on the axis cell, else the first time / date cell.
   Tokens resolve in ``zone`` at ``now``: today / tomorrow / yesterday = that local day; tonight
   18:00 → 06:00; this_week / next_week ISO weeks; this_month; next_days:N = [now, today + N);
   past_days:N = [today − N, now]; next_hours / past_hours around now; date / month / year their
   calendar span. ``direction`` (the CALLER's: "forward" for a forecast axis, "backward" for
   dated past rows) places the relative ones: dow:x is that weekday within today..today+6
   forward, today−6..today backward; weekend is the coming (current on Sat/Sun) or the last.
   A date row or an hour-step columns row covers a span kept when it overlaps the cut; any
   other row is an instant kept when start <= t < end; a row whose time does not parse is
   dropped. A point cut (now..now) keeps the span holding now and leaves an instant list
   (alerts, fires, coins) uncut.
4. ``order`` (numbers numerically, times by instant, missing last), ``limit``, ``agg``.

Result: ``{"rows": [...], "indices": [...], "count": int | None}`` — ``indices`` are positions
in the declared row source (0 for a value answer), so two IRs over the same rows compare.
"""
from __future__ import annotations

import datetime as dt
import email.utils
from zoneinfo import ZoneInfo

from .decl import axis_of, cells_of, time_cell

_UTC = dt.UTC
_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MAX_ROWS = 5000
_MAX_PATH = 32
DIRECTIONS = ("forward", "backward")


def dig(obj: object, path: str) -> object:
    """``a.b[2].c`` → the value, None on any miss (dots between keys, ``[n]`` list indices)."""
    assert isinstance(path, str), "path must be a str"
    assert len(path) <= 400, "path bounded"
    cur, key, i = obj, "", 0
    steps: list[object] = []
    for _ in range(len(path) + 1):  # bounded: one pass over the path
        if i >= len(path) or path[i] in ".[":
            if key:
                steps.append(key)
            key = ""
            if i < len(path) and path[i] == "[":
                close = path.find("]", i)
                if close < 0 or not path[i + 1:close].isdigit():
                    return None
                steps.append(int(path[i + 1:close]))
                i = close
            if i >= len(path):
                break
        else:
            key += path[i]
        i += 1
    for step in steps[:_MAX_PATH]:
        if isinstance(step, int):
            cur = cur[step] if isinstance(cur, list) and step < len(cur) else None
        else:
            cur = cur.get(step) if isinstance(cur, dict) else None
    return cur


def rows_of(answer: dict, payload: object) -> tuple[list[tuple[int, dict]], str]:
    """[(index, row)] in the declared order, and the row kind (list | columns | value | count)."""
    assert isinstance(answer, dict) and answer.get("kind") in ("value", "list", "columns"), "a declared answer"
    assert payload is not None, "payload required"
    if answer["kind"] == "value":
        v = dig(payload, str(answer.get("path") or ""))
        if answer.get("type") == "count" and isinstance(v, list):
            return list(enumerate(v[:_MAX_ROWS])), "count"
        return [(0, {"__value__": v})], "value"
    cells = cells_of(answer)
    if answer["kind"] == "columns":
        axis = axis_of(answer) or cells[0]["path"]
        arrays = {c["path"]: dig(payload, c["path"]) for c in cells}
        n = len(arrays.get(axis) or []) if isinstance(arrays.get(axis), list) else 0
        return [(i, {"__cols__": {p: (a[i] if isinstance(a, list) and i < len(a) else None) for p, a in arrays.items()}})
                for i in range(min(n, _MAX_ROWS))], "columns"
    items = dig(payload, str(answer.get("path") or ""))
    items = [items] if isinstance(items, dict) else (items if isinstance(items, list) else [])
    out = list(enumerate(items[:_MAX_ROWS]))
    flt = answer.get("filter")
    if flt:
        out = [(i, r) for i, r in out if str(dig(r, flt["path"])) == str(flt["equals"])]
    if answer.get("newest_first"):
        out.reverse()
    return out, "list"


def cell_value(row: dict, path: str) -> object:
    assert isinstance(row, dict), "row must be a dict"
    assert isinstance(path, str), "path must be a str"
    if "__cols__" in row:
        return row["__cols__"].get(path)
    if "__value__" in row:
        return row["__value__"]
    return dig(row, path)


def _day(d: dt.date, tz: ZoneInfo) -> dt.datetime:
    assert isinstance(d, dt.date), "d must be a date"
    assert isinstance(tz, ZoneInfo), "tz must be a ZoneInfo"
    return dt.datetime.combine(d, dt.time(0), tzinfo=tz)


def _relative(kind: str, arg: str, today: dt.date, direction: str) -> dt.date:
    """The start day of a dow / weekend token, placed by ``direction``."""
    assert kind in ("dow", "weekend"), "a relative token"
    assert direction in DIRECTIONS, "a closed direction"
    wd = today.weekday()
    if kind == "dow":
        target = _DOW.index(arg)
        step = (target - wd) % 7
        return today + dt.timedelta(days=step) if direction == "forward" else today - dt.timedelta(days=(wd - target) % 7)
    if wd >= 5:
        return today - dt.timedelta(days=wd - 5)
    return today + dt.timedelta(days=5 - wd) if direction == "forward" else today - dt.timedelta(days=wd + 2)


def token_interval(token: str, now: dt.datetime, tz: ZoneInfo, direction: str) -> tuple[dt.datetime, dt.datetime]:
    """One IR token → [start, end) in ``tz`` at ``now`` (module docstring, step 3)."""
    assert isinstance(token, str) and token, "token required"
    assert direction in DIRECTIONS, "a closed direction"
    n = now.astimezone(tz)
    today, one = n.date(), dt.timedelta(days=1)
    fixed = {"now": (n, n), "today": (_day(today, tz), _day(today + one, tz)),
             "tonight": (dt.datetime.combine(today, dt.time(18), tzinfo=tz),
                         dt.datetime.combine(today + one, dt.time(6), tzinfo=tz)),
             "tomorrow": (_day(today + one, tz), _day(today + 2 * one, tz)),
             "yesterday": (_day(today - one, tz), _day(today, tz))}
    if token in fixed:
        return fixed[token]
    monday = today - dt.timedelta(days=today.weekday())
    if token in ("this_week", "next_week"):
        start = monday + (7 * one if token == "next_week" else dt.timedelta(0))
        return _day(start, tz), _day(start + 7 * one, tz)
    if token in ("this_month", "weekend"):
        if token == "weekend":
            sat = _relative("weekend", "", today, direction)
            return _day(sat, tz), _day(sat + 2 * one, tz)
        first = today.replace(day=1)
        return _day(first, tz), _day((first + 32 * one).replace(day=1), tz)
    return _counted_interval(token, n, tz, direction)


def _counted_interval(token: str, n: dt.datetime, tz: ZoneInfo, direction: str) -> tuple[dt.datetime, dt.datetime]:
    assert ":" in token, "a counted or calendar token"
    assert n.tzinfo is not None, "n must be aware"
    kind, _, arg = token.partition(":")
    today, one = n.date(), dt.timedelta(days=1)
    if kind in ("next_days", "past_days"):
        return (n, _day(today + int(arg) * one, tz)) if kind == "next_days" else (_day(today - int(arg) * one, tz), n)
    if kind in ("next_hours", "past_hours"):
        span = dt.timedelta(hours=int(arg))
        return (n, n + span) if kind == "next_hours" else (n - span, n)
    if kind == "dow":
        d = _relative("dow", arg, today, direction)
        return _day(d, tz), _day(d + one, tz)
    if kind == "date":
        d = dt.date.fromisoformat(arg)
        return _day(d, tz), _day(d + one, tz)
    if kind == "month":
        first = dt.date(int(arg[:4]), int(arg[5:7]), 1)
        return _day(first, tz), _day((first + 32 * one).replace(day=1), tz)
    if kind == "year":
        return _day(dt.date(int(arg), 1, 1), tz), _day(dt.date(int(arg) + 1, 1, 1), tz)
    raise ValueError(f"unknown time token {token!r}")


def _plain_date(s: str) -> bool:
    assert isinstance(s, str), "s must be a str"
    assert len(s) <= 400, "a value, not a text"
    return len(s) == 10 and s[4] == "-" and s[7] == "-" and s.replace("-", "").isdigit()


def parse_time(value: object, cell: dict, payload: object, tz: ZoneInfo) -> tuple[str, object] | None:
    """("date", date) | ("instant", aware datetime) | None — epoch numbers (ms when > 1e11), ISO
    dates and datetimes (zoneless: the payload's ``utc_offset_seconds``, else UTC for a ``utc``
    cell, else ``tz``), RFC 2822 (feeds)."""
    assert isinstance(cell, dict), "cell must be a dict"
    assert isinstance(tz, ZoneInfo), "tz must be a ZoneInfo"
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        x = float(value) / (1000.0 if float(value) > 1e11 else 1.0)
        return "instant", dt.datetime.fromtimestamp(x, _UTC)
    s = str(value).strip()
    if _plain_date(s):
        return "date", dt.date.fromisoformat(s)
    iso = s.replace("Z", "+00:00")
    if len(iso) > 11 and _plain_date(iso[:10]) and iso[10] == " " and iso[11:12].isdigit():
        iso = iso[:10] + "T" + iso[11:]
    try:
        d = dt.datetime.fromisoformat(iso)
    except ValueError:
        d = None
    if d is not None:
        if d.tzinfo is None:
            offset = payload.get("utc_offset_seconds") if isinstance(payload, dict) else None
            zone = dt.timezone(dt.timedelta(seconds=offset)) if isinstance(offset, int) and not isinstance(offset, bool) \
                else (_UTC if cell.get("utc") else tz)
            d = d.replace(tzinfo=zone)
        return "instant", d
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        return None
    return "instant", (d if d.tzinfo is not None else d.replace(tzinfo=_UTC))


def _covers(pt: tuple, answer: dict, cell: dict, tz: ZoneInfo) -> tuple | None:
    """The span a row covers (a date; an hour-step columns row), else None (an instant)."""
    assert isinstance(pt, tuple) and len(pt) == 2, "a parsed time"
    assert isinstance(answer, dict), "answer must be a dict"
    kind, v = pt
    if kind == "date":
        return _day(v, tz), _day(v + dt.timedelta(days=1), tz)
    axis = answer.get("axis") or {}
    if answer.get("kind") == "columns" and axis.get("step") == "hour" and axis.get("cell") == cell["path"]:
        return v, v + dt.timedelta(hours=1)
    return None


def time_cut(rows: list, answer: dict, time: dict | None, payload: object, now: dt.datetime, tz: ZoneInfo,
             direction: str) -> list:
    """Step 3 of the module docstring."""
    assert isinstance(rows, list), "rows must be a list"
    assert direction in DIRECTIONS, "a closed direction"
    cell = time_cell(answer)
    if not time or (time.get("from") is None and time.get("to") is None) or cell is None:
        return rows
    point = time.get("from") == "now" and time.get("to") == "now"
    lo = token_interval(time["from"], now, tz, direction)[0] if time.get("from") else None
    hi = token_interval(time["to"], now, tz, direction)[1] if time.get("to") else None
    n, out, parsed = now.astimezone(tz), [], []
    for idx, row in rows:  # bounded by _MAX_ROWS
        pt = parse_time(cell_value(row, cell["path"]), cell, payload, tz)
        if pt is None:
            continue
        sp = _covers(pt, answer, cell, tz)
        parsed.append(sp)
        if point:
            keep = sp is None or sp[0] <= n < sp[1]
        elif sp is not None:
            keep = (hi is None or sp[0] < hi) and (lo is None or sp[1] > lo)
        else:
            keep = (lo is None or pt[1] >= lo) and (hi is None or pt[1] < hi)
        if keep:
            out.append((idx, row))
    if point and all(sp is None for sp in parsed):
        return rows      # an instant list: "right now" is the list as it stands
    return out


def _num(x: object) -> float | None:
    assert x is None or isinstance(x, (str, int, float, bool, list, dict)), "a JSON value"
    assert not isinstance(x, bytes), "decoded text only"
    if isinstance(x, bool):
        return None
    try:
        return float(x) if isinstance(x, (int, float)) else float(str(x).strip())
    except (TypeError, ValueError):
        return None


def _eq(a: object, b: object) -> bool:
    assert not isinstance(a, bytes) and not isinstance(b, bytes), "decoded values only"
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    assert na is None or nb is None, "one side is not a number"
    return str(a).strip().casefold() == str(b).strip().casefold()


def matches(row: dict, w: dict, answer: dict) -> bool:
    """One ``where`` entry against one row (step 2)."""
    assert isinstance(row, dict) and isinstance(w, dict), "row + where entry required"
    assert isinstance(answer, dict), "answer must be a dict"
    col, op, val = w.get("col"), w.get("op"), w.get("value")
    if op == "names":
        texts = ([row["__value__"]] if "__value__" in row else
                 [cell_value(row, c["path"]) for c in cells_of(answer) if c.get("type") == "text"]) if col == "*" \
            else [cell_value(row, str(col))]
        return any(t is not None and str(val).casefold() in str(t).casefold() for t in texts)
    cv = cell_value(row, str(col))
    if op == "contains":
        return cv is not None and str(val).casefold() in str(cv).casefold()
    if op in ("in", "="):
        vals = val if (op == "in" and isinstance(val, list)) else [val]
        return cv is not None and any(_eq(cv, x) for x in vals)
    if op == "!=":
        return cv is None or not _eq(cv, val)
    a, b = _num(cv), _num(val)
    if a is None or b is None:
        return False
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}.get(str(op), False)


def _sort_key(row: dict, col: str, answer: dict, payload: object, tz: ZoneInfo) -> object:
    assert isinstance(row, dict), "row must be a dict"
    assert isinstance(col, str), "col must be a str"
    cell = next((c for c in cells_of(answer) if c["path"] == col), {"path": col})
    v = cell_value(row, col)
    if cell.get("type") in ("time", "date"):
        pt = parse_time(v, cell, payload, tz)
        if pt is None:
            return None
        return _day(pt[1], tz).timestamp() if pt[0] == "date" else pt[1].timestamp()
    n = _num(v)
    return n if n is not None else (None if v is None else str(v).casefold())


def _ordered(rows: list, order: list[dict], answer: dict, payload: object, tz: ZoneInfo) -> list:
    """Step 4's order: stable sorts, last key first; missing values last."""
    assert isinstance(rows, list) and isinstance(order, list), "rows + order required"
    assert len(order) <= 3, "order bounded"
    for o in reversed(order):
        keyed = [(_sort_key(r, str(o.get("col")), answer, payload, tz), idx, r) for idx, r in rows]
        present = [k for k in keyed if k[0] is not None]
        missing = [k for k in keyed if k[0] is None]
        try:
            present.sort(key=lambda k: k[0], reverse=o.get("dir") == "desc")
        except TypeError:   # numbers beside text in one column: compare as text
            present.sort(key=lambda k: str(k[0]), reverse=o.get("dir") == "desc")
        rows = [(idx, r) for _, idx, r in present + missing]
    return rows


def apply_query(ir: dict, payload: object, answer: dict, *, now: dt.datetime, zone: str, direction: str) -> dict:
    """The IR over ``payload`` → ``{"rows", "indices", "count"}`` (module docstring)."""
    assert isinstance(ir, dict) and ir.get("answer") == answer.get("name"), "the IR's own declared answer"
    assert isinstance(now, dt.datetime) and now.tzinfo is not None and direction in DIRECTIONS, "now + direction"
    tz = ZoneInfo(zone)
    rows, kind = rows_of(answer, payload)
    for w in ir.get("where") or []:
        rows = [(idx, r) for idx, r in rows if matches(r, w, answer)]
    rows = time_cut(rows, answer, ir.get("time"), payload, now, tz, direction)
    rows = _ordered(rows, list(ir.get("order") or []), answer, payload, tz)
    if ir.get("limit"):
        rows = rows[:int(ir["limit"])]
    counted = ir.get("agg") == "count" or kind == "count"
    return {"rows": [r.get("__value__") if "__value__" in r else r.get("__cols__", r) for _, r in rows],
            "indices": [idx for idx, _ in rows], "count": len(rows) if counted else None}
