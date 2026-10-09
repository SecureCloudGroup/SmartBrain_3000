"""RECOGNIZE (Round 20, Q3a): the ask's time, number, ordinal and comparator spans from two
pinned recognizer libraries, merged into ONE closed shape. The query layer uses them as
VALIDATORS of the model's clauses (an explicit time phrase decides ``time``; a ``where``
number must be one the ask states) — never as a substitute for the model's reading.

Libraries (imported lazily inside the calls — the app's import-time discipline, as
``stt_local`` imports faster-whisper):
- ms-recognizers-text-suite 1.0.1 (MIT; the community fork of Microsoft Recognizers-Text,
  importable only as ``ms_recognizers_*``): DateTime, Number, NumberWithUnit (dimension,
  currency, temperature) and Ordinal, culture en-us, reference = ``now`` in ``zone``.
- puckling 0.5.0 (Apache-2.0; a pure-Python port of Duckling): time + duration with
  ``Context(reference_time=now, locale=EN/US)``; resolves holidays WITH the asked year.

Rules — each measured in the Q0 bake-off over 4,737 asks (scratch bakeoff/out/summary.md):
1. Word boundaries. A span that ENDS inside a word is dropped ("Sa" in "Salt Lake", "in 9810"
   in a zip); one that STARTS inside a word is cut at the next word start, because puckling
   absorbs a preposition that is the tail of the previous word ("Boston tomorrow" read as
   "on tomorrow"); nothing left → dropped ("now" in "snow", "cent" in "recent").
2. Mask. A span overlapping a ``masked`` range (parameter-consumed entities, zips, codes) is
   dropped.
3. Agreement (time only). A time span is kept when the other library also found a time span
   overlapping it: puckling-only spans were 10/10 false ("sun" in "the sun's UV" read as
   Sunday), ms-only spans 71, nearly all false or tokenless ("the day", "may" in "Cape May",
   model years in "2020 toyota camry", "the first quarter" moon). A holiday named in
   ``_HOLIDAYS`` needs no partner. ms resolves the span; puckling only where ms cannot
   ("at the moment", "later today").
4. Holidays. puckling's holiday value (with the asked year) wins over ms's date for the same
   words — ms ignores "in 2027"; a puckling phrase only that ms date confirmed stays ("this
   year" in "Thanksgiving this year"). Seasons and places puckling labels holidays ("winter",
   "fall", "Corpus Christi") are dropped: a holiday is kept only when its words name one in
   ``_HOLIDAYS`` (a built-in table — the ``holidays`` package is a lookup table only).
5. Range vs point. "this week / next week / this month / this year": ms's daterange wins over
   puckling's grain-point (kind ``range``, ms bounds).
6. Rolling ranges. "past 12 months" → ``past_days:365`` (backward), "next 6 hours" →
   ``next_hours:6`` — ms's rolling bounds; every point-only library resolved them FORWARD.
7. Units. A unit reading whose unit token is followed by a capitalized word ("magnitude 4 in
   Alaska") drops the unit and keeps the number.
8. Comparators. No library reads them: a number preceded by a ``_CMP`` phrase with at most one
   word between ("above magnitude 3", "stronger than magnitude 4") — the one ask parser here
   beside the word-boundary check.
9. A number or ordinal inside a kept time span belongs to the time phrase ("past 12 months",
   "every 30 minutes", "July 4th") and is dropped.

Output (closed): ``{"time": [{text, start, end, kind, from, to, grain, lib, token}],
"numbers": [{text, start, end, value, unit, lib}], "ordinals": [{text, start, end, value, lib}],
"comparators": [{text, start, end, op, value, unit}]}`` — offsets into the ask, ``end``
exclusive; ``kind`` ∈ point | range | holiday | duration; ``from``/``to`` ISO-8601 in
``zone`` (None for a duration); ``token`` = the Query IR token the span implies, or None when
no token fits (the bounds stand alone: "at noon", "since Monday", "every 30 minutes").
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

_MAX_ASK = 2000            # characters of an ask the recognizers read (ni_flow._MAX_REQUEST)
_MAX_SPANS = 64            # spans kept per library per ask
_MAX_WORDS = 400           # words of an ask scanned by the adjacency rules
TIME_KINDS = ("point", "range", "holiday", "duration")
# comparison phrase → IR op. "within" bounds from above (within 50 miles = distance <= 50);
# "at least"/"at most" are inclusive; every other phrase is strict. Two-word phrases first.
_CMP: tuple[tuple[str, str], ...] = (
    ("more than", ">"), ("greater than", ">"), ("stronger than", ">"), ("bigger than", ">"),
    ("larger than", ">"), ("higher than", ">"), ("at least", ">="),
    ("less than", "<"), ("fewer than", "<"), ("smaller than", "<"), ("lower than", "<"),
    ("weaker than", "<"), ("at most", "<="),
    ("above", ">"), ("over", ">"), ("below", "<"), ("under", "<"), ("within", "<="))
# US holidays a span must name to be kept as one (words, apostrophes removed, lowercase).
_HOLIDAYS: tuple[tuple[str, ...], ...] = (
    ("thanksgiving",), ("christmas",), ("xmas",), ("new", "years"), ("new", "year"),
    ("independence", "day"), ("july", "4th"), ("fourth", "of", "july"), ("4th", "of", "july"),
    ("labor", "day"), ("memorial", "day"), ("easter",), ("halloween",), ("veterans", "day"),
    ("mlk", "day"), ("martin", "luther", "king"), ("presidents", "day"),
    ("washingtons", "birthday"), ("columbus", "day"), ("juneteenth",), ("mothers", "day"),
    ("fathers", "day"))
_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_NIGHT_PARTS = frozenset({"EV", "NI"})     # ms part-of-day timex suffixes read as "tonight"
_DAY_GRAINS = ("second", "minute", "hour", "day")


def words(text: str) -> list[tuple[str, int, int]]:
    """(lowercased word, start, end) for every maximal run of letters/digits — the only
    tokenizer the query layer applies to an ask (no regex)."""
    assert isinstance(text, str), "text must be a str"
    assert len(text) <= _MAX_ASK * 4, "text bounded"
    out: list[tuple[str, int, int]] = []
    start = -1
    for i, ch in enumerate(text):  # bounded by the text
        if ch.isalnum():
            if start < 0:
                start = i
        elif start >= 0:
            out.append((text[start:i].lower(), start, i))
            start = -1
    if start >= 0:
        out.append((text[start:].lower(), start, len(text)))
    return out


def _snap(ask: str, start: int, end: int) -> tuple[int, int] | None:
    """Rule 1: (start, end) on word boundaries, or None."""
    assert isinstance(ask, str), "ask must be a str"
    assert isinstance(start, int) and isinstance(end, int), "offsets must be ints"
    start, end = max(0, start), min(len(ask), end)
    if start >= end or (end < len(ask) and ask[end].isalnum() and ask[end - 1].isalnum()):
        return None
    if start > 0 and ask[start - 1].isalnum() and ask[start].isalnum():
        for _ in range(end - start):  # bounded: skip the rest of the word it starts inside
            if start >= end or not ask[start].isalnum():
                break
            start += 1
    for _ in range(end - start):  # bounded: no leading or trailing separators
        if start < end and not ask[start].isalnum():
            start += 1
        elif start < end and not ask[end - 1].isalnum():
            end -= 1
        else:
            break
    return (start, end) if start < end else None


def _overlaps(a: dict, b: dict) -> bool:
    assert "start" in a and "end" in a, "a must carry offsets"
    assert "start" in b and "end" in b, "b must carry offsets"
    return a["start"] < b["end"] and b["start"] < a["end"]


def _kept(ask: str, start: int, end: int, masked: tuple) -> tuple[int, int] | None:
    """Rules 1 + 2 for one raw span."""
    assert isinstance(masked, tuple), "masked must be a tuple"
    assert len(masked) <= _MAX_SPANS, "masked bounded"
    snapped = _snap(ask, start, end)
    if snapped is None:
        return None
    if any(snapped[0] < m_end and m_start < snapped[1] for m_start, m_end in masked):
        return None
    return snapped


def _holiday_named(*texts: str) -> bool:
    """Rule 4: does one of ``texts`` name a holiday from ``_HOLIDAYS`` (word sequence)?"""
    assert texts, "at least one text"
    assert all(isinstance(t, str) for t in texts), "texts must be str"
    for text in texts:  # bounded: two texts
        ws = [w for w, _, _ in words(text.replace("'", "").replace("’", ""))]
        for name in _HOLIDAYS:  # bounded table
            n = len(name)
            if any(tuple(ws[i:i + n]) == name for i in range(len(ws) - n + 1)):
                return True
    return False


def _iso(value: dt.datetime | None) -> str | None:
    assert value is None or isinstance(value, dt.datetime), "value must be a datetime"
    assert value is None or value.tzinfo is not None, "value must be aware"
    return None if value is None else value.isoformat()


def _local(text: str, tz: ZoneInfo) -> dt.datetime | None:
    """An ms date ("2026-10-05") or datetime ("2026-10-05 20:00:00") as an aware instant."""
    assert isinstance(tz, ZoneInfo), "tz must be a ZoneInfo"
    assert text is None or isinstance(text, str), "text must be a str"
    try:
        return dt.datetime.fromisoformat(str(text).strip()).replace(tzinfo=tz)
    except (TypeError, ValueError):
        return None


def _day_token(day: dt.date, today: dt.date) -> str:
    assert isinstance(day, dt.date), "day must be a date"
    assert isinstance(today, dt.date), "today must be a date"
    one = dt.timedelta(days=1)
    return {today: "today", today + one: "tomorrow", today - one: "yesterday"}.get(day, f"date:{day.isoformat()}")


def _rolling(timex: str, start: dt.datetime, end: dt.datetime, now: dt.datetime) -> tuple[str | None, str]:
    """Rule 6: "(start,end,PnX)" → (past_/next_ token, grain); None when the stretch has no token."""
    assert timex.startswith("("), "a rolling timex"
    assert start <= end, "start before end"
    period = timex.rstrip(")").rsplit(",", 1)[-1]
    hours = "T" in period
    unit = period[-1:]
    grain = ("hour" if unit == "H" else "minute") if hours else \
        {"D": "day", "W": "week", "M": "month", "Y": "year"}.get(unit, "day")
    back = end <= now + dt.timedelta(minutes=1) if hours else end.date() <= now.date()
    n = int((end - start).total_seconds() // 3600) if hours else (end - start).days
    if (hours and unit != "H") or not 1 <= n <= (999 if (back or hours) else 99):
        return None, grain
    return f"{'past' if back else 'next'}_{'hours' if hours else 'days'}:{n}", grain


def _range_token(timex: str, now: dt.datetime) -> tuple[str | None, str]:
    """Rule 5: a calendar daterange timex → (token or None, grain)."""
    assert isinstance(timex, str) and timex, "timex required"
    assert isinstance(now, dt.datetime), "now must be a datetime"
    iso_year, iso_week, _ = now.isocalendar()
    nxt_year, nxt_week, _ = (now + dt.timedelta(days=7)).isocalendar()
    if timex.endswith("-WE"):
        return ("weekend" if timex == f"{iso_year}-W{iso_week:02d}-WE" else None), "day"
    if len(timex) == 8 and timex[4:6] == "-W":
        token = {f"{iso_year}-W{iso_week:02d}": "this_week", f"{nxt_year}-W{nxt_week:02d}": "next_week"}
        return token.get(timex), "week"
    if len(timex) == 7 and timex[4] == "-" and timex[:4].isdigit() and timex[5:].isdigit():
        return ("this_month" if timex == now.strftime("%Y-%m") else f"month:{timex}"), "month"
    if len(timex) == 4 and timex.isdigit():
        return f"year:{timex}", "year"
    return None, "day"


def _ms_value(values: list, partner_day: dt.date | None, today: dt.date) -> dict | None:
    """The one ms resolution value to read: the one on puckling's partner day, else the first
    not before today (ms lists last year's and this year's "July 4th"), else the last."""
    assert isinstance(values, list), "values must be a list"
    assert isinstance(today, dt.date), "today must be a date"
    vals = [v for v in values[:8] if isinstance(v, dict)]
    if len(vals) <= 1:
        return vals[0] if vals else None
    day = lambda v: str(v.get("value") or v.get("start") or "")[:10]
    if partner_day is not None:
        hit = next((v for v in vals if day(v) == partner_day.isoformat()), None)
        if hit is not None:
            return hit
    return next((v for v in vals if day(v) >= today.isoformat()), vals[-1])


def _span(ask: str, raw: dict, lib: str, **fields: object) -> dict:
    """One closed time span."""
    assert lib in ("ms", "puckling"), "lib must be ms or puckling"
    assert fields.get("kind") in TIME_KINDS, "kind must be closed"
    out = {"text": ask[raw["start"]:raw["end"]], "start": raw["start"], "end": raw["end"], "kind": fields["kind"],
           "from": _iso(fields.get("lo")), "to": _iso(fields.get("hi")), "grain": fields.get("grain"),
           "lib": lib, "token": fields.get("token")}
    return out


def _from_ms(ask: str, raw: dict, now: dt.datetime, partner_day: dt.date | None) -> dict | None:
    """One ms DateTime result as a time span; None when ms could not resolve it (puckling then)."""
    assert isinstance(raw, dict) and "type" in raw, "an ms raw span"
    assert now.tzinfo is not None, "now must be aware"
    tz, today, typ = now.tzinfo, now.date(), raw["type"]
    if typ in ("duration", "set"):  # a length or a cadence ("30 year", "every 30 minutes"): never a stretch
        return _span(ask, raw, "ms", kind="duration", grain=None)
    v = _ms_value(list((raw.get("resolution") or {}).get("values") or []), partner_day, today)
    if v is None or (v.get("value") == "not resolved" and not v.get("start")):
        return None
    timex = str(v.get("timex") or "")
    if v.get("Mod"):  # an open stretch ("since Monday", "until Friday", "by 5 pm"): bounds only
        return _span(ask, raw, "ms", kind="range", lo=_local(v.get("start"), tz), hi=_local(v.get("end"), tz),
                     grain="day")
    if typ == "datetime":
        at = _local(v.get("value"), tz)
        return None if at is None else _span(ask, raw, "ms", kind="point", lo=at, hi=at, grain="second" if
                                             timex == "PRESENT_REF" else "hour",
                                             token="now" if timex == "PRESENT_REF" else None)
    if typ == "time":
        at = _local(f"{today.isoformat()} {v.get('value')}", tz)
        return None if at is None else _span(ask, raw, "ms", kind="point", lo=at, hi=at, grain="hour")
    if typ == "date":
        at = _local(v.get("value"), tz)
        if at is None:
            return None
        token = f"dow:{_DOW[int(timex[9]) - 1]}" if timex.startswith("XXXX-WXX-") and timex[9:].isdigit() \
            else _day_token(at.date(), today)
        return _span(ask, raw, "ms", kind="point", lo=at, hi=at + dt.timedelta(days=1), grain="day", token=token)
    return _from_ms_range(ask, raw, v, now)


def _from_ms_range(ask: str, raw: dict, v: dict, now: dt.datetime) -> dict | None:
    """ms daterange / datetimerange / timerange: rolling, part-of-day or calendar stretches."""
    assert isinstance(v, dict), "v must be an ms value"
    assert now.tzinfo is not None, "now must be aware"
    tz, timex = now.tzinfo, str(v.get("timex") or "")
    lo, hi = _local(v.get("start"), tz), _local(v.get("end"), tz)
    if lo is None or hi is None or hi < lo:
        return None
    part = timex.rsplit("T", 1)[-1] if "T" in timex else ""
    if timex.startswith("("):
        token, grain = _rolling(timex, lo, hi, now)
    elif part in ("MO", "AF", "EV", "NI", "DT", "MI"):
        grain = "hour"
        if timex.startswith("XXXX-WXX-") and timex[9:10].isdigit():
            token = f"dow:{_DOW[int(timex[9]) - 1]}"
        elif part in _NIGHT_PARTS and lo.date() == now.date():
            token = "tonight"
        else:
            token = _day_token(lo.date(), now.date())
    else:
        token, grain = _range_token(timex, now)
    return _span(ask, raw, "ms", kind="range", lo=lo, hi=hi, grain=grain, token=token)


def _month_after(day: dt.datetime) -> dt.datetime:
    assert isinstance(day, dt.datetime), "day must be a datetime"
    assert day.day == 1, "day must be a month start"
    return day.replace(year=day.year + 1, month=1) if day.month == 12 else day.replace(month=day.month + 1)


def _from_pk(ask: str, raw: dict, now: dt.datetime) -> dict:
    """One puckling entity as a time span (holidays, ms-unresolvable words, puckling-only grains)."""
    assert isinstance(raw, dict) and "entity" in raw, "a puckling raw span"
    assert now.tzinfo is not None, "now must be aware"
    ent, today = raw["entity"], now.date()
    if ent.dim == "duration":
        return _span(ask, raw, "puckling", kind="duration", grain=ent.value.grain.value)
    prim = ent.value.primary
    kind_name = type(prim).__name__
    if kind_name == "IntervalValue":
        lo, hi = prim.start.value.astimezone(now.tzinfo), prim.end.value.astimezone(now.tzinfo)
        weekend = lo.weekday() == 5 and (hi - lo).days == 2 and lo.isocalendar()[:2] == now.isocalendar()[:2]
        return _span(ask, raw, "puckling", kind="range", lo=lo, hi=hi, grain=prim.start.grain.value,
                     token="weekend" if weekend else None)
    if kind_name != "InstantValue":  # an open interval ("since Monday"): bounds only
        at = prim.instant.value.astimezone(now.tzinfo)
        after = getattr(prim.direction, "value", "") == "after"
        return _span(ask, raw, "puckling", kind="range", lo=at if after else None, hi=None if after else at,
                     grain=prim.instant.grain.value)
    at, grain = prim.value.astimezone(now.tzinfo), prim.grain.value
    if raw["holiday"]:
        day = at.replace(hour=0, minute=0, second=0, microsecond=0)
        return _span(ask, raw, "puckling", kind="holiday", lo=day, hi=day + dt.timedelta(days=1), grain="day",
                     token=f"date:{day.date().isoformat()}")
    if grain in ("second", "minute"):
        token = "now" if abs((at - now).total_seconds()) <= 120 else None
        return _span(ask, raw, "puckling", kind="point", lo=at, hi=at, grain=grain, token=token)
    if grain in ("hour", "day"):
        hi = at + (dt.timedelta(hours=1) if grain == "hour" else dt.timedelta(days=1))
        return _span(ask, raw, "puckling", kind="point", lo=at, hi=hi, grain=grain,
                     token=_day_token(at.date(), today) if grain == "day" else None)
    return _from_pk_calendar(ask, raw, at, grain, now)


def _from_pk_calendar(ask: str, raw: dict, at: dt.datetime, grain: str, now: dt.datetime) -> dict:
    """puckling's grain-point week / month / quarter / year as a range (rule 5)."""
    assert grain in ("week", "month", "quarter", "year"), "a calendar grain"
    assert now.tzinfo is not None, "now must be aware"
    monday = (now - dt.timedelta(days=now.weekday())).date()
    if grain == "week":
        token = {monday: "this_week", monday + dt.timedelta(days=7): "next_week"}.get(at.date())
        return _span(ask, raw, "puckling", kind="range", lo=at, hi=at + dt.timedelta(days=7), grain=grain, token=token)
    if grain == "month":
        token = "this_month" if (at.year, at.month) == (now.year, now.month) else f"month:{at.strftime('%Y-%m')}"
        return _span(ask, raw, "puckling", kind="range", lo=at, hi=_month_after(at), grain=grain, token=token)
    if grain == "year":
        return _span(ask, raw, "puckling", kind="range", lo=at, hi=at.replace(year=at.year + 1), grain=grain,
                     token=f"year:{at.year}")
    return _span(ask, raw, "puckling", kind="range", lo=at, hi=None, grain=grain)


