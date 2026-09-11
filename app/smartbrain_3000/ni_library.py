"""Neural Interface Global Library — Phase 3 (docs/internal/ni-format.md §19/§20/§21).

A **template** is an item spec (§2) with its slots empty; a **pack** is a signed,
versioned collection of templates. Trust machinery mirrors vault subscriptions:
Ed25519 over canonical JSON (``vault_format.canonical``), fingerprint display law
(``vault_format.fingerprint``), TOFU pin, monotonic seq, rollback refusal, KeyChanged
blocking. This module owns the pack shape + verification + the LibraryStore over
NIStore's reserved-id snapshot rows; the routes live in ``ni_routes``.

Everything read from a pack is UNTRUSTED. Every bound below is enforced, and exceeding
one is a clean refusal — never a silent truncation. The signature is never a validator
bypass (P5): ``parse_pack`` runs the full §2 spec validator (with empty-param
relaxations only) and the §5 bound-scene validator on every template.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from urllib.parse import urlparse

from . import identity, ni, vault_format

log = logging.getLogger(__name__)

# --- bounds (§19 — every one verifiable, every one refused when exceeded) -----------

MAX_PACK_BYTES = 2 * 1024 * 1024           # §19: pack ≤ 2 MB (raw bytes, pre-parse)
MAX_TEMPLATES = 200                        # §19: ≤ 200 templates per pack
MAX_TEMPLATE_ID = 80
MAX_TITLE = 300                            # matches ni._MAX_TITLE
MAX_GOAL = 5000                            # matches ni._MAX_GOAL
MAX_CATEGORY = 40
MAX_TAGS = 20
MAX_TAG = 40
MAX_NOTES = 500
MAX_PACK_ID = 100
MAX_LABEL = 100
MAX_PUBKEY_B64 = 100

# §19 signature domain — vault_format precedent: signed over the prefix + canonical(payload).
PACK_SIG_PREFIX = b"sb-ni-pack-sig:v1\n"

# Reserved snapshot id (§20): the source pin + verified pack live under one non-item id.
# No matching ``ni_items`` row exists, so board/list queries never surface it — verified by
# construction because every list query starts from ``ni_items`` (see NIStore.list_items).
LIBRARY_RESERVED_ID = "__library__"
LIBRARY_SLOT_SOURCE = "source"
LIBRARY_SLOT_PACK = "pack"

# §20 cadence: 24h default, 1h floor (vault_sync precedent). One check per tick when due.
DEFAULT_CHECK_INTERVAL_SECONDS = 86_400
MIN_CHECK_INTERVAL_SECONDS = 3_600

# Dead-host escalation (vault_sync mirror): 8 consecutive failures AND ≥7 elapsed days.
UNREACHABLE_FAILURE_COUNT = 8
UNREACHABLE_MIN_DAYS = 7


class LibraryError(Exception):
    """A pack (or source pin) is malformed, untrusted, or refused by policy."""


class RollbackError(LibraryError):
    """The host is serving a validly-signed OLDER pack than the pin — refused (§5 rule).

    Carries ``remote_seq`` + ``pinned_seq`` so the caller (route + tick) can persist a
    truthful status message ("host is serving an older pack (v{remote} < pinned v{pinned})")
    without re-parsing the exception string.
    """

    def __init__(self, remote_seq: int, pinned_seq: int) -> None:
        assert isinstance(remote_seq, int) and remote_seq >= 0, "remote_seq >= 0"
        assert isinstance(pinned_seq, int) and pinned_seq >= 0, "pinned_seq >= 0"
        super().__init__(
            f"the host is serving library pack v{remote_seq}, older than "
            f"the pinned v{pinned_seq} — refusing")
        self.remote_seq = remote_seq
        self.pinned_seq = pinned_seq


class KeyChanged(LibraryError):
    """The pack is self-consistently signed by a key that is NOT the pinned one.

    Carries the OFFERED key so the caller can block the source and show both fingerprints
    — modelled line-for-line on ``vault_sync.KeyChanged``. A pack that verifies under
    NEITHER the pinned NOR the embedded key is tampering (a plain LibraryError with the
    message "pack signature does not verify"), NOT a key change. Verifying the embedded
    key BEFORE raising KeyChanged is what stops an attacker from presenting garbage
    signed by a random pubkey as if it were a legitimate rotation (audit 2026-09-09 H1).
    """

    def __init__(self, offered_pubkey: str) -> None:
        assert isinstance(offered_pubkey, str) and offered_pubkey, "offered pubkey required"
        super().__init__("the library publisher's key changed")
        self.offered_pubkey = offered_pubkey


# --- parse (verification-independent shape guards) --------------------------------------

def parse_pack(raw: bytes) -> dict:
    """Parse + shape-validate one pack; return the payload dict (WITHOUT the sig envelope).

    Runs BEFORE signature verification because a hostile pack that never verifies must
    still refuse cleanly (never crash the caller). Order: byte cap → canonical JSON with
    duplicate-key rejection → envelope shape → payload shape → each template's spec
    (full §2 validator, empty-param mode) + preview_payload (§5 bound-scene validator) +
    duplicate id refusal.
    """
    assert isinstance(raw, (bytes, bytearray)), "pack must be bytes"
    if len(raw) > MAX_PACK_BYTES:
        raise LibraryError(f"pack is larger than {MAX_PACK_BYTES} bytes")
    if not raw:
        raise LibraryError("pack is empty")
    try:
        envelope = vault_format.parse_canonical(bytes(raw))
    except vault_format.VaultError as exc:
        raise LibraryError(f"pack JSON is not canonical: {exc}") from None
    payload = envelope.get("sb_ni_pack")
    sig = envelope.get("sig")
    if not isinstance(payload, dict) or not isinstance(sig, dict):
        raise LibraryError("pack envelope must be {sb_ni_pack: {...}, sig: {...}}")
    if set(envelope.keys()) != {"sb_ni_pack", "sig"}:
        raise LibraryError("pack envelope has unexpected top-level keys")
    _validate_sig_block(sig)
    _validate_payload_shape(payload)
    _validate_templates(payload["templates"])
    return payload


def _validate_sig_block(sig: dict) -> None:
    """Envelope ``sig``: {alg: 'ed25519', value: <b64 string>}."""
    assert isinstance(sig, dict), "sig must be a dict"
    if set(sig.keys()) != {"alg", "value"}:
        raise LibraryError("pack sig must be {alg, value}")
    if sig.get("alg") != "ed25519":
        raise LibraryError("pack sig.alg must be 'ed25519'")
    value = sig.get("value")
    if not isinstance(value, str) or not value or len(value) > 200:
        raise LibraryError("pack sig.value must be a non-empty base64 string")


def _validate_payload_shape(payload: dict) -> None:
    """Top-level pack payload shape + bounds (§19)."""
    assert isinstance(payload, dict), "payload must be a dict"
    allowed = {"version", "pack_id", "seq", "published_at", "publisher", "templates"}
    extra = set(payload.keys()) - allowed
    if extra:
        raise LibraryError(f"pack payload has unknown keys: {sorted(extra)}")
    if payload.get("version") != 1:
        raise LibraryError("pack version must be 1")
    pack_id = payload.get("pack_id")
    if not isinstance(pack_id, str) or not pack_id or len(pack_id) > MAX_PACK_ID:
        raise LibraryError("pack_id must be a non-empty string")
    seq = payload.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise LibraryError("pack seq must be a positive int")
    published_at = payload.get("published_at")
    if not (isinstance(published_at, str) and vault_format._DATE_RE.match(published_at)):
        raise LibraryError("pack published_at must be YYYY-MM-DD")
    _validate_publisher_block(payload.get("publisher"))
    templates = payload.get("templates")
    if not isinstance(templates, list):
        raise LibraryError("pack templates must be a list")
    if len(templates) > MAX_TEMPLATES:
        raise LibraryError(f"pack has more than {MAX_TEMPLATES} templates")
    if not templates:
        raise LibraryError("pack must carry at least one template")


def _validate_publisher_block(pub: object) -> None:
    """§19 publisher: {label, pubkey}. pubkey is what TOFU pins on first fetch."""
    if not isinstance(pub, dict):
        raise LibraryError("pack publisher must be an object")
    extra = set(pub.keys()) - {"label", "pubkey"}
    if extra:
        raise LibraryError(f"pack publisher has unknown keys: {sorted(extra)}")
    label = pub.get("label")
    if not isinstance(label, str) or len(label) > MAX_LABEL:
        raise LibraryError(f"pack publisher.label must be a string <= {MAX_LABEL}")
    pubkey = pub.get("pubkey")
    if not isinstance(pubkey, str) or not pubkey or len(pubkey) > MAX_PUBKEY_B64:
        raise LibraryError("pack publisher.pubkey must be a non-empty base64 string")
    try:
        raw = base64.b64decode(pubkey, validate=True)
    except Exception:
        raise LibraryError("pack publisher.pubkey is not valid base64") from None
    if len(raw) != 32:
        raise LibraryError("pack publisher.pubkey must be a 32-byte Ed25519 key")


def _validate_templates(templates: list) -> None:
    """Every template shape-checked + full-spec + preview-bound; duplicate ids refused."""
    assert isinstance(templates, list), "templates must be a list"
    seen: set[str] = set()
    for i, template in enumerate(templates):  # bounded by MAX_TEMPLATES
        _validate_one_template(template, i, seen)


def _validate_one_template(template: object, index: int, seen: set[str]) -> None:
    """Shape + spec + preview for ONE template row (§19)."""
    assert index >= 0 and isinstance(seen, set), "index + seen required"
    where = f"pack.templates[{index}]"
    if not isinstance(template, dict):
        raise LibraryError(f"{where} must be an object")
    allowed = {"id", "title", "goal", "category", "tags", "spec_template",
               "preview_payload", "notes"}
    extra = set(template.keys()) - allowed
    if extra:
        raise LibraryError(f"{where} has unknown keys: {sorted(extra)}")
    tid = template.get("id")
    if not isinstance(tid, str) or not tid or len(tid) > MAX_TEMPLATE_ID:
        raise LibraryError(f"{where}.id must be a non-empty slug")
    if not ni._KEY_RE.match(tid):
        raise LibraryError(f"{where}.id must match [A-Za-z_][A-Za-z0-9_-]*")
    if tid in seen:
        raise LibraryError(f"{where}.id {tid!r} is a duplicate in this pack")
    seen.add(tid)
    _validate_template_metadata(template, where)
    _validate_template_spec_and_preview(template, where)


def _validate_template_metadata(template: dict, where: str) -> None:
    """The purely-descriptive fields (title/goal/category/tags/notes)."""
    assert isinstance(template, dict) and where, "template + where required"
    caps = (("title", MAX_TITLE), ("goal", MAX_GOAL),
            ("category", MAX_CATEGORY), ("notes", MAX_NOTES))
    for key, cap in caps:
        value = template.get(key)
        if not isinstance(value, str) or not value:
            raise LibraryError(f"{where}.{key} must be a non-empty string")
        if len(value) > cap:
            raise LibraryError(f"{where}.{key} exceeds {cap} chars")
    tags = template.get("tags")
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise LibraryError(f"{where}.tags must be a list <= {MAX_TAGS}")
    for j, tag in enumerate(tags):  # bounded by MAX_TAGS
        if not isinstance(tag, str) or not tag or len(tag) > MAX_TAG:
            raise LibraryError(f"{where}.tags[{j}] must be a non-empty string <= {MAX_TAG}")


def _validate_template_spec_and_preview(template: dict, where: str) -> None:
    """Full §2 spec validation (empty-param mode) + §5 bound-scene validation.

    LOW#6 (audit 2026-09-09): a template pack MUST NOT ship engine/consent-owned keys —
    ``contract`` / ``_c2_ok`` / ``_l1_last_attempt`` / ``_l1_trial`` / ``_template`` are
    all engine- or install-time state. ``build_installed_spec`` already strips them at
    install (belt); refusing them at parse (suspenders) means a hostile pack can't
    ship a self-attested consent by including ``_c2_ok: true`` inside spec_template.

    Phase 4b audit 2026-09-11:
      * §23 state (`_l2_last_attempt` / `_l2_proposal`) joins the forbidden set — a
        pack could otherwise ship a pre-approved "proposal" for the installer to
        Apply blind (D3 forged-proposal defence).
      * `repair_policy` is refused wholesale (D2c) — repair policy is always the
        INSTALLER'S local choice; letting a pack carry `l2_frontier: true`
        invisibly flips a consent-bearing switch on every new install.
    """
    assert isinstance(template, dict) and where, "template + where required"
    spec = template.get("spec_template")
    if not isinstance(spec, dict):
        raise LibraryError(f"{where}.spec_template must be an object")
    forbidden = {"contract", "_c2_ok", "_l1_last_attempt", "_l1_trial", "_template",
                 "_l2_last_attempt", "_l2_proposal", "repair_policy"}
    present = sorted(forbidden.intersection(spec.keys()))
    if present:
        raise LibraryError(
            f"{where}.spec_template must not carry system-owned keys: {present}")
    try:
        validated = ni.validate_spec(spec, allow_empty_params=True)
    except ValueError as exc:
        raise LibraryError(f"{where}.spec_template invalid: {exc}") from None
    preview = template.get("preview_payload")
    if not isinstance(preview, dict):
        raise LibraryError(f"{where}.preview_payload must be an object")
    # Phase 4c audit 2026-09-11: a template whose scene contains an image node MUST
    # supply an image_ref to bind_scene or _bind_image_src raises image_missing here
    # and the whole pack fails to parse. Use the template's own id as the item_id
    # placeholder — there is no minted item yet, and _bind_image_src only formats
    # the value into the src URL (never dereferenced).
    image_ref = ni._preview_image_ref(validated, str(template.get("id") or "template"))
    try:
        ni.bind_scene(validated["scene"], preview, history=ni._seed_history(validated),
                      image_ref=image_ref)
    except (ni.NIError, ValueError) as exc:
        raise LibraryError(f"{where}.preview_payload does not bind: {exc}") from None


# --- verify (§19 order: shape → pack_id → sig against PIN → seq compare) --------------

def verify_pack(raw: bytes, pinned_key_b64: str, pinned_pack_id: str, pinned_seq: int) -> dict:
    """Verify ``raw`` against a stored pin; return a verdict dict.

    Order (§19, mirroring vault_sync §5):
      1. shape (parse_pack — same guards a first-fetch runs);
      2. ``pack_id`` matches the pin — else "different pack";
      3. signature verifies against the PINNED key over the exact served bytes — else
         KeyChanged (raised carrying the pack's own claimed key) OR plain LibraryError
         when neither key verifies;
      4. ``seq``: strictly greater = update, equal = up-to-date, lower = RollbackError.

    Return: ``{"payload": dict, "remote_seq": int, "pinned_seq": int, "behind": bool,
    "up_to_date": bool}``. Raises KeyChanged / RollbackError / LibraryError otherwise.
    """
    assert isinstance(raw, (bytes, bytearray)), "raw must be bytes"
    assert isinstance(pinned_key_b64, str) and pinned_key_b64, "pinned key required"
    assert isinstance(pinned_pack_id, str) and pinned_pack_id, "pinned pack id required"
    assert isinstance(pinned_seq, int) and pinned_seq >= 0, "pinned seq >= 0"
    payload = parse_pack(bytes(raw))  # (1)
    if payload["pack_id"] != pinned_pack_id:  # (2)
        raise LibraryError(
            "that URL now serves a DIFFERENT library pack — refusing "
            "(the source is pinned to another pack identity)"
        )
    if not _signed_by(bytes(raw), pinned_key_b64):  # (3)
        # H1 (audit 2026-09-09): the pinned key rejected the signature. Before treating
        # this as a rotation (KeyChanged), verify the PACK'S EMBEDDED key against the
        # exact served bytes. A one-byte tamper OR an attacker-authored garbage signature
        # under a random pubkey would ALSO fail here — those are tampering, NOT a
        # legitimate publisher rotation, and must land as a plain LibraryError so the
        # caller does not offer the operator a "trust this new key" affordance.
        embedded = payload["publisher"]["pubkey"]
        if not _signed_by(bytes(raw), embedded):
            raise LibraryError("pack signature does not verify")
        raise KeyChanged(embedded)
    remote_seq = payload["seq"]
    if remote_seq < pinned_seq:  # (4) — rollback
        raise RollbackError(remote_seq, pinned_seq)
    return {
        "payload": payload,
        "remote_seq": remote_seq,
        "pinned_seq": pinned_seq,
        "behind": remote_seq > pinned_seq,
        "up_to_date": remote_seq == pinned_seq,
    }


def _signed_by(raw: bytes, pubkey_b64: str) -> bool:
    """True iff ``raw`` (envelope bytes) is signed by ``pubkey_b64``.

    Mirrors ``vault_format.manifest_signed_by``: an envelope that fails to parse is
    simply "not signed by this key" — a bare False, never an exception into the caller.
    """
    assert isinstance(raw, (bytes, bytearray)), "raw must be bytes"
    assert isinstance(pubkey_b64, str) and pubkey_b64, "pubkey required"
    try:
        envelope = vault_format.parse_canonical(bytes(raw))
    except vault_format.VaultError:
        return False
    payload = envelope.get("sb_ni_pack")
    sig = envelope.get("sig") or {}
    if not isinstance(payload, dict) or not isinstance(sig.get("value"), str):
        return False
    signed = PACK_SIG_PREFIX + vault_format.canonical(payload)
    return identity.verify(pubkey_b64, signed, sig["value"])


def spec_hash(spec_template: dict) -> str:
    """Deterministic hash of a template's spec — the value pinned in ``_template`` (§19).

    Canonical JSON (same helper that signs), sha256, hex. Independent of pack_id / seq
    so ``apply-template-update`` compares only the spec CONTENT the user cares about.
    """
    assert isinstance(spec_template, dict), "spec_template must be a dict"
    return hashlib.sha256(vault_format.canonical(spec_template)).hexdigest()


def _index_templates(payload: dict) -> dict[str, dict]:
    """{template_id: template dict} — pack templates are unique by id (parse enforces it)."""
    assert isinstance(payload, dict), "payload must be a dict"
    templates = payload.get("templates") or []
    return {t["id"]: t for t in templates}  # bounded by MAX_TEMPLATES


# --- LibraryStore -----------------------------------------------------------------------

class LibraryStore:
    """The single library source in v1 (§20). Sealed via NIStore's reserved-id snapshots.

    Two rows live under ``LIBRARY_RESERVED_ID`` in ``ni_snapshots``:
      * ``source`` — the pin: {url, publisher_pubkey, pack_id, seq, added_at,
        last_checked, blocked, unreachable, consecutive_failures, first_failure_at,
        check_interval_seconds}. A URL leak reveals the user's subscription; sealed
        at rest (feed/vault subscription precedent).
      * ``pack``   — the verified pack payload (all templates, ready for install /
        board comparison / apply-template-update).

    Never a real ``ni_items`` row: the id is reserved, and every board/list query starts
    from ``ni_items``, so nothing surfaces the library through the item surface.
    """

    def __init__(self, ni_store: ni.NIStore, netguard_mod=None) -> None:
        """``netguard_mod`` is injected so tests can serve packs without opening sockets."""
        assert ni_store is not None, "NIStore required"
        self._ni = ni_store
        if netguard_mod is None:
            from . import netguard as _default
            netguard_mod = _default
        self._netguard = netguard_mod

    # --- source pin (sealed at rest) ---------------------------------------------

    def source(self) -> dict | None:
        """The current source pin, or None if no library is connected."""
        row = self._ni.read_reserved_snapshot(LIBRARY_RESERVED_ID, LIBRARY_SLOT_SOURCE)
        return None if row is None else dict(row["payload"])

    def _write_source(self, source: dict) -> None:
        """Persist the source pin (sealed under AAD ``ni_snapshot:__library__:source``)."""
        assert isinstance(source, dict), "source must be a dict"
        self._ni.write_reserved_snapshot(LIBRARY_RESERVED_ID, LIBRARY_SLOT_SOURCE, source)

    def _write_pack(self, payload: dict) -> None:
        """Persist the verified pack payload (sealed under the ``pack`` slot)."""
        assert isinstance(payload, dict), "payload must be a dict"
        self._ni.write_reserved_snapshot(LIBRARY_RESERVED_ID, LIBRARY_SLOT_PACK, payload)

    def pack(self) -> dict | None:
        """The last verified + applied pack payload, or None."""
        row = self._ni.read_reserved_snapshot(LIBRARY_RESERVED_ID, LIBRARY_SLOT_PACK)
        return None if row is None else dict(row["payload"])

    def cached_index_and_hashes(self) -> tuple[dict[str, dict], dict[str, str], str] | None:
        """LOW#1 (audit 2026-09-09): decrypt+parse the pack once per (created_at, pack_id)
        and precompute the ``spec_hash`` per template so the board's template_update flag
        stops re-hashing every template on every board poll.

        Cache is module-level and thread-safe (`_pack_cache_lock`); invalidation is
        automatic — the created_at timestamp of the sealed ``pack`` row changes on every
        connect / apply_update, so any pack rewrite kicks the cache without an explicit
        flush. Bounded to a single live pack (v1 allows one library source).
        Returns ``(index_by_template_id, spec_hash_by_template_id, pack_id)`` or None.
        """
        row = self._ni.read_reserved_snapshot(LIBRARY_RESERVED_ID, LIBRARY_SLOT_PACK)
        if row is None:
            return None
        created_at = str(row.get("created_at") or "")
        payload = row["payload"]
        cached = _pack_cache_get(created_at)
        if cached is not None:
            return cached
        index = _index_templates(payload)
        hashes = {tid: spec_hash(t.get("spec_template") or {})
                  for tid, t in index.items()}  # bounded by MAX_TEMPLATES
        result = (index, hashes, str(payload.get("pack_id") or ""))
        _pack_cache_put(created_at, result)
        return result

    # --- connect / disconnect / templates ----------------------------------------

    def connect(self, url: str, *, now: datetime | None = None) -> dict:
        """First-fetch TOFU: pull the pack, run shape-only parse, pin publisher.pubkey.

        Refuses when a library source already exists (v1 allows exactly one). Returns
        the new source pin. The signature is verified against the PACK'S OWN key here
        (self-consistency) — the pin makes future fetches strict.
        """
        assert isinstance(url, str) and url, "url required"
        if self.source() is not None:
            raise LibraryError(
                "a library source is already connected — disconnect first before adding another")
        raw = self._netguard.safe_fetch_ni_pack(url, MAX_PACK_BYTES)
        payload = parse_pack(raw)
        pubkey = payload["publisher"]["pubkey"]
        if not _signed_by(raw, pubkey):
            raise LibraryError("that pack's signature does not verify against its own key")
        stamp = (now or _now()).isoformat()
        source = {
            "url": url,
            "publisher_pubkey": pubkey,
            "pack_id": payload["pack_id"],
            "seq": payload["seq"],
            "added_at": stamp,
            "last_checked": stamp,
            "blocked": None,
            "unreachable": None,
            "consecutive_failures": 0,
            "first_failure_at": None,
            "check_interval_seconds": DEFAULT_CHECK_INTERVAL_SECONDS,
            "last_error": None,
        }
        self._write_source(source)
        self._write_pack(payload)
        return source

    def disconnect(self) -> None:
        """Remove the library source + stored pack (both sealed rows).

        Installed items are UNTOUCHED (their sealed ``_template`` provenance survives —
        a later reconnect to the same pack_id will re-light template_update on them if
        the spec_hash differs). No consent flip: consent lives per item.
        """
        self._ni.delete_reserved_snapshot(LIBRARY_RESERVED_ID, LIBRARY_SLOT_SOURCE)
        self._ni.delete_reserved_snapshot(LIBRARY_RESERVED_ID, LIBRARY_SLOT_PACK)

    def templates(self) -> list[dict]:
        """The stored pack's templates (fresh list). Empty when no library is connected."""
        payload = self.pack()
        if payload is None:
            return []
        return [dict(t) for t in (payload.get("templates") or [])]  # bounded by MAX_TEMPLATES

    def get_template(self, template_id: str) -> dict | None:
        """One template by id, or None. The template's own ``spec_template`` is returned
        untouched — the install path deep-copies + fills."""
        assert isinstance(template_id, str) and template_id, "template id required"
        payload = self.pack()
        if payload is None:
            return None
        index = _index_templates(payload)
        template = index.get(template_id)
        return None if template is None else dict(template)

    # --- update check + apply ----------------------------------------------------

    def check_update(self, *, now: datetime | None = None) -> dict:
        """Fetch + verify against the pin. Returns a verdict; raises on refusal.

        A CLEAN verdict clears the failure counter (host is up, even if only serving
        an up-to-date pack) and advances ``last_checked``. Callers map refusals to
        HTTP shape.
        """
        pin = self._required_pin()
        raw = self._netguard.safe_fetch_ni_pack(pin["url"], MAX_PACK_BYTES)
        verdict = verify_pack(raw, pin["publisher_pubkey"], pin["pack_id"], int(pin["seq"] or 0))
        stamp = (now or _now()).isoformat()
        cleared = {**pin, **_clear_failure_state(),
                   "last_checked": stamp, "last_error": None}
        self._write_source(cleared)
        return {**verdict, "raw": raw}

    def apply_update(self, verdict: dict, *, now: datetime | None = None) -> dict:
        """Apply a checked, newer pack: seal it, bump the pin's seq.

        All-or-nothing at the two-write level: pack row and source row are both
        touched here, but the caller runs inside the request/tick — a DuckDB failure
        rolls back the request naturally (auto-commit stores). ``verdict`` is what
        ``check_update`` returned (must be ``behind``).
        """
        assert isinstance(verdict, dict), "verdict must be a dict"
        assert verdict.get("behind"), "apply requires a behind verdict"
        payload = verdict["payload"]
        assert isinstance(payload, dict), "verdict payload must be a dict"
        pin = self._required_pin()
        self._write_pack(payload)
        stamp = (now or _now()).isoformat()
        updated = {**pin, **_clear_failure_state(),
                   "seq": int(payload["seq"]),
                   "last_checked": stamp, "last_error": None}
        self._write_source(updated)
        return {"applied": True, "seq": int(payload["seq"]),
                "pack_id": payload["pack_id"], "templates": len(payload.get("templates") or [])}

    def block(self, offered_pubkey: str, *, now: datetime | None = None) -> bool:
        """Record a pending key change (KeyChanged during a check). Subscription pauses.

        Returns True iff this is a fresh transition (the source was not already blocked
        by the same offered key) — LOW#5 (audit 2026-09-09) uses this to post a single
        carrier notice per block rather than one per tick.
        """
        assert isinstance(offered_pubkey, str) and offered_pubkey, "offered pubkey required"
        pin = self._required_pin()
        prior = (pin.get("blocked") or {}).get("offered_pubkey")
        transition = prior != offered_pubkey
        stamp = (now or _now()).isoformat()
        blocked = {**pin, "blocked": {"offered_pubkey": offered_pubkey},
                   "last_checked": stamp,
                   "last_error": "the publisher's key changed — trust the new key to resume"}
        self._write_source(blocked)
        return transition

    def record_rollback(self, remote_seq: int, pinned_seq: int, *,
                        now: datetime | None = None) -> bool:
        """Persist a truthful rollback status and stamp last_checked so the 30s refetch
        loop stops (M3 audit 2026-09-09). Rollback is NOT a host-unreachable event —
        the host answered validly with an older pack — so the consecutive-failure
        counter and the ``unreachable`` escalation stay UNTOUCHED.

        Returns True iff this is a fresh transition (previous tick wasn't already in
        rollback under the same remote seq) — LOW#5 uses this to post one carrier
        notice per rollback rather than one per tick.
        """
        assert isinstance(remote_seq, int) and remote_seq >= 0, "remote_seq >= 0"
        assert isinstance(pinned_seq, int) and pinned_seq >= 0, "pinned_seq >= 0"
        pin = self._required_pin()
        message = (f"host is serving an older pack (v{remote_seq} < pinned "
                   f"v{pinned_seq})")
        prior = pin.get("last_error")
        transition = prior != message
        stamp = (now or _now()).isoformat()
        updated = {**pin, "last_checked": stamp, "last_error": message}
        self._write_source(updated)
        return transition

    def trust(self, offered_pubkey: str, *, now: datetime | None = None) -> dict:
        """Re-pin the source to the NEW key the user confirmed out-of-band; clear the
        block and the failure streak (fresh start under the new identity). The seq
        floor deliberately survives — a new key is not a license to roll back
        (vault trust-publisher law). Returns the persisted pin."""
        assert isinstance(offered_pubkey, str) and offered_pubkey, "offered pubkey required"
        pin = self._required_pin()
        blocked = (pin.get("blocked") or {}).get("offered_pubkey")
        assert blocked == offered_pubkey, "trust must bless the exact blocked key"
        updated = {**pin, "publisher_pubkey": offered_pubkey, "blocked": None,
                   "unreachable": False, "consecutive_failures": 0,
                   "first_failure_at": None, "last_error": None,
                   "last_checked": (now or _now()).isoformat()}
        self._write_source(updated)
        return updated

    def record_failure(self, exc: Exception, *, now: datetime | None = None) -> dict:
        """Advance the consecutive-failure counter; escalate to ``unreachable`` when both
        thresholds trip (vault_sync mirror). Returns the persisted pin. Never raises.
        """
        assert isinstance(exc, Exception), "exc must be an exception"
        pin = self._required_pin()
        stamp_now = now or _now()
        host = urlparse(pin["url"]).hostname or ""
        prev_count = int(pin.get("consecutive_failures") or 0)
        count = prev_count + 1
        first_iso = pin.get("first_failure_at")
        first_at = _parse_iso(first_iso) if isinstance(first_iso, str) else None
        if first_at is None:
            first_iso = stamp_now.isoformat()
            first_at = stamp_now
        changes: dict = {
            "consecutive_failures": count,
            "first_failure_at": first_iso,
            "last_checked": stamp_now.isoformat(),
            "last_error": _host_error(host, exc),
        }
        if count >= UNREACHABLE_FAILURE_COUNT and \
                (stamp_now - first_at).total_seconds() >= UNREACHABLE_MIN_DAYS * 86_400:
            changes["unreachable"] = True
        updated = {**pin, **changes}
        self._write_source(updated)
        return updated

    def is_due(self, *, now: datetime | None = None) -> bool:
        """True when the check interval has elapsed since ``last_checked`` AND the
        source is not blocked or unreachable (which only clear on user action)."""
        pin = self.source()
        if pin is None:
            return False
        if pin.get("blocked") or pin.get("unreachable"):
            return False
        last = _parse_iso(pin.get("last_checked"))
        if last is None:
            return True
        interval = max(int(pin.get("check_interval_seconds") or DEFAULT_CHECK_INTERVAL_SECONDS),
                       MIN_CHECK_INTERVAL_SECONDS)
        return ((now or _now()) - last).total_seconds() >= interval

    def _required_pin(self) -> dict:
        """Return the source pin or raise LibraryError — used by every write path."""
        pin = self.source()
        if pin is None:
            raise LibraryError("no library source is connected")
        return pin


# --- helpers ---------------------------------------------------------------------------

def _now() -> datetime:
    """Aware UTC datetime — module seam so tests can pin the clock."""
    return datetime.now(UTC)


def _parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None on absence or bad shape."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _clear_failure_state() -> dict:
    """The pin fields that any verified answer from the host clears."""
    return {"consecutive_failures": 0, "first_failure_at": None,
            "unreachable": None, "last_error": None}


def _host_error(host: str, exc: Exception) -> str:
    """A host-only failure note for the source card — never the URL path (feed law)."""
    assert isinstance(host, str), "host must be a string"
    return f"couldn't reach {host}" if host else "couldn't reach the library host"


# LOW#1 (audit 2026-09-09): tiny module-level cache for the decrypted pack + per-template
# spec hash. Bounded to a SINGLE live pack — v1 allows one library source, and the store
# key is the sealed row's created_at (any apply_update rewrites the row, changing the
# timestamp, which invalidates the entry). The lock covers a plain-dict read-modify write.
_pack_cache_lock = threading.Lock()
_pack_cache: dict[str, tuple[dict, dict[str, str], str]] = {}
_PACK_CACHE_MAX = 4  # small — really just 1 in practice, headroom for a race during rotation


def _pack_cache_get(created_at: str) -> tuple[dict, dict[str, str], str] | None:
    """Return the cached (index, hashes, pack_id) triple for ``created_at`` or None."""
    assert isinstance(created_at, str), "created_at must be a string"
    if not created_at:
        return None
    with _pack_cache_lock:
        return _pack_cache.get(created_at)


def _pack_cache_put(created_at: str,
                    value: tuple[dict, dict[str, str], str]) -> None:
    """Insert one entry; evict oldest when past the bound (invalidation via timestamp)."""
    assert isinstance(created_at, str) and created_at, "created_at required"
    assert isinstance(value, tuple) and len(value) == 3, "value shape (index, hashes, pack_id)"
    with _pack_cache_lock:
        if len(_pack_cache) >= _PACK_CACHE_MAX and created_at not in _pack_cache:
            # FIFO trim: pop the first inserted key (dict insertion order preserved).
            first_key = next(iter(_pack_cache))
            _pack_cache.pop(first_key, None)
        _pack_cache[created_at] = value


# --- install (build a spec from a template, fill params) --------------------------------

# System keys that a template pack must NEVER carry into an installed item, per §20:
# consent-bearing (contract, _c2_ok), engine-owned (_l1_*, _l2_*), the installer's
# LOCAL repair-policy choice (Phase 4b D2c — a pack cannot silently opt items into
# `l2_frontier: true`), and provenance itself (`_template` — the install path stamps
# its own from the pack it read).
_TEMPLATE_STRIP_KEYS = ("contract", "_c2_ok", "_l1_last_attempt", "_l1_trial",
                        "_l2_last_attempt", "_l2_proposal", "_template",
                        "repair_policy")


def build_installed_spec(template: dict, param_values: dict) -> dict:
    """Fill a template's ``spec_template`` with user param values; return a spec fit for
    ``NIStore.add_item``. Refuses secret values in the body (secrets travel the
    credential path). §19/§20.

    Rules:
      * string/number param values are copied in verbatim (bounded by _MAX_PARAM_VALUE);
      * secret params keep their ``ni:self:<name>`` placeholder — the item's credential
        PUT stores the real value under ``ni:<item_id>:<name>`` post-creation;
      * ``_c2_ok`` / ``contract`` / ``_l1_*`` / ``_l2_*`` / ``_template`` and
        ``repair_policy`` are stripped (belt-and-suspenders — a template that carried
        them fails parse_pack's full-spec check; this is the second gate at install
        time). Phase 4b D2c: ``repair_policy`` is REPLACED with the safe default
        ``{"l1": true, "l2_frontier": false}`` — the installer's local /repair-policy
        endpoint is the only way to opt into L2, per §23's consent posture;
      * every param declared by the template MUST be filled (empty string is OK only
        when the template shipped it empty AND the kind is string/number).
    """
    assert isinstance(template, dict), "template must be a dict"
    assert isinstance(param_values, dict), "param_values must be a dict"
    spec_template = template.get("spec_template")
    if not isinstance(spec_template, dict):
        raise LibraryError("template.spec_template missing")
    spec = _deep_copy_json(spec_template)
    for key in _TEMPLATE_STRIP_KEYS:
        spec.pop(key, None)
    # D2c: default repair policy is the installer's LOCAL choice; force the safe
    # default here so an installed item never inherits an invisible l2_frontier flag.
    spec["repair_policy"] = {"l1": True, "l2_frontier": False}
    _fill_params(spec, param_values)
    return spec


def _fill_params(spec: dict, values: dict) -> None:
    """Copy string/number values into spec.params[name].value; refuse secrets in body."""
    assert isinstance(spec, dict) and isinstance(values, dict), "spec + values required"
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        raise LibraryError("template spec.params is malformed")
    unknown = set(values) - set(params)
    if unknown:
        raise LibraryError(f"unknown params in install body: {sorted(unknown)}")
    for name, decl in params.items():  # bounded by ni._MAX_PARAMS
        if not isinstance(decl, dict):
            raise LibraryError(f"template spec.params.{name} malformed")
        kind = decl.get("kind")
        if name not in values:
            continue  # leave the template's default (may be empty)
        raw = values[name]
        if kind == "secret":
            raise LibraryError(
                f"secret param {name!r} may not be supplied in the install body — "
                "use the credential path after the item is created")
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            raise LibraryError(f"param {name!r} value must be a string or number")
        if isinstance(raw, str) and len(raw) > ni._MAX_PARAM_VALUE:
            raise LibraryError(f"param {name!r} value too long")
        decl["value"] = raw


def _deep_copy_json(value: dict) -> dict:
    """Deep-copy via canonical JSON round-trip — used before mutation so a shared
    template dict is never touched. Canonical here just guarantees float-free deep copy."""
    assert isinstance(value, dict), "value must be a dict"
    assert value, "value must be non-empty"
    return json.loads(vault_format.canonical(value).decode("utf-8"))


# --- ni:self: <-> ni:<item_id>: rewrites (§19/§20/§21) --------------------------------

def rewrite_self_refs(spec: dict, item_id: str) -> None:
    """Bind a template's ``ni:self:<name>`` refs to the concrete ``ni:<item_id>:<name>``.

    The flagship authenticated template path (H2, audit 2026-09-09): templates ship with
    ``{"$secret": "ni:self:api_key"}`` headers and ``params.api_key.value = "ni:self:api_key"``
    because the item id is unknown at PUBLISH time. The item id is minted at add_item
    time; the install path pre-mints so this rewrite runs BEFORE the spec is sealed
    (single-write, no re-seal race). apply-template-update runs the same rewrite against
    the existing item id (spec is being re-sealed anyway via update_spec).

    Without this rewrite, ``_load_credential`` rejects the ref (expected prefix
    ``ni:<item_id>:``) and every fetch fails ``secret_not_scoped`` — the item's engine
    path never attaches the credential. Mutates ``spec`` in place.
    """
    assert isinstance(spec, dict), "spec must be a dict"
    assert isinstance(item_id, str) and item_id, "item id required"
    prefix_new = f"ni:{item_id}:"
    source = spec.get("source") or {}
    headers = source.get("headers") if isinstance(source, dict) else None
    if isinstance(headers, dict):
        for value in headers.values():  # bounded by ni._MAX_HEADERS
            if not isinstance(value, dict):
                continue
            ref = value.get("$secret")
            if isinstance(ref, str) and ref.startswith(ni._NI_SELF_PLACEHOLDER):
                value["$secret"] = prefix_new + ref[len(ni._NI_SELF_PLACEHOLDER):]
    params = spec.get("params") or {}
    if isinstance(params, dict):
        for decl in params.values():  # bounded by ni._MAX_PARAMS
            if not isinstance(decl, dict) or decl.get("kind") != "secret":
                continue
            value = decl.get("value")
            if isinstance(value, str) and value.startswith(ni._NI_SELF_PLACEHOLDER):
                decl["value"] = prefix_new + value[len(ni._NI_SELF_PLACEHOLDER):]


def rewrite_refs_to_self(spec: dict, item_id: str) -> None:
    """Export-side inverse of ``rewrite_self_refs`` — rewrites this item's concrete
    ``ni:<item_id>:<name>`` refs BACK to ``ni:self:<name>`` (§21).

    Two guarantees at once: (a) the exported template installs cleanly for the next
    subscriber (round-trips through parse_pack + build_installed_spec) and (b) the
    export never leaks this item's UUID inside a header ref or a secret param value.
    """
    assert isinstance(spec, dict), "spec must be a dict"
    assert isinstance(item_id, str) and item_id, "item id required"
    prefix_old = f"ni:{item_id}:"
    source = spec.get("source") or {}
    headers = source.get("headers") if isinstance(source, dict) else None
    if isinstance(headers, dict):
        for value in headers.values():  # bounded by ni._MAX_HEADERS
            if not isinstance(value, dict):
                continue
            ref = value.get("$secret")
            if isinstance(ref, str) and ref.startswith(prefix_old):
                value["$secret"] = ni._NI_SELF_PLACEHOLDER + ref[len(prefix_old):]
    params = spec.get("params") or {}
    if isinstance(params, dict):
        for decl in params.values():  # bounded by ni._MAX_PARAMS
            if not isinstance(decl, dict) or decl.get("kind") != "secret":
                continue
            value = decl.get("value")
            if isinstance(value, str) and value.startswith(prefix_old):
                decl["value"] = ni._NI_SELF_PLACEHOLDER + value[len(prefix_old):]


def provenance_for(pack_id: str, template_id: str, seq: int, spec_template: dict) -> dict:
    """The sealed ``_template`` stamp written into an installed item's spec (§20)."""
    assert isinstance(pack_id, str) and pack_id, "pack_id required"
    assert isinstance(template_id, str) and template_id, "template_id required"
    assert isinstance(seq, int) and seq >= 1, "seq must be positive"
    return {"pack_id": pack_id, "template_id": template_id,
            "seq": int(seq), "spec_hash": spec_hash(spec_template)}


# --- update-check tick hook (§20) -------------------------------------------------------

def tick(app, pass_budget_seconds: float | None = None) -> dict:
    """One library check per tick when due (§20 cadence: 24h default, 1h floor).

    Mirrors ``vault_sync.tick`` discipline: unlocked-only, per-thread cursor, one bounded
    try/except so a dead host can NEVER kill the scheduler. A KeyChanged blocks the source
    (never applied); a rollback stamps last_checked WITHOUT counting toward unreachable
    (M3 audit 2026-09-09); a network/verify failure advances the failure counter; a clean
    verdict clears failures and applies iff behind.

    Returns ``{"ran": 0|1, "notice": None | {"kind": "blocked"|"rollback", "message": str}}``
    (LOW#5 audit 2026-09-09): ``notice`` is populated only on a state TRANSITION so the
    scheduler wrapper posts one carrier row per state change (not per tick).
    """
    assert app is not None, "app required"
    key = getattr(app.state, "master_key", None)
    session = getattr(app.state, "session_id", None)
    if key is None or session is None:
        return {"ran": 0, "notice": None}
    cursor = app.state.db.cursor()
    assert cursor is not None, "per-thread cursor required"
    try:
        store = LibraryStore(ni.NIStore(cursor, key))
        if not store.is_due():
            return {"ran": 0, "notice": None}
        if pass_budget_seconds is not None and pass_budget_seconds <= 0:
            return {"ran": 0, "notice": None}
        notice = _tick_one(store)
        return {"ran": 1, "notice": notice}
    finally:
        try:
            cursor.close()
        except Exception:  # already closed / DB torn down
            pass


_LIBRARY_BLOCKED_NOTICE = "Library updates are blocked — open Neural Interface → Library."
_LIBRARY_ROLLBACK_NOTICE = (
    "Library updates paused — the host is serving an older pack. "
    "Open Neural Interface → Library."
)


def _tick_one(store: LibraryStore) -> dict | None:
    """Run ONE library check + optional apply — every failure caught here (never re-raised).

    Returns a notice hint (``{"kind": ..., "message": ...}``) when the source's status
    TRANSITIONED to blocked or rollback THIS tick, else None (LOW#5). Log-only outcomes
    (network failures, up-to-date, applied) return None — they surface on the sheet.
    """
    assert store is not None, "store required"
    try:
        verdict = store.check_update()
    except KeyChanged as exc:
        transitioned = store.block(exc.offered_pubkey)
        log.info("ni library: publisher key changed — blocked (offered=%s)",
                 vault_format.fingerprint(exc.offered_pubkey))
        return {"kind": "blocked", "message": _LIBRARY_BLOCKED_NOTICE} if transitioned else None
    except RollbackError as exc:
        transitioned = store.record_rollback(exc.remote_seq, exc.pinned_seq)
        log.info("ni library: rollback refused: %s", exc)
        return {"kind": "rollback", "message": _LIBRARY_ROLLBACK_NOTICE} if transitioned else None
    except LibraryError as exc:
        store.record_failure(exc)
        log.warning("ni library: check failed: %s", type(exc).__name__)
        return None
    except Exception as exc:  # netguard.FetchError etc. — never let ONE bad host wedge the tick
        store.record_failure(exc)
        log.warning("ni library: check failed with %s", type(exc).__name__)
        return None
    if not verdict.get("behind"):
        return None
    try:
        store.apply_update(verdict)
    except Exception as exc:  # rare: a write failure after verify
        store.record_failure(exc)
        log.warning("ni library: apply failed with %s", type(exc).__name__)
    return None
