"""The egress child (§35, P3 design §2): the engine's only network route.

The in-process tests run the real ``EgressServer`` with an injected resolver (answers
public-looking addresses, or the unsafe ones a test wants) and an injected dialer (maps the
validated public address to a TLS-free local target), so the real ``netguard._is_unsafe``
check runs on every answer. The child-process tests spawn the real child through
``egress.start`` and only use names that resolve without a network (``localhost``) or IP
literals, so they stay offline."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from smartbrain_3000 import jailrun
from smartbrain_3000.browsers import egress, jail_egress
from tests import _browsers as hb

PAGE = b"<html><body><p>fixture page</p></body></html>"


def _config(**over: object) -> dict:
    base = {"listen": "tcp", "socket_path": None, "policy": "own", "sites": [],
            "main_host": "fixture.test", "main_port": 443, "max_life_s": 60, "caps": {}}
    base.update(over)
    return base


@pytest.fixture()
def target():
    page = hb.PageTarget(PAGE)
    yield page
    page.close()


class Rig:
    def __init__(self, config: dict, names: dict, target_port: int) -> None:
        self.calls: list[str] = []
        self.dials: list[str] = []
        resolve = hb.resolver_for(names)
        dial = hb.dialer_to(target_port)

        def counting_resolve(host: str, port: int) -> list[str]:
            self.calls.append(host)
            return resolve(host, port)

        def counting_dial(ip: str, port: int, timeout: float) -> socket.socket:
            self.dials.append(ip)
            return dial(ip, port, timeout)

        self.server = jail_egress.EgressServer(config, resolver=counting_resolve, dialer=counting_dial)
        self.port = self.server.listen()["port"]
        import threading
        threading.Thread(target=self.server.serve, daemon=True).start()

    def send(self, head: bytes) -> tuple[socket.socket, bytes]:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(head)
        reply = b""
        for _ in range(64):
            chunk = sock.recv(1)
            if not chunk:
                break
            reply += chunk
            if reply.endswith(b"\r\n\r\n"):
                break
        return sock, reply

    def connect(self, target: str) -> tuple[socket.socket, bytes]:
        return self.send(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())

    def rows(self) -> dict[str, dict]:
        got = self.server.census()
        return {r["site"]: r for r in got["census"]}

    def close(self) -> dict:
        self.server.close()
        return self.server.census()


NAMES = {"fixture.test": [hb.PUBLIC_IP], "www.fixture.test": [hb.PUBLIC_IP],
         "cdn.other.test": [hb.PUBLIC_IP], "other.test": [hb.PUBLIC_IP],
         "foo.github.io": [hb.PUBLIC_IP], "other.github.io": [hb.PUBLIC_IP]}


def _fetch_through(sock: socket.socket, host: str = "fixture.test") -> bytes:
    sock.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    data = b""
    for _ in range(1000):
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def _wait(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    for _ in range(int(seconds / 0.02) + 1):
        if predicate():
            return True
        if time.monotonic() > deadline:
            break
        time.sleep(0.02)
    return predicate()


def test_connect_relays_and_counts_the_site(target) -> None:
    rig = Rig(_config(), NAMES, target.port)
    sock, reply = rig.connect("fixture.test:443")
    assert reply.startswith(b"HTTP/1.1 200")
    body = _fetch_through(sock)
    sock.close()
    assert PAGE in body
    assert _wait(lambda: rig.rows().get("fixture.test", {}).get("down", 0) > 0)
    row = rig.close()["census"][0]
    assert set(row) == {"site", "tunnels", "up", "down", "verdict"}
    assert (row["site"], row["tunnels"], row["verdict"]) == ("fixture.test", 1, "ok")
    assert row["up"] > 0 and row["down"] >= len(PAGE)
    assert rig.dials == [hb.PUBLIC_IP]  # dialled the validated address, never a name


def test_plain_requests_are_refused_without_dialling(target) -> None:
    rig = Rig(_config(), NAMES, target.port)
    _sock, reply = rig.send(b"GET http://fixture.test/ HTTP/1.1\r\nHost: fixture.test\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 405")
    assert rig.rows()["fixture.test"]["verdict"] == "method"
    assert rig.calls == [] and rig.dials == []


@pytest.mark.parametrize("literal", ["93.184.216.34:443", "[::1]:443", "127.0.0.1:443",
                                     "2130706433:443", "0x7f.1:443", "10.0.0.1:443"])
def test_ip_literals_are_refused_before_resolution(target, literal: str) -> None:
    rig = Rig(_config(policy="open"), NAMES, target.port)
    _sock, reply = rig.connect(literal)
    assert reply.startswith(b"HTTP/1.1 403")
    assert [r["verdict"] for r in rig.rows().values()] == ["ip_literal"]
    assert rig.calls == [] and rig.dials == []


@pytest.mark.parametrize("answers", [
    ["127.0.0.1"], ["::1"], ["10.1.2.3"], ["192.168.1.10"], ["169.254.169.254"], ["0.0.0.0"],
    ["100.64.0.1"], ["::ffff:127.0.0.1"], ["64:ff9b::7f00:1"], ["fe80::1"],
    [hb.PUBLIC_IP, "10.0.0.5"],  # one bad answer poisons the whole name
])
def test_unsafe_answers_are_refused_and_never_dialled(target, answers: list[str]) -> None:
    rig = Rig(_config(main_host="evil.test"), {"evil.test": answers}, target.port)
    _sock, reply = rig.connect("evil.test:443")
    assert reply.startswith(b"HTTP/1.1 403")
    assert rig.rows()["evil.test"]["verdict"] == "unsafe"
    assert rig.dials == []


def test_unresolvable_names_are_refused(target) -> None:
    rig = Rig(_config(main_host="nowhere.test"), {}, target.port)
    _sock, reply = rig.connect("nowhere.test:443")
    assert reply.startswith(b"HTTP/1.1 403")
    assert rig.rows()["nowhere.test"]["verdict"] == "resolve"


def test_each_name_resolves_once_per_run_so_rebinding_has_no_window(target) -> None:
    answers = iter([[hb.PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])
    rig = Rig(_config(), {}, target.port)
    rig.server._resolve = lambda host, port: (rig.calls.append(host), next(answers))[1]
    for _ in range(3):
        sock, reply = rig.connect("fixture.test:443")
        assert reply.startswith(b"HTTP/1.1 200")
        sock.close()
    assert rig.calls == ["fixture.test"]
    assert rig.dials == [hb.PUBLIC_IP] * 3


def test_port_rule(target) -> None:
    rig = Rig(_config(), NAMES, target.port)
    _s, reply = rig.connect("fixture.test:8443")
    assert reply.startswith(b"HTTP/1.1 403") and rig.rows()["fixture.test"]["verdict"] == "port"
    rig = Rig(_config(main_port=8443), NAMES, target.port)
    sock, reply = rig.connect("fixture.test:8443")
    assert reply.startswith(b"HTTP/1.1 200")  # the sealed URL's explicit port, that host only
    sock.close()
    _s, reply = rig.connect("www.fixture.test:8443")
    assert reply.startswith(b"HTTP/1.1 403")


def test_site_policies(target) -> None:
    own = Rig(_config(), NAMES, target.port)
    assert own.connect("www.fixture.test:443")[1].startswith(b"HTTP/1.1 200")
    assert own.connect("other.test:443")[1].startswith(b"HTTP/1.1 403")
    assert own.rows()["other.test"]["verdict"] == "offsite"
    assert "other.test" not in own.calls  # an off-policy site is never even resolved
    sealed = Rig(_config(policy="sealed", sites=["fixture.test", "other.test"]), NAMES, target.port)
    assert sealed.connect("cdn.other.test:443")[1].startswith(b"HTTP/1.1 200")
    assert sealed.connect("foo.github.io:443")[1].startswith(b"HTTP/1.1 403")
    opened = Rig(_config(policy="open"), NAMES, target.port)
    assert opened.connect("other.test:443")[1].startswith(b"HTTP/1.1 200")
    pages = Rig(_config(policy="sealed", sites=["foo.github.io"], main_host="foo.github.io"),
                NAMES, target.port)
    assert pages.connect("foo.github.io:443")[1].startswith(b"HTTP/1.1 200")
    assert pages.connect("other.github.io:443")[1].startswith(b"HTTP/1.1 403")


def test_tunnel_cap(target) -> None:
    rig = Rig(_config(caps={"tunnels": 3}), NAMES, target.port)
    replies = [rig.connect("fixture.test:443")[1] for _ in range(4)]
    assert [r[:12] for r in replies] == [b"HTTP/1.1 200"] * 3 + [b"HTTP/1.1 403"]
    assert rig.rows()["fixture.test"]["verdict"] == "cap_tunnels"


def test_site_cap(target) -> None:
    rig = Rig(_config(policy="open", caps={"sites": 2}), NAMES, target.port)
    assert rig.connect("fixture.test:443")[1].startswith(b"HTTP/1.1 200")
    assert rig.connect("other.test:443")[1].startswith(b"HTTP/1.1 200")
    assert rig.connect("foo.github.io:443")[1].startswith(b"HTTP/1.1 403")
    assert rig.rows()["foo.github.io"]["verdict"] == "cap_sites"


def test_concurrency_cap(target) -> None:
    rig = Rig(_config(caps={"concurrent": 1}), NAMES, target.port)
    held, reply = rig.connect("fixture.test:443")
    assert reply.startswith(b"HTTP/1.1 200")
    assert rig.connect("fixture.test:443")[1].startswith(b"HTTP/1.1 403")
    held.close()
    assert _wait(lambda: rig.server._active == 0)
    assert rig.connect("fixture.test:443")[1].startswith(b"HTTP/1.1 200")


def test_download_cap_closes_the_tunnel(target) -> None:
    rig = Rig(_config(caps={"down": 100}), NAMES, target.port)
    sock, _reply = rig.connect("fixture.test:443")
    body = _fetch_through(sock)
    assert len(body) <= 100
    assert _wait(lambda: rig.rows()["fixture.test"]["verdict"] == "cap_down")


def test_upload_cap_closes_the_tunnel(target) -> None:
    rig = Rig(_config(caps={"up": 64}), NAMES, target.port)
    sock, _reply = rig.connect("fixture.test:443")
    sock.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n" + b"x" * 500)
    assert _wait(lambda: rig.rows()["fixture.test"]["verdict"] == "cap_up")


def test_idle_tunnels_are_closed(target) -> None:
    rig = Rig(_config(caps={"idle_s": 0.3}), NAMES, target.port)
    sock, reply = rig.connect("fixture.test:443")
    assert reply.startswith(b"HTTP/1.1 200")
    sock.settimeout(5)
    started = time.monotonic()
    assert sock.recv(1) == b""  # the egress hung up on the idle tunnel
    assert time.monotonic() - started < 3


def test_oversized_request_heads_are_refused(target) -> None:
    rig = Rig(_config(), NAMES, target.port)
    prefix = b"CONNECT fixture.test:443 HTTP/1.1\r\nX: "
    _sock, reply = rig.send(prefix + b"a" * (jail_egress.HEAD_CAP + 1 - len(prefix)))
    assert reply.startswith(b"HTTP/1.1 403")
    assert rig.rows()["(invalid)"]["verdict"] == "bad_request"


def test_census_is_capped_and_closed(target) -> None:
    rig = Rig(_config(), NAMES, target.port)
    for i in range(70):  # 70 distinct off-policy sites: refused before DNS, all counted
        rig.connect(f"site{i}.example:443")
    got = rig.close()
    assert len(got["census"]) == jail_egress.MAX_ROWS and got["dropped"] == 70 - jail_egress.MAX_ROWS
    for row in got["census"]:
        assert set(row) == {"site", "tunnels", "up", "down", "verdict"}
        assert row["verdict"] == "offsite"


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"listen": "udp"}, {"policy": "anything"}, {"policy": "sealed", "sites": []},
    {"caps": {"tunnels": 151}}, {"caps": {"down": 10 ** 9}}, {"caps": {"bogus": 1}},
    {"main_port": 0}, {"max_life_s": 0}, {"sites": ["Bad Site"]},
    {"listen": "unix", "socket_path": "relative.sock"},
])
def test_config_is_closed_and_caps_only_tighten(change: dict) -> None:
    with pytest.raises(ValueError):
        jail_egress.validate_config(_config(**change))


# --- the real child process ------------------------------------------------------


@pytest.fixture()
def short_dir():
    """Unix socket paths are limited to ~104 bytes: keep the run dir short."""
    path = tempfile.mkdtemp(prefix="sbeg-", dir="/tmp")
    os.chmod(path, 0o700)
    yield path
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _child_connect(endpoint: dict, line: bytes) -> bytes:
    if endpoint["kind"] == "tcp":
        sock = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=5)
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(endpoint["path"])
    sock.sendall(line)
    reply = sock.recv(256)
    sock.close()
    return reply


def test_child_process_contract_over_tcp(short_dir, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")  # must never reach the child
    handle = egress.start(short_dir, kind="tcp", policy="own", sites=[], main_host="localhost",
                          main_port=443, max_life_s=30)
    assert handle.endpoint["kind"] == "tcp"
    if os.path.isdir(f"/proc/{handle.proc.pid}"):
        with open(f"/proc/{handle.proc.pid}/environ", "rb") as fh:
            env = fh.read().split(b"\0")
        assert not [v for v in env if v.upper().startswith((b"HTTPS_PROXY", b"HTTP_PROXY", b"SMARTBRAIN_"))]
    # localhost resolves through /etc/hosts (no network) to loopback: refused as unsafe
    assert _child_connect(handle.endpoint, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _child_connect(handle.endpoint, b"GET http://plain.example/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 405")
    assert _child_connect(handle.endpoint, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    rows, dropped = handle.stop()
    assert handle.proc.returncode == 0 and dropped == 0
    assert {r["site"]: r["verdict"] for r in rows} == {
        "localhost": "unsafe", "plain.example": "method", "127.0.0.1": "ip_literal"}
    assert all(r["tunnels"] == 0 for r in rows)


def test_child_process_contract_over_a_unix_socket(short_dir) -> None:
    handle = egress.start(short_dir, kind="unix", policy="open", sites=[], main_host="example.com",
                          main_port=443, max_life_s=30)
    path = handle.endpoint["path"]
    assert path == os.path.join(short_dir, "egress.sock")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert _child_connect(handle.endpoint, b"CONNECT 10.0.0.1:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    rows, _ = handle.stop()
    assert rows == [{"site": "10.0.0.1", "tunnels": 0, "up": 0, "down": 0, "verdict": "ip_literal"}]


def test_child_exits_by_itself_at_its_life_cap(short_dir) -> None:
    handle = egress.start(short_dir, kind="tcp", policy="own", sites=[], main_host="example.com",
                          main_port=443, max_life_s=1)
    handle.proc.wait(timeout=10)  # no stdin EOF needed: the life cap ends it
    rows, dropped = handle.stop()  # the census line is still there to read
    assert (rows, dropped) == ([], 0)


def test_child_refuses_a_bad_config_line(short_dir) -> None:
    child = subprocess.Popen([sys.executable, "-s", "-c", egress._BOOTSTRAP], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=short_dir,
                             env=jailrun._jail_env())
    out, _ = child.communicate(json.dumps({"listen": "tcp"}).encode() + b"\n", timeout=30)
    assert child.returncode == 2
    assert json.loads(out.splitlines()[0]) == {"error": "config"}


@pytest.mark.parametrize("line", [
    b'{"census": [], "dropped": 0, "extra": 1}',
    b'{"census": [{"site": "a.test", "tunnels": 1, "up": 0, "down": 0, "verdict": "ok", "x": 1}], "dropped": 0}',
    b'{"census": [{"site": "a.test", "tunnels": 1, "up": 0, "down": 0, "verdict": "smuggled"}], "dropped": 0}',
    b'{"census": [{"site": "", "tunnels": 1, "up": 0, "down": 0, "verdict": "ok"}], "dropped": 0}',
    b'{"census": [{"site": "a.test", "tunnels": -1, "up": 0, "down": 0, "verdict": "ok"}], "dropped": 0}',
    b'{"census": [], "dropped": -1}',
    b'not json',
])
def test_census_lines_with_smuggled_shapes_are_refused(line: bytes) -> None:
    with pytest.raises(egress.EgressError):
        egress.parse_census(line)
    many = {"census": [{"site": f"s{i}.test", "tunnels": 0, "up": 0, "down": 0, "verdict": "ok"}
                       for i in range(65)], "dropped": 0}
    with pytest.raises(egress.EgressError):
        egress.parse_census(json.dumps(many).encode())
