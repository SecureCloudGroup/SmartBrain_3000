"""Egress child for browser components (ni-format §35; P3 design §2) — the engine's ONLY
network route. It holds no key and runs under jail discipline (``egress.start`` spawns it
with ``jailrun``'s stripped env, no proxy variables, its own session; this side sets
RLIMIT_CPU/RLIMIT_CORE and talks closed-key JSON on stdout).

Protocol: one config line on stdin (closed keys) → one ``{"endpoint": ...}`` line on
stdout → serve until stdin reaches EOF (the parent's lifeline) or ``max_life_s`` passes →
one ``{"census": [...], "dropped": n}`` line → exit.

Rules, per tunnel:

- CONNECT only, request head ≤ 8 KB. Anything else (a plain ``GET http://…``) is refused,
  so rendering is HTTPS-only by construction.
- Hostnames only: IP literals (and all-numeric "hosts" a resolver would read as one) are
  refused. Port 443, or the render URL's explicit port for that exact host.
- Site policy by registrable domain (``pagegraph.registrable_domain``, the app's
  conservative public-suffix table): ``own`` = the URL's site, ``sealed`` = the listed
  sites, ``open`` = any public site. Checked BEFORE any DNS, so a refused site is never
  even resolved.
- Resolve once per host per run on a bounded thread (3 s); refuse the host if ANY answer
  fails ``netguard._is_unsafe`` (imported, unchanged); dial only a validated address.
- Caps per run: 24 concurrent tunnels, 150 tunnels, 24 distinct sites, 16 MB down,
  1 MB up, 15 s idle per tunnel (a config may only tighten them).

Census rows ``{site, tunnels, up, down, verdict}`` (≤ 64) go to the parent on exit and
nowhere else: this process writes no log, and hostnames never reach the app log.
"""

from __future__ import annotations

import json
import os
import select
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from ..netguard import _is_unsafe
from ..pagegraph import registrable_domain

CAPS = {"concurrent": 24, "tunnels": 150, "sites": 24, "down": 16 * 1024 * 1024,
        "up": 1024 * 1024, "idle_s": 15.0}
HEAD_CAP = 8192
RESOLVE_S = 3.0
DIAL_S = 10.0
MAX_ROWS = 64
MAX_LIFE_S = 600.0
VERDICTS = frozenset({"ok", "method", "bad_request", "bad_host", "ip_literal", "port",
                      "offsite", "cap_tunnels", "cap_concurrent", "cap_sites", "resolve",
                      "resolve_timeout", "unsafe", "connect", "cap_up", "cap_down"})
POLICIES = ("own", "open", "sealed")
CONFIG_KEYS = frozenset({"listen", "socket_path", "policy", "sites", "main_host", "main_port",
                         "max_life_s", "caps"})
_CONFIG_CAP = 16 * 1024
_RECV = 65536
_MAX_ANSWERS = 32
_MAX_DIALS = 4
_MAX_SITES_LISTED = 12
_RLIMIT_CPU_S = 300
_INVALID_SITE = "(invalid)"
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")

Resolver = Callable[[str, int], list[str]]
Dialer = Callable[[str, int, float], socket.socket]


def _system_resolve(host: str, port: int) -> list[str]:
    assert host and 1 <= port <= 65535, "host and port required"
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    assert isinstance(infos, list), "getaddrinfo returns a list"
    return [str(info[4][0]) for info in infos[:_MAX_ANSWERS]]


def _system_dial(ip: str, port: int, timeout: float) -> socket.socket:
    assert ip and 1 <= port <= 65535, "address required"
    assert timeout > 0, "dial timeout must be positive"
    return socket.create_connection((ip, port), timeout=timeout)


class _Refusal(Exception):
    def __init__(self, verdict: str, site: str) -> None:
        assert verdict in VERDICTS and verdict != "ok", "refusals carry a refusal verdict"
        assert isinstance(site, str) and site, "refusals name a site"
        super().__init__(verdict)
        self.verdict = verdict
        self.site = site


