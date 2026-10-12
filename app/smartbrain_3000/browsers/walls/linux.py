"""The Linux wall (ni-format §35; P3 design §2): user + mount + network + PID namespaces.

``wrap`` runs the engine through ``userns_boot.py`` (stdlib only, run by path with
``python -I -S``): it unshares USER|NS|NET|PID, maps only the app's own uid/gid, and its
namespace init masks /home /root /run /tmp /var/tmp /dev/shm /mnt /media and the data dir
with tmpfs, binds the engine dir back read-only and the run dir read-write, mounts a fresh
/proc, brings ``lo`` up and relays 127.0.0.1:``RELAY_PORT`` to the egress child's Unix
socket in the run dir — the namespace's only way out. The engine is a non-root process in
its user namespace, so it holds no capability there.

The bootstrap and the namespace init share the engine's process group (``helpers = 2``).
``verify`` (every watchdog poll): every PID sits in a user AND a network namespace other
than the app's and carries a ``Seccomp`` field (≥ 0); the only tolerated exception is the
lone bootstrap before its ``unshare`` (it has run nothing yet). ``check`` runs the
bootstrap's ``probe`` mode once per process and maps a failure to a closed reason:
``apparmor_userns`` (Ubuntu's AppArmor restriction on unprivileged user namespaces),
``userns_blocked`` (unshare refused: disabled by sysctl or filtered by seccomp), else
``sandbox``; running as root is ``sandbox`` too (the identity map would make the engine root
inside its namespace). ``self_test`` runs the ``canary`` mode (capabilities dropped) and
the engine's ``--version`` inside the wall.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .. import egress
from .. import manifest as mf
from .base import Canaries, Unavailable, Wall, canary_file, checked_dir, row, verdict

BOOT = Path(__file__).resolve().with_name("userns_boot.py")
RELAY_PORT = 3128  # inside the private network namespace, so any fixed port is free
BASE_MASKS = ("/home", "/root", "/run", "/tmp", "/var/tmp", "/dev/shm", "/mnt", "/media")
EXIT_SETUP = 125
_PROBE_TIMEOUT_S = 20.0
_LINE_CAP = 65536
_REQUIRED = ("loopback_tcp", "lan_tcp", "metadata", "udp", "dns", "home_read", "run_read",
             "proxy", "ns_facts", "verify", "engine_version", "census", "no_hits")
_ROW_KEYS = frozenset({"row", "ok", "detail"})
_lock = threading.Lock()
_cache: dict[str, Unavailable | None] = {}


def data_dir_of(engine_dir: Path) -> Path:
    """``<data>/browsers/<name>/<version>`` → ``<data>`` (the install layout)."""
    assert isinstance(engine_dir, Path), "engine dir must be a Path"
    assert engine_dir.parents[1].name == "browsers", "engines live under <data>/browsers"
    return engine_dir.parents[2]


def mount_plan(engine_dir: str | Path, run_dir: str | Path, data_dir: str | Path) -> dict:
    """The masks and the two binds, canonical and closed-charset. ValueError otherwise.
    The data dir (the vault's home) is masked unless a base mask already covers it."""
    engine, run, data = checked_dir(engine_dir), checked_dir(run_dir), checked_dir(data_dir)
    if engine == run or run.startswith(engine + "/") or engine.startswith(run + "/"):
        raise ValueError("unsafe_path")  # the engine dir must never be writable
    if data == "/" or run == data or data.startswith(run + "/"):
        raise ValueError("unsafe_path")
    covered = any(data == m or data.startswith(m + "/") for m in BASE_MASKS)
    masks = list(BASE_MASKS) + ([] if covered else [data])
    assert len(masks) <= 9, "bounded masks"
    assert all(m.startswith("/") for m in masks), "absolute masks"
    return {"masks": masks, "engine_dir": engine, "run_dir": run}


def boot_config(mode: str, plan: dict, socket_path: str | None, canary: dict | None) -> dict:
    """The bootstrap's closed-key config (``userns_boot.CONFIG_KEYS``)."""
    assert mode in ("engine", "probe", "canary"), "a bootstrap mode"
    assert (mode == "canary") == (canary is not None), "canary config only in canary mode"
    if socket_path is not None and not socket_path.startswith(plan["run_dir"] + "/"):
        raise ValueError("the egress socket lives in the run dir")
    return {"v": 1, "mode": mode, "engine_dir": plan["engine_dir"], "run_dir": plan["run_dir"],
            "socket": socket_path, "relay_port": RELAY_PORT, "masks": list(plan["masks"]),
            "uid": os.getuid(), "gid": os.getgid(), "canary": canary}


def boot_argv(cfg: dict, engine_argv: list[str] | None = None) -> list[str]:
    """``python -I -S userns_boot.py <cfg> [-- engine argv]``."""
    assert set(cfg) >= {"v", "mode"}, "a bootstrap config"
    assert (cfg["mode"] == "engine") == bool(engine_argv), "engine argv only in engine mode"
    argv = [sys.executable, "-I", "-S", str(BOOT), json.dumps(cfg, separators=(",", ":"))]
    return argv + (["--", *engine_argv] if engine_argv else [])


def _ns(pid: int | str) -> tuple[str, str, int] | None:
    """(user ns, net ns, Seccomp) of a PID; None when it is gone or unreadable."""
    assert str(pid).isdigit() or pid == "self", "a pid"
    try:
        user = os.readlink(f"/proc/{pid}/ns/user")
        net = os.readlink(f"/proc/{pid}/ns/net")
        with open(f"/proc/{pid}/status") as fh:
            fields = dict(line.split(":", 1) for line in fh.read(8192).splitlines() if ":" in line)
    except OSError:
        return None
    seccomp = fields.get("Seccomp", "").strip()
    assert isinstance(user, str), "a namespace link"
    return user, net, int(seccomp) if seccomp.isdigit() else -1


def _is_boot(pid: int) -> bool:
    assert isinstance(pid, int) and pid > 0, "a real pid"
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = fh.read(8192).split(b"\0")
    except OSError:
        return False
    assert isinstance(argv, list), "cmdline parts"
    return str(BOOT).encode() in argv and b"-I" in argv


def ns_confined(pid_tree: list[int], app_user: str, app_net: str) -> bool:
    """Every live PID sits in a user AND a network namespace other than the app's and
    carries a Seccomp field; the lone bootstrap before its unshare is tolerated."""
    assert isinstance(pid_tree, list), "pid tree must be a list"
    assert all(isinstance(p, int) and p > 0 for p in pid_tree), "pids are positive ints"
    for pid in pid_tree:  # bounded by the tree
        facts = _ns(pid)
        if facts is None:
            continue  # gone
        user, net, seccomp = facts
        if user != app_user and net != app_net and seccomp >= 0:
            continue
        if len(pid_tree) == 1 and user == app_user and _is_boot(pid):
            continue  # the bootstrap before unshare: nothing has run yet
        return False
    return True


class UsernsWall(Wall):
    """One render inside fresh user/mount/network/PID namespaces."""

    kind = "userns"
    endpoint_kind = "unix"
    expects_children = False
    helpers = 2  # the bootstrap and the namespace init

    def __init__(self, engine_dir: str | Path, run_dir: str | Path, socket_path: str) -> None:
        self._plan = mount_plan(engine_dir, run_dir, data_dir_of(Path(engine_dir)))
        self._socket = socket_path
        app = _ns("self")
        assert app is not None, "the app reads its own namespaces"
        self._app_user, self._app_net = app[0], app[1]
        assert socket_path.startswith(self._plan["run_dir"] + "/"), "socket in the run dir"

    def proxy_port(self, endpoint: dict) -> int:
        assert endpoint.get("kind") == "unix" and endpoint.get("path") == self._socket, \
            "the relay serves this run's egress socket"
        return RELAY_PORT

    def wrap(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        assert argv and isinstance(env, dict), "argv and env required"
        if os.path.dirname(os.path.realpath(argv[0])) != self._plan["engine_dir"]:
            raise ValueError("argv: the wall runs its own engine only")
        cfg = boot_config("engine", self._plan, self._socket, None)
        return boot_argv(cfg, argv), dict(env)

    def verify(self, pid_tree: list[int]) -> bool:
        assert self._plan, "wall was prepared"
        return ns_confined(pid_tree, self._app_user, self._app_net)

    def teardown(self) -> None:
        assert self._plan, "wall was prepared"
        assert self.kind == "userns", "userns teardown"


def _sysctl(name: str) -> str:
    assert name and ".." not in name, "a sysctl name"
    try:
        with open(f"/proc/sys/{name}") as fh:
            value = fh.read(64).strip()
    except OSError:
        return ""
    assert isinstance(value, str), "sysctl text"
    return value


def check(engine_dir: Path | None) -> Unavailable | None:
    """Once per process (cached): ``None`` when the namespaces can be built here."""
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    with _lock:
        if "probe" in _cache:
            return _cache["probe"]
    got = _probe(engine_dir)
    with _lock:
        _cache["probe"] = got
    assert got is None or isinstance(got, Unavailable), "a wall or a reason"
    return got


def _probe(engine_dir: Path | None) -> Unavailable | None:
    """The bootstrap's ``probe`` mode with the real masks; failures mapped to reasons."""
    assert engine_dir is None or isinstance(engine_dir, Path), "engine dir must be a Path"
    if os.geteuid() == 0 or not BOOT.is_file():
        return Unavailable("sandbox")
    if "0" in (_sysctl("kernel/unprivileged_userns_clone"), _sysctl("user/max_user_namespaces")):
        return Unavailable("userns_blocked")
    lab = Path(tempfile.mkdtemp(prefix="smartbrain-userns-probe-"))
    try:
        (lab / "engine").mkdir()
        (lab / "run").mkdir(mode=0o700)
        data = data_dir_of(engine_dir) if engine_dir is not None else lab / "data"
        data.mkdir(parents=True, exist_ok=True)
        plan = mount_plan(lab / "engine", lab / "run", data)
        got = _run_boot(boot_argv(boot_config("probe", plan, None, None)))
    except (OSError, ValueError):
        return Unavailable("sandbox")
    finally:
        shutil.rmtree(lab, ignore_errors=True)
    assert isinstance(got, dict), "a parsed result line"
    if got.get("ok") is True:
        return None
    if _sysctl("kernel/apparmor_restrict_unprivileged_userns") == "1":
        return Unavailable("apparmor_userns")
    return Unavailable("userns_blocked" if got.get("stage") == "unshare" else "sandbox")


def _run_boot(argv: list[str]) -> dict:
    """Run a probe-mode bootstrap; its one JSON line (closed keys) or ``{}``."""
    assert argv[0] == sys.executable, "the bootstrap runs on this interpreter"
    assert "-I" in argv and str(BOOT) in argv, "the isolated bootstrap"
    try:
        res = subprocess.run(argv, capture_output=True, timeout=_PROBE_TIMEOUT_S, check=False,
                             env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                             start_new_session=True)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_line(res.stdout[:_LINE_CAP].split(b"\n", 1)[0])


def parse_line(line: bytes) -> dict:
    """The bootstrap's result line: ``{ok: bool[, stage, errno][, rows]}``; else ``{}``."""
    assert isinstance(line, bytes), "bytes line"
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(obj, dict) or not isinstance(obj.get("ok"), bool):
        return {}
    if set(obj) - {"ok", "stage", "errno", "rows"}:
        return {}
    rows = obj.get("rows", [])
    if not isinstance(rows, list) or len(rows) > 16 or not all(
            isinstance(r, dict) and set(r) == _ROW_KEYS and isinstance(r["ok"], bool)
            and isinstance(r["row"], str) and isinstance(r["detail"], str) for r in rows):
        return {}
    assert isinstance(obj["ok"], bool), "a verdict"
    return obj


def self_test(m: mf.Manifest, engine_dir: Path, home: Path) -> tuple[str, list[dict]]:
    """The canary program inside the namespaces (capabilities dropped), a live ``verify``,
    the engine's ``--version`` inside the wall, the census and the host-side hit count."""
    assert isinstance(engine_dir, Path) and isinstance(home, Path), "paths required"
    assert m.version, "pinned version"
    rows: list[dict] = []
    lab = canaries = canary = handle = None
    try:
        lab, canaries = Path(tempfile.mkdtemp(prefix="smartbrain-userns-selftest-")), Canaries()
        os.chmod(lab, 0o700)
        canary, token = canary_file(home)
        handle = egress.start(str(lab), kind="unix", policy="own", sites=[],
                              main_host="selftest.invalid", main_port=443, max_life_s=120)
        plan = mount_plan(engine_dir, lab, data_dir_of(engine_dir))
        rows += _canary_run(plan, handle.endpoint["path"], canaries, canary, lab)
        rows.append(_version_run(m, plan))
    except (OSError, ValueError, egress.EgressError) as exc:
        rows.append(row("setup", False, exc.__class__.__name__))
    finally:
        rows.append(_census_row(handle))
        if canaries is not None:
            time.sleep(0.3)
            rows.append(row("no_hits", canaries.hit_count() == 0, f"hits={canaries.hit_count()}"))
            canaries.close()
        if canary is not None:
            canary.unlink(missing_ok=True)
        if lab is not None:
            shutil.rmtree(lab, ignore_errors=True)
    return verdict(rows, _REQUIRED), rows


def _canary_run(plan: dict, socket_path: str, canaries: Canaries, canary: Path,
                lab: Path) -> list[dict]:
    """Bootstrap in canary mode: read its rows, verify the live tree, then release it."""
    from .. import proc  # lazy: proc imports this package

    assert socket_path.startswith(plan["run_dir"] + "/"), "the egress socket is in the run dir"
    inside = lab / "inside.txt"
    inside.write_text("USERNS-INSIDE-OK\n")
    app = _ns("self")
    assert app is not None, "the app reads its own namespaces"
    can = {"tcp4": canaries.ports["tcp4"], "tcp6": canaries.ports.get("tcp6", 0),
           "udp4": canaries.ports["udp4"], "lan": canaries.lan, "inside": str(inside),
           "inside_text": "USERNS-INSIDE-OK\n", "home_file": str(canary),
           "app_userns": app[0], "app_netns": app[1], "hold_s": 20}
    child = subprocess.Popen(boot_argv(boot_config("canary", plan, socket_path, can)),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"},
                             start_new_session=True)
    try:
        line = _read_line(child)
        got = parse_line(line)
        live = [pid for pid, _rss in proc.group_members(child.pid)]
        alive_ok = len(live) == 2 and ns_confined(live, app[0], app[1])
    finally:
        if child.stdin is not None:
            child.stdin.close()
        proc.kill_tree(child)
    rows = list(got.get("rows", [])) if got.get("ok") else [
        row("setup", False, f"{got.get('stage', 'no_line')}:{got.get('errno', '')}")]
    return rows + [row("verify", alive_ok, f"pids={len(live)}")]


def _read_line(child: subprocess.Popen) -> bytes:
    assert child.stdout is not None, "stdout pipe"
    buf = b""
    deadline = time.monotonic() + _PROBE_TIMEOUT_S
    for _ in range(_LINE_CAP):  # each pass adds ≥ 1 byte, ends, or times out
        remaining = deadline - time.monotonic()
        ready = select.select([child.stdout], [], [], max(0.0, remaining))[0] if remaining > 0 else []
        if not ready:
            break
        chunk = os.read(child.stdout.fileno(), 4096)
        buf += chunk
        if not chunk or b"\n" in buf or len(buf) > _LINE_CAP:
            break
    assert isinstance(buf, bytes), "bytes"
    return buf.split(b"\n", 1)[0]


def _version_run(m: mf.Manifest, plan: dict) -> dict:
    """The engine starts inside the namespaces and prints the pinned version."""
    assert m.version, "pinned version"
    exe = os.path.join(plan["engine_dir"], m.executable)
    assert exe.startswith("/"), "absolute engine path"
    try:
        res = subprocess.run(boot_argv(boot_config("engine", plan, None, None), [exe, "--version"]),
                             capture_output=True, timeout=_PROBE_TIMEOUT_S, check=False,
                             env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                             start_new_session=True)
    except (OSError, subprocess.TimeoutExpired):
        return row("engine_version", False, "timeout")
    out = res.stdout[:4096].decode("utf-8", "replace")
    return row("engine_version", res.returncode == 0 and m.version in out, f"rc={res.returncode}")


def _census_row(handle: object) -> dict:
    """The canary's CONNECT went through the relay to THIS egress child (it refused the
    IP literal)."""
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
