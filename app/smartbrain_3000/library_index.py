"""SmartBrain Library — the local index of where the data lives (NI Library page, R8/R9/R12).

The Library (github.com/SecureCloudGroup/SmartBrain_Library) publishes one DuckDB file per release.
This app release pins that file's sha256 (``PACK``): the first time the Library is needed the gzip is
fetched through netguard, its hash must match exactly, and it is unpacked into the data directory and
opened READ-ONLY. It is public catalog data (no user content), so it is plaintext at rest.

The user's own **local sources** are user data: they live sealed in NIStore's reserved-id snapshot rows
(``__library_local__``), never in the plaintext pack, and are searched in memory.

Lookup is plain SQL over the pack's term table (the same scoring as ``sourcetool lookup``) — DuckDB's
FTS extension is not used because it downloads at runtime and the Library must work offline.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from . import library_embed, netguard, ni

log = logging.getLogger(__name__)

# --- the pinned pack (ruling R12: the app release pins the exact bytes) ----------------------------
# words that name no outlet on their own: a provider called only these ("US Weather", "News") is
# never "named" by an ask that uses them
_GENERIC_PROVIDER_WORDS = frozenset({
    "a", "an", "and", "the", "of", "us", "usa", "uk", "news", "weather", "world", "daily", "live",
    "data", "info", "online", "today", "times", "now", "top", "latest", "service", "services",
    "network", "media", "group", "report", "reports", "update", "updates"})
PACK = {
    "tag": "v1.3.0",
    "url": "https://github.com/SecureCloudGroup/SmartBrain_Library/releases/download/v1.3.0/library.duckdb.gz",
    "sha256": "73772a56f202f27883488a25c2c151835287910f291159f5d17409a55be1d411",
}
MAX_PACK_GZ_BYTES = 60_000_000       # the download cap (the v1.2 gzip is ~18 MB)
MAX_PACK_BYTES = 600_000_000         # the unpacked cap (the v1.2 file is ~80 MB)

LOCAL_RESERVED_ID = "__library_local__"
LOCAL_SLOT = "sources"
VOTES_SLOT = "votes"  # R6: this user's Yes taps on Library sources (sealed)
MAX_LOCAL_SOURCES = 500
MAX_PAGE = 50
# formats the card flow can sample after a tap: JSON APIs, JSON discovery docs (gbfs),
# CSV downloads, RSS/Atom news feeds, XML documents, plain-text pages, and web pages
# (the page door parses HTML separately). Each kind maps to the ``format`` a candidate
# row carries so the sampling fetch parses accordingly. ``docs_only`` / ``internal`` /
# the transit / calendar / image kinds still stay off the card — they need consent
# machinery a card flow does not have yet (protobuf, gtfs zip archives, ical, images).
_FLOW_KINDS = ("http_json", "gbfs", "http_csv", "rss", "atom", "http_xml", "text", "html")
_FORMAT_BY_KIND: dict[str, str] = {
    "http_json": "json", "gbfs": "json", "http_csv": "csv",
    "rss": "feed", "atom": "feed", "http_xml": "xml",
    "text": "text", "html": "html",
}

_STATE_CODES = frozenset({"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
                          "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
                          "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
                          "VT", "VA", "WA", "WV", "WI", "WY", "PR"})
# words that never say WHAT the user wants (relevance ignores them)
# with vectors, a source outside the asked category is about the ask only through words few reviewed sources
# say: a word at most this many sources' own words name. Fitted on the Library's source and route asks
# ((ask, source) pairs, no embedder; right = filed under the ask's subcategory): 1 / 2 / 3 / 5 / 8 / any ->
# .877 / .812 / .799 / .747 / .720 / .590 precision. 3 is the knee, but 2 keeps precision above .8 and a word
# three sources share ("activity": space weather, Congress) still admitted strays: precision wins
RARE_WORD_SOURCES = 2
_FILLER = frozenset({"gonna", "gotta", "wanna", "will", "there", "does", "do", "did", "over", "coming", "going",
                     "can", "see", "when", "how", "whats"})

ACCESS_KINDS = ("http_json", "http_csv", "http_xml", "rss", "atom", "gtfs", "gtfs_rt", "gbfs", "ics", "html",
                "image", "text")
TIERS = ("curated", "provider_trusted", "harvested", "local")
STATUSES = ("ok", "degraded", "failed", "refused", "unvalidated")

_STOP = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "for", "to", "and", "or", "is", "are", "was", "be", "by",
    "with", "from", "as", "it", "its", "this", "that", "what", "whats", "how", "when", "where", "who",
    "which", "my", "me", "i", "show", "get", "give", "tell", "today", "now", "current", "latest", "near",
    "about", "into", "per", "vs",
})
_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_SECRETISH = re.compile(r"(api[_-]?key|apikey|token|secret|password|access_key)=([^&{}\s]+)", re.IGNORECASE)

# candidates: the sources that take a named entity, that the ask's words hit, or that are filed in the asked
# category (membership is relevance). An entity lifts its sources (and those whose declared subject the ask
# names); it never gates the rest out: a league-wide or index source takes no resolver (blind 2026-09-29:
# "MLB wild card standings" -> [])
_LOOKUP_SQL = """
WITH q(term) AS (SELECT unnest(?::VARCHAR[])),
hits AS (SELECT t.source_id, sum(t.weight) AS rel FROM library_terms t JOIN q ON t.term = q.term
         GROUP BY t.source_id),
catb AS (SELECT source_id, max(CASE WHEN category || '/' || subcategory IN (SELECT unnest(?::VARCHAR[]))
                               THEN 2.5 ELSE 0 END) AS cb
         FROM library_source_categories GROUP BY source_id),
pref(authority, pos) AS (SELECT unnest(?::VARCHAR[]), generate_subscripts(?::VARCHAR[], 1)),
ent AS (SELECT DISTINCT source_id FROM library_source_resolvers WHERE resolver IN (SELECT unnest(?::VARCHAR[]))),
geo AS (SELECT DISTINCT r.source_id FROM library_source_resolvers r JOIN catb c ON c.source_id = r.source_id
        WHERE c.cb > 0 AND r.resolver IN (SELECT unnest(?::VARCHAR[]))),
cand AS (SELECT source_id FROM ent UNION SELECT source_id FROM hits UNION SELECT source_id FROM catb WHERE cb > 0)
SELECT s.id,
       coalesce(h.rel / (SELECT max(rel) FROM hits), 0) * 4
         * (CASE s.tier WHEN 'harvested' THEN 0.7 WHEN 'provider_trusted' THEN 0.9 ELSE 1.0 END)
         + coalesce(c.cb, 0) + s.prior
         - (CASE WHEN s.audience <> '' AND NOT list_contains(?::VARCHAR[], s.audience) THEN 1.5 ELSE 0 END)
         + coalesce((SELECT 0.6 - 0.3 * (p.pos - 1) FROM pref p WHERE p.authority = s.authority), 0)
         + (CASE WHEN s.id IN (SELECT source_id FROM ent) OR (s.entity <> '' AND strpos(?,
                 ' ' || trim(regexp_replace(lower(s.entity), '[^a-z0-9]+', ' ', 'g')) || ' ') > 0)
            THEN 3.5 ELSE 0 END)
         + (CASE WHEN s.id IN (SELECT source_id FROM geo) THEN 1.5 ELSE 0 END)
         + (CASE WHEN list_has_any(s.kinds, ?::VARCHAR[]) THEN 1.0 ELSE 0 END) AS score
FROM cand JOIN library_sources s ON s.id = cand.source_id LEFT JOIN hits h ON h.source_id = s.id
     LEFT JOIN catb c ON c.source_id = s.id
