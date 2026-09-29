"""Library resolvers + fills in the app: the words of an ask -> a concrete, consentable source URL.

A faithful port of SmartBrain_Library's ``sourcetool/resolve.py`` (matcher) and ``fills.py`` (fill
semantics), reading the resolver tables the pinned pack ships (``library_resolver_entries`` /
``library_resolver_aliases``). Keep the two in step: the Library's evaluation sets are the contract.

Nothing here fetches. A candidate is offered only when every parameter can be filled from the ask, the
clock or a default — a key, another source's answer, a contact email or a named gap keeps it off the card
(it would need a fetch or a secret before the user's consent). An ambiguous entity becomes one candidate
per reading, so the user's tap IS the answer to the question.
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


def states_in(ask: str) -> set[str]:
    found = {m for m in re.findall(r"\b([A-Z]{2})\b", ask or "") if m in US_STATES}
    low = f" {norm(ask)} "
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
        states = states_in(ask)
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
             differ_on: tuple[str, ...] = ()) -> dict:
        dlat = max_km / 111.0 + 0.5
        dlon = max_km / (111.0 * max(0.2, math.cos(lat * math.pi / 180))) + 0.5
        rows = self._con.execute(
            f"SELECT {_ENTRY_COLS} FROM library_resolver_entries e WHERE e.resolver = ? "
            f"AND e.lat BETWEEN ? AND ? AND e.lon BETWEEN ? AND ?",
            [resolver, lat - dlat, lat + dlat, lon - dlon, lon + dlon]).fetchall()
        dist = sorted(((km(lat, lon, r[5], r[6]), _row(r)) for r in rows if r[5] is not None),
                      key=lambda x: x[0])[:8]
        within = [(d, r) for d, r in dist if d <= max_km]
        if not within:
            return {"status": "none", "best": None, "candidates": [],
                    "reason": f"no {resolver.replace('_', ' ')} within {max_km:g} km"}
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
    vocabulary ("latest react version" for "npm package latest version" -> "react")."""
    pat = fill.get("pattern")
    if pat:
        m = re.search(pat, ask or "", re.IGNORECASE)
        if not m:
            raise Unfillable("the ask doesn't name it")
        return m.group(1) if m.groups() else m.group(0)
    words = [w for w in norm(ask).split() if w not in ENGLISH and w not in own_words]
    if not words:
        raise Unfillable("the ask doesn't name it")
    return " ".join(words[:4])


def candidate_urls(record: dict, ask: str, policy: dict, resolver: Resolver,
                   now: datetime | None = None) -> tuple[list[dict], str]:
    """Every concrete reading of ``record`` for ``ask``: [{url, label, choice}], or ([], reason).

    A source that takes the user's own key returns its address WITHOUT the key plus ``needs_key``
    (where the key goes); one whose provider wants a contact email carries ``needs_contact``. The
    card asks for either before the first fetch — neither is ever filled in here."""
    now = now or datetime.now().astimezone()  # the card's own local clock
    access = record.get("access") or {}
    if record.get("role") == "helper":
        return [], "a lookup helper, not an answer"
    params = access.get("params") or []
    key_param = ""
    values: dict[str, list[tuple[str, str]]] = {}  # name -> [(value, label)]
    groups: dict[str, str] = {}  # name -> the reading it came from (lat+lon of one place vary together)
    cache: dict[str, dict] = {}
    place_cache: dict = {}

    def place() -> dict:
        if "r" not in place_cache:
            z = resolver.by_name("zip", ask) if re.search(r"\b\d{5}\b", ask or "") else {"status": "none"}
            place_cache["r"] = z if z["status"] == "resolved" else resolver.by_name("place", ask)
        return place_cache["r"]

    for p in params:
        fill = p.get("fill") or {"from": "gap", "reason": "no fill"}
        src = fill.get("from")
        if src == "default":
            values[p["name"]] = [(str(fill["value"]), "")]
        elif src == "clock":
            values[p["name"]] = [(_clock(fill["format"], _clock_offset(record, fill, params), now), "")]
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
                    return [], "name the place (a city, town or ZIP)"
                origins = [pl["best"]] if pl["status"] == "resolved" else pl["candidates"][:MAX_CHOICES]
                opts = []
                for o in origins:
                    nr = resolver.near(res, o["lat"], o["lon"], float(policy.get("max_km") or 30),
                                       tuple(policy.get("differ_on") or ()))
                    picks = [nr["best"]] if nr["status"] == "resolved" else nr["candidates"][:MAX_CHOICES]
                    opts += [(_field(e, fill.get("field", "key")), _near_label(e)) for e in picks if e]
                if not opts:
                    return [], f"no {res.replace('_', ' ')} near there"
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
                    return [], f"the ask doesn't name a {res.replace('_', ' ')}"
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
        else:  # source, gap
            return [], {"source": "needs another lookup before your consent"}.get(
                src, f"{p['name']}: {fill.get('reason') or 'not fillable yet'}")
    urls = _expand(access.get("url_template") or "", values, groups)
    if len({u["url"] for u in urls}) < len(urls):
        return [], "can't tell the readings apart (the address has no room for which one)"
    if key_param:
        where = key_placement(access, key_param)
        if where is None:
            return [], "this provider takes its key somewhere SmartBrain can't send it safely"
        for u in urls:
            u["url"] = _without_key(u["url"])
            u["needs_key"] = {**where, "docs_url": str(access.get("docs_url") or record.get("docs_url") or "")}
    if access.get("contact_ua"):
        for u in urls:
            u["needs_contact"] = True
    return urls, ""


_KEY_MARK = "SBKEYSLOT"  # survives quoting; never leaves this module


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


def _near_label(e: dict) -> str:
    water = e.get("attrs", {}).get("water") or e.get("attrs", {}).get("river") or ""
    return e["name"] + (f" ({water})" if water and water.lower() not in e["name"].lower() else "")


def _expand(template: str, values: dict[str, list[tuple[str, str]]], groups: dict[str, str]) -> list[dict]:
    """One URL per reading of the ONE ambiguous entity; parameters filled from that same reading (a
    place's lat AND lon) take the same index, so a URL never mixes two readings."""
    choice_group = next((groups.get(n, n) for n, v in values.items() if len(v) > 1), None)
    members = [n for n in values if groups.get(n, n) == choice_group] if choice_group else []
    count = min(len(values[members[0]]), MAX_CHOICES) if members else 1
    host_param = re.fullmatch(r"\{([a-z_][a-z0-9_]*)\}", urlsplit(template).netloc)
    out = []
    for i in range(count):
        chosen = {n: (v[i][0] if n in members and i < len(v) else v[0][0]) for n, v in values.items()}

        def sub(m, chosen=chosen):
            v = chosen.get(m.group(1), "")
            return v if host_param and m.group(1) == host_param.group(1) else quote(v, safe=",.-_:~")
        url = re.sub(r"\{([a-z_][a-z0-9_]*)\}", sub, template)
        labels = [v[i][1] if n in members and i < len(v) else v[0][1] for n, v in values.items()]
        out.append({"url": url, "label": " · ".join(dict.fromkeys(x for x in labels if x)),
                    "choice": bool(members),
                    # the values this reading filled (never the key slot): answers paths name them
                    "params": {n: v for n, v in chosen.items() if v != _KEY_MARK}})
    return out
