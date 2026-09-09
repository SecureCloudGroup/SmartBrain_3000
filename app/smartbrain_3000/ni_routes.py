"""HTTP surface for Neural Interface items (requires unlock).

The board endpoint is read-only and safe for any unlocked session; the write
endpoints (validate / run / patch / delete / credential) run only when unlocked.
The credential PUT is additionally Desktop-local (the raw value ships in the
body, so a bridged phone must not reach it — same posture as the passphrase
reset and the MCP-token endpoints).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import gateway, ni, tools
from .account import _require_desktop_local
from .scheduler import ScheduleStore

router = APIRouter()

_MAX_RUNS_RETURNED = 20  # bounded per-item health tail (matches read_ni_item tool)
_MAX_HOST_LEN = 253  # RFC-1035 total host length; mirrors consent.py's guard
_MAX_NOTE = 2000  # C2 note length cap (upper-bounded; validate route body)
_MAX_CRED_VALUE = 8000  # bounded credential value; well over any real token/api key
_MAX_POSITION = 10_000  # position is a display sort key; verifiable upper bound


class ValidateIn(BaseModel):
    """C2 verdict body: user answers "Looks right" (ok=true) or "Something's wrong"."""

    ok: bool
    note: str | None = Field(default=None, max_length=_MAX_NOTE)


class PatchIn(BaseModel):
    """Restricted PATCH surface: only enabled / position / display (§10 §9)."""

    enabled: bool | None = None
    position: int | None = Field(default=None, ge=0, le=_MAX_POSITION)
    display: dict | None = None


class CredentialIn(BaseModel):
    """Store one secret parameter value under ``ni:<item_id>:<name>`` bound to ``host``."""

    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=_MAX_CRED_VALUE)
    host: str = Field(min_length=1, max_length=_MAX_HOST_LEN)


def _store(request: Request) -> ni.NIStore:
    """Return the unlocked NIStore, or 423 (locked contract, mirrors schedule/feed routes)."""
    store = getattr(request.app.state, "ni", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


def _secret_store(request: Request):
    """Return the unlocked SecretStore, or 423. Only the credential PUT touches this."""
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


def _pick_board_snapshot(store: ni.NIStore, item: dict) -> dict | None:
    """Board-view snapshot: preview for draft, else latest-if-ok, else last_good (§10)."""
    assert store is not None and item, "store + item required"
    if item["state"] == "draft":
        return store.read_snapshot(item["id"], "preview")
    latest = store.read_snapshot(item["id"], "latest")
    if latest is not None and latest["ok"]:
        return {**latest, "slot": "latest"}
    fallback = store.read_snapshot(item["id"], "last_good")
    return None if fallback is None else {**fallback, "slot": "last_good"}


def _board_row(store: ni.NIStore, item: dict) -> dict:
    """One board row: plaintext operational fields + display + the chosen snapshot's payload."""
    snap = _pick_board_snapshot(store, item)
    slot = "preview" if item["state"] == "draft" else (snap.get("slot") if snap else None)
    return {
        "id": item["id"], "title": item["spec"].get("title", ""),
        "state": item["state"], "enabled": item["enabled"],
        "interval_minutes": item["interval_minutes"],
        "last_checked": item["last_checked"], "last_status": item["last_status"],
        "consecutive_failures": item["consecutive_failures"],
        "position": item["position"],
        "display": item["spec"].get("display") or {"size": "small"},
        "payload_slot": slot,
        "payload_at": snap["created_at"] if snap else None,
        "payload_ok": snap["ok"] if snap else None,
        "payload": snap["payload"] if snap else None,
    }


@router.get("/api/ni/board")
def board(request: Request) -> dict:
    """All items + decrypted bound payload, sorted by position (already the store's order)."""
    store = _store(request)
    return {"items": [_board_row(store, item) for item in store.list_items()]}


# Literal paths would live here if any existed — /api/ni/items/... has no literal
# children (validate/run/credential are all under /{id}/...), so the ordering
# comment is here for parity with schedule_routes even though nothing shadows it.


@router.get("/api/ni/items/{item_id}")
def get_item(request: Request, item_id: str) -> dict:
    """One item: spec (secrets are stored as $secret NAMES, never values), health, run tail."""
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return {
        "id": item["id"], "state": item["state"], "enabled": item["enabled"],
        "interval_minutes": item["interval_minutes"], "last_checked": item["last_checked"],
        "last_status": item["last_status"],
        "consecutive_failures": item["consecutive_failures"],
        "position": item["position"], "spec_rev": item["spec_rev"],
        "spec": item["spec"],  # $secret refs are NAMES only — no plaintext secret ever
        "runs": store.list_runs(item_id, limit=_MAX_RUNS_RETURNED),
    }


