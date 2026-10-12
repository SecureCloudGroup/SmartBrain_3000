"""The renderer sidecar's agent (ni-format §35; P3 design §2), Docker only.

It runs as uid 65532 (the app's gid) in the ``render`` compose service: ``network_mode:
none``, read-only root, ``cap_drop: [ALL]``, no-new-privileges, init, pid/memory limits,
and no app data volume — only the shared ``/render`` volume. There is no listener of any
kind: jobs arrive as files.

- ``--install`` (image build only): the pinned Linux engine through the SAME installer the
  app uses (pinned URL, size cap, sha256 before unpack, member hashes), then
  ``verify_installed`` must pass or the build fails.
- serve: once per boot, a canary (TCP to a public IP, the Docker gateways and the metadata
  address, UDP to a public resolver, a DNS lookup — all must fail; only ``lo`` may be up;
  the root must be read-only; the uid must not be 0; the engine must re-hash clean) →
  ``READY.json``; then a heartbeat every 2 s and a poll for ``<run dir>/job.json``.
- one job at a time: closed-key JSON; a ``render`` job's argv is re-walked with the
  manifest grammar (the agent substitutes its own engine path), the proxy must be the relay
  and every path must sit in the job's run dir; the engine is re-hashed; the relay
  127.0.0.1:3128 → ``<run dir>/egress.sock`` starts; the engine runs under the same
  ``proc.spawn``/``watch``/``kill_tree`` as a native run (``verify``: every PID shares the
  agent's canary-proven network namespace); every other process in the container is then
  killed, the engine's state (home/tmp/storage) removed, and ``result.json`` written.
"""

from __future__ import annotations

import collections
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__
from . import cmdline, install, proc
from . import manifest as mf
from .walls import docker as dw
from .walls.base import Wall
from .walls.userns_boot import Relay, SetupError

ENGINE = "obscura"
AGENT_ENV = "SMARTBRAIN_RENDER_AGENT"
BOOT_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 53), ("172.17.0.1", 80), ("192.168.65.254", 80),
                ("169.254.169.254", 80))
BOOT_UDP = ("8.8.8.8", 53)
HEARTBEAT_S = 2.0
POLL_S = 0.1
_MAX_LOOPS = 10**9  # ~3 years of polls; the container restarts the agent after that
_MAX_JOBS_LISTED = 256
_SEEN_CAP = 1024
_MAX_ARGV = 64
_MAX_PROC = 4096
_GRACE_S = 5.0


class InnerWall(Wall):
    """Inside the sidecar: every engine PID shares the agent's own network namespace — the
    one the boot canary proved has no route out."""

    kind = "sidecar"
    expects_children = False

    def __init__(self, netns: str) -> None:
        assert netns.startswith("net:["), "a network namespace id"
        self._netns = netns

    def verify(self, pid_tree: list[int]) -> bool:
        assert isinstance(pid_tree, list), "pid tree must be a list"
        assert self._netns, "the proven namespace"
        for pid in pid_tree:  # bounded by the tree
            try:
                if os.readlink(f"/proc/{pid}/ns/net") != self._netns:
                    return False
            except FileNotFoundError:
                continue  # gone
            except OSError:
                return False
        return True

    def teardown(self) -> None:
        assert self._netns, "the proven namespace"


def _outcome(target: str, family: int, kind: int, address: tuple) -> dict:
    """One boot canary: ``connected`` is a failed wall; anything else is the errno name."""
    assert target and address, "a named target"
    assert kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM), "tcp or udp"
    sock = socket.socket(family, kind)
    sock.settimeout(2.0)
    try:
        if kind == socket.SOCK_DGRAM:
            sock.sendto(b"canary", address)
        else:
            sock.connect(address)
        return {"target": target, "outcome": "connected"}
    except OSError as exc:
        return {"target": target, "outcome": os.strerror(exc.errno) if exc.errno else "timeout"}
    finally:
        sock.close()