WHERE s.role <> 'helper' AND {where}
ORDER BY score DESC, (s.auth <> 'none'), s.prior DESC, s.id"""  # ties: keyless first, then the likelier

# --- ranking context (a port of SmartBrain_Library sourcetool/build.py; the Library's eval sets are the contract)
ENTITY_RESOLVERS = ("team_mlb", "team_nhl", "team_espn", "ticker", "crypto", "currency", "airport", "statuspage",
                    "soccer_competition", "fr_agency", "spending_agency", "sports_league")  # a ZIP is a location
GEO_RESOLVERS = ("place", "zip", "county", "us_state", "tide_station", "buoy", "airport", "radar_site", "nwps_gauge")
ENTITY_CUES = {
    "airport": {"airport", "airports", "flight", "flights", "delay", "delays", "delayed", "tsa", "ground", "gate",
                "departures", "arrivals", "runway"},
    "ticker": {"stock", "stocks", "share", "shares", "ticker", "trading", "earnings", "filings", "filing", "sec",
               "dividend", "market", "nasdaq", "nyse", "etf", "fund", "10k", "10q", "8k", "quote"},
    "crypto": {"crypto", "coin", "coins", "token", "tokens", "cryptocurrency", "btc", "eth", "blockchain"},
    "currency": {"exchange", "rate", "rates", "fx", "forex", "currency", "currencies", "convert", "conversion",
                 "to", "vs", "per"},
    "soccer_competition": {"table", "standings", "fixtures", "league", "match", "matches", "game", "games", "score",
                           "scores", "cup", "season"},
    "fr_agency": {"rule", "rules", "regulation", "regulations", "register", "notice", "notices", "federal",
                  "proposed", "comment", "comments"},
    "spending_agency": {"spending", "spent", "budget", "contract", "contracts", "award", "awards", "grant",
                        "grants", "obligations", "outlays"},
    "zip": set(),
    "team_espn": {"game", "games", "score", "scores", "schedule", "standings", "play", "plays", "won", "win", "lost",
                  "vs", "match", "season", "roster", "football", "basketball", "baseball", "hockey", "soccer"},
    "statuspage": {"down", "status", "outage", "outages", "incident", "working", "up"},
}
AUDIENCE_CUES = {
    "aviation": r"\b(aviation|airport|flight|flights|pilot|pilots|metar|taf|runway)\b",
    "marine": r"\b(marine|boat|boating|sailing|offshore|coastal waters|small craft|buoy|mariners?|surf|waves?)\b",
}
KIND_CUES = [
    ("trend", r"\b(chart|history|historical|trend|over time|since|past \d+|last \d+|this year|over the)\b"),
    ("ranking", r"\b(top \d*|best|standings|table|ranking|rankings|leaders|leaderboard|most)\b"),
    ("next_event", r"\b(next|when is|when does|when will|upcoming|countdown)\b"),
    ("schedule", r"\b(schedule|calendar|fixtures|this week|tonight|lineup)\b"),
    ("alerts", r"\b(alert|alerts|warning|warnings|advisory|watch)\b"),
    ("latest_items", r"\b(latest|new|recent|headlines|news|feed)\b"),
    ("count", r"\b(how many|number of|count)\b"),
    ("status", r"\b(status|down|outage|open|closed|delays?|delayed)\b"),
    ("forecast", r"\b(forecast|tomorrow|will it|this weekend|next week)\b"),
]


def question_kinds(ask: str) -> list[str]:
    low = (ask or "").lower()
    return [k for k, rx in KIND_CUES if re.search(rx, low)] or ["current_value"]


# --- the frame (§29 stage 2): the ask's question kind, parsed by code from a closed cue table ---------
FRAME_KINDS = ("current_value", "next_event", "schedule", "forecast", "result", "trend", "ranking",
               "latest_items", "alerts", "status", "count")
# the first cue that matches is the kind; no cue leaves the kind open ("latest X" is often a current value,
# "tonight" is a window, not a kind)
_FRAME_CUES = [
    ("forecast", r"\b(forecasts?|outlook|will it|is it (going to|gonna)|(chance|chances|odds) of|rain chance|"
                 r"going to (rain|snow|storm|freeze|be (hot|cold|warm|windy|sunny|nice)))\b"),
    ("next_event", r"\b(next|upcoming|countdown|when (is|does|do|will|can|are)|when's|whens|"
                   r"what time (is|does|do|will|are)(?! it\b)|game ?time|kick ?off|tip ?off|first pitch|"
                   r"how long until|days (until|till|to)|predictions?|pass(es)? over|flyovers?)\b"),
    ("alerts", r"\b(alerts?|warnings?|advisory|advisories|(tornado|storm|flood|hurricane|tropical storm|freeze|"
               r"frost|fire weather|tsunami|severe thunderstorm) watch(es)?)\b"),
    ("count", r"\b(how many|number of)\b"),
    ("schedule", r"\b(schedules?|calendar|fixtures|lineup|timetable|tour dates)\b"),
    # how a market closed is its closing value, not a game's result
    ("current_value", r"\b(how|where) did (the )?(stock )?(markets?|stocks|dow|s ?p|s and p|nasdaq|index|indexes|indices)"
                      r"( \d+)? (close|end|finish|do)\b"),
    ("result", r"\b(scores?|won|win|wins|lost|lose|beat|final|results?|how did|who won|"
               r"(last|previous|most recent) (\w+ )?(game|match|launch|race|fight))\b"),
    ("ranking", r"\b(standings|rankings?|ranked|leaderboard|leaders|top \d+|top (ten|five|twenty)|bestsellers?|"
                r"best sellers?|(music|song|album|billboard) charts?|playoff picture|league table)\b"),
    ("trend", r"\b(charts?|history|historical|trends?|over time|since (19|20)\d\d|(past|last) \d+ "
              r"(days|weeks|months|years)|over the (past|last))\b"),
    ("status", r"\b(status|down|outages?|delays?|delayed|running|closures?|trackers?)\b"),
    # "right now" is a window, not a kind: "hurricanes right now" wants the list of storms
    ("current_value", r"\b(price of|how much (is|are|does)|where is|where's|how('s| is| are) .{1,40} doing)\b"),
    ("latest_items", r"\b(news|headlines|breaking|stories|articles|posts)\b"),
]
# which declared source kinds can serve each frame kind (a source whose kinds can't is not a candidate)
_SERVES = {
    "next_event": {"next_event", "schedule", "forecast"}, "schedule": {"schedule", "next_event"},
    "result": {"result", "latest_items"}, "forecast": {"forecast"}, "current_value": {"current_value", "status"},
    "trend": {"trend"}, "ranking": {"ranking"}, "alerts": {"alerts", "status"},
    "status": {"status", "alerts", "current_value"}, "count": {"count", "latest_items", "alerts"},
    "latest_items": {"latest_items"},
}


# an intent "place" that names no particular place: the pack's own scope, or the user's own whereabouts
_NOT_A_PLACE = frozenset({"us", "usa", "u s", "united states", "america", "the us", "nationwide", "national",
                          "country", "the country", "here", "near me", "nearby", "my area", "local", "my location",
                          "current location", "home"})


def frame_kind_from_text(ask: str) -> str | None:
    """The question kind the ask's wording states (one of FRAME_KINDS), or None when it states none.
    Code parses it; when the intent's model disagrees, this wins (as the cadence does)."""
    low = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", (ask or "").lower().replace("’", "'"))).strip()
    return next((k for k, rx in _FRAME_CUES if re.search(rx, low)), None)


def _fold(word: str) -> str:
    """A simple plural folded to its singular (as the answer matcher folds it)."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _speaks_to(need: set[str], own: str, phrases: list[str], ask: str, name: str = "") -> bool:
    """Does a source's own text name any word the user named? Whole words, plurals folded; a word inside a
    declared multi-word phrase ("geomagnetic storm", "near miss") counts only when the ask says the phrase.
    The source's ``name`` is what it is: its words always count."""
    from .library_resolve import ENGLISH as ENGLISH_WORDS
    from .library_resolve import norm

    def folded(t: str) -> str:
        return " " + " ".join(_fold(w) for w in norm(t).split()) + " "
    text, asked = folded(own), folded(ask)
    for p in phrases:  # bounded by the source's answers and the taxonomy
        pn = folded(p)
        # a phrase counts when the ask says it, or two of its words ("fed" + "interest" of "interest rate the
        # fed set"); "storm" alone is not "geomagnetic storm"
        if pn.count(" ") > 2 and pn not in asked and len(set(pn.split()) & set(asked.split()) - ENGLISH_WORDS) < 2:
            text = text.replace(pn, " | ")
    return bool({_fold(w) for w in need} & set((text + folded(name)).split()))


def named_place(res, ask: str, hint_place: str | None = None) -> tuple[set[str], bool, str]:
    """The place the ask names, decided as the fill decides it: (its words, named?, the resolver's status).

    Starts from the resolver's own reading (a "largest of its name" pick is a big place), then the other
    ties; a place is the ask's when it is big, follows in/at/near/for/around, is a reviewed area name, comes
    with a state, or is the intent's own place. A place the resolver can't settle is "ambiguous", never
    dropped. A ZIP or a state name is a place too."""
    from .library_resolve import norm
    low = f" {norm(ask)} "
    if re.search(r"\b\d{5}\b", ask or ""):
        return set(), True, "resolved"
    st = res.by_name("us_state", ask)
    state_words = set(norm(st["best"]["name"]).split()) if st["status"] == "resolved" else set()
    hinted = f" {norm(hint_place or '')} "
    if hint_place:
        # the intent's place, when the ask says it, is the place: "Lake Michigan water temperature in Chicago" is
        # Chicago, not the Michigan beach town a longer reading names
        said, status = _said_place(res, hint_place, low)
        if said:
            return set(said.split()) | state_words, True, status
    r = res.by_name("place", ask)
    words, passed = _place_words_in(res, r, low, ask, hinted)
    status = r["status"]
    if not words and passed:
        # a longer name that isn't the place can hide the one that is ("Lake Erie Beach" in "Lake Erie water
        # temp Cleveland"): the rest of the ask is read once more
        rest = low
        for said in passed:
            rest = rest.replace(f" {said} ", " ")
        r = res.by_name("place", rest)
        words, _ = _place_words_in(res, r, low, ask, hinted)
        status = r["status"] if words else status
    if words:
        return words | state_words, True, status
    if state_words:
        return state_words, True, "resolved"
    # the intent's place, said in the ask, that the resolver can't settle ("Reykjavik"): named, never dropped
    said = f" {norm(hint_place or '')} ".strip()
    if said and said not in _NOT_A_PLACE and f" {said} " in low:
        return set(), True, "unresolved"
    return set(), False, "none"


def _said_place(res, hint_place: str, low: str) -> tuple[str, str]:
    """The words of the intent's place as the ask says them (its name or an alias), and the resolver's status
    for it; ('', 'none') when the ask doesn't say it."""
    from .library_resolve import norm
    h = res.by_name("place", hint_place)
    for c in ([h["best"]] if h["best"] else h["candidates"][:3]):  # bounded: three readings
        nm = norm(c["name"])
        said = nm if f" {nm} " in low else next((w for w in _aliases_in(res, c["id"], low)), "")
        if said:
            return said, h["status"]
    return "", "none"


def _place_words_in(res, r: dict, low: str, ask: str, hinted: str) -> tuple[set[str], list[str]]:
    """The words of a place reading's candidates that the ask names as its place, and the names said but
    passed over (a small place said without a cue)."""
    from .library_resolve import norm, states_in
    words: set[str] = set()
    passed: list[str] = []
    if r["status"] == "none":
        return words, passed
    first = [r["best"]] if r["best"] else []
    dominant = r.get("reason") == "largest of its name"
    for i, c in enumerate(first + [c for c in r["candidates"] or [] if not first or c["id"] != first[0]["id"]]):
        nm = norm(c["name"])
        said = nm if f" {nm} " in low else next((w for w in _aliases_in(res, c["id"], low)), "")
        if not said:
            continue
        # the resolver's "largest of its name" pick is big when its NAME is said (not a short form: the
        # "silver" of Silver City is a metal) and the name is not a word the Library's categories use ("winter"
        # is the season, not Winter, WI)
        big = (c.get("attrs", {}).get("pop") or 0) >= 100_000 or (
            dominant and i == 0 and said == nm and not res._con.execute(
                "SELECT 1 FROM (SELECT unnest(keywords) AS k FROM library_taxonomy) "
                "WHERE ' ' || k || ' ' LIKE '% ' || ? || ' %' LIMIT 1", [nm]).fetchone())
        # "of" is not a place cue: "price of silver" is a metal, not Silver City (live 2026-09-29)
        prep = re.search(rf" (in|at|near|for|around) {re.escape(said)} ", low)
        # a reviewed area name ("tahoe", "obx", "cape cod") is a place on its own
        nickname = said in ((c.get("attrs") or {}).get("nicknames") or [])
        # a small place's FULL name of two or more words is unmistakable ("Cape Canaveral", "Daytona Beach",
        # "North Platte"); a one-word name ("silver", "startup") still needs its cue
        full_name = said == nm and len(nm.split()) >= 2
        if big or prep or nickname or full_name or states_in(ask) or f" {said} " in hinted:
            words |= set(said.split())
        else:
            passed.append(said)
    return words, passed


def _place_state_of(res, text: str) -> str:
    """The US state code the pack's place resolver reads from ``text`` (the dominant 'largest of its
    name' reading, else the best candidate's state), or ''. Used by ``_context`` to tell a team
    entity's own city apart from the asked place's city (fix10 blind-8 2026-10-04: 'Durham Bulls'
    shipped the Chicago Bulls' schedule because the pack has no Durham Bulls)."""
    assert isinstance(text, str), "text must be a string"
    text = text.strip()
    if not text:
        return ""
    r = res.by_name("place", text)
    best = r.get("best") or ((r.get("candidates") or [None])[0])
    return str((best or {}).get("state") or "").upper()


def audiences(ask: str) -> list[str]:
    return [a for a, rx in AUDIENCE_CUES.items() if re.search(rx, (ask or "").lower())]


def _aliases_in(res, entry_id: str, low: str, partial: bool = False) -> list[str]:
    rows = res._con.execute("SELECT alias FROM library_resolver_aliases WHERE entry_id = ? AND partial = ?",
                            [entry_id, partial]).fetchall()
    return sorted((a for (a,) in rows if f" {a} " in low), key=len, reverse=True)


