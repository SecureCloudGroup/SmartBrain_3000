"""Browser components (Round 20, ni-format §35): standalone, replaceable, hash-pinned
headless browsers the app can load to read a page the way a browser does.

Each engine is described by a manifest shipped inside this package (``engines/<name>.json``).
Nothing here trusts the engine: it runs as a one-shot ``fetch`` subprocess under jail
hygiene, inside an OS wall, with its only network route through a key-less egress child
that enforces the SSRF guard, the site policy and the caps. A missing or failed wall
means ``unavailable``, never a render (fail closed).

Modules:

- ``manifest``   — the strict manifest schema and the argv token grammar.
- ``cmdline``    — value checks + the argv/env builder (closed placeholders, allowlisted
  flags, forbidden tokens golden-tested).
- ``release``    — pinned download (one allowlisted redirect, size cap, hash) + safe unpack.
- ``install``    — lifecycle: phases, commit/prune, self-test, ``verify_installed`` on
  every launch, Status rows.
- ``jail_egress``/``egress`` — the CONNECT-only egress child and its parent-side handle.
- ``walls``      — the confinement walls: macOS Seatbelt (the measured five-rule profile),
  Linux user/mount/network/PID namespaces with a relay, the Docker renderer sidecar
  (``render_agent`` runs inside it), and the test-only ``NullWall`` for the fake engine.
- ``proc``       — engine process hygiene: spawn, watchdogs, group kill, stderr tap.
- ``runner``     — one render: verify → wall → egress → engine → tripwires → cleanup.
- ``router``     — static tier (netguard) → render tier, need signals, circuit breaker.

The flow gate (Round 20, B3a): ``available()`` is the ONE eligibility answer every caller
consults before a render — ``{eligible, engine, version, reason}`` from the Status rows
(eligible only after a self-test passed on this machine under the wall in use;
``SMARTBRAIN_NO_BROWSER=1`` → ``disabled``). ``prepare()`` starts the pinned install /
the once-per-boot self-test in the background on first need (never at boot);
``progress()`` is the phase and percentage a flow shows while it waits.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("smartbrain.browsers")

DEFAULT_ENGINE = "obscura"
AVAILABLE_KEYS = ("eligible", "engine", "version", "reason")
# Why no render may run now (closed): the opt-out, a broken manifest, an install blocker or
# wall reason from the Status row, or the row's phase when it names no reason.
REASONS = frozenset({"", "disabled", "manifest", "platform", "missing_libs", "sandbox",
                     "userns_blocked", "apparmor_userns", "docker_no_sidecar", "tier_disabled",
                     "absent", "stale", "preparing", "error", "self_test"})
# Reasons ``prepare()`` can clear (download, re-install, or the once-per-boot self-test).
PREPARABLE = frozenset({"absent", "stale", "self_test"})
SIDECAR_STATES = ("missing", "ready", "stale", "n/a")
# Set the first time since boot a render is WANTED — a flow's ladder would render, or a
# rendered card is due or run — whether or not this machine can render (never cleared).
_WANTED = threading.Event()
_PHASE_REASON = {"absent": "absent", "stale": "stale", "error": "error", "ready": "self_test",
                 "disabled": "disabled", "unavailable": "sandbox", "downloading": "preparing",
                 "verifying": "preparing", "unpacking": "preparing", "self-testing": "preparing"}
_PREPARE_LOCK = threading.Lock()


def _answer(eligible: bool, engine: str, version: str, reason: str) -> dict:
    assert reason in REASONS, f"unknown availability reason: {reason}"
    assert eligible == (reason == ""), "eligible exactly when no reason"
    return {"eligible": eligible, "engine": engine, "version": version, "reason": reason}


def available(engine: str = DEFAULT_ENGINE) -> dict:
    """``{eligible, engine, version, reason}`` for ``engine`` — cheap (the Status row: no
    hashing, no network; the wall pre-check probes at most once per process)."""
    from . import install
    from . import manifest as mf

    assert isinstance(engine, str) and engine, "engine name required"
    if install.disabled():
        return _answer(False, engine, "", "disabled")
    try:
        m = mf.load(engine)
    except ValueError:
        return _answer(False, engine, "", "manifest")
    try:
        row = install.status_row(m)
    except Exception as exc:  # never a caller's crash: an unreadable row is no render
        log.warning("browsers: status row for %s failed (%s)", engine, exc.__class__.__name__)
        return _answer(False, m.name, m.version, "error")
    if row["eligible"]:
        return _answer(True, m.name, m.version, "")
    reason = row["reason"] if row["reason"] in REASONS and row["reason"] else \
        _PHASE_REASON.get(str(row["phase"]), "preparing")
    assert reason, "an ineligible engine names its reason"
    return _answer(False, m.name, m.version, reason)


def note_wanted() -> None:
    """A render is wanted (the ladder would render, a rendered card is due or run): the
    launcher's cue to start the Docker renderer sidecar. Sticky until the process ends."""
    assert isinstance(_WANTED, threading.Event), "the wanted flag"
    _WANTED.set()
    assert _WANTED.is_set(), "the flag is set"


def renderer_status() -> dict:
    """``{wanted, sidecar}`` for the Status surface, readable while locked and never raising:
    ``sidecar`` = ``walls.sidecar_state()`` (``n/a`` outside Docker), ``missing`` when the
    shared volume can't be read."""
    from . import walls

    try:
        sidecar = walls.sidecar_state()
    except Exception as exc:  # a broken volume reads as no sidecar, never a status failure
        log.warning("browsers: sidecar state unreadable (%s)", exc.__class__.__name__)
        sidecar = "missing"
    out = {"wanted": _WANTED.is_set(), "sidecar": sidecar}
    assert out["sidecar"] in SIDECAR_STATES, "a closed sidecar state"
    assert set(out) == {"wanted", "sidecar"}, "closed keys"
    return out


def progress(engine: str = DEFAULT_ENGINE) -> dict:
    """``{phase, pct}`` of ``engine``'s Status row (what a waiting flow shows)."""
    from . import install
    from . import manifest as mf

    assert isinstance(engine, str) and engine, "engine name required"
    try:
        row = install.status_row(mf.load(engine))
    except Exception:  # a broken manifest or an unreadable row: nothing to show as progress
        return {"phase": "unavailable", "pct": 0}
    out = {"phase": str(row["phase"]), "pct": max(0, min(100, int(row["pct"])))}
    assert out["phase"] in install.PHASES, "phases are closed"
    return out


def prepare(engine: str = DEFAULT_ENGINE) -> bool:
    """Start ``install.ensure(engine)`` on a daemon thread (single-flight here; the installer
    is single-flight per engine too). True when this call started it. Never at boot: callers
    are a creation / Fix that needs a render and the tick's first due rendered card."""
    from . import install

    assert isinstance(engine, str) and engine, "engine name required"
    if install.disabled() or not _PREPARE_LOCK.acquire(blocking=False):
        return False

    def run() -> None:
        assert _PREPARE_LOCK.locked(), "the prepare claim is held"
        assert engine, "an engine to prepare"
        try:
            install.ensure(engine)
        except Exception as exc:  # the Status row carries the fixed sentence; log the class only
            log.warning("browsers: prepare %s failed (%s)", engine, exc.__class__.__name__)
        finally:
            _PREPARE_LOCK.release()

    try:
        threading.Thread(target=run, name="browser-prepare", daemon=True).start()
    except RuntimeError:
        _PREPARE_LOCK.release()
        return False
    assert engine, "a started prepare names its engine"
    return True
