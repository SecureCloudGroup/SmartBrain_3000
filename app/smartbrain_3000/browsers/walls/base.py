"""Shared pieces of the confinement walls (ni-format §35; P3 design §2): the ``Wall`` and
``Unavailable`` types, the closed reason and kind sets, the path check every wall applies
to its parameters, the fake-engine marker check, and the canary listeners the self-tests
count hits on. The platform modules import from here; ``walls/__init__`` re-exports.
"""

from __future__ import annotations

import os
import secrets
import socket
import stat
import string
import threading
from dataclasses import dataclass
from pathlib import Path

NULLWALL_ENV = "SMARTBRAIN_BROWSER_TEST_NULLWALL"
FAKE_ENGINE_MARKER = b"# smartbrain-test-fake-engine"
# Why no wall can be built here — a closed set the Status surface shows.
REASONS = frozenset({"platform", "docker_no_sidecar", "apparmor_userns", "userns_blocked",
                     "missing_libs", "sandbox"})
KINDS = frozenset({"none", "null", "seatbelt", "userns", "sidecar"})
FAMILIES = ("darwin", "linux", "docker", "windows", "other")
# Characters a wall parameter (engine dir, run dir) may carry. Seatbelt parameters are
# strings, never SBPL source, but the set stays closed anyway; the space is in it because
# the macOS data dir lives under "Application Support".
SAFE_PATH_CHARS = frozenset(string.ascii_letters + string.digits + "/._-+@~, ")
MAX_PATH = 1024
MAX_ROWS = 32
_MAX_ENGINE_ENTRIES = 16
_MARKER_WINDOW = 256
_MAX_HITS = 1000


@dataclass(frozen=True)
class Unavailable:
    """No wall here; ``reason`` is a closed code the Status surface shows."""

    reason: str

    def __post_init__(self) -> None:
        assert self.reason in REASONS, f"unknown wall reason: {self.reason}"
        assert isinstance(self.reason, str), "reason must be a string"


