"""Provenance recipes: every text prim is built from a recipe that lint re-runs
against the record (CONTRACTS.md 6.4 `provenance`). A recipe is JSON-like data:

  ["cell", field, row, opts]      formatted cell   opts: sign, unit, dec, derived
  ["raw", field, row]             text cell, one line
  ["label", field]                humanised source key / lexicon label
  ["title"] | ["ask"] | ["host"] | ["host_short"] | ["cadence"]
  ["tpl", id, {arg: recipe}]      a templates.py sentence
  ["join", sep, [recipe, ...]]    joined parts (empty parts skipped)
  ["d", op, args, opts]           a value derived by code from cells:
        diff  [fa, ra, fb, rb]            a - b
        pct   [fa, ra, fb, rb]            100 (a - b) / b
        count [n]                         a code count (rows, overflow ...)
        agg   [field, "min"|"max"|"avg"|"sum", rows?]
        compass [field, row]
        relday  [iso_date, iso_today]
        age     [iso, now_iso]            "2 h old" (live binding age)
        countdown [now_iso, iso]          "in 4 h 18 m" (live binding countdown)
        date    [iso, fmt, zone]          a date string made by fmt.format_time
        ratio   [fa, ra, fb, rb]          100 a / b  (progress)
  ["abs", recipe]                 the recipe's text without its leading sign
  ["lit", s]                      punctuation only (no letters or digits)
  ["part", name, recipe]          the recipe evaluated against record part `name`
  ["lex", name, field, row]       display word from a generic lexicon (e.g. weather codes)
  ["lower", recipe]               first letter lower-cased (labels inside a sentence)
  ["d", "host", [field, row]]     registrable domain of a link cell
"""
from __future__ import annotations

from datetime import date, datetime

from . import fmt
from .templates import render

_LEAD = ("+", fmt.MINUS, "-")
# closed generic nouns a program's honesty statement may name when the ask does not word them
ASKW_NOUNS = frozenset({"names", "titles", "prices", "times", "dates", "units", "locations", "results",
                        "forecasts", "readings", "scores", "ratings", "details", "values", "counts", "rankings",
                        "descriptions", "images", "stations", "items", "reports", "current data", "recent data",
                        "a ranking", "a total", "an answer"})


def run(rc, R, inp, host: str):
    """Recipe -> string. `R` is a rec.R view; `inp` the CardInput."""
    k = rc[0]
    if k == "cell":
        _, name, row, opts = rc
        v = R.cell(row, name)
        if v is None:
            return ""
        f = R.f.get(name)
        if f is not None and f.type in ("datetime", "date") and isinstance(v, str):
            # a time shown inside a table or a fact reads as a time in the card's zone, never as raw ISO
            try:
                if opts.get("short"):      # a date for a list's meta line: the day, and the year when not this year
                    fy = str(getattr(R.rec.context, "fetched_at", "") or "")[:4]
                    return fmt.format_time(v, "MMM d" if str(v)[:4] == fy else "MMM d, yyyy", R.tz)
                return fmt.format_time(v, "MMM d" if f.type == "date" else "MMM d, h:mm a", R.tz)
            except (ValueError, KeyError, TypeError):
                return v
        return fmt.value_text(v, R.f[name], derived=opts.get("derived", False), sign=opts.get("sign", False),
                              dec=opts.get("dec"), unit=opts.get("unit", True), compact=opts.get("compact", False))
    if k == "raw":
        v = R.cell(rc[2], rc[1])
        if v is None:
            return ""
        al = ((getattr(R.rec, "flags", None) or {}).get("aliases") or {}).get(rc[1]) or {}
        if isinstance(v, str) and v in al:
            return fmt.one_line(al[v])        # the user's own word for a code the data abbreviates (round 3)
        f = R.f.get(rc[1]) if hasattr(R.f, "get") else None
        kinds = f is not None and f.role == "name" and len(set(R.col(rc[1]))) < len(R.rows)   # a repeating name is a kind
        if f is not None and f.type == "category" and (f.role not in ("name", "link", "link_secondary") or kinds) \
                and isinstance(v, str):
            v = fmt.machine_words(v)          # a machine enumeration reads as words (round 3)
        return fmt.one_line(v)
    if k == "label":
        return R.f[rc[1]].label
    if k == "title":
        return fmt.one_line(inp.title)
    if k == "ask":
        return fmt.one_line(inp.ask)
    if k == "host":
        return fmt.host_display(host)
    if k == "host_short":
        return fmt.host_short(host)
    if k == "cadence":
        return fmt.cadence(inp.cadence_s)
    if k == "tpl":
        return render(rc[1], **{a: run(v, R, inp, host) if isinstance(v, list) else v for a, v in rc[2].items()})
    if k == "join":
        parts = [run(p, R, inp, host) for p in rc[2]]
        return rc[1].join(p for p in parts if p)
    if k == "lex":
        e = lex_entry(rc[1], R.cell(rc[3], rc[2]))
        return (e or {}).get("word", "")
    if k == "lower":
        t = run(rc[1], R, inp, host)
        return t[:1].lower() + t[1:] if t[:2] != t[:2].upper() else t
    if k == "part":
        from .rec import R as RView
        return run(rc[2], RView(R.rec.parts[rc[1]]), inp, host)
    if k == "askw":
        # a phrase of the user's own words (or one closed generic noun) inside a code sentence (pipeline/program)
        w = fmt.one_line(str(rc[1])).strip()
        if w and (w.lower() in fmt.one_line(inp.ask).lower() or w.lower() in ASKW_NOUNS):
            return w
        raise ValueError("askw phrase is not in the ask")
    if k == "lit":
        if any(ch.isalnum() for ch in rc[1]):
            raise ValueError("literal recipes carry punctuation only")
        return rc[1]
    if k == "abs":
        s = run(rc[1], R, inp, host)
        return s[1:] if s[:1] in _LEAD else s
    if k == "d":
        return derived(rc[1], rc[2], rc[3] if len(rc) > 3 else {}, R)
    raise ValueError(f"unknown recipe {k!r}")


