"""Library resolvers + fills in the app: the words of an ask -> a concrete, consentable source URL.

A faithful port of SmartBrain_Library's ``sourcetool/resolve.py`` (matcher) and ``fills.py`` (fill
semantics), reading the resolver tables the pinned pack ships (``library_resolver_entries`` /
``library_resolver_aliases``). Keep the two in step: the Library's evaluation sets are the contract.

Nothing here fetches. A candidate is offered only when every parameter can be filled from the ask, the
clock or a default — a key, another source's answer, a contact email or a named gap keeps it off the card
(it would need a fetch or a secret before the user's consent). An ambiguous entity becomes one candidate
per reading, so the user's tap IS the answer to the question.

One exception, sealed rather than fetched: a parameter filled from a keyless helper on the SAME host whose
own parameters fill from the ask (NWS: nws-points turns a place into the forecast office and grid). The
candidate carries the helper address as a ``lookup`` chain; the tap consents to both same-host fetches, and
``resolve_lookup`` runs the chain after consent. Code owns it — no model.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

MAX_NGRAM = 6
TIE = 0.75
DOMINANCE = 20
ATTR_CONTEXT = ("sport", "league", "exchange")
RANK_WEIGHT = {"team_espn": 1.0, "team_mlb": 1.0, "team_nhl": 1.0, "crypto": 0.004, "airport": 1.0,
               "ticker": 0.8, "tide_station": 0.1, "place": 0.0, "county": 0.0, "zip": 0.0}
MAX_CHOICES = 3  # an ambiguous entity offers at most this many readings

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico", "GU": "Guam", "VI": "U.S. Virgin Islands",
    "AS": "American Samoa", "MP": "Northern Mariana Islands",
}
_STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}

ENGLISH = frozenset(["a", "an", "the", "of", "in", "on", "at", "for", "to", "and", "or", "is", "are", "was", "be", "by", "with", "from", "as", "it", "its", "this", "that", "what", "whats", "how", "when", "where", "who", "which", "my", "me", "i", "show", "get", "give", "tell", "today", "now", "current", "latest", "near", "about", "into", "per", "vs", "next", "last", "this", "week", "weekend", "month", "year", "tonight", "tomorrow", "yesterday", "price", "prices", "stock", "stocks", "score", "scores", "game", "games", "team", "schedule", "weather", "forecast", "status", "down", "up", "is", "are", "open", "closed", "news", "report", "delays", "delay", "rate", "rates", "level", "levels", "air", "quality", "index", "time", "times", "tide", "tides", "high", "low", "sunset", "sunrise", "map", "chart", "live", "new", "top", "best", "any", "all", "list", "value", "city", "town", "county", "state", "station", "buoy", "airport", "near", "around", "local", "home", "work", "traffic", "bus", "train", "subway", "flight", "flights", "day", "days", "daily", "hour", "hours", "hourly", "minute", "minutes", "week", "weekly", "month", "monthly", "year", "yearly", "date", "info", "information", "data", "update", "updates"])

GEO_NEAR = ("tide_station", "buoy", "radar_site", "nwps_gauge")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower().replace("&", " and "))).strip()


def states_in(ask: str, resolver: Resolver | None = None) -> set[str]:
    """The US states the ask names. A state inside a water body's name ("Lake Michigan", "Ohio River") is
    not one, when the pack has a water_body resolver to say so (field 2026-09-29: 'Lake Michigan water temp
    Milwaukee' read as Michigan and pushed Milwaukee WI down)."""
    found = {m for m in re.findall(r"\b([A-Z]{2})\b", ask or "") if m in US_STATES}
    low = f" {norm(ask)} "
    for span in (resolver.water_spans(ask) if resolver is not None else ()):
        low = low.replace(f" {span} ", " | ")
    found |= {code for name, code in _STATE_BY_NAME.items()
              if f" {name} " in low and not re.search(rf" {re.escape(name)} (city|beach|dunes|harbor)\b", low)}
    return found


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(min(1.0, a)))


def _row(r: tuple) -> dict:
    return {"id": r[0], "resolver": r[1], "kind": r[2], "key": r[3], "name": r[4], "lat": r[5], "lon": r[6],
            "state": r[7] or "", "attrs": json.loads(r[8]) if r[8] else {}, "rank": float(r[9] or 0)}


_ENTRY_COLS = "e.id, e.resolver, e.kind, e.key, e.name, e.lat, e.lon, e.state, e.attrs, e.rank"