def _own_words(record: dict) -> tuple[str, list[str]]:
    """A source's own words besides its name (description, examples, declared answers) and the phrases its
    answers declare."""
    own = [str(record.get("description") or ""), " ".join(str(e) for e in record.get("examples") or [])]
    declared: list[str] = []
    for a in record.get("answers") or []:  # bounded by the Library's answers limit
        if isinstance(a, dict):
            own += [str(a.get("label", "")), " ".join(str(w) for w in a.get("words") or [])]
            declared += [str(w) for w in a.get("words") or []]
    return " | ".join(own), declared


def _needs_place(record: dict) -> bool:
    """Does the source need a location the user must name (not a default, not an entity like an airport)? A
    place parameter a same-host helper fills from the place (NWS's grid from its points lookup) needs one too."""
    return any(((p.get("fill") or {}).get("from") == "resolver" and p["fill"].get("resolver") in GEO_RESOLVERS
                and p["fill"].get("resolver") not in ENTITY_RESOLVERS and not p["fill"].get("fallback"))
               or ((p.get("fill") or {}).get("from") == "source" and p.get("kind") == "place")
               for p in (record.get("access") or {}).get("params") or [])


def _league_of(entry: dict | None) -> str:
    return str(((entry or {}).get("attrs") or {}).get("league") or "")


