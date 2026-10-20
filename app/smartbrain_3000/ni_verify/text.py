"""Token and vocabulary helpers the verify checks share (moved verbatim from ni_flow)."""

from __future__ import annotations

import re

from ..library_resolve import ENGLISH as LIBRARY_ENGLISH

_MAX_INTENT_FIELDS = 4


_MAX_INTENT_NAMES = 5


# day names → the engine's weekday tokens (``_window_words`` says a window back in the user's words)
_DAY_NAMES = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
              "saturday": "sat", "sunday": "sun"}


# names that hold a day word and are no window ("Saturday Night Live" is a show, "Black Friday" a sale): the
# verify step reads their words as a name (``_other_subject``)
_DAY_NAME_RE = re.compile(
    r"\b(?:saturday night live|(?:monday|thursday|sunday|friday) night (?:football|baseball|hockey|lights|raw|"
    r"smackdown)|black friday|cyber monday|super tuesday|good friday|fat tuesday|giving tuesday|ash wednesday|"
    r"holy (?:thursday|saturday)|small business saturday|super bowl sunday|taco tuesday)\b")


_ANSWER_MEASURES = ("temperature", "feels_like", "precip_chance", "precip_amount", "conditions", "thunderstorm",
                    "snow", "wind", "humidity", "waves", "swell", "wave_direction", "water_temp", "alerts", "kp",
                    "uv", "air_quality", "tide", "sunrise", "sunset")


# Library answers spec v1.5 (Round 20, Q3d): ``codes`` names the cell's code table — "wmo_weather" on a number (a
# weather code the engine's ``label`` step turns into words, ni.LABEL_TABLES), or on a text value / row / column cell
# a Library code table (taxonomy/codes/<name>.json, packed as ``library_codes``): the closed list of values the cell
# holds, read as a row filter's evidence (ni_query.ground rule 3), never a pipeline step
_NUMBER_CODES = ("wmo_weather",)


_MAX_VALUE_ANSWERS = 4


_ANSWER_STOP = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "for", "to", "and", "or", "is", "are", "was", "be", "by",
    "with", "from", "as", "it", "its", "this", "that", "what", "whats", "how", "when", "where", "who",
    "which", "my", "me", "i", "show", "get", "give", "tell", "will", "do", "does", "there", "please", "s",
})


def _answer_tokens(text: str) -> set[str]:
    """Lowercased words, fillers dropped, a simple plural folded to its singular."""
    out = set()
    for word in re.findall(r"[a-z0-9]+", str(text).lower()):  # bounded by the text
        if word in _ANSWER_STOP:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return out


