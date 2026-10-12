"""Helpers for the SG canary harness (``tools/browsers-sg.py``; ni-format §35, P3 design §8).

Everything here drives the REAL engine under the REAL wall with the app's own process code
(``walls.prepare`` → ``Wall.wrap`` → ``proc.spawn`` → ``proc.watch`` with ``Wall.verify`` on
every poll → ``proc.kill_tree``). Only the command line is test-only: the canary page is a
``file://`` page in the run dir (the engine validates TLS, so a loopback https fixture can
never serve it — measured), and one run omits ``--proxy``. The runner itself still refuses
anything but https.

Also runnable as ``python3 tools/browsers_sg_lib.py --parent '<json>'``: the "parent"
process of the kill-parent row (it starts the engine under the wall, prints the pids, and
waits to be SIGKILLed).
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from smartbrain_3000.browsers import cmdline, jail_egress, proc, walls
from smartbrain_3000.browsers import manifest as mf
from smartbrain_3000.browsers.walls import base as wbase

PUBLIC_IP = "93.184.216.34"  # answered by the resolver seam; the dialer maps it to the sink
NAMES = {"canary.test": [PUBLIC_IP], "rebind.test": ["127.0.0.1"]}
_SG_RE = re.compile(r'data-sg="([^"]*)"')
_MAX_SAMPLES = 400
LISTEN_SINK: dict | None = None  # the harness points this at its accumulator


class Listeners:
    """The harness's canary targets: TCP on 127.0.0.1, ::1 and the LAN address (spare
    ports) plus 127.0.0.1 on each named port (the app port, Bifrost, a model server) where
    it can be bound. Every accepted connection is a hit."""

    def __init__(self, named_ports: dict[str, int]) -> None:
        self.hits: list[str] = []
        self.ports: dict[str, int] = {}
        self.hosts: dict[str, str] = {}
        self.unbound: list[str] = []
        self._socks: list[socket.socket] = []
        self._lock = threading.Lock()
        lan = wbase.lan_ip()
        plan = [("lo4", socket.AF_INET, "127.0.0.1", 0), ("lo6", socket.AF_INET6, "::1", 0)]
        plan += [("lan", socket.AF_INET, lan, 0)] if lan else []
        plan += [(name, socket.AF_INET, "127.0.0.1", port) for name, port in named_ports.items()]
        for name, family, host, port in plan:  # bounded: ≤ 6
            self._open(name, family, host, port)
        assert "lo4" in self.ports, "the loopback canary always listens"

    def _open(self, name: str, family: int, host: str, port: int) -> None:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            sock.listen(32)
        except OSError:
            sock.close()
            self.unbound.append(name)
            return
        self.ports[name], self.hosts[name] = int(sock.getsockname()[1]), host
        self._socks.append(sock)
        threading.Thread(target=self._serve, args=(name, sock), daemon=True).start()

    def _serve(self, name: str, sock: socket.socket) -> None:
        for _ in range(1000):  # bounded
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            conn.close()
            with self._lock:
                self.hits.append(name)

    def count(self) -> int:
        with self._lock:
            return len(self.hits)

    def close(self) -> None:
        for sock in self._socks:
            sock.close()


class Sink:
    """The control target behind the resolver seam: accepts and closes."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = int(self.sock.getsockname()[1])
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        for _ in range(1000):  # bounded
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self.sock.close()


def egress(kind: str, run_dir: str, sink: Sink) -> tuple[jail_egress.EgressServer, dict]:
    """The real egress server in this process, ``open`` policy, with the resolver seam
    (``canary.test`` → a public-looking address, ``rebind.test`` → loopback) and a dialer
    that reaches only the sink. Every answer still goes through ``netguard._is_unsafe``."""

    def resolve(host: str, port: int) -> list[str]:
        if host in NAMES:
            return list(NAMES[host])
        return [str(i[4][0]) for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)][:8]

    def dial(ip: str, _port: int, timeout: float) -> socket.socket:
        if ip != PUBLIC_IP:
            raise OSError("the harness dials only its sink")
        return socket.create_connection(("127.0.0.1", sink.port), timeout=timeout)

    cfg = {"listen": kind, "socket_path": os.path.join(run_dir, "egress.sock") if kind == "unix" else None,
           "policy": "open", "sites": [], "main_host": "canary.test", "main_port": 443,
           "max_life_s": 300, "caps": {}}
    server = jail_egress.EgressServer(cfg, resolver=resolve, dialer=dial)
    endpoint = server.listen()
    threading.Thread(target=server.serve, daemon=True).start()
    if kind == "unix":
        os.chmod(cfg["socket_path"], 0o600)
    return server, endpoint


