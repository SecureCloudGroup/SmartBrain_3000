"""What a question asks for, read from its words alone (pure: no model, no Library, no clock).

``parse_ask`` turns the request into the constraints the four verify classes check a built result
against (``classes``): a time window, a numeric threshold, N compared entities, and a count over an
area wider than a point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text import _DAY_NAME_RE, _WINDOW_WORDS


@dataclass(frozen=True)
class Ask:
    """The constraints one request states. Every field is empty when the words say nothing."""

    window: str | None = None                       # today | tonight | tomorrow | yesterday | last_night | weekend
    threshold: tuple[str, float] | None = None      # (">" | ">=" | "<" | "<=", number)
    entities: tuple[str, ...] = ()                  # the things compared (two or more)
    area: str | None = None                         # basin | national | world | region
    counting: bool = False                          # asks "how many" / "number of" / "total"
    words: frozenset[str] = frozenset()             # lower-cased words of the request


_WINDOWS: tuple[tuple[str, re.Pattern], ...] = tuple((name, re.compile(rx)) for name, rx in (
    ("tonight", r"\btonight(?:'s)?\b|\btonite\b|\bthis (?:evening|night)\b"),
    ("last_night", r"\blast night(?:'s)?\b"),
    ("yesterday", r"\byesterday(?:'s)?\b"),
    ("tomorrow", r"\btomorrow(?:'s)?\b"),
    ("weekend", r"(?<!next )\bweekend\b"),
    ("today", r"\btoday(?:'s)?\b"),
))

_ABOVE = {"above": ">", "over": ">", "exceeding": ">", "exceeds": ">", "exceed": ">", "greater than": ">",
          "more than": ">", "higher than": ">", "at least": ">=", "no less than": ">=", "minimum of": ">="}
_BELOW = {"below": "<", "under": "<", "less than": "<", "lower than": "<", "fewer than": "<",
          "at most": "<=", "no more than": "<=", "maximum of": "<="}
_OPS = {**_ABOVE, **_BELOW}
_THRESHOLD_RE = re.compile(
    r"\b(?P<op>" + "|".join(sorted((re.escape(k) for k in _OPS), key=len, reverse=True)) + r")\s+"
    r"(?:a\s+|an\s+)?(?:magnitude\s+|mag\s+|m\s?)?\$?(?P<n>-?\d+(?:\.\d+)?)(?![\d.])"
    r"(?!\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|seconds?|miles?|km|kilometers?)\b)")
_PLUS_RE = re.compile(r"(?<![\w.])m?(?P<n>\d+(?:\.\d+)?)\s*\+")

_VS_RE = re.compile(r"\s(?:vs\.?|versus|v\.|against|compared (?:to|with))\s")
_COMPARE_RE = re.compile(r"\b(?:compare|comparison of|difference between|differences between)\s+(.+)")
# every whitespace-led branch starts only at the head of a whitespace run, so a long run is scanned once (linear)
_LIST_SPLIT_RE = re.compile(r",\s*(?:and\s+)?|(?<!\s)\s+and\s+|(?<!\s)\s*&\s*|(?<!\s)\s+(?:with|to)\s+")
_ENTITY_FILL = frozenset({
    "weather", "forecast", "price", "prices", "temperature", "temperatures", "temp", "temps", "stats", "stat",
    "score", "scores", "comparison", "compare", "the", "a", "an", "how", "is", "are", "what", "whats", "current",
    "currently", "latest", "live", "show", "me", "of", "and", "which", "better", "warmer", "colder", "cooler",
    "hotter", "cost", "costs", "rate", "rates", "standings", "record", "records", "news", "difference",
    # what kind of thing the compared names are ("Nvidia and AMD stock prices"): context, not part of a name
    "stock", "stocks", "share", "shares", "coin", "coins", "crypto", "cryptocurrency", "token", "tokens",
    "downloads", "download", "views", "followers", "population", "exchange"}) | _WINDOW_WORDS

_COUNTING_RE = re.compile(r"\bhow many\b|\bnumber of\b|\bcounts? of\b|\btotal\b|\bhow much total\b")
_AREAS: tuple[tuple[str, re.Pattern], ...] = tuple((name, re.compile(rx)) for name, rx in (
    ("basin", r"\b(?:pacific|atlantic|indian ocean|arctic|caribbean|mediterranean|gulf of mexico|gulf of alaska"
              r"|north sea|bering sea)\b(?!\s+(?:time|standard|daylight|ave|avenue|st|street|heights|city|grove"
              r"|palisades|beach|grove|bell|rim|county))"),
    ("national", r"\b(?:nationwide|nationally|national|countrywide|across (?:the )?(?:us|u\.s\.|usa|country|nation"
                 r"|united states)|in (?:the )?(?:us|u\.s\.|usa|united states|america|country))\b"),
    ("world", r"\b(?:worldwide|globally|global|in the world|around the world|across the (?:world|globe)"
              r"|on earth)\b"),
    ("region", r"\b(?:midwest|northeast|southeast|southwest|northwest|west coast|east coast|gulf coast"
               r"|great lakes|new england|great plains|europe|asia|africa|south america|north america"
               r"|middle east)\b"),
))


def parse_window(text: str) -> str | None:
    """The one window the words name ("tonight", "today", "this weekend"), else None — two different
    windows in one ask ("today and tomorrow") are not read. A name that holds a day word ("Saturday
    Night Live") is masked first."""
    assert isinstance(text, str), "text must be a string"
    low = _DAY_NAME_RE.sub(" ", text.lower())
    found = [name for name, rx in _WINDOWS if rx.search(low)]
    assert len(found) <= len(_WINDOWS), "one hit per window at most"
    return found[0] if len(found) == 1 else None


def parse_threshold(text: str) -> tuple[str, float] | None:
    """(operator, number) for "above 4.0", "at least 5", "under 10", "M4.5+"; None when the words state no
    bound. The first bound stated wins."""
    assert isinstance(text, str), "text must be a string"
    low = text.lower()
    m = _THRESHOLD_RE.search(low)
    if m is not None:
        return _OPS[m.group("op")], float(m.group("n"))
    p = _PLUS_RE.search(low)
    out = (">=", float(p.group("n"))) if p is not None else None
    assert out is None or out[0] in (">=", ">"), "a plus bound is a floor"
    return out


def _entity(piece: str) -> str:
    """One compared thing from a piece of the ask: what follows its last " in " / " for " / " at ", less the
    filler words ("weather", "price", "today") at either end."""
    assert isinstance(piece, str), "piece must be a string"
    text = piece.strip(" ?!.,")
    for lead in (" in ", " for ", " at "):
        if lead in f" {text} ":
            text = f" {text} ".rsplit(lead, 1)[1]
    tokens = text.split()
    while tokens and tokens[0].strip(".,") in _ENTITY_FILL:
        tokens.pop(0)
    while tokens and tokens[-1].strip(".,") in _ENTITY_FILL:
        tokens.pop()
    out = " ".join(tokens).strip(" ?!.,")
    assert len(out) <= len(piece), "cleaning only removes words"
    return out if re.search(r"[a-z]", out) else ""


def parse_entities(text: str) -> tuple[str, ...]:
    """The things a compare ask names ("Denver vs Salt Lake City", "compare Ethereum and Solana prices"):
    two or more, or () when the words are not a comparison."""
    assert isinstance(text, str), "text must be a string"
    low = f" {' '.join(text.lower().split()).strip(' ?!.')} "
    pieces: list[str] = []
    if _VS_RE.search(low):
        pieces = _VS_RE.split(low)
    else:
        m = _COMPARE_RE.search(low)
        if m is not None:
            pieces = _LIST_SPLIT_RE.split(m.group(1))
    names = list(dict.fromkeys(e for e in (_entity(p) for p in pieces[:6]) if e))
    assert len(names) <= 6, "bounded by the pieces"
    return tuple(names) if len(names) >= 2 else ()


def parse_area(text: str) -> str | None:
    """The reach the words name when it is wider than a point: a basin ("in the Pacific"), the nation, the
    world or a region; None for a place, a state or no reach at all."""
    assert isinstance(text, str), "text must be a string"
    low = text.lower()
    for name, rx in _AREAS:
        if rx.search(low):
            return name
    return None


def parse_ask(request: str) -> Ask:
    """Every constraint the request's words state (pure)."""
    assert isinstance(request, str), "request must be a string"
    low = request.lower()
    return Ask(window=parse_window(request), threshold=parse_threshold(request),
               entities=parse_entities(request), area=parse_area(request),
               counting=_COUNTING_RE.search(low) is not None,
               words=frozenset(re.findall(r"[a-z0-9]+", low)))
