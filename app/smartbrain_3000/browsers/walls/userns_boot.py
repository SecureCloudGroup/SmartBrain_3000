"""Linux wall bootstrap (ni-format §35; P3 design §2) — the Linux wall runs it BY PATH (the
renderer sidecar's agent imports only its ``Relay``):

    python -I -S userns_boot.py '<config json>' [-- <engine argv…>]

Standard library only, and every import is at the top: after the tmpfs masks nothing under
/home, /tmp, /run … (site-packages included) is readable, so nothing may load lazily.

The bootstrap (B, in the caller's PID namespace) calls ``unshare(CLONE_NEWUSER |
CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID)``, maps only its own uid/gid (setgroups
denied), and forks the namespace init (C, PID 1 of the new PID namespace). C makes every
mount private, opens the engine and run dirs, mounts a size-capped tmpfs over each mask
(/home /root /run /tmp /var/tmp /dev/shm /mnt /media and the data dir), binds the engine
dir back read-only and the run dir read-write at their own paths, mounts a fresh /proc,
brings ``lo`` up, starts the relay 127.0.0.1:<relay_port> → the egress Unix socket in the
run dir, sets NO_NEW_PRIVS and runs the engine. The engine runs as the caller's own uid
(not root) inside the namespace, so exec leaves it with no capability there; it has no
route out except the relay. C reports the engine's exit code to B over a pipe, B mirrors
it (a signal death is re-raised). When C exits the kernel kills everything left in the
namespace.

Modes: ``engine`` (the above), ``probe`` (the setup only, then ``{"ok": true}``), and
``canary`` (the setup, capabilities dropped, then the canary rows as one JSON line; it holds
until stdin closes so the caller can verify the live tree). A setup failure exits 125 and
never execs the engine; probe/canary modes say where it failed: ``{"ok": false, "stage",
"errno"}``.
"""

import ctypes
import errno
import fcntl
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading

CLONE_NEWNS, CLONE_NEWUSER = 0x00020000, 0x10000000
CLONE_NEWPID, CLONE_NEWNET = 0x20000000, 0x40000000
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 0x1, 0x2, 0x4, 0x8
MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 0x20, 0x1000, 0x4000, 0x40000
MS_NOATIME, MS_NODIRATIME, MS_RELATIME, MS_STRICTATIME = 0x400, 0x800, 0x200000, 0x1000000
PR_SET_PDEATHSIG, PR_SET_NO_NEW_PRIVS = 1, 38
SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP = 0x8913, 0x8914, 0x1
EXIT_SETUP = 125
MODES = ("engine", "probe", "canary")
CONFIG_KEYS = frozenset({"v", "mode", "engine_dir", "run_dir", "socket", "relay_port", "masks",
                         "uid", "gid", "canary"})
CANARY_KEYS = frozenset({"tcp4", "tcp6", "udp4", "lan", "inside", "inside_text", "home_file",
                         "app_userns", "app_netns", "hold_s"})
_MAX_CONFIG = 16384
_MAX_MASKS = 16
_MAX_RELAY_CONNS = 512
_RELAY_IDLE_S = 30.0
_RELAY_STEPS = 1 << 20
_TMPFS_OPTS = b"size=64m,mode=0755"

_libc = ctypes.CDLL(None, use_errno=True)


class SetupError(Exception):
    """A wall setup step failed; ``stage`` names it, ``code`` is the errno."""

    def __init__(self, stage: str, code: int) -> None:
        assert stage, "a stage name"
        assert isinstance(code, int), "an errno"
        super().__init__(stage)
        self.stage, self.code = stage, code


def _call(stage: str, result: int) -> None:
    """libc returned ``result``; -1 is a ``SetupError`` with the errno."""
    assert isinstance(result, int), "libc returns an int"
    assert stage, "a stage name"
    if result != 0:
        raise SetupError(stage, ctypes.get_errno() or errno.EPERM)