def validate_config(config: object) -> dict:
    """The closed-key config line; raises ``ValueError`` on any deviation."""
    assert CONFIG_KEYS and set(CAPS) == {"concurrent", "tunnels", "sites", "down", "up",
                                         "idle_s"}, "config schema is closed"
    if not isinstance(config, dict) or set(config) != CONFIG_KEYS:
        raise ValueError("egress config keys")
    if config["listen"] not in ("tcp", "unix") or config["policy"] not in POLICIES:
        raise ValueError("egress listen/policy")
    if config["listen"] == "unix" and not (isinstance(config["socket_path"], str)
                                           and os.path.isabs(config["socket_path"])):
        raise ValueError("egress socket path")
    sites = config["sites"]
    if (not isinstance(sites, list) or len(sites) > _MAX_SITES_LISTED
            or not all(isinstance(s, str) and s and set(s) <= _HOST_CHARS for s in sites)):
        raise ValueError("egress sites")
    if config["policy"] == "sealed" and not sites:
        raise ValueError("sealed policy needs sites")
    host, port = config["main_host"], config["main_port"]
    if not isinstance(host, str) or not host or set(host) - _HOST_CHARS:
        raise ValueError("egress main host")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("egress main port")
    life = config["max_life_s"]
    if not isinstance(life, (int, float)) or not 1 <= life <= MAX_LIFE_S:
        raise ValueError("egress max life")
    caps = config["caps"]
    if not isinstance(caps, dict) or set(caps) - set(CAPS):
        raise ValueError("egress caps keys")
    for key, value in caps.items():  # bounded: ≤ 6 keys; a config may only tighten
        if not isinstance(value, (int, float)) or not 0 < value <= CAPS[key]:
            raise ValueError("egress caps may only tighten")
    assert config["policy"] in POLICIES, "policy is closed"
    return config