def derived_value(op: str, args: list, R):
    if op in ("diff", "pct", "ratio"):
        a, b = R.num(args[1], args[0]), R.num(args[3], args[2])
        if a is None or b is None:
            return None
        if op == "diff":
            return a - b
        if b == 0:
            return None
        return 100 * (a - b) / b if op == "pct" else 100 * a / b
    if op == "count":
        return args[0]
    if op == "agg":
        rows = args[2] if len(args) > 2 and args[2] is not None else range(len(R.rows))
        vals = [R.num(i, args[0]) for i in rows]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        return {"min": min(vals), "max": max(vals), "avg": sum(vals) / len(vals), "sum": sum(vals)}[args[1]]
    return None


def derived(op: str, args: list, opts: dict, R) -> str:
    if op == "compass":
        v = R.num(args[1], args[0])
        return "" if v is None else fmt.compass(v)
    if op == "quarter":                      # a quarterly period's start date reads as its quarter (round 3)
        dd = fmt.parse_t(args[0])
        return "" if dd is None else f"Q{(dd.month - 1) // 3 + 1} {dd.year}"
    if op == "relday":
        return fmt.rel_day(date.fromisoformat(args[0]), date.fromisoformat(args[1]))
    if op == "date":
        return fmt.format_time(args[0], args[1], args[2])
    if op == "host":
        v = R.cell(args[1], args[0])
        from urllib.parse import urlsplit
        try:
            return fmt.host_short(urlsplit(str(v)).hostname or "") if v else ""
        except ValueError:
            return ""
    if op == "age":
        a, b = fmt.parse_t(args[0]), fmt.parse_t(args[1])
        return fmt.age_text((b - a).total_seconds())
    if op == "countdown":
        a, b = fmt.parse_t(args[0]), fmt.parse_t(args[1])
        if not isinstance(b, datetime):
            b = fmt.day_start(b, R.tz)
        return fmt.countdown_text((b - a).total_seconds())
    v = derived_value(op, args, R)
    if v is None:
        return ""
    if op == "count":
        return f"{int(v):,}"
    fld = R.f.get(opts["field"]) if opts.get("field") else None
    if op == "pct" or op == "ratio":
        return fmt.num(v, opts.get("dec", 1 if op == "ratio" else 2), sign=opts.get("sign", False)) + "%"
    if op == "agg" and opts.get("dec") is None and fld is not None and args[1] != "avg":
        return fmt.value_text(v, fld, sign=opts.get("sign", False), unit=opts.get("unit", True))
    dec = opts.get("dec")
    if fld is not None:
        return fmt.value_text(v, fld, derived=dec is None, dec=dec, sign=opts.get("sign", False),
                              unit=opts.get("unit", True))
    return fmt.num(v, dec if dec is not None else fmt.decimals(None, v, derived=True), sign=opts.get("sign", False))


def uses_data(rc) -> bool:
    """True when a recipe reads a record cell (src must then be 'data')."""
    if not isinstance(rc, list) or not rc:
        return False
    if rc[0] in ("cell", "raw", "d"):
        return rc[0] != "d" or rc[1] not in ("count", "relday", "date", "age", "countdown", "quarter")
    if rc[0] == "tpl":
        return any(uses_data(v) for v in rc[2].values() if isinstance(v, list))
    if rc[0] == "join":
        return any(uses_data(p) for p in rc[2])
    if rc[0] in ("abs", "lower"):
        return uses_data(rc[1])
    if rc[0] == "part":
        return uses_data(rc[2])
    return False


def cells_used(rc, out: list | None = None) -> list:
    """[(field, row, opts)] for the L-PREC and L-UNIT checks."""
    out = [] if out is None else out
    if not isinstance(rc, list) or not rc:
        return out
    if rc[0] == "cell":
        out.append((rc[1], rc[2], rc[3]))
    elif rc[0] == "tpl":
        for v in rc[2].values():
            cells_used(v, out)
    elif rc[0] == "join":
        for p in rc[2]:
            cells_used(p, out)
    elif rc[0] == "abs":
        cells_used(rc[1], out)
    return out


def lex_entry(name: str, v):
    """Display-vocabulary lookup (assets/lexicon/<name>.json, owned by the data builder)."""
    from .fmt import lexicon
    if v is None:
        return None
    lx = lexicon(name)
    key = str(int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
    e = lx.get(key)
    return e if isinstance(e, dict) else ({"word": e} if isinstance(e, str) else None)
