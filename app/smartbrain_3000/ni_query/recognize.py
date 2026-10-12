"""RECOGNIZE (Round 20, Q3a): the ask's time, number, ordinal and comparator spans from a pinned
recognizer library and a pinned holiday calendar, merged into ONE closed shape. The query layer
uses them as VALIDATORS of the model's clauses (an explicit time phrase decides ``time``; a
``where`` number must be one the ask states) — never as a substitute for the model's reading.

Libraries (imported lazily inside the calls — the app's import-time discipline, as
``stt_local`` imports faster-whisper):
- ms-recognizers-text-suite 1.0.1 (MIT; the community fork of Microsoft Recognizers-Text,
  importable only as ``ms_recognizers_*``): DateTime, Number, NumberWithUnit (dimension,
  currency, temperature) and Ordinal, culture en-us, reference = ``now`` in ``zone``.
- holidays 0.106 (MIT; pure Python, python-dateutil its one dependency): the US calendar's
  holiday dates for the asked year (rule 4). puckling 0.5.0, the Duckling port the bake-off
  paired with ms, left in Round 20: no release of it installs on Python 3.12, the launcher's.

Rules — each measured in the Q0 bake-off over 4,737 asks (scratch bakeoff/out/summary.md; rules
3-4 re-measured on ms alone when puckling left):
1. Word boundaries. A span that ENDS inside a word is dropped ("Sa" in "Salt Lake", "in 9810"
   in a zip); one that STARTS inside a word is cut at the next word start; nothing left →
   dropped ("now" in "snow", "cent" in "recent").
2. Mask. A span overlapping a ``masked`` range (parameter-consumed entities, zips, codes) is
   dropped.
3. One library. A time span is kept when ms yields it and rules 1-2 keep it; ms resolves it, and
   a span ms cannot resolve is dropped ("later today", "fall", "Corpus Christi").
4. Holidays. An ms date span with no open bound is a holiday when its words name one in
   ``_HOLIDAYS`` or its timex is ms's floating-holiday pattern (``XXXX-11-WXX-4-4``). When its
   words — less a trailing year phrase ("2027", "in 2027", "this / next / last year"), a leading
   "the" and a trailing "day" — name a ``_HOLIDAYS`` entry exactly, its day is that holiday's in
   the asked year (the span's own year phrase, else the ask's first ms year — "in 2027", which ms
   alone ignores): the US calendar's day of that name or a closed rule (``_holiday_day``); with no
   year, its next occurrence from today. Any other holiday span ("Christmas Eve", "Labor Day
   weekend") keeps ms's own day, none outside the asked year. A year phrase ms read into the span
   stays a span of its own (``year:2027``): the holiday is the subject, the year the stretch.
5. Range vs point. "this week / next week / this month / this year": ms's daterange (kind
   ``range``, ms bounds).
6. Rolling ranges. "past 12 months" → ``past_days:365`` (backward), "next 6 hours" →
   ``next_hours:6`` — ms's rolling bounds; every point-only library resolved them FORWARD. A
   period of no year ("the first quarter", "q2") is no stretch from now: no token.
7. Units. A unit reading whose unit token is followed by a capitalized word ("magnitude 4 in
   Alaska") drops the unit and keeps the number.
8. Comparators. No library reads them: a number preceded by a ``_CMP`` phrase with at most one
   word between ("above magnitude 3", "stronger than magnitude 4") — the one ask parser here
   beside the word-boundary check. ``quantity`` (Round 20, Q3b): the words that may name what is
   compared, nearest first — the word between the phrase and the number ("magnitude"), then the
   word before the phrase ("earthquakes above 4.0"); ``clauses.comparator_cell`` reads them.
9. A number or ordinal inside a kept time span belongs to the time phrase ("past 12 months",
   "every 30 minutes", "July 4th") and is dropped.
10. Now phrases (Round 20, Q3b — the removed window parser's table): a closed word table
   (``_NOW_PHRASES``: currently, at present, as of now, at the moment, as we speak, right this
   minute / second / moment / instant, this instant; "rn" / "atm" only as the ask's last word —
   "open atm near me" is a cash machine) is a point span with token ``now`` that needs no library
   partner and wins over an overlapping library span ("this minute" alone is tokenless).

Names (Round 20, Q3b): ``name_masks`` turns the proper names the intent copied from the ask into
masked ranges, so a name is never a time phrase or a stated number ("Saturday Night Live" is no
Saturday, "Black Friday" no Friday, "Area 51" no 51) — except a name the holiday table names
("when is Thanksgiving" keeps the holiday's date).

Output (closed): ``{"time": [{text, start, end, kind, from, to, grain, lib, token}],
"numbers": [{text, start, end, value, unit, lib}], "ordinals": [{text, start, end, value, lib}],
"comparators": [{text, start, end, op, value, unit, quantity}]}`` — offsets into the ask, ``end``
exclusive; ``kind`` ∈ point | range | holiday | duration; ``lib`` ∈ ms | holidays | table (rule
10); ``from``/``to`` ISO-8601 in
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
# US holidays (words, apostrophes removed, lowercase) → the US calendar's name for the day (``holidays``;
# matched case-insensitively, so an "(observed)" day is another name) or a closed rule (``_holiday_day``).
_HOLIDAYS: dict[tuple[str, ...], str] = {
    ("thanksgiving",): "Thanksgiving Day", ("christmas",): "Christmas Day", ("xmas",): "Christmas Day",
    ("new", "years"): "New Year's Day", ("new", "year"): "New Year's Day",
    ("independence", "day"): "Independence Day", ("july", "4th"): "Independence Day",
    ("fourth", "of", "july"): "Independence Day", ("4th", "of", "july"): "Independence Day",
    ("labor", "day"): "Labor Day", ("memorial", "day"): "Memorial Day", ("veterans", "day"): "Veterans Day",
    ("mlk", "day"): "Martin Luther King Jr. Day", ("martin", "luther", "king"): "Martin Luther King Jr. Day",
    ("presidents", "day"): "Washington's Birthday", ("washingtons", "birthday"): "Washington's Birthday",
    ("columbus", "day"): "Columbus Day", ("juneteenth",): "Juneteenth National Independence Day",
    ("easter",): "easter", ("easter", "sunday"): "easter", ("halloween",): "halloween",
    ("valentines", "day"): "valentines", ("st", "patricks", "day"): "st patricks",
    ("saint", "patricks", "day"): "st patricks", ("mothers", "day"): "mothers", ("fathers", "day"): "fathers"}
_FIXED_DAYS: dict[str, tuple[int, int]] = {"halloween": (10, 31), "valentines": (2, 14), "st patricks": (3, 17)}
_NTH_SUNDAYS: dict[str, tuple[int, int]] = {"mothers": (5, 2), "fathers": (6, 3)}   # (month, nth Sunday)
_YEAR_WORDS = {"this": 0, "next": 1, "last": -1}   # "<word> year" ending a holiday span → years from now
_YEARS = range(1000, 9999)                          # a written year a holiday is dated in
_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# rule 10: now phrases, longest first (an overlapping shorter one never doubles a span)
_NOW_PHRASES: tuple[tuple[str, ...], ...] = (
    ("right", "this", "minute"), ("right", "this", "second"), ("right", "this", "moment"),
    ("right", "this", "instant"), ("as", "of", "now"), ("at", "the", "moment"), ("as", "we", "speak"),
    ("at", "present"), ("this", "instant"), ("currently",), ("rn",), ("atm",))
_LAST_WORD_ONLY = frozenset({("rn",), ("atm",)})
_MAX_NAMES = 8
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
    """Rule 6: "(start,end,PnX)" → (past_/next_ token, grain); None when the stretch has no token — a
    period of no year ("(XXXX-01-01,XXXX-04-01,P3M)": "the first quarter", "q2") is no stretch from now."""
    assert timex.startswith("("), "a rolling timex"
    assert start <= end, "start before end"
    period = timex.rstrip(")").rsplit(",", 1)[-1]
    hours = "T" in period
    unit = period[-1:]
    grain = ("hour" if unit == "H" else "minute") if hours else \
        {"D": "day", "W": "week", "M": "month", "Y": "year"}.get(unit, "day")
    back = end <= now + dt.timedelta(minutes=1) if hours else end.date() <= now.date()
    n = int((end - start).total_seconds() // 3600) if hours else (end - start).days
    if timex.startswith("(XXXX") or (hours and unit != "H") or not 1 <= n <= (999 if (back or hours) else 99):
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


def _ms_value(values: list, today: dt.date) -> dict | None:
    """The one ms resolution value to read: the first not before today (ms lists last year's
    and this year's "July 4th"), else the last."""
    assert isinstance(values, list), "values must be a list"
    assert isinstance(today, dt.date), "today must be a date"
    vals = [v for v in values[:8] if isinstance(v, dict)]
    if len(vals) <= 1:
        return vals[0] if vals else None
    day = lambda v: str(v.get("value") or v.get("start") or "")[:10]
    return next((v for v in vals if day(v) >= today.isoformat()), vals[-1])


def _span(ask: str, raw: dict, lib: str, **fields: object) -> dict:
    """One closed time span."""
    assert lib in ("ms", "holidays"), "lib must be ms or holidays"
    assert fields.get("kind") in TIME_KINDS, "kind must be closed"
    out = {"text": ask[raw["start"]:raw["end"]], "start": raw["start"], "end": raw["end"], "kind": fields["kind"],
           "from": _iso(fields.get("lo")), "to": _iso(fields.get("hi")), "grain": fields.get("grain"),
           "lib": lib, "token": fields.get("token")}
    return out


def _from_ms(ask: str, raw: dict, now: dt.datetime) -> dict | None:
    """One ms DateTime result as a time span; None when ms could not resolve it (rule 3: dropped)."""
    assert isinstance(raw, dict) and "type" in raw, "an ms raw span"
    assert now.tzinfo is not None, "now must be aware"
    tz, today, typ = now.tzinfo, now.date(), raw["type"]
    if typ in ("duration", "set"):  # a length or a cadence ("30 year", "every 30 minutes"): never a stretch
        return _span(ask, raw, "ms", kind="duration", grain=None)
    v = _ms_value(list((raw.get("resolution") or {}).get("values") or []), today)
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


def _year_phrase(ws: list[tuple[str, int, int]], this_year: int) -> tuple[int, int, int | None]:
    """Rule 4: the year phrase ending a span's words → (the words before it, its first word, its year);
    (len, len, None) without one. An "in" before a written year belongs to neither part."""
    assert isinstance(ws, list), "words must be a list"
    assert isinstance(this_year, int), "this_year must be an int"
    n = len(ws)
    if n >= 3 and ws[-1][0] == "year" and ws[-2][0] in _YEAR_WORDS:
        return n - 2, n - 2, this_year + _YEAR_WORDS[ws[-2][0]]
    if n >= 2 and ws[-1][0].isdigit() and len(ws[-1][0]) == 4 and int(ws[-1][0]) in _YEARS:
        return (n - 2 if ws[-2][0] == "in" else n - 1), n - 1, int(ws[-1][0])
    return n, n, None


def _holiday_name(text: str) -> str | None:
    """Rule 4: the ``_HOLIDAYS`` entry ``text`` names exactly (a leading "the", a trailing "day" aside)."""
    assert isinstance(text, str), "text must be a str"
    assert len(text) <= _MAX_ASK, "text bounded"
    key = tuple(w for w, _, _ in words(text.replace("'", "").replace("’", "")))
    key = key[1:] if key[:1] == ("the",) else key
    return _HOLIDAYS.get(key) or (_HOLIDAYS.get(key[:-1]) if key[-1:] == ("day",) else None)


def _holiday_day(name: str, year: int) -> dt.date | None:
    """Rule 4: the named holiday's day in ``year`` — the closed rule for a name the US calendar lacks
    (dateutil's Easter, a fixed day, the nth Sunday of a month), else the US calendar's day of that name."""
    assert name in _HOLIDAYS.values(), "a closed holiday name"
    assert year in _YEARS, "a written year"
    # lazy: the app's import-time discipline
    import holidays
    from dateutil.easter import easter
    from dateutil.relativedelta import SU, relativedelta

    if name == "easter":
        return easter(year)
    if name in _FIXED_DAYS:
        return dt.date(year, *_FIXED_DAYS[name])
    if name in _NTH_SUNDAYS:
        month, nth = _NTH_SUNDAYS[name]
        return dt.date(year, month, 1) + relativedelta(weekday=SU(nth))
    calendar = holidays.country_holidays("US", years=[year])
    return next((day for day, named in sorted(calendar.items()) if named.lower() == name.lower()), None)


def _named_day(name: str, year: int | None, today: dt.date) -> dt.date | None:
    """Rule 4: a named holiday's day in the asked year, else its next occurrence from today."""
    assert year is None or isinstance(year, int), "year must be an int"
    assert isinstance(today, dt.date), "today must be a date"
    if year is not None:
        return _holiday_day(name, year)
    day = _holiday_day(name, today.year)
    return day if day is not None and day >= today else _holiday_day(name, today.year + 1)


def _asked_year(ms_raw: list[dict]) -> int | None:
    """Rule 4: the first year ms reads as a stretch of its own ("2027" in "in 2027", "next year")."""
    assert isinstance(ms_raw, list), "a raw span list"
    assert len(ms_raw) <= _MAX_SPANS, "raw spans bounded"
    for m in ms_raw:  # bounded by _MAX_SPANS
        values = list((m.get("resolution") or {}).get("values") or [])
        timex = str(values[0].get("timex") or "") if values and isinstance(values[0], dict) else ""
        if m["type"] == "daterange" and timex.isdigit() and len(timex) == 4 and int(timex) in _YEARS:
            return int(timex)
    return None


def _holiday(ask: str, raw: dict, now: dt.datetime, asked: int | None) -> list[dict] | None:
    """Rule 4: an ms date span that is a holiday → [its day, the year phrase ms read into it] (either may
    be absent); None when the span is no holiday (``_from_ms`` reads it)."""
    assert isinstance(raw, dict) and "type" in raw, "an ms raw span"
    assert now.tzinfo is not None, "now must be aware"
    values = [v for v in list((raw.get("resolution") or {}).get("values") or [])[:8] if isinstance(v, dict)]
    timex = str(values[0].get("timex") or "") if values else ""
    floating = timex[4:5] == "-" and timex[7:12] == "-WXX-"
    if raw["type"] != "date" or any(v.get("Mod") for v in values) or not (
            floating or _holiday_named(ask[raw["start"]:raw["end"]])):
        return None
    ws = [w for w in words(ask[:raw["end"]]) if w[1] >= raw["start"]]
    name_end, year_at, own = _year_phrase(ws, now.year)
    assert 0 < name_end <= len(ws), "a holiday span keeps its name before its year"
    name, year = _holiday_name(ask[raw["start"]:ws[name_end - 1][2]]), own if own is not None else asked
    if name is not None:
        day = _named_day(name, year, now.date())
    else:  # "Christmas Eve", "Labor Day weekend": ms's own day, none outside the asked year
        at = _local((_ms_value(values, now.date()) or {}).get("value"), now.tzinfo)
        day = at.date() if at is not None and (year is None or at.year == year) else None
    out = []
    if day is not None:
        lo = dt.datetime.combine(day, dt.time(0), tzinfo=now.tzinfo)
        out.append(_span(ask, {"start": raw["start"], "end": ws[name_end - 1][2]}, "holidays" if name else "ms",
                         kind="holiday", lo=lo, hi=lo + dt.timedelta(days=1), grain="day",
                         token=f"date:{day.isoformat()}"))
    if own is not None:  # the year phrase ms read into the span: the stretch, a span of its own
        lo = dt.datetime(own, 1, 1, tzinfo=now.tzinfo)
        out.append(_span(ask, {"start": ws[year_at][1], "end": raw["end"]}, "ms", kind="range", lo=lo,
                         hi=lo.replace(year=own + 1), grain="year", token=f"year:{own}"))
    return out


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


def _merge_time(ask: str, ms_raw: list, now: dt.datetime) -> list[dict]:
    """Rules 3-5: every kept ms span stands on its own; rule 4 dates a holiday, ms resolves the rest."""
    assert isinstance(ms_raw, list), "a raw span list"
    assert now.tzinfo is not None, "now must be aware"
    asked = _asked_year(ms_raw)
    out: list[dict] = []
    for m in ms_raw:  # bounded by _MAX_SPANS
        spans = _holiday(ask, m, now, asked)
        if spans is None:
            span = _from_ms(ask, m, now)
            spans = [] if span is None else [span]
        out += spans
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
        prior = [w for w in ws if w[2] <= n["start"]]
        before = prior[-4:]
        hit = None
        for gap in (0, 1):
            stop = len(before) - gap
            for size in (2, 1):
                phrase = " ".join(w[0] for w in before[stop - size:stop]) if stop - size >= 0 else ""
                if hit is None and phrase in table:
                    lead = len(prior) - len(before) + stop - size   # the phrase's first word, in `prior`
                    quantity = [before[-1][0]] if gap else []
                    hit = (table[phrase], before[stop - size][1], quantity + [w[0] for w in prior[lead - 1:lead]])
        if hit is not None:
            out.append({"text": ask[hit[1]:n["end"]], "start": hit[1], "end": n["end"], "op": hit[0],
                        "value": n["value"], "unit": n["unit"], "quantity": hit[2]})
    return out


def _now_phrases(ask: str, now: dt.datetime, masked: tuple) -> list[dict]:
    """Rule 10: the closed now-phrase table as point spans with token ``now`` (word-boundary runs of the
    ask's own words, masked like every span; "rn" / "atm" only as the last word)."""
    assert now.tzinfo is not None, "now must be aware"
    assert isinstance(masked, tuple), "masked must be a tuple"
    ws = words(ask)[:_MAX_WORDS]
    out: list[dict] = []
    for phrase in _NOW_PHRASES:  # bounded table
        n = len(phrase)
        for i in range(len(ws) - n + 1):  # bounded by the ask's words
            if tuple(w for w, _, _ in ws[i:i + n]) != phrase or (phrase in _LAST_WORD_ONLY and i + n != len(ws)):
                continue
            kept = _kept(ask, ws[i][1], ws[i + n - 1][2], masked)
            span = None if kept is None else {"start": kept[0], "end": kept[1]}
            if span is not None and not any(_overlaps(span, o) for o in out):
                out.append({"text": ask[kept[0]:kept[1]], **span, "kind": "point", "from": _iso(now),
                            "to": _iso(now), "grain": "second", "lib": "table", "token": "now"})
    return out


def name_masks(ask: str, names: list | tuple) -> list[tuple[int, int]]:
    """The ask's character ranges of the proper names the intent copied from it (module docstring,
    Names): every whole-word occurrence, case-insensitive; a name the holiday table names is left
    readable."""
    assert isinstance(ask, str), "ask must be a str"
    assert isinstance(names, (list, tuple)), "names must be a list"
    ws = words(ask[:_MAX_ASK])[:_MAX_WORDS]
    out: list[tuple[int, int]] = []
    for name in list(names)[:_MAX_NAMES]:  # bounded
        nw = [w for w, _, _ in words(str(name)[:200])][:12]
        if not nw or _holiday_named(str(name)):
            continue
        n = len(nw)
        out += [(ws[i][1], ws[i + n - 1][2]) for i in range(len(ws) - n + 1)  # bounded by the ask's words
                if [w for w, _, _ in ws[i:i + n]] == nw]
    return out[:_MAX_SPANS]


def recognize(ask: str, *, now: dt.datetime, zone: str, masked: list[tuple[int, int]] | tuple = ()) -> dict:
    """The ask's spans in the closed shape the module docstring states. ``now`` is the
    reference instant (aware); ``zone`` the card's IANA zone; ``masked`` the char ranges of
    parameter-consumed entities (a place, a team, a zip) and of the intent's proper names
    (``name_masks``) whose spans are dropped."""
    assert isinstance(ask, str) and ask.strip(), "ask required"
    assert isinstance(now, dt.datetime) and now.tzinfo is not None, "now must be an aware datetime"
    text = ask[:_MAX_ASK]
    local = now.astimezone(ZoneInfo(zone))
    mask = tuple((int(a), int(b)) for a, b in list(masked)[:_MAX_SPANS])
    time = _merge_time(text, _ms_time(text, local, mask), local)
    table = _now_phrases(text, local, mask)
    time = sorted([s for s in time if not any(_overlaps(s, n) for n in table)] + table,
                  key=lambda s: (s["start"], s["end"]))[:_MAX_SPANS]
    numbers = _numbers(text, mask, time)
    return {"time": time, "numbers": numbers, "ordinals": _ordinals(text, mask, time),
            "comparators": _comparators(text, numbers)}
