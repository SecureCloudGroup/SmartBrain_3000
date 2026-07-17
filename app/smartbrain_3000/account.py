"""Account + secrets HTTP API for SmartBrain_3000.

Wires the local key vault (passphrase / recovery key — see ``keyvault.py``) and
the encrypted secret store (``secrets.py``) into the running app:

* first-run **setup** sets a passphrase and returns the Emergency Kit once,
* **unlock** loads the master key into memory for the session,
* secret values can be **stored / listed / deleted** only while unlocked.

Secret *values* are never returned over the API — only their names. The app
reads secret values internally (e.g. to call an LLM provider).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import email_account, gateway, keyvault
from .approvals import ApprovalStore
from .audit import AuditLog
from .history import ChatHistory
from .kb import KnowledgeBase
from .memory import MemoryStore
from .planner import Planner
from .scheduler import ScheduleStore
from .secrets import MASTER_KEY_BYTES, SecretStore
from .vaults import VaultStore

router = APIRouter()
log = logging.getLogger(__name__)

_MIN_PASSPHRASE = 8


class SetupRequest(BaseModel):
    passphrase: str = Field(min_length=_MIN_PASSPHRASE)


class UnlockRequest(BaseModel):
    passphrase: str | None = None
    recovery_key: str | None = None


class PassphraseChange(BaseModel):
    current_passphrase: str = Field(min_length=1)
    new_passphrase: str = Field(min_length=_MIN_PASSPHRASE)


class PassphraseReset(BaseModel):
    new_passphrase: str = Field(min_length=_MIN_PASSPHRASE)


class SecretValue(BaseModel):
    value: str = Field(min_length=1)


def _conn(request: Request):
    """Return the thread-safe DB facade (per-thread cursors) from app state.

    All request-time DB access (keyvault + every store) flows through here, so
    routing it to the ThreadLocalConn facade makes the whole request path safe
    against the Starlette threadpool sharing one DuckDB connection.
    """
    dbx = getattr(request.app.state, "dbx", None)
    assert dbx is not None, "database facade must be initialized"
    return dbx


def _set_unlocked(request: Request, master_key: bytes) -> None:
    """Hold the master key + an open SecretStore in memory; provision Bifrost."""
    assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
    request.app.state.master_key = master_key
    request.app.state.secret_store = SecretStore(_conn(request), master_key)
    # `vaults` MUST be provisioned before `kb`: a live `kb` with a still-None `vaults` is a fail-OPEN
    # window in which the MCP read tools would serve imported-vault content WITHOUT its provenance
    # banner (tagging short-circuits to "no tag" when vaults is None). Unlock runs on the threadpool
    # sharing app.state with the concurrently-served MCP mount, so that window is reachable. Ordering
    # vaults-then-kb makes it fail-CLOSED (kb None -> _knowledge() raises), mirroring _set_locked which
    # clears kb before vaults for the same reason.
    request.app.state.vaults = VaultStore(_conn(request), master_key)
    request.app.state.kb = KnowledgeBase(_conn(request), master_key)
    request.app.state.history = ChatHistory(_conn(request), master_key)
    request.app.state.memory = MemoryStore(_conn(request), master_key)
    request.app.state.planner = Planner(_conn(request), master_key)
    request.app.state.audit = AuditLog(_conn(request), master_key)
    session_id = uuid.uuid4().hex  # binds pending approvals to this unlock session
    request.app.state.session_id = session_id
    request.app.state.approvals = ApprovalStore(_conn(request), master_key, session_id)
    request.app.state.schedules = ScheduleStore(_conn(request), master_key)
    request.app.state.email = email_account.build_client(request.app.state.secret_store)  # None until connected
    try:
        gateway.provision_from_store(request.app.state.secret_store)
    except Exception as exc:  # gateway unreachable — best effort
        log.warning("provider provisioning skipped: %s", exc)
    try:
        gateway.provision_local_from_store(request.app.state.secret_store)
    except Exception as exc:  # gateway unreachable — best effort
        log.warning("local provisioning skipped: %s", exc)
    # Resume remote access if the user has already paired a device — a fresh, never-paired vault
    # stays fully offline. Pairing is the opt-in; this just reconnects the broker link across
    # restarts (see main._webrtc_loop / lazy-start).
    try:
        from . import devices, remote_config
        ev = getattr(request.app.state, "webrtc_active", None)
        if ev is not None and remote_config.signaling_url() and devices.list_devices(request.app.state.secret_store):
            ev.set()
    except Exception as exc:  # never block unlock on the remote-access check
        log.warning("remote-access resume check skipped: %s", exc)
    # NOTE: the one-shot eager embeddings backfill (after the destructive 13->14
    # migration) runs on the scheduler's first tick (scheduler.eager_reindex), not a
    # per-unlock daemon thread — so it can't leak a DB cursor past teardown.


def _require_store(request: Request) -> SecretStore:
    """Return the unlocked SecretStore, or raise 423 if locked."""
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


# B8: Desktop-local marker for security-sensitive admin endpoints.
#
# The WebRTC bridge (``webrtc_bridge.py``) accepts framed requests from paired
# remote devices and replays them onto loopback via httpx. ``parse_request``
# filters the peer's headers down to a tiny allowlist (currently only
# ``content-type``, ``accept``, ``accept-language``), so any header outside that
# allowlist CANNOT survive the bridge. We require the real Desktop UI to send
# ``X-SB-Local: 1`` on destructive admin calls (restore, passphrase-reset); a
# bridged-in request will not carry it and is refused with 403.
_LOCAL_HEADER = "x-sb-local"


def _require_desktop_local(request: Request) -> None:
    """Refuse requests that arrived via the WebRTC bridge (paired remote device)."""
    assert request is not None, "request required"
    marker = request.headers.get(_LOCAL_HEADER)
    assert isinstance(marker, str) or marker is None, "header must be a string or absent"
    if marker != "1":
        raise HTTPException(status_code=403, detail="this endpoint is Desktop-local only")


@router.get("/api/account/status")
def account_status(request: Request) -> dict[str, bool]:
    """Report whether the vault is initialized, unlocked, and has a recovery key."""
    conn = _conn(request)
    unlocked = getattr(request.app.state, "secret_store", None) is not None
    return {
        "initialized": keyvault.is_initialized(conn),
        "unlocked": unlocked,
        "has_recovery": keyvault.has_recovery(conn),
    }


@router.post("/api/account/setup")
def account_setup(request: Request, body: SetupRequest) -> dict[str, str]:
    """First run: set the passphrase, create a Recovery Key, return the kit once."""
    conn = _conn(request)
    if keyvault.is_initialized(conn):
        raise HTTPException(status_code=409, detail="already initialized")
    master_key = keyvault.set_passphrase(conn, body.passphrase)
    recovery_key = keyvault.add_recovery_key(conn, master_key)
    _set_unlocked(request, master_key)
    assert keyvault.has_recovery(conn), "recovery key must exist after setup"
    return {
        "recovery_key": recovery_key,
        "emergency_kit": keyvault.emergency_kit_text(recovery_key),
    }


@router.post("/api/account/unlock")
def account_unlock(request: Request, body: UnlockRequest) -> dict[str, bool]:
    """Unlock with the passphrase or the recovery key; load the master key."""
    conn = _conn(request)
    if not keyvault.is_initialized(conn):
        raise HTTPException(status_code=409, detail="not initialized; run setup")
    try:
        if body.passphrase:
            master_key = keyvault.unlock(conn, body.passphrase)
        elif body.recovery_key:
            master_key = keyvault.unlock_with_recovery(conn, body.recovery_key)
        else:
            raise HTTPException(status_code=400, detail="passphrase or recovery_key required")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="invalid credentials") from None
    _set_unlocked(request, master_key)
    return {"unlocked": True}


@router.post("/api/account/lock")
def account_lock(request: Request) -> dict[str, bool]:
    """Remove provider keys from Bifrost, then drop the master key from memory."""
    try:
        gateway.deprovision()
    except Exception as exc:  # best effort
        log.warning("provider deprovision skipped: %s", exc)
    try:
        gateway.deprovision_local()
    except Exception as exc:  # best effort
        log.warning("local deprovision skipped: %s", exc)
    request.app.state.master_key = None
    request.app.state.secret_store = None
    request.app.state.kb = None
    request.app.state.history = None
    request.app.state.memory = None
    request.app.state.planner = None
    request.app.state.audit = None
    request.app.state.approvals = None
    request.app.state.session_id = None
    request.app.state.schedules = None
    request.app.state.vaults = None
    request.app.state.email = None
    request.app.state.email_oauth_pending = None
    return {"unlocked": False}


@router.post("/api/account/passphrase")
def change_passphrase(request: Request, body: PassphraseChange) -> dict[str, bool]:
    """Change the unlock passphrase (requires unlock + the current passphrase).

    Re-wraps the same master key, so all data and the Recovery Key stay valid.
    """
    _require_store(request)  # must be unlocked
    try:
        keyvault.change_passphrase(_conn(request), body.current_passphrase, body.new_passphrase)
    except Exception:
        raise HTTPException(status_code=401, detail="current passphrase is incorrect") from None
    return {"ok": True}


@router.post("/api/account/passphrase/reset")
def reset_passphrase(request: Request, body: PassphraseReset) -> dict[str, bool]:
    """Set a new passphrase without the current one — for a session unlocked via
    the Recovery Key. Requires unlock (the in-memory master key is the authority).

    Desktop-local only: a request bridged in from a paired remote device (over
    WebRTC) MUST NOT be able to rotate the passphrase. See ``_require_desktop_local``.
    """
    _require_desktop_local(request)  # B8: refuse bridged-in requests
    _require_store(request)  # must be unlocked
    master_key = getattr(request.app.state, "master_key", None)
    assert master_key is not None, "unlocked session must hold the master key"
    keyvault.reset_passphrase(_conn(request), master_key, body.new_passphrase)
    return {"ok": True}


def _require_provider_key(key: str) -> None:
    """The generic secrets API manages ONLY provider API keys (the Providers page; named
    ``provider:<name>:api_key``). Refuse every other namespace so an unlocked-session bug or a
    confused-deputy through the local API can't clobber/delete the Gmail refresh token, device
    pairing records, or the MCP/WebRTC creds — those are written only by their own code paths."""
    if not (key.startswith("provider:") and key.endswith(":api_key")):
        raise HTTPException(status_code=403, detail="this endpoint manages provider keys only")


@router.put("/api/secrets/{key}")
def put_secret(request: Request, key: str, body: SecretValue) -> dict[str, bool]:
    """Store a secret (requires unlock); sync provider keys to Bifrost live.

    The response carries ``gateway_synced`` so the UI can tell the user whether
    the key is actually live in Bifrost — a transient gateway hiccup leaves the
    secret stored (so it isn't lost) but reports ``gateway_synced: false`` so
    the user doesn't believe the provider is configured when it isn't.
    """
    assert key, "secret key must be non-empty"
    assert body.value, "secret value must be non-empty"
    _require_provider_key(key)
    _require_store(request).put(key, body.value)
    bifrost_name = gateway.provider_for_secret_key(key)
    gateway_synced = False
    if bifrost_name:
        try:
            gateway.set_provider(bifrost_name, body.value)
            gateway_synced = True
        except Exception as exc:  # secret stays stored; UI sees gateway_synced=False
            log.warning("provider sync failed: %s", exc)
    return {"ok": True, "gateway_synced": gateway_synced}


@router.get("/api/secrets")
def list_secrets(request: Request) -> dict[str, list[str]]:
    """List provider-key names only — never values (requires unlock).

    The secret store also holds device pairing records, the Gmail refresh token,
    and the MCP access token. Returning all key names would enumerate paired
    devices, email state, and MCP setup to any unlocked session — the UI only
    needs the provider-key namespace (the Providers page), so we filter to that.
    """
    all_keys = _require_store(request).list_keys()
    assert isinstance(all_keys, list), "list_keys must return a list"
    provider_keys = [k for k in all_keys if k.startswith("provider:") and k.endswith(":api_key")]
    assert len(provider_keys) <= len(all_keys), "filter cannot grow the list"
    return {"keys": provider_keys}


@router.delete("/api/secrets/{key}")
def delete_secret(request: Request, key: str) -> dict[str, bool]:
    """Delete a secret (requires unlock); remove provider keys from Bifrost."""
    assert key, "secret key must be non-empty"
    _require_provider_key(key)
    _require_store(request).delete(key)
    bifrost_name = gateway.provider_for_secret_key(key)
    if bifrost_name:
        try:
            gateway.remove_provider(bifrost_name)
        except Exception as exc:  # best effort
            log.warning("provider removal skipped: %s", exc)
    return {"ok": True}
