"""RSS/Atom feed subscriptions — vaults that fill themselves.

A feed subscription is a URL polled on the scheduler's tick (every ~6h per feed); new
items become ordinary encrypted Knowledge documents inside a LOCAL vault created for
the feed, so everything downstream — semantic search, chat grounding, the vault chips,
keep-vs-remove-docs deletion — is the machinery that already exists. Adding a feed is
an explicit act in the UI (the click is the consent, exactly like subscribing to a
vault), so background refreshes fetch that host without per-fetch approvals and the
agent gains no new tool.

Design bounds, stated where they bind below: stdlib XML only (no feedparser
dependency), RSS 2.0 + Atom, bounded items per fetch, capped field sizes, per-feed
errors isolated with a host-free status string, and GUID-based dedup that survives
forever (feed_seen mirrors vault_import_traces: tiny plaintext ids, no content).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import netguard
from . import vaults as vaults_mod

log = logging.getLogger("smartbrain.feeds")

_NONCE_BYTES = 12
_MAX_FEEDS = 100
_MAX_ITEMS_PER_FETCH = 50  # a first fetch of a busy feed must not flood the KB
_MAX_TITLE = 300
_MAX_SUMMARY = 20_000
_MAX_URL = 2000
REFRESH_SECONDS = 6 * 3600  # per-feed cadence; the tick enforces it, not a timer per feed
_MAX_FEEDS_PER_PASS = 5  # one tick refreshes at most this many due feeds

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


class FeedError(Exception):
    """A feed problem worth showing the user (bad XML, not a feed, too big)."""


def _strip_html(text: str) -> str:
    """Feed summaries arrive as HTML; store readable text (tags out, entities decoded)."""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", no_tags)).strip()


def parse_feed(xml_text: str) -> dict:
    """Parse RSS 2.0 or Atom into {title, items:[{guid,title,link,summary,published}]}.

    Tolerant where the wild feeds are sloppy (missing guids fall back to link, then to
    a title hash), strict where it protects us (not-a-feed raises FeedError; items and
    field sizes are capped). Never returns unbounded data.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedError(f"not valid XML: {exc}") from None

    items: list[dict] = []
    if root.tag == "rss" or root.tag.endswith("rss"):
        channel = root.find("channel")
        if channel is None:
            raise FeedError("RSS feed has no <channel>")
        feed_title = (channel.findtext("title") or "Untitled feed").strip()[:_MAX_TITLE]
        for item in channel.findall("item")[:_MAX_ITEMS_PER_FETCH]:
            title = (item.findtext("title") or "(untitled)").strip()[:_MAX_TITLE]
            link = (item.findtext("link") or "").strip()[:_MAX_URL]
            guid = (item.findtext("guid") or link or title).strip()[:_MAX_URL]
            summary = _strip_html(item.findtext("description") or "")[:_MAX_SUMMARY]
            published = (item.findtext("pubDate") or "").strip()[:100]
            items.append({"guid": guid, "title": title, "link": link,
                          "summary": summary, "published": published})
    elif root.tag == f"{_ATOM_NS}feed" or root.tag.endswith("feed"):
        ns = _ATOM_NS if root.tag.startswith(_ATOM_NS) else ""
        feed_title = (root.findtext(f"{ns}title") or "Untitled feed").strip()[:_MAX_TITLE]
        for entry in root.findall(f"{ns}entry")[:_MAX_ITEMS_PER_FETCH]:
            title = (entry.findtext(f"{ns}title") or "(untitled)").strip()[:_MAX_TITLE]
            link = ""
            for ln in entry.findall(f"{ns}link"):  # bounded by entry links
                if ln.get("rel") in (None, "alternate"):
                    link = (ln.get("href") or "").strip()[:_MAX_URL]
                    break
            guid = (entry.findtext(f"{ns}id") or link or title).strip()[:_MAX_URL]
            body = entry.findtext(f"{ns}summary") or entry.findtext(f"{ns}content") or ""
            summary = _strip_html(body)[:_MAX_SUMMARY]
            published = (entry.findtext(f"{ns}published")
                         or entry.findtext(f"{ns}updated") or "").strip()[:100]
            items.append({"guid": guid, "title": title, "link": link,
                          "summary": summary, "published": published})
    else:
        raise FeedError("not an RSS or Atom feed")
    return {"title": feed_title, "items": items}


