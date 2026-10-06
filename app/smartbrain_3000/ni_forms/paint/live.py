"""Live bindings and time formatting (CONTRACTS.md 6.2, 7.1). Pure functions.

This is the Python mirror of the vector page's client JS (vector.LIVE_JS). Both
implement the same semantics so a raster snapshot at instant T equals what the
browser shows at T:

  now_marker    {prims:[line_id, dot_id?], box:{x0,x1,y0,y1}, t0, t1, path?: path_prim_id}
                moves the now line (and dot) to frac = (now-t0)/(t1-t0) of the box; the dot's
                y follows the path prim (linear interpolation of its fractional points).
                Outside [t0, t1] the marker prims are hidden.
  countdown     {prim, t, fmt}      text = countdown(t - now); fmt in COUNTDOWN_FMTS
  count_up      {prim, t0, unit}    text = elapsed since t0; unit "day"|"hour"|"minute"|"auto"
  timed_variants{slot, variants:[{t_from, t_to, prims}]}
                exactly one variant is visible: the one with t_from <= now < t_to
                (null bounds are open). `prims` are prim ids already in the CLIR; the prims
                of inactive variants are hidden. If no variant matches, the last one whose
                t_from <= now stays visible (never an empty slot).
  past_dim      {prims, t}          when now >= t the prims get alpha tokens.VIZ.past_alpha
  extrapolate   {prim, v0, rate_per_s, t0, fmt}  text = format(v0 + rate*(now-t0), fmt)
  age           {prim, t}           text = "{n} min old" | "{n} h old" | "{n} d old"

X anchors: ["l",px] | ["r",px] | ["c",px] | ["f",frac], plus the painter-internal
["m", a, b, frac] (a point frac of the way between anchors a and b) that apply_live
emits when a moved marker cannot be expressed as one simple anchor.
"""
from __future__ import annotations

import copy
import itertools
import re
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .. import tokens as TK

COUNTDOWN_FMTS = ("in_hm", "hm", "in_dhm", "rel")
_NUMFMT = re.compile(r"^,?\.\d[f%]$|^,?d$|^,$")


# --------------------------------------------------------------------------- anchors
def resolve_x(x, left: float, width: float) -> float:
    """Anchor -> absolute px. Mirrors forms.lint.resolve_x plus the internal 'm' kind."""
    k = x[0]
    if k == "l":
        return left + x[1]
    if k == "r":
        return left + width - x[1]
    if k == "c":
        return left + width / 2 + x[1]
    if k == "f":
        return left + x[1] * width
    if k == "m":
        a, b = resolve_x(x[1], left, width), resolve_x(x[2], left, width)
        return a + (b - a) * x[3]
    raise ValueError(f"unknown x anchor {x!r}")


def lerp_anchor(a: list, b: list, frac: float) -> list:
    """A single anchor for the point `frac` between anchors a and b (simple when possible)."""
    if a[0] == b[0] and a[0] in ("l", "c", "f"):
        return [a[0], a[1] + (b[1] - a[1]) * frac]
    if a[0] == b[0] == "r":
        return ["r", a[1] + (b[1] - a[1]) * frac]
    if a[0] == "l" and b[0] == "r" and a[1] == 0 and b[1] == 0:
        return ["f", frac]
    return ["m", a, b, frac]


# --------------------------------------------------------------------------- time
def parse_t(t: str) -> datetime | date:
    """ISO8601Z -> aware UTC datetime; 'YYYY-MM-DD' -> date (a floating calendar day)."""
    if len(t) == 10:
        return date.fromisoformat(t)
    return datetime.fromisoformat(t).astimezone(UTC)


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _ampm(dt: datetime) -> str:
    return "am" if dt.hour < 12 else "pm"


def _abbr(dt: datetime) -> str:
    n = dt.tzname() or ""
    if n and n[0] in "+-":
        sign, hh, mm = n[0], n[1:3], n[3:5] if len(n) >= 5 else "00"
        return f"UTC{sign}{int(hh)}" + (f":{mm}" if mm != "00" else "")
    return n


def format_time(t_iso: str, fmt: str, zone: str, show_zone: bool) -> str:
    """Format an absolute time in `zone` (IANA). Date-only values are floating days
    (no zone shift). Output strings are the approved look: '7:10 pm', '12a', 'Thu'."""
    v = parse_t(t_iso)
    if isinstance(v, datetime):
        dt = v.astimezone(_zone(zone))
    else:
        dt = datetime(v.year, v.month, v.day)
        show_zone = False
    h12 = dt.hour % 12 or 12
    if fmt == "h:mm a":
        s = f"{h12}:{dt.minute:02d} {_ampm(dt)}"
    elif fmt == "h a":
        s = f"{h12} {_ampm(dt)}"
    elif fmt == "ha_short":
        s = f"{h12}{_ampm(dt)[0]}"
    elif fmt == "EEE":
        s = dt.strftime("%a")
    elif fmt == "EEE d":
        s = f"{dt.strftime('%a')} {dt.day}"
    elif fmt == "MMM d":
        s = f"{dt.strftime('%b')} {dt.day}"
    elif fmt == "EEE MMM d":
        s = f"{dt.strftime('%a')} {dt.strftime('%b')} {dt.day}"
    elif fmt == "yyyy":
        s = f"{dt.year}"
    elif fmt == "MMM yyyy":
        s = f"{dt.strftime('%b')} {dt.year}"
    elif fmt == "MMM d, yyyy":
        s = f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    elif fmt == "MMM d, h:mm a":
        s = f"{dt.strftime('%b')} {dt.day}, {h12}:{dt.minute:02d} {_ampm(dt)}"
    elif fmt == "HH:mm":
        s = f"{dt.hour:02d}:{dt.minute:02d}"
    else:
        raise ValueError(f"unknown time fmt {fmt!r}")
    if show_zone and isinstance(v, datetime):
        s += " " + _abbr(dt)
    return s


