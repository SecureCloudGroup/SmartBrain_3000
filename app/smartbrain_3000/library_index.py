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

from . import netguard, ni

log = logging.getLogger(__name__)

# --- the pinned pack (ruling R12: the app release pins the exact bytes) ----------------------------
PACK = {
    "tag": "v1.2.0",
    "url": "https://github.com/SecureCloudGroup/SmartBrain_Library/releases/download/v1.2.0/library.duckdb.gz",
    "sha256": "ddfb0f40bcd469073c0ccd7c5d3f351f13a9a7d91b506ccaf3713f712eb91d63",
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
cand AS (SELECT source_id FROM ent
         UNION SELECT source_id FROM hits WHERE NOT EXISTS (SELECT 1 FROM ent))
SELECT s.id,
       coalesce(h.rel / (SELECT max(rel) FROM hits), 0) * 4
         * (CASE s.tier WHEN 'harvested' THEN 0.7 WHEN 'provider_trusted' THEN 0.9 ELSE 1.0 END)
         + coalesce(c.cb, 0) + s.prior
         - (CASE WHEN s.audience <> '' AND NOT list_contains(?::VARCHAR[], s.audience) THEN 1.5 ELSE 0 END)
         + coalesce((SELECT 0.6 - 0.3 * (p.pos - 1) FROM pref p WHERE p.authority = s.authority), 0)
         + (CASE WHEN s.id IN (SELECT source_id FROM ent) THEN 3.5 ELSE 0 END)
         + (CASE WHEN s.id IN (SELECT source_id FROM geo) THEN 1.5 ELSE 0 END)
         + (CASE WHEN list_has_any(s.kinds, ?::VARCHAR[]) THEN 1.0 ELSE 0 END) AS score
FROM cand JOIN library_sources s ON s.id = cand.source_id LEFT JOIN hits h ON h.source_id = s.id
     LEFT JOIN catb c ON c.source_id = s.id
WHERE s.role <> 'helper' AND {where}
ORDER BY score DESC, (s.auth <> 'none'), s.prior DESC, s.id"""  # ties: keyless first, then the likelier

# --- ranking context (a port of SmartBrain_Library sourcetool/build.py; the Library's eval sets are the contract)
ENTITY_RESOLVERS = ("team_mlb", "team_nhl", "team_espn", "ticker", "crypto", "currency", "airport", "statuspage",
                    "soccer_competition", "fr_agency", "spending_agency")  # a ZIP is a location, not a subject
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


def audiences(ask: str) -> list[str]:
    return [a for a, rx in AUDIENCE_CUES.items() if re.search(rx, (ask or "").lower())]


def _aliases_in(res, entry_id: str, low: str) -> list[str]:
    rows = res._con.execute("SELECT alias FROM library_resolver_aliases WHERE entry_id = ? AND NOT partial",
                            [entry_id]).fetchall()
    return sorted((a for (a,) in rows if f" {a} " in low), key=len, reverse=True)


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
            self._taxonomy_cache = None
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
        return " ".join(words)

    def classify(self, text: str, limit: int = 3) -> list[str]:
        low = " " + re.sub(r"[^a-z0-9.&+ ]+", " ", (text or "").lower()) + " "
        scored = []
        for c in self.taxonomy():
            for s in c["subcategories"]:
                hits = sum(1 for kw in s["keywords"] if f" {kw} " in low or (len(kw) > 5 and kw in low))
                if hits:
                    scored.append((hits, f"{c['id']}/{s['id']}"))
        scored.sort(key=lambda x: -x[0])
        return [cid for _, cid in scored[:limit]]

    def search(self, q: str = "", category: str = "", subcategory: str = "", tier: str = "", status: str = "",
               offset: int = 0, limit: int = 20) -> dict:
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
                ctx = self._context(con, q)
                sql = _LOOKUP_SQL.format(where=cond)
                ranked = con.execute(sql, [ctx["terms"], ctx["cats"], ctx["prefer"], ctx["prefer"], ctx["entities"],
                                           ctx["geo"], ctx["audiences"], ctx["kinds"]] + args).fetchall()
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
            rows = {r[0]: r for r in con.execute(
                "SELECT id, name, description, provider_name, authority, tier, geo, access_kind, auth, "
                "terms_status, cadence, validation_status, kinds FROM library_sources WHERE id IN (SELECT unnest(?))",
                [page]).fetchall()} if page else {}
            cats = {}
            for sid, cat, sub in con.execute(
                    "SELECT source_id, category, subcategory FROM library_source_categories "
                    "WHERE source_id IN (SELECT unnest(?))", [page]).fetchall() if page else []:
                cats.setdefault(sid, []).append(f"{cat}/{sub}")
        results = []
        for sid in page:
            r = rows[sid]
            results.append({"id": r[0], "name": r[1], "description": r[2], "provider": r[3], "authority": r[4],
                            "tier": r[5], "geo": r[6], "access_kind": r[7], "auth": r[8], "terms": r[9],
                            "cadence": r[10], "status": r[11], "categories": cats.get(sid, []),
                            "kinds": list(r[12] or [])})
        return {"total": total, "offset": offset, "results": results}

    def _context(self, con, ask: str) -> dict:
        """What the ask is about: category, the subcategory's trust order, named subjects, a location
        (a parameter, not a relevance word), question kind and audience — the Library's lookup, ported."""
        from .library_resolve import Resolver, norm
        res = Resolver(con)
        cats = self.classify(ask)
        prefer, geo_policy = [], False
        if cats:
            row = con.execute("SELECT policy FROM library_taxonomy WHERE category = ? AND subcategory = ?",
                              [*cats[0].split("/", 1)]).fetchone()
            pol = json.loads(row[0]) if row and row[0] else {}
            prefer, geo_policy = pol.get("prefer", []), pol.get("match") == "geo"
        words = set(norm(ask).split())
        # a code that names the ask's place ("NYC weather") is the place, not a ticker, unless a cue says so
        codes = set(re.findall(r"\b[A-Z]{3,5}\b", ask or "")) - {w.upper() for w in self._place_words(res, ask)[0]}
        # an acronym that names a data provider (NASA, NOAA, USGS, FAA) is that provider, not a ticker
        codes = {c for c in codes if not con.execute(
            "SELECT 1 FROM library_sources WHERE provider_name = ? OR provider_name LIKE ? LIMIT 1",
            [c, f"%({c})%"]).fetchone() and not con.execute(
            "SELECT 1 FROM library_sources WHERE regexp_matches(provider_name, ?) LIMIT 1",
            [rf"\b{c}\b"]).fetchone()}
        found: dict[str, dict] = {}
        for r in ENTITY_RESOLVERS:
            m = res.by_name(r, ask)
            if m["status"] == "none":
                continue
            best = m["best"] or (m["candidates"] or [{}])[0]
            kind = "team_espn" if r.startswith("team_") else r
            cue = ENTITY_CUES.get(kind)
            typed = (best.get("key", "").upper() in codes and kind in ("ticker", "crypto", "currency")) or kind == "zip"
            known = (kind == "crypto" and (best.get("rank") or 0) > 0 and norm(best.get("name", "")) in norm(ask)
                     and norm(best.get("name", "")) not in self._vocabulary_outside("markets/crypto"))
            if cue is not None and not (words & cue) and not typed and not known:
                continue
            found[r] = best
        if "crypto" in found and "ticker" in found and not (found["crypto"].get("rank") or 0) > 0:
            found.pop("crypto")
        if "ticker" in found and "crypto" in found and (found["crypto"].get("rank") or 0) > 0 \
                and not found["ticker"].get("attrs", {}).get("sp500"):
            found.pop("ticker")
        pwords, has_place = self._place_words(res, ask)
        terms = [t for t in tokens(ask) if t not in pwords] or tokens(ask)
        return {"terms": terms, "cats": cats, "prefer": prefer, "entities": list(found),
                "geo": list(GEO_RESOLVERS) if (has_place and geo_policy) else [],
                "audiences": audiences(ask), "kinds": question_kinds(ask)}

    def _vocabulary_outside(self, subcategory: str) -> set[str]:
        out: set[str] = set()
        for c in self.taxonomy():
            for sc in c["subcategories"]:
                if f"{c['id']}/{sc['id']}" != subcategory:
                    for kw in sc["keywords"]:
                        out.update(kw.lower().split())
        return out

    @staticmethod
    def _place_words(res, ask: str) -> tuple[set[str], bool]:
        from .library_resolve import norm, states_in
        low = f" {norm(ask)} "
        if re.search(r"\b\d{5}\b", ask or ""):
            return set(), True
        st = res.by_name("us_state", ask)
        state_words = set(norm(st["best"]["name"]).split()) if st["status"] == "resolved" else set()
        r = res.by_name("place", ask)
        words: set[str] = set()
        if r["status"] != "none":
            for c in (r["candidates"] or [])[:3]:
                nm = norm(c["name"])
                said = nm if f" {nm} " in low else next((w for w in _aliases_in(res, c["id"], low)), "")
                if not said:
                    continue
                big = (c.get("attrs", {}).get("pop") or 0) >= 100_000
                prep = re.search(rf" (in|at|near|for|around|of) {re.escape(said)} ", low)
                if big or prep or states_in(ask):
                    words |= set(said.split())
        return words | state_words, bool(words or state_words)

    def candidates(self, ask: str, limit: int = 3) -> tuple[list[dict], list[str]]:
        """Up to ``limit`` consentable Library candidates for the card flow: the best-ranked sources whose
        parameters all fill from the ask (an ambiguous entity -> one candidate per reading). Also returns
        the honest reasons the top sources were skipped."""
        from .library_resolve import ENGLISH as ENGLISH_WORDS
        from .library_resolve import Resolver, candidate_urls, norm
        out, skipped = [], []
        with self._conn() as con:
            ask = self._expand_short_words(con, ask)
            ranked = self.search(ask, limit=12)["results"]
            res = Resolver(con)
            ctx = self._context(con, ask)
            asked_top = {ctx["cats"][0].split("/")[0]} if ctx["cats"] else set()
            subjects = set(ctx["entities"])
            # only STRONG kinds filter ("next", "trend", "top"…); "latest X" is often a current value
            kinds = set(ctx["kinds"]) & {"next_event", "trend", "ranking", "alerts", "status", "count"}
            audience = set(ctx["audiences"])
            # the words that say WHAT the user wants: real words, not state codes, ZIPs or clitics ("what's")
            distinctive = [w for w in norm(ask).split() if w not in ENGLISH_WORDS and len(w) >= 2
                           and not w.isdigit() and w.upper() not in _STATE_CODES]
            named_place = bool(ctx["geo"])
            place_words = self._place_words(res, ask)[0]
            for row in ranked:
                if row["access_kind"] not in _FLOW_KINDS or row["status"] in ("failed", "refused"):
                    continue
                # a candidate must be about what was asked: the asked category, the asked kind of question,
                # and — when a place is named — able to take a location or be about that place
                if asked_top and not asked_top & {c.split("/")[0] for c in row["categories"]}:
                    continue
                if kinds and not kinds & set(row.get("kinds") or []):
                    continue
                if named_place and not self._serves_place(con, row["id"], place_words):
                    continue
                aud = (con.execute("SELECT audience FROM library_sources WHERE id = ?", [row["id"]]).fetchone()
                       or [""])[0]
                if aud and aud not in audience:
                    continue  # an aviation or marine source only when the ask speaks to that audience
                need = [w for w in distinctive if w not in place_words]
                takes_subject = bool(subjects) and bool(con.execute(
                    "SELECT count(*) FROM library_source_resolvers WHERE source_id = ? AND resolver IN "
                    "(SELECT unnest(?::VARCHAR[]))", [row["id"], list(subjects)]).fetchone()[0])
                if need and not takes_subject:
                    # what the source is about: its own words plus its categories' vocabulary
                    kw = " ".join(k for c in self.taxonomy() for sc in c["subcategories"]
                                  if f"{c['id']}/{sc['id']}" in row["categories"] for k in sc["keywords"])
                    rec_text = norm(" ".join([row["name"], row["description"], kw, " ".join(json.loads(
                        con.execute("SELECT record FROM library_sources WHERE id = ?", [row["id"]]).fetchone()[0]
                    ).get("examples", []))]))
                    if not any(w[:5] in rec_text for w in need):
                        continue  # nothing the user named is what this source is about
                if row["tier"] == "harvested":
                    # harvested classification is keyword-drafted: the dataset itself must name every
                    # distinctive word of the ask (a restaurant-inspection set is not "egg prices")
                    text = norm(f"{row['name']} {row['description']}")
                    need = [w for w in distinctive if w not in place_words]
                    if not need or not all(w[:5] in text for w in need):
                        continue
                rec = json.loads(con.execute("SELECT record FROM library_sources WHERE id = ?",
                                             [row["id"]]).fetchone()[0])
                cats = rec.get("categories") or []
                pol = {}
                if cats:
                    prow = con.execute("SELECT policy FROM library_taxonomy WHERE category = ? AND subcategory = ?",
                                       [*cats[0].split("/", 1)]).fetchone()
                    pol = json.loads(prow[0]) if prow and prow[0] else {}
                urls, why = candidate_urls(rec, ask, pol, res)
                if not urls:
                    skipped.append(f"{rec['name']}: {why}")
                    continue
                if rec.get("tier") == "harvested" and any(o["tier"] != "harvested" for o in out):
                    continue  # reviewed sources first; a harvested dataset only when none fits
                for u in urls:
                    out.append({"source_id": rec["id"], "tier": rec.get("tier", ""), "categories": rec.get("categories") or [], "title": rec["name"], "provider": rec["provider"]["name"],
                                "authority": rec["provider"].get("authority", ""), "url": u["url"],
                                "host": urlsplit(u["url"]).hostname or "", "label": u["label"], "choice": u["choice"],
                                "status": row["status"],
                                "format": _FORMAT_BY_KIND.get(row["access_kind"], "json"),
                                "needs_key": u.get("needs_key"), "params": u.get("params") or {},
                                "needs_contact": bool(u.get("needs_contact"))})
                if sum(1 for c in out if not c["needs_key"]) >= limit:
                    break  # keyed sources are gathered too, but enough keyless ones end the search
        # when something answers the asked subcategory exactly (tides, not water temperature), offer only those
        asked_sub = ctx["cats"][0] if ctx["cats"] else ""
        exact = [c for c in out if asked_sub and asked_sub in (c.get("categories") or [])]
        # the BEST source leads even when it needs the user's key (the card asks for it); a keyless source
        # goes ahead of it only when it is the same kind of source (shares its category) — "better if the
        # best source doesn't need a key", never a worse fit just because it is keyless
        ordered = list(exact or out)
        if ordered and ordered[0]["needs_key"]:
            top_cats = set(ordered[0].get("categories") or [])
            same = next((c for c in ordered if not c["needs_key"] and top_cats & set(c.get("categories") or [])),
                        None)
            if same is not None:
                ordered.remove(same)
                ordered.insert(0, same)
        return ordered[:limit], skipped[:5]

    @staticmethod
    def _serves_place(con, source_id: str, place_words: set[str]) -> bool:
        takes = con.execute("SELECT count(*) FROM library_source_resolvers WHERE source_id = ? AND resolver IN "
                            "(SELECT unnest(?::VARCHAR[]))", [source_id, list(GEO_RESOLVERS)]).fetchone()[0]
        if takes:
            return True
        entity = (con.execute("SELECT entity || ' ' || name FROM library_sources WHERE id = ?",
                              [source_id]).fetchone() or [""])[0].lower()
        return bool(place_words) and all(w in entity for w in place_words)

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
