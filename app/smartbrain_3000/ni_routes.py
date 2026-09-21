"""HTTP surface for Neural Interface items (requires unlock).

The board endpoint is read-only and safe for any unlocked session; the write
endpoints (validate / run / patch / delete / credential) run only when unlocked.
The credential PUT is additionally Desktop-local (the raw value ships in the
body, so a bridged phone must not reach it — same posture as the passphrase
reset and the MCP-token endpoints).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from . import (
    gateway,
    netguard,
    ni,
    ni_catalog,
    ni_flow,
    ni_library,
    ni_mcp,
    ni_watch,
    tools,
    vault_format,
)
from .account import _require_desktop_local
from .data_routes import _reauthorize
from .scheduler import _NI_FEED_ID, ScheduleStore, post_ni_carrier_notices

log = logging.getLogger("smartbrain.ni.routes")

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
    """Restricted PATCH surface: enabled / position / display, plus (P3,
    2026-09-17) the card's Edit modal fields — title and interval_minutes.
    Source/pipeline/scene NEVER ride this route (§10 §9)."""

    enabled: bool | None = None
    position: int | None = Field(default=None, ge=0, le=_MAX_POSITION)
    display: dict | None = None
    title: str | None = Field(default=None, max_length=300)
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)


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


def _board_row(store: ni.NIStore, item: dict, *,
               library_index: dict[str, dict] | None = None,
               library_pack_id: str | None = None,
               spec_hashes: dict[str, str] | None = None,
               secrets_store=None) -> dict:
    """One board row: plaintext operational fields + display + the chosen snapshot's payload.

    ``interpreted`` (§13 honesty): True when a model reads the item's data — either a
    ``model`` source or a pipeline that contains an ``llm`` stage. The card renders an
    "Interpreted" chip so the user can always see which readings came from a model.

    ``template_update`` / ``template_gone`` (§20): populated when the item carries a
    sealed ``_template`` stamp AND a library is connected — True on ``template_update``
    when the stored pack's current template has a different ``spec_hash`` (fleet
    healing); ``template_gone`` when the template id is no longer in the pack (a
    later pack removed it). Absent library ⇒ both False.

    ``needs_credentials`` (§28 Status truth): list of ``{name, label}`` for every
    secret-kind param whose ``ni:<item_id>:<name>`` key is NOT present in the
    SecretStore. The frontend's Add-key modal uses ``name`` for the credential PUT
    body while displaying ``label``. Empty list when the item declares no secret
    params OR when every one has been filled. ``secrets_store=None`` (locked or
    the route couldn't reach one) skips the check — the caller's 423 layer already
    caught it, and an empty list is safer than a false-positive.
    """
    assert store is not None and item, "store + item required"
    snap = _pick_board_snapshot(store, item)
    slot = "preview" if item["state"] == "draft" else (snap.get("slot") if snap else None)
    source_type = (item["spec"].get("source") or {}).get("type")
    interpreted = source_type == "model" or ni._spec_has_llm_stage(item["spec"])
    update_flag, gone_flag = _template_update_flags(item, library_index, library_pack_id,
                                                    spec_hashes)
    return {
        "id": item["id"], "title": item["spec"].get("title", ""),
        "state": item["state"], "enabled": item["enabled"],
        "interval_minutes": item["interval_minutes"],
        "last_checked": item["last_checked"], "last_status": item["last_status"],
        "consecutive_failures": item["consecutive_failures"],
        "position": item["position"],
        "display": item["spec"].get("display") or {"size": "small"},
        "interpreted": interpreted,
        "template_update": update_flag,
        "template_gone": gone_flag,
        "needs_credentials": _needs_credentials(item, secrets_store),
        # needs_params (2026-09-14): unfilled non-secret slots the spec references —
        # the card renders a fill affordance and the engine refuses runs meanwhile.
        "needs_params": _needs_params(item),
        # §23: True when a §14 frontier proposal is parked on this item — the card
        # renders "Fix proposed — review" (Apply / Dismiss are per-item routes).
        "l2_proposal": isinstance(item["spec"].get("_l2_proposal"), dict),
        # F2 (C2-feedback, 2026-09-15): the sealed _c2_ok attestation surfaces
        # so the card can stop asking "is it right?" after the user answered.
        "c2_ok": item["spec"].get("_c2_ok") is True,
        # W2: True while the flow has not replaced the placeholder spec — the
        # card hides Activate and says creation didn't finish.
        "shell": item["spec"].get("_shell") is True,
        # §29 flow record: {state, error?} for any active / terminal-non-ready
        # flow; None once the flow reaches ``ready`` so the tile renders
        # normally. Read from the sealed ``flow`` slot via ni_flow.
        "flow": ni_flow.board_flow_field(store, item["id"]),
        "payload_slot": slot,
        "payload_at": snap["created_at"] if snap else None,
        "payload_ok": snap["ok"] if snap else None,
        "payload": snap["payload"] if snap else None,
    }


def _needs_credentials(item: dict, secrets_store) -> list[dict]:
    """§28 Status truth: return ``[{name, label}]`` for every secret param whose
    ``ni:<item_id>:<name>`` key is ABSENT from the store.

    Kept as a route-layer helper (not on the tool side) because the tool has no
    SecretStore access by construction — the credential firewall §9 rule. The
    board is Desktop-side and DOES have the store, so a truthful list is what
    the card renders. A store-side read failure treats the key as ABSENT so the
    card still nudges the user to add / re-add the credential (defence-in-depth
    over the mirrored S5 route check at commission time).
    """
    assert isinstance(item, dict), "item required"
    params = item["spec"].get("params") or {}
    if not isinstance(params, dict) or secrets_store is None:
        return []
    out: list[dict] = []
    for name, decl in params.items():  # bounded by ni._MAX_PARAMS
        if not (isinstance(decl, dict) and decl.get("kind") == "secret"):
            continue
        label = decl.get("label") if isinstance(decl.get("label"), str) else ""
        key = f"ni:{item['id']}:{name}"
        try:
            stored = secrets_store.get(key)
        except Exception:  # unreadable/malformed — treat as absent, never a route error
            stored = None
        if not stored:
            out.append({"name": str(name), "label": label or str(name)})
    return out


def _needs_params(item: dict) -> list[dict]:
    """needs_params (2026-09-14): ``[{name, label}]`` for every NON-secret param that
    the spec actually references via ``{{param:X}}`` but whose value is still empty.

    The generic class behind the $0.00 Finnhub card: an item whose frozen URL (or any
    other spec string) carries an unfilled slot must say so on the card — the engine
    refuses the run (``param_empty``), commission refuses activation, and this list is
    what the tile renders as a fill affordance. Referenced-ness is computed over the
    whole spec MINUS the params block itself, so a declared-but-unused slot never
    blocks anything.
    """
    assert isinstance(item, dict), "item required"
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    return [{"name": name, "label": _param_label(item, name)}
            for name in ni.unfilled_referenced_params(spec)]


def _template_update_flags(item: dict, library_index: dict[str, dict] | None,
                            library_pack_id: str | None,
                            spec_hashes: dict[str, str] | None) -> tuple[bool, bool]:
    """Compute (template_update, template_gone) for one item row (§20).

    LOW#1 (audit 2026-09-09): ``spec_hashes`` is the per-pack precomputed
    {template_id: spec_hash} map from ``LibraryStore.cached_index_and_hashes`` so a
    board poll over 200 items does not re-hash 200 templates on every request.
    """
    assert isinstance(item, dict), "item must be a dict"
    provenance = item["spec"].get("_template")
    if not isinstance(provenance, dict) or library_index is None or library_pack_id is None:
        return False, False
    if provenance.get("pack_id") != library_pack_id:
        return False, False  # this item came from a different pack — no signal here
    template_id = provenance.get("template_id") or ""
    template = library_index.get(template_id)
    if template is None:
        return False, True   # the template was removed in a later pack — surface it
    current_hash = (spec_hashes or {}).get(template_id)
    if current_hash is None:  # cache miss fallback (should not happen — same source)
        current_hash = ni_library.spec_hash(template.get("spec_template") or {})
    return current_hash != provenance.get("spec_hash"), False


def _load_library_index(store: ni.NIStore) -> tuple[
        dict[str, dict] | None, str | None, dict[str, str] | None]:
    """Read the cached (pack_index, pack_id, spec_hashes) or (None, None, None).

    LOW#1: uses ``LibraryStore.cached_index_and_hashes`` so a board poll runs one
    pack decrypt + one spec_hash pass per unique pack timestamp, not per request.
    """
    assert store is not None, "store required"
    library = ni_library.LibraryStore(store)
    cached = library.cached_index_and_hashes()
    if cached is None:
        return None, None, None
    index, hashes, pack_id = cached
    return index, pack_id, hashes


@router.get("/api/ni/board")
def board(request: Request) -> dict:
    """All items + decrypted bound payload, sorted by position (already the store's order).

    Each row carries ``template_update`` (§20 fleet healing) when a library is connected
    and the item's sealed ``_template.spec_hash`` differs from the currently-stored pack's
    template hash; ``template_gone`` when the template id was removed in a later pack.
    """
    store = _store(request)
    library_index, library_pack_id, spec_hashes = _load_library_index(store)
    # ``secret_store`` may be absent from ``app.state`` (a non-desktop context or
    # a route hit before startup completes) — getattr keeps the board renderable
    # even then; ``_needs_credentials`` degrades to an empty list.
    secrets_store = getattr(request.app.state, "secret_store", None)
    return {"items": [
        _board_row(store, item, library_index=library_index,
                   library_pack_id=library_pack_id, spec_hashes=spec_hashes,
                   secrets_store=secrets_store)
        for item in store.list_items()  # bounded by ni._MAX_ITEMS
    ]}


# Literal paths would live here if any existed — /api/ni/items/... has no literal
# children (validate/run/credential are all under /{id}/...), so the ordering
# comment is here for parity with schedule_routes even though nothing shadows it.
# (/api/ni/notices below is literal too, but lives outside /items/ entirely.)


_MAX_NOTICES = 20  # §17: limit clamp for the launcher's notice poll
_DEFAULT_NOTICES = 10
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# status -> kind (§17). post_ni_carrier_notices writes "complete" for fired
# alerts, "broken" for broken transitions, "repaired" for §14 self-repairs,
# "proposal" for §23 L2 proposals; anything unexpected reads as the mildest kind
# rather than being dropped. The launcher's per-kind title switch (launcher/
# notices.go) falls back to "SmartBrain alert" for unknown kinds — verified by
# notices_test.go's unknown-kind fallback — so "proposal" surfaces on
# macOS/Linux trays without a launcher rebuild (Windows has no Notify
# implementation yet — stack.Notify is a no-op there, documented in
# launcher/notices.go).
_NOTICE_KIND_BY_STATUS = {"broken": "broken", "repaired": "repaired",
                          "proposal": "proposal"}


def _notice_id(ran_at: str) -> int:
    """Stable, monotonic notice id: the run's UTC timestamp in microseconds.

    schedule_runs primary keys are UUIDs (record_run), which cannot serve the
    launcher's highest-seen-id dedupe. ran_at is UTC (the DB session is pinned —
    see db.open_db), immutable, and microsecond-granular, so the derived integer
    is stable across polls and ordered by insertion. Two notices landing in the
    same microsecond would share an id — accepted: autocommit inserts are
    microseconds apart in practice, and a coalesced toast still shows on the board.
    """
    assert ran_at, "ran_at required"
    stamp = datetime.fromisoformat(str(ran_at))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (stamp - _EPOCH) // timedelta(microseconds=1)


@router.get("/api/ni/notices")
def list_notices(request: Request, limit: int = _DEFAULT_NOTICES) -> list[dict]:
    """Newest NI carrier notices for the launcher's tray notifications (§17).

    Desktop-local (the launcher calls 127.0.0.1 directly; a bridged phone must not
    pull notice bodies) AND unlocked-only — a locked vault answers 423, which the
    launcher treats as "skip", so sealed content never crosses the unlock boundary.
    Bodies are the already-sanitized carrier messages (§12 H1 guard upstream).
    """
    _require_desktop_local(request)
    _store(request)  # 423 while locked — the notices surface simply goes dark
    state = request.app.state
    schedules = getattr(state, "schedules", None) or ScheduleStore(state.dbx, state.master_key)
    runs = schedules.list_runs(_NI_FEED_ID, limit=min(max(int(limit), 1), _MAX_NOTICES))
    return [
        {
            "id": _notice_id(run["ran_at"]),
            "kind": _NOTICE_KIND_BY_STATUS.get(run["status"], "alert"),
            "body": run["message"],
            "ts": run["ran_at"],
        }
        for run in runs  # list_runs is newest-first and bounded by the clamp above
    ]


@router.get("/api/ni/items/{item_id}/image")
def get_item_image(request: Request, item_id: str) -> Response:
    """§24: serve the sealed image bytes with the SNIFFED media type.

    ``Cache-Control: no-store`` so a stale response never masks a fresh fetch;
    the src carries ``?v=<created_at>`` for browser cache-busting when the same
    URL is reused across renders. 404 when the item has no image slot yet
    (draft, first-run pending, or a non-image source). 423 while locked.
    Registered BEFORE ``GET /items/{item_id}`` per the schedule_routes ordering
    convention — FastAPI matches most-specific first and the two paths differ,
    but keeping literals first keeps the surface obvious to a reader.
    """
    store = _store(request)
    if store.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="item not found")
    snap = store.read_image_snapshot(item_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="image not available")
    media_type = ni._IMAGE_MEDIA_BY_FORMAT.get(snap["format"], "application/octet-stream")
    return Response(content=snap["bytes"], media_type=media_type,
                    headers={"Cache-Control": "no-store"})


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
    if not body.ok:
        # §28: the user's verbatim note rides the journal so a later reader (a
        # model or the operator) sees the human authorship. Kind ``c2_wrong``
        # names the verdict; the summary is the note (bounded by the store).
        note = (body.note or "").strip()
        _journal_best_effort(store, item_id, "c2_wrong",
                             f"user rejected first run: {note or '(no note)'}")
        # G4a: a note ACTS — begin_refine routes it (cadence / new source /
        # rebuild-with-note). A refusal (source type can't refine) degrades to
        # the plain rewind with the guidance surfaced, never a 500.
        refined = None
        if note:
            item_now = store.get_item(item_id)
            try:
                refined = ni_flow.begin_refine(store, item_now, note)
            except ValueError as exc:
                _journal_best_effort(store, item_id, "c2_wrong",
                                     f"note could not drive a rebuild: {exc}")
        return {"ok": True, "state": store.get_item(item_id)["state"],
                "refine": (refined or {}).get("kind")}
    # F1 (C2-feedback, 2026-09-15): "Looks right" used to leave the card in
    # Commissioning until the NEXT scheduled run performed the C3 contract
    # check — up to a full cadence window (the 30-minute NVDA card) with the
    # SAME banner re-rendering, so the tap read as a dead button (the field
    # run logged three 200s from one confused user). The verdict now kicks
    # the C3 proof run immediately and synchronously; a run failure is
    # reported honestly but never turns the recorded verdict into an error.
    run_result: dict = {}
    refreshed = store.get_item(item_id)
    if refreshed is not None and refreshed["state"] == "commissioning"             and refreshed["enabled"]:
        try:
            run_result = _execute_manual_run(request, store, refreshed)
        except Exception as exc:  # verdict already recorded — degrade honestly
            log.warning("ni validate: C3 kick failed: %s", exc)
            run_result = {"status": "error", "kind": "internal"}
    final = store.get_item(item_id)
    return {"ok": True, "state": final["state"] if final else "unknown",
            "run": run_result.get("status") or "skipped"}


@router.post("/api/ni/items/{item_id}/commission")
def commission_item(request: Request, item_id: str) -> dict:
    """A2: draft -> commissioning (the Activate button on the drafted card).

    Refuses (409) when the current state is not ``draft`` and separately (409) when
    any ``secret``-kind param still has an empty value (the credential must be
    entered via PUT /credential before the engine attempts a run).

    S5 (audit 2026-09-12): a secret param whose value is a ``ni:...`` placeholder
    (installed-template shape) is treated as UNFILLED unless the SecretStore
    actually holds a value under that key — so a template install that never had
    its credential PUT'd refuses commission with the same clear detail instead
    of dying on the next fetch with ``secret_missing``.

    S6 (audit 2026-09-12): a successful commission writes an audit row so the
    consent event is on the spine (metadata: item_id only — no titles / content).
    """
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if item["state"] != "draft":
        raise HTTPException(status_code=409,
                            detail=f"commission refused: state={item['state']!r}")
    # W2 (field 2026-09-15): a flow shell whose creation never finished must
    # not be activatable — the placeholder model source would run and report
    # "ok" on a card that renders "Preparing card…" forever. The flow's
    # finalize replaces the spec (dropping ``_shell``); until then the honest
    # answers are retry-in-chat or delete.
    if item["spec"].get("_shell") is True:
        raise HTTPException(
            status_code=409,
            detail=("commission refused: this card's creation flow never "
                    "finished — ask in chat to retry it, or delete the card"),
        )
    secrets = getattr(request.app.state, "secret_store", None)
    params = item["spec"].get("params") or {}
    for name, param in params.items():  # bounded by _MAX_PARAMS
        if not (isinstance(param, dict) and param.get("kind") == "secret"):
            continue
        value = param.get("value")
        if not value:
            raise HTTPException(
                status_code=409,
                detail=f"commission refused: secret param {name!r} not yet filled",
            )
        # S5: a ``ni:...`` placeholder counts as filled ONLY when the SecretStore
        # actually holds a value under that key. secrets=None means locked —
        # _store() already 423'd, so this branch is defensive.
        if isinstance(value, str) and value.startswith("ni:") and secrets is not None:
            try:
                stored = secrets.get(value)
            except Exception:  # decrypt / catalog issue — treat as unfilled
                stored = None
            if not stored:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"commission refused: secret param {name!r} not yet filled"
                    ),
                )
    # needs_params (2026-09-14): the same refusal for NON-secret slots the spec
    # references — commissioning an item whose frozen URL still reads
    # ``?symbol={{param:symbol}}`` with an empty value produced a "commissioning ok"
    # $0.00 card (Finnhub returns sentinel zeros for an empty symbol). The engine
    # also refuses the run (``param_empty``); this check keeps the Activate tap
    # honest instead of deferring the failure to the first run.
    unfilled = _needs_params(item)
    if unfilled:
        first = unfilled[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"commission refused: param {first['name']!r} ({first['label']}) "
                "not yet filled"
            ),
        )
    store.commission(item_id)
    _journal_best_effort(store, item_id, "commissioned",
                         "user activated the card (draft -> commissioning)")
    # H1 (audit 2026-09-13): a flow-authored item's ``awaiting_credential``
    # slot lingered after the user Activated the card, so board_flow_field
    # kept returning the stale flow state indefinitely. Commission is the
    # honest terminal for the flow's lifecycle — drop the slot here so the
    # board renders the tile normally from now on.
    ni_flow.clear_flow_slot(store, item_id)
    request.app.state.audit.append(
        "user", "ni_commission", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id}),
        result_summary=tools.summarize({"state": "commissioning"}),
    )
    return {"state": "commissioning"}


def _journal_best_effort(store: ni.NIStore, item_id: str, kind: str,
                          summary: str) -> None:
    """§28 route-side journal writer. Never raises past the route boundary:
    a store hiccup here would otherwise turn a successful commission /
    validate / repair-policy edit into a 500.
    """
    assert store is not None and item_id and kind, "args required"
    try:
        store.append_journal(item_id, kind, summary)
    except Exception as exc:  # bookkeeping only
        log.warning("ni route journal append failed: item=%s kind=%s: %s",
                    item_id, kind, exc)


@router.post("/api/ni/items/{item_id}/run")
def run_item(request: Request, item_id: str) -> dict:
    """Manual refresh: clear last_checked + synchronously run the item (schedule /run parity).

    Uses the request-scoped stores directly — SecretStore and ScheduleStore are Desktop-side
    objects; the tool path never sees them. On success/failure we still ``mark_checked`` so
    the tick's backoff bookkeeping is consistent whether the tick or this endpoint fired it.

    M1b (audit 2026-09-09): fired alerts + broken-transition notices land on the NI
    carrier row exactly like ``_auto_update_ni`` — the surfacing surface must not depend
    on whether the run came from the tick or a manual /run click. Posting is best-effort
    (a locked carrier or a record_ni_run failure must never turn a successful run into
    a route error).

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
    return _execute_manual_run(request, store, item)


def _execute_manual_run(request: Request, store: ni.NIStore, item: dict) -> dict:
    """Shared synchronous run body for /run and the C2 validate kick (F1).

    L7 (audit 2026-09-12): mark_checked BEFORE the synchronous run instead of
    clear_last_checked — clearing would leave the item due for the concurrent
    tick to pick up during our multi-second fetch (a double-run window). The
    tick's due gate reads last_checked; a fresh timestamp (with a distinct
    ``manual`` status so telemetry stays honest) keeps the item out of the
    next tick's due set until our own run stamps its own status via run_item's
    finalize path.
    """
    assert store is not None and isinstance(item, dict), "store + item required"
    state = request.app.state
    secrets = _secret_store(request)
    schedules = getattr(state, "schedules", None) or ScheduleStore(state.dbx, state.master_key)
    item_id = item["id"]
    prior_state = item["state"]
    store.mark_checked(item_id, "manual")
    started = time.monotonic()
    try:
        # kb rides along so internal.kb items work on manual refresh too, not
        # just engine ticks (None when locked mid-request — run_item refuses).
        result = ni.run_item(store, item_id, gateway_mod=gateway,
                             secrets_store=secrets, schedules_store=schedules,
                             kb=getattr(state, "kb", None))
    except ni.NIError as exc:
        store.mark_checked(item_id, exc.kind[:200])
        _post_carrier_after_run(schedules, store, item_id, prior_state, item,
                                alerts=[], repaired=[])
        return {"status": "error", "kind": exc.kind,
                "duration_ms": int((time.monotonic() - started) * 1000)}
    _post_carrier_after_run(schedules, store, item_id, prior_state, item,
                            alerts=result.get("alerts") or [],
                            repaired=result.get("repaired") or [])
    return {"status": "ok", **result}


def _post_carrier_after_run(schedules, store: ni.NIStore, item_id: str,
                            prior_state: str, prior_item: dict, *, alerts: list,
                            repaired: list) -> None:
    """M1b helper: post fired alerts + any this-run broken transition to the carrier.

    D6 (audit 2026-09-09): a §14 repair trial can succeed on a manual /run just as
    on a tick pass; the ``repaired`` list from ``ni.run_item`` must ride the same
    carrier path as it does from ``_auto_update_ni`` — otherwise the user misses
    the "<title> repaired itself" notice when they clicked Refresh themselves.

    Never raises — surfacing is best-effort and must not turn a completed /run into a
    route error. A locked carrier or a record_ni_run failure logs and returns.
    """
    assert schedules is not None and store is not None, "schedules + store required"
    assert item_id and prior_item is not None, "item context required"
    assert isinstance(alerts, list) and isinstance(repaired, list), "alerts + repaired must be lists"
    broken: list = []
    try:
        ni._collect_broken_transition(store, item_id, prior_state, prior_item, broken)
    except Exception as exc:  # collection must not shadow a real run outcome
        log.warning("ni carrier: broken-transition collect failed: %s", exc)
        broken = []
    if not alerts and not broken and not repaired:
        return
    try:
        post_ni_carrier_notices(schedules, alerts, broken, repaired=repaired)
    except Exception as exc:
        log.warning("ni carrier: manual-run post failed: %s", exc)


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
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title must be non-empty")
        new_spec = dict(current["spec"])
        new_spec["title"] = title[:300]
        store.update_spec(item_id, new_spec, origin="user",
                          preserve_attestations=True)
        _journal_best_effort(store, item_id, "updated",
                              f"renamed to {title[:60]!r} on the card")
        current = store.get_item(item_id)
    if body.interval_minutes is not None:
        new_spec = dict((store.get_item(item_id) or current)["spec"])
        new_spec["interval_minutes"] = int(body.interval_minutes)
        # Cadence is operational, not a source change — the contract
        # fingerprints SHAPE, so the C1/C2 attestations survive (display-PATCH
        # precedent); the store re-clamps to its floor on write.
        store.update_spec(item_id, new_spec, origin="user",
                          preserve_attestations=True)
        _journal_best_effort(store, item_id, "updated",
                              f"cadence set to every {int(body.interval_minutes)}m on the card")
    if body.display is not None:
        try:
            ni._validate_display(body.display)  # closed schema check before write
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        new_spec = dict(current["spec"])
        new_spec["display"] = body.display
        # L2 (audit 2026-09-12): display is a purely cosmetic field the contract
        # never fingerprinted — preserve the C1/C2 attestations and streak so a
        # visual toggle can't kick a live item out of C3 into a contract
        # dead-end.
        store.update_spec(item_id, new_spec, origin="user",
                          preserve_attestations=True)
    return {"ok": True}


@router.delete("/api/ni/items/{item_id}")
def delete_item(request: Request, item_id: str) -> dict:
    """Permanent delete; cascades snapshots/revisions/runs in code (no FK — feeds precedent).

    L9 (audit 2026-09-12): also best-effort deletes every ``ni:<item_id>:<name>``
    secret key stored under this item's namespace (from the sealed spec's secret
    params). Without this, credentials outlive their owning item — unreachable by
    the delete tool (no SecretStore in ToolContext by design) and invisible to the
    Providers-only ``/api/secrets`` surface. Delete failures are swallowed so a
    stuck secret never blocks the item cascade.
    """
    store = _store(request)
    if store.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="item not found")
    # Enumerate secret keys from the sealed spec BEFORE the item row disappears.
    item = store.get_item(item_id)
    params = (item["spec"].get("params") or {}) if item is not None else {}
    secrets = getattr(request.app.state, "secret_store", None)
    if secrets is not None and isinstance(params, dict):
        for name, param in params.items():  # bounded by ni._MAX_PARAMS
            if not (isinstance(param, dict) and param.get("kind") == "secret"):
                continue
            key = f"ni:{item_id}:{name}"
            try:
                secrets.delete(key)
            except Exception as exc:  # best-effort: never block the item cascade
                log.warning("ni delete: secret drop failed for %s: %s", key, exc)
    store.delete(item_id)
    return {"ok": True}


