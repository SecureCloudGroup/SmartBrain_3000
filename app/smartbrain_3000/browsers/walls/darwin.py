"""The macOS wall (ni-format §35; P3 design §2): Seatbelt, deny-default, around the engine.

The profile is ``walls/obscura.sb`` — the B0 spike's five measured rules, byte for byte
(sha256 pinned below; a changed file makes the wall unavailable):

1. ``process-exec`` of ``ENGINE_DIR/obscura`` only (no ``process-fork``: the engine spawns
   nothing, and nothing else can be exec'd);
2. ``sysctl-read hw.pagesize_compat`` (Rust std's guard page);
3. ``file-read-data`` of the literal ``/`` (dyld's cryptex lookup);
4. ``file-read* file-write*`` under ``RUN_DIR`` (output, storage, HOME, TMPDIR, cwd);
5. ``network-outbound`` TCP to ``localhost:PROXY_PORT`` — the egress child, nothing else.

``wrap`` prepends ``sandbox-exec -f <profile> -D ENGINE_DIR=… -D RUN_DIR=… -D PROXY_PORT=…``
with canonical paths (Seatbelt matches the resolved ``/private/var/…`` form) from a closed
character set and a live port (0 is refused; sandbox-exec itself refuses it, exit 65).
sandbox-exec applies the profile and execs the engine in place (same PID). ``verify`` calls
``sandbox_check(pid, NULL, SANDBOX_CHECK_NO_REPORT)`` on every PID of the tree: 1 =
confined; 0 for any PID that is no longer the sandbox-exec wrapper = the watchdog's
``wall`` verdict.

``check`` runs once per process and caches: sandbox-exec present, the profile hash pinned,
``sandbox_check`` loadable, a dry run of the exact profile with real parameters (exec of
``/usr/bin/true`` must be refused with exit 71 — the profile compiled, exec is confined),
and a canary read (the exact profile plus an exec rule for ``/bin/cat``: a file in the run
dir is readable, a file beside it is not). ``self_test`` runs the canary program under the
same rules (``nc``/``cat`` with one exec rule each) and the engine's ``--version`` under the
exact profile.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .. import egress
from .. import manifest as mf
from .base import Canaries, Unavailable, Wall, canary_file, checked_dir, row, verdict

PROFILE = Path(__file__).resolve().with_name("obscura.sb")
PROFILE_SHA256 = "c674ffc122a945a628eec83b577b96c85422c0ddf1691605165f61f38272fc67"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_LIB = "/usr/lib/system/libsystem_sandbox.dylib"
LIBPROC = "/usr/lib/libproc.dylib"
ENGINE_NAME = "obscura"  # the profile's exec rule names this file inside ENGINE_DIR
EXEC_DENIED_RC = 71      # sandbox-exec: execvp() of a binary the profile does not allow
NO_REPORT_FALLBACK = 0x40000000
PROBE_BINARIES = ("/usr/bin/nc", "/bin/cat")
EPERM_TEXT = "Operation not permitted"
_RUN_TIMEOUT_S = 15.0
_OUT_CAP = 4096
_REQUIRED = ("exec_denied", "loopback_tcp", "lan_tcp", "metadata", "udp", "dns", "proxy",
             "home_read", "run_read", "sandbox_check", "engine_version", "census", "no_hits")
_lock = threading.Lock()
_cache: dict[str, object] = {}  # "lib" | "libproc" → loaded handles; "probe" → the verdict


def _sandbox_fn() -> tuple[object, int] | None:
    """``sandbox_check`` from libsystem_sandbox and the NO_REPORT flag, loaded once."""
    assert SANDBOX_LIB.startswith("/usr/lib/"), "the system library"
    with _lock:
        if "lib" in _cache:
            return _cache["lib"]  # type: ignore[return-value]
        try:
            lib = ctypes.CDLL(SANDBOX_LIB, use_errno=True)
            fn = lib.sandbox_check
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            fn.restype = ctypes.c_int
            try:
                flag = int(ctypes.c_int.in_dll(lib, "SANDBOX_CHECK_NO_REPORT").value)
            except ValueError:
                flag = NO_REPORT_FALLBACK
            got: tuple[object, int] | None = (fn, flag)
        except (OSError, AttributeError):
            got = None
        _cache["lib"] = got
    assert got is None or len(got) == 2, "function and flag"
    return got


def sandbox_check(pid: int) -> int:
    """1 = confined, 0 = not confined, -1 = unknown (no library or an error)."""
    assert isinstance(pid, int) and pid > 0, "a real pid"
    loaded = _sandbox_fn()
    if loaded is None:
        return -1
    fn, flag = loaded
    state = int(fn(pid, None, flag))  # type: ignore[operator]
    assert isinstance(state, int), "sandbox_check returns an int"
    return state if state in (0, 1) else -1


def _proc_path(pid: int) -> str:
    """The executable path of ``pid`` (libproc ``proc_pidpath``), or ``""``."""
    assert pid > 0, "a real pid"
    with _lock:
        if "libproc" not in _cache:
            try:
                _cache["libproc"] = ctypes.CDLL(LIBPROC, use_errno=True).proc_pidpath
            except (OSError, AttributeError):
                _cache["libproc"] = None
        fn = _cache["libproc"]
    if fn is None:
        return ""
    buf = ctypes.create_string_buffer(4096)
    size = int(fn(ctypes.c_int(pid), buf, ctypes.c_uint32(4096)))  # type: ignore[operator]
    assert size <= 4096, "the path fits the buffer"
    return buf.raw[:size].decode("utf-8", "replace") if size > 0 else ""


def _alive(pid: int) -> bool:
    assert isinstance(pid, int), "a pid"
    assert pid > 0, "a real pid"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def confined(pid_tree: list[int]) -> bool:
    """Every PID is under a Seatbelt profile (``sandbox_check`` = 1). Tolerated: the
    sandbox-exec wrapper before it exec'd (it has not run the engine yet) and a PID that
    is already gone."""
    assert isinstance(pid_tree, list), "pid tree must be a list"
    assert all(isinstance(p, int) and p > 0 for p in pid_tree), "pids are positive ints"
    for pid in pid_tree:  # bounded by the tree
        state = sandbox_check(pid)
        if state == 1:
            continue
        if state == 0 and _proc_path(pid) == SANDBOX_EXEC:
            continue
        if state == -1 and not _alive(pid):
            continue
        return False
    return True


def profile_params(engine_dir: str | Path, run_dir: str | Path, port: object) -> list[str]:
    """The ``-D`` arguments: canonical, closed-charset paths and a live port. ValueError."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("bad_port")
    engine, run = checked_dir(engine_dir), checked_dir(run_dir)
    if engine == run or run.startswith(engine + "/") or engine.startswith(run + "/"):
        raise ValueError("unsafe_path")  # the run dir is writable; the engine dir must not be
    params = ["-D", f"ENGINE_DIR={engine}", "-D", f"RUN_DIR={run}", "-D", f"PROXY_PORT={port}"]
    assert engine.startswith("/") and run.startswith("/"), "canonical absolute paths"
    assert len(params) == 6, "three parameters"
    return params