class Resolver:
    """The one matcher over the pack's resolver tables (see the module docstring)."""

    def __init__(self, con) -> None:
        self._con = con
        self._water: dict[str, list[str]] = {}

    def water_spans(self, ask: str) -> list[str]:
        """The water-body names in the ask (multi-word aliases of the pack's water_body resolver, if any)."""
        if ask not in self._water:
            tokens = norm(ask).split()
            grams = {" ".join(tokens[i:j]) for i in range(len(tokens))
                     for j in range(i + 2, min(len(tokens), i + MAX_NGRAM) + 1)}
            rows = self._con.execute(
                "SELECT DISTINCT a.alias FROM library_resolver_aliases a JOIN library_resolver_entries e "
                "ON e.id = a.entry_id WHERE e.resolver = 'water_body' AND a.alias IN (SELECT unnest(?::VARCHAR[]))",
                [sorted(grams)]).fetchall() if grams else []
            self._water[ask] = sorted((r[0] for r in rows), key=len, reverse=True)
        return self._water[ask]

    def source_record(self, source_id: str) -> dict | None:
        """A pack record by id (a lookup helper another record fills from), or None."""
        row = self._con.execute("SELECT record FROM library_sources WHERE id = ?", [source_id]).fetchone()
        return json.loads(row[0]) if row else None

    def by_name(self, resolver: str, ask: str, *, many: bool = False) -> dict:
        tokens = norm(ask).split()
        grams = []
        for i in range(len(tokens)):
            for j in range(i + 1, min(len(tokens), i + MAX_NGRAM) + 1):
                grams.append((i, " ".join(tokens[i:j])))
        if not grams:
            return {"status": "none", "best": None, "candidates": [], "reason": "empty ask"}
        rows = self._con.execute(
            f"SELECT {_ENTRY_COLS}, a.alias, a.partial FROM library_resolver_aliases a "
            f"JOIN library_resolver_entries e ON e.id = a.entry_id "
            f"WHERE e.resolver = ? AND a.alias IN (SELECT unnest(?::VARCHAR[]))",
            [resolver, sorted({g for _, g in grams})]).fetchall()
        aliases_of: dict[str, list[str]] = {}
        entries: dict[str, dict] = {}
        by_alias: dict[str, list[tuple[str, bool]]] = {}
        for r in rows:
            e = entries.setdefault(r[0], _row(r))
            by_alias.setdefault(r[10], []).append((e["id"], bool(r[11])))
            aliases_of.setdefault(e["id"], []).append(r[10])
        upper = set(re.findall(r"\b[A-Z0-9$]{1,6}\b", ask or ""))
        states = states_in(ask, self)
        # a capitalised state code that is also a place's nickname ("LA", "DC") names that place only when
        # no other place is named: "LA weather" is Los Angeles, "Lafayette LA" is Lafayette, Louisiana
        codes = {c.lower() for c in re.findall(r"\b([A-Z]{2})\b", ask or "") if c in US_STATES} \
            if resolver == "place" else set()
        via_other: set[str] = set()  # candidates named by something other than a bare state code
        text = f" {' '.join(tokens)} "
        scored: dict[str, float] = {}
        first_at: dict[str, int] = {}
        for i, gram in grams:
            n = len(gram.split())
            spec = sum(1 for w in gram.split() if w not in ENGLISH) or 1
            if n == 1 and gram in ENGLISH:
                continue
            for eid, partial in by_alias.get(gram, []):
                e = entries[eid]
                if n == 1 and gram == norm(e["key"]) and len(gram) <= 5 and gram not in norm(e["name"]).split():
                    if e["kind"] == "ticker" and gram.upper() not in upper:
                        continue
                    if e["kind"] == "crypto_asset" and not e["rank"] and gram.upper() not in upper:
                        continue
                if n == 1 and e["kind"] == "crypto_asset" and gram != norm(e["name"]) and not e["rank"] \
                        and gram.upper() not in upper:
                    continue
                if n == 1 and e["kind"] == "place" and len(gram) <= 2 and gram.upper() not in upper:
                    continue  # a two-letter place nickname counts only in capitals ("LA", never the word "la")
                score = (2.0 * spec - (0.5 if partial else 0.0) + (0.5 if gram == norm(e["name"]) else 0.0)
                         + RANK_WEIGHT.get(resolver, 0.05) * e["rank"])
                said = states - {gram.upper()} if n == 1 and gram in codes else states
                if said:
                    score += 3.0 if e["state"] in said else (-3.0 if e["state"] else 0.0)
                if not (n == 1 and gram in codes):
                    via_other.add(eid)
                for k in ATTR_CONTEXT:
                    v = norm(str(e["attrs"].get(k) or ""))
                    if v and any(f" {w} " in text for w in {v, v.replace("college ", "")} if w):
                        score += 1.5
                scored[eid] = max(scored.get(eid, -1e9), score)
                first_at[eid] = min(first_at.get(eid, 99), i)
        if via_other and codes:  # another place is named, so the code is its state ("Lafayette LA")
            scored = {i: sc for i, sc in scored.items() if i in via_other}
        if not scored:
            return {"status": "none", "best": None, "candidates": [], "reason": f"nothing in {resolver} matches"}
        ranked = sorted(scored.items(), key=lambda kv: -kv[1])
        if many:
            seen, picks = set(), []
            for eid, sc in sorted(ranked, key=lambda kv: first_at[kv[0]]):
                nm = norm(entries[eid]["name"])
                if nm not in seen and sc >= ranked[0][1] - 2.0:
                    seen.add(nm)
                    picks.append(entries[eid])
            return {"status": "resolved", "best": picks[0], "candidates": picks, "reason": ""}
        best_score = ranked[0][1]
        all_ties = [entries[e] for e, s in ranked if best_score - s < TIE]
        ties = all_ties[:6]
        top = entries[ranked[0][0]]
        pops = sorted(((t["attrs"].get("pop") or 0, t) for t in all_ties), key=lambda x: -x[0])
        if len(pops) > 1 and pops[0][0] and pops[0][0] >= DOMINANCE * max(pops[1][0], 1):
            return {"status": "resolved", "best": pops[0][1], "candidates": ties, "reason": "largest of its name"}
        distinct = {(t["name"], t["state"], json.dumps(t["attrs"].get("league"))) for t in ties}
        same = len({norm(t["name"]) for t in ties}) == 1 and len({t["state"] for t in ties}) == 1
        if len(ties) > 1 and len(distinct) > 1 and not same:
            # offer the likeliest readings first: the largest places, the most popular teams/assets
            ties = sorted(all_ties, key=lambda t: (-(t["attrs"].get("pop") or 0), -t["rank"]))[:6]
            return {"status": "ambiguous", "best": None, "candidates": ties,
                    "reason": "several match equally: " + "; ".join(_label(t) for t in ties)}
        return {"status": "resolved", "best": top, "candidates": [entries[e] for e, _ in ranked[:5]], "reason": ""}

    def near(self, resolver: str, lat: float, lon: float, max_km: float,
             differ_on: tuple[str, ...] = (), measure: tuple[str, ...] = ()) -> dict:
        """The nearest entries within ``max_km``. With ``measure`` (the policy's requirement, e.g. WTMP for
        water temperature) and entries that declare ``attrs.measures``, only stations that report every
        one of them count: the nearest station that doesn't measure the asked thing is no answer."""
        dlat = max_km / 111.0 + 0.5
        dlon = max_km / (111.0 * max(0.2, math.cos(lat * math.pi / 180))) + 0.5
        rows = self._con.execute(
            f"SELECT {_ENTRY_COLS} FROM library_resolver_entries e WHERE e.resolver = ? "
            f"AND e.lat BETWEEN ? AND ? AND e.lon BETWEEN ? AND ?",
            [resolver, lat - dlat, lat + dlat, lon - dlon, lon + dlon]).fetchall()
        entries = [_row(r) for r in rows if r[5] is not None]
        what = resolver.replace("_", " ")
        if measure and any("measures" in e["attrs"] for e in entries):
            entries = [e for e in entries if set(measure) <= set(e["attrs"].get("measures") or ())]
            what = f"{what} that reports {'+'.join(measure)}"
        dist = sorted(((km(lat, lon, e["lat"], e["lon"]), e) for e in entries), key=lambda x: x[0])[:8]
        within = [(d, r) for d, r in dist if d <= max_km]
        if not within:
            return {"status": "none", "best": None, "candidates": [],
                    "reason": f"no {what} within {max_km:g} km"}
        d1, best = within[0]
        rivals = [(d, r) for d, r in within[1:] if d <= max(1.5 * d1, d1 + 5)]
        for _d, r in rivals:
            if any((r["attrs"].get(k) or "") != (best["attrs"].get(k) or "") for k in differ_on):
                cands = [best] + [x for _, x in rivals]
                return {"status": "ambiguous", "best": None, "candidates": cands,
                        "reason": "close choices differ: " + "; ".join(
                            f"{c['name']} ({km(lat, lon, c['lat'], c['lon']):.0f} km)" for c in cands)}
        return {"status": "resolved", "best": best, "candidates": [r for _, r in within[:5]],
                "reason": f"{best['name']} is {d1:.0f} km away"}


