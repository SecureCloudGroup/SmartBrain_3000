"""Browser component installer (ni-format §35) — the Whisper download (``stt_local``) with
its gaps closed. ``release`` does the pinned download and the safe unpack; this module owns
the lifecycle around them:

- layout ``<data>/browsers/<name>/<version>/`` (outside ``models/``), single flight per
  engine, phases ``absent · downloading(pct) · verifying · unpacking · self-testing · ready ·
  unavailable · error · stale · disabled``;
- ``<version>.partial/`` → ``INSTALLED.json`` → one atomic rename → prune every other
  version → self-test (injected; by default ``<engine> --version`` under the wall when one
  is available, else ``skipped(wall_unavailable)``);
- ``verify_installed`` re-hashes the installed files against the manifest on EVERY launch;
  nothing is cached;
- ``status_row`` for the Status API: cheap (no hashing, no network), readable while locked.

``SMARTBRAIN_NO_BROWSER=1`` turns the whole component off (phase ``disabled``, nothing
downloads); the test suite sets it. An engine that cannot run here (no build for this
platform, glibc below the manifest's minimum) is never downloaded. User-facing errors are
fixed sentences that name github.com and never echo an exception, a path or a URL — the
class goes to the log.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import platform as _platform
import re
import shutil
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .. import db
from . import manifest as mf
from . import release, walls

log = logging.getLogger("smartbrain.browsers")

DISABLE_ENV = "SMARTBRAIN_NO_BROWSER"
PHASES = ("absent", "downloading", "verifying", "unpacking", "self-testing", "ready",
          "unavailable", "error", "stale", "disabled")
TRANSIENT = frozenset({"downloading", "verifying", "unpacking", "self-testing"})
INSTALLED_NAME = "INSTALLED.json"
SELF_TEST_RESULTS = ("never", "ok", "skipped(wall_unavailable)")
_INSTALLED_KEYS = frozenset({"name", "version", "sha256", "installed_at", "platform"})
_CHUNK = 1 << 20
_MAX_DIR_ENTRIES = 64
_SENTENCES = {
    "network": "could not reach the page browser download (github.com) — check the network and retry",
    "timeout": "the page browser download from github.com timed out — check the network and retry",
    "truncated": "the page browser download from github.com was cut short — retry",
    "server": "github.com could not serve the page browser right now — retry later",
    "refused": "github.com refused the page browser download — retry later",
    "redirect": "the page browser download left github.com for a host SmartBrain does not trust — refused",
    "oversize": "the page browser download from github.com was larger than this release pins — refused",
    "hash": "the page browser download from github.com did not match this release's pinned hash — refused",
    "unsafe_tar": "the page browser package from github.com held unexpected files — refused",
    "disk": "not enough disk space to install the page browser — free some space and retry",
    "self_test": "the page browser did not pass its start-up check on this computer",
    "internal": "the page browser install failed — retry; if it keeps failing, check the log",
}

_state_lock = threading.Lock()
_state: dict[str, dict] = {}   # engine -> {phase, pct, error, last_self_test} (transient/error only)
_inflight: set[str] = set()
_rename = os.rename            # module attribute so a test can fail the commit step

SelfTest = Callable[[mf.Manifest, Path], str]


def disabled() -> bool:
    """The ``SMARTBRAIN_NO_BROWSER`` opt-out (any non-empty value but ``0``)."""
    value = os.environ.get(DISABLE_ENV, "")
    assert isinstance(value, str), "env values are strings"
    assert DISABLE_ENV.startswith("SMARTBRAIN_"), "the opt-out is a SmartBrain variable"
    return value not in ("", "0")


def platform_key() -> str | None:
    """This machine's manifest platform key, or None where no engine build exists."""
    machine = _platform.machine().lower()
    assert isinstance(machine, str), "machine must be a string"
    assert isinstance(sys.platform, str), "platform must be a string"
    if sys.platform == "darwin" and machine in ("arm64", "aarch64"):
        return "darwin-arm64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    if sys.platform.startswith("linux") and machine in ("aarch64", "arm64"):
        return "linux-aarch64"
    return None


def glibc_version() -> tuple[int, int] | None:
    """The running glibc as (major, minor); None on musl/macOS or when unknown."""
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError, AttributeError):
        return None
    assert isinstance(text, str), "confstr returns a string"
    match = re.fullmatch(r"glibc (\d+)\.(\d+).*", text)
    assert match is None or len(match.groups()) == 2, "glibc version has two parts"
    return (int(match.group(1)), int(match.group(2))) if match else None