def boot_facts(m: mf.Manifest, platform: str | None) -> dict:
    """The READY.json body (``docker.READY_KEYS``) from this container's own canary."""
    assert isinstance(m, mf.Manifest), "manifest required"
    canaries = [_outcome(f"{h}:{p}", socket.AF_INET, socket.SOCK_STREAM, (h, p)) for h, p in BOOT_TARGETS]
    canaries.append(_outcome(f"udp:{BOOT_UDP[0]}:{BOOT_UDP[1]}", socket.AF_INET, socket.SOCK_DGRAM,
                             BOOT_UDP))
    try:
        socket.getaddrinfo("example.com", 443)
        canaries.append({"target": "dns:example.com", "outcome": "connected"})
    except OSError as exc:
        canaries.append({"target": "dns:example.com", "outcome": f"gaierror {exc.errno}"})
    verified = platform is not None and install.verify_installed(m.name, manifest_obj=m,
                                                                  platform=platform)[0]
    facts = {"v": 1, "boot": secrets.token_hex(8), "at": datetime.now(UTC).isoformat(),
             "uid": os.getuid(), "gid": os.getgid(), "interfaces": up_interfaces(),
             "canaries": canaries, "netns": os.readlink("/proc/self/ns/net"),
             "readonly_root": _readonly_root(),
             "engine": {"name": m.name, "version": m.version, "platform": platform or "",
                        "sha256": m.platforms[platform].sha256 if platform in m.platforms else "",
                        "verified": bool(verified)}, "ok": False,
             "build": __version__[:dw._MAX_BUILD]}  # B3a: the app build this image was built with
    facts["ok"] = dw.valid_ready({**facts, "ok": True})
    assert set(facts) == dw.READY_KEYS, "closed READY keys"
    return facts


def up_interfaces() -> list[str]:
    """Interfaces that are UP. A ``network_mode: none`` namespace still lists the kernel's
    fallback tunnel devices (gre0, sit0, tunl0, … — down, no address; measured) and the
    ``bonding_masters`` file; only an UP interface can carry a packet."""
    names = sorted(os.listdir("/sys/class/net"))[:64]
    assert isinstance(names, list), "a listing"
    up = []
    for name in names:  # bounded
        try:
            with open(f"/sys/class/net/{name}/flags") as fh:
                flags = int(fh.read(32).strip(), 16)
        except (OSError, ValueError):
            continue  # not an interface (bonding_masters) or gone
        if flags & 0x1:  # IFF_UP
            up.append(name)
    assert len(up) <= len(names), "a subset of the listing"
    return up


def _readonly_root() -> bool:
    """The root filesystem is mounted read-only (the mount flag, not a permission error)."""
    flags = os.statvfs("/").f_flag
    assert isinstance(flags, int), "statvfs flags"
    return bool(flags & os.ST_RDONLY)


def validate_render(job: dict, m: mf.Manifest, run_dir: Path, engine: Path) -> tuple[list[str], dict]:
    """The app's argv re-walked with the manifest grammar, our engine path substituted,
    the relay as the proxy, every path inside the run dir. ``ValueError`` otherwise."""
    assert isinstance(job, dict) and run_dir.name.startswith(dw.RUN_PREFIX), "a job in a run dir"
    argv, env = job["argv"], job["env"]
    if not isinstance(argv, list) or not 2 <= len(argv) <= _MAX_ARGV or not all(
            isinstance(t, str) for t in argv) or os.path.basename(argv[0]) != m.executable:
        raise ValueError("argv")
    real = [str(engine), *argv[1:]]
    flags = mf.walk_argv(real[1:], m.forbidden, rendered=True)
    cmdline.check_argv(m, real, cmdline.validate_url(flags["fetch"])[0])
    if flags["--proxy"] != f"http://127.0.0.1:{dw.RELAY_PORT}":
        raise ValueError("argv")
    inside = str(run_dir) + "/"
    if not all(os.path.realpath(flags[f]).startswith(inside) for f in ("--output", "--storage-dir")):
        raise ValueError("argv")
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ValueError("env")
    cmdline.check_env(env)
    if not all(os.path.realpath(env.get(k, "/")).startswith(inside) for k in ("HOME", "TMPDIR")):
        raise ValueError("env")
    assert real[0] == str(engine), "our own engine runs"
    return real, dict(env)