class Wall:
    """One render's confinement.

    ``wrap(argv, env)`` may prepend a wrapper command; ``verify(pids)`` runs on every
    watchdog poll (every PID must be confined); ``teardown()`` after the run. ``helpers``
    counts the processes the wall itself adds to the engine's process group (the Linux
    bootstrap and namespace init), so they are never mistaken for engine children.
    ``remote`` walls run the engine elsewhere (the Docker sidecar): the runner calls
    ``run(...)`` instead of spawning, and ``wrap`` changes nothing."""

    kind = "none"
    endpoint_kind = "tcp"
    expects_children = False
    helpers = 0
    remote = False

    def proxy_port(self, endpoint: dict) -> int:
        """The loopback port the engine's ``--proxy`` dials: the egress child's own port
        for a TCP endpoint; walls that relay to a Unix socket override this."""
        assert endpoint.get("kind") == "tcp", "a relay wall must override proxy_port"
        port = endpoint["port"]
        assert isinstance(port, int) and 1 <= port <= 65535, "a live egress port"
        return port

    def wrap(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError

    def verify(self, pid_tree: list[int]) -> bool:
        raise NotImplementedError

    def run(self, argv: list[str], env: dict[str, str], *, out_path: str, cap: int,
            deadline_s: float, rss_mb: int, cpu_s: int, data_mb: int) -> dict:
        raise NotImplementedError

    def teardown(self) -> None:
        raise NotImplementedError


def checked_dir(path: str | os.PathLike[str]) -> str:
    """The canonical (symlink-free) form of a wall parameter path: absolute, at most
    ``MAX_PATH`` long, every character from ``SAFE_PATH_CHARS``. Raises ``ValueError``.
    Seatbelt matches the kernel's resolved path, so ``/var/…`` must become
    ``/private/var/…`` before it reaches a profile parameter."""
    raw = os.fspath(path)
    assert isinstance(raw, str), "path must be text"
    if not os.path.isabs(raw):
        raise ValueError("unsafe_path")
    real = os.path.realpath(raw)
    if not os.path.isabs(real) or not 1 < len(real) <= MAX_PATH:
        raise ValueError("unsafe_path")
    if set(real) - SAFE_PATH_CHARS or "\x00" in real or "//" in real:
        raise ValueError("unsafe_path")
    assert real.startswith("/"), "canonical paths are absolute"
    return real


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


def null_allowed(engine_dir: Path | None) -> bool:
    """The test-only NullWall: the env flag AND an engine dir holding only the fake."""
    assert NULLWALL_ENV.startswith("SMARTBRAIN_"), "the test flag is a SmartBrain variable"
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    return os.environ.get(NULLWALL_ENV) == "1" and is_fake_engine(engine_dir)


def row(name: str, ok: bool, detail: str = "") -> dict:
    """One self-test row: ``{row, ok, detail}`` (closed keys, short host-free detail)."""
    assert name and name.replace("_", "").isalnum(), "row names are identifiers"
    assert isinstance(ok, bool), "ok is a flag"
    return {"row": name, "ok": ok, "detail": str(detail)[:120]}


def verdict(rows: list[dict], required: tuple[str, ...]) -> str:
    """``ok`` when every required row ran and passed; ``failed:<row>`` names the first
    failing row (or a required row that never ran — a vacuous pass is a failure)."""
    assert len(rows) <= MAX_ROWS, "rows are bounded"
    assert required, "a self-test names its required rows"
    for item in rows:  # bounded
        if not item["ok"]:
            return f"failed:{item['row']}"
    ran = {item["row"] for item in rows}
    missing = [name for name in required if name not in ran]
    return f"failed:{missing[0]}" if missing else "ok"


def lan_ip() -> str | None:
    """This machine's primary non-loopback IPv4 (a UDP connect sends no packet), or None."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    assert probe.type == socket.SOCK_DGRAM, "a UDP probe sends nothing on connect"
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routing lookup only
        addr = str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()
    assert isinstance(addr, str), "an address string"
    return None if addr.startswith(("127.", "0.")) else addr


class Canaries:
    """The self-tests' own listeners on spare loopback ports: TCP on 127.0.0.1 and ::1
    (where IPv6 exists) and UDP on 127.0.0.1. Every accepted connection or received
    datagram is a hit; a wall that holds leaves ``hits`` empty. The LAN address is only
    named (``lan``), never listened on: binding it would raise the macOS firewall prompt,
    and the wall's refusal of the connect itself is the proof."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.ports: dict[str, int] = {}
        self.lan = lan_ip()
        self._socks: list[socket.socket] = []
        self._lock = threading.Lock()
        for name, family, host, kind in (("tcp4", socket.AF_INET, "127.0.0.1", socket.SOCK_STREAM),
                                         ("tcp6", socket.AF_INET6, "::1", socket.SOCK_STREAM),
                                         ("udp4", socket.AF_INET, "127.0.0.1", socket.SOCK_DGRAM)):
            self._open(name, family, host, kind)
        assert "tcp4" in self.ports and "udp4" in self.ports, "the loopback listeners exist"
        assert len(self._socks) <= 3, "at most three listeners"

    def _open(self, name: str, family: int, host: str, kind: int) -> None:
        assert name not in self.ports, "one listener per name"
        assert kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM), "tcp or udp"
        sock = socket.socket(family, kind)
        try:
            sock.bind((host, 0))
            if kind == socket.SOCK_STREAM:
                sock.listen(16)
        except OSError:
            sock.close()
            return
        self.ports[name] = int(sock.getsockname()[1])
        self._socks.append(sock)
        threading.Thread(target=self._serve, args=(name, sock, kind), daemon=True).start()

    def _serve(self, name: str, sock: socket.socket, kind: int) -> None:
        assert kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM), "tcp or udp"
        assert name, "listener name required"
        for _ in range(_MAX_HITS):  # bounded: a hit count past this fails the row anyway
            try:
                if kind == socket.SOCK_STREAM:
                    conn, _addr = sock.accept()
                    conn.close()
                else:
                    sock.recvfrom(2048)
            except OSError:
                return  # closed
            with self._lock:
                self.hits.append(name)

    def hit_count(self) -> int:
        with self._lock:
            count = len(self.hits)
        assert count >= 0, "a count"
        return count

    def close(self) -> None:
        assert len(self._socks) <= 3, "at most three listeners"
        for sock in self._socks:  # bounded: ≤ 3
            try:
                sock.close()
            except OSError:
                pass
        assert all(s.fileno() == -1 for s in self._socks), "every listener closed"


def canary_file(directory: Path) -> tuple[Path, bytes]:
    """A random-content canary file in ``directory`` (the user's data dir): a wall must
    keep the confined side from reading it. The caller removes it."""
    assert isinstance(directory, Path), "directory required"
    directory.mkdir(parents=True, exist_ok=True)
    token = ("SMARTBRAIN-CANARY-" + secrets.token_hex(12)).encode()
    path = directory / f".selftest-canary-{secrets.token_hex(6)}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(token + b"\n")
    assert path.is_file(), "the canary exists"
    return path, token