def _label(r: dict) -> str:
    extra = r.get("state") or r.get("attrs", {}).get("league") or r.get("attrs", {}).get("exchange") or ""
    return r["name"] + (f" ({extra})" if extra else "")


# --- fills ----------------------------------------------------------------------------------------

class Unfillable(Exception):
    """A parameter cannot be filled from the ask alone (message = the honest reason)."""


def _field(entry: dict, field: str) -> str:
    if field.startswith("attrs."):
        return str(entry.get("attrs", {}).get(field[6:]) or "")
    return str(entry.get(field) if entry.get(field) is not None else "")


def _clock_offset(record: dict, fill: dict, params: list[dict]) -> int:
    """A date window looks BACK by default (history: "the last 30 days"). A source that answers
    "next game" or a schedule looks FORWARD: the window's start becomes today and its end moves
    ahead by the same span (field 2026-09-28: "next Dodgers game" asked for the past month)."""
    offset = int(fill.get("offset_days") or 0)
    if not {"next_event", "schedule"} & set(record.get("kinds") or []):
        return offset
    offsets = [int((q.get("fill") or {}).get("offset_days") or 0) for q in params
               if (q.get("fill") or {}).get("from") == "clock"]
    span = -min(offsets) if offsets and min(offsets) < 0 else 0
    return 0 if offset < 0 else offset + span