def _ms_time(ask: str, now: dt.datetime, masked: tuple) -> list[dict]:
    """ms DateTime results, word-snapped and masked (rules 1-2)."""
    assert now.tzinfo is not None, "now must be aware"
    assert isinstance(masked, tuple), "masked must be a tuple"
    # lazy: the app's import-time discipline (stt_local imports faster-whisper the same way)
    from ms_recognizers_date_time import Culture, recognize_datetime

    out = []
    for r in recognize_datetime(ask, Culture.English, reference=now.replace(tzinfo=None))[:_MAX_SPANS]:
        kept = _kept(ask, int(r.start), int(r.end) + 1, masked)  # ms ends are inclusive
        if kept is not None:
            out.append({"start": kept[0], "end": kept[1], "type": str(r.type_name).rsplit(".", 1)[-1],
                        "resolution": r.resolution})
    return out


def _pk_time(ask: str, now: dt.datetime, masked: tuple) -> list[dict]:
    """puckling time + duration entities, word-snapped and masked; a 'holiday' that names no
    US holiday (a season, a place) is dropped whole (rules 1, 2, 4)."""
    assert now.tzinfo is not None, "now must be aware"
    assert isinstance(masked, tuple), "masked must be a tuple"
    import puckling  # lazy: the app's import-time discipline

    ctx = puckling.Context(reference_time=now, locale=puckling.Locale(puckling.Lang.EN, puckling.Region.US))
    out = []
    for ent in puckling.parse(ask, ctx, puckling.Options(with_latent=False), dims=("time", "duration"))[:_MAX_SPANS]:
        kept = _kept(ask, int(ent.start), int(ent.end), masked)
        holiday = getattr(ent.value, "holiday", None) if ent.dim == "time" else None
        if kept is None or (holiday and not _holiday_named(ask[kept[0]:kept[1]], str(holiday))):
            continue
        out.append({"start": kept[0], "end": kept[1], "entity": ent, "holiday": bool(holiday)})
    return out


