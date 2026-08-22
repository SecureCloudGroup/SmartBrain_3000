"""HTTP surface for feed subscriptions.

The add is the consent: a user pasting a feed URL here is the authorization for every
future background refresh of that host (the vault-subscribe convention). Reads work
from any session surface; add/delete are Desktop-local like the other data-shaping
acts. Deleting mirrors vault deletion: the grouping goes, the documents stay unless
``remove_docs=1`` says otherwise.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import feeds as feedsmod
from . import netguard, tools
from .data_routes import _require_desktop_local

router = APIRouter()


def _store(request: Request) -> feedsmod.FeedStore:
    store = getattr(request.app.state, "feeds", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


def _vaults(request: Request):
    vaults = getattr(request.app.state, "vaults", None)
    if vaults is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return vaults


def _kb(request: Request):
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return kb


class FeedAddIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


@router.get("/api/feeds")
def list_feeds(request: Request) -> dict:
    return {"feeds": _store(request).list_feeds()}


@router.post("/api/feeds")
def add_feed(request: Request, body: FeedAddIn) -> dict:
    """Validate, fetch, parse, create the feed's vault, ingest the current items.

    Everything or nothing: a URL that doesn't fetch or parse creates no vault and no
    registration — the user gets the reason instead of a broken half-subscription.
    """
    _require_desktop_local(request)
    url = body.url.strip()
    try:
        netguard.validate_public_url(url)
        parsed = feedsmod.fetch_and_parse(url)
    except (netguard.FetchError, feedsmod.FeedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    store = _store(request)
    vaults = _vaults(request)
    vault_id = vaults.create(parsed["title"], "Feed subscription", tags=["feed"])
    feed_id = store.add(url, parsed["title"], vault_id)
    feed = store.get(feed_id)
    added = feedsmod.ingest_new_items(store, _kb(request), vaults, feed, parsed)
    store.mark_checked(feed_id, f"ok: {added} new")
    # Audited as INGRESS, like a vault subscribe: the HOST only, never the full URL
    # (a feed path names the topic as plainly as a document title would).
    request.app.state.audit.append(
        "user", "feed_subscribe", "reviewed", "executed", True,
        args_summary=tools.summarize({"host": urlsplit(url).hostname or ""}),
        result_summary=tools.summarize({"items": added}),
    )
    return {"id": feed_id, "title": parsed["title"], "vault_id": vault_id, "items": added}


@router.post("/api/feeds/{feed_id}/refresh")
def refresh_feed(request: Request, feed_id: str) -> dict:
    """Manual refresh — the same path the background tick takes, on demand."""
    store = _store(request)
    feed = store.get(feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="feed not found")
    try:
        parsed = feedsmod.fetch_and_parse(feed["url"])
    except (netguard.FetchError, feedsmod.FeedError) as exc:
        # The stored status is plaintext and must stay host-free; the HTTP detail is
        # in-band to the unlocked user and keeps the full reason.
        store.mark_checked(feed_id, f"error: {feedsmod._safe_status(exc)}"[:200])
        raise HTTPException(status_code=502, detail=str(exc)) from None
    added = feedsmod.ingest_new_items(store, _kb(request), _vaults(request), feed, parsed)
    store.mark_checked(feed_id, "ok" if added == 0 else f"ok: {added} new")
    return {"items": added}


@router.delete("/api/feeds/{feed_id}")
def delete_feed(request: Request, feed_id: str, remove_docs: int = 0) -> dict:
    """Unsubscribe. Removing a feed isn't shredding its articles — unless asked."""
    _require_desktop_local(request)
    store = _store(request)
    feed = store.get(feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="feed not found")
    vaults = _vaults(request)
    removed = 0
    if remove_docs:
        kb = _kb(request)
        for doc_id in vaults.document_ids(feed["vault_id"]):  # bounded by vault size
            kb.delete(doc_id)
            removed += 1
    vaults.delete(feed["vault_id"])
    store.delete(feed_id)
    return {"deleted": True, "docs_removed": removed}
