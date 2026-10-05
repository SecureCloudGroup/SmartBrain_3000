"""Page-card verify gate (C9) — is a page READING data the page states?

The interpreted page tier ships whatever string a model read off a page, and
the field showed what that costs (blind run 2026-09-29): the page title as the
value ('Reddit'), a word the page never says ('Operational', a model slug
'feedback_collection'), a documentation heading as a status ('Service health'),
a glossary definition as current delays, a June user note as the "next"
eruption, one string for "top posts", a bare '49-18' as a result, a news
sentence as a forecast. Each is caught by code that can read the page — no
model involved:

* readable — the page isn't a challenge / JS shell / modal / binary
  (``pagegraph.readability``);
* chrome — a value is the page's own name (title, site name, its JSON-LD
  self-name), a label heading, or the want's own words;
* grounded — numbers appear exactly and word values' content words appear
  together in the page text (``ni.ground_values``, the engine's own rule);
* shape — a many-want ("top posts", standings, alerts) needs >= 3 rows;
* time — a next_event/schedule time parses (in the page's zone when it names
  one) to no earlier than now - 15 min and no later than now + 400 days;
* result — a result names both sides, both scores and a date;
* subject — the page mentions the ask's distinctive subject words;
* current — the value isn't grounded only inside a definition / FAQ / how-to
  section, isn't a normal ("annual snowfall"), isn't a list of past records
  for a "now" ask, and doesn't come from a dated news story.

``verify_page_reading`` returns reasons (empty = accept); ``has_evidence`` is
the pre-model check that skips a page whose readable text holds no want or
subject word at all.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo

from . import pagegraph
from .zones import zone_named

_GRACE = timedelta(minutes=15)
_HORIZON = timedelta(days=400)
_WINDOW_CHARS = 400        # word values: content words together within this span
_MANY_ROWS = 3
_MAX_SPANS = 200           # bound on occurrences examined per token
_MAX_QUOTE = 60            # a value quoted in a reason is cut to this

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SUBJECT_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "has", "have", "had", "its", "our", "your", "their", "into", "onto", "over",
    "than", "then", "but", "not", "any", "all", "per", "via", "out", "due",
    "will", "can", "may", "been", "being", "also", "just", "very", "some",
    "there", "here", "what", "when", "which", "who", "how", "about", "after",
    "before", "since", "until", "between", "including", "currently", "now",
})
# Subject words too generic to prove a page is ABOUT the ask's subject.
_GENERIC_SUBJECT: frozenset[str] = frozenset({
    "the", "and", "for", "status", "down", "weather", "news", "latest",
    "current", "today", "tonight", "now", "live", "score", "scores", "delays",
    "delay", "alerts", "alert", "price", "prices", "forecast", "next", "time",
    "times", "report", "reports", "update", "updates", "game", "games",
    "football", "basketball", "baseball", "soccer", "hockey", "line", "lines",
    "service", "services", "outage", "outages", "near", "local", "top", "best",
})
_TIME_WANT_WORDS: frozenset[str] = frozenset({
    "time", "times", "when", "date", "next", "start", "starts", "begin",
    "departure", "depart", "arrival", "arrive", "eta", "kickoff", "tipoff",
    "eruption", "launch", "pass", "sunrise", "sunset", "drawing", "draw",
})
# F6-ISS (blind-5): a next-event want whose referent is a punctual minute
# needs a clock — a date alone is not useful ("Saturday, Oct 10" as the next
# ISS pass). The narrow subset below triggers the clock-required refusal
# under next_event / schedule; the broader ``_TIME_WANT_WORDS`` continues to
# accept date-only readings on scheduled days (drawing, game, kickoff).
_CLOCK_REQUIRED_WORDS: frozenset[str] = frozenset({
    "pass", "flyover", "sunrise", "sunset", "time", "times", "eta",
})
# Page self-descriptions (the JSON-LD a site emits about itself).
_NEWS_TYPES: frozenset[str] = frozenset({
    "NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle",
    "OpinionNewsArticle", "BlogPosting", "LiveBlogPosting",
})
# List / collection entities whose ``name`` is a label FOR the list (not an
# item in it): a compiled card lifting it is picking a label, not a value.
_LIST_LABEL_TYPES: frozenset[str] = frozenset({
    "ItemList", "OfferCatalog", "Collection", "DataFeed", "DefinedTermSet",
    "CategoryCodeSet",
})
# A reading that reads as a current state (not a bare timestamp): the
# ``_stale_reasons`` rule keeps shipping these even when they name a past
# clock time ("Operational since 2026-09-27T10:00", a burn ban "As of 8/11/26,
# outdoor burning is prohibited").
_STATUS_WORDS: frozenset[str] = frozenset({
    "operational", "degraded", "down", "up", "normal", "healthy", "unhealthy",
    "available", "unavailable", "limited", "ongoing", "resolved", "running",
    "stopped", "halted", "active", "inactive", "online", "offline", "open",
    "closed", "prohibited", "allowed", "banned", "clear", "cleared",
    "elevated", "low", "high", "medium", "moderate", "warn", "warning",
    "advisory", "monitor", "incident", "outage", "yes", "no", "none",
    "delayed", "delay", "cancelled", "canceled", "diverted", "arrived",
    "landed", "departed", "early", "ontime", "scheduled",
})
_STALE_GRACE = timedelta(hours=36)
_FRESH_WINDOW = timedelta(hours=48)
_FRESH_PHRASE_RE = re.compile(
    r"\b(last updated|updated|as of|refreshed|just now|moments ago|"
    r"\d+\s*(minutes?|hours?)\s+ago)\b", re.IGNORECASE)
_TIME_FRAMES = ("next_event", "schedule")
_CURRENT_FRAMES = ("current_value", "status", "alerts", "count")
_NORMALS_FRAMES = ("current_value", "forecast")
_DEFINITION_HEADING_RE = re.compile(
    r"\b(how to (read|use|understand)|glossary|definitions?|what (is|are|does|do)"
    r"|faq|frequently asked|questions|explained|understanding|methodology"
    r"|about (this|the|us)|terms)\b", re.IGNORECASE)
_NORMALS_RE = re.compile(
    r"\b(annual|annually|per year|a year|yearly|on average|typically"
    r"|historically|all-time|climatology)\b", re.IGNORECASE)
_SLUG_RE = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
# fix8 (blind-7, 2026-10-04): a reading that is a link / download label — not a
# value. "2026 Schedule (PDF)" is an anchor the user hasn't followed; the same
# for "Download", "Click here", "Learn more", "View details", "Open PDF". The
# file-ext suffix catches the first class; the call-to-action set catches the
# bare label. Trailing/leading punctuation is normalized off before the match.
_FILE_EXT_SUFFIX_RE = re.compile(
    r"\(\s*(?:pdf|docx?|xlsx?|pptx?|csv|tsv|json|xml|rss|ics|zip|txt)\s*\)\s*$",
    re.IGNORECASE)
_CTA_LABEL_WORDS: frozenset[str] = frozenset({
    "download", "download pdf", "download the schedule", "download schedule",
    "download the full schedule", "download full schedule", "download now",
    "download here", "click here", "click", "tap here", "learn more",
    "read more", "see more", "view more", "view all", "view details",
    "view schedule", "view the schedule", "view full schedule", "see schedule",
    "see all", "see details", "open pdf", "open", "more info", "more",
    "get the schedule", "get schedule", "full schedule", "printable schedule",
    "printable version", "printable pdf", "see printable schedule",
})
_CLOCK_RE = re.compile(r"\d{1,2}:\d{2}|\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)(?!\w)|\bnoon\b|\bmidnight\b",
                       re.IGNORECASE)
# fix10 (blind-8, 2026-10-04): "lake tahoe water temp" shipped the interpreted reading "9.5" with
# no °C / °F — a physical-unit want needs a unit on its value. The required-word set lists wants
# that imply a unit (temperature, speed, height / level, pressure, distance, depth, snowfall);
# the exempt set keeps unitless indexes shipping (AQI, UV, Kp, pollen level, percentages).
_UNIT_REQUIRED_WORDS: frozenset[str] = frozenset({
    "temp", "temperature", "temperatures", "speed", "wind", "winds", "gust", "gusts",
    "height", "heights", "level", "levels", "depth", "depths", "pressure", "pressures",
    "distance", "distances", "snowfall", "rainfall",
})
_UNIT_EXEMPT_WORDS: frozenset[str] = frozenset({
    "aqi", "uv", "kp", "pollen", "index", "indexes", "indices", "percent", "percentage",
    "percentages", "score", "scores", "count", "counts", "rate", "rates", "status",
    "statuses", "ratio", "ratios",
})
_UNIT_LITERAL_RE = re.compile(
    r"°\s*[CFcf]|%|\b(?:ft|feet|foot|in|inch|inches|cm|mm|m|meter|meters|km|mi|mile|miles|"
    r"mph|kph|km/h|kmh|m/s|ms|kts|knots?|psi|bar|hpa|kpa|pa|hr|hour|hours)\b", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
           "nov", "dec")
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})\s+{_MONTH_RE}(?:\s+(\d{{4}}))?", re.IGNORECASE)
_MONTH_DAY_RE = re.compile(rf"\b{_MONTH_RE}\s+(\d{{1,2}})\b(?:,?\s+(\d{{4}}))?", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_WORD_RE = re.compile(
    rf"{_MONTH_RE}\s+\d{{1,2}}|\b\d{{1,2}}\s+{_MONTH_RE}|\b\d{{4}}-\d{{2}}-\d{{2}}\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b"
    r"|\b(yesterday|today|tonight|last night)\b", re.IGNORECASE)
_RESULT_NOISE: frozenset[str] = frozenset({
    "final", "score", "scores", "won", "win", "lost", "beat", "beats", "defeated",
    "the", "and", "vs", "at", "on", "in", "over", "ot", "overtime", "result",
    "today", "tonight", "yesterday", "game", "week",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun", "january", "february",
    "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec",
})
_IANA_RE = re.compile(
    r"\b(US/(?:Eastern|Central|Mountain|Pacific|Alaska|Hawaii|Arizona)"
    r"|(?:America|Europe|Asia|Australia|Pacific|Africa|Atlantic)/[A-Z][A-Za-z_]+"
    r"(?:/[A-Z][A-Za-z_]+)?)\b")
_UTC_OFFSET_RE = re.compile(r"\(UTC([+-])(\d{2}):?(\d{2})\)")


# ---- public ------------------------------------------------------------------


def verify_page_reading(graph: dict, preview: dict, *, frame_kind: str | None,
                        wants: list[str], subject: str, now: datetime,
                        many: bool, tier: str = "interpreted",
                        window: str | None = None) -> list[str]:
    """Reasons a page reading can't ship (empty list = accept).

    ``graph`` is the PageGraph the reading came from (``pagegraph`` shape —
    ``ni._fetch_http_page(full=True)`` output works too); ``preview`` maps
    field → value (a string, number, list, or ``rows``), and its ``title`` key
    (the card title the flow adds) is never treated as a reading. ``now`` must
    be timezone-aware (the user's clock). ``tier`` is ``"interpreted"`` (the
    local-model reader, default: the model saw only text + tables, so grounding
    checks only those) or ``"compiled"`` (the P2 selector program lifts values
    verbatim from entities / meta / tables and grounds against all of them).
    ``window`` is the ask's parsed window (``"now"`` for a 'right now' ask): a
    right-now ask requires the page to carry a freshness signal.
    Deterministic; no model.
    """
    assert isinstance(graph, dict) and isinstance(preview, dict), "graph + preview required"
    assert isinstance(wants, list) and isinstance(subject, str), "wants + subject required"
    assert isinstance(now, datetime) and now.tzinfo is not None, "now must be aware"
    assert tier in ("interpreted", "compiled"), "tier must be interpreted or compiled"
    readable = graph.get("readability") or pagegraph.readability(graph)
    if not readable.get("readable"):
        return [f"the page couldn't be read ({readable.get('kind')})"]
    values = _readings(preview, wants)
    if not values:
        return ["the reading is empty"]
    page_text = _page_text(graph)
    ground_text = page_text if tier == "compiled" else _ground_text(graph)
    reasons: list[str] = []
    reasons += _chrome_reasons(graph, values, wants)
    reasons += _grounding_reasons(values, ground_text)
    if many and not _has_rows(values):
        reasons.append(f"one value for a list ask (need at least {_MANY_ROWS} rows)")
    tz = _page_zone(graph, now)
    if frame_kind in _TIME_FRAMES:
        reasons += _time_reasons(values, now, tz)
    if frame_kind == "result":
        reasons += _result_reasons(values)
    reasons += _subject_reasons(subject, page_text)
    reasons += _currency_reasons(graph, values, frame_kind, now, tz)
    reasons += _stale_reasons(values, frame_kind, now, tz)
    if window == "now" and frame_kind in _CURRENT_FRAMES:
        reasons += _freshness_reasons(graph, now)
    # fix8 (blind-7, 2026-10-04): a 'now' ask against a reading phrased as a
    # typical / average / usually value is a GUIDE answer, not what's happening
    # right now ("line at Franklin Barbecue rn" → "on average, 3 to 5 hours
    # long"). Fires regardless of frame_kind so an ambiguous frame still gates.
    if window == "now":
        reasons += _normals_reasons(values)
    # fix8 (blind-7, 2026-10-04): a day-window ask against a page whose own title
    # names a specific date / day that doesn't match the window ("pollen count
    # atlanta tomorrow" → "Pollen Count on 2026-10-04 | Atlanta Allergy &
    # Asthma", today's date). The title's own date is pure code to parse.
    if window and window not in ("now",):
        reasons += _window_mismatch_reasons(graph, window, now, tz, values)
    reasons += _unit_reasons(values, wants, tier)
    return list(dict.fromkeys(reasons))


def has_evidence(graph: dict, wants: list[str], subject: str) -> bool:
    """Pre-model check: the page is readable and its readable text (article,
    visible text, table cells — not its title, meta or headings) holds at
    least one want or subject word. False skips the page without a model call.
    With no distinctive word to look for there is nothing to refuse on."""
    assert isinstance(graph, dict) and isinstance(wants, list), "graph + wants required"
    readable = graph.get("readability") or pagegraph.readability(graph)
    if not readable.get("readable"):
        return False
    tokens = pagegraph._want_tokens(wants) + _subject_tokens(subject)
    if not tokens:
        return True
    body = "\n".join([str(graph.get("text") or ""), _table_text(graph)]).lower()
    return any(_word_in(tok, body) for tok in tokens)


# ---- readings ----------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def _readings(preview: dict, wants: list[str]) -> dict[str, object]:
    """The preview's reading fields: every key but the flow's card ``title``
    (kept only when a want really is 'title'), empty values dropped."""
    want_slugs = {_slug(w) for w in wants if isinstance(w, str)}
    out: dict[str, object] = {}
    for key, value in preview.items():
        if key == "title" and "title" not in want_slugs:
            continue
        if value is None or value == "" or value == []:
            continue
        out[str(key)] = value
    return out


def _strings(value: object) -> list[str]:
    """The scalar strings inside one reading (a list's items, a row's cells)."""
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in list(value)[:50]:
            out += _strings(item)
        return out
    if isinstance(value, dict):
        return [str(v) for v in list(value.values())[:12]
                if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    if isinstance(value, bool):
        return []
    return [str(value)] if str(value).strip() else []


def _has_rows(values: dict) -> bool:
    for value in values.values():
        if isinstance(value, (list, tuple)) and sum(1 for v in value if _strings(v)) >= _MANY_ROWS:
            return True
    return False


def _quote(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_QUOTE else text[:_MAX_QUOTE - 1] + "…"


# ---- page text -----------------------------------------------------------------


def _table_text(graph: dict) -> str:
    lines: list[str] = []
    for table in graph.get("tables") or []:  # jail-capped
        if not isinstance(table, dict):
            continue
        lines.append(" | ".join(str(h) for h in table.get("headers") or []))
        for row in (table.get("rows") or [])[:500]:
            lines.append(" | ".join(str(c) for c in row))
    return "\n".join(lines)


def _page_text(graph: dict) -> str:
    """Everything the page itself shows: text, table cells, structured values,
    title and headings (a compiled reading is lifted from these verbatim)."""
    parts = [str(graph.get("title") or ""), str(graph.get("text") or ""),
             _table_text(graph)]
    parts += [str(line) for line in graph.get("outline") or []]
    parts += [str(v) for v in (graph.get("meta") or {}).values()]
    for ent in graph.get("entities") or []:
        if isinstance(ent, dict):
            parts += [str(v) for k, v in ent.items() if k != "type"]
    return "\n".join(parts)


def _ground_text(graph: dict) -> str:
    """What a local-model reader was actually shown: the body text and the
    visible table cells. Meta descriptions and JSON-LD entity values never
    reached the model, so a value grounded only there is not grounded (field
    2026-10-04: an interpreted reading matched only against the page's own
    JSON-LD ``offers.price``, which the model never saw)."""
    return "\n".join([str(graph.get("text") or ""), _table_text(graph)])


def _norm(text: str) -> str:
    text = re.sub(r"[®™©]", "", str(text).lower())
    return " ".join(text.split()).strip(" .,:;!?-–—|·•\"'()[]")


def _singulars(token: str) -> list[str]:
    """The token plus its singular readings ("storms" → storm, "batteries" →
    battery/batterie, "watches" → watch/watche); a "-y" token also reads as
    its "-ies" plural. -ss/-us/-is words, "news" and short words never fold."""
    forms = [token]
    if token == "news":
        return forms
    if len(token) >= 4 and token.endswith("ies"):
        forms += [token[:-3] + "y", token[:-1]]
    elif len(token) >= 4 and token.endswith("es"):
        forms += [token[:-2], token[:-1]]
    elif len(token) >= 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        forms.append(token[:-1])
    elif len(token) >= 4 and token.endswith("y") and token[-2] not in "aeiou":
        forms.append(token[:-1] + "ies")
    return forms


def _word_in(token: str, low_text: str) -> bool:
    """Whole-word presence of a lowercase token, plural-tolerant both ways
    (the page's "Tropical Storm" serves the subject "tropical storms")."""
    alts = "|".join(re.escape(f) for f in _singulars(token))
    return re.search(rf"(?<![a-z0-9])(?:{alts})(?:e?s)?(?![a-z0-9])", low_text) is not None


# ---- grounding -----------------------------------------------------------------


def _content_words(value: str) -> list[str]:
    return [t for t in dict.fromkeys(_TOKEN_RE.findall(value.lower()))
            if len(t) >= 3 and not t.isdigit() and t not in _STOPWORDS]


def _ground_spans(value: str, low: str, digits: str) -> list[int]:
    """Positions in ``low`` where ``value`` sits: every number appears
    (comma/space-insensitive) and every content word within one window.
    [] = not found. Locates a grounded value (the section check); whether a
    value is grounded at all is ``ni.ground_values``' call."""
    for num in _NUM_RE.findall(value)[:20]:
        if num.replace(",", "") not in digits:
            return []
    words = _content_words(value)
    if not words:
        nums = _NUM_RE.findall(value)
        if not nums:
            return []
        needle = nums[0]
        return [m.start() for m in re.finditer(re.escape(needle), low)][:_MAX_SPANS] or [0]
    anchor = min(words, key=lambda w: low.count(w))
    spans: list[int] = []
    for match in re.finditer(re.escape(anchor), low):
        if len(spans) >= _MAX_SPANS:
            break
        pos = match.start()
        window = low[max(0, pos - _WINDOW_CHARS // 2): pos + _WINDOW_CHARS // 2 + len(anchor)]
        if all(w in window for w in words):
            spans.append(pos)
    return spans


def _grounding_reasons(values: dict, page_text: str) -> list[str]:
    flat: dict[str, str] = {}
    for key, value in values.items():
        for i, text in enumerate(_strings(value)):
            flat[f"{key}#{i}"] = text
    from . import ni  # lazy: the engine may run this gate itself (no import cycle)
    verdict = ni.ground_values(flat, page_text)  # the engine's rule: build = refresh
    return [f"'{_quote(flat[k])}' isn't on the page" for k, ok in verdict.items() if not ok]


# ---- chrome ----------------------------------------------------------------------


def _chrome_names(graph: dict) -> list[str]:
    """The page's names for itself: title (and its segments), site name,
    og/twitter title, and its JSON-LD self-entities' names. A list/collection
    entity's own ``name`` is a label describing the list (not an item of it)
    — a compiled card that lifts it is picking the heading, not a value
    (fix7-page 2026-10-04: findarepo's ItemList.name shipped as 'trending
    python repositories')."""
    names: list[str] = []
    title = str(graph.get("title") or "")
    names.append(title)
    names += re.split(r"\s+[|–—\-·:]\s+", title)
    meta = graph.get("meta") or {}
    for key in ("og:site_name", "og:title", "twitter:title"):
        if meta.get(key):
            names.append(str(meta[key]))
            names += re.split(r"\s+[|–—\-·:]\s+", str(meta[key]))
    for ent in graph.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        etype = ent.get("type")
        if etype in pagegraph.SELF_TYPES:
            names += [str(ent[k]) for k in ("name", "headline", "alternateName") if ent.get(k)]
        elif etype in _LIST_LABEL_TYPES:
            names += [str(ent[k]) for k in ("name", "alternateName") if ent.get(k)]
    return [n for n in (_norm(x) for x in names) if n]


def _chrome_reasons(graph: dict, values: dict, wants: list[str]) -> list[str]:
    names = _chrome_names(graph)
    labels = {_norm(w) for w in wants if isinstance(w, str)}
    labels |= {_norm(_slug(w).replace("_", " ")) for w in wants if isinstance(w, str)}
    labels |= {_norm(k.replace("_", " ")) for k in values}
    headings = {_norm(re.sub(r"^h\d:\s*", "", str(h))) for h in graph.get("outline") or []}
    want_words = set(pagegraph._want_tokens([w for w in wants if isinstance(w, str)]))
    reasons: list[str] = []
    for value in values.values():
        for text in _strings(value):
            # F6-A (blind-5): a reading that still carries the jail "h<n>: "
            # label (an outline-selector leak) is never the page's value — the
            # heading comparison normalizes it off so a label restating the ask
            # ("h3: Traffic & Road Conditions" for "road conditions") refuses.
            bare = re.sub(r"^h\d:\s*", "", str(text))
            norm = _norm(bare)
            if not norm:
                continue
            has_digit = any(ch.isdigit() for ch in norm)
            words = set(_TOKEN_RE.findall(norm))
            if norm in names or norm in labels:
                reasons.append(f"'{_quote(text)}' is the page's own name, not a reading")
            elif (len(norm) >= 3 and sum(ch.isalpha() for ch in norm) >= 3
                  and any(norm != n and re.search(rf"(?<!\w){re.escape(norm)}(?!\w)", n)
                          for n in names)):
                # fix7-page 2026-10-04: a value that sits whole inside a chrome name
                # is chrome whether or not a stock-ticker-shaped digit rides along
                # ("S&P 500 INDEX (^SPX)" lives inside the Yahoo title); a lone
                # number ("65") has <3 alpha chars and still ships.
                reasons.append(f"'{_quote(text)}' is part of the page's name, not a reading")
            elif not has_digit and norm in headings and words & want_words:
                reasons.append(f"'{_quote(text)}' is a label on the page, not its value")
            elif _SLUG_RE.match(text.strip()):
                reasons.append(f"'{_quote(text)}' is not page text")
            # fix8 (blind-7, 2026-10-04): mlb.com shipped "2026 Schedule (PDF)" as
            # the Durham Bulls schedule value — anchor text / download label, not
            # a value. The file-ext suffix "(PDF)" / "(ICS)" / "(XLSX)" / … catches
            # the first class; a bare call-to-action label ("Download", "View
            # schedule", "Click here", "Learn more") catches the second. Fires on
            # both tiers (interpreted + compiled).
            elif _FILE_EXT_SUFFIX_RE.search(bare):
                reasons.append(f"'{_quote(text)}' is a download link label, not a value")
            elif norm in _CTA_LABEL_WORDS:
                reasons.append(f"'{_quote(text)}' is a link label, not a value")
    return reasons


# ---- time ----------------------------------------------------------------------


def _page_zone(graph: dict, now: datetime) -> tzinfo:
    """The zone the page's bare times are in: an explicit graph ``tz``, an
    IANA name or '(UTC-05:00)' the page prints, else the user's clock."""
    named = graph.get("tz")
    text = "\n".join([str(graph.get("text") or "")[:20000], _table_text(graph)[:5000]])
    if not named:
        match = _IANA_RE.search(text)
        named = match.group(1) if match else None
    if named:
        zone = zone_named(str(named))  # legacy names ('US/Mountain') read the same in every image
        if zone is not None:
            return zone
    match = _UTC_OFFSET_RE.search(text)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    return now.tzinfo or UTC


def _clean_when(text: str) -> str:
    text = re.sub(r"±.*$|\+/-.*$", "", text)
    text = re.sub(r"@\s*(\d{1,2})(\d{2})\b", r" \1:\2", text)
    text = re.sub(r"\bat\s+(\d{2})(\d{2})\b", r"at \1:\2", text)
    return " ".join(text.replace("@", " ").split())


def parse_when(text: str, now: datetime, tz: tzinfo) -> tuple[datetime, bool] | None:
    """A page time → (aware datetime, has_clock). Bare times are in ``tz``
    (relative words like 'Today' resolve against ``now`` in that zone). None
    when the text holds no date or time."""
    assert now.tzinfo is not None, "now must be aware"
    raw = str(text or "").strip()
    if not raw or not re.search(r"\d|today|tomorrow|tonight", raw, re.IGNORECASE):
        return None
    try:
        got = datetime.fromisoformat(raw)  # 3.11+: a trailing Z parses as UTC
        has_clock = "T" in raw or ":" in raw
        return (got if got.tzinfo else got.replace(tzinfo=tz)), has_clock
    except ValueError:
        pass
    clean = _clean_when(raw)
    try:
        import dateparser
        from dateparser.search import search_dates
    except ImportError:  # the engine's deps normally carry it (trafilatura → htmldate)
        return None
    settings = {"RELATIVE_BASE": now.astimezone(tz).replace(tzinfo=None)}
    got = dateparser.parse(clean, settings=settings, languages=["en"])
    if got is None:
        found = search_dates(clean, settings=settings, languages=["en"]) or []
        found = [f for f in found if re.search(r"\d|today|tomorrow|tonight", f[0], re.IGNORECASE)]
        if not found:
            return None
        clean, got = found[0]
    has_clock = bool(_CLOCK_RE.search(clean))
    return (got if got.tzinfo else got.replace(tzinfo=tz)), has_clock


def _time_typed(key: str) -> bool:
    return bool(set(_TOKEN_RE.findall(key.lower().replace("_", " "))) & _TIME_WANT_WORDS)


def _clock_required(key: str) -> bool:
    """A key whose want is a punctual minute (ISS pass, sunrise, flyover) — a
    date alone can't ship under next_event / schedule."""
    return bool(set(_TOKEN_RE.findall(key.lower().replace("_", " "))) & _CLOCK_REQUIRED_WORDS)


def _time_reasons(values: dict, now: datetime, tz: tzinfo) -> list[str]:
    reasons: list[str] = []
    for key, value in values.items():
        if not isinstance(value, str):
            continue  # lists/rows: schedule rows are checked by their own frame
        got = parse_when(value, now, tz)
        if got is None:
            if _time_typed(key):
                reasons.append(f"no time in '{_quote(value)}'")
            continue
        when, has_clock = got
        # F6-ISS (blind-5): a next-event / schedule reading whose key names a
        # punctual minute (``pass``, ``sunrise``, ``flyover``) needs a time of
        # day — the whole-day grace would otherwise ship "Saturday, Oct 10" as
        # the next ISS pass over Tucson. Date-only on scheduled-day wants
        # (``drawing``, ``game``, ``next``) still ships, as before.
        if not has_clock and _clock_required(key):
            reasons.append(f"no time in '{_quote(value)}'")
            continue
        if not has_clock:  # a date: the whole day counts
            when = when.replace(hour=23, minute=59, second=59)
        if when < now - _GRACE:
            reasons.append(f"'{_quote(value)}' is already past")
        elif when > now + _HORIZON:
            reasons.append(f"'{_quote(value)}' is too far ahead to be the next one")
    return reasons


# ---- result ----------------------------------------------------------------------


def _result_reasons(values: dict) -> list[str]:
    text = " ".join(t for v in values.values() for t in _strings(v))
    numbers = re.findall(r"\d+", _DATE_WORD_RE.sub(" ", text))
    names = {w.lower() for w in re.findall(r"\b[A-Z][A-Za-z.&'-]{2,}\b", text)
             if w.lower() not in _RESULT_NOISE}
    if len(numbers) >= 2 and len(names) >= 2 and _DATE_WORD_RE.search(text):
        return []
    return ["a result needs both teams, both scores and the date"]


# ---- subject -------------------------------------------------------------------


def _subject_tokens(subject: str) -> list[str]:
    out: list[str] = []
    for tok in _SUBJECT_TOKEN_RE.findall(str(subject).lower()):
        has_digit = any(ch.isdigit() for ch in tok)
        if tok in _GENERIC_SUBJECT or (len(tok) < 3 and not has_digit):
            continue
        if tok not in out:
            out.append(tok)
    return out[:8]


def _subject_reasons(subject: str, page_text: str) -> list[str]:
    low = page_text.lower()
    missing = [t for t in _subject_tokens(subject) if not _word_in(t, low)]
    return [f"the page never mentions '{t}'" for t in missing]


# ---- currency ------------------------------------------------------------------


def _definition_spans(graph: dict) -> list[tuple[int, int]]:
    """Character spans of the text under definition / FAQ / how-to headings."""
    low = str(graph.get("text") or "").lower()
    heads: list[tuple[int, bool]] = []
    for line in graph.get("outline") or []:  # jail-capped
        head = re.sub(r"^h\d:\s*", "", str(line)).strip().lower()
        pos = low.find(head) if head else -1
        if pos >= 0:  # outline order isn't text order (article first, then the rest)
            heads.append((pos, bool(_DEFINITION_HEADING_RE.search(head))))
    heads.sort()
    spans = []
    for i, (pos, is_def) in enumerate(heads):
        if is_def:
            end = heads[i + 1][0] if i + 1 < len(heads) else len(low)
            spans.append((pos, end))
    return spans


def _only_in_definitions(graph: dict, text: str) -> bool:
    spans = _definition_spans(graph)
    if not spans:
        return False
    low = str(graph.get("text") or "").lower()
    found = _ground_spans(text, low, re.sub(r"[\s,]", "", low))
    return bool(found) and all(any(a <= p < b for a, b in spans) for p in found)


def _dates_in(text: str, now: datetime, tz: tzinfo) -> set:
    """Calendar dates a value names (month/day forms, ISO dates)."""
    local = now.astimezone(tz)
    out = set()
    for match in _DAY_MONTH_RE.finditer(text):
        out.add(_mkdate(match.group(3), match.group(2), match.group(1), local))
    for match in _MONTH_DAY_RE.finditer(text):
        out.add(_mkdate(match.group(3), match.group(1), match.group(2), local))
    for match in _ISO_DATE_RE.finditer(text):
        out.add(_mkdate(match.group(1), match.group(2), match.group(3), local))
    out.discard(None)
    return out


def _mkdate(year: str | None, month: str, day: str, local: datetime):
    try:
        mon = int(month) if month.isdigit() else _MONTHS.index(month[:3].lower()) + 1
        return local.date().replace(year=int(year) if year else local.year,
                                    month=mon, day=int(day))
    except (ValueError, IndexError):
        return None


def _currency_reasons(graph: dict, values: dict, frame: str | None,
                      now: datetime, tz: tzinfo) -> list[str]:
    reasons: list[str] = []
    types = {str(e.get("type")) for e in graph.get("entities") or [] if isinstance(e, dict)}
    if frame and frame != "latest_items" and types & _NEWS_TYPES:
        reasons.append("the page is a dated news story, not a source that stays current")
    today = now.astimezone(tz).date()
    for value in values.values():
        for text in _strings(value):
            if _only_in_definitions(graph, text):
                reasons.append(f"'{_quote(text)}' is explanatory text, not current data")
            if frame in _NORMALS_FRAMES and _NORMALS_RE.search(text):
                reasons.append(f"'{_quote(text)}' is a normal, not a current reading")
            if frame in _CURRENT_FRAMES:
                dates = _dates_in(text, now, tz)
                if len(dates) >= 2 and all(d < today for d in dates):
                    reasons.append(f"'{_quote(text)}' lists past records, not what's happening now")
    return reasons


# ---- freshness ------------------------------------------------------------------


def _reading_has_status_word(text: str) -> bool:
    """True when a reading reads as a current state (not a bare timestamp):
    'operational', 'Open sunrise to sunset', a burn ban 'is prohibited'.
    Keeps _stale_reasons from refusing live status readings that name a
    past declaration date."""
    low = str(text or "").lower()
    for tok in _TOKEN_RE.findall(low):  # bounded by the reading
        if tok in _STATUS_WORDS:
            return True
    return False


def _stale_reasons(values: dict, frame: str | None, now: datetime,
                   tz: tzinfo) -> list[str]:
    """A current/status/schedule reading that is just a clock-timestamped
    event, parsed well past now (fix7-page 2026-10-04: flight-status.com
    shipped 'San Francisco (SFO) 2026-06-30T07:00' as the flight status 96
    days after the fact). A reading with a status word rides through: a
    burn ban 'As of 8/11/26, outdoor burning is prohibited' states today's
    condition, not a stale timestamp."""
    if frame not in _CURRENT_FRAMES and frame not in _TIME_FRAMES:
        return []
    reasons: list[str] = []
    for value in values.values():
        if not isinstance(value, str):
            continue  # list/row readings go through their own frame checks
        if _reading_has_status_word(value):
            continue
        got = parse_when(value, now, tz)
        if got is None:
            continue
        when, has_clock = got
        if has_clock and when < now - _STALE_GRACE:
            reasons.append(f"'{_quote(value)}' names a time that is already past")
    return reasons


def _entity_dates(graph: dict) -> list[datetime]:
    """Every ISO-8601 datetime a JSON-LD field names (dateModified /
    datePublished / lastReviewed / uploadDate): the structured freshness
    signals a page carries, with no model."""
    out: list[datetime] = []
    for ent in (graph.get("entities") or [])[:40]:  # jail-capped list
        if not isinstance(ent, dict):
            continue
        for key in ("dateModified", "datePublished", "lastReviewed", "uploadDate"):
            value = ent.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                got = datetime.fromisoformat(value)
            except ValueError:
                continue
            if got.tzinfo is None:
                got = got.replace(tzinfo=UTC)
            out.append(got)
    return out


def _meta_dates(graph: dict) -> list[datetime]:
    """Every ISO-8601 datetime an HTML meta tag names (article:modified_time,
    og:updated_time): the page's own freshness markup, no model."""
    out: list[datetime] = []
    for key in ("article:modified_time", "article:published_time",
                "og:updated_time", "og:article:modified_time"):
        value = (graph.get("meta") or {}).get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            got = datetime.fromisoformat(value)
        except ValueError:
            continue
        if got.tzinfo is None:
            got = got.replace(tzinfo=UTC)
        out.append(got)
    return out


def _normals_reasons(values: dict) -> list[str]:
    """A 'right now' ask against a reading phrased as a normal / typical /
    average / usual value — a guide answer, not current state (fix8 blind-7,
    2026-10-04: eathealthy365's Franklin Barbecue guide answered 'how long is
    the line rn' with 'on average, 3 to 5 hours long'). The ``_NORMALS_RE``
    phrase set already catches the forecast-frame overreach; this gates the
    same phrases on a 'now' ask under any frame_kind."""
    assert isinstance(values, dict), "values must be a dict"
    reasons: list[str] = []
    for value in values.values():
        for text in _strings(value):
            if _NORMALS_RE.search(text):
                reasons.append(f"'{_quote(text)}' is a typical value, not what's happening now")
    return reasons


_WINDOW_LABEL = {"today": "today", "tonight": "tonight", "tomorrow": "tomorrow",
                 "weekend": "the weekend"}
_DOW_FULL = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def _window_target_dates(window: str, local_today) -> set:
    """The calendar dates a day-named ``window`` points at, in the user's zone.
    ``today`` / ``tonight`` = today; ``tomorrow`` = tomorrow; ``weekend`` = the
    next Sat + Sun; ``dow:<day>`` = the next date on that day of the week.
    None / open windows (``now``, ``next_days:N`` / ``next_hours:N``, 'upcoming')
    return set() so the mismatch check doesn't fire."""
    assert isinstance(window, str), "window must be a string"
    if window in ("today", "tonight"):
        return {local_today}
    if window == "tomorrow":
        return {local_today + timedelta(days=1)}
    if window == "weekend":
        # ahead to Sat and Sun (today counts when today IS the weekend)
        out: set = set()
        for i in range(8):  # bounded: one week ahead at most
            d = local_today + timedelta(days=i)
            if d.weekday() in (5, 6):
                out.add(d)
                if len(out) >= 2:
                    break
        return out
    if window.startswith("dow:"):
        want = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4,
                "sat": 5, "sun": 6}.get(window[4:])
        if want is None:
            return set()
        for i in range(8):  # bounded: this week's instance
            d = local_today + timedelta(days=i)
            if d.weekday() == want:
                return {d}
    return set()


def _title_dates(graph: dict, now: datetime, tz: tzinfo) -> set:
    """Calendar dates the page's TITLE names (ISO date, "October 4", "Oct 4
    2026", "4 Oct"). The title is the page's own name for what it shows; a
    specific date there is the day the page is about. Body text isn't read —
    an article dated last week often names other dates in passing."""
    title = str(graph.get("title") or "")
    return _dates_in(title, now, tz)


def _window_mismatch_reasons(graph: dict, window: str, now: datetime,
                               tz: tzinfo, values: dict) -> list[str]:
    """A day-window ask against a page whose own TITLE names a specific date
    that doesn't match the window (fix8 blind-7, 2026-10-04: 'pollen count
    atlanta tomorrow' shipped from a page titled 'Pollen Count on 2026-10-04'
    — today's date, not tomorrow's). ``window`` is one of the day forms
    (today / tonight / tomorrow / weekend / dow:<day>); other windows don't
    fire the check. fix10 (blind-8, 2026-10-04): the same check also fires on
    the READING's own date — 'ISS pass over chicago tonight' held a reading
    'Monday 12 October 06:19 GMT-5' past tonight's day. Deterministic; no model."""
    assert isinstance(window, str) and isinstance(graph, dict), "args required"
    assert isinstance(values, dict), "values must be a dict"
    local_today = now.astimezone(tz).date()
    want_dates = _window_target_dates(window, local_today)
    if not want_dates:
        return []
    label = _WINDOW_LABEL.get(window) or _DOW_FULL.get(window[4:], window) \
        if window.startswith("dow:") or window in _WINDOW_LABEL else window
    for value in values.values():  # bounded by preview fields
        for text in _strings(value):  # bounded by scalars per field
            got = _dates_in(text, now, tz)
            if got and not (got & want_dates):
                shown = min(got).isoformat()
                return [f"the reading shows {shown}, not {label}"]
    page_dates = _title_dates(graph, now, tz)
    if not page_dates:
        return []
    if page_dates & want_dates:
        return []
    shown = min(page_dates).isoformat()
    return [f"the page shows {shown}, not {label}"]


def _unit_reasons(values: dict, wants: list[str], tier: str) -> list[str]:
    """A reading for a physical-unit want must carry its unit on the value
    (fix10 blind-8, 2026-10-04: 'lake tahoe water temp' → '9.5' shipped with
    no °C / °F). Fires on the interpreted tier only — the compiled tier reads
    the value verbatim from the page's own structure (its column header
    carries the unit the user can see). Unitless indexes (AQI, UV, Kp, pollen
    level, percentages) stay shipping via the exempt word set."""
    assert isinstance(values, dict) and isinstance(wants, list), "args required"
    assert tier in ("interpreted", "compiled"), "tier must be closed"
    if tier != "interpreted":
        return []
    want_tokens: set[str] = set()
    for want in wants[:16]:  # bounded by the intent's wants cap
        if isinstance(want, str):
            want_tokens |= set(_TOKEN_RE.findall(want.lower().replace("_", " ")))
    key_tokens_by_key: dict[str, set[str]] = {}
    for key in list(values)[:16]:  # bounded by the preview fields
        key_tokens_by_key[key] = set(_TOKEN_RE.findall(str(key).lower().replace("_", " ")))
    reasons: list[str] = []
    for key, value in values.items():
        if not isinstance(value, str):
            continue  # a list / rows reading is checked by the many-rows rule, not here
        text = value.strip()
        if not text or not re.search(r"\d", text):
            continue
        tokens_here = (key_tokens_by_key.get(key) or set()) | want_tokens
        if not (tokens_here & _UNIT_REQUIRED_WORDS) or (tokens_here & _UNIT_EXEMPT_WORDS):
            continue
        if _UNIT_LITERAL_RE.search(text):
            continue
        reasons.append(f"no unit on '{_quote(text)}'")
    return reasons


def _freshness_reasons(graph: dict, now: datetime) -> list[str]:
    """A 'right now' ask against a page that carries no freshness signal
    (fix7-page 2026-10-04: cityvibe.me's Franklin Barbecue guide answered
    'how long is the line right now' with a static '50 to 100 people' —
    the page has no updated timestamp, no 'minutes ago', no today's date).

    Signals: an entity date within 48 h, a meta modified time within 48 h,
    an 'updated / as of / N minutes ago' phrase in the page text, or
    today's ISO date in the page text / outline."""
    cutoff = now - _FRESH_WINDOW
    for got in _entity_dates(graph) + _meta_dates(graph):
        if got >= cutoff:
            return []
    text = (str(graph.get("title") or "") + "\n" + str(graph.get("text") or "")
            + "\n" + " ".join(str(line) for line in graph.get("outline") or [])
            + "\n" + _table_text(graph))
    if _FRESH_PHRASE_RE.search(text):
        return []
    today = now.date().isoformat()
    if today in text:
        return []
    return ["the page has no freshness signal — no 'as of' / updated time, "
            "no 'minutes ago', no today's date"]