def _clock(fmt: str, offset_days: int, now: datetime) -> str:
    t = now + timedelta(days=offset_days)
    # %-m / %-d are not portable (Windows): expand them by hand
    fmt = fmt.replace("%-m", str(t.month)).replace("%-d", str(t.day))
    return t.strftime(fmt)


def _text_fill(fill: dict, ask: str, own_words: set[str] = frozenset()) -> str:
    """The thing the user named, as typed: what's left after generic words AND the source's own
    vocabulary ("latest react version" for "npm package latest version" -> "react"). F8 (2026-10-04):
    ``norm`` splits a contraction into a word + leftover ("what's" → "what s"); drop only the
    contraction fragments so the fill never ships a stray "s" (tvmaze?q=s) but keeps a genuine
    1-char name ("latest R version" -> "r"). K-R (2026-10-04)."""
    pat = fill.get("pattern")
    if pat:
        m = re.search(pat, ask or "", re.IGNORECASE)
        if not m:
            raise Unfillable("the ask doesn't name it")
        return m.group(1) if m.groups() else m.group(0)
    words = [w for w in norm(ask).split() if w not in _CONTRACTION_FRAGMENTS
             and w not in ENGLISH and w not in own_words]
    if not words:
        raise Unfillable("the ask doesn't name it")
    return " ".join(words[:4])


# the leftovers ``norm`` produces when it splits an apostrophe out of a contraction ("what's" ->
# "what s"): never a 1-char subject the user named ("R", "Q", "X").
_CONTRACTION_FRAGMENTS = frozenset({"s", "t", "d", "ll", "re", "ve", "m"})