@router.put("/api/ni/items/{item_id}/credential")
def put_credential(request: Request, item_id: str, body: CredentialIn) -> dict:
    """Store a secret parameter value host-bound (Desktop-local; value never travels chat).

    The value is written under ``ni:<item_id>:<name>`` bound to ``host`` — a fetch to any
    other host refuses it (secret_host_mismatch → permanent broken). The audit row carries
    metadata only (item id + name + host); the VALUE never lands in a plaintext log.

    §28 journal: a successful PUT lands a ``param_changed`` entry naming the
    param's user-visible label (falling back to the param name), so the card
    history shows "credential '<label>' added" when the user filled in a key.
    Value bytes never touch the journal (summary is code-composed metadata).
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    secrets = _secret_store(request)
    ni.put_credential(secrets, item_id, body.name, body.value, body.host)
    request.app.state.audit.append(
        "user", "ni_credential", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id, "name": body.name, "host": body.host}),
        result_summary=tools.summarize({"stored": True}),
    )
    _journal_best_effort(store, item_id, "param_changed",
                         f"credential {_param_label(item, body.name)!r} added")
    # H1 (audit 2026-09-13): if the flow record is in ``awaiting_credential``
    # AND every declared secret has now been filled in the SecretStore, drop
    # the flow slot so the tile stops rendering "Needs your API key" the
    # moment the last key lands. Best-effort — the credential PUT itself
    # succeeded; a flow-slot clean-up failure never turns that into a route
    # error (mirrors _journal_best_effort).
    _clear_flow_when_credentials_satisfied(store, item, secrets)
    return {"ok": True}


class IntakeIn(BaseModel):
    """NI Foreman P1: the /ni composer's request — creation without chat."""

    request: str = Field(min_length=3, max_length=2000)
    source_url: str | None = Field(default=None, max_length=2000)
    allow_duplicate: bool = False