def _merge_time(ask: str, ms_raw: list, pk_raw: list, now: dt.datetime) -> list[dict]:
    """Rules 3-5: holidays from puckling; every other span needs both libraries; ms resolves."""
    assert isinstance(ms_raw, list) and isinstance(pk_raw, list), "raw span lists"
    assert now.tzinfo is not None, "now must be aware"
    holidays = [p for p in pk_raw if p["holiday"]]
    out = [_from_pk(ask, p, now) for p in holidays]
    for m in ms_raw:  # bounded by _MAX_SPANS
        partners = [p for p in pk_raw if _overlaps(m, p)]
        if not partners or (m["type"] == "date" and any(_overlaps(m, h) for h in holidays)):
            continue
        pk_day = next((p["entity"].value.primary.value.astimezone(now.tzinfo).date() for p in partners
                       if p["entity"].dim == "time" and type(p["entity"].value.primary).__name__ == "InstantValue"),
                      None)
        out.append(_from_ms(ask, m, now, pk_day) or _from_pk(ask, partners[0], now))
    for p in pk_raw:  # a puckling-resolved span the ms spans did not already cover ("this year" beside a holiday)
        if not p["holiday"] and not any(_overlaps(p, o) for o in out) and any(_overlaps(p, m) for m in ms_raw):
            out.append(_from_pk(ask, p, now))
    return sorted(out, key=lambda s: (s["start"], s["end"]))[:_MAX_SPANS]