def canary_page(token: str, targets: dict[str, str]) -> str:
    """fetch/img/WebSocket/beacon at every target, a localStorage/cookie probe for state
    left by an earlier run, an https control through the proxy, results in the DOM."""
    assert re.fullmatch(r"[a-z0-9]{8,40}", token), "a plain token"
    script = """(function(){
 var T=__TOKEN__, U=__TARGETS__, R={prior:null, cookie:'', attempts:0, settled:0, ran:false};
 try { R.prior = localStorage.getItem('sg'); localStorage.setItem('sg', T); } catch (e) { R.prior = 'n/a'; }
 try { R.cookie = document.cookie; document.cookie = 'sg=' + T; } catch (e) { R.cookie = 'n/a'; }
 function mark(){ document.title='SG '+R.settled+'/'+R.attempts; document.body.setAttribute('data-sg', JSON.stringify(R)); }
 function fin(){ R.settled++; mark(); }
 function f(u){ R.attempts++; try { fetch(u, {mode:'no-cors', cache:'no-store'}).then(fin, fin); } catch (e) { fin(); } }
 function img(u){ R.attempts++; var i=new Image(); i.onload=fin; i.onerror=fin; i.src=u; document.body.appendChild(i); }
 function ws(u){ R.attempts++; try { var w=new WebSocket(u); w.onopen=function(){ try{w.send('sg')}catch(e){}; fin(); }; w.onerror=fin; } catch (e) { fin(); } }
 function bc(u){ try { navigator.sendBeacon(u, 'sg'); } catch (e) {} }
 for (var k in U) { f(U[k]); }
 var L = U.lan || U.lo4;
 img(U.lo4); img(L); ws(U.lo4.replace('http','ws')); ws(L.replace('http','ws')); bc(U.lo4); bc(L);
 f('https://canary.test/control-' + T);
 R.ran = true; mark();
})();""".replace("__TOKEN__", json.dumps(token)).replace("__TARGETS__", json.dumps(targets))
    return ("<html><head><title>SG start</title></head><body><p>sg canary</p><script>"
            + script + "</script></body></html>")


def targets(lst: Listeners, token: str) -> dict[str, str]:
    """The SG target list (design §8): loopback forms, the LAN address, metadata, 0.0.0.0,
    the app/Bifrost/model ports, and the rebinding name (resolver seam, port 443)."""
    p4 = lst.ports["lo4"]
    host = socket.gethostname().lower()
    out = {"lo4": f"http://127.0.0.1:{p4}/sg-{token}", "localhost": f"http://localhost:{p4}/sg-{token}",
           "localdomain": f"http://localhost.localdomain:{p4}/sg-{token}",
           "hostname": f"http://{host}:{p4}/sg-{token}", "mapped6": f"http://[::ffff:127.0.0.1]:{p4}/sg-{token}",
           "decimal": f"http://2130706433:{p4}/sg-{token}", "zero": f"http://0.0.0.0:{p4}/sg-{token}",
           "https_lo4": f"https://127.0.0.1:{p4}/sg-{token}", "imds": "http://169.254.169.254/latest/meta-data/",
           "rebind": f"https://rebind.test/sg-{token}"}
    if "lo6" in lst.ports:
        out["lo6"] = f"http://[::1]:{lst.ports['lo6']}/sg-{token}"
    if "lan" in lst.ports:
        out["lan"] = f"http://{lst.hosts['lan']}:{lst.ports['lan']}/sg-{token}"
    for name in ("app", "gateway", "model"):
        if name in lst.ports:
            out[name] = f"http://127.0.0.1:{lst.ports[name]}/sg-{token}"
    return out