def parse(argv: list[str]) -> tuple[dict, list[str]]:
    """``<config json> [-- engine argv…]`` → (config, engine argv). Closed keys, checked."""
    assert isinstance(argv, list), "argv list"
    if not argv or len(argv[0]) > _MAX_CONFIG:
        raise ValueError("config")
    cfg = json.loads(argv[0])
    engine = argv[2:] if len(argv) > 1 and argv[1] == "--" else []
    if not isinstance(cfg, dict) or set(cfg) != CONFIG_KEYS or cfg["v"] != 1:
        raise ValueError("config keys")
    if cfg["mode"] not in MODES or (cfg["mode"] == "engine") != bool(engine):
        raise ValueError("config mode")
    paths = [cfg["engine_dir"], cfg["run_dir"], *cfg["masks"]]
    if len(cfg["masks"]) > _MAX_MASKS or not all(isinstance(p, str) and p.startswith("/") for p in paths):
        raise ValueError("config paths")
    if cfg["socket"] is not None and not str(cfg["socket"]).startswith(cfg["run_dir"] + "/"):
        raise ValueError("config socket")
    if not isinstance(cfg["relay_port"], int) or not 1 <= cfg["relay_port"] <= 65535:
        raise ValueError("config port")
    if (cfg["mode"] == "canary") != isinstance(cfg["canary"], dict):
        raise ValueError("config canary")
    if cfg["mode"] == "canary" and set(cfg["canary"]) != CANARY_KEYS:
        raise ValueError("config canary keys")
    assert isinstance(engine, list), "engine argv"
    return cfg, engine


def enter_namespaces(uid: int, gid: int) -> None:
    """unshare USER|NS|NET|PID, then map exactly our own uid and gid."""
    assert uid > 0 and gid >= 0, "never root: exec must leave the engine without capabilities"
    assert os.getuid() == uid, "the map names our own uid"
    _call("unshare", _libc.unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID))
    for name, text in (("setgroups", "deny"), ("gid_map", f"{gid} {gid} 1"),
                       ("uid_map", f"{uid} {uid} 1")):
        try:
            with open(f"/proc/self/{name}", "w") as fh:
                fh.write(text)
        except OSError as exc:
            raise SetupError(name, exc.errno or errno.EPERM) from None


def _mount(stage: str, source: bytes | None, target: str, fstype: bytes | None, flags: int,
           data: bytes | None = None) -> None:
    assert target.startswith("/"), "absolute mount target"
    assert flags >= 0, "mount flags"
    _call(stage, _libc.mount(source, target.encode(), fstype, ctypes.c_ulong(flags), data))


def kept_flags(f_flag: int) -> int:
    """The mount flags a bind remount must repeat. A mount copied into a user namespace
    has its atime mode and any nosuid/nodev/noexec/ro LOCKED: a remount that differs is
    EPERM (e.g. a home on a noatime filesystem), so they are carried over from statvfs."""
    assert isinstance(f_flag, int) and f_flag >= 0, "statvfs flags"
    out = MS_NOSUID | MS_NODEV  # adding these is always allowed
    pairs = ((os.ST_RDONLY, MS_RDONLY), (os.ST_NOEXEC, MS_NOEXEC), (os.ST_NOATIME, MS_NOATIME),
             (os.ST_NODIRATIME, MS_NODIRATIME), (os.ST_RELATIME, MS_RELATIME))
    for st_bit, ms_bit in pairs:  # bounded: five pairs
        if f_flag & st_bit:
            out |= ms_bit
    if not f_flag & (os.ST_NOATIME | os.ST_RELATIME):
        out |= MS_STRICTATIME
    assert out & MS_NOSUID, "nosuid is always set"
    return out


def _bind(fd: int, target: str, readonly: bool) -> None:
    """Bind an fd opened before the masks back at its own path; the engine dir then gets a
    read-only remount that keeps the source's locked flags (the run dir needs none)."""
    assert fd >= 0, "an open directory"
    assert target.startswith("/"), "absolute target"
    os.makedirs(target, mode=0o700, exist_ok=True)
    _mount("bind", f"/proc/self/fd/{fd}".encode(), target, None, MS_BIND)
    if readonly:
        flags = MS_REMOUNT | MS_BIND | MS_RDONLY | kept_flags(os.statvfs(target).f_flag)
        _mount("bind_ro", None, target, None, flags)