def _num(value: object) -> int | float | None:
    assert value is None or isinstance(value, (str, int, float)), "a scalar"
    assert not isinstance(value, bool), "not a bool"
    try:
        x = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return int(x) if x.is_integer() else x


def _capital_follows(ask: str, end: int) -> bool:
    """Rule 7: the next word after ``end`` starts with a capital letter."""
    assert isinstance(ask, str), "ask must be a str"
    assert 0 <= end <= len(ask), "end within the ask"
    rest = ask[end:].lstrip()
    return bool(rest) and rest[0].isupper()


def _inside(span: dict, spans: list[dict]) -> bool:
    assert "start" in span, "span must carry offsets"
    assert isinstance(spans, list), "spans must be a list"
    return any(s["start"] <= span["start"] and span["end"] <= s["end"] for s in spans)


def _numbers(ask: str, masked: tuple, time_spans: list[dict]) -> list[dict]:
    """ms Number + NumberWithUnit (dimension, currency, temperature), rules 1, 2, 7, 9."""
    assert isinstance(masked, tuple), "masked must be a tuple"
    assert isinstance(time_spans, list), "time spans must be a list"
    # lazy: the app's import-time discipline
    from ms_recognizers_number import Culture, recognize_number
    from ms_recognizers_number_with_unit import (
        recognize_currency,
        recognize_dimension,
        recognize_temperature,
    )

    plain, units = [], []
    for fn in (recognize_number, recognize_dimension, recognize_currency, recognize_temperature):  # bounded
        for r in fn(ask, Culture.English)[:_MAX_SPANS]:
            res = r.resolution if isinstance(r.resolution, dict) else {}
            kept, value = _kept(ask, int(r.start), int(r.end) + 1, masked), _num(res.get("value"))
            unit = res.get("unit") if fn is not recognize_number else None
            if kept is None or value is None or (unit and _capital_follows(ask, kept[1])):
                continue
            (units if unit else plain).append({"text": ask[kept[0]:kept[1]], "start": kept[0], "end": kept[1],
                                               "value": value, "unit": unit, "lib": "ms"})
    out = units + [p for p in plain if not any(_overlaps(p, u) for u in units)]
    out = [n for n in out if not _inside(n, time_spans)]
    unique = {(n["start"], n["end"]): n for n in sorted(out, key=lambda n: (n["start"], n["end"]))}
    return list(unique.values())[:_MAX_SPANS]