class LibraryIndexError(Exception):
    """The pack could not be installed or read (message is safe to show)."""


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9.+]*", (text or "").lower()) if t not in _STOP and len(t) > 1]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LibraryIndex:
    """The installed pack: install on first need, then read-only queries. One per app."""

    def __init__(self, data_dir: Path, netguard_mod=None, pack: dict | None = None) -> None:
        assert data_dir is not None, "data_dir required"
        self._dir = Path(data_dir) / "library"
        self._path = self._dir / "library.duckdb"
        self._meta = self._dir / "installed.json"
        self._net = netguard_mod or netguard
        self._pack = dict(pack or PACK)
        self._lock = threading.Lock()
        self._taxonomy_cache: list[dict] | None = None
        self._subcats_cache: dict[str, tuple[list[str], dict]] | None = None
        self._league_aliases: list[tuple[str, str]] | None = None
        self._has_result: bool | None = None
        self._has_route_asks_cache: bool | None = None
        self._takes_cache: dict[str, set[str]] | None = None
        self._own_cache: dict[str, set[str]] | None = None
        self._names_cache: list[tuple[str, set[str]]] | None = None
        self._entity_vocab_cache: dict[str, set[str]] = {}
        self._provider_names_cache: list[tuple[str, str]] | None = None

    # --- install -----------------------------------------------------------------------------

    def installed(self) -> dict | None:
        """The installed pack's {tag, sha256} if it is the one this release pins, else None."""
        try:
            meta = json.loads(self._meta.read_text())
        except (OSError, ValueError):
            return None
        ok = meta.get("sha256") == self._pack["sha256"] and self._path.exists()
        return meta if ok else None

    def install(self) -> dict:
        """Fetch the pinned gzip through netguard, verify sha256, unpack atomically. Idempotent."""
        with self._lock:
            done = self.installed()
            if done:
                return done
            if not re.fullmatch(r"[0-9a-f]{64}", self._pack["sha256"]):
                raise LibraryIndexError("this build has no Library pack pinned")
            try:
                raw = self._net.safe_fetch_library_pack(self._pack["url"], MAX_PACK_GZ_BYTES)
            except netguard.FetchError:
                raise LibraryIndexError("couldn't download the Library — check your connection and retry") from None
            if hashlib.sha256(raw).hexdigest() != self._pack["sha256"]:
                raise LibraryIndexError("the downloaded Library didn't match this release's pinned hash — refused")
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._dir / f".library-{uuid.uuid4().hex}.tmp"
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as src, open(tmp, "wb") as dst:
                    total = 0
                    while chunk := src.read(1 << 20):
                        total += len(chunk)
                        if total > MAX_PACK_BYTES:
                            raise LibraryIndexError("the Library pack is larger than allowed — refused")
                        dst.write(chunk)
                os.replace(tmp, self._path)
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                raise LibraryIndexError(f"couldn't unpack the Library ({exc.__class__.__name__})") from None
            finally:
                tmp.unlink(missing_ok=True)
            meta = {"tag": self._pack["tag"], "sha256": self._pack["sha256"], "installed_at": _now()}
            self._meta.write_text(json.dumps(meta))
            self._taxonomy_cache = self._subcats_cache = self._league_aliases = self._has_result = None
            self._has_route_asks_cache = None
            self._takes_cache = self._own_cache = None
            self._entity_vocab_cache = {}
            self._provider_names_cache = None
            return meta

    def _conn(self) -> duckdb.DuckDBPyConnection:
        if not self.installed():
            raise LibraryIndexError("the Library isn't installed yet")
        return duckdb.connect(str(self._path), read_only=True)

    # --- reads ---------------------------------------------------------------------------------

    def status(self) -> dict:
        meta = self.installed()
        if not meta:
            return {"installed": False, "tag": self._pack["tag"]}
        with self._conn() as con:
            kv = dict(con.execute("SELECT key, value FROM library_meta").fetchall())
            by_status = dict(con.execute(
                "SELECT validation_status, count(*) FROM library_sources GROUP BY 1").fetchall())
        return {"installed": True, "tag": meta["tag"], "records": int(kv.get("records", 0)),
                "built_at": kv.get("built_at", ""), "by_status": by_status}

    def taxonomy(self) -> list[dict]:
        """Categories -> subcategories with source counts (failed/refused sources not counted)."""
        if self._taxonomy_cache is not None:
            return self._taxonomy_cache
        with self._conn() as con:
            rows = con.execute("""
                SELECT t.category, t.subcategory, t.label, t.keywords,
                       (SELECT count(*) FROM library_source_categories c JOIN library_sources s ON s.id = c.source_id
                        WHERE c.category = t.category AND c.subcategory = t.subcategory
                          AND s.validation_status NOT IN ('failed', 'refused'))
                FROM library_taxonomy t ORDER BY rowid""").fetchall()
        cats: dict[str, dict] = {}
        for cat, sub, label, keywords, n in rows:
            cat_label, _, sub_label = label.partition(" › ")
            c = cats.setdefault(cat, {"id": cat, "label": cat_label, "count": 0, "subcategories": []})
            c["subcategories"].append({"id": sub, "label": sub_label, "count": int(n), "keywords": list(keywords)})
            c["count"] += int(n)
        self._taxonomy_cache = list(cats.values())
        return self._taxonomy_cache

    @staticmethod
    def _expand_short_words(con, text: str) -> str:
        """People type short forms ("temp", "precip", "humid"): a word the Library doesn't know, of 4+
        letters, becomes the most common Library term it begins ("temperature"). Never a place,
        team, ticker or other resolver name, never an English filler word, never a rare term."""
        from .library_resolve import ENGLISH as ENGLISH_WORDS
        words = []
        for word in (text or "").split():  # bounded by the ask length
            low = word.lower().strip(".,?!:;'\"")
            base = low[:-1] if low.endswith("s") and len(low) > 4 else low
            if len(low) >= 4 and low.isalpha() and low not in ENGLISH_WORDS \
                    and not con.execute("SELECT 1 FROM library_terms WHERE term IN (?, ?) LIMIT 1",
                                        [low, base]).fetchone() \
                    and not con.execute("SELECT 1 FROM library_resolver_aliases WHERE alias IN (?, ?) LIMIT 1",
                                        [low, base]).fetchone():
                row = con.execute("SELECT term, count(*) AS n FROM library_terms WHERE term LIKE ? "
                                  "GROUP BY term ORDER BY n DESC, term LIMIT 1", [base + "%"]).fetchone()
                if row and row[1] >= 5 and row[0].isalpha():
                    word = row[0]
            words.append(word)
        text = " ".join(words)
        # a symbol written with a $ ("$pltr"), or a listed stock symbol typed as the whole ask ("aapl", "jpm"), is
        # written as one — unless the word is an everyday or Library word ("dow" is the index, "news" is news)
        text = re.sub(r"\$([A-Za-z]{1,5})\b", lambda m: m.group(1).upper(), text)
        only = text.split()
        if len(only) == 1 and 2 <= len(only[0]) <= 5 and only[0].isalpha() and only[0].islower() \
                and only[0] not in ENGLISH_WORDS and con.execute(
                    "SELECT 1 FROM library_resolver_entries WHERE resolver = 'ticker' AND key = ? LIMIT 1",
                    [only[0].upper()]).fetchone() and not con.execute(
                    "SELECT 1 FROM (SELECT unnest(keywords) AS k FROM library_taxonomy) "
                    "WHERE ' ' || k || ' ' LIKE '% ' || ? || ' %' LIMIT 1", [only[0]]).fetchone():
            text = only[0].upper()
        return text

    def classify(self, text: str, limit: int = 3) -> list[str]:
        low = " " + re.sub(r"[^a-z0-9.&+ ]+", " ", (text or "").lower()) + " "
        # "scores" says the keyword "score"; "raining" says "rain"
        folded = " ".join(w[:-3] if len(w) > 5 and w.endswith("ing") else _fold(w) for w in low.split())
        low += f" | {folded} "
        matched: list[tuple[str, str]] = []
        for c in self.taxonomy():
            for s in c["subcategories"]:
                matched += [(kw, f"{c['id']}/{s['id']}") for kw in s["keywords"]
                            if f" {kw} " in low or (len(kw) > 5 and kw in low)]
        # the longest match wins: "temp" inside a matched "water temp" says nothing about weather
        said = {kw for kw, _ in matched}
        counts: dict[str, int] = {}
        for kw, cid in matched:  # bounded by the taxonomy's keywords
            if not any(kw != longer and f" {kw} " in f" {longer} " for longer in said):
                counts[cid] = counts.get(cid, 0) + 1
        scored = sorted(counts.items(), key=lambda x: -x[1])
        return [cid for cid, _ in scored[:limit]]

    def search(self, q: str = "", category: str = "", subcategory: str = "", tier: str = "", status: str = "",
               offset: int = 0, limit: int = 20, hint: dict | None = None) -> dict:
        """Ranked sources. With ``q``: term relevance + category match + prior; without: prior only."""
        limit = max(1, min(limit, MAX_PAGE))
        offset = max(0, offset)
        where, args = ["TRUE"], []
        if category:
            where.append("s.id IN (SELECT source_id FROM library_source_categories WHERE category = ?"
                         + (" AND subcategory = ?" if subcategory else "") + ")")
            args += [category] + ([subcategory] if subcategory else [])
        if tier in TIERS:
            where.append("s.tier = ?")
            args.append(tier)
        if status in STATUSES:
            where.append("s.validation_status = ?")
            args.append(status)
        else:  # by default the page never lists sources we know are broken
            where.append("s.validation_status NOT IN ('failed', 'refused')")
        cond = " AND ".join(where)
        with self._conn() as con:
            q = self._expand_short_words(con, q)
            terms = tokens(q)
            if terms:
                ranked = self._ranked(con, self._context(con, q, hint), cond, args)
            else:
                # browsing: reviewed sources first, then in taxonomy order (weather, hazards, water...),
                # then by quality, so the page opens on a spread of everyday needs, not an alphabet
                ranked = con.execute(
                    f"SELECT s.id, s.prior FROM library_sources s WHERE {cond} ORDER BY "
                    "(s.tier IN ('curated', 'provider_trusted')) DESC, "
                    "(SELECT min(t.rowid) FROM library_source_categories c JOIN library_taxonomy t "
                    " ON t.category = c.category AND t.subcategory = c.subcategory WHERE c.source_id = s.id), "
                    "s.prior DESC, s.name", args).fetchall()
            total = len(ranked)
            page = [r[0] for r in ranked[offset:offset + limit]]
            rows = self._rows(con, page)
        return {"total": total, "offset": offset, "results": [rows[sid] for sid in page]}

    @staticmethod
    def _rows(con, ids: list[str]) -> dict[str, dict]:
        """The listing row of each source id."""
        if not ids:
            return {}
        cats: dict[str, list[str]] = {}
        for sid, cat, sub in con.execute("SELECT source_id, category, subcategory FROM library_source_categories "
                                         "WHERE source_id IN (SELECT unnest(?))", [ids]).fetchall():
            cats.setdefault(sid, []).append(f"{cat}/{sub}")
        return {r[0]: {"id": r[0], "name": r[1], "description": r[2], "provider": r[3], "authority": r[4],
                       "tier": r[5], "geo": r[6], "access_kind": r[7], "auth": r[8], "terms": r[9],
                       "cadence": r[10], "status": r[11], "categories": cats.get(r[0], []), "kinds": list(r[12] or [])}
                for r in con.execute(
                    "SELECT id, name, description, provider_name, authority, tier, geo, access_kind, auth, "
                    "terms_status, cadence, validation_status, kinds FROM library_sources "
                    "WHERE id IN (SELECT unnest(?))", [ids]).fetchall()}

    @staticmethod
    def _ranked(con, ctx: dict, cond: str, args: list) -> list[tuple]:
        """The keyword lookup's ranked (id, score) rows for an ask's context, under ``cond``."""
        return con.execute(_LOOKUP_SQL.format(where=cond),
                           [ctx["terms"], ctx["cats"], ctx["prefer"], ctx["prefer"], ctx["entities"], ctx["geo"],
                            ctx["audiences"], f" {ctx['asked']} ", ctx["kinds"]] + args).fetchall()

    def _context(self, con, ask: str, hint: dict | None = None, route: dict | None = None) -> dict:
        """The ask's frame: category (the embedding ``route`` when it is confident, else the ask's keywords when
        there is no route; then the intent's subject and wants, else what a named entity is), the subcategory's
        policy, the question kind (code's parse, else the intent's), named subjects, a named place (a parameter,
        not a relevance word) and audience — the Library's lookup, ported."""
        from .library_resolve import Resolver, norm
        res = Resolver(con)
        hint = hint or {}
        frame = frame_kind_from_text(ask) or (hint.get("frame_kind") if hint.get("frame_kind") in FRAME_KINDS else None)
        pwords, has_place, place_status = named_place(res, ask, hint.get("place"))
        found, said_by, spelled = self._entities(con, res, ask, frame, pwords)
        # L1: an entity whose only said words ARE the ask's place is not the subject on any path, unless
        # the ask spells its code or an ask word is in its cue set. "ISS passes over Denver" is not the
        # Rockies ({denver} ⊆ {denver}, no team cue said); "Chiefs score in Kansas City" is the Chiefs
        # (said={chiefs} is not place; team_espn cue "score" said). Applied every path (routed or not).
        if pwords:
            words_said = {t for t in tokens(ask)}
            for r in list(found):
                span = {w for a in said_by.get(r) or [] for w in a.split()}
                if not span or not (span <= pwords) or r in spelled:
                    continue
                cue = ENTITY_CUES.get("team_espn" if r.startswith("team_") else r) or set()
                if words_said & cue:
                    continue
                found.pop(r)
                said_by.pop(r)
        # fix10 (blind-8, 2026-10-04): "Durham Bulls schedule" shipped the Chicago Bulls' next matchup
        # — the ask names a place word next to a team nickname and the resolved team's own city doesn't
        # contain it. A team entity whose name resolves (via the pack's place resolver) to a state
        # different from the asked place's state is not the asked team. Keeps the asked city's team
        # ("Chicago Bulls", "LA Lakers", "New York Rangers") because the entity's name itself holds the
        # asked place-words; "Texas Rangers" vs "NY Rangers" disambiguation still fires on state.
        if pwords and has_place:
            from .library_resolve import norm as _norm_text
            asked_state = _place_state_of(res, " ".join(pwords))
            for r in list(found):
                if not r.startswith("team_") or r in spelled:
                    continue
                name_words = set(_norm_text(found[r].get("name") or "").split())
                if pwords & name_words:
                    continue  # the team's own name holds the asked place
                entity_state = _place_state_of(res, found[r].get("name") or "")
                if asked_state and entity_state and asked_state != entity_state:
                    found.pop(r)
                    said_by.pop(r)
        if route is not None and not route["confident"]:
            # a weak route names no category, but it still rules out readings: one whose every subcategory trails
            # the top route by the confident gap or more is not the subject ("sunset in Denver" is not the Rockies,
            # a "heat wave" not the Miami Heat), unless the ask spells its code or says its kind
            far = {sub for sub, d in route["behind"].items() if d >= route["threshold"]}
            for r in list(found):  # bounded by the entity resolvers
                subs = {sub for sub in self._subcategories(con) if r in self._takes(con, sub)}
                cue = ENTITY_CUES.get("team_espn" if r.startswith("team_") else r) or set()
                typed = r in spelled or any(w in cue for a in said_by[r] for w in a.split())
                if subs and subs <= far and not typed:
                    found.pop(r)
                    said_by.pop(r)
        cats, cats_from = self._frame_cats(con, ask, hint, set(found), frame, route, pwords)
        if cats_from != "entity":
            # L9: when a route is active, allow entities from BOTH the route's cats and the ask's keyword
            # classify (an airport entity the top route doesn't take must not be dropped when "airport
            # delay" is a keyword hit — faa-nas-status had B-max 0.837 for "any delays at boston logan").
            extra_cats = [c for c in self.classify(ask) if route is not None and c not in cats]
            ask_words = set(norm(ask).split())
            found = self._allowed(con, cats + extra_cats, found, said_by, spelled, ask_words)
            # an entity the ask spells by code outside what its words asked is the frame ("delays at ORD" is
            # airport delays, not transit alerts); one said only by the place's words is already filtered out
            # above (L1), so the check here guards taxonomy routing, not place-only readings.
            takes = set().union(*(self._takes(con, c) for c in cats))
            outside = {r for r in found if r not in takes and not {w for a in said_by[r] for w in a.split()} <= pwords}
            if outside:
                own = self._implied_cats(con, outside, frame, ask)
                cats = own[:1] + [c for c in cats if c not in own[:1]]
        # a place said only as part of a longer named subject of the ask is that subject ("premier league table"
        # is no League City; a bare "Seattle" stays a place), unless the intent says it is the place
        if pwords and any(pwords < set(a.split()) for r in found for a in said_by[r]) \
                and not pwords & set(norm(hint.get("place") or "").split()):
            pwords, has_place, place_status = set(), False, "none"
        pol = self._subcategories(con).get(cats[0], ([], {}))[1] if cats else {}
        terms = [t for t in tokens(ask) if t not in pwords] or tokens(ask)
        if cats_from == "hint":  # the intent's words say what the ask is about
            terms += [t for t in tokens(" ".join(str(w) for w in hint.get("wants") or [])) if t not in terms]
            terms += [t for t in tokens(str(hint.get("subject") or "")) if t not in terms]
        leagues = {lg for lg in (_league_of(e) for e in found.values()) if lg}
        # the ask's words that named its category (membership answers for them); None: the intent vouched
        explained = self._explained(cats[0], ask) if cats and cats_from == "ask" else None
        return {"terms": terms, "cats": cats, "prefer": pol.get("prefer", []), "match": pol.get("match", ""),
                "subject_resolvers": self._takes(con, cats[0]) if cats else set(), "entities": list(found),
                "found": found, "said": said_by, "leagues": leagues, "explained": explained, "routed": route is not None,
                "geo": list(GEO_RESOLVERS) if has_place else [], "place_words": pwords, "has_place": has_place,
                "place_status": place_status, "audiences": audiences(ask), "kinds": question_kinds(ask),
                "frame": frame, "asked": norm(ask),
                # the sources the ask names, when only their names framed it: the ask is about those
                "named": set(self._named_sources(con, ask, pwords)) if cats_from == "name" else set()}

    def _entities(self, con, res, ask: str, frame: str | None,
                  pwords: set[str]) -> tuple[dict[str, dict], dict[str, list[str]], set[str]]:
        """The subjects the ask names: each resolver's reading, kept when its full name is said and a cue word
        (never a word of that name itself), its own code or symbol, or an event question about it says the
        ask means it. Returns (resolver -> entry, resolver -> the names said, resolvers spelled by code)."""
        from .library_resolve import ENGLISH as ENGLISH_WORDS
        from .library_resolve import norm
        low, words = f" {norm(ask)} ", set(norm(ask).split())
        # a code that names the ask's place ("NYC weather") is the place, not a ticker, unless a cue says so
        codes = set(re.findall(r"\b[A-Z]{3,5}\b", ask or "")) - {w.upper() for w in pwords}
        # an acronym that names a data provider (NASA, NOAA, USGS, FAA) is that provider, not a ticker
        codes = {c.lower() for c in codes if not con.execute(
            "SELECT 1 FROM library_sources WHERE provider_name = ? OR provider_name LIKE ? LIMIT 1",
            [c, f"%({c})%"]).fetchone() and not con.execute(
            "SELECT 1 FROM library_sources WHERE regexp_matches(provider_name, ?) LIMIT 1",
            [rf"\b{c}\b"]).fetchone()}
        found: dict[str, dict] = {}
        said_by: dict[str, list[str]] = {}
        spelled: set[str] = set()  # entities the ask names by their own code ("delays at ORD")
        uncued: dict[str, tuple[dict, list[str]]] = {}
        for r in ENTITY_RESOLVERS:
            m = res.by_name(r, ask)
            if m["status"] == "none":
                continue
            best = m["best"] or (m["candidates"] or [{}])[0]
            kind = "team_espn" if r.startswith("team_") else r
            cue = ENTITY_CUES.get(kind)
            said = _aliases_in(res, best["id"], low) if best.get("id") else []
            code = {str(best.get("key") or "").lower(), str((best.get("attrs") or {}).get("symbol") or "").lower()}
            typed = (kind in ("ticker", "crypto", "currency") and bool(codes & code)) or kind == "zip"
            known = (kind == "crypto" and (best.get("rank") or 0) > 0 and norm(best.get("name", "")) in norm(ask)
                     and norm(best.get("name", "")) not in self._vocabulary_outside("markets/crypto"))
            if not said and not typed:
                # only part of a name matched: "crypto" is not The Crypto Dog — unless another reading names
                # those words in full (below)
                uncued[r] = (best, _aliases_in(res, best["id"], low, partial=True)) if best.get("id") else ({}, [])
                continue
            # a cue is never a word of the matched name itself ("nasdaq" doesn't cue Nasdaq, Inc.; a symbol
            # said, "BTC", still does), and a name that is only cue or everyday words ("market") is not a company
            name_words = {w for a in said for w in a.split()} - code
            generic = bool(name_words) and name_words <= (cue or set()) | ENGLISH_WORDS
            # an event question about a team is its cue ("how did the Cubs do")
            asked = kind == "team_espn" and frame in ("result", "next_event", "schedule", "ranking")
            cued = cue is None or asked or (not generic and bool((words & cue) - name_words))
            if not (typed or known or cued):
                if words & (cue or set()) & name_words:  # its cue is inside its own name ("Champions League")
                    uncued[r] = (best, said)
                continue
            found[r], said_by[r] = best, said
            if (typed and kind != "zip") or re.search(rf"\b{re.escape(best.get('key') or '#')}\b", ask or ""):
                spelled.add(r)
        # a reading cued only by its own name, or said only by part of its name, is the same thing as another
        # reading of those words that is named ("Champions League": a league, and a soccer competition)
        for r, (best, said) in uncued.items():
            if said and any(set(said) & set(said_by[o]) for o in list(found)):
                found[r], said_by[r] = best, said
        if "crypto" in found and "ticker" in found and not (found["crypto"].get("rank") or 0) > 0:
            found.pop("crypto")
        if "ticker" in found and "crypto" in found and (found["crypto"].get("rank") or 0) > 0 \
                and not found["ticker"].get("attrs", {}).get("sp500"):
            found.pop("ticker")
        # a reading named by fewer words than another's is not named ("Miami Heat" is not the Marlins' "Miami")
        spans = {r: {w for a in said_by[r] for w in a.split()} for r in found}
        for r in [r for r in found if any(spans[r] < spans[o] for o in found if o != r)]:
            found.pop(r)
        # a named league is the subject: a team of another league is not ("MLB wild card" is not the Wild)
        league = _league_of(found.get("sports_league"))
        for r in [r for r in found if r.startswith("team_") and league and _league_of(found[r]) != league]:
            found.pop(r)
        return found, said_by, spelled

    def _frame_cats(self, con, ask: str, hint: dict, found: set[str], frame: str | None,
                    route: dict | None = None, place_words: set[str] | None = None) -> tuple[list[str], str]:
        """The asked subcategories and where they came from ('ask', 'hint', 'entity' or 'name'). With an
        embedding ``route`` that is confident, the route is the category; a weak one leaves it to the ask's
        keywords. When nothing else names one, a reviewed source the ask names by its own name does. The kind
        decides between sibling subcategories: "next Dodgers game" is a schedule, not a score."""
        if route is not None and route["confident"]:
            cats, cats_from = [route["route"]], "ask"
        else:  # no route, or a weak one: the ask's keywords (with vectors, evidence of relevance, never a gate)
            cats, cats_from = self.classify(ask), "ask"
        if not cats and hint:
            cats, cats_from = self.classify(" ".join([str(hint.get("subject") or ""),
                                                      *(str(w) for w in hint.get("wants") or [])])), "hint"
        if not cats and found:
            cats, cats_from = self._implied_cats(con, found, frame, ask), "entity"
        if not cats:
            cats, cats_from = self._named_source_cats(con, ask, place_words or set()), "name"
        subs = self._subcategories(con)
        if frame and cats and not self._serves(con, frame) & set(subs.get(cats[0], ([], {}))[0]):
            top = cats[0].split("/")[0]
            fits = [c for c in cats + self._implied_cats(con, found, frame, ask) if c.startswith(f"{top}/")
                    and self._serves(con, frame) & set(subs.get(c, ([], {}))[0])]
            if fits:
                cats = [fits[0]] + [c for c in cats if c != fits[0]]
        return cats, cats_from

    def _named_sources(self, con, ask: str, place_words: set[str]) -> list[str]:
        """The reviewed sources the ask names by their own name: every word the user said (the place aside)
        is a word of the source's name — "NASA picture of the day" is NASA's Astronomy Picture of the Day,
        though no category keyword is said (live 2026-10-04: the keyword path failed closed on it)."""
        if self._names_cache is None:
            self._names_cache = [(sid, {_fold(t) for t in tokens(name)}) for sid, name in con.execute(
                "SELECT id, name FROM library_sources WHERE tier <> 'harvested' AND coalesce(role, '') <> 'helper' "
                "AND validation_status NOT IN ('failed', 'refused') ORDER BY prior DESC, id").fetchall()]
        said = {_fold(t) for t in tokens(ask) if t not in place_words}
        return [sid for sid, words in self._names_cache if said and said <= words]  # bounded by the pack

    def _named_source_cats(self, con, ask: str, place_words: set[str]) -> list[str]:
        """The subcategories of the sources the ask names (``_named_sources``), theirs in filing order."""
        named = self._named_sources(con, ask, place_words)
        cats = [f"{c}/{sub}" for sid in named for c, sub in con.execute(
            "SELECT category, subcategory FROM library_source_categories WHERE source_id = ? ORDER BY rowid",
            [sid]).fetchall()]
        return list(dict.fromkeys(cats))[:3]

    def _allowed(self, con, cats: list[str], found: dict[str, dict], said_by: dict[str, list[str]],
                 spelled: set[str], ask_words: set[str] = frozenset()) -> dict[str, dict]:
        """An entity the asked subcategories don't take is not the subject ("DC metro" is no airport) — unless
        the ask spells its code or a name that says its kind, its kind of data sits beside what was asked, and
        nothing the asked subcategories take reads the same words ("delays at ORD" and "delays at Newark
        airport" are the airport; "USD" is the currency). An ask-level cue word outside the entity's own name
        counts too (fix7-page 2026-10-04: "delays at O'Hare" said "delays", an airport cue — the entity said_by
        span is just the name "o hare", so the pre-fix check missed it and the FAA source wasn't offered). An
        entity named ONLY by a word that is a sibling category's own keyword ("Mesquite Metro Airport" for "DC
        metro red line delays": "metro" is a transit keyword) is the sibling's subject, not this one."""
        takes = set().union(*(self._takes(con, c) for c in cats))
        if not takes:
            return found
        top = cats[0].split("/")[0]
        near = set().union(*(self._takes(con, c) for c in self._subcategories(con) if c.startswith(f"{top}/")))
        cat_vocab = {_fold(w) for c in cats for kw in self._keywords(c) for w in kw.split()}

        def typed(r: str) -> bool:
            cue = ENTITY_CUES.get("team_espn" if r.startswith("team_") else r) or set()
            if r in spelled:
                return True
            name_words = {w for a in said_by[r] for w in a.split()}
            if any(w in cue for w in name_words):
                return True
            if not (ask_words & cue) - name_words:
                return False
            # Guard the ask-level cue bypass against entities named only by their
            # short lowercase code (airport "RED" matching Mifflin on "red"): a
            # single said token under 5 chars isn't a real name mention, and the
            # ask never spelled the code in capitals.
            if len(name_words) < 2 and all(len(w) < 5 for w in name_words):
                return False
            # Guard it against entities whose SAID name sits inside a sibling
            # category's vocabulary ("metro" said → Mesquite Metro Airport, but
            # "metro" is a transit_alerts keyword).
            return not (cat_vocab and name_words
                        and {_fold(w) for w in name_words} <= cat_vocab)
        return {r: e for r, e in found.items() if r in takes or (
            typed(r) and r in near and not any(o in takes and set(said_by[o]) & set(said_by[r])
                                               for o in found if o != r))}

    def _explained(self, sub: str, ask: str) -> set[str]:
        """The ask's words that name the subcategory (its keywords said), plurals folded."""
        low = " " + re.sub(r"[^a-z0-9.&+ ]+", " ", (ask or "").lower()) + " "
        low += " | " + " ".join(_fold(w) for w in low.split()) + " "
        return {_fold(w) for kw in self._keywords(sub) if f" {kw} " in low or (len(kw) > 5 and kw in low) for w in kw.split()}

    def _subcategories(self, con) -> dict[str, tuple[list[str], dict]]:
        """Each subcategory's declared kinds and policy, in taxonomy order."""
        if self._subcats_cache is None:
            self._subcats_cache = {
                f"{c}/{s}": (list(k or []), json.loads(p) if p else {})
                for c, s, k, p in con.execute("SELECT category, subcategory, kinds, policy FROM library_taxonomy "
                                              "ORDER BY rowid").fetchall()}
        return self._subcats_cache

    def _takes(self, con, sub: str) -> set[str]:
        """The resolvers a subcategory is about: its policy's, and those its own sources take."""
        if self._takes_cache is None:
            self._takes_cache = {}
            for cid, r in con.execute("SELECT DISTINCT c.category || '/' || c.subcategory, r.resolver FROM "
                                      "library_source_categories c JOIN library_source_resolvers r "
                                      "ON r.source_id = c.source_id").fetchall():
                self._takes_cache.setdefault(cid, set()).add(r)
        pol = self._subcategories(con).get(sub, ([], {}))[1]
        return set(pol.get("resolvers") or []) | self._takes_cache.get(sub, set())

    def _implied_cats(self, con, resolvers: set[str], frame: str | None, ask: str = "") -> list[str]:
        """No keyword named the category, but a named entity does: the subcategories that take it — those
        whose kinds serve the frame first ("when do the Cubs play" -> schedules, not scores), then those whose
        keywords share the ask's words ("delays at ORD" -> airport delays), then taxonomy order."""
        said = {_fold(w) for w in re.findall(r"[a-z0-9]+", (ask or "").lower())}
        serves = self._serves(con, frame) if frame else set()
        subs = [(sid, kinds) for sid, (kinds, _pol) in self._subcategories(con).items()
                if resolvers & self._takes(con, sid)]
        subs.sort(key=lambda s: (frame is not None and not serves & set(s[1]),
                                 -len(said & {_fold(w) for kw in self._keywords(s[0]) for w in kw.split()})))
        return [sid for sid, _ in subs[:3]]

    def _keywords(self, sub: str) -> list[str]:
        cat, _, subcat = sub.partition("/")
        return next((sc["keywords"] for c in self.taxonomy() if c["id"] == cat for sc in c["subcategories"]
                     if sc["id"] == subcat), [])

    def _serves(self, con, frame: str) -> set[str]:
        """The source kinds that can serve ``frame``. Until the pack declares a ``result`` kind, a result
        frame also takes a current value (live and final scores are filed as current values)."""
        if self._has_result is None:
            self._has_result = bool(con.execute(
                "SELECT 1 FROM library_sources WHERE list_contains(kinds, 'result') LIMIT 1").fetchone())
        return _SERVES[frame] | ({"current_value"} if frame == "result" and not self._has_result else set())

    def _has_route_asks(self) -> bool:
        """True when the installed pack carries a ``library_route_asks`` table with at least one row —
        locate v2's vectors are inert without it, so a pack that lacks it skips the sidecar build (L8)."""
        assert self._pack is not None, "pack metadata required"
        if self._has_route_asks_cache is None:
            try:
                with self._conn() as con:
                    row = con.execute("SELECT 1 FROM library_route_asks LIMIT 1").fetchone()
                self._has_route_asks_cache = row is not None
            except (duckdb.CatalogException, LibraryIndexError):
                self._has_route_asks_cache = False
        return bool(self._has_route_asks_cache)

    def _leagues_named(self, con, text: str) -> set[str]:
        """The sports leagues a text names (by the league resolver's own names)."""
        from .library_resolve import norm
        low = f" {norm(text)} "
        return {lg for a, lg in self._load_league_aliases(con) if lg and f" {a} " in low}

    def _vocabulary_outside(self, subcategory: str) -> set[str]:
        out: set[str] = set()
        for c in self.taxonomy():
            for sc in c["subcategories"]:
                if f"{c['id']}/{sc['id']}" != subcategory:
                    for kw in sc["keywords"]:
                        out.update(kw.lower().split())
        return out

    def entity_vocabulary(self, entity: str) -> set[str]:
        """Tokens the ``entity``'s domain names (a league / provider's own words). For a sports
        league entity ("MLB", "NFL"): the sports_league aliases of that league plus every team
        alias whose ``attrs.league`` matches it (team_mlb, team_nhl, team_espn) — the ask may name
        a team of that league without the source having to list each one. {} for a non-league
        entity: the fix6-rows F7-C class fix (2026-10-04) relies on readings / own words to cover
        those; sports leagues need the resolver because a league-wide source lists divisions, not
        teams.
        """
        assert isinstance(entity, str), "entity must be a string"
        from .library_resolve import norm
        text = norm(entity).strip()
        if not text:
            return set()
        if text in self._entity_vocab_cache:
            return self._entity_vocab_cache[text]
        with self._conn() as con:
            league = next((lg for a, lg in self._load_league_aliases(con) if lg and a == text), "")
            if not league:
                self._entity_vocab_cache[text] = set()
                return set()
            rows = con.execute(
                "SELECT a.alias, e.attrs FROM library_resolver_aliases a "
                "JOIN library_resolver_entries e ON e.id = a.entry_id "
                "WHERE e.resolver IN ('sports_league','team_mlb','team_nhl','team_espn') AND NOT a.partial"
            ).fetchall()
        out: set[str] = set()
        for alias, attrs in rows:  # bounded by the pack's team aliases
            attr_lg = _league_of({"attrs": json.loads(attrs) if attrs else {}})
            if attr_lg == league:
                out |= {t for t in re.findall(r"[a-z0-9]+", str(alias).lower()) if len(t) >= 2}
        self._entity_vocab_cache[text] = out
        return out

    def _load_league_aliases(self, con) -> list[tuple[str, str]]:
        """The sports_league alias → league pairs, cached (shared with ``_leagues_named``)."""
        from .library_resolve import ENGLISH as ENGLISH_WORDS
        if self._league_aliases is None:
            self._league_aliases = [
                (a, _league_of({"attrs": json.loads(attrs) if attrs else {}}))
                for a, attrs in con.execute(
                    "SELECT a.alias, e.attrs FROM library_resolver_aliases a JOIN library_resolver_entries e "
                    "ON e.id = a.entry_id WHERE e.resolver = 'sports_league' AND NOT a.partial").fetchall()
                if len(a) >= 3 and a not in ENGLISH_WORDS]
        return self._league_aliases

    def providers_named_in(self, request: str) -> set[str]:
        """The Library provider names (case-folded, bounded by the pack's providers) that appear
        as a whole-phrase substring of ``request`` (case-folded). An outlet named ("Fox News",
        "Associated Press", "NPR") the intent's ``names`` blank missed is still caught here —
        every pack provider gives a vocabulary the stray-topic check can consult. {} when the
        Library isn't installed or holds no providers.

        fix7-lib (2026-10-04): "reuters business news" shipped from NPR Business because the
        intent's model didn't flag "reuters" as a proper name — the deterministic backstop in
        ``_stray_topics`` reads this vocabulary so an ask that names a provider the pick isn't
        from refuses.
        """
        assert isinstance(request, str), "request must be a string"
        low = " " + " ".join(re.findall(r"[A-Za-z0-9]+", request.lower())) + " "
        if not low.strip():
            return set()
        names = self._provider_names()
        out: set[str] = set()
        for name, phrase in names:  # bounded by the pack's distinct providers
            if phrase in low:
                out.add(name)
        return out

    @staticmethod
    def _provider_phrase(name: str) -> str | None:
        """The phrase an ask must hold to name this provider: its WHOLE folded name (every part,
        short ones too), or None when every part is a generic word ("US Weather", "News") — such a
        name names no outlet, and a partial match ("weather" for "HG Weather") is no mention."""
        tokens = re.findall(r"[A-Za-z0-9]+", (name or "").lower())
        if not tokens or all(t in _GENERIC_PROVIDER_WORDS for t in tokens):
            return None
        return " " + " ".join(tokens) + " "

    def _provider_names(self) -> list[tuple[str, str]]:
        """Cached (``name``, " ".join(folded_tokens) " ") pairs for every distinct provider name
        with at least one folded token of length ≥ 3 (so "Fox News" rides but a single stop like
        "a" doesn't bind on every ask). The second element is the pre-wrapped phrase the lookup
        matches against ``" <folded-request> "``.
        """
        if self._provider_names_cache is not None:
            return self._provider_names_cache
        with self._conn() as con:
            rows = con.execute(
                "SELECT DISTINCT provider_name FROM library_sources "
                "WHERE provider_name IS NOT NULL AND provider_name <> ''").fetchall()
        pairs: list[tuple[str, str]] = []
        for (name,) in rows:  # bounded by the pack's distinct providers
            phrase = self._provider_phrase(name or "")
            if phrase:
                pairs.append((name, phrase))
        self._provider_names_cache = pairs
        return pairs

    @staticmethod
    def _place_words(res, ask: str) -> tuple[set[str], bool]:
        words, named, _status = named_place(res, ask)
        return words, named

    def candidates(self, ask: str, limit: int = 3, hint: dict | None = None) -> tuple[list[dict], list[str]]:
        """Up to ``limit`` consentable Library candidates for the card flow (§29 stage 2, locate v2).

        With the Library's vectors (an embedder is wired and they are built): the reviewed sources ranked by the
        keyword lookup fused with the embedding ranking, boosted toward the ask's embedding route. A source is
        offered when nothing it can't do rules it out (a format a card can't read, failed or refused, a question
        kind it doesn't give, a place, named subject or parameter the ask doesn't fill) and it is about the ask:
        it takes the named subject, it is at least ``library_embed.FLOOR`` similar to the ask, or its own words
        (or membership of a confident route's category, for a place-matched one) answer for what the user named.
        A weak route names no category and restricts nothing; nothing about the ask -> no candidate.

        Without them (no embedder, or not built yet), WP1's frame: a category is established from the ask's
        keywords (else the intent's hint, else a named entity) or locate fails closed ("couldn't tell what kind of
        data this is"); the source must be filed under the asked top category and pass the same relevance on its
        own whole words; where the category is about places, it must take the named place.

        ``hint`` is the intent's frame ({subject, wants, place, frame_kind, window}). An ambiguous entity gives one
        candidate per reading. Each candidate carries ``scope`` ('place' when it is for the named place, else
        'global'), ``frame_kind`` and the ``lookup`` chain a same-host helper fills after consent. Also returns why
        each ranked source passed over was not offered."""
        from .library_resolve import ENGLISH as ENGLISH_WORDS
        from .library_resolve import Resolver, candidate_urls, norm
        out, skipped = [], []
        subject_ids: set[str] = set()
        # the kind is read from the words as typed (a short-word expansion can turn "passes" into another word)
        hint = {**(hint or {}), "frame_kind": frame_kind_from_text(ask) or (hint or {}).get("frame_kind")}
        meta = self.installed()
        # L8: skip the sidecar build/load entirely when the pack has no route asks (v2 is inert without
        # a router) — otherwise every first use would embed ~4.8k cards+asks and every ask would embed a
        # query for a path that discards them.
        got = library_embed.ready(self._dir, meta["sha256"], self._conn) \
            if meta and self._has_route_asks() else None
        q = library_embed.embed_query(got[1], ask) if got else None
        vectors = got[0] if got and q is not None else None
        # L7: a query of a different dimension to the sidecar's vectors would raise ValueError on the
        # first matmul (route/dense/bmax). Treat it as the embedder having changed under us — drop to
        # the keyword path for this ask; the next ready() call will rebuild under the new scheme.
        if vectors is not None and (q.ndim != 1 or vectors.cards.shape[0] == 0
                                    or vectors.cards.shape[1] != q.shape[0]):
            log.info("library_embed: query dim %s does not match sidecar dim %s; falling back",
                     None if q is None else q.shape, vectors.cards.shape)
            vectors = None
        with self._conn() as con:
            ask = self._expand_short_words(con, ask)
            route = vectors.route(q) if vectors is not None else None
            if route is None:  # a pack without route asks: the keyword frame, as without vectors
                vectors = None
            ctx = self._context(con, ask, hint, route)
            if vectors is not None:
                keyword = [sid for sid, _score in self._ranked(con, ctx, "s.tier <> 'harvested'", [])]
                ranked = vectors.fuse(keyword, vectors.dense(q), route)
                near = vectors.similar(q)
                if route is not None and route["confident"]:
                    # the confident route's member nearest the ask is about it ONLY when it also clears the
                    # similarity FLOOR: without a floor every ask admits its route's nearest member whatever
                    # the match ("price of a used honda civic" admitted bestbuy at B-max 0.393) — L2
                    bmax = vectors.bmax(q)
                    members = [i for i, c in enumerate(vectors.cats) if route["route"] in c]
                    if members:
                        top = max(members, key=lambda i: bmax[i])
                        if bmax[top] >= library_embed.FLOOR:
                            near.add(vectors.ids[top])
            elif not ctx["cats"]:
                return [], ["couldn't tell what kind of data this is"]
            else:
                ranked, near = [r["id"] for r in self.search(ask, limit=12, hint=hint)["results"]], set()
            rows = self._rows(con, ranked)
            records = {sid: json.loads(con.execute("SELECT record FROM library_sources WHERE id = ?",
                                                   [sid]).fetchone()[0]) for sid in rows}
            res = Resolver(con)
            fill_ask = self._fill_ask(res, ask, hint.get("place"), ctx["place_words"])
            phrases = [kw for c in self.taxonomy() for sc in c["subcategories"] for kw in sc["keywords"] if " " in kw]
            # the words that say WHAT the user wants: real words, not state codes, ZIPs, clitics, filler or the place
            need = {w for w in norm(ask).split() if w not in ENGLISH_WORDS and w not in _FILLER and len(w) >= 2
                    and not w.isdigit() and w.upper() not in _STATE_CODES and w not in ctx["place_words"]}
            if vectors is not None:
                # with no category gate, a named subject's own name says nothing about a source that doesn't take
                # it ("Bills score" is not Congress's bills)
                need -= {w for names in ctx["said"].values() for a in names for w in a.split()}
            # the words the user named beyond the category's own that name one of its reviewed members (its
            # name or declared coverage): they tell the members apart ("bart" in "BART delays"); "blizzard"
            # doesn't
            left = set() if ctx["explained"] is None else {w for w in need if _fold(w) not in ctx["explained"]}
            ctx["telling"] = {w for w in left if any(
                ctx["cats"][0] in rows[s]["categories"] and rows[s]["tier"] != "harvested" and _speaks_to(
                    {w}, str((records[s].get("coverage") or {}).get("entity") or ""), [], ask, rows[s]["name"])
                for s in rows)}
            # the ask's distinguishing words (not category keywords, not place): _off_words needs this to
            # tell "nothing distinguishing was said" (membership is fine) from "distinguishing words were
            # said but no member names any of them" (routed membership isn't evidence)
            ctx["left"] = left
            for sid in ranked:  # bounded by two rankings of RRF_DEPTH, or the keyword page
                row = rows.get(sid)
                if row is None:
                    continue
                record = records[sid]
                why, about = self._off_frame(con, row, record, ctx)
                if not why and ctx["named"] and sid not in ctx["named"]:
                    why = "not the source the ask names"
                # relevance: a source about the named subject is. Without vectors (WP1), its own words must name
                # something the user named, unless the ask names only everyday words (the category gate decides).
                # With them, one near the ask is; a member of the asked category passes as WP1's does; any other
                # (no category gate stops it) must name every RARE word that says what the user wants, and the ask
                # must have one ("pollen count" is not a download count; "activity" is anyone's word)
                if why or about or sid in near:
                    pass
                elif vectors is None or (ctx["cats"] and ctx["cats"][0] in row["categories"]):
                    why = self._off_words(row, record, ctx, need, phrases, ask) if need else ""
                elif not (rare := {w for w in need if self._sources_saying(con, w) <= RARE_WORD_SOURCES}) \
                        or not all(self._says(row, record, {w}, phrases, ask) for w in rare):
                    why = "not about what was asked"
                urls: list[dict] = []
                if not why:
                    cats = record.get("categories") or []
                    pol = self._subcategories(con).get(cats[0], ([], {}))[1] if cats else {}
                    # R3-A (field 2026-10-04): pass the engine's clock so clock-filled URLs read the
                    # user's zone (not the server's) and the sealed URL template rides through.
                    urls, why = candidate_urls(record, fill_ask, pol, res, now=ni._clock())
                if not urls:
                    skipped.append(f"{row['name']}: {why}")
                    continue
                if record.get("tier") == "harvested" and any(o["tier"] != "harvested" for o in out):
                    continue  # reviewed sources first; a harvested dataset only when none fits
                if about:
                    subject_ids.add(record["id"])
                scope = "place" if ctx["has_place"] and self._serves_place(con, record, ctx["place_words"]) \
                    else "global"
                for u in urls:
                    out.append({"source_id": record["id"], "tier": record.get("tier", ""), "categories": cats,
                                "title": record["name"], "provider": record["provider"]["name"],
                                "authority": record["provider"].get("authority", ""), "url": u["url"],
                                "host": urlsplit(u["url"]).hostname or "", "label": u["label"], "choice": u["choice"],
                                "status": row["status"],
                                "format": _FORMAT_BY_KIND.get(row["access_kind"], "json"),
                                "needs_key": u.get("needs_key"), "params": u.get("params") or {},
                                "needs_contact": bool(u.get("needs_contact")), "lookup": u.get("lookup"),
                                # R3-A (field 2026-10-04): the sealed clock-template URL + its
                                # clock-fill metadata ride through so the engine refills every tick
                                # (seal was deriving a template from the filled values, mis-reading
                                # year codes and URL-encoded separators).
                                "url_template": u.get("url_template") or u["url"],
                                "clock_params": u.get("clock_params") or {},
                                "scope": scope, "frame_kind": ctx["frame"]})
                # keyed sources are gathered too, but enough keyless ones end the search (for a named place,
                # enough keyless ones FOR that place: a place-scoped source may rank below global ones)
                if sum(1 for c in out if not c["needs_key"] and (c["scope"] == "place" or not ctx["has_place"])) \
                        >= limit:
                    break
        return self._order(out, ctx, subject_ids)[:limit], skipped

    def _off_frame(self, con, row: dict, record: dict, ctx: dict) -> tuple[str, bool]:
        """Why a ranked source can't answer the ask ('' when it can), and whether it is about the ask's named
        subject (takes it, or declares it)."""
        if row["access_kind"] not in _FLOW_KINDS:
            return "not a format a card can read yet", False
        if row["status"] in ("failed", "refused"):
            return f"{row['status']} when last checked", False
        cats = set(row["categories"])
        if not ctx["routed"] and ctx["cats"][0].split("/")[0] not in {c.split("/")[0] for c in cats}:
            return "a different kind of data", False
        kinds = set(row.get("kinds") or [])
        serves = self._serves(con, ctx["frame"]) if ctx["frame"] else set()
        if ctx["frame"] == "forecast" and re.search(r"\boutlooks?\b", ctx["asked"]):
            serves |= {"text_brief"}  # an outlook is a written forecast ("nhc tropical outlook")
        if ctx["frame"] and not serves & kinds:
            return (f"gives {' / '.join(sorted(k.replace('_', ' ') for k in kinds)) or 'no declared kind'}, "
                    f"not {ctx['frame'].replace('_', ' ')}"), False
        # locate and fill agree on the place: a source that needs one is offered only when the ask names one
        if not ctx["has_place"] and _needs_place(record):
            return "name the place (a city, town or ZIP)", False
        # a named place is part of the frame: where the category is about places, the source must take one
        if ctx["has_place"] and ctx["cats"] and ctx["match"] not in ("none", "name") \
                and not self._serves_place(con, record, ctx["place_words"]):
            return "not for a particular place", False
        aud = (con.execute("SELECT audience FROM library_sources WHERE id = ?", [row["id"]]).fetchone() or [""])[0]
        if not ctx["routed"] and aud and aud not in set(ctx["audiences"]) and not cats & set(ctx["cats"]):
            return f"for {aud} users", False  # unless it is filed under what was asked (a buoy reports surf)
        takes = {r for (r,) in con.execute("SELECT resolver FROM library_source_resolvers WHERE source_id = ?",
                                           [row["id"]]).fetchall()}
        why, about = self._off_subject(con, record, takes, ctx)
        # L1: on a routed path, an "about" admission must not bypass relevance when the source is in a
        # different top category than the route's — the ask said what kind of data, which outranks the
        # entity reading. The caller still admits a highly-similar source via ``near``.
        if about and ctx["routed"] and ctx["cats"]:
            top = ctx["cats"][0].split("/")[0]
            if top not in {c.split("/")[0] for c in cats}:
                about = False
        return why, about

    def _off_subject(self, con, record: dict, takes: set[str], ctx: dict) -> tuple[str, bool]:
        """The named subject (a team, ticker, league, airport…) must be one the source can take or is about."""
        from .library_resolve import norm
        found = ctx["found"]
        for p in (record.get("access") or {}).get("params") or []:  # bounded by the record's params
            fill = p.get("fill") or {}
            r = fill.get("resolver")
            if fill.get("from") == "resolver" and r in ENTITY_RESOLVERS and r not in found and not fill.get("fallback"):
                return f"the ask doesn't name a {r.replace('_', ' ')}", False
        declared = str((record.get("coverage") or {}).get("entity") or "") or str(record.get("name") or "")
        leagues = self._leagues_named(con, declared) if ctx["leagues"] else set()
        if leagues and not leagues & ctx["leagues"]:
            return f"about {', '.join(sorted(leagues)).upper()}, not {', '.join(sorted(ctx['leagues'])).upper()}", False
        covers = bool(leagues)  # a league-wide source covers that league's teams
        own = f" {norm(' '.join([str(record.get('name') or ''), str(record.get('description') or ''), declared]))} "
        span = {r: {w for a in ctx["said"].get(r) or [] for w in a.split()} for r in found}
        for r, e in found.items():
            # taking any reading of the same words is taking the subject ("premier league": a league or a
            # soccer competition)
            same = {o for o in found if o == r or span[o] & span[r]}
            if r in ctx["subject_resolvers"] and not takes & same and not covers \
                    and not any(f" {a} " in own for a in ctx["said"].get(r) or [norm(e.get("name", ""))]):
                return f"not about {e.get('name', r)}", False
        return "", bool(takes & set(found)) or covers

    def _off_words(self, row: dict, record: dict, ctx: dict, need: set[str], phrases: list[str], ask: str) -> str:
        """Relevance: a source filed in the asked subcategory passes on membership where the category is about
        places; otherwise its OWN words (name, description, examples, declared answers) must name something the
        user named — never its category's vocabulary: "gold" is a commodities word, and WTI crude is a
        commodities source, but WTI crude is not about gold (live 2026-09-29)."""
        if row["tier"] == "harvested":
            # harvested classification is keyword-drafted: the dataset itself must name every distinctive
            # word of the ask (a restaurant-inspection set is not "egg prices")
            return "" if all(_speaks_to({w}, row["description"], phrases, ask, row["name"]) for w in need) \
                else "not about what was asked"
        if ctx["cats"] and ctx["match"] not in ("none", "name") and ctx["cats"][0] in row["categories"]:
            # a member answers for what the user named, except a word that names one of its siblings: "BART
            # delays" is not the CTA's alerts, but "blizzard warning" is every alerts source's
            reduced = need & ctx["telling"]
            if not reduced:
                # L2: on a routed path, if the ask has distinguishing words (``ctx['left']``) that NO member
                # of the asked category names, membership alone is not evidence ("price of a used honda
                # civic" admitted bestbuy because every member passed telling-empty). When ``left`` is empty
                # too (the ask named only the category's own keyword, "temp in Charleston"), membership
                # still answers.
                if ctx["routed"] and ctx.get("left"):
                    return "not about what was asked"
                return ""
            need = reduced
        text, declared = _own_words(record)
        return "" if _speaks_to(need, text, phrases + declared, ask, row["name"]) else "not about what was asked"

    @staticmethod
    def _fill_ask(res, ask: str, hint_place: str | None, place_words: set[str]) -> str:
        """The ask the parameters fill from: when the intent's place is the one the ask says, other place readings
        are taken out of it, so the fill can't pick them ("Lake Michigan water temperature in Chicago" fills
        Chicago, not Lake Michigan Beach, MI)."""
        from .library_resolve import norm
        low = f" {norm(ask)} "
        if not hint_place or not place_words or not _said_place(res, hint_place, low)[0]:
            return ask
        r = res.by_name("place", ask)
        for c in ([r["best"]] if r["best"] else []) + list(r["candidates"] or []):  # bounded by the readings
            nm = norm(c["name"])
            said = nm if f" {nm} " in low else next(iter(_aliases_in(res, c["id"], low)), "")
            if said and not set(said.split()) <= place_words:
                ask = re.sub(r"(?i)\b" + r"\W+".join(map(re.escape, said.split())) + r"\b", " ", ask)
                low = f" {norm(ask)} "
        return " ".join(ask.split())

    def _sources_saying(self, con, word: str) -> int:
        """How many offerable reviewed sources' own words (and names) name ``word``, plurals folded."""
        from .library_resolve import norm
        if self._own_cache is None:
            self._own_cache = {}
            for sid, raw in con.execute("SELECT id, record FROM library_sources WHERE tier <> 'harvested' AND "
                                        "role <> 'helper' AND validation_status NOT IN ('failed', 'refused')"
                                        ).fetchall():  # bounded by the pack
                rec = json.loads(raw)
                self._own_cache[sid] = {_fold(w) for w in norm(_own_words(rec)[0] + " " + str(rec.get("name") or "")
                                                               ).split()}
        w = _fold(word)
        return sum(1 for words in self._own_cache.values() if w in words)

    @staticmethod
    def _says(row: dict, record: dict, need: set[str], phrases: list[str], ask: str) -> bool:
        """Do the source's own words (name, description, examples, declared answers) name a word of ``need``?"""
        text, declared = _own_words(record)
        return _speaks_to(need, text, phrases + declared, ask, row["name"])

    def _order(self, out: list[dict], ctx: dict, subject_ids: set[str]) -> list[dict]:
        # when something answers the asked subcategory exactly (tides, not water temperature), or takes the named
        # subject and serves the stated kind of question, offer only those
        exact = [c for c in out if (ctx["cats"] and ctx["cats"][0] in (c.get("categories") or []))
                 or (ctx["frame"] and c["source_id"] in subject_ids)]
        ordered = list(exact or out)
        ordered.sort(key=lambda c: c["scope"] != "place")  # stable: sources for the named place lead
        # among sources for the same named subject, the policy's authority order leads (MLB's own schedule
        # before a community one), whatever their example words weigh
        rank = {a: i for i, a in enumerate(ctx["prefer"])}
        slots = [i for i, c in enumerate(ordered) if c["source_id"] in subject_ids]
        for i, c in zip(slots, sorted((ordered[i] for i in slots), key=lambda c: rank.get(c["authority"], len(rank))),
                        strict=True):
            ordered[i] = c
        # the BEST source leads even when it needs the user's key (the card asks for it); a keyless source
        # goes ahead of it only when it is the same kind of source (shares its category) — "better if the
        # best source doesn't need a key", never a worse fit just because it is keyless
        if ordered and ordered[0]["needs_key"]:
            top_cats = set(ordered[0].get("categories") or [])
            same = next((c for c in ordered if not c["needs_key"] and top_cats & set(c.get("categories") or [])),
                        None)
            if same is not None:
                ordered.remove(same)
                ordered.insert(0, same)
        return ordered

    @staticmethod
    def _serves_place(con, record: dict, place_words: set[str]) -> bool:
        """Is the source for a particular place: it takes one (a geo resolver, or a helper chained from the
        place), or its declared entity names the named place."""
        takes = con.execute("SELECT count(*) FROM library_source_resolvers WHERE source_id = ? AND resolver IN "
                            "(SELECT unnest(?::VARCHAR[]))", [record["id"], list(GEO_RESOLVERS)]).fetchone()[0]
        if takes or _needs_place(record):
            return True
        entity = (con.execute("SELECT entity || ' ' || name FROM library_sources WHERE id = ?",
                              [record["id"]]).fetchone() or [""])[0].lower()
        return bool(place_words) and all(w in entity for w in place_words)

    def subcategory(self, sub: str) -> dict:
        """A taxonomy subcategory's ``kinds``, ``policy`` and ``expects`` (what its answers should report), or {}
        when the pack has no such subcategory."""
        with self._conn() as con:
            got = self._subcategories(con).get(sub)
        if got is None:
            return {}
        kinds, policy = got
        return {"kinds": list(kinds), "policy": dict(policy), "expects": list(policy.get("expects") or [])}

    def official_hosts(self, subject: str) -> dict[str, list[str]]:
        """{name said: [its official hosts]} for each organisation the subject names (whole words), from the pack's
        ``official_site`` resolver; {} when the pack has none or the subject names none."""
        from .library_resolve import norm
        low = f" {norm(subject)} "
        if not low.strip():
            return {}
        with self._conn() as con:
            try:
                rows = con.execute(
                    "SELECT a.alias, e.attrs FROM library_resolver_aliases a JOIN library_resolver_entries e "
                    "ON e.id = a.entry_id WHERE e.resolver = 'official_site' AND NOT a.partial "
                    "AND strpos(?, ' ' || a.alias || ' ') > 0", [low]).fetchall()
            except duckdb.Error:  # an older pack without the resolver tables' columns
                return {}
        out: dict[str, list[str]] = {}
        for alias, attrs in rows:  # bounded by the official_site entries
            hosts = [str(h) for h in (json.loads(attrs) if attrs else {}).get("domains") or [] if h]
            if hosts:
                out.setdefault(alias, [])
                out[alias] += [h for h in hosts if h not in out[alias]]
        return out

    def get(self, source_id: str) -> dict | None:
        with self._conn() as con:
            row = con.execute("SELECT record FROM library_sources WHERE id = ?", [source_id]).fetchone()
        return None if row is None else json.loads(row[0])

    def answers(self, source_id: str) -> list[dict]:
        """The record's declared ``answers`` (which response paths answer which questions), or []."""
        rec = self.get(source_id) if source_id else None
        got = (rec or {}).get("answers")
        return [a for a in got if isinstance(a, dict)] if isinstance(got, list) else []