@router.post("/api/ni/intake")
def intake(request: Request, body: IntakeIn) -> dict:
    """NI Foreman P1 (2026-09-16): the composer on /ni — the PRIMARY creation
    surface. The user's sentence goes straight to the deterministic flow
    engine: shell card immediately (instant acknowledgment, before any model
    call), single-flight worker, every subsequent step a card affordance
    (Approve source / Add key / Fill / Activate / Looks right / Retry). No
    chat model anywhere in the path — the operator-ruled manager posture.

    Desktop-local (creation is consent-bearing); audited as a user action.
    ``source_url`` (optional) is the user's own URL — validated for shape here
    and by netguard at the sampling fetch.
    """
    _require_desktop_local(request)
    store = _store(request)
    text = body.request.strip()
    if not text:
        raise HTTPException(status_code=400, detail="request required")
    source_url = (body.source_url or "").strip() or None
    if source_url is not None:
        try:
            ni._validate_http_json_url_shape(source_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"source_url: {exc}") from None
    try:
        item_id = ni_flow.create_shell_item(store, text,
                                             allow_duplicate=body.allow_duplicate)
    except ValueError as exc:  # duplicate title — name the existing card
        raise HTTPException(status_code=409, detail=str(exc)) from None
    started = ni_flow.start_flow_worker(store, item_id, source_url=source_url)
    request.app.state.audit.append(
        "user", "ni_intake", "reviewed", "executed", True,
        args_summary=tools.summarize({"request": text[:200],
                                       "source_url": source_url}),
        result_summary=tools.summarize({"id": item_id, "started": bool(started)}),
    )
    return {"id": item_id, "started": bool(started)}


@router.post("/api/ni/items/{item_id}/flow/retry")
def retry_flow(request: Request, item_id: str) -> dict:
    """NI Foreman P1: the failed-creation card's Retry — re-run the flow with
    the SAME sealed request (and optionally the same user URL). Allowed only
    on a shell whose flow ended terminally (failed/unsupported); a healthy or
    running flow refuses (409). Deterministic: nothing about the request or
    consent state changes — this is 'try the same thing again', honestly.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if item["spec"].get("_shell") is not True:
        raise HTTPException(status_code=409,
                            detail="retry is for unfinished creations only")
    record = ni_flow._flow_read(store, item_id) or {}
    state = str(record.get("state") or "")
    if state not in ("failed", "unsupported"):
        raise HTTPException(
            status_code=409,
            detail=f"nothing to retry (flow state {state or 'none'!r})")
    request_text = str(record.get("request") or item["spec"].get("goal") or "")
    if not request_text:
        raise HTTPException(status_code=409, detail="flow record lost the request")
    source_url = record.get("source_url")
    # A failed SAMPLING url should not silently re-ride a retry that the user
    # intends as a fresh start — but a user-named URL was their consent, so it
    # stays. Heuristic: keep the URL only when the failure was NOT at fetch.
    error = str(record.get("error") or "")
    # Consent guard (claims audit 2026-09-21): a record that died at the
    # confirm pause carries the recipe URL the user NEVER approved — a retry
    # must not promote it to a consented fetch.
    died_unapproved = "confirm" in error or bool(record.get("_recipe_id"))
    keep_url = isinstance(source_url, str) and bool(source_url) and \
        not error.startswith("fetch") and not died_unapproved
    # Claims audit 2026-09-21: clear-then-spawn CRASHED the worker ("no flow
    # record") — Retry never worked; the mocked route test was the mask.
    # RE-SEED the record with the sealed request instead of clearing it.
    ni_flow._flow_write(store, item_id, ni_flow._make_record(
        request_text, "intent",
        source_url=source_url if keep_url else None,
        notes=["retrying the build with the same request"]))
    started = ni_flow.start_flow_worker(
        store, item_id, source_url=source_url if keep_url else None)
    request.app.state.audit.append(
        "user", "ni_intake_retry", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id}),
        result_summary=tools.summarize({"started": bool(started)}),
    )
    return {"id": item_id, "started": bool(started)}


class PickSourceIn(BaseModel):
    """P3: the source-pick card's paste-a-URL — the universal generic path."""

    url: str = Field(min_length=8, max_length=2000)