def run_render(job: dict, m: mf.Manifest, run_dir: Path, engine: Path, netns: str) -> dict:
    """One engine run, the relay up for its whole life; strays killed afterwards."""
    argv, env = validate_render(job, m, run_dir, engine)
    lim = m.limits
    deadline = min(float(job["deadline_s"]), lim.timeout_s + _GRACE_S)
    rss = min(int(job["rss_mb"]), lim.rss_mb)
    assert deadline > 0 and rss > 0, "positive limits"
    relay = Relay(dw.RELAY_PORT, str(run_dir / "egress.sock"))
    out = {"spawned": False, "rc": None, "verdict": "", "signals": []}
    try:
        child = proc.spawn(argv, env, str(run_dir), cpu_s=int(4 * deadline),
                           data_mb=min(int(job["data_mb"]), 65536))
    except (OSError, ValueError):
        relay.close()
        return out
    tap = proc.StderrTap(child)
    tap.start()
    try:
        out["verdict"] = proc.watch(child, InnerWall(netns), rss, deadline)
    finally:
        proc.kill_tree(child)
        kill_strays()
        tap.join(_GRACE_S)
        relay.close()
    out.update(spawned=True, rc=child.returncode, signals=tap.signals())
    assert out["verdict"] in dw.VERDICTS, "a closed verdict"
    return out


def run_selftest(job: dict, m: mf.Manifest, run_dir: Path, engine: Path) -> list[dict]:
    """In-sidecar rows: the relay reaches the app's egress child, no app data is here, DNS
    fails, the engine starts."""
    assert job["kind"] == "selftest", "a selftest job"
    assert engine.is_file(), "the engine is installed"
    relay = Relay(dw.RELAY_PORT, str(run_dir / "egress.sock"))
    rows = [_proxy_row()]
    relay.close()
    data = Path(install.browsers_root()).parent
    leftovers = sorted(set(os.listdir(data)) - {"browsers"}) if data.is_dir() else []
    rows.append({"row": "data_absent", "ok": not leftovers, "detail": ",".join(leftovers)[:100]})
    try:
        socket.getaddrinfo("example.com", 443)
        rows.append({"row": "dns", "ok": False, "detail": "resolved"})
    except OSError:
        rows.append({"row": "dns", "ok": True, "detail": ""})
    try:
        res = subprocess.run([str(engine), "--version"], capture_output=True, timeout=20,
                             env={"PATH": "/usr/local/bin:/usr/bin:/bin"}, cwd=str(run_dir),
                             check=False)
        version_ok = res.returncode == 0 and m.version in res.stdout[:4096].decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        version_ok = False
    kill_strays()
    return rows + [{"row": "engine_version", "ok": version_ok, "detail": ""}]


def _proxy_row() -> dict:
    assert 1 <= dw.RELAY_PORT <= 65535, "the relay port"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    try:
        sock.connect(("127.0.0.1", dw.RELAY_PORT))
        sock.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        head = sock.recv(64)
    except OSError as exc:
        return {"row": "proxy", "ok": False, "detail": os.strerror(exc.errno or 0)[:60]}
    finally:
        sock.close()
    assert isinstance(head, bytes), "a reply"
    return {"row": "proxy", "ok": head.startswith(b"HTTP/1.1 403"), "detail": ""}