def make_run_dir(root: pathlib.Path) -> str:
    """A fresh 0700 canonical run dir with home/tmp/storage/out (the runner's layout)."""
    run = os.path.realpath(tempfile.mkdtemp(prefix="smartbrain-render-", dir=str(root)))
    for sub in ("home", "tmp", "storage", "out"):
        os.mkdir(os.path.join(run, sub), 0o700)
    assert os.stat(run).st_mode & 0o077 == 0, "the run dir is private"
    return run


def test_argv(m: mf.Manifest, engine: str, run_dir: str, url: str, port: int, *, proxy: bool,
              timeout_s: int) -> list[str]:
    """The manifest's fetch template shape with a test-only URL; ``proxy=False`` drops it."""
    argv = [engine, "--v8-flags", f"--max-old-space-size={m.limits.heap_mb}"]
    argv += ["--proxy", f"http://127.0.0.1:{port}"] if proxy else []
    return argv + ["--storage-dir", f"{run_dir}/storage", "fetch", url, "--timeout", str(timeout_s),
                   "--wait-until", "load", "--dump", "html", "--output", f"{run_dir}/out/page.html"]


def engine_pids(members: list[int], engine: str) -> list[int]:
    """The group members that ARE the engine (not the wall's helpers)."""
    real = os.path.realpath(engine)
    out = []
    for pid in members:
        exe = _exe(pid)
        if exe and os.path.realpath(exe) == real:
            out.append(pid)
    return out


def _exe(pid: int) -> str:
    if os.path.isdir("/proc/self"):
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return ""
    from smartbrain_3000.browsers.walls import darwin

    return darwin._proc_path(pid)


def listening(pids: list[int]) -> list[str]:
    """Listening TCP or bound UDP sockets held by ``pids`` (lsof on macOS, /proc on Linux)."""
    if not pids:
        return []
    if not os.path.isdir("/proc/self"):
        res = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(map(str, pids)), "-i"],
                             capture_output=True, text=True, timeout=10, check=False)
        return [ln for ln in res.stdout.splitlines()[1:] if "(LISTEN)" in ln or " UDP " in ln]
    found = []
    for pid in pids:
        inodes = set()
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
            for table in ("tcp", "tcp6", "udp", "udp6"):
                with open(f"/proc/{pid}/net/{table}") as fh:
                    for line in fh.read().splitlines()[1:]:
                        cols = line.split()
                        if cols[9] in inodes and (table.startswith("udp") or cols[3] == "0A"):
                            found.append(f"{pid}:{table}:{cols[1]}")
        except OSError:
            continue
    return found


def run_under_wall(platform: str, m: mf.Manifest, edir: pathlib.Path, run_dir: str,
                   argv_for: object, endpoint: dict, *, deadline_s: float,
                   kill_after_s: float | None = None) -> dict:
    """One real engine run under the real wall; ``argv_for(port)`` builds the argv."""
    wall = walls.prepare(platform, run_dir, str(edir), endpoint)
    assert isinstance(wall, walls.Wall), f"no wall: {wall}"
    engine = str(edir / m.executable)
    argv = argv_for(wall.proxy_port(endpoint))  # type: ignore[operator]
    env = cmdline.render_env(m, run_dir=run_dir, timezone="UTC", profile=0)
    argv, env = wall.wrap(argv, env)
    child = proc.spawn(argv, env, run_dir, cpu_s=int(4 * deadline_s), data_mb=m.limits.rlimit_data_mb)
    tap = proc.StderrTap(child)
    tap.start()
    seen: dict = {"listen": [], "samples": 0, "engine_seen": 0, "killed": False}
    stop = threading.Event()
    sampler = threading.Thread(target=_sample, args=(child, engine, seen, stop, kill_after_s), daemon=True)
    sampler.start()
    started = time.monotonic()
    try:
        verdict = proc.watch(child, wall, m.limits.rss_mb, deadline_s)
    finally:
        stop.set()
        sampler.join(5)
        left = proc.kill_tree(child)
        tap.join(5)
        wall.teardown()
    out, _over = proc.read_capped_file(os.path.join(run_dir, "out", "page.html"), 4_000_000)
    if LISTEN_SINK is not None:
        LISTEN_SINK["samples"] += seen["samples"]
        LISTEN_SINK["found"] += seen["listen"]
    return {"rc": child.returncode, "verdict": verdict, "signals": tap.signals(), "output": out,
            "secs": round(time.monotonic() - started, 2), "tree_clean": left, **seen}