class EgressServer:
    """The CONNECT proxy. ``resolver``/``dialer`` default to the system; tests inject a
    resolver that answers public-looking addresses and a dialer that reaches a local
    target, so the real ``_is_unsafe`` check still runs on every answer."""

    def __init__(self, config: dict, *, resolver: Resolver | None = None,
                 dialer: Dialer | None = None) -> None:
        cfg = validate_config(config)
        assert cfg is config, "config validated in place"
        self._cfg = cfg
        self._caps = {**CAPS, **cfg["caps"]}
        self._main_host = cfg["main_host"]
        self._main_site = registrable_domain(cfg["main_host"])
        self._sites = frozenset(cfg["sites"]) if cfg["policy"] == "sealed" else frozenset()
        self._resolve = resolver or _system_resolve
        self._dial = dialer or _system_dial
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._dropped = 0
        self._active = 0
        self._tunnels = 0
        self._allowed_sites: set[str] = set()
        self._up = 0
        self._down = 0
        self._dns: dict[str, tuple[str, list[str]]] = {}
        self._open: set[socket.socket] = set()
        self._closing = threading.Event()
        self._listener: socket.socket | None = None
        assert self._main_site, "the main host has a site"

    # --- listening -----------------------------------------------------------------

    def listen(self) -> dict:
        """Bind (127.0.0.1 ephemeral port, or the Unix socket in the 0700 run dir)."""
        assert self._listener is None, "listen once"
        if self._cfg["listen"] == "unix":
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self._cfg["socket_path"])
            os.chmod(self._cfg["socket_path"], 0o600)
            endpoint = {"kind": "unix", "path": self._cfg["socket_path"]}
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            endpoint = {"kind": "tcp", "port": int(sock.getsockname()[1])}
        sock.listen(64)
        self._listener = sock
        assert endpoint["kind"] in ("tcp", "unix"), "endpoint kind is closed"
        return endpoint

    def serve(self) -> None:
        """Accept loop (bounded); one handler thread per connection."""
        listener = self._listener
        assert listener is not None, "listen() first"
        assert self._caps["tunnels"] > 0, "tunnel cap must be positive"
        for _ in range(int(self._caps["tunnels"]) * 4):  # fixed bound on accepted sockets
            try:
                conn, _addr = listener.accept()
            except OSError:
                return  # closed by close()
            if self._closing.is_set():
                conn.close()
                return
            with self._lock:
                self._open.add(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        listener.close()

    def close(self) -> None:
        """Stop accepting and cut every open socket (unblocks every relay)."""
        assert isinstance(self._closing, threading.Event), "closing flag required"
        self._closing.set()
        if self._listener is not None:
            for stop in (lambda: self._listener.shutdown(socket.SHUT_RDWR), self._listener.close):
                try:  # shutdown wakes a blocked accept() on Linux; close releases the fd
                    stop()
                except OSError:
                    pass
        with self._lock:
            sockets = list(self._open)
        for sock in sockets:  # bounded by the concurrent cap
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        assert self._closing.is_set(), "closing flag set"

    def census(self) -> dict:
        """``{"census": rows (≤ 64, first-seen order), "dropped": rows that did not fit}``."""
        with self._lock:
            rows = [dict(r) for r in self._rows.values()]
            dropped = self._dropped
        assert len(rows) <= MAX_ROWS, "census rows are capped"
        assert dropped >= 0, "dropped count is non-negative"
        return {"census": rows, "dropped": dropped}

    # --- one connection ------------------------------------------------------------

    def _handle(self, conn: socket.socket) -> None:
        assert isinstance(conn, socket.socket), "a client socket"
        assert self._listener is not None, "serving"
        upstream: socket.socket | None = None
        reserved = False
        try:
            conn.settimeout(float(self._caps["idle_s"]))
            head, rest = _read_head(conn)
            host, port = _parse_connect(head)
            site = self._policy_site(host, port)
            self._reserve(site)
            reserved = True
            ips = self._validated(host, port, site)
            upstream = self._dial_first(ips, port, site)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self._row_update(site, tunnels=1)
            if rest and self._spend("up", len(rest), site):
                upstream.sendall(rest)
            self._relay(conn, upstream, site)
        except _Refusal as refusal:
            self._row_update(refusal.site, verdict=refusal.verdict)
            _reply(conn, 405 if refusal.verdict == "method" else 403)
        except OSError:
            pass  # a reset or timeout ends this tunnel only
        finally:
            for sock in (conn, upstream):
                if sock is not None:
                    _close(sock)
            with self._lock:
                self._open.discard(conn)
                if reserved:
                    self._active -= 1

    def _policy_site(self, host: str, port: int) -> str:
        """Port rule, then site policy — both before any DNS."""
        assert host and port, "host and port required"
        site = registrable_domain(host) or _INVALID_SITE
        if port != 443 and not (host == self._main_host and port == self._cfg["main_port"]):
            raise _Refusal("port", site)
        policy = self._cfg["policy"]
        if policy == "own" and site != self._main_site:
            raise _Refusal("offsite", site)
        if policy == "sealed" and site not in self._sites:
            raise _Refusal("offsite", site)
        assert site, "an allowed site is named"
        return site

    def _reserve(self, site: str) -> None:
        """Count caps under the lock: total tunnels, concurrent tunnels, distinct sites."""
        assert site, "site required"
        with self._lock:
            if self._tunnels >= self._caps["tunnels"]:
                raise _Refusal("cap_tunnels", site)
            if self._active >= self._caps["concurrent"]:
                raise _Refusal("cap_concurrent", site)
            if site not in self._allowed_sites and len(self._allowed_sites) >= self._caps["sites"]:
                raise _Refusal("cap_sites", site)
            self._tunnels += 1
            self._active += 1
            self._allowed_sites.add(site)
            assert self._active <= self._caps["concurrent"], "concurrency cap holds"

    def _validated(self, host: str, port: int, site: str) -> list[str]:
        """Resolve once per host per run (bounded thread); every answer must be safe."""
        assert host and 1 <= port <= 65535, "host and port required"
        with self._lock:
            cached = self._dns.get(host)
        if cached is None:
            box: dict[str, list[str]] = {}

            def work() -> None:
                assert host, "host required"
                assert not box, "one resolution per worker"
                try:
                    box["ips"] = list(self._resolve(host, port))[:_MAX_ANSWERS]
                except (OSError, UnicodeError, ValueError):
                    box["ips"] = []

            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            worker.join(RESOLVE_S)
            cached = _judge(box.get("ips"), worker.is_alive())
            with self._lock:
                self._dns.setdefault(host, cached)
        verdict, ips = cached
        if verdict != "ok":
            raise _Refusal(verdict, site)
        assert ips, "a safe answer set is never empty"
        return ips

    def _dial_first(self, ips: list[str], port: int, site: str) -> socket.socket:
        """Dial a validated address — never re-resolve, never a name."""
        assert ips, "validated addresses required"
        assert 1 <= port <= 65535, "port required"
        for ip in ips[:_MAX_DIALS]:  # bounded
            try:
                sock = self._dial(ip, port, DIAL_S)
            except OSError:
                continue
            with self._lock:
                self._open.add(sock)
            return sock
        raise _Refusal("connect", site)

    def _relay(self, conn: socket.socket, upstream: socket.socket, site: str) -> None:
        """Pump bytes both ways until EOF, idle, a cap, or close()."""
        assert conn is not None and upstream is not None, "both ends required"
        assert site, "the tunnel's site is named"
        sel = selectors.DefaultSelector()
        sel.register(conn, selectors.EVENT_READ, "up")
        sel.register(upstream, selectors.EVENT_READ, "down")
        steps = int(self._caps["up"]) + int(self._caps["down"]) + 2
        try:
            for _ in range(steps):  # fixed bound: every step spends ≥ 1 budget byte or ends
                if self._closing.is_set():
                    return
                events = sel.select(timeout=float(self._caps["idle_s"]))
                if not events:
                    return  # idle
                for key, _mask in events:
                    if not self._pump(key, conn, upstream, site):
                        return
        finally:
            sel.close()
            with self._lock:
                self._open.discard(upstream)

    def _pump(self, key: selectors.SelectorKey, conn: socket.socket,
              upstream: socket.socket, site: str) -> bool:
        direction = key.data
        assert direction in ("up", "down"), "direction is closed"
        assert site, "the tunnel's site is named"
        src, dst = (conn, upstream) if direction == "up" else (upstream, conn)
        data = src.recv(_RECV)
        if not data:
            return False
        if not self._spend(direction, len(data), site):
            return False
        dst.sendall(data)
        return True

    def _spend(self, direction: str, size: int, site: str) -> bool:
        """Charge ``size`` bytes against the run's budget; False (and the verdict) on a cap."""
        assert direction in ("up", "down") and size >= 0, "direction + size required"
        assert site, "the tunnel's site is named"
        with self._lock:
            total = (self._up if direction == "up" else self._down) + size
            if total > self._caps[direction]:
                row = self._rows.get(site)
                if row is not None and row["verdict"] == "ok":
                    row["verdict"] = f"cap_{direction}"
                return False
            if direction == "up":
                self._up = total
            else:
                self._down = total
            row = self._rows.get(site)
            if row is not None:
                row[direction] += size
        return True

    def _row_update(self, site: str, *, tunnels: int = 0, verdict: str = "ok") -> None:
        assert verdict in VERDICTS, "verdict is closed"
        assert tunnels >= 0, "tunnel count is non-negative"
        with self._lock:
            row = self._rows.get(site)
            if row is None:
                if len(self._rows) >= MAX_ROWS:
                    self._dropped += 1
                    return
                row = {"site": site[:253], "tunnels": 0, "up": 0, "down": 0, "verdict": "ok"}
                self._rows[site] = row
            row["tunnels"] += tunnels
            if verdict != "ok" and row["verdict"] == "ok":
                row["verdict"] = verdict


def _judge(ips: list[str] | None, timed_out: bool) -> tuple[str, list[str]]:
    """A resolution's verdict: every answer must be a public address."""
    assert ips is None or isinstance(ips, list), "answers are a list"
    if timed_out:
        return "resolve_timeout", []
    if not ips:
        return "resolve", []
    unique = sorted(set(ips))
    for ip in unique:  # bounded by _MAX_ANSWERS
        try:
            if _is_unsafe(ip):
                return "unsafe", []
        except ValueError:
            return "unsafe", []
    assert unique, "safe answers are non-empty"
    return "ok", unique


def _read_head(conn: socket.socket) -> tuple[bytes, bytes]:
    """The request head (≤ 8 KB) and any bytes the client already sent after it."""
    assert HEAD_CAP > 0, "head cap must be positive"
    assert conn is not None, "client socket required"
    buf = b""
    for _ in range(HEAD_CAP + 1):  # every recv adds ≥ 1 byte or ends
        chunk = conn.recv(HEAD_CAP + 1 - len(buf))
        if not chunk:
            break
        buf += chunk
        if b"\r\n\r\n" in buf:
            head, _sep, rest = buf.partition(b"\r\n\r\n")
            return head, rest
        if len(buf) > HEAD_CAP:
            break
    raise _Refusal("bad_request", _INVALID_SITE)


def _parse_connect(head: bytes) -> tuple[str, int]:
    """``CONNECT host:port HTTP/1.x`` → (host, port); everything else is a refusal."""
    assert isinstance(head, bytes), "head must be bytes"
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _Refusal("bad_request", _INVALID_SITE)
    method, target = parts[0], parts[1]
    if method != "CONNECT":
        raise _Refusal("method", _plain_site(target))
    if target.startswith("["):
        raise _Refusal("ip_literal", target[:64])
    host, sep, port_text = target.rpartition(":")
    host = host.lower().rstrip(".")
    if not sep or not port_text.isdigit() or len(port_text) > 5 or not 1 <= int(port_text) <= 65535:
        raise _Refusal("bad_request", _INVALID_SITE)
    if not host or len(host) > 253 or set(host) - _HOST_CHARS:
        raise _Refusal("bad_host", _INVALID_SITE)
    if _ip_like(host):
        raise _Refusal("ip_literal", host)
    if any(not label or len(label) > 63 for label in host.split(".")):
        raise _Refusal("bad_host", _INVALID_SITE)
    assert not _ip_like(host), "literals never pass"
    return host, int(port_text)


def _ip_like(host: str) -> bool:
    """An IP literal, or an all-numeric last label (``2130706433``, ``0x7f.1``) a system
    resolver would read as an address."""
    assert host, "host required"
    last = host.rsplit(".", 1)[-1]
    assert isinstance(last, str), "label must be a string"
    return last.isdigit() or last.startswith("0x")


def _plain_site(target: str) -> str:
    assert isinstance(target, str), "target must be a string"
    try:
        host = urlsplit(target).hostname or ""
    except ValueError:
        host = ""
    site = (registrable_domain(host) or _INVALID_SITE)[:253]
    assert site, "a refusal always names a site"
    return site


def _reply(conn: socket.socket, code: int) -> None:
    assert code in (403, 405), "only refusals are written here"
    assert conn is not None, "client socket required"
    text = "Forbidden" if code == 403 else "Method Not Allowed"
    try:
        conn.sendall(f"HTTP/1.1 {code} {text}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    except OSError:
        pass


def _close(sock: socket.socket) -> None:
    assert sock is not None, "socket required"
    assert isinstance(sock, socket.socket), "a socket"
    try:
        sock.close()
    except OSError:
        pass


# --- the child process ---------------------------------------------------------------


def _apply_rlimits() -> None:
    """RLIMIT_CPU + RLIMIT_CORE (POSIX only; ``resource`` imported inside the guard)."""
    if os.name != "posix":
        return
    assert os.name == "posix", "resource is POSIX-only"
    import resource  # POSIX-only stdlib module

    for which, value in ((resource.RLIMIT_CPU, _RLIMIT_CPU_S), (resource.RLIMIT_CORE, 0)):
        try:
            resource.setrlimit(which, (value, value))
        except (OSError, ValueError):
            pass  # the parent's watchdog and stdin lifeline still bound this process
    assert _RLIMIT_CPU_S > 0, "cpu limit positive"


def _read_config_line() -> dict:
    """One newline-terminated JSON line from fd 0 (bounded)."""
    assert _CONFIG_CAP > 0, "config cap must be positive"
    assert os.name == "posix", "the child runs on POSIX"
    buf = b""
    for _ in range(_CONFIG_CAP):  # each read adds ≥ 1 byte or ends
        chunk = os.read(0, 4096)
        if not chunk:
            break
        buf += chunk
        if b"\n" in buf or len(buf) > _CONFIG_CAP:
            break
    line = buf.split(b"\n", 1)[0]
    if len(line) > _CONFIG_CAP:
        raise ValueError("config line too long")
    return validate_config(json.loads(line.decode("utf-8")))


def _emit(obj: dict) -> None:
    assert isinstance(obj, dict) and len(obj) <= 2, "closed-key line"
    assert sys.stdout is not None, "stdout is the parent's pipe"
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _wait_for_eof(max_life_s: float) -> None:
    """Block until the parent closes stdin, or the life cap passes."""
    assert 0 < max_life_s <= MAX_LIFE_S, "life cap is bounded"
    deadline = time.monotonic() + max_life_s
    assert deadline > 0, "a monotonic deadline"
    for _ in range(int(max_life_s / 0.5) + 2):  # fixed bound
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        ready, _, _ = select.select([0], [], [], min(0.5, remaining))
        if ready and not os.read(0, 4096):
            return


def main() -> None:
    """Child entry: rlimits → config → endpoint line → serve → census line → exit."""
    _apply_rlimits()
    try:
        config = _read_config_line()
    except (ValueError, UnicodeDecodeError):
        _emit({"error": "config"})
        os._exit(2)
    assert isinstance(config, dict), "a validated config"
    server = EgressServer(config)
    endpoint = server.listen()
    assert endpoint["kind"] == config["listen"], "listening as configured"
    _emit({"endpoint": endpoint})
    threading.Thread(target=server.serve, daemon=True).start()
    _wait_for_eof(float(config["max_life_s"]))
    server.close()
    _emit(server.census())
    os._exit(0)