def install_blocker(m: mf.Manifest, platform: str | None) -> str:
    """Why this engine can never run here (``platform`` / ``missing_libs``), else ``""``.
    An engine that cannot run is never downloaded."""
    assert isinstance(m, mf.Manifest), "manifest required"
    if platform is None or platform not in m.platforms:
        return "platform"
    if platform.startswith("linux"):
        version = glibc_version()
        if version is None or version < m.min_glibc:
            return "missing_libs"
    assert platform in mf.PLATFORMS, "platform keys come from the closed set"
    return ""


def browsers_root() -> Path:
    """``<data dir>/browsers`` — outside ``models/`` so the voice-model size stays honest."""
    root = db.resolve_db_path().parent / "browsers"
    assert root.name == "browsers", "engines live under browsers/"
    assert root.parent.name, "the data dir has a name"
    return root


def engine_dir(name: str, version: str) -> Path:
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name), "engine names are path-safe"
    assert re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version), "versions are path-safe"
    return browsers_root() / name / version


def _set(name: str, **fields: object) -> None:
    assert name, "engine name required"
    assert fields, "nothing to set"
    with _state_lock:
        entry = _state.setdefault(name, {"phase": "", "pct": 0, "error": "",
                                         "last_self_test": "never"})
        assert set(fields) <= set(entry), "state keys are closed"
        entry.update(fields)


def _snapshot(name: str) -> dict:
    assert name, "engine name required"
    with _state_lock:
        entry = dict(_state.get(name) or {"phase": "", "pct": 0, "error": "",
                                          "last_self_test": "never"})
    assert "phase" in entry, "state carries a phase"
    return entry


def _claim(name: str) -> bool:
    """Single flight per engine: True when this caller owns the install."""
    assert name, "engine name required"
    with _state_lock:
        if name in _inflight:
            return False
        _inflight.add(name)
    assert name in _inflight, "the claim is recorded"
    return True


def _release(name: str) -> None:
    assert name, "engine name required"
    with _state_lock:
        _inflight.discard(name)
    assert name not in _inflight, "the claim is gone"


def ensure(name: str, *, transport: release.Transport | None = None,
           self_test: SelfTest | None = None) -> dict:
    """Install the pinned version of ``name`` unless it is present and re-hashes clean (a
    modified install is replaced); return its status row. Never downloads when disabled,
    on an unsupported platform, or with missing libraries."""
    m = mf.load(name)
    assert m.name == name, "manifest name must match"
    plat = platform_key()
    if disabled() or install_blocker(m, plat):
        return status_row(m)
    if verify_installed(name, manifest_obj=m, platform=plat)[0]:
        return status_row(m)
    assert plat is not None, "an unblocked platform is known"
    install(m, plat, transport=transport, self_test=self_test)
    return status_row(m)


def install(m: mf.Manifest, platform: str, *, transport: release.Transport | None = None,
            self_test: SelfTest | None = None) -> dict:
    """Download → verify → unpack → commit → prune → self-test, single-flight.
    Returns ``{ok, code, served_by, self_test}``; failures land in the status row."""
    assert platform in m.platforms, "platform must be pinned"
    assert isinstance(m, mf.Manifest), "manifest required"
    if disabled():
        return {"ok": False, "code": "disabled", "served_by": "", "self_test": ""}
    if not _claim(m.name):
        return {"ok": False, "code": "busy", "served_by": "", "self_test": ""}
    root = browsers_root() / m.name
    part = root / f"{m.version}.tar.gz.part"
    partial = root / f"{m.version}.partial"
    try:
        root.mkdir(parents=True, exist_ok=True)
        _remove(part)
        _remove(partial)
        served_by = release.fetch_verified(
            m, platform, part, transport or release.PinnedStream,
            lambda phase, pct: _set(m.name, phase=phase, pct=pct))
        _set(m.name, phase="unpacking", pct=99)
        release.unpack(m, platform, part, partial)
        _commit(m, platform, partial, engine_dir(m.name, m.version))
        prune(m.name, keep=m.version)
        result = _run_self_test(m, self_test)
        return {"ok": True, "code": "", "served_by": served_by, "self_test": result}
    except release.ReleaseError as exc:
        _set(m.name, phase="error", pct=0, error=_SENTENCES[exc.code])
        log.warning("browsers: %s install refused (%s)", m.name, exc.code)
        return {"ok": False, "code": exc.code, "served_by": "", "self_test": ""}
    except Exception as exc:  # a bug must still end in a fixed sentence, never a traceback
        _set(m.name, phase="error", pct=0, error=_SENTENCES["internal"])
        log.warning("browsers: %s install failed (%s)", m.name, exc.__class__.__name__)
        return {"ok": False, "code": "internal", "served_by": "", "self_test": ""}
    finally:
        _remove(part)
        _remove(partial)
        _release(m.name)


