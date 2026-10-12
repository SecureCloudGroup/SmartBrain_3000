"""Confinement walls for browser components (ni-format §35; P3 design §2).

The engine has no sandbox of its own: the OS wall around the whole engine process tree is
the ONLY wall, and a missing or failed wall means ``unavailable`` — never a render.

Interface: ``prepare(platform, run_dir, engine_dir, egress_endpoint) -> Wall | Unavailable``
is called AFTER the egress child is up, so the endpoint carries its live port (macOS) or
socket path (Linux/Docker); ``Wall.wrap(argv, env) -> (argv, env)`` may prepend a wrapper
command (``sandbox-exec -f <profile> -D ENGINE_DIR=… -D RUN_DIR=… -D PROXY_PORT=…``);
``Wall.verify(pid_tree)`` runs on every watchdog poll (every PID must be confined);
``Wall.teardown()``. ``expects_children`` is False for every wall: the engine spawns no
process for ``fetch`` (measured), so a child in the engine's group is itself a confinement
failure. ``check(platform, engine_dir)`` is the spawn-free pre-check the runner and the
Status surface use, so an unavailable wall never costs a process.

This step ships NO real wall. Every platform reports ``Unavailable`` with its reason code:
darwin ``sandbox_pending`` (deny-default Seatbelt), linux ``userns_pending``
(unshare USER|NET|NS|PID + relay), docker ``docker_no_sidecar`` (renderer sidecar),
windows ``platform``. The walls land with the measured profiles of the B0 spike (B2).

``NullWall`` exists for the test suite only: it is built ONLY when
``SMARTBRAIN_BROWSER_TEST_NULLWALL=1`` AND every executable in the engine directory is the
fake engine fixture (a Python script carrying ``FAKE_ENGINE_MARKER``) — both checked, both
asserted. A real engine binary can never run under it.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import runtime

NULLWALL_ENV = "SMARTBRAIN_BROWSER_TEST_NULLWALL"
FAKE_ENGINE_MARKER = b"# smartbrain-test-fake-engine"
REASONS = frozenset({"platform", "sandbox_pending", "userns_pending", "docker_no_sidecar",
                     "apparmor_userns", "missing_libs", "sandbox"})
# The wall each platform will get, and why it is unavailable until it does.
_PENDING = {"darwin": "sandbox_pending", "linux": "userns_pending",
            "docker": "docker_no_sidecar", "windows": "platform", "other": "platform"}
# Which egress endpoint each platform's wall consumes: macOS allows one loopback port in
# its profile; the Linux/Docker walls bind-mount a Unix socket and relay inside the netns.
_ENDPOINT_KIND = {"darwin": "tcp", "linux": "unix", "docker": "unix", "windows": "tcp",
                  "other": "tcp"}
_MAX_ENGINE_ENTRIES = 16
_MARKER_WINDOW = 256


@dataclass(frozen=True)
class Unavailable:
    """No wall here; ``reason`` is a closed code the Status surface shows."""

    reason: str

    def __post_init__(self) -> None:
        assert self.reason in REASONS, f"unknown wall reason: {self.reason}"
        assert isinstance(self.reason, str), "reason must be a string"


class Wall:
    """One render's confinement. Subclasses implement all three methods."""

    kind = "none"
    endpoint_kind = "tcp"
    expects_children = False

    def wrap(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError

    def verify(self, pid_tree: list[int]) -> bool:
        raise NotImplementedError

    def teardown(self) -> None:
        raise NotImplementedError


class NullWall(Wall):
    """TEST ONLY: confines nothing. The fake engine dials the egress port directly."""

    kind = "null"
    endpoint_kind = "tcp"
    expects_children = False

    def __init__(self, engine_dir: Path) -> None:
        assert os.environ.get(NULLWALL_ENV) == "1", "NullWall needs the test env flag"
        assert is_fake_engine(engine_dir), "NullWall only ever runs the fake engine"
        self._engine_dir = engine_dir

    def wrap(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        assert argv and argv[0].startswith(str(self._engine_dir)), "argv runs the engine"
        assert isinstance(env, dict), "env must be a dict"
        return list(argv), dict(env)

    def verify(self, pid_tree: list[int]) -> bool:
        assert isinstance(pid_tree, list), "pid tree must be a list"
        assert all(isinstance(p, int) for p in pid_tree), "pids are ints"
        return all(p > 1 for p in pid_tree)

    def teardown(self) -> None:
        assert self._engine_dir is not None, "wall was prepared"
        assert self.kind == "null", "only the null wall tears down this way"


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
    assert family in _PENDING, "platform family is closed"
    return family


def is_fake_engine(engine_dir: Path | None) -> bool:
    """Every executable regular file in ``engine_dir`` is the fake engine fixture (a
    ``#!`` script carrying the marker near its top), and there is at least one."""
    assert _MARKER_WINDOW >= len(FAKE_ENGINE_MARKER) + 2, "the marker fits the window"
    if engine_dir is None or not Path(engine_dir).is_dir():
        return False
    found = 0
    entries = sorted(Path(engine_dir).iterdir())[:_MAX_ENGINE_ENTRIES]
    for entry in entries:  # bounded
        try:
            info = os.lstat(entry)
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o100:
                continue
            with open(entry, "rb") as fh:
                head = fh.read(_MARKER_WINDOW)
        except OSError:
            return False
        if not head.startswith(b"#!") or FAKE_ENGINE_MARKER not in head:
            return False
        found += 1
    assert found >= 0, "count is non-negative"
    return found > 0


def _null_allowed(engine_dir: Path | None) -> bool:
    assert NULLWALL_ENV.startswith("SMARTBRAIN_"), "the test flag is a SmartBrain variable"
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    return os.environ.get(NULLWALL_ENV) == "1" and is_fake_engine(engine_dir)


def check(platform: str, engine_dir: Path | None) -> Unavailable | None:
    """Spawn-free pre-check: ``None`` when ``prepare`` can build a wall, else the reason."""
    assert platform in _PENDING, f"unknown platform family: {platform}"
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    if _null_allowed(engine_dir):
        return None
    return Unavailable(_PENDING[platform])


def available_kind(platform: str, engine_dir: Path | None) -> str:
    """The kind of wall ``prepare`` would build here (``null`` in tests), else ``none``."""
    assert platform in _PENDING, f"unknown platform family: {platform}"
    assert NullWall.kind == "null", "the only wall in this step is the test wall"
    return NullWall.kind if check(platform, engine_dir) is None else "none"


def endpoint_kind(platform: str, engine_dir: Path | None) -> str:
    """The egress endpoint the wall for this run consumes (``tcp`` | ``unix``)."""
    assert platform in _ENDPOINT_KIND, f"unknown platform family: {platform}"
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    if _null_allowed(engine_dir):
        return NullWall.endpoint_kind
    return _ENDPOINT_KIND[platform]


def prepare(platform: str, run_dir: str, engine_dir: str,
            egress_endpoint: dict | None) -> Wall | Unavailable:
    """Build this run's wall around ``engine_dir`` with ``egress_endpoint`` as the only
    network route (None = no network at all, e.g. the version probe)."""
    assert platform in _PENDING, f"unknown platform family: {platform}"
    assert run_dir and os.path.isdir(run_dir), "run dir must exist"
    assert egress_endpoint is None or egress_endpoint.get("kind") in ("tcp", "unix"), \
        "egress endpoint must be tcp or unix"
    blocked = check(platform, Path(engine_dir))
    if blocked is not None:
        return blocked
    return NullWall(Path(engine_dir))