def candidate_urls(record: dict, ask: str, policy: dict, resolver: Resolver,
                   now: datetime | None = None) -> tuple[list[dict], str]:
    """Every concrete reading of ``record`` for ``ask``: [{url, label, choice}], or ([], reason).

    A source that takes the user's own key returns its address WITHOUT the key plus ``needs_key``
    (where the key goes); one whose provider wants a contact email carries ``needs_contact``. The
    card asks for either before the first fetch — neither is ever filled in here. A parameter filled
    from a same-host helper stays a ``{placeholder}`` in ``url`` and the candidate carries ``lookup``
    ([{url, path, param}]) for ``resolve_lookup`` to run after consent."""
    now = now or datetime.now().astimezone()  # the card's own local clock
    access = record.get("access") or {}
    if record.get("role") == "helper":
        return [], "a lookup helper, not an answer"
    params = access.get("params") or []
    key_param = ""
    values: dict[str, list[tuple[str, str]]] = {}  # name -> [(value, label)]
    groups: dict[str, str] = {}  # name -> the reading it came from (lat+lon of one place vary together)
    chain: list[dict] = []  # parameters a same-host helper fills after consent
    cache: dict[str, dict] = {}
    place_cache: dict = {}
    marine = _marine_only(record)
    # F1 (2026-10-04): clock-fill params ride as metadata ({name: {format, offset_days}}) so the engine
    # refills them from the current clock on every refresh — never a literal baked into the URL.
    clock_params: dict[str, dict] = {}

    def place() -> dict:
        if "r" not in place_cache:
            z = resolver.by_name("zip", ask) if re.search(r"\b\d{5}\b", ask or "") else {"status": "none"}
            r = z if z["status"] == "resolved" else resolver.by_name("place", ask)
            place_cache["r"] = _coastal_readings(r) if marine else r
        return place_cache["r"]

    for p in params:
        fill = p.get("fill") or {"from": "gap", "reason": "no fill"}
        src = fill.get("from")
        if src == "default":
            values[p["name"]] = [(str(fill["value"]), "")]
        elif src == "clock":
            offset = _clock_offset(record, fill, params)
            # the filled value rides the display URL at consent; the engine refills every tick from
            # ``clock_params`` so day 2 reads day-2's date, never the creation day's literal
            values[p["name"]] = [(_clock(fill["format"], offset, now), "")]
            clock_params[p["name"]] = {"format": str(fill["format"]), "offset_days": int(offset),
                                       "label": str(p.get("label") or p["name"])[:200]}
        elif src == "text":
            # the source's own vocabulary (name + description, NOT its example asks, whose subjects are
            # samples like "react") is never the thing the user named
            own = set(norm(f"{record.get('name', '')} {record.get('description', '')}").split())
            own |= {"version", "versions", "package", "packages", "release", "releases"}
            try:
                values[p["name"]] = [(_text_fill(fill, ask, own), "")]
            except Unfillable as exc:
                return [], f"{p['name']}: {exc}"
        elif src == "resolver":
            res = fill["resolver"]
            if res in GEO_NEAR:
                pl = place()
                if pl["status"] == "none":
                    return [], pl.get("inland") or "name the place (a city, town or ZIP)"
                origins = [pl["best"]] if pl["status"] == "resolved" else pl["candidates"][:MAX_CHOICES]
                opts, why_not = [], []
                for o in origins:
                    nr = resolver.near(res, o["lat"], o["lon"], float(policy.get("max_km") or 30),
                                       tuple(policy.get("differ_on") or ()), _measure(policy, res))
                    picks = [nr["best"]] if nr["status"] == "resolved" else nr["candidates"][:MAX_CHOICES]
                    opts += [(_field(e, fill.get("field", "key")), _near_label(e)) for e in picks if e]
                    why_not.append(nr["reason"])
                if not opts:
                    return [], why_not[0] if len(origins) == 1 else f"no {res.replace('_', ' ')} near there"
                values[p["name"]] = opts[:MAX_CHOICES]
                groups[p["name"]] = res
            else:
                if res == "place" and fill.get("field") in ("lat", "lon", "name"):
                    r = place()
                    groups[p["name"]] = "place"
                else:
                    key = f"{res}|{bool(fill.get('many_index') is not None)}"
                    if key not in cache:
                        cache[key] = resolver.by_name(res, ask, many=fill.get("many_index") is not None)
                    r = cache[key]
                if r["status"] == "none":
                    fb = fill.get("fallback")
                    if fb and fb.get("from") == "default":
                        values[p["name"]] = [(str(fb["value"]), "")]
                        continue
                    return [], r.get("inland") or f"the ask doesn't name a {res.replace('_', ' ')}"
                if fill.get("many_index") is not None:
                    i = int(fill["many_index"])
                    if res == "currency" and len(r["candidates"]) == 1 and r["candidates"][0]["key"] != "USD":
                        # one currency named ("yen exchange rate"): the other side is the US dollar (US scope)
                        usd = resolver.by_name("currency", "USD")
                        if usd["status"] == "resolved":
                            r = {**r, "candidates": [usd["best"], r["candidates"][0]]}
                    if len(r["candidates"]) <= i:
                        return [], f"name two {res}s"
                    picks = [r["candidates"][i]]
                else:
                    picks = [r["best"]] if r["status"] == "resolved" else r["candidates"][:MAX_CHOICES]
                opts = []
                for e in picks:
                    v = _field(e, fill.get("field", "key"))
                    fmt = fill.get("format")
                    if fmt and "{UPPER}" in fmt and fmt.count("{") == 1 and v:
                        v = fmt.replace("{UPPER}", v.upper())  # "{UPPER}", or "{UPPER}-USD" (a Coinbase pair)
                    elif fmt:
                        return [], "this provider's parameter format isn't supported yet"
                    if v:
                        opts.append((v, _label(e)))
                if not opts:
                    return [], f"{p['name']}: no value for that {res.replace('_', ' ')}"
                values[p["name"]] = opts
                groups.setdefault(p["name"], res)
        elif src == "vault_key" and not key_param:
            key_param = p["name"]
            values[p["name"]] = [(_KEY_MARK, "")]
        elif src == "source" and fill.get("source") and fill.get("path"):
            chain.append({"param": p["name"], "source": fill["source"], "path": str(fill["path"])})
        else:  # source, gap
            return [], {"source": "needs another lookup before your consent"}.get(
                src, f"{p['name']}: {fill.get('reason') or 'not fillable yet'}")
    urls = _expand(access.get("url_template") or "", values, groups,
                   keep={c["param"] for c in chain}, clock=clock_params)
    if chain:
        urls, why = _with_lookup(record, urls, chain, ask, policy, resolver, now)
        if not urls:
            return [], why
    if len({(u["url"], json.dumps(u.get("lookup"))) for u in urls}) < len(urls):
        return [], "can't tell the readings apart (the address has no room for which one)"
    if key_param:
        where = key_placement(access, key_param)
        if where is None:
            return [], "this provider takes its key somewhere SmartBrain can't send it safely"
        for u in urls:
            u["url"] = _without_key(u["url"])
            # R3-A (field 2026-10-04): the sealed clock-template URL carries the same key slot;
            # strip it there too so the engine's refills on day 2+ never ship ``api_key=SBKEYSLOT``.
            if "url_template" in u:
                u["url_template"] = _without_key(u["url_template"])
            u["needs_key"] = {**where, "docs_url": str(access.get("docs_url") or record.get("docs_url") or "")}
    if access.get("contact_ua"):
        for u in urls:
            u["needs_contact"] = True
    return urls, ""