def time_prim_text(p: dict, viewer_tz: str) -> str:
    zone = p["zone"] if p.get("tz", "card") == "card" else viewer_tz
    return format_time(p["t"], p["fmt"], zone, bool(p.get("show_zone")) and zone != viewer_tz)


def countdown_text(seconds: float, fmt: str = "in_hm") -> str:
    """Remaining time as words. Past (<= 0) reads 'now'."""
    s = int(seconds)
    if s <= 0:
        return "now"
    m_total = (s + 59) // 60
    d, rem = divmod(m_total, 1440)
    h, m = divmod(rem, 60)
    if fmt == "rel" or (fmt == "in_dhm" and d >= 2):
        if d >= 2:
            return f"in {d} days"
        if d == 1:
            return "in 1 day" if h == 0 else f"in 1 d {h} h"
    h += d * 24
    core = f"{h} h {m} m" if h and m else f"{h} h" if h else f"{m} min"
    return core if fmt == "hm" else f"in {core}"


def count_up_text(seconds: float, unit: str = "auto") -> str:
    s = max(0, int(seconds))
    if unit == "auto":
        unit = "day" if s >= 2 * 86400 else "hour" if s >= 2 * 3600 else "minute"
    n = s // {"day": 86400, "hour": 3600, "minute": 60}[unit]
    word = {"day": "day", "hour": "hour", "minute": "minute"}[unit]
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def age_text(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)} min old"
    if s < 2 * 86400:
        return f"{s // 3600} h old"
    return f"{s // 86400} d old"


def number_text(v: float, fmt: str) -> str:
    if not _NUMFMT.match(fmt or ""):
        fmt = ",.0f"
    if fmt.endswith("d"):
        return format(round(v), fmt)
    return format(v, fmt)


# --------------------------------------------------------------------------- apply
def _secs(a: datetime, b) -> float:
    if isinstance(b, date) and not isinstance(b, datetime):
        b = datetime(b.year, b.month, b.day, tzinfo=UTC)
    return (b - a).total_seconds()


def _path_y(path: dict, fx: float) -> float | None:
    pts = path["pts"]
    if not pts:
        return None
    if fx <= pts[0][0]:
        fy = pts[0][1]
    elif fx >= pts[-1][0]:
        fy = pts[-1][1]
    else:
        fy = pts[-1][1]
        for (x0, y0), (x1, y1) in itertools.pairwise(pts):
            if x0 <= fx <= x1:
                fy = y0 if x1 == x0 else y0 + (y1 - y0) * (fx - x0) / (x1 - x0)
                break
    b = path["box"]
    return b["y0"] + fy * (b["y1"] - b["y0"])


def active_variant(variants: list, now: datetime) -> int:
    best = None
    for i, v in enumerate(variants):
        f = parse_t(v["t_from"]) if v.get("t_from") else None
        t = parse_t(v["t_to"]) if v.get("t_to") else None
        if (f is None or _secs(now, f) <= 0) and (t is None or _secs(now, t) > 0):
            return i
        if f is None or _secs(now, f) <= 0:
            best = i
    return best if best is not None else 0