@router.post("/api/ni/items/{item_id}/flow/pick-source")
def pick_flow_source(request: Request, item_id: str, body: PickSourceIn) -> dict:
    """P3 (2026-09-17): resume a source-pick pause with a URL the USER pasted
    on the card — their paste is the consent (the same posture the chat
    resume tool carried); netguard guards the sampling fetch as always.
    Desktop-local, audited. 409 unless the flow is paused at ``source``.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    record = ni_flow._flow_read(store, item_id)
    state = str((record or {}).get("state") or "")
    if record is None or state != "source" \
            or record.get("error") != ni_flow.AWAITING_SOURCE_PICK:
        raise HTTPException(
            status_code=409,
            detail="the card isn't asking for a source right now — it may "
                   "still be searching; give it a moment")
    url = body.url.strip()
    try:
        ni._validate_http_json_url_shape(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"url: {exc}") from None
    started = ni_flow.start_flow_worker(store, item_id, source_url=url)
    request.app.state.audit.append(
        "user", "ni_flow_pick_source", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id, "url": url}),
        result_summary=tools.summarize({"started": bool(started)}),
    )
    return {"ok": True, "started": bool(started)}


class PickRecipeIn(BaseModel):
    """P3: the source-pick card's vetted-suggestion tap."""

    recipe_id: str = Field(min_length=1, max_length=80)


@router.post("/api/ni/items/{item_id}/flow/pick-recipe")
def pick_flow_recipe(request: Request, item_id: str, body: PickRecipeIn) -> dict:
    """P3: route a source-pick pause into the standard ``confirm_source``
    consent for a catalog recipe the user tapped — the Approve-source card
    (exact URL, sealed fills, geocode/coverage disclosures) takes over from
    there. Deterministic end to end; 409 unless paused at ``source``.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    record = ni_flow._flow_read(store, item_id)
    state = str((record or {}).get("state") or "")
    if record is None or state != "source" \
            or record.get("error") != ni_flow.AWAITING_SOURCE_PICK:
        raise HTTPException(
            status_code=409,
            detail="the card isn't asking for a source right now — it may "
                   "still be searching; give it a moment")
    recipe = ni_catalog.get_recipe(body.recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="unknown recipe")
    intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
    ni_flow._pause_for_recipe_confirm(store, item_id, intent, recipe,
                                       call_model=ni_flow.default_call_model(store))
    request.app.state.audit.append(
        "user", "ni_flow_pick_recipe", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id,
                                       "recipe_id": body.recipe_id}),
        result_summary=tools.summarize({"state": "confirm_source"}),
    )
    return {"ok": True, "state": "confirm_source"}


@router.post("/api/ni/items/{item_id}/flow/fix")
def fix_item_flow(request: Request, item_id: str) -> dict:
    """P3: the card's Fix — re-derive this card against its OWN frozen source
    (the shared remap entry: http_json only, params filled, never a new
    host). Desktop-local, audited. ValueError guidance surfaces as 409.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    try:
        started = ni_flow.begin_remap(store, item)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    request.app.state.audit.append(
        "user", "ni_flow_fix", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id}),
        result_summary=tools.summarize({"started": bool(started)}),
    )
    return {"ok": True, "started": bool(started)}


@router.post("/api/ni/items/{item_id}/flow/confirm-source")
def confirm_flow_source(request: Request, item_id: str) -> dict:
    """Card-consent (2026-09-15): the tile's own [Approve source] tap.

    Deterministic by construction: the tap approves EXACTLY what the sealed
    flow record holds — the card displayed ``source_url`` (+ geocode lookup +
    not_covered) straight from that record via ``board_flow_field``, and this
    route re-reads the record itself; no caller-supplied URL exists to drift.
    The chat tool (``confirm_ni_flow_source``) remains as an alternative
    surface, but a wandering chat model can no longer strand the consent —
    the affordance renders the moment the flow pauses, from code alone.

    Desktop-local (consent-bearing, like credential/param PUTs); audited as a
    user consent event with the approved URL's host in the metadata. 409 when
    the flow is not awaiting confirmation. Runs the continuation
    synchronously (recipe handoff + optional consented geocode — seconds).
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    record = ni_flow._flow_read(store, item_id)
    state = str((record or {}).get("state") or "")
    if record is None or state != "confirm_source":
        raise HTTPException(
            status_code=409,
            detail=f"no source confirmation pending (flow state {state or 'none'!r})")
    source_url = str(record.get("source_url") or "")
    if not source_url:
        raise HTTPException(status_code=409,
                            detail="flow record carries no source URL")
    try:
        result = ni_flow.continue_from_recipe_confirm(store, item_id, source_url)
    except ValueError as exc:  # raced by a concurrent confirm — report honestly
        raise HTTPException(status_code=409, detail=str(exc)) from None
    request.app.state.audit.append(
        "user", "ni_flow_confirm_source", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id,
                                       "source_url": source_url}),
        result_summary=tools.summarize({"state": str(result.get("state") or "")}),
    )
    _journal_best_effort(store, item_id, "source_changed",
                          f"user approved the source on the card ({_host_of(source_url)})")
    return {"ok": True, "state": str(result.get("state") or ""),
            "item_state": (store.get_item(item_id) or {}).get("state")}


@router.post("/api/ni/items/{item_id}/flow/decline-source")
def decline_flow_source(request: Request, item_id: str) -> dict:
    """Card-consent: the tile's [Not this source] tap — a fork, not a death.

    G1 (field 2026-09-17): declining used to fail the flow terminally and the
    card went dead. Now the flow RE-ENTERS the ``source`` pick pause, so the
    card immediately offers the other vetted suggestions plus paste-a-URL.
    Never a fetch; audited like the approval.
    """
    _require_desktop_local(request)
    store = _store(request)
    if store.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="item not found")
    record = ni_flow._flow_read(store, item_id)
    state = str((record or {}).get("state") or "")
    if record is None or state != "confirm_source":
        raise HTTPException(
            status_code=409,
            detail=f"no source confirmation pending (flow state {state or 'none'!r})")
    ni_flow.reenter_source_pick(store, item_id,
                                 "user declined the source — picking again")
    request.app.state.audit.append(
        "user", "ni_flow_decline_source", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id}),
        result_summary=tools.summarize({"state": "source"}),
    )
    _journal_best_effort(store, item_id, "c2_wrong",
                          "user declined the proposed source")
    return {"ok": True, "state": "source"}


class RefineIn(BaseModel):
    """G4a: the card's Refine… note — the user's words drive a rebuild."""

    note: str = Field(min_length=3, max_length=500)


@router.post("/api/ni/items/{item_id}/refine")
def refine_item(request: Request, item_id: str, body: RefineIn) -> dict:
    """G4a (SUSTAIN.refine): rebuild this card from the user's note.

    Deterministic routing (cadence note → interval update; source-change note
    → the source pick pause; anything else → re-sample the item's OWN frozen
    source with the note sealed as part of the goal, judge-verified).
    Desktop-local, audited; ValueError guidance surfaces as 409.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    try:
        result = ni_flow.begin_refine(store, item, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    request.app.state.audit.append(
        "user", "ni_refine", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id,
                                       "note": body.note[:120]}),
        result_summary=tools.summarize(result),
    )
    return {"ok": True, **result}


class AnswerIn(BaseModel):
    """G1: the card's answer to a question the master asked (closed kinds)."""

    kind: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=200)


@router.post("/api/ni/items/{item_id}/flow/answer")
def answer_flow_question(request: Request, item_id: str, body: AnswerIn) -> dict:
    """G1: resume an answerable terminal with the user's typed answer.

    ``supply_date``: the user's date (YYYY-MM-DD) is stamped ``_supplied`` on
    the flow record (their typed answer is the truth — never a model's guess)
    and the worker re-runs the sealed request. 409 unless the record's stamped
    question matches the answered kind. Desktop-local, audited.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    record = ni_flow._flow_read(store, item_id)
    stamped = (record or {}).get("_question") if isinstance(record, dict) else None
    if not isinstance(stamped, dict) or stamped.get("kind") != body.kind:
        raise HTTPException(status_code=409,
                            detail="this card is not asking that question")
    if body.kind != "supply_date":
        raise HTTPException(status_code=409, detail="unanswerable question kind")
    value = body.value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(status_code=400,
                            detail="date must be written as YYYY-MM-DD")
    ni_flow._transition(store, item_id, "intent",
                         note=f"user supplied the date {value}",
                         _supplied={"date": value}, _question=None)
    started = ni_flow.start_flow_worker(store, item_id)
    request.app.state.audit.append(
        "user", "ni_flow_answer", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id, "kind": body.kind}),
        result_summary=tools.summarize({"started": bool(started)}),
    )
    return {"ok": True, "started": bool(started)}


@router.post("/api/ni/items/{item_id}/flow/reopen")
def reopen_flow_pick(request: Request, item_id: str) -> dict:
    """G1: a terminally failed SHELL re-enters the source pick — the "pick a
    different source" way out the no-dead-end law promises. 409 for finalized
    cards (Fix/remap owns those) and non-terminal flows.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if not item["spec"].get("_shell"):
        raise HTTPException(status_code=409,
                            detail="finished cards reopen via Fix, not the source pick")
    record = ni_flow._flow_read(store, item_id)
    state = str((record or {}).get("state") or "")
    if record is None or state not in ("failed", "unsupported"):
        raise HTTPException(status_code=409,
                            detail=f"nothing to reopen (flow state {state or 'none'!r})")
    ni_flow.reenter_source_pick(store, item_id, "user reopened the source pick")
    request.app.state.audit.append(
        "user", "ni_flow_reopen", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id}),
        result_summary=tools.summarize({"state": "source"}),
    )
    return {"ok": True, "state": "source"}


