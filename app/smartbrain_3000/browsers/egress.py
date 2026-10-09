"""Parent side of the egress child (ni-format §35): spawn it under ``jailrun``'s process
hygiene, hand it one closed-key config line, read its endpoint, and on ``stop()`` close
its stdin (the lifeline) and read back the census.

The child's environment is ``jailrun._jail_env()`` — minimal PATH + PYTHONPATH, no
``SMARTBRAIN_*`` and no proxy variable — in its own session with stderr to devnull; it
never sees the vault key or the app's environment. Every read of its stdout is bounded in
size and time, and every line must carry exactly the expected keys.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .. import jailrun
from . import jail_egress

_BOOTSTRAP = "from smartbrain_3000.browsers.jail_egress import main; main()"
_LINE_CAP = 64 * 1024
_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 5.0
_EXIT_GRACE_S = 2.0  # after the census line the child only has os._exit(0) left to do
_ROW_KEYS = frozenset({"site", "tunnels", "up", "down", "verdict"})


class EgressError(Exception):
    """The egress child failed its contract; ``reason`` is a short host-free class."""

    def __init__(self, reason: str) -> None:
        assert reason, "reason required"
        assert isinstance(reason, str), "reason must be a string"
        super().__init__(reason)
        self.reason = reason


@dataclass
class Egress:
    """A running egress child for one render."""

    proc: subprocess.Popen
    endpoint: dict
    _stopped: bool = field(default=False, repr=False)

    def stop(self) -> tuple[list[dict], int]:
        """Close the lifeline, read the census line, reap. Returns ``(rows, dropped)``.
        Raises ``EgressError`` when the line is missing or malformed. A child that has
        written its census exits by itself: it gets a short grace before the group kill."""
        assert self.proc is not None, "process required"
        assert not self._stopped, "stop once"
        self._stopped = True
        reported = False
        try:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            except OSError:
                pass
            line = _read_line(self.proc, _STOP_TIMEOUT_S)
            reported = True
        finally:
            if reported:
                try:
                    self.proc.wait(timeout=_EXIT_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
            if self.proc.poll() is None:
                jailrun._kill_group(self.proc)
            jailrun._reap(self.proc)
        return parse_census(line)


def start(run_dir: str, *, kind: str, policy: str, sites: list[str], main_host: str,
          main_port: int, max_life_s: float, caps: dict | None = None) -> Egress:
    """Spawn the egress child for one render and wait (bounded) for its endpoint."""
    assert os.path.isdir(run_dir), "run dir must exist"
    assert kind in ("tcp", "unix"), "endpoint kind is closed"
    config = jail_egress.validate_config({
        "listen": kind, "socket_path": os.path.join(run_dir, "egress.sock") if kind == "unix" else None,
        "policy": policy, "sites": list(sites), "main_host": main_host, "main_port": main_port,
        "max_life_s": max_life_s, "caps": dict(caps or {})})
    try:
        proc = subprocess.Popen(
            [sys.executable, "-s", "-c", _BOOTSTRAP], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=run_dir,
            env=jailrun._jail_env(), start_new_session=(os.name == "posix"))
    except (OSError, ValueError):
        raise EgressError("spawn") from None
    try:
        assert proc.stdin is not None, "stdin pipe required"
        proc.stdin.write(json.dumps(config).encode("utf-8") + b"\n")
        proc.stdin.flush()
        endpoint = _parse_endpoint(_read_line(proc, _START_TIMEOUT_S), kind)
    except (OSError, EgressError, ValueError) as exc:
        jailrun._kill_group(proc)
        jailrun._reap(proc)
        raise EgressError("start") from exc
    return Egress(proc=proc, endpoint=endpoint)


def _read_line(proc: subprocess.Popen, timeout_s: float) -> bytes:
    """One stdout line, bounded by ``_LINE_CAP`` bytes and ``timeout_s`` seconds."""
    assert proc.stdout is not None, "stdout pipe required"
    assert timeout_s > 0, "timeout must be positive"
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout_s
    buf = b""
    for _ in range(_LINE_CAP):  # each pass adds ≥ 1 byte, ends, or times out
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EgressError("timeout")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise EgressError("timeout")
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk
        if b"\n" in buf or len(buf) > _LINE_CAP:
            break
    line = buf.split(b"\n", 1)[0]
    if not line or len(line) > _LINE_CAP:
        raise EgressError("no_line")
    return line


def _parse_endpoint(line: bytes, kind: str) -> dict:
    """``{"endpoint": {"kind": "tcp", "port": n} | {"kind": "unix", "path": p}}``."""
    assert kind in ("tcp", "unix"), "endpoint kind is closed"
    assert isinstance(line, bytes), "line must be bytes"
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict) or set(obj) != {"endpoint"} or not isinstance(obj["endpoint"], dict):
        raise EgressError("endpoint")
    endpoint = obj["endpoint"]
    if kind == "tcp":
        port = endpoint.get("port")
        ok = (set(endpoint) == {"kind", "port"} and endpoint["kind"] == "tcp"
              and isinstance(port, int) and 1 <= port <= 65535)
    else:
        ok = (set(endpoint) == {"kind", "path"} and endpoint["kind"] == "unix"
              and isinstance(endpoint["path"], str) and os.path.isabs(endpoint["path"]))
    if not ok:
        raise EgressError("endpoint")
    return endpoint


def parse_census(line: bytes) -> tuple[list[dict], int]:
    """The closed-key census line → ``(rows, dropped)``; anything else is ``census``."""
    assert isinstance(line, bytes), "line must be bytes"
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EgressError("census") from None
    if not isinstance(obj, dict) or set(obj) != {"census", "dropped"}:
        raise EgressError("census")
    rows, dropped = obj["census"], obj["dropped"]
    if not isinstance(rows, list) or len(rows) > jail_egress.MAX_ROWS:
        raise EgressError("census")
    if not isinstance(dropped, int) or dropped < 0:
        raise EgressError("census")
    for row in rows:  # bounded by MAX_ROWS
        if (not isinstance(row, dict) or set(row) != _ROW_KEYS
                or not isinstance(row["site"], str) or not 0 < len(row["site"]) <= 253
                or row["verdict"] not in jail_egress.VERDICTS
                or not all(isinstance(row[k], int) and row[k] >= 0 for k in ("tunnels", "up", "down"))):
            raise EgressError("census")
    assert len(rows) <= jail_egress.MAX_ROWS, "rows are capped"
    return rows, dropped