def _run_self_test(m: mf.Manifest, self_test: SelfTest | None) -> str:
    """Run the injected (or default) self-test on the committed engine; record the result."""
    assert isinstance(m, mf.Manifest), "manifest required"
    _set(m.name, phase="self-testing", pct=99)
    exe = engine_dir(m.name, m.version) / m.executable
    result = (self_test or _default_self_test)(m, exe)
    assert isinstance(result, str) and result, "self-test returns a result string"
    if result in SELF_TEST_RESULTS:
        _set(m.name, phase="", pct=0, error="", last_self_test=result)
        return result
    _set(m.name, phase="error", pct=0, error=_SENTENCES["self_test"], last_self_test=result)
    log.warning("browsers: %s self-test %s", m.name, result)
    return result


def _default_self_test(m: mf.Manifest, exe: Path) -> str:
    """Run the engine under the walls when one is available; otherwise record the skip."""
    assert exe.name == m.executable, "self-test runs the manifest's executable"
    assert exe.parent.name == m.version, "self-test runs the committed version"
    blocked = walls.check(walls.host_platform(), exe.parent)
    if blocked is not None:
        return "skipped(wall_unavailable)"
    from . import runner  # lazy: runner imports this module

    return runner.probe_version(m, exe.parent)


def _commit(m: mf.Manifest, platform: str, partial: Path, final: Path) -> None:
    """``INSTALLED.json`` into the partial dir, then ONE atomic rename makes it visible."""
    assert partial.is_dir(), "partial dir required"
    meta = {"name": m.name, "version": m.version, "sha256": m.platforms[platform].sha256,
            "installed_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "platform": platform}
    assert set(meta) == _INSTALLED_KEYS, "INSTALLED.json keys are closed"
    target = partial / INSTALLED_NAME
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fh:
        json.dump(meta, fh)
    _remove(final)  # a broken earlier copy of the same version
    _rename(partial, final)


def _remove(path: Path) -> None:
    """Delete a file, link or tree without following links; missing is fine."""
    assert isinstance(path, Path), "path required"
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except FileNotFoundError:
        pass
    assert not path.is_symlink(), "links are always removed"


def prune(name: str, *, keep: str) -> None:
    """Remove every entry under ``browsers/<name>/`` except the ``keep`` version."""
    root = browsers_root() / name
    assert keep, "a version to keep is required"
    assert root.parent.name == "browsers", "prune stays inside browsers/"
    if not root.is_dir():
        return
    entries = sorted(root.iterdir())
    for entry in entries[:_MAX_DIR_ENTRIES]:  # bounded
        if entry.name != keep:
            _remove(entry)


def installed_meta(m: mf.Manifest, platform: str | None) -> dict | None:
    """The committed ``INSTALLED.json`` for the pinned version and this platform, if any
    (no hashing — the cheap check the status surface uses)."""
    assert isinstance(m, mf.Manifest), "manifest required"
    if platform is None or platform not in m.platforms:
        return None
    meta = _read_installed(engine_dir(m.name, m.version))
    want = (m.name, m.version, platform, m.platforms[platform].sha256)
    if meta is None or (meta["name"], meta["version"], meta["platform"], meta["sha256"]) != want:
        return None
    assert set(meta) == _INSTALLED_KEYS, "INSTALLED.json keys are closed"
    return meta


def _read_installed(directory: Path) -> dict | None:
    assert isinstance(directory, Path), "directory required"
    try:
        raw = (directory / INSTALLED_NAME).read_bytes()[:4096]
        meta = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or set(meta) != _INSTALLED_KEYS:
        return None
    assert len(meta) == len(_INSTALLED_KEYS), "closed keys"
    return meta if all(isinstance(v, str) for v in meta.values()) else None


def _sha256_file(path: Path, size: int) -> str:
    assert size >= 0, "size must be non-negative"
    assert isinstance(path, Path), "path required"
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for _ in range(size // _CHUNK + 2):  # fixed bound from the pinned size
            block = fh.read(_CHUNK)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def verify_installed(name: str, *, manifest_obj: mf.Manifest | None = None,
                     platform: str | None = None) -> tuple[bool, str]:
    """Re-hash the installed engine against the manifest — on EVERY launch, no cache.
    Returns ``(True, "")`` or ``(False, reason)``; reason ∈ platform / absent / stale /
    extra_files / modified."""
    m = manifest_obj or mf.load(name)
    assert m.name == name, "manifest must describe this engine"
    plat = platform or platform_key()
    if plat is None or plat not in m.platforms:
        return False, "platform"
    directory = engine_dir(m.name, m.version)
    if installed_meta(m, plat) is None:
        return False, "absent" if _read_installed(directory) is None else "stale"
    members = m.platforms[plat].members
    modes = {f.name: f.mode for f in m.files}
    try:
        present = set(os.listdir(directory)[:_MAX_DIR_ENTRIES])
    except OSError:
        return False, "absent"
    if present != {INSTALLED_NAME} | {mname for mname, _, _ in members}:
        return False, "extra_files"
    for mname, sha, size in members:  # bounded: ≤ 8 members
        if not _member_intact(directory / mname, sha, size, modes[mname]):
            return False, "modified"
    assert members, "a verified install has members"
    return True, ""


def _member_intact(path: Path, sha: str, size: int, mode: int) -> bool:
    """A regular file (never a link) of the pinned size, exec bit as pinned, no
    group/other write, and the pinned sha256."""
    assert len(sha) == 64, "sha256 required"
    assert size > 0, "pinned size required"
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size or info.st_mode & 0o022:
            return False
        if bool(info.st_mode & 0o100) != bool(mode & 0o100):
            return False
        return _sha256_file(path, size) == sha
    except OSError:
        return False


def _other_version_installed(m: mf.Manifest) -> bool:
    root = browsers_root() / m.name
    assert m.version, "version required"
    assert root.parent.name == "browsers", "looks only inside browsers/"
    if not root.is_dir():
        return False
    entries = sorted(root.iterdir())[:_MAX_DIR_ENTRIES]
    return any(e.name != m.version and _read_installed(e) is not None for e in entries)


def status_row(m: mf.Manifest) -> dict:
    """One engine's Status row: {name, phase, pct, version, age_days, eligible, reason,
    sandbox, last_self_test, error}. Cheap: no hashing, no network, readable while locked."""
    assert isinstance(m, mf.Manifest), "manifest required"
    plat = platform_key()
    snap = _snapshot(m.name)
    blocker = "disabled" if disabled() else install_blocker(m, plat)
    directory = engine_dir(m.name, m.version)
    wall = None if blocker else walls.check(walls.host_platform(), directory)
    reason = blocker or (wall.reason if wall is not None else "")
    sandbox = "none" if reason else walls.available_kind(walls.host_platform(), directory)
    if not reason and m.name not in mf.SELECTABLE:
        reason = "tier_disabled"
    phase, pct, error = _phase(m, plat, snap, reason)
    age = max(0, (_dt.date.today() - m.released).days)
    assert phase in PHASES, "phase must come from the closed set"
    return {"name": m.name, "phase": phase, "pct": pct, "version": m.version, "age_days": age,
            "eligible": not reason, "reason": reason, "sandbox": sandbox,
            "last_self_test": snap["last_self_test"], "error": error}


def _phase(m: mf.Manifest, plat: str | None, snap: dict, reason: str) -> tuple[str, int, str]:
    assert isinstance(snap, dict), "snapshot required"
    assert isinstance(reason, str), "reason must be a string"
    if reason == "disabled":
        return "disabled", 0, ""
    if snap["phase"] in TRANSIENT:
        return snap["phase"], int(snap["pct"]), ""
    if snap["phase"] == "error":
        return "error", 0, str(snap["error"])
    if reason and reason != "tier_disabled":
        return "unavailable", 0, ""
    if installed_meta(m, plat) is not None:
        return "ready", 100, ""
    if _other_version_installed(m):
        return "stale", 0, ""
    return "absent", 0, ""


def status() -> list[dict]:
    """Every shipped engine's Status row (sorted by name)."""
    rows = [status_row(m) for m in mf.load_all()]
    assert all(r["phase"] in PHASES for r in rows), "phases are closed"
    assert len({r["name"] for r in rows}) == len(rows), "one row per engine"
    return rows