class FeedStore:
    """Encrypted feed registry. The URL and title are sealed (a feed list is a reading
    profile); cadence metadata (enabled, last_checked, last_status) is plaintext so the
    tick finds due rows without the key — the schedules table's exact convention."""

    def __init__(self, conn, master_key: bytes) -> None:
        assert conn is not None and master_key, "conn + key required"
        self._conn = conn
        self._aes = AESGCM(master_key)

    def add(self, url: str, title: str, vault_id: str, tags: list[str] | None = None) -> str:
        """``tags`` are stamped on every document the feed ever ingests — set at subscribe
        time, sealed with the URL (which topics you follow is as telling as where)."""
        assert url and title and vault_id, "url, title, vault id required"
        count = self._conn.execute("SELECT COUNT(*) FROM feeds;").fetchone()[0]
        if int(count) >= _MAX_FEEDS:
            raise FeedError(f"feed limit reached ({_MAX_FEEDS})")
        feed_id = str(uuid.uuid4())
        nonce, ciphertext = self._seal(feed_id, {"url": url[:_MAX_URL], "title": title[:_MAX_TITLE],
                                                 "tags": [str(t)[:100] for t in (tags or [])[:20]]})
        self._conn.execute(
            "INSERT INTO feeds (id, vault_id, nonce, ciphertext) VALUES (?, ?, ?, ?);",
            [feed_id, vault_id, nonce, ciphertext],
        )
        return feed_id

    def list_feeds(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, vault_id, nonce, ciphertext, enabled, last_checked, last_status, created_at "
            f"FROM feeds ORDER BY created_at DESC LIMIT {_MAX_FEEDS};"
        ).fetchall()
        return [self._row(r) for r in rows]  # bounded by _MAX_FEEDS

    def get(self, feed_id: str) -> dict | None:
        assert feed_id, "feed id required"
        row = self._conn.execute(
            "SELECT id, vault_id, nonce, ciphertext, enabled, last_checked, last_status, created_at "
            "FROM feeds WHERE id = ?;",
            [feed_id],
        ).fetchone()
        return None if row is None else self._row(row)

    def due_feeds(self) -> list[dict]:
        """Enabled feeds never checked, or checked longer than the cadence ago (bounded)."""
        rows = self._conn.execute(
            "SELECT id, vault_id, nonce, ciphertext, enabled, last_checked, last_status, created_at "
            "FROM feeds WHERE enabled AND (last_checked IS NULL OR "
            "date_diff('second', last_checked, now()) > ?) "
            "ORDER BY last_checked ASC NULLS FIRST LIMIT ?;",
            [REFRESH_SECONDS, _MAX_FEEDS_PER_PASS],
        ).fetchall()
        return [self._row(r) for r in rows]  # bounded by _MAX_FEEDS_PER_PASS

    def mark_checked(self, feed_id: str, status: str) -> None:
        """Record a refresh outcome. ``status`` must stay host-free (it's plaintext)."""
        assert feed_id, "feed id required"
        self._conn.execute(
            "UPDATE feeds SET last_checked = now(), last_status = ? WHERE id = ?;",
            [status[:200], feed_id],
        )

    def delete(self, feed_id: str) -> None:
        """Remove the feed registration + its seen-set. The vault/documents are the caller's
        decision (keep-vs-remove-docs mirrors vault deletion)."""
        assert feed_id, "feed id required"
        self._conn.execute("DELETE FROM feed_seen WHERE feed_id = ?;", [feed_id])
        self._conn.execute("DELETE FROM feeds WHERE id = ?;", [feed_id])

    def unseen(self, feed_id: str, guids: list[str]) -> list[str]:
        """The subset of ``guids`` this feed has never ingested (order preserved)."""
        assert feed_id, "feed id required"
        out: list[str] = []
        for g in guids:  # bounded by _MAX_ITEMS_PER_FETCH
            row = self._conn.execute(
                "SELECT 1 FROM feed_seen WHERE feed_id = ? AND guid = ?;", [feed_id, g]
            ).fetchone()
            if row is None:
                out.append(g)
        return out

    def mark_seen(self, feed_id: str, guid: str) -> None:
        self._conn.execute(
            "INSERT INTO feed_seen (feed_id, guid) VALUES (?, ?) ON CONFLICT DO NOTHING;",
            [feed_id, guid],
        )

    def _seal(self, feed_id: str, body: dict) -> tuple[bytes, bytes]:
        nonce = os.urandom(_NONCE_BYTES)
        aad = b"feed:" + feed_id.encode("utf-8")
        return nonce, self._aes.encrypt(nonce, json.dumps(body).encode("utf-8"), aad)

    def _row(self, row: tuple) -> dict:
        feed_id = str(row[0])
        aad = b"feed:" + feed_id.encode("utf-8")
        body = json.loads(self._aes.decrypt(bytes(row[2]), bytes(row[3]), aad))
        return {
            "id": feed_id, "vault_id": str(row[1]), "url": body["url"], "title": body["title"],
            "tags": body.get("tags", []),  # absent in pre-tag rows
            "enabled": bool(row[4]),
            "last_checked": None if row[5] is None else str(row[5]),
            "last_status": str(row[6] or ""), "created_at": str(row[7]),
        }