# --- local sources (user data, sealed) ---------------------------------------------------------

def validate_local(body: dict, known_categories: set[str]) -> dict:
    """Build a tier-``local`` source record from the Add-a-source form, or raise ValueError.

    The same rules the Library's CI enforces: https, public host, no credential in the URL, declared
    params, known category. The host must also pass netguard's SSRF pre-check.
    """
    name = str(body.get("name") or "").strip()
    url = str(body.get("url") or "").strip()
    desc = str(body.get("description") or "").strip()
    category = str(body.get("category") or "")
    kind = str(body.get("access_kind") or "http_json")
    needs_key = bool(body.get("needs_key"))
    if not 2 <= len(name) <= 120:
        raise ValueError("Give the source a name (2–120 characters).")
    if len(desc) > 600:
        raise ValueError("Keep the description under 600 characters.")
    if category not in known_categories:
        raise ValueError("Choose a category.")
    if kind not in ACCESS_KINDS:
        raise ValueError("Choose the data format.")
    if len(url) > 2000 or not url.startswith("https://"):
        raise ValueError("The address must start with https://")
    if _SECRETISH.search(url) and "{" not in _SECRETISH.search(url).group(2):
        raise ValueError("Don't put a key in the address — tick “needs a key” and SmartBrain will ask for it.")
    host = urlsplit(url).hostname or ""
    if "{" in host or not host or "." not in host:
        raise ValueError("The address needs a real public host name.")
    try:
        netguard.validate_public_url(_PARAM.sub("x", url))
    except netguard.FetchError:
        raise ValueError("That address isn't a public internet host.") from None
    params = [{"name": n, "kind": "key" if n == "key" else "none", "example": None, "required": True}
              for n in dict.fromkeys(_PARAM.findall(url))]
    if needs_key and not any(p["name"] == "key" for p in params):
        params.append({"name": "key", "kind": "key", "example": None, "required": True})
    return {
        "id": "local-" + uuid.uuid4().hex[:12], "name": name, "description": desc,
        "provider": {"id": "local", "name": host, "url": f"https://{host}", "authority": "community"},
        "tier": "local", "categories": [category], "kinds": ["lookup"],
        "coverage": {"geo": "local", "entity": ""},
        "access": {"kind": kind, "url_template": url, "params": params,
                   "auth": "free_key" if needs_key else "none", "headers": {}, "docs_url": url},
        "terms": {"status": "unverified", "note": "added by you", "terms_url": ""},
        "freshness": {"cadence": "irregular"}, "examples": [], "notes": "",
        "origin": {"by": "user", "at": _now()}, "validation": {"status": "unvalidated"},
        "votes": {"yes": 0, "no": 0},
    }