def build_mounts(cfg: dict) -> None:
    """Private mounts, the tmpfs masks, the engine (ro) and run dir (rw) bound back, a
    fresh /proc for the new PID namespace, and ``lo`` up."""
    assert os.getpid() == 1, "the namespace init builds the mounts"
    assert cfg["masks"], "masks required"
    keep = {path: os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
            for path in (cfg["engine_dir"], cfg["run_dir"])}
    _mount("private", None, "/", None, MS_REC | MS_PRIVATE)
    for mask in cfg["masks"]:  # bounded: ≤ 16
        if os.path.isdir(mask):
            _mount("mask", b"tmpfs", mask, b"tmpfs", MS_NOSUID | MS_NODEV, _TMPFS_OPTS)
    _bind(keep[cfg["engine_dir"]], cfg["engine_dir"], readonly=True)
    _bind(keep[cfg["run_dir"]], cfg["run_dir"], readonly=False)
    for fd in keep.values():
        os.close(fd)
    _mount("proc", b"proc", "/proc", b"proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    _lo_up()


def _lo_up() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        flags = struct.unpack("16sH22x", fcntl.ioctl(sock, SIOCGIFFLAGS,
                                                     struct.pack("16sH22x", b"lo", 0)))[1]
        fcntl.ioctl(sock, SIOCSIFFLAGS, struct.pack("16sH22x", b"lo", flags | IFF_UP))
    except OSError as exc:
        raise SetupError("lo", exc.errno or errno.EPERM) from None
    finally:
        sock.close()
    assert struct.calcsize("16sH22x") == 40, "struct ifreq is 40 bytes"
    assert flags >= 0, "interface flags"


class Relay:
    """127.0.0.1:<port> in the new netns → the egress Unix socket; one thread per tunnel."""

    def __init__(self, port: int, path: str) -> None:
        assert 1 <= port <= 65535, "relay port"
        assert path.startswith("/"), "absolute socket path"
        self.path = path
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.listener.bind(("127.0.0.1", port))
            self.listener.listen(64)
        except OSError as exc:
            raise SetupError("relay", exc.errno or errno.EADDRINUSE) from None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        assert self.listener is not None, "listening"
        for _ in range(_MAX_RELAY_CONNS):  # bounded; the egress child caps tunnels anyway
            try:
                conn, _addr = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._pump, args=(conn,), daemon=True).start()
        assert _MAX_RELAY_CONNS > 0, "bounded accept loop"

    def _pump(self, conn: socket.socket) -> None:
        assert conn is not None, "a client"
        assert self.path.startswith("/"), "absolute socket path"
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(self.path)
            pair = {conn: upstream, upstream: conn}
            for _ in range(_RELAY_STEPS):  # bounded: every step moves bytes or ends
                ready, _, _ = select.select(list(pair), [], [], _RELAY_IDLE_S)
                if not ready:
                    return
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    pair[src].sendall(data)
        except OSError:
            return
        finally:
            for sock in (conn, upstream):
                sock.close()

    def close(self) -> None:
        """Stop accepting (open tunnels end with the egress socket or their idle bound).
        ``shutdown`` first: on Linux a listening socket closed under a blocked ``accept``
        stays bound until that call returns, so the next relay could not bind (measured)."""
        assert self.listener is not None, "listening"
        for stop in (lambda: self.listener.shutdown(socket.SHUT_RDWR), self.listener.close):
            try:
                stop()
            except OSError:
                pass
        assert self.listener.fileno() == -1, "the listener is closed"


