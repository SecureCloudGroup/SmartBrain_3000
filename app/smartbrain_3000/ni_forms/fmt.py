"""Every displayed number, unit, time, compass point, relative day and cadence is
formatted here (CONTRACTS.md 5.1). Pure functions of (value, Field, card zone).

Rules:
  * a published value keeps its published decimals (Field.precision) - never fewer;
  * magnitude rules apply only to derived values;
  * units come from Field.unit through the unit lexicon (display + join); no unit
    literal is chosen by a form;
  * negative numbers use the typographic minus; direction is ALSO drawn (tri).
"""
from __future__ import annotations

import itertools
import json
import math
import re
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

# ASSETS points at the package's own assets dir (proto anchored to parents[2]).
ASSETS = Path(__file__).with_name("assets")
MINUS = "−"

# Generic display fallback, used only when assets/lexicon/units.json lacks an id.
# (display, join) - join "tight" renders 70°F as one run, "space" renders 14 mph.
_UNIT_FALLBACK = {
    "degF": ("°F", "tight"), "degC": ("°C", "tight"), "deg": ("°", "tight"), "pct": ("%", "tight"),
    "percent": ("%", "tight"), "ft": ("ft", "space"), "m": ("m", "space"), "km": ("km", "space"),
    "mi": ("mi", "space"), "cm": ("cm", "space"), "mm": ("mm", "space"), "in": ("in", "space"),
    "mph": ("mph", "space"), "kmh": ("km/h", "space"), "kn": ("kt", "space"), "kt": ("kt", "space"),
    "ms": ("m/s", "space"), "hPa": ("hPa", "space"), "mb": ("mb", "space"), "inHg": ("inHg", "space"),
    "kg": ("kg", "space"), "lb": ("lb", "space"), "g": ("g", "space"), "kcal": ("kcal", "space"),
    "kW": ("kW", "space"), "kWh": ("kWh", "space"), "MW": ("MW", "space"), "W": ("W", "space"),
    "s": ("s", "space"), "min": ("min", "space"), "h": ("h", "space"), "d": ("days", "space"),
    "ugm3": ("µg/m³", "space"), "ppm": ("ppm", "space"), "ft3_s": ("ft³/s", "space"),
    # Dated 2026-10-06: non-Latin-1 subscript 2 was outside the bundled Inter subset; ASCII keeps it visible.
    "gCO2_kWh": ("gCO2/kWh", "space"), "au": ("au", "space"), "count": ("", "space"),
}
# Dated 2026-10-06: ₿ was outside the bundled Inter subset; "BTC " renders cleanly in its place.
_CURRENCY = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "INR": "₹", "KRW": "₩",
             "AUD": "A$", "CAD": "C$", "BTC": "BTC "}