def _folded_words(text: str) -> list[str]:
    """Every word in order (fillers kept), a simple plural folded as ``_answer_tokens`` folds it."""
    words = re.findall(r"[a-z0-9]+", str(text).lower())
    return [w[:-3] + "y" if len(w) > 4 and w.endswith("ies") else
            w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


def _answer_score(answer: dict, ask: set[str]) -> float:
    """How well an answer's words / label / name cover the ask: one point per ask word it names,
    plus half a point per multi-word phrase of its ``words`` said in full ("rain tomorrow")."""
    own: set[str] = set()
    for text in [*answer["words"], answer["label"], answer["name"].replace("_", " ")]:
        own |= _answer_tokens(text)
    phrases = sum(1 for w in answer["words"] if len(_answer_tokens(w)) > 1 and _answer_tokens(w) <= ask)
    return len(ask & own) + 0.5 * phrases


# The words that say WHEN, never WHICH answer (C7): with a window asked, they are taken out of the
# scoring — "tonight" used to pick whatever answer listed it, on any source.
_WINDOW_WORDS = frozenset({
    "today", "todays", "tonight", "tonite", "tomorrow", "tmrw", "tmr", "weekend", "now", "right", "currently",
    "current", "morning", "afternoon", "evening", "overnight", "later", "week", "day", "hour", "next", "this",
    "coming", "upcoming", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"})


# the quantity an ask names (C7), as a closed ``measure`` of the v1.2 answers; a declared answer covers
# it by its own ``measure`` or by its label / cell labels naming it
_MEASURE_CUES: dict[str, re.Pattern] = {m: re.compile(rx, re.IGNORECASE) for m, rx in (
    ("water_temp", r"\b(water|sea|ocean|lake) temp(erature)?s?\b|\bsst\b|\bhow warm is the water\b"),
    ("temperature", r"\b(temperatures?|temps?|degrees)\b"),
    ("wind", r"\b(wind|winds|windy|gusts?|gusty)\b"),
    ("humidity", r"\b(humid|humidity|muggy)\b"),
    ("uv", r"\buv\b"),
    ("waves", r"\b(waves?|surf)\b"),
    ("swell", r"\bswells?\b"),
    ("precip_chance", r"\b(rain|rainy|precip|precipitation|umbrella|showers?)\b"),
    ("snow", r"\b(snow|snowfall|flurries)\b"),
    ("thunderstorm", r"\b(thunderstorms?|t-?storms?|lightning)\b"),
    ("air_quality", r"\b(air quality|aqi)\b"),
    ("kp", r"\bkp\b"),
    ("tide", r"\btides?\b"),
    ("sunrise", r"\bsunrise\b"),
    ("sunset", r"\bsunset\b"),
)}


def _covers_measure(a: dict, measure: str) -> bool:
    """Does this answer report ``measure``? Its declared measure, a weather-code table for the sky's own
    events (a thunderstorm, snow — ``wmo_weather`` only: a text cell's table, the NWS event names, reports
    no sky), or a label / cell label that names it."""
    if a.get("measure") == measure:
        return True
    coded = a.get("codes") in _NUMBER_CODES or any(c.get("codes") in _NUMBER_CODES for c in a.get("cells") or [])
    if measure in ("thunderstorm", "snow") and (a.get("measure") == "conditions" or coded):
        return True
    labels = [a["label"], *[c.get("label", "") for c in a.get("cells") or []]]
    return any(_MEASURE_CUES[measure].search(t) for t in labels)


def _v12(answers: list[dict]) -> bool:
    """Does this source declare what its answers deliver (spec v1.2)? An older record's answers can't say
    which window they hold — ``_legacy_window_gap`` reads their labels instead."""
    return any(a.get("window") or a.get("axis") or a.get("measure") for a in answers)


def _event_time(a: dict) -> bool:
    """A time an event happens at (not an observation's own "as of", which is window now)."""
    return a.get("type") in ("time", "date") and a.get("window") != "now"


def _window_words(window: str) -> str:
    """The asked window in the user's words ("the weekend", "Saturday", "the next 3 days")."""
    if window.startswith("dow:"):
        return {v: k for k, v in _DAY_NAMES.items()}[window[4:]].capitalize()
    if window.startswith(("next_days:", "next_hours:")):
        unit, n = window.split(":")
        return f"the next {n} {unit[5:]}"
    return {"now": "right now", "weekend": "the weekend"}.get(window, window)


# words too general to say what a source is about ("gas PRICES", "kp INDEX", "the FORECAST")
_GENERIC_SUBJECT = frozenset({"price", "rate", "index", "level", "value", "status", "data", "info", "information",
                              "number", "count", "forecast", "report", "latest", "update", "chance", "amount",
                              "total", "list", "time", "today", "live", "new", "top"})


# what a result card must carry (a score, who won)
_SCORE_WORDS = frozenset({"score", "point", "pts", "run", "goal", "result", "final", "won", "winner", "win",
                          "loss", "lost"})


# the parameters that carry a place into an address
_GEO_PARAMS = frozenset({"lat", "lon", "latitude", "longitude", "zip", "place", "city", "station", "point",
                         "office", "grid_x", "grid_y", "gauge", "buoy", "site", "county", "state"})


# taxonomy ``expects`` components that are not measures: the words that report them
_COMPONENT_WORDS: dict[str, frozenset[str]] = {
    "wave_period": frozenset({"period"}), "score": _SCORE_WORDS,
    "opponent": frozenset({"opponent", "vs", "versus", "away", "home", "matchup", "game", "match"}),
    "venue": frozenset({"venue", "stadium", "arena", "field", "where"}),
    "rank": frozenset({"rank", "standing", "position", "place", "seed", "gb"}),
    "record": frozenset({"record", "w", "l", "win", "loss", "wins", "losses", "pct"}),
}


def _subcategory(lib: object, sub: str) -> dict:
    """A taxonomy subcategory's ``kinds`` / ``policy`` / ``expects`` from the Library's ``subcategory()``
    ({} when unknown, or for an older Library without it)."""
    fn = getattr(lib, "subcategory", None)
    if not callable(fn):
        return {}
    try:
        got = fn(sub)
    except Exception:  # a broken Library: the frame has no category facts, never a crash
        return {}
    return got if isinstance(got, dict) else {}


def _entity_vocabulary(lib: object, entity: str) -> set[str]:
    """Tokens the ``entity``'s domain names (team / league aliases for a sports league entity),
    folded as ``_answer_tokens`` folds them (plurals → singular, filler stop) so the subtraction
    reads the same vocabulary the proper-noun check does. {} when the Library has no such method
    or the entity isn't a league. The fix6-rows F7-C class fix (2026-10-04) subtracts these from
    the proper-noun check so a single-entity league source still accepts a team ask it covers
    while refusing a foreign conference ask."""
    assert isinstance(entity, str), "entity must be a string"
    getter = getattr(lib, "entity_vocabulary", None)
    if not callable(getter) or not entity:
        return set()
    try:
        got = getter(entity)
    except Exception:  # a broken Library: the proper check falls back to no league subtraction
        return set()
    if not isinstance(got, set):
        return set()
    folded: set[str] = set()
    for word in got:  # bounded by the league's team / sport_league aliases
        folded |= _answer_tokens(str(word))
    return folded


def _request_proper(request: str, intent_names: list[str] | tuple = ()) -> set[str]:
    """Proper-noun tokens in the RAW request (never the model's subject casing). Three disjoint
    sources: (a) the validated intent ``names`` — the model's narrow closed blank (named topics,
    2026-10-04); (b) capitalized tokens NOT at a sentence start — mid-sentence caps are always
    proper; (c) all-caps acronyms of two or more letters that aren't a stop word — "FDA", "SEC",
    "TSA" survive even sentence-initial.

    A sentence-initial capitalized word is proper ONLY when the intent named it: phones auto-
    capitalize the start of every ask ("Biggest earthquakes today"), and without a corroborating
    name from the model that cap is no signal on its own. With no intent names (no model, or a
    hallucinated name was dropped), sentence-initial capitals are not names."""
    assert isinstance(request, str), "request must be a string"
    assert isinstance(intent_names, (list, tuple)), "intent_names must be a list"
    text = _amp(request)
    named = _answer_tokens(_amp(" ".join(str(n) for n in intent_names)))
    out: set[str] = set(named)
    # the start of the ask and anything after a sentence-ending . ! ? begins a sentence; the first
    # [A-Za-z0-9]+ word that follows is the sentence-initial one.
    for sentence in re.split(r"[.!?]+\s*", text):  # bounded by the ask length
        words = re.findall(r"[A-Za-z0-9]+", sentence)
        for idx, word in enumerate(words):  # bounded by the sentence length
            if not (word[0].isupper() or any(ch.isdigit() for ch in word)):
                continue
            folded = _answer_tokens(word)
            if word.isupper() and len(word) >= 2 and word.isalpha() and not folded <= _NAME_STOP:
                out |= folded  # acronym (c): "FDA", "SEC", "TSA" — never a stop word
                continue
            if idx == 0 and not (folded <= named):
                continue  # (a) sentence-initial caps are proper only when the intent named them
            out |= folded  # (b) mid-sentence cap
    return out


# words that say what KIND of data a source gives (its name's "service alerts", "latest version") or fill an ask
# out ("did", "doing"), never which subject it is about
_KIND_WORDS = frozenset(_answer_tokens(
    "service services alert alerts advisory advisories version versions release releases schedule schedules "
    "standings update updates outage incident average daily close observations result game games per spot all "
    "items trains system official api feed did doing done having they their them get got can could should would "
    "yesterday last night ago past going gonna"))


_NAME_STOP = _ANSWER_STOP | _GENERIC_SUBJECT | _WINDOW_WORDS | _SCORE_WORDS | _KIND_WORDS | _answer_tokens(
    " ".join(LIBRARY_ENGLISH))


def _amp(text: str) -> str:
    """"S&P" is one name ("sp"), not the letters s and p."""
    return re.sub(r"(?<=[A-Za-z])&(?=[A-Za-z])", "", str(text or ""))


def _proper_tokens(text: str) -> set[str]:
    """The words of a record's declared entity that name something (capitalized, an acronym, a number:
    "CTA", "Chicago", "Win 4"), not the lowercase words that describe it ("trains", "versions")."""
    words = re.findall(r"[A-Za-z0-9]+", _amp(text))
    return _answer_tokens(" ".join(w for w in words if w[0].isupper() or any(ch.isdigit() for ch in w)))


def _names_hit(said: set[str], names: set[str]) -> bool:
    """Does a word the user said name one of ``names``? Whole words, or a short form the name begins
    ("fed" → "federal", "gas" → "gasoline")."""
    return any(a == n or (len(a) >= 3 and len(n) >= 3 and (a.startswith(n) or n.startswith(a)))
               for a in said for n in names)


def _kind_tokens(lib: object, cats: list[str]) -> set[str]:
    """The words of the taxonomy keywords of the subcategories a source is filed under (its kind)."""
    try:
        taxonomy = lib.taxonomy() if lib is not None else []
    except Exception:  # an older or broken Library: no kind facts
        return set()
    out: set[str] = set()
    for cat in taxonomy:  # bounded by the taxonomy
        for sub in cat.get("subcategories") or []:
            if f"{cat.get('id')}/{sub.get('id')}" in cats:
                for kw in sub.get("keywords") or []:
                    out |= _answer_tokens(_amp(kw))
    return out


def _own_words(source: dict, answers: list[dict]) -> set[str]:
    """Every word the source uses about itself: name, provider, description, entity, examples, answers."""
    texts = [str(source.get("name") or ""), str(source.get("provider") or ""), str(source.get("description") or ""),
             str((source.get("coverage") or {}).get("entity") or ""), *(str(x) for x in source.get("examples") or [])]
    for a in answers:  # bounded by _MAX_ANSWERS
        texts += [*a["words"], a["label"], a["name"].replace("_", " "), *[c.get("label", "") for c in a.get("cells") or []]]
    return _answer_tokens(_amp(" ".join(texts)))


def _subject_names(source: dict, kind: set[str]) -> set[str]:
    """What an entity-specific source is called: the naming words of its declared entity, its name, and the
    words its example asks use for it ("baseball" for MLB, "jobs" for payrolls, the CTA's "red line") that
    aren't the words of its kind ("delays")."""
    cov = source.get("coverage") or {}
    base = _proper_tokens(str(cov.get("entity") or "")) | _answer_tokens(_amp(str(source.get("name") or "")))
    alias = _answer_tokens(_amp(" ".join(str(x) for x in source.get("examples") or [])))
    names = base | (alias - kind)
    return {n for n in names - _NAME_STOP if len(n) >= 2 or n.isdigit()}


def _single_keywords(lib: object, cats: list[str]) -> set[str]:
    """The one-word taxonomy keywords of the subcategories a source is filed under ("wmata", "nasdaq"), less
    the words of those subcategories' own names ("Inflation & CPI")."""
    try:
        taxonomy = lib.taxonomy() if lib is not None else []
    except Exception:  # an older or broken Library: no keyword facts
        return set()
    subs = [sub for cat in taxonomy for sub in cat.get("subcategories") or []
            if f"{cat.get('id')}/{sub.get('id')}" in cats]
    named = _answer_tokens(_amp(" ".join(str(sub.get("label") or "") for sub in subs)))
    return {tok for sub in subs for kw in sub.get("keywords") or [] if len(kw.split()) == 1
            for tok in _answer_tokens(_amp(kw))} - named


# fix8 (blind-7, 2026-10-04): "Axios Denver latest" shipped a Rocky Mountain
# Voice page (that only mentioned Axios Denver); "Fox News headlines" is the
# same class but is caught via the Library's providers_named_in (fix7-lib).
# A page card for an ask that NAMES an outlet must be from THAT outlet's own
# site. The signal is: pack providers_named_in + outlet-shaped intent.names
# (ends in a publisher word, or is one of a bounded well-known-outlet set
# the pack hasn't indexed yet).
_OUTLET_SUFFIX_WORDS: frozenset[str] = frozenset({
    "news", "times", "post", "journal", "herald", "tribune", "chronicle",
    "gazette", "daily", "weekly", "observer", "dispatch", "sentinel",
    "press", "mail", "report", "digest", "media", "channel", "network",
    "radio", "wire",
})


_KNOWN_OUTLET_WORDS: frozenset[str] = frozenset({
    "axios", "reuters", "bloomberg", "politico", "vox", "vice", "verge",
    "economist", "forbes", "cnn", "cbs", "nbc", "abc", "msnbc", "npr", "pbs",
    "huffpost", "cnet", "techcrunch", "engadget", "wired", "nyt", "wsj",
    "newsweek", "slate", "salon",
})