def run_engine(cfg: dict, argv: list[str], report_fd: int) -> int:
    """Exec the engine as a child of the namespace init; write its returncode to B."""
    assert argv and argv[0].startswith("/"), "absolute engine path"
    assert report_fd >= 0, "report pipe"
    _call("no_new_privs", _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, cwd=cfg["run_dir"], env=dict(os.environ),
                             close_fds=True)
    code = child.wait()  # bounded by the caller's watchdog, which kills the whole group
    os.write(report_fd, f"{code}\n".encode())
    return code


def drop_capabilities() -> None:
    """capset(all zero) for this thread + NO_NEW_PRIVS: the canary runs like the engine."""
    header = (ctypes.c_uint32 * 2)(0x20080522, 0)  # _LINUX_CAPABILITY_VERSION_3, this thread
    data = (ctypes.c_uint32 * 6)()
    assert ctypes.sizeof(data) == 24, "two v3 capability structs"
    _call("capset", _libc.capset(ctypes.byref(header), ctypes.byref(data)))
    _call("no_new_privs", _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
    with open("/proc/thread-self/status") as fh:
        caps = [line for line in fh.read().splitlines() if line.startswith("CapEff:")]
    assert caps and int(caps[0].split()[1], 16) == 0, "no effective capability is left"


def _connect_row(name: str, family: int, address: tuple) -> dict:
    """A TCP connect that must FAIL from inside the namespace."""
    assert name and address, "a named target"
    assert family in (socket.AF_INET, socket.AF_INET6), "an IP family"
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(address)
    except OSError as exc:
        return {"row": name, "ok": True, "detail": errno.errorcode.get(exc.errno or 0, "timeout")}
    finally:
        sock.close()
    return {"row": name, "ok": False, "detail": "connected"}


def canary_rows(cfg: dict) -> list[dict]:
    """The canary program (capabilities dropped, inside every namespace)."""
    can = cfg["canary"]
    assert set(can) == CANARY_KEYS, "closed canary config"
    rows = [_connect_row("loopback_tcp", socket.AF_INET, ("127.0.0.1", can["tcp4"])),
            _connect_row("lan_tcp", socket.AF_INET, (can["lan"] or "192.168.0.1", can["tcp4"])),
            _connect_row("metadata", socket.AF_INET, ("169.254.169.254", 80))]
    if can["tcp6"]:
        rows.append(_connect_row("loopback_v6", socket.AF_INET6, ("::1", can["tcp6"])))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.sendto(b"canary", ("127.0.0.1", can["udp4"]))
        rows.append({"row": "udp", "ok": True, "detail": "sent into the private netns"})
    except OSError as exc:
        rows.append({"row": "udp", "ok": True, "detail": errno.errorcode.get(exc.errno, "")})
    finally:
        udp.close()
    try:
        socket.getaddrinfo("example.com", 443, type=socket.SOCK_STREAM)
        rows.append({"row": "dns", "ok": False, "detail": "resolved"})
    except OSError as exc:
        rows.append({"row": "dns", "ok": True, "detail": str(exc.errno)})
    rows += _file_rows(can) + [_proxy_row(cfg), _ns_row(can, cfg["uid"])]
    assert len(rows) <= 16, "bounded rows"
    return rows


def _file_rows(can: dict) -> list[dict]:
    assert can["home_file"].startswith("/"), "absolute canary path"
    assert can["inside"].startswith("/"), "absolute inside path"
    try:
        with open(can["home_file"], "rb") as fh:
            fh.read(64)
        home = {"row": "home_read", "ok": False, "detail": "read"}
    except OSError as exc:
        home = {"row": "home_read", "ok": True, "detail": errno.errorcode.get(exc.errno, "")}
    try:
        with open(can["inside"]) as fh:
            inside_ok = fh.read(4096) == can["inside_text"]
    except OSError:
        inside_ok = False
    return [home, {"row": "run_read", "ok": inside_ok, "detail": ""}]


def _proxy_row(cfg: dict) -> dict:
    """The relay reaches THE egress child: a CONNECT to a literal is refused with 403."""
    assert cfg["socket"], "the canary needs the egress socket"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    try:
        sock.connect(("127.0.0.1", cfg["relay_port"]))
        sock.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        head = sock.recv(64)
    except OSError as exc:
        return {"row": "proxy", "ok": False, "detail": errno.errorcode.get(exc.errno or 0, "")}
    finally:
        sock.close()
    assert isinstance(head, bytes), "a reply"
    return {"row": "proxy", "ok": head.startswith(b"HTTP/1.1 403"), "detail": ""}


def _ns_row(can: dict, uid: int) -> dict:
    """Different user and network namespaces, a fresh /proc, our uid, a Seccomp field."""
    assert uid > 0, "a mapped uid"
    assert set(can) == CANARY_KEYS, "closed canary config"
    user, net = os.readlink("/proc/self/ns/user"), os.readlink("/proc/self/ns/net")
    with open("/proc/self/status") as fh:
        fields = dict(line.split(":", 1) for line in fh.read().splitlines() if ":" in line)
    procs = [name for name in os.listdir("/proc") if name.isdigit()]
    ok = (user != can["app_userns"] and net != can["app_netns"] and os.getuid() == uid
          and int(fields.get("Seccomp", "-1").strip() or -1) >= 0 and len(procs) <= 2)
    return {"row": "ns_facts", "ok": ok, "detail": f"procs={len(procs)}"}


def init_main(cfg: dict, argv: list[str], report_fd: int) -> int:
    """PID 1 of the new namespace: mounts, relay, then the mode's work."""
    assert os.getpid() == 1, "running as the namespace init"
    assert cfg["mode"] in MODES, "a known mode"
    _call("pdeathsig", _libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0))
    build_mounts(cfg)
    if cfg["socket"] is not None:
        Relay(cfg["relay_port"], cfg["socket"])
    if cfg["mode"] == "probe":
        _emit({"ok": True})
        return 0
    if cfg["mode"] == "canary":
        drop_capabilities()
        _emit({"ok": True, "rows": canary_rows(cfg)})
        select.select([0], [], [], float(min(30, cfg["canary"]["hold_s"])))  # caller verifies
        return 0
    return run_engine(cfg, argv, report_fd)


