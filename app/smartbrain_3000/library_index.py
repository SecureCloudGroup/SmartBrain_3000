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
    "tag": "v1.0.0",
    "url": "https://github.com/SecureCloudGroup/SmartBrain_Library/releases/download/v1.0.0/library.duckdb.gz",
    "sha256": "554b97630122c702ce48827e213f6fb535e3a1a2b8b6cca8dd1fee4d10de2e77",
}
MAX_PACK_GZ_BYTES = 40_000_000       # the download cap (the v1 gzip is ~8 MB)
MAX_PACK_BYTES = 400_000_000         # the unpacked cap (the v1 file is ~34 MB)

LOCAL_RESERVED_ID = "__library_local__"
LOCAL_SLOT = "sources"
MAX_LOCAL_SOURCES = 500
MAX_PAGE = 50

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
                               THEN 1.5 ELSE 0 END) AS cb
         FROM library_source_categories GROUP BY source_id)
SELECT s.id, (h.rel / (SELECT max(rel) FROM hits)) * 4 + coalesce(c.cb, 0) + s.prior AS score
FROM hits h JOIN library_sources s ON s.id = h.source_id LEFT JOIN catb c ON c.source_id = s.id
WHERE {where}
ORDER BY score DESC"""


class LibraryIndexError(Exception):
    """The pack could not be installed or read (message is safe to show)."""


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9.+-]*", (text or "").lower()) if t not in _STOP and len(t) > 1]


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
            terms = tokens(q)
            if terms:
                sql = _LOOKUP_SQL.format(where=cond)
                ranked = con.execute(sql, [terms, self.classify(q)] + args).fetchall()
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
                "terms_status, cadence, validation_status FROM library_sources WHERE id IN (SELECT unnest(?))",
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
                            "cadence": r[10], "status": r[11], "categories": cats.get(sid, [])})
        return {"total": total, "offset": offset, "results": results}

    def get(self, source_id: str) -> dict | None:
        with self._conn() as con:
            row = con.execute("SELECT record FROM library_sources WHERE id = ?", [source_id]).fetchone()
        return None if row is None else json.loads(row[0])


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