class SeatbeltWall(Wall):
    """One render under the measured deny-default profile."""

    kind = "seatbelt"
    endpoint_kind = "tcp"
    expects_children = False

    def __init__(self, engine_dir: str | Path, run_dir: str | Path, port: int) -> None:
        self._params = profile_params(engine_dir, run_dir, port)
        self._engine = os.path.join(checked_dir(engine_dir), ENGINE_NAME)
        assert self._params[1].startswith("ENGINE_DIR="), "parameters in order"
        assert self._engine.endswith("/" + ENGINE_NAME), "the profile names the engine file"

    def wrap(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        assert argv and isinstance(env, dict), "argv and env required"
        if os.path.realpath(argv[0]) != self._engine:
            raise ValueError("argv: the wall runs its own engine only")
        # the canonical path: under deny-default even reading a symlink on the way is denied
        return [SANDBOX_EXEC, "-f", str(PROFILE), *self._params, self._engine, *argv[1:]], dict(env)

    def verify(self, pid_tree: list[int]) -> bool:
        assert self._params, "wall was prepared"
        return confined(pid_tree)

    def teardown(self) -> None:
        assert self._params, "wall was prepared"
        assert self.kind == "seatbelt", "seatbelt teardown"


def profile_ok() -> bool:
    """The shipped profile is the measured one (sha256 pinned) and sandbox-exec exists."""
    assert len(PROFILE_SHA256) == 64, "a pinned sha256"
    assert PROFILE.suffix == ".sb", "a Seatbelt profile"
    try:
        digest = hashlib.sha256(PROFILE.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == PROFILE_SHA256 and os.access(SANDBOX_EXEC, os.X_OK)


def check(engine_dir: Path | None) -> Unavailable | None:
    """Once per process (cached): ``None`` when the wall can be built here."""
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    with _lock:
        if "probe" in _cache:
            return _cache["probe"]  # type: ignore[return-value]
    got = _probe(engine_dir)
    with _lock:
        _cache["probe"] = got
    assert got is None or isinstance(got, Unavailable), "a wall or a reason"
    return got


def _probe(engine_dir: Path | None) -> Unavailable | None:
    """The dry run + canary read with real parameters (see the module docstring)."""
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    if not profile_ok() or _sandbox_fn() is None:
        return Unavailable("sandbox")
    try:
        lab = _Lab()
    except OSError:
        return Unavailable("sandbox")
    try:
        params = profile_params(engine_dir or lab.root / "engine", lab.run, lab.port)
        dry = _run([SANDBOX_EXEC, "-f", str(PROFILE), *params, "/usr/bin/true"])
        if dry[0] != EXEC_DENIED_RC or EPERM_TEXT not in dry[2]:
            return Unavailable("sandbox")
        rc, out, err = _run([SANDBOX_EXEC, "-f", str(lab.probe_profile), *params, "/bin/cat",
                             str(lab.inside), str(lab.outside)])
        if rc == 0 or out != lab.inside_text or EPERM_TEXT not in err:
            return Unavailable("sandbox")
        assert lab.outside_text not in out, "the outside file stayed unread"
        return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return Unavailable("sandbox")
    finally:
        lab.close()


class _Lab:
    """A temp dir holding the run dir, a file inside it, a file beside it, the probe
    profile (the exact profile + one exec rule per probe binary) and a live loopback port."""

    def __init__(self) -> None:
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="smartbrain-wallprobe-")))
        self.run = self.root / "run"
        self.run.mkdir(mode=0o700)
        self.inside, self.outside = self.run / "inside.txt", self.root / "outside.txt"
        self.inside_text, self.outside_text = "WALL-INSIDE-OK\n", "WALL-OUTSIDE-SECRET\n"
        self.inside.write_text(self.inside_text)
        self.outside.write_text(self.outside_text)
        rules = "".join(f'\n(allow process-exec (literal "{b}"))' for b in PROBE_BINARIES)
        self.probe_profile = self.root / "probe.sb"
        self.probe_profile.write_text(PROFILE.read_text() + rules + "\n")
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = int(self._listener.getsockname()[1])
        assert self.port > 0, "a live port"
        assert self.run.is_dir() and self.probe_profile.is_file(), "the lab is set up"

    def close(self) -> None:
        self._listener.close()
        shutil.rmtree(self.root, ignore_errors=True)
        assert not self.root.exists(), "the lab is gone"