def apply_live(clir: dict, now: datetime, viewer_tz: str) -> dict:
    """Resolve every live binding at `now` -> a static CLIR (live=[]). Hidden prims are
    removed; dimmed prims get 'alpha'. Never mutates the input."""
    out = copy.deepcopy(clir)
    by = {p["id"]: p for p in out["prims"]}
    hidden: set = set()
    for lb in out.get("live", []):
        k, a = lb["k"], lb["args"]
        if k == "now_marker":
            t0, t1 = parse_t(a["t0"]), parse_t(a["t1"])
            span = _secs(t0, t1)
            frac = _secs(t0, now) / span if span > 0 else -1
            ids = [i for i in a.get("prims", []) if i in by]
            if not (0 <= frac <= 1):
                hidden.update(ids)
                continue
            box = a["box"]
            x = lerp_anchor(box["x0"], box["x1"], frac)
            py = None
            if a.get("path") in by:
                py = _path_y(by[a["path"]], frac)
            for i in ids:
                p = by[i]
                if p["k"] == "line":
                    p["x0"], p["x1"] = x, x
                    if a.get("gaps"):
                        p["gaps"] = list(a["gaps"])
                elif p["k"] in ("dot", "tri", "icon", "text", "time"):
                    p["x"] = x
                    if py is not None and p["k"] == "dot":
                        p["y"] = py
        elif k == "countdown":
            p = by.get(a["prim"])
            if p is not None:
                p["lines"] = [countdown_text(_secs(now, parse_t(a["t"])), a.get("fmt", "in_hm"))]
        elif k == "count_up":
            p = by.get(a["prim"])
            if p is not None:
                p["lines"] = [count_up_text(-_secs(now, parse_t(a["t0"])), a.get("unit", "auto"))]
        elif k == "timed_variants":
            vs = a.get("variants", [])
            if vs:
                act = active_variant(vs, now)
                for i, v in enumerate(vs):
                    if i != act:
                        hidden.update(pid for pid in v.get("prims", []) if isinstance(pid, int))
        elif k == "past_dim":
            if _secs(now, parse_t(a["t"])) <= 0:
                for i in a.get("prims", []):
                    if i in by:
                        by[i]["alpha"] = TK.VIZ["past_alpha"]
        elif k == "extrapolate":
            p = by.get(a["prim"])
            if p is not None:
                v = a["v0"] + a["rate_per_s"] * -_secs(now, parse_t(a["t0"]))
                p["lines"] = [number_text(v, a.get("fmt", ",.0f"))]
        elif k == "age":
            p = by.get(a["prim"])
            if p is not None:
                p["lines"] = [age_text(-_secs(now, parse_t(a["t"])))]
    out["prims"] = [p for p in out["prims"] if p["id"] not in hidden]
    out["reading_order"] = [i for i in out.get("reading_order", []) if i not in hidden]
    out["live"] = []
    return out


def live_boundaries(clir: dict, now: datetime) -> list[datetime]:
    """Future instants at which a live binding changes what is SHOWN discretely
    (variant switches, past-dim flips, marker entering/leaving its window)."""
    out = []
    for lb in clir.get("live", []):
        a = lb["args"]
        ts = []
        if lb["k"] == "timed_variants":
            for v in a.get("variants", []):
                ts += [v.get("t_from"), v.get("t_to")]
        elif lb["k"] == "past_dim":
            ts.append(a.get("t"))
        elif lb["k"] == "now_marker":
            ts += [a.get("t0"), a.get("t1")]
        for t in ts:
            if t:
                v = parse_t(t)
                if isinstance(v, date) and not isinstance(v, datetime):
                    v = datetime(v.year, v.month, v.day, tzinfo=UTC)
                if v > now:
                    out.append(v)
    return sorted(set(out))


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_midnight(now: datetime, zone: str) -> datetime:
    z = _zone(zone)
    loc = now.astimezone(z)
    nxt = datetime(loc.year, loc.month, loc.day, tzinfo=z) + timedelta(days=1)
    # re-normalise across DST: wall-clock midnight of the next calendar day
    nxt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=z)
    return nxt.astimezone(UTC)


def dst_transitions(now: datetime, zone: str, hours: int = 48) -> list[datetime]:
    """UTC instants within `hours` where the zone's UTC offset changes (15-min scan + bisection)."""
    z = _zone(zone)
    out = []
    step = timedelta(minutes=15)
    t = now
    prev = t.astimezone(z).utcoffset()
    end = now + timedelta(hours=hours)
    while t < end:
        t2 = t + step
        off = t2.astimezone(z).utcoffset()
        if off != prev:
            lo, hi = t, t2
            while (hi - lo) > timedelta(seconds=1):
                mid = lo + (hi - lo) / 2
                if mid.astimezone(z).utcoffset() == prev:
                    lo = mid
                else:
                    hi = mid
            out.append(hi.replace(microsecond=0))
            prev = off
        t = t2
    return out


def line_segments(p: dict, prims_by_id: dict, W: float, pad: float = 3.0) -> list[tuple[float, float]]:
    """The visible y-segments of a vertical line whose `gaps` name labels it must not cross: the line
    breaks around each listed label box (painters share this; the client JS mirrors it)."""
    from ..lint import text_box
    x = resolve_x(p["x0"], 0, W)
    ya, yb = sorted((p["y0"], p["y1"]))
    cuts = []
    for i in p.get("gaps") or []:
        q = prims_by_id.get(i)
        if q is None or q.get("k") not in ("text", "time"):
            continue
        x0, y0, x1, y1 = text_box(q, W)[:4]
        if x0 - pad <= x <= x1 + pad:
            cuts.append((y0 - 2, y1 + 2))
    segs = [(ya, yb)]
    for c0, c1 in sorted(cuts):
        nxt = []
        for s0, s1 in segs:
            if c1 <= s0 or c0 >= s1:
                nxt.append((s0, s1))
                continue
            if c0 > s0:
                nxt.append((s0, c0))
            if c1 < s1:
                nxt.append((c1, s1))
        segs = nxt
    return [(a, b) for a, b in segs if b - a > 1]