_KEY_MARK = "SBKEYSLOT"  # survives quoting; never leaves this module
_HELPER_FILLS = ("resolver", "clock", "default")  # a helper fills from the ask alone, never another lookup


def _host(template: str) -> str:
    host = (urlsplit(template).hostname or "").lower()
    return "" if "{" in urlsplit(template).netloc else host


def _with_lookup(record: dict, urls: list[dict], chain: list[dict], ask: str, policy: dict,
                 resolver: Resolver, now: datetime) -> tuple[list[dict], str]:
    """Seal a same-host helper chain onto each reading: [{url, path, param}], one per helper-filled
    parameter. Refused (as before) unless the ONE helper is on the record's own host, keyless, needs no
    contact, and fills only from the ask — so the tap consents to exactly two fetches on one host."""
    refused = [], "needs another lookup before your consent"
    helper = resolver.source_record(chain[0]["source"]) if len({c["source"] for c in chain}) == 1 else None
    if helper is None:
        return refused
    h_access = helper.get("access") or {}
    host = _host(str((record.get("access") or {}).get("url_template") or ""))
    if not host or _host(str(h_access.get("url_template") or "")) != host \
            or h_access.get("auth") not in (None, "", "none") or h_access.get("contact_ua") \
            or any((q.get("fill") or {}).get("from") not in _HELPER_FILLS for q in h_access.get("params") or []):
        return refused
    readings, why = candidate_urls({**helper, "role": ""}, ask, policy, resolver, now)
    if not readings:
        return [], why
    if len(urls) > 1 and len(readings) > 1:
        return [], "can't tell the readings apart (the address has no room for which one)"
    out = []
    for u in urls:
        for h in readings:
            out.append({**u, "label": " · ".join(x for x in (u["label"], h["label"]) if x),
                        "choice": u["choice"] or h["choice"], "params": {**h["params"], **u["params"]},
                        "lookup": [{"url": h["url"], "path": c["path"], "param": c["param"]} for c in chain]})
    return out, ""


def _at_path(doc, path: str):
    for part in path.split("."):
        if isinstance(doc, list) and part.isdigit() and int(part) < len(doc):
            doc = doc[int(part)]
        elif isinstance(doc, dict):
            doc = doc.get(part)
        else:
            return None
    return doc