def _sample(child: subprocess.Popen, engine: str, seen: dict, stop: threading.Event,
            kill_after_s: float | None) -> None:
    started = time.monotonic()
    for _ in range(_MAX_SAMPLES):  # bounded: ~40 s at 0.1 s
        if stop.is_set():
            return
        members = [pid for pid, _rss in proc.group_members(child.pid)]
        pids = engine_pids(members, engine)
        if pids:
            seen["engine_seen"] += 1
            seen["samples"] += 1
            seen["listen"] += listening(pids)
            if kill_after_s is not None and not seen["killed"] and time.monotonic() - started >= kill_after_s:
                for pid in pids:
                    os.kill(pid, signal.SIGKILL)
                seen["killed"] = True
        stop.wait(0.1)


def page_results(output: bytes | None) -> dict | None:
    """The page's ``data-sg`` JSON from the dumped DOM."""
    if not output:
        return None
    match = _SG_RE.search(output.decode("utf-8", "replace"))
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except ValueError:
        return None


def engine_processes(engine: str) -> list[int]:
    """Every live process running ``engine`` (/proc on Linux — slim images have no ps)."""
    out = []
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as fh:
                    argv = fh.read(8192).split(b"\0")
                with open(f"/proc/{name}/stat", "rb") as fh:
                    state = fh.read(512).rsplit(b")", 1)[1].split()[0]
            except (OSError, IndexError):
                continue
            if state != b"Z" and any(engine.encode() in a for a in argv):
                out.append(int(name))
        return [p for p in out if p != os.getpid()]
    res = subprocess.run(["ps", "-A", "-o", "pid=,stat=,command="], capture_output=True, text=True,
                         timeout=10, check=False)
    for line in res.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and not parts[1].startswith("Z") and engine in parts[2]:
            out.append(int(parts[0]))
    return [p for p in out if p != os.getpid()]


def parent_main(raw: str) -> None:
    """``--parent``: start the engine under the wall on a hanging page, print the pids,
    then wait to be SIGKILLed (the kill-parent row)."""
    cfg = json.loads(raw)
    m = mf.load(cfg["engine"])
    edir = pathlib.Path(cfg["edir"])
    run_dir = cfg["run_dir"]
    if cfg["platform"] == "darwin":
        hold = socket.socket()
        hold.bind(("127.0.0.1", 0))
        hold.listen(1)
        endpoint = {"kind": "tcp", "port": int(hold.getsockname()[1])}
    else:
        endpoint = {"kind": "unix", "path": os.path.join(run_dir, "egress.sock")}
    wall = walls.prepare(cfg["platform"], run_dir, str(edir), endpoint)
    assert isinstance(wall, walls.Wall), "a wall"
    argv = test_argv(m, str(edir / m.executable), run_dir, cfg["url"], wall.proxy_port(endpoint),
                     proxy=True, timeout_s=int(cfg["timeout_s"]))
    env = cmdline.render_env(m, run_dir=run_dir, timezone="UTC", profile=0)
    argv, env = wall.wrap(argv, env)
    child = proc.spawn(argv, env, run_dir, cpu_s=120, data_mb=0)
    print(json.dumps({"group": child.pid}), flush=True)
    time.sleep(120)


def cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def token() -> str:
    return secrets.token_hex(8)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--parent":
        parent_main(sys.argv[2])