def _ordinals(ask: str, masked: tuple, time_spans: list[dict]) -> list[dict]:
    """ms Ordinal ("first", "3rd"; relative "next"/"last"/"current" resolve to 0), rules 1, 2, 9."""
    assert isinstance(masked, tuple), "masked must be a tuple"
    assert isinstance(time_spans, list), "time spans must be a list"
    # lazy: the app's import-time discipline
    from ms_recognizers_number import Culture, recognize_ordinal

    out = []
    for r in recognize_ordinal(ask, Culture.English)[:_MAX_SPANS]:
        res = r.resolution if isinstance(r.resolution, dict) else {}
        kept, value = _kept(ask, int(r.start), int(r.end) + 1, masked), _num(res.get("value"))
        if kept is None or not isinstance(value, int):
            continue
        span = {"text": ask[kept[0]:kept[1]], "start": kept[0], "end": kept[1], "value": value, "lib": "ms"}
        if not _inside(span, time_spans):
            out.append(span)
    return out


def _comparators(ask: str, numbers: list[dict]) -> list[dict]:
    """Rule 8: a number preceded by a ``_CMP`` phrase, at most one word between them."""
    assert isinstance(ask, str), "ask must be a str"
    assert isinstance(numbers, list), "numbers must be a list"
    table = dict(_CMP)
    ws = words(ask)[:_MAX_WORDS]
    out = []
    for n in numbers:  # bounded by _MAX_SPANS
        before = [w for w in ws if w[2] <= n["start"]][-4:]
        hit = None
        for gap in (0, 1):
            stop = len(before) - gap
            for size in (2, 1):
                phrase = " ".join(w[0] for w in before[stop - size:stop]) if stop - size >= 0 else ""
                if hit is None and phrase in table:
                    hit = (table[phrase], before[stop - size][1])
        if hit is not None:
            out.append({"text": ask[hit[1]:n["end"]], "start": hit[1], "end": n["end"], "op": hit[0],
                        "value": n["value"], "unit": n["unit"]})
    return out


def recognize(ask: str, *, now: dt.datetime, zone: str, masked: list[tuple[int, int]] | tuple = ()) -> dict:
    """The ask's spans in the closed shape the module docstring states. ``now`` is the
    reference instant (aware); ``zone`` the card's IANA zone; ``masked`` the char ranges of
    parameter-consumed entities (a place, a team, a zip) whose spans are dropped."""
    assert isinstance(ask, str) and ask.strip(), "ask required"
    assert isinstance(now, dt.datetime) and now.tzinfo is not None, "now must be an aware datetime"
    text = ask[:_MAX_ASK]
    local = now.astimezone(ZoneInfo(zone))
    mask = tuple((int(a), int(b)) for a, b in list(masked)[:_MAX_SPANS])
    time = _merge_time(text, _ms_time(text, local, mask), _pk_time(text, local, mask), local)
    numbers = _numbers(text, mask, time)
    return {"time": time, "numbers": numbers, "ordinals": _ordinals(text, mask, time),
            "comparators": _comparators(text, numbers)}