_LOOKUP_VALUE_RE = re.compile(r"[A-Za-z0-9,._:-]+")
_LOOKUP_VALUE_MAX = 64


def resolve_lookup(candidate: dict, fetch_json) -> str:
    """Run a sealed candidate's ``lookup`` chain through ``fetch_json(url) -> parsed JSON`` (each helper
    address fetched once), fill its parameters and return the final address. ValueError when a step
    leaves the source's host or its path holds no plain value; the fetch's own errors propagate.

    FETCH-F5 (2026-10-04): the helper's JSON is untrusted. Each pulled value must be a bounded single
    identifier (≤ ``_LOOKUP_VALUE_MAX`` chars of ``[A-Za-z0-9,._:-]``); ``.`` / ``..`` are refused
    (they'd rewrite path segments), and a value that fails is a ValueError rather than a surprise URL."""
    url = str(candidate["url"])
    host = _host(url)
    docs: dict[str, object] = {}
    for step in candidate.get("lookup") or []:
        if not host or _host(step["url"]) != host:
            raise ValueError("a lookup must stay on the source's own host")
        if step["url"] not in docs:
            docs[step["url"]] = fetch_json(step["url"])
        v = _at_path(docs[step["url"]], step["path"])
        if v is None or isinstance(v, (bool, dict, list)) or str(v) == "":
            raise ValueError(f"{step['param']}: the lookup has no {step['path']}")
        text = str(v)
        if text in (".", "..") or len(text) > _LOOKUP_VALUE_MAX or not _LOOKUP_VALUE_RE.fullmatch(text):
            raise ValueError(f"{step['param']}: the lookup value {text[:16]!r} is not a safe id")
        url = url.replace("{" + step["param"] + "}", quote(text, safe=",.-_:~"))
    return url


def _measure(policy: dict, resolver_name: str) -> tuple[str, ...]:
    """What a station must report for this subcategory (policy ``measure``: {"buoy": "WTMP"} or a list)."""
    need = (policy.get("measure") or {}).get(resolver_name) if isinstance(policy.get("measure"), dict) else None
    return (need,) if isinstance(need, str) else tuple(need or ())


def _marine_only(record: dict) -> bool:
    """A source that serves only ocean and coastal water (the Library's closed `coverage.water`, or an older
    pack's geo wording): an inland or Great Lakes place gets nothing from it."""
    coverage = record.get("coverage") or {}
    geo = str(coverage.get("geo") or "").lower()
    return coverage.get("water") == "ocean_coastal" or "ocean" in geo or "coastal" in geo


def _coastal_readings(r: dict) -> dict:
    """A marine source's place readings minus the ones the pack marks inland (attrs.coastal false);
    a place with no such mark stays, as before."""
    if r["status"] == "none":
        return r
    readings = [r["best"]] if r["status"] == "resolved" else r["candidates"]
    kept = [e for e in readings if (e.get("attrs") or {}).get("coastal") is not False]
    if len(kept) == len(readings):
        return r
    if not kept:
        return {"status": "none", "best": None, "candidates": [], "reason": "inland",
                "inland": f"{_label(readings[0])} isn't on the coast; this source covers the ocean and coast"}
    return {**r, "status": "resolved" if len(kept) == 1 else r["status"], "best": kept[0] if len(kept) == 1
            else None, "candidates": kept}


def key_placement(access: dict, key_param: str) -> dict | None:
    """Where the provider takes the user's key: a query parameter or a request header (with any
    literal prefix, "Token {key}"). None for anything else (a key in the path would sit in logs)."""
    slot = "{" + key_param + "}"
    for name, value in (access.get("headers") or {}).items():
        value = str(value)
        if slot in value:
            prefix = value.replace(slot, "")
            if not value.endswith(slot) or not re.fullmatch(r"[A-Za-z]*\s?", prefix):
                return None
            return {"in": "header", "name": str(name), "prefix": prefix}
    template = str(access.get("url_template") or "")
    m = re.search(r"[?&]([A-Za-z0-9_.\-]{1,40})=" + re.escape(slot) + r"(?:&|$)", template)
    return {"in": "query", "name": m.group(1), "prefix": ""} if m else None