@router.get("/api/ni/findings")
def get_findings(request: Request, limit: int = 50) -> dict:
    """G1 oversight surface: open watcher findings, newest first (desktop-local)."""
    _require_desktop_local(request)
    store = _store(request)
    return {"findings": ni_watch.list_findings(store.conn, limit=limit)}


@router.post("/api/ni/findings/{finding_id}/resolve")
def resolve_finding_route(request: Request, finding_id: str) -> dict:
    """Mark one finding resolved from the Health surface. 404 when absent/closed."""
    _require_desktop_local(request)
    store = _store(request)
    if not ni_watch.resolve_finding(store.conn, finding_id):
        raise HTTPException(status_code=404, detail="finding not found or already resolved")
    return {"ok": True}


def _host_of(url: str) -> str:
    """Hostname for audit/journal lines (host-free-content rule: host only)."""
    assert isinstance(url, str), "url required"
    try:
        return urlparse(url).hostname or "unknown"
    except ValueError:
        return "unknown"


class ParamIn(BaseModel):
    """Fill one NON-secret parameter value (needs_params affordance, 2026-09-14)."""

    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=500)


@router.put("/api/ni/items/{item_id}/param")
def put_param(request: Request, item_id: str, body: ParamIn) -> dict:
    """Fill a non-secret param value from the card (Desktop-local; audited).

    The needs_params counterpart of the credential PUT: a recipe- or flow-born
    card whose ``{{param:X}}`` slot code could not derive lands draft with a
    "Needs: <label>" affordance; this route collects the value. Secrets are
    REFUSED here (they belong to the credential PUT, host-bound, and their
    values must never ride a plaintext spec). Writes through ``update_spec``
    (origin=user), so the A3 rule applies — a param that shapes the effective
    URL strips ``_c2_ok``/``contract`` and the card re-commissions honestly.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    params = item["spec"].get("params") or {}
    decl = params.get(body.name) if isinstance(params, dict) else None
    if not isinstance(decl, dict):
        raise HTTPException(status_code=404,
                            detail=f"param {body.name!r} not declared on this item")
    if decl.get("kind") == "secret":
        raise HTTPException(
            status_code=409,
            detail="secret params are filled via the credential PUT, never here")
    new_spec = json.loads(json.dumps(item["spec"]))
    new_spec["params"][body.name]["value"] = body.value
    store.update_spec(item_id, new_spec, origin="user")
    request.app.state.audit.append(
        "user", "ni_param", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id, "name": body.name}),
        result_summary=tools.summarize({"filled": True}),
    )
    _journal_best_effort(store, item_id, "param_changed",
                          f"param {_param_label(item, body.name)!r} filled")
    refreshed = store.get_item(item_id)
    # Mirror of the credential PUT's H1 clean-up: an ``awaiting_params`` flow
    # slot drops the moment the last referenced slot is filled.
    _clear_flow_when_credentials_satisfied(store, refreshed, None)
    return {"ok": True, "needs_params": _needs_params(refreshed)}


def _clear_flow_when_credentials_satisfied(store: ni.NIStore, item: dict,
                                            secrets_store) -> None:
    """H1 (audit 2026-09-13): drop the ``flow`` snapshot slot when
    ``awaiting_credential`` no longer applies (every secret param has a value
    in the SecretStore). Idempotent; a missing flow slot or store-read failure
    logs at debug and returns.
    """
    assert store is not None and isinstance(item, dict), "args required"
    record = ni_flow._flow_read(store, item["id"])
    state = str((record or {}).get("state") or "")
    if record is None or state not in ("awaiting_credential", "awaiting_params"):
        return
    if state == "awaiting_credential" and _needs_credentials(item, secrets_store):
        return  # some secret is still unfilled — keep the flow record
    if state == "awaiting_params" and _needs_params(item):
        return  # some slot is still unfilled — keep the flow record
    ni_flow.clear_flow_slot(store, item["id"])


def _param_label(item: dict, name: str) -> str:
    """Return the user-visible label for a spec param, falling back to its name."""
    assert isinstance(item, dict) and isinstance(name, str), "item + name required"
    params = (item["spec"].get("params") or {}) if isinstance(item.get("spec"), dict) else {}
    entry = params.get(name) if isinstance(params, dict) else None
    label = entry.get("label") if isinstance(entry, dict) else None
    return label if isinstance(label, str) and label else name


# --- Phase 4b D2b: repair-policy setter (desktop-local) ---------------------

class RepairPolicyIn(BaseModel):
    """Partial repair-policy update: either flag may be absent (unchanged).

    Closed shape mirrors ``ni._validate_repair_policy``. The route is desktop-local:
    an L2 opt-in is a consent-bearing switch (§23) and must never ride the bridged
    phone response surface. The web UI's toggle lands here; the tool chokepoint's
    ``update_ni_item`` handler is the agent-side equivalent (approval card = consent).
    """

    l1: bool | None = None
    l2_frontier: bool | None = None


@router.post("/api/ni/items/{item_id}/repair-policy")
def set_repair_policy(request: Request, item_id: str, body: RepairPolicyIn) -> dict:
    """Set `repair_policy` on the item (desktop-local; audited).

    Metadata (the flags landed) is what rides the audit row; the sealed spec is the
    truth. Uses ``update_spec`` so the update strips ``_c2_ok`` / ``contract`` (the
    A3 rule) — a policy flip is a plain user edit and inherits the same posture.
    Refuses when the item is missing (404); pydantic validates the shape.
    """
    _require_desktop_local(request)
    store = _store(request)
    current = store.get_item(item_id)
    if current is None:
        raise HTTPException(status_code=404, detail="item not found")
    existing = dict(current["spec"].get("repair_policy") or {"l1": True,
                                                              "l2_frontier": False})
    if body.l1 is not None:
        existing["l1"] = bool(body.l1)
    if body.l2_frontier is not None:
        existing["l2_frontier"] = bool(body.l2_frontier)
    new_spec = dict(current["spec"])
    new_spec["repair_policy"] = {"l1": bool(existing.get("l1", True)),
                                  "l2_frontier": bool(existing.get("l2_frontier", False))}
    try:
        # L2 (audit 2026-09-12): repair_policy is a run-time behavior flag the
        # contract never fingerprinted — preserve the attestations + streak so a
        # user's policy flip can't kick the item out of C3 into a contract
        # dead-end (which would leave L1 permanently ineligible).
        store.update_spec(item_id, new_spec, origin="user",
                          preserve_attestations=True)
    except (ValueError, ni.NIError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    request.app.state.audit.append(
        "user", "ni_repair_policy_set", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id,
                                       "repair_policy": new_spec["repair_policy"]}),
        result_summary=tools.summarize({"repair_policy": new_spec["repair_policy"]}),
    )
    return {"ok": True, "repair_policy": new_spec["repair_policy"]}


# --- §23 L2 frontier proposal apply/dismiss ---------------------------------

@router.post("/api/ni/items/{item_id}/l2-proposal/apply")
def apply_l2_proposal(request: Request, item_id: str) -> dict:
    """Apply a parked §23 L2 proposal via the §14 trial machinery.

    409 unless a ``_l2_proposal`` is present on the sealed spec. Uses
    ``apply_repair(origin='repair_l2', expected_rev=...)`` so a concurrent user
    edit races the apply cleanly (returns None → 409). The proposal is stripped
    FIRST (rev-preserving) so the pre-repair revision snapshot the trial revert
    would restore does NOT re-instate the consumed proposal.
    """
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    # Phase 4b D4 (audit 2026-09-11): applying to a broken item would stamp
    # ``_l1_trial`` on a spec whose engine path is stopped (``due_items`` excludes
    # broken), so the trial's success/failure ballot never scores — the item is
    # wedged and a later revert has no clean state to restore. A broken item's fix
    # path is edit → re-commission; the parked proposal is stale context by then
    # (see ni._transition_on_failure, which clears the proposal on the broken hop).
    if item["state"] == "broken":
        raise HTTPException(status_code=409,
                            detail="item is broken — re-commission it first")
    proposal = item["spec"].get("_l2_proposal")
    if not isinstance(proposal, dict) or not isinstance(proposal.get("stages"), dict):
        raise HTTPException(status_code=409, detail="no L2 proposal to apply")
    stages = proposal["stages"]
    try:
        new_spec = ni._spec_with_repaired_stages(item["spec"], stages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # ``_spec_with_repaired_stages`` pops ``_l2_proposal`` alongside the _l1_*
    # markers, so apply_repair's seal-write REPLACES the sealed spec with a
    # proposal-free body atomically. On a spec_changed abort the sealed state
    # is UNTOUCHED (proposal still parked), so the user can retry — dropping
    # the proposal pre-apply would silently consume it on a race.
    expected_rev = int(item["spec_rev"])
    try:
        applied_rev = store.apply_repair(item_id, new_spec, origin="repair_l2",
                                          expected_rev=expected_rev)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if applied_rev is None:
        raise HTTPException(status_code=409,
                            detail="spec changed since proposal — retry")
    store.record_run(item_id, "repair_l2_applied", duration_ms=0,
                     error=None, contract_ok=None)
    _journal_best_effort(store, item_id, "repaired",
                         "user applied L2 proposal (repair_l2 trial armed)")
    request.app.state.audit.append(
        "user", "ni_l2_proposal_apply", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id}),
        result_summary=tools.summarize({"spec_rev": applied_rev}),
    )
    return {"ok": True, "spec_rev": applied_rev}


@router.post("/api/ni/items/{item_id}/l2-proposal/dismiss")
def dismiss_l2_proposal(request: Request, item_id: str) -> dict:
    """Dismiss a parked §23 L2 proposal — clears ``_l2_proposal`` (audited).

    No re-propose this streak: ``_l2_last_attempt`` was stamped at proposal
    time and stays past the dismiss, so the one-attempt-per-streak gate blocks
    further tries until the streak resets (success/update/commission).
    """
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if not isinstance(item["spec"].get("_l2_proposal"), dict):
        raise HTTPException(status_code=409, detail="no L2 proposal to dismiss")
    store.clear_l2_proposal(item_id)
    request.app.state.audit.append(
        "user", "ni_l2_proposal_dismiss", "reviewed", "executed", True,
        args_summary=tools.summarize({"item_id": item_id}),
        result_summary=tools.summarize({"dismissed": True}),
    )
    return {"ok": True}


# --- Global library (§19/§20) -------------------------------------------------------

class LibraryConnectIn(BaseModel):
    """Connect to a template library by URL. First fetch pins the publisher key (TOFU)."""

    url: str = Field(min_length=1, max_length=2048)


class LibraryInstallIn(BaseModel):
    """Install one library template. Values are string/number ONLY — secrets travel the
    credential path (host-bound), never the chat/install body."""

    template_id: str = Field(min_length=1, max_length=ni_library.MAX_TEMPLATE_ID)
    params: dict[str, str | int | float] = Field(default_factory=dict)


def _library(store: ni.NIStore) -> ni_library.LibraryStore:
    """Wrap the unlocked NIStore in a LibraryStore. Never creates a source row."""
    assert store is not None, "store required"
    return ni_library.LibraryStore(store)


def _explain_library_refusal(exc: Exception) -> str:
    """Map library errors to a user-facing sentence — never echo raw exception text
    for netguard/library errors that could leak the URL path."""
    if isinstance(exc, netguard.FetchError):
        return "couldn't reach the library host — check the URL and try again"
    return str(exc)


def _template_sources_for_display(spec_template: dict) -> list[dict]:
    """Derive the install sheet's UNMISSABLE sources line (§20): for http sources the
    host + path (the template string, params visible as ``{{param:x}}``), else the
    type alone. Display-only — consent still happens at Activate."""
    assert isinstance(spec_template, dict), "spec_template must be a dict"
    source = spec_template.get("source") or {}
    stype = source.get("type") or "?"
    if stype in ("http_json", "http_page"):
        parsed = urlparse(str(source.get("url") or ""))
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return [{"type": stype, "host": parsed.hostname or "", "path": path}]
    return [{"type": stype, "host": "", "path": ""}]


def _template_row(t: dict) -> dict:
    """One template as the Library sheet renders it: metadata + derived sources +
    param descriptors (name/label/kind — never values) + the bound preview scene.

    M1 (audit 2026-09-09): ``preview_payload`` is now the BOUND scene (what
    ``bind_scene`` would produce), not the raw dummy data — an installed item's
    preview slot IS the bound scene, and the sheet's preview must match. parse_pack
    proved the payload binds against the scene, so a runtime bind failure here is a
    data-corruption bug (return an empty scene rather than crash the whole sheet)."""
    assert isinstance(t, dict), "template must be a dict"
    spec_template = t.get("spec_template") or {}
    params = [{"name": name, "label": (p or {}).get("label") or name,
               "kind": (p or {}).get("kind") or "string"}
              for name, p in (spec_template.get("params") or {}).items()]
    raw_preview = t.get("preview_payload") or {}
    scene = spec_template.get("scene") or {}
    # Phase 4c audit 2026-09-11: a template scene with an image node needs a
    # preview-style image_ref or bind_scene raises image_missing (finding #1). No
    # item yet — pass the template's own id as the item_id placeholder.
    image_ref = ni._preview_image_ref(spec_template, str(t.get("id") or "template"))
    try:
        bound_preview = ni.bind_scene(scene, raw_preview,
                                      history=ni._seed_history(spec_template),
                                      image_ref=image_ref)
    except (ni.NIError, ValueError) as exc:  # defence-in-depth — never crash the listing
        log.warning("ni library: template %r preview bind failed: %s",
                    t.get("id"), exc)
        bound_preview = {"type": "stack", "dir": "v", "gap": "sm", "children": []}
    return {"id": t["id"], "title": t["title"], "goal": t["goal"],
            "category": t["category"], "tags": t.get("tags") or [],
            "notes": t.get("notes", ""),
            "sources": _template_sources_for_display(spec_template),
            "params": params,
            "preview_payload": bound_preview}


def _library_state(store: ni.NIStore) -> dict:
    """The Library sheet's one response shape — GET / connect / check all return it."""
    library = _library(store)
    pin = library.source()
    if pin is None:
        return {"connected": False}
    blocked = pin.get("blocked")
    return {
        "connected": True,
        "url": pin["url"],
        "pack_id": pin["pack_id"],
        "seq": pin["seq"],
        "added_at": pin["added_at"],
        "last_checked": pin.get("last_checked"),
        "last_error": pin.get("last_error"),
        "blocked": ({"offered_fingerprint":
                     vault_format.fingerprint(blocked["offered_pubkey"])}
                    if blocked else None),
        "unreachable": bool(pin.get("unreachable")),
        "fingerprint": vault_format.fingerprint(pin["publisher_pubkey"]),
        "templates": [_template_row(t) for t in library.templates()],  # ≤ MAX_TEMPLATES
    }