class LocalSources:
    """The user's own sources, sealed under NIStore's reserved snapshot row."""

    def __init__(self, ni_store: ni.NIStore) -> None:
        assert ni_store is not None, "NIStore required"
        self._ni = ni_store

    def list(self) -> list[dict]:
        row = self._ni.read_reserved_snapshot(LOCAL_RESERVED_ID, LOCAL_SLOT)
        return list(row["payload"].get("sources", [])) if row else []

    def add(self, record: dict) -> dict:
        rows = self.list()
        if len(rows) >= MAX_LOCAL_SOURCES:
            raise ValueError(f"You can keep up to {MAX_LOCAL_SOURCES} of your own sources.")
        if any(r["access"]["url_template"] == record["access"]["url_template"] for r in rows):
            raise ValueError("You already added that address.")
        rows.append(record)
        self._ni.write_reserved_snapshot(LOCAL_RESERVED_ID, LOCAL_SLOT, {"sources": rows})
        return record

    def record_yes(self, source_id: str) -> int:
        """R6: the user said Yes to a Library source — count it (sealed, this device only)."""
        assert source_id, "source_id required"
        row = self._ni.read_reserved_snapshot(LOCAL_RESERVED_ID, VOTES_SLOT)
        votes = dict(row["payload"].get("yes", {})) if row else {}
        entry = dict(votes.get(source_id) or {"count": 0})
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last"] = _now()
        votes[source_id] = entry
        if len(votes) > MAX_LOCAL_SOURCES * 4:  # bounded: keep the most recent
            votes = dict(sorted(votes.items(), key=lambda kv: kv[1].get("last", ""))[-MAX_LOCAL_SOURCES * 4:])
        self._ni.write_reserved_snapshot(LOCAL_RESERVED_ID, VOTES_SLOT, {"yes": votes})
        return entry["count"]

    def yes_votes(self) -> dict[str, dict]:
        row = self._ni.read_reserved_snapshot(LOCAL_RESERVED_ID, VOTES_SLOT)
        return dict(row["payload"].get("yes", {})) if row else {}

    def delete(self, source_id: str) -> bool:
        rows = self.list()
        keep = [r for r in rows if r["id"] != source_id]
        if len(keep) == len(rows):
            return False
        self._ni.write_reserved_snapshot(LOCAL_RESERVED_ID, LOCAL_SLOT, {"sources": keep})
        return True

    def search(self, q: str, category: str = "", subcategory: str = "") -> list[dict]:
        want = set(tokens(q))
        out = []
        for r in self.list():
            if category and not any(c.startswith(f"{category}/{subcategory}" if subcategory else f"{category}/")
                                    for c in r["categories"]):
                continue
            text = set(tokens(" ".join([r["name"], r["description"], r["provider"]["name"]])))
            if want and not (want & text):
                continue
            out.append(r)
        return out