@lru_cache(maxsize=1)
def _units() -> dict:
    p = ASSETS / "lexicon" / "units.json"
    try:
        return {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")}
    except Exception:
        return {}


@lru_cache(maxsize=8)
def lexicon(name: str) -> dict:
    p = ASSETS / "lexicon" / f"{name}.json"
    try:
        d = json.loads(p.read_text())
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        if set(d) == {"codes"} or "codes" in d and isinstance(d["codes"], dict):
            d = d["codes"]
        return d
    except Exception:
        return {}


def unit_display(unit: str | None) -> tuple[str, str]:
    """(display, join) for a UnitId; ('', 'space') for None."""
    if not unit:
        return "", "space"
    u = _units().get(unit)
    if isinstance(u, dict) and "display" in u:
        return u["display"], u.get("join", "space")
    return _UNIT_FALLBACK.get(unit, (unit, "space"))


# ----------------------------------------------------------------------------- numbers
def _published_decimals(v) -> int:
    if isinstance(v, (bool, int)):
        return 0
    if isinstance(v, float):
        r = repr(v)
        if "e" in r or "E" in r:
            return 2
        return min(len(r.split(".")[1]), 6) if "." in r else 0
    return 0


def decimals(field, v, *, derived: bool = False) -> int:
    """Decimals to show. Published: Field.precision (or the value's own repr). Derived:
    magnitude rules (the only place they apply)."""
    if not derived and field is not None and getattr(field, "unrounded", False):
        derived = True        # an unrounded float column: the source stated no precision (G3 rule)
    if not derived:
        if field is not None and field.precision is not None:
            p = field.precision
            if field.type == "percent" and field.scale == "0..1":
                p = max(p - 2, 0)
            return max(p, 0)
        d = _published_decimals(v)
        if field is not None and field.type == "percent" and field.scale == "0..1":
            d = max(d - 2, 0)
        return d
    a = abs(float(v))
    if field is not None and (field.type == "currency" or field.currency):
        return 0 if a >= 1000 else 2
    if a >= 1000:
        return 0
    if a >= 100:
        return 1
    if a >= 1:
        return 2
    return 3 if a >= 0.01 else 4


def column_decimals(field, values: list) -> int | None:
    """The precision a column publishes: ``Field.precision`` when the source stated it, else the
    most decimals its values are written with (None for an empty / unrounded column)."""
    assert isinstance(values, list), "values must be a list"
    assert field is None or hasattr(field, "precision"), "field must be a Field or None"
    if field is not None and field.precision is not None and not getattr(field, "unrounded", False):
        return field.precision
    if field is not None and getattr(field, "unrounded", False):
        return None
    obs = [_published_decimals(x) for x in values if is_num(x)]
    return max(obs) if obs else None


def stat_decimals(field, values: list, v: float) -> int:
    """Decimals for a statistic DERIVED over a column (an average): the magnitude rule, clamped to
    the column's own precision .. precision + 1 — never "0.0000 in" over a one-decimal column, never
    fewer decimals than the column shows (fix round 1a-5, class G)."""
    assert isinstance(values, list), "values must be a list"
    assert is_num(v), "v must be a finite number"
    mag = decimals(field, v, derived=True)
    pub = column_decimals(field, values)
    if pub is None:
        return mag
    return pub if mag <= pub else min(mag, pub + 1)


def num(v: float, dec: int, *, sign: bool = False, grouping: bool = True) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return ""
    s = f"{abs(v):,.{dec}f}" if grouping else f"{abs(v):.{dec}f}"
    neg = v < 0 and float(s.replace(",", "") or 0) != 0
    if neg:
        return MINUS + s
    return ("+" + s) if sign and v > 0 else s


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


_COMPACT = ((1e12, "T"), (1e9, "B"), (1e6, "M"))


def compact_num(x: float, sign: bool = False) -> str | None:
    """1692113715423 -> '1.69T' (3 significant digits); None below a million."""
    a = abs(x)
    for base, suf in _COMPACT:
        if a >= base:
            m = a / base
            d = 2 if m < 10 else 1 if m < 100 else 0
            s = f"{m:.{d}f}{suf}"
            if x < 0:
                return MINUS + s
            return ("+" + s) if sign and x > 0 else s
    return None


def value_text(v, field, *, derived: bool = False, sign: bool = False, dec: int | None = None,
               unit: bool = True, compact: bool = False) -> str:
    """One cell as display text: currency, percent scale, unit join, bool, text.
    compact=True shows values of a million or more as 1.69T / 38.1B / 2.4M (secondary facts)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if not is_num(v):
        if field is not None and field.type == "category" and field.role not in ("name", "link", "link_secondary"):
            return machine_words(str(v))
        return str(v)
    t = field.type if field is not None else "number"
    if t == "identifier":
        return str(v)
    d = decimals(field, v, derived=derived) if dec is None else dec
    x = float(v)
    if compact and t != "percent" and compact_num(x) is not None:
        s = compact_num(x, sign)
        cur = field.currency if field is not None else None
        sym = _CURRENCY.get(cur or "", None)
        if sym:
            return (s[0] + sym + s[1:]) if s[:1] in (MINUS, "+") else sym + s
        if cur:
            return f"{s} {cur}"
        if unit and field is not None and field.unit:
            disp, join = unit_display(field.unit)
            if disp:
                return s + disp if join == "tight" else f"{s} {disp}"
        return s
    if t == "percent":
        if field.scale == "0..1":
            x *= 100
        return num(x, d, sign=sign) + "%"
    s = num(x, d, sign=sign)
    cur = field.currency if field is not None else None
    if t == "currency" or cur:
        sym = _CURRENCY.get(cur or "", None)
        per = ""
        if unit and field is not None and field.unit and str(field.unit).startswith("per_"):
            per = unit_display(field.unit)[0].replace(" ", "")    # a price per unit: '€12.50/MWh' (round 3)
        if sym:
            if s.startswith((MINUS, "+")):
                return s[0] + sym + s[1:] + per
            return sym + s + per
        return (f"{s} {cur}" if cur else s) + per
    if unit and field is not None and field.unit:
        disp, join = unit_display(field.unit)
        if disp:
            return s + disp if join == "tight" else f"{s} {disp}"
    return s


_MACHINE = re.compile(r"^(?:[A-Z][A-Z]+|[a-z][a-z]+)(?:[_-](?:[A-Z][A-Z]+|[a-z][a-z]+))+$")


def machine_words(s: str) -> str:
    """A machine enumeration written as one snake_case / kebab-case / UPPER_SNAKE token of whole words
    ('SOME_STATE', 'other-kind') reads as words ('Some state', 'Other kind'); anything else is
    shown as the source wrote it (round 3: a spelling rule, no vocabulary)."""
    t = s.strip()
    if not _MACHINE.match(t):
        return s
    parts = re.split(r"[_-]", t)
    if not all(re.search(r"[aeiouyAEIOUY]", p) and len(p) >= 3 for p in parts):
        return s
    w = " ".join(p.lower() for p in parts)
    return w[:1].upper() + w[1:]


def unit_suffix(field) -> str:
    if field is None or not field.unit:
        return ""
    disp, join = unit_display(field.unit)
    return disp if join == "tight" else (" " + disp if disp else "")


def delta_text(d: float, dec: int, field=None, *, pct: bool = False) -> str:
    """Signed delta. Direction is also drawn with a tri by the form."""
    if pct:
        return num(d, dec, sign=True) + "%"
    return value_text(d, field, dec=dec, sign=True, unit=False) if field is not None else num(d, dec, sign=True)


# ----------------------------------------------------------------------------- time
def parse_t(t) -> datetime | date | None:
    """ISO8601Z -> aware UTC datetime; 'YYYY-MM-DD' -> date. None on junk."""
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.astimezone(UTC)
    if isinstance(t, date):
        return t
    s = str(t)
    try:
        if len(s) == 10:
            return date.fromisoformat(s)
        return datetime.fromisoformat(s).astimezone(UTC)
    except ValueError:
        return None


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=64)
def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(zone(tz))


def local_date(t, tz: str) -> date | None:
    v = parse_t(t)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(zone(tz)).date()
    return v


def day_start(d: date, tz: str) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=zone(tz)).astimezone(UTC)


def coarse_fmt(gap_s: float | None, fine: str) -> str:
    """A date label as coarse as the data (round 1): monthly points read 'Aug 2026', yearly '2026' - a
    first-of-month date is never shown as a weekday or a day of the month."""
    if gap_s is None:
        return fine
    if gap_s >= 360 * 86400:
        return "yyyy"
    if gap_s >= 27 * 86400:
        return "MMM yyyy"
    return fine


def median_gap(times: list) -> float | None:
    """Median spacing (seconds) of epoch seconds or ISO strings, or None."""
    ts = []
    for t in times:
        if t is None:
            continue
        if isinstance(t, (int, float)):
            ts.append(float(t))
            continue
        v = parse_t(t)
        ts.append((v if isinstance(v, datetime) else datetime(v.year, v.month, v.day, tzinfo=UTC)).timestamp())
    ts = sorted(set(ts))
    d = sorted(b - a for a, b in itertools.pairwise(ts))
    return d[len(d) // 2] if d else None


def format_time(t_iso: str, fmt: str, tz: str, show_zone: bool = False) -> str:
    """The painter's own formatter (ni_forms.paint.live.format_time) so the layout
    measures exactly what is painted. Local fallback mirrors it."""
    try:
        from .paint.live import format_time as _ft
        return _ft(t_iso, fmt, tz, show_zone)
    except ImportError:   # pragma: no cover - painter missing
        v = parse_t(t_iso)
        dt = v.astimezone(zone(tz)) if isinstance(v, datetime) else datetime(v.year, v.month, v.day)
        h12 = dt.hour % 12 or 12
        ap = "am" if dt.hour < 12 else "pm"
        return {"h:mm a": f"{h12}:{dt.minute:02d} {ap}", "h a": f"{h12} {ap}", "ha_short": f"{h12}{ap[0]}",
                "EEE": dt.strftime("%a"), "EEE d": f"{dt.strftime('%a')} {dt.day}",
                "MMM d": f"{dt.strftime('%b')} {dt.day}", "EEE MMM d": dt.strftime("%a %b ") + str(dt.day),
                "yyyy": str(dt.year), "MMM yyyy": f"{dt.strftime('%b')} {dt.year}",
                "MMM d, yyyy": f"{dt.strftime('%b')} {dt.day}, {dt.year}", "MMM d, h:mm a": f"{dt.strftime('%b')} {dt.day}, {h12}:{dt.minute:02d} {ap}",
                "HH:mm": f"{dt.hour:02d}:{dt.minute:02d}"}[fmt]


# The widest value each format can produce (width reserved for viewer-zone prims).
WIDEST = {"h:mm a": "12:59 pm", "h a": "12 pm", "ha_short": "12p", "EEE": "Wed", "EEE d": "Wed 28",
          "MMM d": "May 28", "EEE MMM d": "Wed May 28", "yyyy": "2028", "MMM yyyy": "May 2028", "MMM d, yyyy": "May 28, 2028",
          "MMM d, h:mm a": "May 28, 12:59 pm", "HH:mm": "20:58"}
ZONE_WIDEST = " AKDT"


def countdown_text(seconds: float) -> str:
    try:
        from .paint.live import countdown_text as _cd
        return _cd(seconds, "in_hm")
    except ImportError:   # pragma: no cover
        m = max(0, int(seconds + 59) // 60)
        h, m = divmod(m, 60)
        return "now" if seconds <= 0 else (f"in {h} h {m} m" if h else f"in {m} min")


COUNTDOWN_WIDEST = "in 23 h 59 m"


def age_text(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)} min old"
    if s < 2 * 86400:
        return f"{s // 3600} h old"
    return f"{s // 86400} d old"


AGE_WIDEST = "59 min old"


def cadence(s: int) -> str:
    if not s:
        return ""
    if s % 86400 == 0:
        return "daily" if s == 86400 else f"every {s // 86400} days"
    if s % 3600 == 0:
        return "every hour" if s == 3600 else f"every {s // 3600} h"
    if s >= 60:
        m = round(s / 60)
        return f"every {m} min"
    return f"every {s} s"


def rel_day(d: date, today: date) -> str:
    n = (d - today).days
    if n == 0:
        return "Today"
    if n == 1:
        return "Tomorrow"
    if n == -1:
        return "Yesterday"
    return f"+{n} days" if n > 0 else f"{-n} days ago"


REL_DAY_WIDEST = "Yesterday"

_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg: float) -> str:
    return _COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


_SLD2 = {"co", "com", "org", "net", "gov", "ac", "edu", "ne", "or", "go"}


def host_short(host: str) -> str:
    """Registrable domain (heuristic public-suffix: 2 labels, 3 for *.co.uk-like)."""
    h = (host or "").lower().split(":")[0]
    h = h.removeprefix("www.")
    parts = h.split(".")
    if len(parts) <= 2:
        return h
    if len(parts[-1]) == 2 and parts[-2] in _SLD2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def host_display(host: str) -> str:
    h = (host or "").lower().split(":")[0]
    return h.removeprefix("www.")


_WS = re.compile(r"\s+")


def one_line(s: str) -> str:
    return _WS.sub(" ", str(s)).strip()