@router.get("/api/ni/library")
def library_status(request: Request) -> dict:
    """The current library source pin + fingerprint + templates (install-sheet shape).

    Absent library ⇒ ``{connected: false}``.
    """
    return _library_state(_store(request))


@router.post("/api/ni/library/connect")
def library_connect(request: Request, body: LibraryConnectIn) -> dict:
    """First contact: fetch + verify + pin the publisher key (TOFU). One source in v1.

    Desktop-local (feeds law: adding a recurring-fetch source is an explicit local act).
    Returns the full library state so the sheet renders in one round trip.
    """
    _require_desktop_local(request)
    store = _store(request)
    library = _library(store)
    try:
        pin = library.connect(body.url.strip())
    except ni_library.LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except netguard.FetchError as exc:
        raise HTTPException(status_code=400, detail=_explain_library_refusal(exc)) from None
    request.app.state.audit.append(
        "user", "ni_library_connect", "reviewed", "executed", True,
        args_summary=tools.summarize({"host": urlparse(pin["url"]).hostname or "",
                                       "pack_id": pin["pack_id"]}),
        result_summary=tools.summarize({"seq": pin["seq"]}),
    )
    return _library_state(store)


@router.post("/api/ni/library/check")
def library_check(request: Request) -> dict:
    """Check the pinned URL and, when a newer pack verifies, apply it in the same call.

    Applying only refreshes the STORED pack (flips template_update flags on cards);
    per-item consent is untouched — §20's safety lives at apply-template-update.
    KeyChanged blocks the source (rendered by the sheet's blocked panel) rather than
    erroring; fetch/verify failures record a host-free status. Always returns the
    library state so "Check now" is idempotent and always renderable.
    """
    store = _store(request)
    library = _library(store)
    try:
        verdict = library.check_update()
        if verdict.get("behind"):
            library.apply_update(verdict)
    except ni_library.KeyChanged as exc:
        library.block(exc.offered_pubkey)
    except ni_library.RollbackError as exc:
        # M3 (audit 2026-09-09): rollback is NOT unreachable — the host answered with
        # a validly-signed older pack. Persist a truthful status ("host is serving an
        # older pack (vN < pinned vM)") and stamp last_checked so the sheet stops the
        # 30s refetch loop, WITHOUT counting toward the unreachable-host escalation.
        library.record_rollback(exc.remote_seq, exc.pinned_seq)
    except ni_library.LibraryError as exc:
        library.record_failure(exc)
    except netguard.FetchError as exc:
        library.record_failure(exc)
    return _library_state(store)


class TrustKeyIn(BaseModel):
    """Re-auth + the exact fingerprint being blessed (vault trust-publisher law: a
    rotation AFTER the user confirmed must be refused, never re-pinned blind)."""

    offered_fingerprint: str = Field(min_length=1, max_length=32)
    passphrase: str | None = None
    recovery_key: str | None = None


@router.post("/api/ni/library/trust-key")
def library_trust_key(request: Request, body: TrustKeyIn) -> dict:
    """Re-pin the library to the changed publisher key the user confirmed out-of-band.

    Gates exactly like the vault version: Desktop-local + passphrase re-entry + the
    body must name the exact offered fingerprint. Seq floor survives the re-pin.
    """
    _require_desktop_local(request)
    _reauthorize(request, body)
    store = _store(request)
    library = _library(store)
    pin = library.source()
    if pin is None:
        raise HTTPException(status_code=409, detail="no library is connected")
    offered = (pin.get("blocked") or {}).get("offered_pubkey")
    if not offered:
        raise HTTPException(status_code=409, detail=(
            "there is no pending key change — check for updates first"))
    if vault_format.fingerprint(offered) != body.offered_fingerprint.strip():
        raise HTTPException(status_code=409, detail=(
            "the offered key changed since you confirmed it — check again and verify "
            "the new fingerprint with the publisher"))
    library.trust(offered)
    request.app.state.audit.append(
        "user", "ni_library_trust_key", "reviewed", "executed", True,
        args_summary=tools.summarize({"publisher": vault_format.fingerprint(offered)}),
        result_summary=tools.summarize({"repinned": True}),
    )
    return _library_state(store)


@router.delete("/api/ni/library")
def library_disconnect(request: Request) -> dict:
    """Remove the library source pin + stored pack. Installed items are UNTOUCHED.
    Desktop-local, mirroring connect.

    H3 (audit 2026-09-09): the frontend sends DELETE /api/ni/library (`api.ts`
    ``niLibraryDisconnect``). Keeping DELETE + the resourceful path — not
    ``POST /disconnect`` — matches the client contract; installed items survive
    because ``LibraryStore.disconnect`` only drops the two reserved snapshot rows.
    """
    _require_desktop_local(request)
    store = _store(request)
    library = _library(store)
    if library.source() is None:
        raise HTTPException(status_code=409, detail="no library is connected")
    library.disconnect()
    return {"ok": True}


