"""Four constraint CLASSES the verify step checks a built result against (plan S2, shadow mode):
time window, numeric threshold, compare count, source scope. Each is a pure function over the
question's constraints (``Ask``), the built result (``Result``) and the source (``SourceInfo``) and
returns the reasons the result does not answer the ask ([] = no objection, or nothing to judge).

They are phrased in classes, never per question: a check stays silent when the result carries nothing
it can judge (no times, no matching column, no rows), so a right card is never refused for lack of data.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, tzinfo

from .constraints import Ask, parse_ask
from .rows import Result, SourceInfo, Span

_NIGHT_START_H = 17
_NIGHT_END_H = 6
_WEEKEND_START = (4, 18)  # Friday 18:00
_WINDOW_WORDS = {"today": "today", "tonight": "tonight", "tomorrow": "tomorrow", "yesterday": "yesterday",
                 "last_night": "last night", "weekend": "this weekend"}


def _at(day: object, hour: int, zone: tzinfo) -> datetime:
    """Local wall clock ``hour``:00 on a date, in ``zone``."""
    assert isinstance(hour, int) and 0 <= hour <= 24, "hour in range"
    base = datetime.combine(day, time.min, zone)  # type: ignore[arg-type]
    return base + timedelta(hours=hour)


def window_span(window: str, now: datetime, zone: tzinfo) -> Span:
    """[start, end) of a named window on the user's clock in ``zone`` (``now`` may be in any zone).
    "tonight" runs 17:00 to 06:00, and before 06:00 it is still the night that began the evening before —
    so after midnight "tonight" and "today" name different spans; "this weekend" runs Friday 18:00 to
    Monday 00:00 of the week ``now`` falls in (a Sunday asks about the weekend just begun)."""
    assert window in _WINDOW_WORDS, f"unknown window {window}"
    assert now.tzinfo is not None, "now must be aware"
    local = now.astimezone(zone)
    day = local.date()
    one = timedelta(days=1)
    if window == "today":
        return _at(day, 0, zone), _at(day + one, 0, zone)
    if window == "tomorrow":
        return _at(day + one, 0, zone), _at(day + 2 * one, 0, zone)
    if window == "yesterday":
        return _at(day - one, 0, zone), _at(day, 0, zone)
    if window == "tonight":
        base = day - one if local.hour < _NIGHT_END_H else day
        return _at(base, _NIGHT_START_H, zone), _at(base + one, _NIGHT_END_H, zone)
    if window == "last_night":
        return _at(day - one, _NIGHT_START_H, zone), _at(day, _NIGHT_END_H, zone)
    friday = day + timedelta(days=_WEEKEND_START[0] - local.weekday())  # a Sat / Sun lands on the Friday before
    return _at(friday, _WEEKEND_START[1], zone), _at(friday + 3 * one, 0, zone)


def check_window(ask: Ask, result: Result, now: datetime, zone: tzinfo) -> list[str]:
    """Class 1, time window: the asked window ("tonight", "today", "this weekend") in the user's zone must
    hold at least one moment the result shows. A result with no readable moment is not judged."""
    assert isinstance(ask, Ask) and isinstance(result, Result), "typed inputs"
    if ask.window is None or not result.spans:
        return []
    start, end = window_span(ask.window, now, zone)
    if any(s < end and e >= start for s, e in result.spans):
        return []
    shown = min(result.spans, key=lambda p: p[0])[0].astimezone(zone)
    return [f"its times are outside {_WINDOW_WORDS[ask.window]} (first {shown:%a %b} {shown.day} {shown:%H:%M})"]


_PASSES = {">": lambda x, v: x > v, ">=": lambda x, v: x >= v, "<": lambda x, v: x < v, "<=": lambda x, v: x <= v}
_OP_WORDS = {">": "above", ">=": "at least", "<": "below", "<=": "at most"}
# the column names a measure noun reads from (closed vocabulary, matched as a prefix of the column key)
_MEASURE_COLUMNS: dict[str, tuple[str, ...]] = {
    "earthquake": ("mag", "magnitude"), "earthquakes": ("mag", "magnitude"), "quake": ("mag", "magnitude"),
    "quakes": ("mag", "magnitude"), "tremor": ("mag", "magnitude"), "tremors": ("mag", "magnitude"),
    "seismic": ("mag", "magnitude"), "temperature": ("temperature", "temp"), "temp": ("temperature", "temp"),
    "temps": ("temperature", "temp"), "wind": ("wind", "gust"), "winds": ("wind", "gust"),
    "gusts": ("wind", "gust"), "rain": ("precip",), "precipitation": ("precip",), "snow": ("snow",),
    "humidity": ("humidity", "relativehumidity"), "aqi": ("aqi",), "price": ("price", "current_price", "close"),
    "prices": ("price", "current_price", "close"), "uv": ("uv",),
}
_FLOOR_RES = (re.compile(r"(?<![\w.])m?(\d+(?:\.\d+)?)\s*\+"),
              re.compile(r"(\d+(?:\.\d+)?)_(?:hour|day|week|month)\b"))


def _bound_columns(ask: Ask, result: Result) -> dict[str, tuple[float, ...]]:
    """The numeric columns the threshold is about: those named by a word of the ask or by the columns its
    measure nouns read from ("earthquakes" -> mag / magnitude). Empty when no column can be told."""
    assert isinstance(ask, Ask) and isinstance(result, Result), "typed inputs"
    stems = {w for w in ask.words if len(w) >= 4}
    for word in ask.words:
        stems.update(_MEASURE_COLUMNS.get(word, ()))
    out = {key: vals for key, vals in result.fields.items()
           if key in ask.words or any(key.startswith(w) for w in stems)}
    assert len(out) <= len(result.fields), "a subset of the result's columns"
    return out


def source_floor(source: SourceInfo) -> float | None:
    """The lowest value a feed carries when its own name or address says so ("M2.5+ earthquakes",
    "2.5_day.geojson"), else None."""
    assert isinstance(source, SourceInfo), "typed source"
    for text in (source.name, source.url):
        for rx in _FLOOR_RES:
            m = rx.search(text.lower())
            if m is not None:
                return float(m.group(1))
    return None


def check_threshold(ask: Ask, result: Result, source: SourceInfo) -> list[str]:
    """Class 2, numeric threshold ("above 4.0", "at least 5"): every value of the column the bound is about
    must pass it; with no such column in the result, a feed whose own floor sits below an "above" bound
    cannot have applied it."""
    assert isinstance(ask, Ask) and isinstance(result, Result), "typed inputs"
    if ask.threshold is None:
        return []
    op, bound = ask.threshold
    cols = _bound_columns(ask, result)
    reasons = []
    for key, vals in cols.items():
        bad = [x for x in vals if not _PASSES[op](x, bound)]
        if bad:
            reasons.append(f"{len(bad)} of {len(vals)} {key} values are not {_OP_WORDS[op]} {bound:g}")
    floor = None if cols else source_floor(source)
    if floor is not None and op in (">", ">=") and floor < bound:
        reasons.append(f"its feed starts at {floor:g}, below {bound:g}")
    return reasons[:3]


def _names(text: str, entity: str) -> bool:
    """Is the entity named, as whole words, in the lower-cased text?"""
    assert isinstance(text, str) and isinstance(entity, str), "strings required"
    return re.search(rf"(?<!\w){re.escape(entity)}(?!\w)", text) is not None


def check_compare(ask: Ask, result: Result) -> list[str]:
    """Class 3, compare count: an ask naming N things needs N of them named in the result. A result that
    shows no words at all is not judged."""
    assert isinstance(ask, Ask) and isinstance(result, Result), "typed inputs"
    if len(ask.entities) < 2 or not result.text.strip():
        return []
    missing = [e for e in ask.entities if not _names(result.text, e)]
    if not missing:
        return []
    have = len(ask.entities) - len(missing)
    return [f"it names {have} of {len(ask.entities)} compared ({', '.join(missing)} missing)"]


_POINT_PARAMS = frozenset({"lat", "lon", "latitude", "longitude", "zip", "place", "city", "station", "point",
                           "office", "grid_x", "grid_y", "gauge", "buoy", "site", "county", "state"})
_POINT_URL_RE = re.compile(r"[?&](?:point|lat|latitude|station|zip)=", re.IGNORECASE)
_AREA_WORDS = {"basin": "a basin", "national": "the nation", "world": "the world", "region": "a region"}


def check_scope(ask: Ask, source: SourceInfo) -> list[str]:
    """Class 4, source scope: a count over a basin, region, the nation or the world cannot come from a
    source that answers for one point (offered for a place, filled from a coordinate, station or zip)."""
    assert isinstance(ask, Ask) and isinstance(source, SourceInfo), "typed inputs"
    if not (ask.counting and ask.area):
        return []
    point = source.scope == "place" or bool(source.params & _POINT_PARAMS) \
        or _POINT_URL_RE.search(source.url) is not None
    if not point:
        return []
    return [f"it is a single-place source, not a count over {_AREA_WORDS[ask.area]}"]


def shadow_reasons(request: str, result: Result, source: SourceInfo, now: datetime,
                   zone: tzinfo) -> dict[str, list[str]]:
    """Reasons per class, only the classes with any: {"window": [...], "threshold": [...], "compare": [...],
    "scope": [...]}. {} = no class objects."""
    assert isinstance(request, str), "request must be a string"
    assert now.tzinfo is not None, "now must be aware"
    ask = parse_ask(request)
    found = {"window": check_window(ask, result, now, zone), "threshold": check_threshold(ask, result, source),
             "compare": check_compare(ask, result), "scope": check_scope(ask, source)}
    return {name: reasons for name, reasons in found.items() if reasons}