def kill_strays() -> int:
    """SIGKILL every process in this container but init and the agent (sidecar only)."""
    if os.environ.get(AGENT_ENV) != "1":
        return 0  # never outside the sidecar
    me = os.getpid()
    assert me != 1, "init stays"
    killed = 0
    for name in os.listdir("/proc")[:_MAX_PROC]:  # bounded
        if name.isdigit() and int(name) not in (1, me):
            try:
                os.kill(int(name), signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                continue
    assert killed >= 0, "a count"
    return killed


def clean_state(run_dir: Path) -> None:
    """Remove what the engine left in home/, tmp/ and storage/ (the cookie file)."""
    assert run_dir.name.startswith(dw.RUN_PREFIX), "a run dir"
    for sub in ("home", "tmp", "storage"):  # fixed set; the dirs themselves are the app's
        base = run_dir / sub
        if not base.is_dir() or base.is_symlink():
            continue
        for entry in list(base.iterdir())[:_MAX_PROC]:  # bounded
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    assert run_dir.is_dir(), "the run dir stays for the app"


def process(run_dir: Path, m: mf.Manifest, platform: str, netns: str) -> None:
    """Read, validate and run one job; always answer with ``result.json``."""
    assert run_dir.name.startswith(dw.RUN_PREFIX), "a run dir"
    job = dw.read_json(run_dir, dw.JOB_NAME, dw.JOB_KEYS)
    result = {"v": 1, "run_id": run_dir.name, "spawned": False, "rc": None, "verdict": "",
              "signals": [], "refused": "", "rows": []}
    engine = install.engine_dir(m.name, m.version) / m.executable
    try:
        if job is None or job["v"] != 1 or job["run_id"] != run_dir.name or job["relay_port"] != dw.RELAY_PORT:
            result["refused"] = "job"
        elif not install.verify_installed(m.name, manifest_obj=m, platform=platform)[0]:
            result["refused"] = "engine"
        elif job["kind"] == "selftest":
            result["rows"] = run_selftest(job, m, run_dir, engine)
        elif job["kind"] == "render":
            result.update(run_render(job, m, run_dir, engine, netns))
        else:
            result["refused"] = "job"
    except ValueError as exc:
        result["refused"] = "env" if str(exc) == "env" else "argv"
    except SetupError:
        result["refused"] = "busy"  # the relay port is still held: answer, keep serving
    except Exception as exc:  # one bad job must never take the agent down
        print(json.dumps({"job_error": exc.__class__.__name__}), flush=True)
        result["refused"] = "job"
    clean_state(run_dir)
    assert set(result) == dw.RESULT_KEYS, "closed result keys"
    dw.write_json(run_dir, dw.RESULT_NAME, result)


def pending(seen: set[str]) -> list[Path]:
    """Run dirs on the volume with a job and no result yet (bounded listing)."""
    assert isinstance(seen, set), "seen ids"
    out = []
    for name in sorted(os.listdir(dw.RENDER_ROOT))[:_MAX_JOBS_LISTED]:  # bounded
        path = dw.RENDER_ROOT / name
        if (name.startswith(dw.RUN_PREFIX) and name not in seen and path.is_dir()
                and not path.is_symlink() and (path / dw.JOB_NAME).is_file()
                and not (path / dw.RESULT_NAME).exists()):
            out.append(path)
    assert len(out) <= _MAX_JOBS_LISTED, "bounded"
    return out


def _heartbeat(boot: str, stop: threading.Event) -> None:
    assert boot, "a boot id"
    assert isinstance(stop, threading.Event), "a stop flag"
    for _ in range(_MAX_LOOPS):  # bounded
        try:
            dw.write_json(dw.RENDER_ROOT, dw.HEARTBEAT_NAME, {"v": 1, "boot": boot, "ts": time.time()})
        except OSError:
            pass
        if stop.wait(HEARTBEAT_S):
            return


def serve() -> int:
    """Boot canary → READY.json → heartbeat + job loop (never returns while healthy)."""
    os.umask(0o007)
    m = mf.load(ENGINE)
    platform = install.platform_key()
    facts = boot_facts(m, platform)
    assert m.name == ENGINE and facts["netns"].startswith("net:["), "our engine, our namespace"
    dw.write_json(dw.RENDER_ROOT, dw.READY_NAME, facts)
    print(json.dumps({"ready": facts["ok"], "canaries": facts["canaries"]}), flush=True)
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(facts["boot"], stop), daemon=True).start()
    assert len(facts["boot"]) == 16, "a boot id for the heartbeat"
    seen: set[str] = set()
    order: collections.deque[str] = collections.deque()
    for _ in range(_MAX_LOOPS):  # bounded
        for run_dir in (pending(seen) if facts["ok"] else []):
            seen.add(run_dir.name)
            order.append(run_dir.name)
            if len(order) > _SEEN_CAP:
                seen.discard(order.popleft())
            try:
                process(run_dir, m, platform or "", facts["netns"])
            except OSError:
                continue  # the app removed the run dir before the answer could land
        time.sleep(POLL_S)
    stop.set()
    return 0


def install_engine() -> int:
    """Image build: install + verify the pinned engine with the app's own installer."""
    m = mf.load(ENGINE)
    assert m.name == ENGINE, "the sidecar's engine"
    platform = install.platform_key()
    if platform is None or install.install_blocker(m, platform):
        print(json.dumps({"install": "blocked"}), flush=True)
        return 1
    got = install.install(m, platform, self_test=lambda _m, _exe: "skipped(wall_unavailable)")
    ok, why = install.verify_installed(m.name, manifest_obj=m, platform=platform)
    assert isinstance(got, dict) and isinstance(ok, bool), "an install result"
    print(json.dumps({"install": got["code"] or "ok", "verified": ok, "why": why}), flush=True)
    return 0 if got["ok"] and ok else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    assert isinstance(args, list), "argv list"
    if args == ["--install"]:
        return install_engine()
    if args:
        return 2
    assert os.environ.get(AGENT_ENV) == "1", "the agent serves only inside the sidecar"
    return serve()


if __name__ == "__main__":
    sys.exit(main())