@router.post("/api/ni/library/install")
def library_install(request: Request, body: LibraryInstallIn) -> dict:
    """Install one template → create an item in ``draft`` with sealed provenance.

    Draft state gates on the existing Activate/commission flow (§20): installing NEVER
    auto-runs anything, and secret params must be entered via PUT /credential AFTER
    creation (this body accepts string/number values ONLY).
    """
    store = _store(request)
    library = _library(store)
    template = library.get_template(body.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found in the connected library")
    pin = library.source()
    assert pin is not None, "template must belong to a connected library"
    try:
        spec = ni_library.build_installed_spec(template, body.params)
    except ni_library.LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    spec["_template"] = ni_library.provenance_for(
        pin["pack_id"], template["id"], int(pin["seq"]), template["spec_template"])
    # H2 (audit 2026-09-09): pre-mint the item id so we can rewrite ``ni:self:<name>``
    # placeholders (header $secret refs + secret param values) to ``ni:<item_id>:<name>``
    # BEFORE the spec is sealed. Without this rewrite, ``_load_credential`` refuses the
    # header ref (prefix mismatch) and every fetch dies with ``secret_not_scoped`` — the
    # flagship authenticated-template path could not run at all. add_item accepts the
    # pre-minted id so we get a single write, no re-seal race under _SPEC_LOCK.
    item_id = str(uuid.uuid4())
    ni_library.rewrite_self_refs(spec, item_id)
    # Phase 4c audit 2026-09-11 (finding #4): the install route must run the §25
    # composite-depth guard BEFORE add_item — otherwise a pack that ships an
    # internal.ni template referencing another internal.ni item lands as a live
    # dangling reference and the runtime guard fires per tick. Dangling references
    # (target not present) still install fine — the guard skips them.
    try:
        ni.check_composite_depth(store, spec)
    except ni.NIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    try:
        store.add_item(spec, template.get("preview_payload") or {},
                       origin="template", item_id=item_id)
    except (ValueError, ni.NIError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    request.app.state.audit.append(
        "user", "ni_library_install", "reviewed", "executed", True,
        args_summary=tools.summarize({"template_id": template["id"],
                                       "pack_id": pin["pack_id"]}),
        result_summary=tools.summarize({"item_id": item_id, "state": "draft"}),
    )
    needs = [name for name, p in (spec.get("params") or {}).items()
             if (p or {}).get("kind") == "secret"]
    return {"ok": True, "item_id": item_id, "state": "draft",
            "needs_credentials": needs}


@router.post("/api/ni/items/{item_id}/apply-template-update")
def apply_template_update(request: Request, item_id: str) -> dict:
    """Rebuild the item from the CURRENT stored template (§20 fleet healing).

    Carries over param VALUES + credentials (same names — secrets stay in the SecretStore,
    unchanged); new params start empty. Strips ``_c2_ok`` / ``contract`` / ``_l1_*`` /
    trial per §20 (via update_spec's existing rules), resets to ``draft`` (the user's
    explicit Activate is the consent event, NEVER silent), bumps revision with origin
    ``template``, and updates ``_template`` to the new seq + hash.
    """
    store = _store(request)
    library = _library(store)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    provenance = item["spec"].get("_template")
    if not isinstance(provenance, dict):
        raise HTTPException(status_code=400,
                            detail="this item was not installed from the library")
    pin = library.source()
    if pin is None or pin.get("pack_id") != provenance.get("pack_id"):
        raise HTTPException(status_code=409,
                            detail="the connected library no longer holds this item's pack")
    template = library.get_template(provenance.get("template_id") or "")
    if template is None:
        raise HTTPException(status_code=410,
                            detail="the template was removed from the current pack")
    new_spec = _rebuild_from_template(item["spec"], template)
    new_spec["_template"] = ni_library.provenance_for(
        pin["pack_id"], template["id"], int(pin["seq"]), template["spec_template"])
    # H2 (audit 2026-09-09): the incoming template still carries ``ni:self:<name>`` in
    # any $secret headers and secret param values. Rewrite them to this item's id so the
    # fetch path resolves under the existing SecretStore keys (which never moved).
    ni_library.rewrite_self_refs(new_spec, item_id)
    # LOW#3 (audit 2026-09-09): set_state('draft') BEFORE update_spec closes a
    # millisecond consent race — a tick landing between the two writes would see the
    # NEW spec under the OLD 'live' state and fire the un-consented template. The
    # tick's due gate skips draft, so ordering "draft → new spec" is safe either way.
    store.set_state(item_id, "draft")
    try:
        store.update_spec(item_id, new_spec, origin="template")
    except (ValueError, ni.NIError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # LOW#3 rebind: refresh the sealed preview + preview_data snapshots from the NEW
    # template so the card renders the new bound preview immediately (parse_pack proved
    # this binds — a raise here is a data-corruption bug, not user input).
    preview_payload = template.get("preview_payload") or {}
    # Phase 4c audit 2026-09-11: an image-node template rebind needs a preview
    # image_ref (finding #1). The item_id is already known here.
    image_ref = ni._preview_image_ref(new_spec, item_id)
    try:
        bound = ni.bind_scene(new_spec["scene"], preview_payload,
                              history=ni._seed_history(new_spec),
                              image_ref=image_ref)
    except (ni.NIError, ValueError) as exc:
        raise HTTPException(status_code=500,
                            detail=f"template preview bind failed: {exc}") from None
    store.write_snapshot(item_id, "preview", bound, ok=True)
    store.write_snapshot(item_id, "preview_data", preview_payload, ok=True)
    _journal_best_effort(store, item_id, "apply_template_update",
                         f"applied template update to seq {int(pin['seq'])}")
    return {"ok": True, "state": "draft",
            "spec_hash": new_spec["_template"]["spec_hash"]}


def _rebuild_from_template(current_spec: dict, template: dict) -> dict:
    """Build the new spec from the template, carrying param values over by NAME.

    Same-name params: the user's value (string/number) rides across; secret values are
    left as the ni:self placeholder — the SecretStore key was written under the item id
    and is not touched by a template update (§20 credential-carryover guarantee).

    M2 (audit 2026-09-09): a carried value whose name is ABSENT from the new template
    (renamed / removed) or whose new kind is ``secret`` (re-kinded) is silently DROPPED
    here rather than passed through to ``build_installed_spec`` — that helper refuses
    unknown params AND secret-in-body, both of which would 400 the whole apply. Fleet
    healing must not fail the moment a publisher renames or promotes a param to secret.
    """
    assert isinstance(current_spec, dict) and isinstance(template, dict), "args required"
    carried_values: dict = {}
    current_params = current_spec.get("params") or {}
    for name, decl in current_params.items():  # bounded by ni._MAX_PARAMS
        if not isinstance(decl, dict) or decl.get("kind") == "secret":
            continue
        value = decl.get("value")
        # Only carry over VALUES the user actually filled (empty stays empty for the
        # new template's slot); reject bool since ``isinstance(True, int)`` is True.
        if (isinstance(value, (str, int, float)) and not isinstance(value, bool)
                and value != ""):
            carried_values[name] = value
    new_params = (template.get("spec_template") or {}).get("params") or {}
    filtered: dict = {}
    for name, value in carried_values.items():  # bounded by ni._MAX_PARAMS
        decl = new_params.get(name)
        if not isinstance(decl, dict):
            continue  # param removed / renamed in the new template
        if decl.get("kind") == "secret":
            continue  # promoted to secret — value must live in SecretStore, not spec
        filtered[name] = value
    return ni_library.build_installed_spec(template, filtered)


# --- export-as-template (§21) --------------------------------------------------------

@router.get("/api/ni/items/{item_id}/export-template")
def export_template(request: Request, item_id: str) -> dict:
    """Return the §19 template JSON for this item — sanitized per §21.

    Desktop-local: the sanitizer runs against secret VALUES loaded from the item's
    SecretStore rows to catch a credential accidentally pasted into a header literal
    or URL query — never a value that could ride a bridged phone response.
    """
    _require_desktop_local(request)
    store = _store(request)
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    secrets = _secret_store(request)
    try:
        template = _build_export_template(store, item, secrets)
    except ni_library.LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return template


def _build_export_template(store: ni.NIStore, item: dict, secrets_store) -> dict:
    """Run the §21 sanitizer + wrap as a §19 template object; validate round-trip via
    parse_pack's template validator so a broken export is a clean 400."""
    assert store is not None, "store required"
    assert isinstance(item, dict), "item must be a dict"
    assert secrets_store is not None, "secrets_store required"
    spec = _sanitize_spec_for_export(item, secrets_store)
    preview = _neutralized_preview(store, item)
    template = {
        "id": _slugify(item["spec"].get("title") or item["id"]),
        "title": (item["spec"].get("title") or "Untitled")[:ni_library.MAX_TITLE],
        "goal": (item["spec"].get("goal") or "")[:ni_library.MAX_GOAL] or "exported item",
        "category": "exported",
        "tags": [],
        "spec_template": spec,
        "preview_payload": preview,
        "notes": "exported from an installed item — review before publishing",
    }
    # Round-trip through the same per-template validator parse_pack runs (P5: the
    # exporter must produce something parse_pack would accept — otherwise the registry
    # PR catches this only after the operator sends the file).
    try:
        ni_library._validate_one_template(template, 0, set())
    except ni_library.LibraryError as exc:
        raise ni_library.LibraryError(f"exported template does not round-trip: {exc}") from None
    return template


# Phase 4b D2c (audit 2026-09-11): export strips `_l2_*` state (proposal + attempt
# marker) AND `repair_policy` — repair policy is always the installer's local choice,
# so a template ships with none and the install path forces the safe default.
_EXPORT_STRIP_KEYS = ("contract", "_c2_ok", "_l1_last_attempt", "_l1_trial",
                      "_l2_last_attempt", "_l2_proposal", "_template", "repair_policy",
                      "_born")


def _sanitize_spec_for_export(item: dict, secrets_store) -> dict:
    """§21 sanitizer: strip system fields, empty user values, refuse credential-in-header,
    refuse internal.schedule + internal.kb sources, rewrite ``ni:<id>:`` refs back to
    ``ni:self:`` (H2 export inverse).
    """
    assert isinstance(item, dict), "item must be a dict"
    spec = ni_library._deep_copy_json(item["spec"])
    for key in _EXPORT_STRIP_KEYS:
        spec.pop(key, None)
    source = spec.get("source") or {}
    stype = source.get("type")
    if stype == "internal.schedule":
        raise ni_library.LibraryError(
            "internal.schedule items reference a machine-local schedule id and can't be "
            "exported as a template")
    # M6 (audit 2026-09-09): internal.kb.query is personal search text — recreate as a
    # template manually. Refusing here mirrors internal.schedule's stance.
    if stype == "internal.kb":
        raise ni_library.LibraryError(
            "internal.kb items contain your personal search text — recreate as a "
            "template manually")
    _empty_param_values(spec)
    # H2 (audit 2026-09-09) export inverse: rewrite this item's concrete
    # ``ni:<item_id>:<name>`` refs back to ``ni:self:<name>`` so (a) the exported
    # template installs cleanly for the next subscriber and (b) the item UUID never
    # appears inside the emitted JSON (subscribers won't accept it — but we won't leak
    # it either).
    ni_library.rewrite_refs_to_self(spec, item["id"])
    _refuse_credential_leaks(item["id"], spec, secrets_store)
    return spec


def _empty_param_values(spec: dict) -> None:
    """Zero out param VALUES per §21 (labels + kinds kept). Secrets become the ni:self
    placeholder so subscribers know they must enter a credential."""
    assert isinstance(spec, dict), "spec must be a dict"
    params = spec.get("params") or {}
    for name, decl in params.items():  # bounded by ni._MAX_PARAMS
        if not isinstance(decl, dict):
            continue
        if decl.get("kind") == "secret":
            decl["value"] = f"{ni._NI_SELF_PLACEHOLDER}{name}"
        else:
            decl["value"] = ""


def _refuse_credential_leaks(item_id: str, spec: dict, secrets_store) -> None:
    """Refuse export when THIS item's stored credential VALUE appears anywhere the
    exporter would carry it into a published template. Belt-and-braces over the
    primary rule (§21 doc note): the real protection is $secret discipline — a
    credential rides ``{"$secret": "ni:..."}`` refs, never a plain literal.

    M5 (audit 2026-09-09) widens the search:
      * whole URL (path + query + fragment) — not just the query — AND a percent-decoded
        copy of it, so a credential pasted as ``/tokens/xyz/refresh`` or ``?t=xy%2Dz``
        is caught;
      * every ``llm``-stage instruction string — an operator who pastes a credential
        into an LLM instruction shouldn't ship it as a template either.
    Base64 encoding is out of scope (documented in ni-format.md §21).
    """
    assert isinstance(item_id, str) and item_id, "item id required"
    assert isinstance(spec, dict), "spec must be a dict"
    values = _load_item_credential_values(item_id, spec, secrets_store)
    if not values:
        return
    source = spec.get("source") or {}
    url = source.get("url") if isinstance(source, dict) else None
    if isinstance(url, str) and url:
        decoded = unquote(url)
        for cred in values:  # bounded by ni._MAX_PARAMS
            if not cred:
                continue
            if cred in url or cred in decoded:
                raise ni_library.LibraryError(
                    "refusing to export: a stored credential value appears in the URL")
    headers = source.get("headers") if isinstance(source, dict) else None
    if isinstance(headers, dict):
        for name, value in headers.items():
            if not isinstance(value, str):
                continue
            for cred in values:
                if cred and cred in value:
                    raise ni_library.LibraryError(
                        f"refusing to export: a stored credential value appears in "
                        f"header {name!r}")
    for stage in (spec.get("pipeline") or []):  # bounded by ni._MAX_PIPELINE_STAGES
        if not isinstance(stage, dict) or stage.get("op") != "llm":
            continue
        instruction = stage.get("instruction")
        if not isinstance(instruction, str) or not instruction:
            continue
        for cred in values:
            if cred and cred in instruction:
                raise ni_library.LibraryError(
                    "refusing to export: a stored credential value appears in an "
                    "llm-stage instruction")


def _load_item_credential_values(item_id: str, spec: dict, secrets_store) -> list[str]:
    """Best-effort load of every secret value written under this item's namespace.

    Errors from the SecretStore fall through as an empty list — the sanitizer's URL /
    header substring guard is defence-in-depth over the primary rule (headers with a
    credential ride $secret refs, never plain literals), so an unreadable secret must
    NEVER block a legitimate export.
    """
    assert isinstance(item_id, str) and item_id, "item id required"
    assert isinstance(spec, dict), "spec must be a dict"
    out: list[str] = []
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        return out
    for name, decl in params.items():  # bounded by ni._MAX_PARAMS
        if not isinstance(decl, dict) or decl.get("kind") != "secret":
            continue
        try:
            raw = secrets_store.get(f"ni:{item_id}:{name}")
        except Exception:  # a secret we can't read is one we can't leak — skip it
            continue
        if not raw:
            continue
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            continue
        value = body.get("value") if isinstance(body, dict) else None
        if isinstance(value, str) and value:
            out.append(value)
    return out


def _neutralized_preview(store: ni.NIStore, item: dict) -> dict:
    """Return a preview payload that binds against the item's scene without leaking data.

    Reads the sealed ``preview_data`` slot — the RAW payload that fed ``bind_scene``
    at add_item time (dummy data, item-owner-authored, never a live fetch).

    LOW#7 (audit 2026-09-09): items created BEFORE the ``preview_data`` slot existed
    (pre-Phase-3 add_item) have no snapshot to read; the empty-dict fallback made the
    round-trip validator refuse with a generic "preview_payload does not bind" message
    that didn't name the cause. Raise a LibraryError with the actual reason instead
    (still a refusal — the export contract is unchanged — just an honest sentence).
    """
    assert store is not None and isinstance(item, dict), "store + item required"
    snap = store.read_snapshot(item["id"], "preview_data")
    if snap is not None and isinstance(snap.get("payload"), dict):
        return dict(snap["payload"])
    raise ni_library.LibraryError(
        "this item was created before previews were stored per-item and can't be "
        "exported as a template — recreate it in the current app first")


def _slugify(text: str) -> str:
    """Best-effort slug for the exported template's id — bounded and grammar-safe.

    Kept simple: lowercase, non-alnum → '-', truncated to ni_library.MAX_TEMPLATE_ID.
    A caller-supplied title of "Weather (KSFO)" becomes "weather--ksfo-" which passes
    ni._KEY_RE (starts with a letter, ASCII alphanumerics + hyphens/underscores).
    """
    assert isinstance(text, str), "text must be a string"
    assert text, "text must be non-empty"
    out = []
    for ch in text.lower()[:ni_library.MAX_TEMPLATE_ID]:  # bounded by MAX_TEMPLATE_ID
        out.append(ch if ch.isalnum() or ch in "-_" else "-")
    slug = "".join(out).strip("-") or "exported-item"
    if not slug[0].isalpha() and slug[0] != "_":
        slug = "x" + slug
    return slug[:ni_library.MAX_TEMPLATE_ID]


# --- MCP server registry (§22 outbound-MCP source) --------------------------

class McpServerIn(BaseModel):
    """Create / update body for one MCP server config (§22).

    The transport-specific fields are validated in ``ni_mcp._validate_new_config``;
    Pydantic here bounds the raw shape only (labels + short strings) so the request
    is rejected fast before it lands in the registry.
    """

    label: str = Field(min_length=1, max_length=ni_mcp.MAX_LABEL)
    transport: str = Field(min_length=1, max_length=10)
    enabled: bool = True
    command: str | None = Field(default=None, max_length=ni_mcp.MAX_COMMAND)
    args: list[str] | None = Field(default=None, max_length=ni_mcp.MAX_ARGS)
    url: str | None = Field(default=None, max_length=ni_mcp.MAX_URL)


def _registry(store: ni.NIStore) -> ni_mcp.ServerRegistry:
    """Wrap the unlocked NIStore in a ServerRegistry."""
    assert store is not None, "store required"
    return ni_mcp.ServerRegistry(store)


def _to_body(model: McpServerIn) -> dict:
    """Pydantic body → dict fit for the registry validator (drops unset optionals)."""
    assert model is not None, "model required"
    body: dict = {"label": model.label, "transport": model.transport,
                  "enabled": bool(model.enabled)}
    if model.command is not None:
        body["command"] = model.command
    if model.args is not None:
        body["args"] = list(model.args)
    if model.url is not None:
        body["url"] = model.url
    return body


@router.get("/api/ni/mcp-servers")
def list_mcp_servers(request: Request) -> dict:
    """List every configured MCP server (§22). Requires unlock.

    Read-only from any unlocked surface — the payload carries the raw command/url
    so the config sheet can render it, but the payload is secrets-free by
    construction (§22 credentials live in the user's OWN server, never in the
    registry). Writes stay desktop-local.
    """
    store = _store(request)
    return {"servers": _registry(store).list_servers()}


@router.post("/api/ni/mcp-servers")
def add_mcp_server(request: Request, body: McpServerIn) -> dict:
    """Create one MCP server config. Desktop-local (§22: server config carries
    execution/connection authority)."""
    _require_desktop_local(request)
    store = _store(request)
    try:
        row = _registry(store).add(_to_body(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    request.app.state.audit.append(
        "user", "ni_mcp_server_add", "reviewed", "executed", True,
        args_summary=tools.summarize({"label": row["label"],
                                       "transport": row["transport"]}),
        result_summary=tools.summarize({"id": row["id"]}),
    )
    return row


@router.put("/api/ni/mcp-servers/{server_id}")
def update_mcp_server(request: Request, server_id: str, body: McpServerIn) -> dict:
    """Replace one MCP server config. Desktop-local."""
    _require_desktop_local(request)
    store = _store(request)
    registry = _registry(store)
    try:
        row = registry.update(server_id, _to_body(body))
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    request.app.state.audit.append(
        "user", "ni_mcp_server_update", "reviewed", "executed", True,
        args_summary=tools.summarize({"label": row["label"],
                                       "transport": row["transport"]}),
        result_summary=tools.summarize({"id": row["id"]}),
    )
    return row


@router.delete("/api/ni/mcp-servers/{server_id}")
def delete_mcp_server(request: Request, server_id: str) -> dict:
    """Drop one MCP server config. Desktop-local. Refuses when any item still
    references this server (409 with the item count so the sheet can show it)."""
    _require_desktop_local(request)
    store = _store(request)
    in_use = _count_items_using_mcp_server(store, server_id)
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"refusing: {in_use} item(s) still reference this server",
        )
    registry = _registry(store)
    try:
        registry.delete(server_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found") from None
    request.app.state.audit.append(
        "user", "ni_mcp_server_delete", "reviewed", "executed", True,
        args_summary=tools.summarize({"id": server_id}),
        result_summary=tools.summarize({"deleted": True}),
    )
    return {"ok": True}


def _count_items_using_mcp_server(store: ni.NIStore, server_id: str) -> int:
    """Scan every item's sealed spec for an ``mcp_tool`` source referencing this
    server id. Bounded by ``NIStore._MAX_ITEMS``.

    Delete-in-use is refused so a stale server config can't be dropped out from
    under a live item (§22 fetch would then raise ``mcp_unavailable`` forever until
    the source were re-consented). The count is metadata only — no source detail.
    """
    assert store is not None, "store required"
    assert isinstance(server_id, str) and server_id, "server id required"
    count = 0
    for item in store.list_items():  # bounded by NIStore._MAX_ITEMS
        source = item["spec"].get("source") or {}
        if not isinstance(source, dict):
            continue
        if source.get("type") == "mcp_tool" and source.get("server_id") == server_id:
            count += 1
    return count