def _without_key(url: str) -> str:
    """The address with the key's query pair removed (the engine adds the stored key at fetch time)."""
    parts = urlsplit(url)
    kept = [q for q in parts.query.split("&") if q and _KEY_MARK not in q]
    out = parts._replace(query="&".join(kept)).geturl()
    assert _KEY_MARK not in out, "a key slot outside the query or headers never reaches here"
    return out


# FETCH-F3 (2026-10-04): a host-param value must be a bare DNS host, optionally with a path (and that
# path's query — feed URLs carry ``?outputType=xml``), with no userinfo, no port, no fragment, no IP
# literal. ``_expand`` drops a non-matching value.
_HOST_PARAM_RE = re.compile(r"[a-z0-9][a-z0-9.-]*(?:/[^#@\s]*)?")
_IP_LITERAL_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")


def _near_label(e: dict) -> str:
    water = e.get("attrs", {}).get("water") or e.get("attrs", {}).get("river") or ""
    return e["name"] + (f" ({water})" if water and water.lower() not in e["name"].lower() else "")


def _expand(template: str, values: dict[str, list[tuple[str, str]]], groups: dict[str, str],
            keep: set[str] = frozenset(), clock: dict[str, dict] | None = None) -> list[dict]:
    """One URL per reading of the ONE ambiguous entity; parameters filled from that same reading (a
    place's lat AND lon) take the same index, so a URL never mixes two readings. A ``keep`` parameter
    stays a {placeholder} (a same-host lookup fills it after consent). A clock parameter is filled
    with its current value in ``url`` (shown at consent) and KEPT as ``{{param:name}}`` in
    ``url_template`` (the sealed spec URL; the engine refills every tick)."""
    clock = clock or {}
    choice_group = next((groups.get(n, n) for n, v in values.items() if len(v) > 1), None)
    members = [n for n in values if groups.get(n, n) == choice_group] if choice_group else []
    # fix6-rows E (2026-10-04): a resolver param that isn't a {placeholder} in the url_template
    # (FAA airport-events: ``{airport}`` lives in ``params`` only, as a filter the answers read)
    # reads the SAME URL for every reading, so expanding across them just dedup-fails at the
    # caller. Keep the first reading only; its label shows which airport was taken, the ask stays
    # one URL. "delays at Orlando airport" → the FAA source ships for KMCO (its first reading).
    if members and not any("{" + m + "}" in template for m in members):
        members = []
    count = min(len(values[members[0]]), MAX_CHOICES) if members else 1
    host_param = re.fullmatch(r"\{([a-z_][a-z0-9_]*)\}", urlsplit(template).netloc)
    out = []
    for i in range(count):
        chosen = {n: (v[i][0] if n in members and i < len(v) else v[0][0]) for n, v in values.items()}
        # FETCH-F3 (2026-10-04): a host-parameter fills raw into the URL's netloc; a value with
        # userinfo (@), a port (:n), a query (?x), a fragment (#f), or an IP literal would relocate
        # the fetch (or carry credentials). Only a bare DNS host (optionally a path) is allowed.
        if host_param is not None:
            host_value = chosen.get(host_param.group(1), "")
            if not _HOST_PARAM_RE.fullmatch(host_value) or _IP_LITERAL_RE.fullmatch(host_value.split("/")[0]):
                continue  # the resolver falls to the next reading; a sibling source may still work

        def sub(m, chosen=chosen):
            if m.group(1) in keep:
                return m.group(0)
            v = chosen.get(m.group(1), "")
            return v if host_param and m.group(1) == host_param.group(1) else quote(v, safe=",.-_:~")

        def sub_template(m, chosen=chosen):
            # F1: a clock-fill param stays a {{param:name}} slot in the sealed URL
            if m.group(1) in clock:
                return "{{param:" + m.group(1) + "}}"
            return sub(m, chosen=chosen)
        url = re.sub(r"\{([a-z_][a-z0-9_]*)\}", sub, template)
        url_template = re.sub(r"\{([a-z_][a-z0-9_]*)\}", sub_template, template) if clock else url
        labels = [v[i][1] if n in members and i < len(v) else v[0][1] for n, v in values.items()]
        row: dict = {"url": url, "label": " · ".join(dict.fromkeys(x for x in labels if x)),
                     "choice": bool(members),
                     # the values this reading filled (never the key slot): answers paths name them
                     "params": {n: v for n, v in chosen.items() if v != _KEY_MARK}}
        if clock:
            row["url_template"] = url_template
            row["clock_params"] = dict(clock)
        out.append(row)
    return out
