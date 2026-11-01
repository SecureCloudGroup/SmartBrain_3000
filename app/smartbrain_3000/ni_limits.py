"""A source's free request limits against how often a card refreshes (2026-10-31, T: "Prefer a source that fits").

"NVDA stock price every 30 minutes" is 48 requests a day. Finnhub's free key allows 60 a minute and covers it.
Alpha Vantage's allows 25 a day, so a card built on it stops updating from about midday each day. Until the Library
pack v1.6.0 the limits were prose in each record's ``terms.note``, which no code read, and the auto-pick tried the
Alpha Vantage row first. The pack now carries ``access.limits``: 1 to 4 windows of ``{requests, seconds}``, per key
for a keyed source and per address for a keyless one. A build tries the rows whose limits cover the card's cadence
first (``ni_flow._auto_pick``), and a card on a source whose limit it overruns says so (``line``): at Keep, and while
the host holds SmartBrain off.

A card refreshing every ``m`` minutes makes at most ceil(W / 60m) requests in any window of W seconds (a Library row
is one fetch a refresh). Third-party hosts are never refreshed faster than the politeness floor
(``ni_polite.CADENCE_FLOOR_MINUTES``), so the cadence that counts is the larger of the two (``effective_minutes``).
A pack field is data: a malformed one reads as unknown, never as a limit. Unknown counts as covering, since most
providers document no number. The rule counts this card only. Cards on one key or address share its limit, and when
they go over together the host's own "slow down" pauses it (``ni_polite``), which the card then says. Pure code: no
store, no network.
"""
from __future__ import annotations

import math

MAX_WINDOWS = 4
MAX_REQUESTS = 10_000_000
MAX_SECONDS = 31 * 86400          # the longest month: a monthly quota is written as 31 days
MAX_PROVIDER = 120
# how a window reads after "N requests": the common ones by name, the rest as "every ..."
_PER = {1: "a second", 60: "a minute", 3600: "an hour", 86400: "a day", 7 * 86400: "a week", MAX_SECONDS: "a month"}
_EACH = {60: "minute", 3600: "hour", 86400: "day", 7 * 86400: "week", MAX_SECONDS: "month"}
_UNITS = ((86400, "day"), (3600, "hour"), (60, "minute"))


def _whole(value: object, top: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= top


def clean(limits: object) -> list[dict]:
    """A record's ``access.limits`` as ``[{requests, seconds}]``, shortest window first; ``[]`` when there are none or
    the field is malformed. All or nothing: one bad window makes the whole field unknown."""
    if not isinstance(limits, list) or not 1 <= len(limits) <= MAX_WINDOWS:
        return []
    out: list[dict] = []
    for window in limits:  # bounded: at most MAX_WINDOWS
        if not (isinstance(window, dict) and set(window) == {"requests", "seconds"}
                and _whole(window["requests"], MAX_REQUESTS) and _whole(window["seconds"], MAX_SECONDS)):
            return []
        if any(w["seconds"] == window["seconds"] for w in out):
            return []
        out.append({"requests": window["requests"], "seconds": window["seconds"]})
    assert 1 <= len(out) <= MAX_WINDOWS, "a known limit has 1 to 4 windows"
    return sorted(out, key=lambda w: w["seconds"])


def effective_minutes(cadence_minutes: int, floor_minutes: int) -> int:
    """The cadence a card really refreshes at: its own, held to its host's floor."""
    assert isinstance(cadence_minutes, int) and cadence_minutes >= 1, "a cadence in minutes"
    assert isinstance(floor_minutes, int) and floor_minutes >= 0, "a floor in minutes"
    return max(cadence_minutes, floor_minutes)


def need(seconds: int, cadence_minutes: int) -> int:
    """The requests a card refreshing every ``cadence_minutes`` makes in any window of ``seconds``."""
    assert _whole(seconds, MAX_SECONDS), "a window in seconds"
    assert isinstance(cadence_minutes, int) and cadence_minutes >= 1, "a cadence in minutes"
    return math.ceil(seconds / (cadence_minutes * 60))


def overrun(windows: list[dict], cadence_minutes: int) -> dict | None:
    """The window the cadence overruns most, as ``{requests, seconds, need}``; None when every window covers it, or
    no window is known."""
    assert isinstance(windows, list) and len(windows) <= MAX_WINDOWS, "clean windows"
    over = [{**w, "need": need(w["seconds"], cadence_minutes)} for w in windows]
    over = [w for w in over if w["need"] > w["requests"]]
    return max(over, key=lambda w: (w["need"] / w["requests"], w["seconds"])) if over else None


def fits(windows: list[dict], cadence_minutes: int) -> bool | None:
    """True when every window covers the cadence, False when one does not, None when no window is known."""
    if not windows:
        return None
    return overrun(windows, cadence_minutes) is None


def block(provider: object, keyed: object, windows: object) -> dict | None:
    """What a build seals about its source's limits (``spec._built_from.limits``), or None when none is known."""
    cleaned = clean(windows)
    if not cleaned:
        return None
    return {"provider": " ".join(str(provider or "").split())[:MAX_PROVIDER], "keyed": bool(keyed),
            "windows": cleaned}


def valid_block(value: object) -> bool:
    """True for exactly what ``block`` seals."""
    return (isinstance(value, dict) and set(value) == {"provider", "keyed", "windows"}
            and isinstance(value["provider"], str) and len(value["provider"]) <= MAX_PROVIDER
            and isinstance(value["keyed"], bool)
            and isinstance(value["windows"], list) and bool(value["windows"])
            and clean(value["windows"]) == value["windows"])


def _per(seconds: int) -> str:
    if seconds in _PER:
        return _PER[seconds]
    for size, unit in _UNITS:  # bounded: three units
        if seconds % size == 0:
            return f"every {seconds // size} {unit}s"
    return f"every {seconds} seconds"


def _every(minutes: int) -> str:
    assert minutes >= 1, "a cadence"
    for size, unit in ((1440, "day"), (60, "hour"), (1, "minute")):  # bounded: three units
        if minutes % size == 0:
            n = minutes // size
            return f"every {unit}" if n == 1 else f"every {n} {unit}s"
    return f"every {minutes} minutes"  # unreachable: every minute count divides by 1


def _requests(n: int) -> str:
    return "1 request" if n == 1 else f"{n:,} requests"


def line(limits: object, cadence_minutes: int) -> str | None:
    """What a card says when its source's free limit doesn't cover how often it refreshes, else None:
    "Alpha Vantage's free key allows 25 requests a day. Refreshing every 30 minutes needs 48, so this card will stop
    updating partway through each day." ``limits`` is a sealed ``block``; ``cadence_minutes`` the effective one."""
    if not valid_block(limits):
        return None
    over = overrun(limits["windows"], cadence_minutes)
    if over is None:
        return None
    name = limits["provider"] or "This source"
    possessive = name + ("'" if name.endswith("s") else "'s")
    who = f"{possessive} free key allows" if limits["keyed"] else f"{name} allows"
    without = "" if limits["keyed"] else " without a key"
    each = _EACH.get(over["seconds"])
    tail = f"so this card will stop updating partway through each {each}" if each \
        else "so some of its refreshes will be refused"
    return (f"{who} {_requests(over['requests'])} {_per(over['seconds'])}{without}. Refreshing "
            f"{_every(cadence_minutes)} needs {over['need']:,}, {tail}.")
