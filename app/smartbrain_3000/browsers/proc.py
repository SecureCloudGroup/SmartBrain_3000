"""Process hygiene for browser engines (ni-format §35): spawn, watch, kill, read.

- ``spawn``: own session (so the whole tree is one process group), stdin from devnull,
  stdout to devnull (the page goes to the run-dir output file), stderr to a pipe that
  ``StderrTap`` drains, cwd = the run dir, the manifest's closed env. On POSIX a
  pre-exec step sets RLIMIT_CPU, RLIMIT_CORE=0, RLIMIT_NOFILE and RLIMIT_NPROC (headroom
  over this user's current count), RLIMIT_DATA only when the manifest turns it on (V8
  reserves ~450 GB of address space, so a data/address-space cap is off by default —
  measured), and PR_SET_PDEATHSIG on Linux. Everything the pre-exec step needs is imported
  and computed in the parent; the child only calls ``setrlimit``/``prctl``.
- ``watch``: every 250 ms — exit, the deadline, the tree's RSS against ``rss_mb``, a child
  process (no wall expects one: the engine spawns nothing for ``fetch``), and
  ``Wall.verify`` on the live PID tree.
- ``kill_tree``: SIGKILL the group, reap, and prove no live member is left (orphans
  included; zombies awaiting another reaper do not count).
- ``StderrTap``: the only reader of the engine's stderr. It keeps the first 4 KB and up to
  64 marker lines (``Page loaded:``, ``Failed to navigate``, ``V8 heap limit reached``,
  ``V8 watchdog fired``, ``hard timeout exceeded``) with ANSI colour stripped. The text is
  used to classify a run; it is never shown to a user and never logged.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from .. import jailrun
from . import walls

log = logging.getLogger("smartbrain.browsers")

POLL_S = 0.25
_HEAD_CAP = 4096
_LINE_CAP = 2048
_MAX_SIGNAL_LINES = 64
_MARKERS = ("page loaded:", "failed to navigate", "v8 heap limit reached", "v8 watchdog fired",
            "hard timeout exceeded")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MAX_PROC_ENTRIES = 100_000
_NOFILE = 1024
_NPROC_HEADROOM = 256
_KILL_ROUNDS = 40


def _user_tasks() -> int | None:
    """Tasks (Linux: threads) or processes (macOS) this user runs now; None if unknown."""
    assert _NPROC_HEADROOM > 0, "headroom must be positive"
    uid = os.getuid()
    assert uid >= 0, "uid is non-negative"
    if os.path.isdir("/proc/self"):
        total = 0
        for name in os.listdir("/proc")[:_MAX_PROC_ENTRIES]:  # bounded
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/status", "rb") as fh:
                    fields = dict(line.split(b":", 1) for line in fh.read(8192).splitlines()
                                  if b":" in line)
                if int(fields[b"Uid"].split()[0]) == uid:
                    total += int(fields[b"Threads"].strip())
            except (OSError, ValueError, KeyError, IndexError):
                continue
        return total
    try:
        res = subprocess.run(["ps", "-U", str(uid), "-o", "pid="], capture_output=True,
                             timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return len(res.stdout.splitlines()) if res.returncode == 0 else None


def _preexec(cpu_s: int, data_mb: int) -> Callable[[], None]:
    """Prepare the child-side limit setter (imports + values + libc handle in the parent)."""
    import ctypes
    import resource  # POSIX-only stdlib — never at module top (v0.9.25 Windows lesson)

    assert cpu_s > 0 and data_mb >= 0, "limits must be sane"
    wanted = [(resource.RLIMIT_CPU, cpu_s), (resource.RLIMIT_CORE, 0),
              (resource.RLIMIT_NOFILE, _NOFILE)]
    tasks = _user_tasks()
    if tasks is not None:
        wanted.append((resource.RLIMIT_NPROC, tasks + _NPROC_HEADROOM))
    if data_mb:
        wanted.append((resource.RLIMIT_DATA, data_mb * 1024 * 1024))
    limits = []
    for which, value in wanted:  # bounded: ≤ 5
        hard = resource.getrlimit(which)[1]
        limits.append((which, value if hard == resource.RLIM_INFINITY else min(value, hard)))
    prctl = None
    if sys.platform.startswith("linux"):
        prctl = getattr(ctypes.CDLL(None, use_errno=True), "prctl", None)
    assert len(limits) == len(wanted), "every wanted limit prepared"

    def child() -> None:
        assert limits, "limits prepared in the parent"
        assert all(value >= 0 for _, value in limits), "limits are non-negative"
        for which, value in limits:
            try:
                resource.setrlimit(which, (value, value))
            except (OSError, ValueError):
                pass
        if prctl is not None:
            prctl(1, int(signal.SIGKILL))  # PR_SET_PDEATHSIG: die with the spawning thread

    return child


def spawn(argv: list[str], env: dict[str, str], run_dir: str, *, cpu_s: int,
          data_mb: int) -> subprocess.Popen:
    """Start the (wall-wrapped) engine in its own session; raises OSError/ValueError."""
    assert argv and os.path.isabs(argv[0]), "argv must start with an absolute executable"
    assert os.path.isdir(run_dir), "run dir must exist"
    preexec = _preexec(cpu_s, data_mb) if os.name == "posix" else None
    # The child-side callable only calls setrlimit/prctl on values (and a libc handle)
    # prepared here in the parent: no import, no lock, no allocation-heavy Python after fork.
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, cwd=run_dir, env=env, close_fds=True,
                            start_new_session=True,
                            preexec_fn=preexec)  # noqa: PLW1509 - limits only, see above


class StderrTap(threading.Thread):
    """Drain stderr (bounded memory) and keep what classification needs."""

    def __init__(self, proc: subprocess.Popen) -> None:
        super().__init__(name="render-stderr", daemon=True)
        assert proc.stderr is not None, "stderr pipe required"
        assert proc.pid > 0, "a started process"
        self._stream = proc.stderr
        self._head = bytearray()
        self._signals: list[str] = []

    def run(self) -> None:
        assert self._stream is not None, "stderr stream required"
        fd = self._stream.fileno()
        assert fd >= 0, "a valid descriptor"
        partial = b""
        for _ in range(1 << 20):  # bounded: 4 KB reads
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            room = _HEAD_CAP - len(self._head)
            if room > 0:
                self._head += chunk[:room]
            *lines, partial = (partial + chunk).split(b"\n")
            partial = partial[:_LINE_CAP]
            for line in lines:  # bounded by the chunk size
                self._consider(line)
        self._consider(partial)
        try:
            self._stream.close()
        except OSError:
            pass

    def _consider(self, raw: bytes) -> None:
        assert isinstance(raw, bytes), "raw line must be bytes"
        assert len(self._signals) <= _MAX_SIGNAL_LINES, "signal lines are capped"
        if len(self._signals) >= _MAX_SIGNAL_LINES or not raw:
            return
        text = _ANSI_RE.sub("", raw[:_LINE_CAP].decode("utf-8", "replace")).strip()
        if any(marker in text.lower() for marker in _MARKERS):
            self._signals.append(text)

    def signals(self) -> list[str]:
        assert len(self._signals) <= _MAX_SIGNAL_LINES, "signal lines are capped"
        assert all(isinstance(s, str) for s in self._signals), "lines are text"
        return list(self._signals)


def watch(proc: subprocess.Popen, wall: walls.Wall, rss_mb: int, deadline_s: float) -> str:
    """Poll until something decides: ``exited`` · ``timeout`` · ``memory`` · ``wall`` ·
    ``children``. The caller kills the group whatever the verdict."""
    assert rss_mb > 0 and deadline_s > 0, "limits required"
    assert proc.pid > 1, "a real child is required"
    deadline = time.monotonic() + deadline_s
    for _ in range(int(deadline_s / POLL_S) + 2):  # fixed bound
        try:
            proc.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            pass
        else:  # the engine is reaped: any member left in its group is a child it spawned
            return "children" if group_members(proc.pid) and not wall.expects_children else "exited"
        tree = group_members(proc.pid)
        if len(tree) > 1 and not wall.expects_children:
            return "children"
        if tree and not wall.verify([pid for pid, _ in tree]):
            return "wall"
        if sum(rss for _, rss in tree) > rss_mb * 1024:
            return "memory"
        if time.monotonic() >= deadline:
            return "timeout"
    return "timeout"


def group_members(pgid: int) -> list[tuple[int, int]]:
    """Live (non-zombie) members of process group ``pgid`` as ``(pid, rss_kb)``."""
    assert pgid > 1, "a real process group is required"
    if os.path.isdir("/proc/self"):
        return _proc_members(pgid)
    try:
        res = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,rss=,stat="], capture_output=True,
                             timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: list[tuple[int, int]] = []
    for line in res.stdout.decode("ascii", "replace").splitlines()[:_MAX_PROC_ENTRIES]:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == str(pgid) and not parts[3].startswith("Z"):
            out.append((int(parts[0]), int(parts[2])))
    assert all(pid > 0 for pid, _ in out), "pids are positive"
    return out


def _proc_members(pgid: int) -> list[tuple[int, int]]:
    assert pgid > 1, "a real process group is required"
    page_kb = max(1, os.sysconf("SC_PAGE_SIZE") // 1024)
    assert page_kb >= 1, "page size known"
    out: list[tuple[int, int]] = []
    for name in os.listdir("/proc")[:_MAX_PROC_ENTRIES]:  # bounded
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                fields = fh.read(2048).decode("ascii", "replace").rsplit(")", 1)[1].split()
            if fields[0] == "Z" or int(fields[2]) != pgid:
                continue
            with open(f"/proc/{name}/statm", "rb") as fh:
                rss_pages = int(fh.read(256).split()[1])
        except (OSError, ValueError, IndexError):
            continue
        out.append((int(name), rss_pages * page_kb))
    return out


def kill_tree(proc: subprocess.Popen) -> bool:
    """SIGKILL the engine's whole process group, reap the child, and prove no live member
    is left. Returns False — and logs — if any survived."""
    assert proc is not None, "process required"
    assert proc.pid > 1, "a real child is required"
    for _ in range(_KILL_ROUNDS):  # bounded: ~2 s of 50 ms rounds
        jailrun._kill_group(proc)  # pgid == pid via start_new_session
        if proc.poll() is None:
            jailrun._reap(proc)
        if not group_members(proc.pid):
            return True
        threading.Event().wait(0.05)
    log.warning("browsers: engine processes survived the group kill")
    return False


def read_capped_file(path: str, cap: int) -> tuple[bytes | None, bool]:
    """``(bytes, False)``; ``(None, True)`` over the cap; ``(None, False)`` when missing or
    not a regular file. Never follows a link."""
    assert cap > 0, "cap must be positive"
    assert os.path.isabs(path), "absolute path required"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None, False
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None, False
        if info.st_size > cap:
            return None, True
        data = fh.read(cap + 1)
    return (None, True) if len(data) > cap else (data, False)
