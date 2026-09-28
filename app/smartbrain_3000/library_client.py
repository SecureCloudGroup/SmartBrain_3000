"""The app's side of the SmartBrain Library API (ruling R7: users contribute through an API, not Git).

Two things leave this machine, both disclosed in docs/07-privacy-security.md:
  * a **vote** when the user taps a Library source on a card ("a Yes is a good source", R6):
    ``{source_id, verdict: "yes", app_version}`` — never the ask, the filled address, a place or a key;
  * a **suggestion** when the user ticks "suggest it to the Library" on the Add-a-source form: the
    source record with every parameter value removed (the template, never the user's station or ticker).

Both wait in a sealed outbox (NIStore's reserved row) and are sent by the scheduler tick through
netguard, so a click never waits on the network, an offline machine loses nothing, and a service that
is down or refusing just means a later retry. A refusal the service will never accept (422) is dropped.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

from . import __version__, netguard
from .library_index import LOCAL_RESERVED_ID

log = logging.getLogger("smartbrain.library.client")

DEFAULT_API = "https://smartbrain.securecloudgroup.com/library/v1"
OUTBOX_SLOT = "outbox"
MAX_OUTBOX = 200          # oldest votes drop first beyond this (suggestions are kept)
MAX_SEND_PER_TICK = 10
MAX_TRIES = 8             # then the item is dropped: the service has been unreachable for days
_BACKOFF_CAP = timedelta(hours=24)
# the fields the service accepts on a suggested record (SmartBrain_Library sourcetool/submission.py)
_RECORD_FIELDS = ("name", "description", "provider", "categories", "kinds", "coverage", "access", "terms",
                  "freshness", "examples", "notes")
_ACCESS_FIELDS = ("kind", "url_template", "docs_url", "params", "auth", "headers", "contact_ua")


_LOCK = threading.Lock()  # guards read-merge-write of the outbox; never held across the network


def _now() -> datetime:
    return datetime.now(UTC)


def _read(store) -> list[dict]:
    row = store.read_reserved_snapshot(LOCAL_RESERVED_ID, OUTBOX_SLOT)
    return list(row["payload"].get("items", [])) if row else []


def _write(store, items: list[dict]) -> None:
    store.write_reserved_snapshot(LOCAL_RESERVED_ID, OUTBOX_SLOT, {"items": items})


def _add(store, item: dict) -> None:
    with _LOCK:
        items = _read(store) + [{**item, "id": uuid.uuid4().hex}]
        while len(items) > MAX_OUTBOX:
            oldest_vote = next((i for i, x in enumerate(items) if x["path"] == "votes"), 0)
            items.pop(oldest_vote)
        _write(store, items)


def queue_vote(store, source_id: str, verdict: str = "yes") -> None:
    """A tap on a Library source (R6). Only the source's id and the verdict are ever sent."""
    assert source_id and verdict in ("yes", "no", "broken"), "a Library id and a verdict"
    _add(store, {"path": "votes", "body": {"source_id": source_id[:120], "verdict": verdict,
                                            "app_version": __version__[:32]},
                 "tries": 0, "next": _now().isoformat()})


def suggestion_record(record: dict) -> dict:
    """A local source as the Library accepts it: only the fields it knows, every parameter VALUE
    removed (examples null), no headers (a header could carry a credential), no origin or tier."""
    access = record.get("access") or {}
    out = {k: record[k] for k in _RECORD_FIELDS if k in record}
    out["provider"] = {k: v for k, v in (record.get("provider") or {}).items()
                       if k in ("id", "name", "url")}
    out["access"] = {k: access[k] for k in _ACCESS_FIELDS if k in access}
    out["access"]["headers"] = {}
    out["access"]["params"] = [{"name": p["name"], "kind": p.get("kind", "none"),
                                "required": bool(p.get("required", True)), "example": None}
                               for p in access.get("params") or []]
    out["examples"] = []
    out["terms"] = {"status": "unverified", "note": "", "terms_url": ""}
    return out


def queue_suggestion(store, record: dict, via: str = "form") -> None:
    """The user asked to suggest their source to the Library."""
    assert via in ("form", "yes"), "via is form or yes"
    _add(store, {"path": "suggestions", "body": {"record": suggestion_record(record), "via": via,
                                                  "app_version": __version__[:32]},
                 "tries": 0, "next": _now().isoformat()})


def pending(store) -> int:
    return len(_read(store))


def flush(store, *, net=netguard, now: datetime | None = None, budget: int = MAX_SEND_PER_TICK) -> dict:
    """Send what is due (bounded). Returns counts; never raises past a bad item."""
    api = os.environ.get("SMARTBRAIN_LIBRARY_API", DEFAULT_API).rstrip("/")
    if not api:
        return {"sent": 0, "dropped": 0, "waiting": pending(store)}  # sending switched off
    now = now or _now()
    with _LOCK:
        items = _read(store)
    keep, sent, dropped, tried = [], 0, 0, 0
    for item in items:
        due = datetime.fromisoformat(item["next"]) <= now
        if not due or tried >= budget:
            keep.append(item)
            continue
        tried += 1
        try:
            net.safe_post_json(f"{api}/{item['path']}", item["body"])
            sent += 1
        except Exception as exc:  # unreachable, refusing, rate-limited or a malformed reply
            status = getattr(exc, "status", None)
            if status in (400, 413, 415, 422) or item["tries"] + 1 >= MAX_TRIES:
                dropped += 1  # the service will never take it as sent (or has been gone for days)
                log.info("library outbox: dropped a %s (%s)", item["path"], status or type(exc).__name__)
                continue
            keep.append({**item, "tries": item["tries"] + 1,
                         "next": (now + min(timedelta(minutes=15 * 2 ** item["tries"]),
                                            _BACKOFF_CAP)).isoformat()})
    if tried:
        with _LOCK:  # keep anything queued while this pass was sending
            seen = {x.get("id") for x in items}
            keep += [x for x in _read(store) if x.get("id") not in seen]
            _write(store, keep)
    return {"sent": sent, "dropped": dropped, "waiting": len(keep)}