def _run(argv: list[str], stdin: bytes = b"") -> tuple[int, str, str]:
    """One probe process: ``(rc, stdout, stderr)`` text, capped; a timeout is rc -999."""
    assert argv and os.path.isabs(argv[0]), "absolute executable"
    assert len(stdin) < 4096, "small input"
    try:
        res = subprocess.run(argv, input=stdin, capture_output=True, timeout=_RUN_TIMEOUT_S,
                             env={"PATH": "/usr/bin:/bin"}, check=False)
    except subprocess.TimeoutExpired:
        return -999, "", "timeout"
    out = res.stdout[:_OUT_CAP].decode("utf-8", "replace")
    return res.returncode, out, res.stderr[:_OUT_CAP].decode("utf-8", "replace")


def self_test(m: mf.Manifest, engine_dir: Path, home: Path) -> tuple[str, list[dict]]:
    """The canary program under the wall: loopback/LAN/metadata/UDP/DNS/home-file must
    fail, the run dir and the egress proxy must work, ``sandbox_check`` must see a live
    confined process, and the engine must start under the exact profile."""
    assert isinstance(engine_dir, Path) and isinstance(home, Path), "paths required"
    assert m.executable == ENGINE_NAME, "the profile execs the obscura binary"
    rows: list[dict] = []
    lab = canaries = canary = handle = None
    try:
        lab, canaries = _Lab(), Canaries()
        canary, token = canary_file(home)
        handle = egress.start(str(lab.run), kind="tcp", policy="own", sites=[],
                              main_host="selftest.invalid", main_port=443, max_life_s=120)
        port = int(handle.endpoint["port"])
        params = profile_params(engine_dir, lab.run, port)
        rows += _network_rows(lab, params, canaries, port)
        rows += _file_rows(lab, params, canary, token)
        rows.append(_check_row(lab, params))
        rows.append(_version_row(m, engine_dir, params))
    except (OSError, ValueError, egress.EgressError) as exc:
        rows.append(row("setup", False, exc.__class__.__name__))
    finally:
        rows.append(_census_row(handle))
        if canaries is not None:
            time.sleep(0.3)  # late datagrams/accepts land before the count
            rows.append(row("no_hits", canaries.hit_count() == 0, f"hits={canaries.hit_count()}"))
            canaries.close()
        if canary is not None:
            canary.unlink(missing_ok=True)
        if lab is not None:
            lab.close()
    return verdict(rows, _REQUIRED), rows


def _probe_run(lab: _Lab, params: list[str], binary: str, args: list[str],
               stdin: bytes = b"") -> tuple[int, str, str]:
    """A probe binary under the exact profile plus its one exec rule."""
    assert binary in PROBE_BINARIES, "only the listed probe binaries"
    assert params, "profile parameters required"
    return _run([SANDBOX_EXEC, "-f", str(lab.probe_profile), *params, binary, *args], stdin)


