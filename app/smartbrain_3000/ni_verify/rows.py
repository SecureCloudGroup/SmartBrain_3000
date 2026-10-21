"""A built result as the four verify classes read it: the times it shows, its numeric columns, its words and
the source it came from (pure: nothing here fetches, resolves a place or asks a model)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo

Span = tuple[datetime, datetime]

_MAX_NODES = 2000
_MAX_LIST = 400
_MAX_KEYS = 80
_MAX_VALUES = 500
_ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_CLOCK_RE = re.compile(r"\d{2}:\d{2}(?::\d{2})?")
_TIME_KEY_RE = re.compile(r"time|date|start|end|stamp|updated|_at$|^at$|^ts$")
_EPOCH_SECONDS = (946_684_800, 4_102_444_800)  # 2000-01-01 .. 2100-01-01


@dataclass(frozen=True)
class Result:
    """What a build shows: the moments (as spans, a day-only value spans its day), the numeric columns by
    lower-cased key, and every word of the card and its source as one lower-cased blob."""

    spans: tuple[Span, ...] = ()
    fields: dict[str, tuple[float, ...]] = field(default_factory=dict)
    text: str = ""


@dataclass(frozen=True)
class SourceInfo:
    """The facts about the picked source the scope and threshold classes need."""

    name: str = ""
    url: str = ""
    scope: str | None = None          # "place" for a source offered for one place
    params: frozenset[str] = frozenset()


def iso_span(value: str, zone: tzinfo) -> Span | None:
    """A date or timestamp string as a span: a day spans its own day in ``zone``, a timestamp without an
    offset is read in ``zone``. None for anything else."""
    assert isinstance(value, str), "value must be a string"
    assert zone is not None, "a zone is required"
    text = value.strip()
    try:
        if _ISO_DAY_RE.fullmatch(text):
            start = datetime.combine(date.fromisoformat(text), time.min, zone)
            return start, start + timedelta(days=1)
        if _ISO_STAMP_RE.fullmatch(text):
            at = datetime.fromisoformat(text)
            at = at if at.tzinfo is not None else at.replace(tzinfo=zone)
            return at, at
    except ValueError:
        return None
    return None


def _epoch_span(key: str, value: float, zone: tzinfo) -> Span | None:
    """An epoch-seconds or epoch-milliseconds number under a time-like key, as a moment."""
    assert isinstance(key, str), "key must be a string"
    if not _TIME_KEY_RE.search(key) or isinstance(value, bool):
        return None
    secs = value / 1000 if value > _EPOCH_SECONDS[1] else value
    if not _EPOCH_SECONDS[0] <= secs <= _EPOCH_SECONDS[1]:
        return None
    at = datetime.fromtimestamp(secs, tz=zone)
    return at, at


def _pair_span(row: dict) -> Span | None:
    """A row that splits its moment into a date and a clock cell ("dateEvent" 2026-10-06 + "strTime" 23:00:00,
    both UTC): the moment they make. A cell naming itself local is a venue's wall clock, not the instant."""
    assert isinstance(row, dict), "row must be a dict"
    days = [v for k, v in row.items() if isinstance(v, str) and "date" in str(k).lower()
            and "local" not in str(k).lower() and _ISO_DAY_RE.fullmatch(v.strip())]
    clocks = [v for k, v in row.items() if isinstance(v, str) and "time" in str(k).lower()
              and "local" not in str(k).lower() and _CLOCK_RE.fullmatch(v.strip())]
    if not days or not clocks:
        return None
    clock = clocks[0].strip()
    return iso_span(f"{days[0].strip()}T{clock if len(clock) > 5 else clock + ':00'}Z", UTC)


def _take(key: str, value: object, zone: tzinfo, spans: list[Span], cols: dict[str, list[float]],
          paired: bool = False) -> None:
    """One scalar cell: a timestamp joins the spans, a plain number joins its column. ``paired``: the row
    already made its moment from a date and a clock cell, so its bare dates add no whole-day span."""
    assert isinstance(spans, list) and isinstance(cols, dict), "collectors required"
    low = str(key).lower()
    if isinstance(value, str):
        span = iso_span(value, zone) if len(value) <= 40 else None
        if span is not None and not (paired and span[0] != span[1] and _ISO_DAY_RE.fullmatch(value.strip())):
            spans.append(span)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        span = _epoch_span(low, float(value), zone)
        if span is not None:
            spans.append(span)
        elif len(cols.get(low, ())) < _MAX_VALUES:
            cols.setdefault(low, []).append(float(value))


def scan(value: object, zone: tzinfo) -> tuple[list[Span], dict[str, list[float]]]:
    """Every moment and numeric column inside a built preview (iterative, bounded walk)."""
    assert zone is not None, "a zone is required"
    spans: list[Span] = []
    cols: dict[str, list[float]] = {}
    stack: list[object] = [value]
    seen = 0
    while stack and seen < _MAX_NODES:
        node = stack.pop()
        seen += 1
        if isinstance(node, list):
            stack.extend(node[:_MAX_LIST])
        elif isinstance(node, dict):
            pair = _pair_span(node)
            if pair is not None:
                spans.append(pair)
            for key, cell in list(node.items())[:_MAX_KEYS]:
                if isinstance(cell, dict):
                    stack.append(cell)
                    continue
                for item in (cell[:_MAX_LIST] if isinstance(cell, list) else [cell]):
                    if isinstance(item, (dict, list)):
                        stack.append(item)
                    else:
                        _take(str(key), item, zone, spans, cols, pair is not None)
    assert len(spans) >= 0 and seen <= _MAX_NODES, "the walk is bounded"
    return spans, cols


def read_result(preview: object, texts: list[str], zone: tzinfo, extra_spans: list[Span] | None = None) -> Result:
    """The Result of a built preview, the card's words and any moments the build already knows."""
    assert isinstance(texts, list), "texts must be a list"
    spans, cols = scan(preview, zone)
    spans = [*spans, *(extra_spans or [])]
    blob = " ".join(str(t) for t in texts).lower()
    return Result(spans=tuple(spans), fields={k: tuple(v) for k, v in cols.items()}, text=blob)