@router.post("/api/ni/items/{item_id}/validate")
def validate_item(request: Request, item_id: str, body: ValidateIn) -> dict:
    """C2 verdict: ok=true stamps _c2_ok inside the sealed spec so the next real run
    can transition to live; ok=false rewinds the item to draft (per §6 C2-wrong).
    The note is not persisted in v1 — the operator saw it in the UI at verdict time.

    C1 integrity: refuses (409) unless the item is currently ``commissioning`` —
    a verdict against live/broken/draft has no C1 output to endorse.
    """
    store = _store(request)
    if store.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="item not found")
    try:
        store.record_validation(item_id, body.ok, body.note or "")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"ok": True, "state": store.get_item(item_id)["state"]}


@router.post("/api/ni/items/{item_id}/commission")
def commission_item(request: Request, item_id: str) -> dict:
    """A2: draft -> commissioning (the Activate button on the drafted card).

    Refuses (409) when the current state is not ``draft`` and separately (409) when
    any ``secret``-kind param still has an empty value (the credential must be
    entered via PUT /credential before the engine attempts a run).
    """
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if item["state"] != "draft":
        raise HTTPException(status_code=409,
                            detail=f"commission refused: state={item['state']!r}")
    params = item["spec"].get("params") or {}
    for name, param in params.items():  # bounded by _MAX_PARAMS
        if isinstance(param, dict) and param.get("kind") == "secret" and not param.get("value"):
            raise HTTPException(
                status_code=409,
                detail=f"commission refused: secret param {name!r} not yet filled",
            )
    store.commission(item_id)
    return {"state": "commissioning"}


@router.post("/api/ni/items/{item_id}/run")
def run_item(request: Request, item_id: str) -> dict:
    """Manual refresh: clear last_checked + synchronously run the item (schedule /run parity).

    Uses the request-scoped stores directly — SecretStore and ScheduleStore are Desktop-side
    objects; the tool path never sees them. On success/failure we still ``mark_checked`` so
    the tick's backoff bookkeeping is consistent whether the tick or this endpoint fired it.

    K6: refuses draft (no C1 yet) and broken (permanent refusal) with 409.
    """
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if item["state"] in ("draft", "broken"):
        raise HTTPException(status_code=409,
                            detail=f"run refused: state={item['state']!r}")
    if not item["enabled"]:
        raise HTTPException(status_code=409, detail="run refused: item paused")
    state = request.app.state
    secrets = _secret_store(request)
    schedules = getattr(state, "schedules", None) or ScheduleStore(state.dbx, state.master_key)
    store.clear_last_checked(item_id)
    started = time.monotonic()
    try:
        result = ni.run_item(store, item_id, gateway_mod=gateway,
                             secrets_store=secrets, schedules_store=schedules)
    except ni.NIError as exc:
        store.mark_checked(item_id, exc.kind[:200])
        return {"status": "error", "kind": exc.kind,
                "duration_ms": int((time.monotonic() - started) * 1000)}
    return {"status": "ok", **result}


@router.patch("/api/ni/items/{item_id}")
def patch_item(request: Request, item_id: str, body: PatchIn) -> dict:
    """UI edits ONLY: enabled / position / display. Spec fields go through the write tool."""
    store = _store(request)
    current = store.get_item(item_id)
    if current is None:
        raise HTTPException(status_code=404, detail="item not found")
    if body.enabled is not None:
        store.set_enabled(item_id, body.enabled)
    if body.position is not None:
        store.set_position(item_id, body.position)
    if body.display is not None:
        try:
            ni._validate_display(body.display)  # closed schema check before write
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        new_spec = dict(current["spec"])
        new_spec["display"] = body.display
        store.update_spec(item_id, new_spec, origin="user")
    return {"ok": True}


@router.delete("/api/ni/items/{item_id}")
def delete_item(request: Request, item_id: str) -> dict:
    """Permanent delete; cascades snapshots/revisions/runs in code (no FK — feeds precedent)."""
    store = _store(request)
    if store.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="item not found")
    store.delete(item_id)
    return {"ok": True}


@router.put("/api/ni/items/{item_id}/credential")
def put_credential(request: Request, item_id: str, body: CredentialIn) -> dict:
    """Store a secret parameter value host-bound (Desktop-local; value never travels chat).

    The value is written under ``ni:<item_id>:<name>`` bound to ``host`` — a fetch to any
    other host refuses it (secret_host_mismatch → permanent broken). The audit row carries
    metadata only (item id + name + host); the VALUE never lands in a plaintext log.
    """
    _require_desktop_local(request)
    store = _store(request)
    if store.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="item not found")
    secrets = _secret_store(request)
    ni.put_credential(secrets, item_id, body.name, body.value, body.host)
    request.app.state.audit.append(
        "user", "ni_credential", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id, "name": body.name, "host": body.host}),
        result_summary=tools.summarize({"stored": True}),
    )
    return {"ok": True}