def _emit(obj: dict) -> None:
    assert isinstance(obj, dict) and "ok" in obj, "closed result line"
    assert sys.stdout is not None, "stdout is the caller's pipe"
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _mirror(status: int, report_r: int) -> None:
    """Exit like the engine did: its code, or re-raise the signal that killed it."""
    assert status >= 0 and report_r >= 0, "a wait status and the pipe"
    text = os.read(report_r, 32).decode("ascii", "replace").strip()
    assert isinstance(text, str), "the init's report"
    if not text.lstrip("-").isdigit():
        os._exit(os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else EXIT_SETUP)
    code = int(text)
    if code < 0:
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    os._exit(code if 0 <= code <= 255 else EXIT_SETUP)


def main() -> None:
    """B: parse, enter the namespaces, fork the init, mirror its outcome. Any failure
    before the engine is exec'd is a setup failure (exit 125, nothing ran)."""
    assert sys.argv, "an argv"
    try:
        cfg, argv = parse(sys.argv[1:])
        enter_namespaces(int(cfg["uid"]), int(cfg["gid"]))
    except SetupError as exc:
        _emit({"ok": False, "stage": exc.stage, "errno": exc.code})
        os._exit(EXIT_SETUP)
    except Exception:  # a malformed config (ValueError/TypeError/KeyError/AssertionError)
        _emit({"ok": False, "stage": "config", "errno": 0})
        os._exit(EXIT_SETUP)
    report_r, report_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(report_r)
        try:
            init_main(cfg, argv, report_w)
            os._exit(0)
        except SetupError as exc:
            _emit({"ok": False, "stage": exc.stage, "errno": exc.code})
        except Exception:
            _emit({"ok": False, "stage": "init", "errno": 0})
        os._exit(EXIT_SETUP)
    os.close(report_w)
    assert pid > 0, "the namespace init was forked"
    _pid, status = os.waitpid(pid, 0)
    _mirror(status, report_r)


if __name__ == "__main__":
    main()