def _network_rows(lab: _Lab, params: list[str], canaries: Canaries, port: int) -> list[dict]:
    """Every target must be refused by the WALL (EPERM), not merely closed."""
    assert port > 0, "the egress port"
    rows = []
    dry = _run([SANDBOX_EXEC, "-f", str(PROFILE), *params, "/usr/bin/true"])
    rows.append(row("exec_denied", dry[0] == EXEC_DENIED_RC, f"rc={dry[0]}"))
    targets = [("loopback_tcp", "127.0.0.1", canaries.ports["tcp4"]),
               ("metadata", "169.254.169.254", 80),
               ("lan_tcp", canaries.lan or "192.168.0.1", canaries.ports["tcp4"])]
    if "tcp6" in canaries.ports:
        targets.append(("loopback_v6", "::1", canaries.ports["tcp6"]))
    for name, host, target_port in targets:  # bounded: ≤ 4
        rc, out, err = _probe_run(lab, params, "/usr/bin/nc", ["-v", "-z", "-w", "2", host,
                                                               str(target_port)])
        rows.append(row(name, rc != 0 and EPERM_TEXT in out + err, f"rc={rc}"))
    rc, _out, _err = _probe_run(lab, params, "/usr/bin/nc",
                                ["-u", "-w", "1", "127.0.0.1", str(canaries.ports["udp4"])],
                                b"canary\n")
    rows.append(row("udp", rc != 0, f"rc={rc}"))
    rc, out, err = _probe_run(lab, params, "/usr/bin/nc", ["-v", "-z", "-w", "2", "localhost",
                                                           str(port)])
    rows.append(row("dns", rc != 0 and "nodename nor servname" in out + err, f"rc={rc}"))
    rc, out, _err = _probe_run(lab, params, "/usr/bin/nc", ["-w", "3", "127.0.0.1", str(port)],
                               b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
    rows.append(row("proxy", out.startswith("HTTP/1.1 403"), f"rc={rc}"))
    assert len(rows) <= 8, "bounded rows"
    return rows


def _file_rows(lab: _Lab, params: list[str], canary: Path, token: bytes) -> list[dict]:
    assert canary.is_file(), "the canary exists"
    assert token, "a canary token"
    rc, out, err = _probe_run(lab, params, "/bin/cat", [str(canary)])
    denied = rc != 0 and token.decode() not in out and EPERM_TEXT in err
    rc2, out2, _err2 = _probe_run(lab, params, "/bin/cat", [str(lab.inside)])
    return [row("home_read", denied, f"rc={rc}"),
            row("run_read", rc2 == 0 and out2 == lab.inside_text, f"rc={rc2}")]


def _check_row(lab: _Lab, params: list[str]) -> dict:
    """``sandbox_check`` sees a live confined process as 1 and this process as 0."""
    assert params, "profile parameters required"
    assert lab.probe_profile.is_file(), "the probe profile exists"
    child = subprocess.Popen([SANDBOX_EXEC, "-f", str(lab.probe_profile), *params, "/bin/cat"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
    try:
        for _ in range(40):  # bounded: ≤ 2 s for sandbox-exec to exec cat
            if _proc_path(child.pid) == "/bin/cat":
                break
            time.sleep(0.05)
        state, wall_ok, own = sandbox_check(child.pid), confined([child.pid]), sandbox_check(os.getpid())
    finally:
        if child.stdin is not None:
            child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    return row("sandbox_check", state == 1 and wall_ok and own == 0, f"child={state} self={own}")


def _version_row(m: mf.Manifest, engine_dir: Path, params: list[str]) -> dict:
    """The engine starts under the EXACT profile and prints the pinned version."""
    assert m.version, "pinned version"
    assert params, "profile parameters required"
    exe = os.path.join(checked_dir(engine_dir), m.executable)
    rc, out, _err = _run([SANDBOX_EXEC, "-f", str(PROFILE), *params, exe, "--version"])
    return row("engine_version", rc == 0 and m.version in out, f"rc={rc}")


def _census_row(handle: object) -> dict:
    """The proxy row's CONNECT reached THIS egress child: it refused the literal."""
    if handle is None:
        return row("census", False, "no egress")
    assert hasattr(handle, "stop"), "an egress handle"
    try:
        rows, _dropped = handle.stop()  # type: ignore[attr-defined]
    except egress.EgressError as exc:
        return row("census", False, exc.reason)
    seen = [r["verdict"] for r in rows]
    assert isinstance(seen, list), "verdict list"
    return row("census", "ip_literal" in seen, ",".join(seen)[:100])
