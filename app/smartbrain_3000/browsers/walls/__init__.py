"""Confinement walls for browser components (ni-format §35; P3 design §2).

The engine has no sandbox of its own: the OS wall around the whole engine process tree is
the ONLY wall, and a missing or failed wall means ``unavailable`` — never a render.

- ``darwin`` — Seatbelt: ``sandbox-exec`` with the B0 spike's five-rule deny-default
  profile (``obscura.sb``, hash-pinned); ``sandbox_check`` on every PID.
- ``linux`` — unshare USER|NS|NET|PID through ``userns_boot.py``: tmpfs masks, the engine
  dir read-only and the run dir read-write bound back, a fresh /proc, a relay from
  127.0.0.1:3128 to the egress child's Unix socket; namespace ids checked on every PID.
- ``docker`` — the renderer sidecar (``network_mode: none``, read-only, no capabilities, no
  app data); the app hands it the job over the shared ``/render`` volume (``remote``).
- ``null`` — the test-only NullWall (flag + fake engine fixture, both checked).
- windows/other — ``platform``.

Interface: ``check(platform, engine_dir) -> Unavailable | None`` (the pre-check the runner
and the Status surface use; the darwin/linux probes spawn once per process and are cached);
``prepare(platform, run_dir, engine_dir, egress_endpoint) -> Wall | Unavailable`` after the
egress child is up; ``Wall.proxy_port/wrap/verify/run/teardown``; ``self_test`` runs the
wall's canary program; ``remote``/``run_parent`` say where the engine runs and where its
run dir must live. The fake engine fixture never runs under a local real wall, and a real
engine never runs under the NullWall (the sidecar re-hashes its own engine per job).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ... import __version__, runtime
from .. import manifest as mf
from . import darwin, docker, linux, null
from .base import (
    FAKE_ENGINE_MARKER,
    KINDS,
    NULLWALL_ENV,
    REASONS,
    Unavailable,
    Wall,
    is_fake_engine,
    null_allowed,
)
from .null import NullWall

__all__ = ["FAKE_ENGINE_MARKER", "KINDS", "NULLWALL_ENV", "REASONS", "NullWall", "Unavailable",
           "Wall", "available_kind", "check", "endpoint_kind", "host_platform", "is_fake_engine",
           "prepare", "remote", "remote_engine_ready", "run_parent", "self_test", "sidecar_state"]

_KIND = {"darwin": "seatbelt", "linux": "userns", "docker": "sidecar"}
_ENDPOINT_KIND = {"darwin": "tcp", "linux": "unix", "docker": "unix", "windows": "tcp",
                  "other": "tcp"}


def host_platform() -> str:
    """darwin | linux | docker | windows | other — the wall family for this process."""
    assert isinstance(sys.platform, str), "platform must be a string"
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    if runtime.in_container():
        return "docker"
    if sys.platform == "darwin":
        return "darwin"
    family = "linux" if sys.platform.startswith("linux") else "other"
    assert family in _ENDPOINT_KIND, "platform family is closed"
    return family


def sidecar_state() -> str:
    """``n/a`` outside Docker; in Docker the renderer sidecar's READY facts against this app's
    build: ``missing`` | ``ready`` | ``stale`` (B3a — the launcher's start / rebuild signal)."""
    family = host_platform()
    assert family in _ENDPOINT_KIND, "a platform family"
    if family != "docker":
        return "n/a"
    state = docker.sidecar_state(__version__)
    assert state in docker.SIDECAR_STATES, "a closed sidecar state"
    return state


def remote(platform: str) -> bool:
    """The engine runs in the Docker sidecar, not in this process's container (the
    test-only NullWall keeps the fake engine local)."""
    assert platform in _ENDPOINT_KIND, f"unknown platform family: {platform}"
    assert NULLWALL_ENV.startswith("SMARTBRAIN_"), "the test flag is a SmartBrain variable"
    return platform == "docker" and os.environ.get(NULLWALL_ENV) != "1"


def check(platform: str, engine_dir: Path | None) -> Unavailable | None:
    """``None`` when ``prepare`` can build a wall here, else the closed reason."""
    assert platform in _ENDPOINT_KIND, f"unknown platform family: {platform}"
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    if null_allowed(engine_dir):
        return None
    if platform != "docker" and is_fake_engine(engine_dir):
        return Unavailable("sandbox")  # the test fixture never runs under a local real wall
    if platform == "darwin":
        return darwin.check(engine_dir)
    if platform == "linux":
        return linux.check(engine_dir)
    return docker.check() if platform == "docker" else Unavailable("platform")


def available_kind(platform: str, engine_dir: Path | None) -> str:
    """The kind of wall ``prepare`` would build here (``null`` in tests), else ``none``."""
    assert platform in _ENDPOINT_KIND, f"unknown platform family: {platform}"
    if check(platform, engine_dir) is not None:
        return "none"
    kind = "null" if null_allowed(engine_dir) else _KIND[platform]
    assert kind in KINDS, "kinds are closed"
    return kind


def endpoint_kind(platform: str, engine_dir: Path | None) -> str:
    """The egress endpoint the wall for this run consumes (``tcp`` | ``unix``)."""
    assert platform in _ENDPOINT_KIND, f"unknown platform family: {platform}"
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    return NullWall.endpoint_kind if null_allowed(engine_dir) else _ENDPOINT_KIND[platform]


def run_parent(platform: str) -> str | None:
    """Where a run dir must live (the sidecar's shared volume); None = the system temp."""
    assert platform in _ENDPOINT_KIND, f"unknown platform family: {platform}"
    parent = docker.run_parent() if remote(platform) else None
    assert parent is None or os.path.isabs(parent), "an absolute parent"
    return parent


def prepare(platform: str, run_dir: str, engine_dir: str,
            egress_endpoint: dict | None) -> Wall | Unavailable:
    """Build this run's wall around ``engine_dir`` with ``egress_endpoint`` as the only
    network route (None = no network at all: only the test wall's version probe)."""
    assert platform in _ENDPOINT_KIND, f"unknown platform family: {platform}"
    assert run_dir and os.path.isdir(run_dir), "run dir must exist"
    assert egress_endpoint is None or egress_endpoint.get("kind") in ("tcp", "unix"), \
        "egress endpoint must be tcp or unix"
    blocked = check(platform, Path(engine_dir))
    if blocked is not None:
        return blocked
    if null_allowed(Path(engine_dir)):
        return NullWall(Path(engine_dir))
    if egress_endpoint is None or egress_endpoint["kind"] != _ENDPOINT_KIND[platform]:
        return Unavailable("sandbox")
    try:
        if platform == "darwin":
            return darwin.SeatbeltWall(engine_dir, run_dir, egress_endpoint["port"])
        if platform == "linux":
            return linux.UsernsWall(engine_dir, run_dir, egress_endpoint["path"])
        return docker.SidecarWall(run_dir, egress_endpoint)
    except (ValueError, KeyError, OSError):
        return Unavailable("sandbox")


def remote_engine_ready(m: mf.Manifest, platform_key: str | None) -> bool:
    """A remote wall holds exactly this manifest's pinned build (the sidecar's READY)."""
    assert isinstance(m, mf.Manifest), "manifest required"
    ready = docker.engine_ready(m, platform_key)
    assert isinstance(ready, bool), "a flag"
    return ready


def self_test(platform: str, m: mf.Manifest, engine_dir: Path,
              home: Path) -> tuple[str, list[dict]]:
    """The wall's canary program: ``ok`` | ``failed:<row>`` | ``skipped(wall_unavailable)``,
    with the rows (``{row, ok, detail}``) for the SG harness. ``home`` is a directory in the
    user's data area where a canary file is placed (the confined side must not read it)."""
    assert platform in _ENDPOINT_KIND, f"unknown platform family: {platform}"
    assert isinstance(engine_dir, Path) and isinstance(home, Path), "paths required"
    if null_allowed(engine_dir):
        return null.self_test(m, engine_dir)
    if check(platform, engine_dir) is not None:
        return "skipped(wall_unavailable)", []
    if platform == "darwin":
        return darwin.self_test(m, engine_dir, home)
    if platform == "linux":
        return linux.self_test(m, engine_dir, home)
    return docker.self_test(m, engine_dir)