def _safe_status(exc: Exception) -> str:
    """last_status is plaintext, so it must stay host-free. Two netguard messages embed
    the address after a colon — keep only their host-free prefix; the rest already are."""
    msg = str(exc)
    for leaky in ("cannot resolve host", "blocked non-global address"):
        if msg.startswith(leaky):
            return leaky
    return msg


def fetch_and_parse(url: str) -> dict:
    """Guarded fetch + parse; FeedError/netguard.FetchError carry the user-facing reason."""
    got = netguard.safe_fetch_feed(url)
    return parse_feed(got["text"])


def ingest_new_items(store: FeedStore, kb, vaults, feed: dict, parsed: dict) -> int:
    """Add unseen items as encrypted documents in the feed's vault; return how many landed.

    Items are marked seen ONLY after the document exists and is in the vault, so a crash
    mid-pass re-ingests at worst (dedup by guid makes that idempotent) and never skips.
    """
    guids = [i["guid"] for i in parsed["items"]]
    fresh = set(store.unseen(feed["id"], guids))
    added = 0
    for item in parsed["items"]:  # bounded by _MAX_ITEMS_PER_FETCH
        if item["guid"] not in fresh:
            continue
        title = f'{parsed["title"]}: {item["title"]}'[:_MAX_TITLE]
        content_parts = [item["title"]]
        if item["link"]:
            content_parts.append(item["link"])
        if item["published"]:
            content_parts.append(f'Published: {item["published"]}')
        if item["summary"]:
            content_parts.append("")
            content_parts.append(item["summary"])
        doc_id = kb.add(title, "\n".join(content_parts),
                        meta={"feed_id": feed["id"], "feed_guid": item["guid"][:_MAX_URL]},
                        tags=feed.get("tags"))
        vaults.add_documents(feed["vault_id"], [doc_id], origin=vaults_mod.FEED)  # someone else's words
        store.mark_seen(feed["id"], item["guid"])
        added += 1
    return added


def tick(app, pass_budget_seconds: float = 20.0) -> None:
    """Refresh due feeds — the vault_sync.tick pattern: own cursor, per-feed isolation,
    wall-clock budget so a slow host can't eat the scheduler's tick."""
    key = getattr(app.state, "master_key", None)
    if key is None:
        return  # locked — feed URLs can't even be decrypted
    from .kb import KnowledgeBase
    from .vaults import VaultStore

    cursor = app.state.db.cursor()
    try:
        store = FeedStore(cursor, key)
        kb = KnowledgeBase(cursor, key)
        vaults = VaultStore(cursor, key)
        started = time.monotonic()
        for feed in store.due_feeds():  # bounded by _MAX_FEEDS_PER_PASS
            if time.monotonic() - started > pass_budget_seconds:
                return  # the rest stay due; the next tick continues
            try:
                parsed = fetch_and_parse(feed["url"])
                added = ingest_new_items(store, kb, vaults, feed, parsed)
                store.mark_checked(feed["id"], "ok" if added == 0 else f"ok: {added} new")
                if added:
                    log.info("feed refresh: %d new item(s)", added)  # host-free by policy
            except (FeedError, netguard.FetchError) as exc:
                store.mark_checked(feed["id"], f"error: {_safe_status(exc)}"[:200])
            except Exception as exc:  # one bad feed must not stop the rest
                log.warning("feed refresh failed: %s", type(exc).__name__)
                store.mark_checked(feed["id"], "error: internal")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
