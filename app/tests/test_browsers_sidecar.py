"""The Docker sidecar wall end to end (§35, B2) with the FAKE ENGINE: the runner's remote seam
(``Wall.run``) hands the job to a thread that plays the sidecar agent with the agent's own
code (``render_agent.process``: argv re-walk, engine re-hash, the relay to the egress Unix
socket, spawn/watch/kill, state cleanup, the closed-key result) over a temp "volume"; the
runner reads the result and the output back through ``O_NOFOLLOW``. Also: the agent's
refusals, the install/status view of a remote engine, the once-per-boot self-test, and a
local wall whose endpoint is a Unix socket (the relay that replaced B1's ``relay_pending``).
Linux only (the agent reads /proc namespace links)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartbrain_3000.browsers import egress, install, render_agent, runner, walls
from smartbrain_3000.browsers import manifest as mf
from smartbrain_3000.browsers.walls import base
from smartbrain_3000.browsers.walls import docker as dw
from smartbrain_3000.browsers.walls.userns_boot import Relay
from tests import _browsers as hb

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc/self/ns"), reason="the sidecar agent is Linux-only")
PAGE = b"<html><head><title>Fixture</title></head><body><p>sidecar fixture page</p></body></html>"
NAMES = {"fixture.test": [hb.PUBLIC_IP]}
URL = "https://fixture.test/page"


def _short_dir() -> Path:
    """A short real path: a Unix socket path must fit sun_path (108 bytes), and pytest's
    tmp_path is longer than production's /render/smartbrain-render-…/egress.sock."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix="sb-", dir="/tmp")))


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def unix_starter(names: dict, target_port: int, started: list):
    """``egress.start`` → the real EgressServer in-process, listening on the run dir's socket."""

    def start(run_dir: str, *, kind: str, policy: str, sites: list[str], main_host: str,
              main_port: int, max_life_s: float, caps: dict | None = None) -> hb.InProcessEgress:
        config = {"listen": kind, "socket_path": os.path.join(run_dir, "egress.sock") if kind == "unix" else None,
                  "policy": policy, "sites": list(sites), "main_host": main_host, "main_port": main_port,
                  "max_life_s": max_life_s, "caps": dict(caps or {})}
        handle = hb.InProcessEgress(config, names, target_port)
        started.append(handle)
        return handle

    return start


def ready_facts(m: mf.Manifest, *, verified: bool = True) -> dict:
    plat = install.platform_key()
    return {"v": 1, "boot": "testboot", "at": "2026-10-09T00:00:00+00:00", "uid": 65532, "gid": 10001,
            "interfaces": ["lo"], "netns": os.readlink("/proc/self/ns/net"), "readonly_root": True,
            "canaries": [{"target": t, "outcome": "Network is unreachable"} for t in ("a", "b", "c")],
            "engine": {"name": m.name, "version": m.version, "platform": plat,
                       "sha256": m.platforms[plat].sha256, "verified": verified}, "ok": True}


@pytest.fixture()
def sidecar(tmp_path, monkeypatch):
    m = hb.install_fake(monkeypatch, tmp_path)  # the "sidecar image" engine (fake), hash-pinned
    monkeypatch.delenv(walls.NULLWALL_ENV)      # the remote path: the sidecar wall, not the NullWall
    short = _short_dir()
    root = short / "render"
    root.mkdir(mode=0o770)
    monkeypatch.setattr(dw, "RENDER_ROOT", root)
    monkeypatch.setattr(dw, "RELAY_PORT", _free_port())
    monkeypatch.setattr(walls, "host_platform", lambda: "docker")
    monkeypatch.setattr(runner, "_RUN_PARENT", None)
    dw.write_json(root, dw.READY_NAME, ready_facts(m))
    dw.write_json(root, dw.HEARTBEAT_NAME, {"v": 1, "boot": "testboot", "ts": time.time()})
    page = hb.PageTarget(PAGE)
    started: list = []
    monkeypatch.setattr(egress, "start", unix_starter(NAMES, page.port, started))
    stop = threading.Event()
    jobs: list[str] = []

    def agent() -> None:
        seen: set[str] = set()
        for _ in range(4000):
            for run_dir in render_agent.pending(seen):
                seen.add(run_dir.name)
                jobs.append(run_dir.name)
                render_agent.process(run_dir, m, install.platform_key(), os.readlink("/proc/self/ns/net"))
            if stop.wait(0.02):
                return

    thread = threading.Thread(target=agent, daemon=True)
    thread.start()
    yield SimpleNamespace(m=m, root=root, page=page, started=started, jobs=jobs, tmp=tmp_path)
    stop.set()
    thread.join(5)
    page.close()
    shutil.rmtree(short, ignore_errors=True)


def _render(**kw: object) -> runner.RenderResult:
    args = {"site_policy": {"mode": "own"}, "identity": {"mode": "honest"}, "timezone": "UTC"}
    args.update(kw)
    return runner.render(URL, **args)


def test_a_render_goes_through_the_sidecar_and_back(sidecar) -> None:
    got = _render()
    assert (got.ok, got.status, got.reason) == (True, "ok", ""), got
    assert got.html == PAGE and got.final_url == URL
    rows = {r["site"]: r for r in got.census}
    assert rows["fixture.test"]["tunnels"] == 1  # the engine's CONNECT came through the relay
    assert len(sidecar.jobs) == 1 and sidecar.started[0].endpoint["kind"] == "unix"
    assert [p.name for p in sidecar.root.iterdir() if p.name.startswith(dw.RUN_PREFIX)] == []


def test_the_job_is_closed_keys_and_carries_the_apps_own_argv(sidecar, monkeypatch) -> None:
    seen: list[dict] = []
    real = dw.SidecarWall.submit

    def spy(self, job: dict, deadline_s: float):
        seen.append(job)
        return real(self, job, deadline_s)

    monkeypatch.setattr(dw.SidecarWall, "submit", spy)
    assert _render(identity={"mode": "mimic", "profile": 2}).ok
    job = seen[0]
    assert set(job) == dw.JOB_KEYS and job["kind"] == "render" and job["relay_port"] == dw.RELAY_PORT
    argv = job["argv"]
    assert argv[argv.index("--proxy") + 1] == f"http://127.0.0.1:{dw.RELAY_PORT}"
    assert argv[argv.index("fetch") + 1] == URL and job["env"]["OBSCURA_PROFILE"] == "2"
    assert job["out"] == "out/page.html"


def test_the_engine_state_is_gone_before_the_app_reads_the_result(sidecar, monkeypatch) -> None:
    real_run = render_agent.run_render

    def run_and_leave_state(job, m, run_dir, engine, netns):  # what the real engine leaves
        out = real_run(job, m, run_dir, engine, netns)
        (run_dir / "storage" / "cookies.json").write_text("[]")
        (run_dir / "home" / ".cache").mkdir()
        (run_dir / "home" / ".cache" / "x").write_text("x")
        return out

    monkeypatch.setattr(render_agent, "run_render", run_and_leave_state)
    seen: list[list[str]] = []
    real = dw.SidecarWall.submit

    def spy(self, job: dict, deadline_s: float):
        result = real(self, job, deadline_s)
        run = Path(self._run_dir)
        seen.append(sorted(str(p.relative_to(run)) for p in run.rglob("*")))
        return result

    monkeypatch.setattr(dw.SidecarWall, "submit", spy)
    assert _render().ok
    left = seen[0]
    assert not [p for p in left if p.startswith(("home/", "tmp/", "storage/"))]  # cookie file gone
    assert "out/page.html" in left and "result.json" in left


@pytest.mark.parametrize(("mode", "status", "reason"), [
    ({"mode": "blocked"}, "blocked", "refused"), ({"mode": "heap_warn"}, "oversize", "heap"),
    ({"mode": "crash"}, "engine_error", "crash"), ({"mode": "huge", "size": 200_001}, "oversize", "html_cap"),
])
def test_engine_outcomes_cross_the_sidecar_unchanged(sidecar, mode: dict, status: str,
                                                     reason: str) -> None:
    (sidecar.root / "fake_mode.json").write_text(json.dumps(mode))
    got = _render()
    assert (got.status, got.reason) == (status, reason), got


def test_a_tampered_job_is_refused_and_never_spawns(sidecar) -> None:
    run = Path(runner.make_run_dir())
    assert run.parent == sidecar.root
    engine = install.engine_dir(sidecar.m.name, sidecar.m.version) / "obscura"
    env = {"PATH": "/bin", "HOME": f"{run}/home", "TMPDIR": f"{run}/tmp"}
    good = ["obscura", "--v8-flags", "--max-old-space-size=128", "--proxy",
            f"http://127.0.0.1:{dw.RELAY_PORT}", "--storage-dir", f"{run}/storage", "fetch", URL,
            "--timeout", "4", "--wait-until", "load", "--dump", "html", "--output", f"{run}/out/page.html"]
    job = {"v": 1, "kind": "render", "run_id": run.name, "argv": good, "env": env, "out": "out/page.html",
           "deadline_s": 9.0, "rss_mb": 64, "cpu_s": 40, "data_mb": 0, "relay_port": dw.RELAY_PORT}
    assert render_agent.validate_render(job, sidecar.m, run, engine)[0][0] == str(engine)
    bad = {"eval": good[:8] + ["--eval", "1"] + good[8:],
           "proxy": [t.replace(str(dw.RELAY_PORT), "38080") for t in good],
           "output": good[:-1] + ["/tmp/elsewhere.html"],
           "binary": ["sh"] + good[1:], "http": [t.replace("https://", "http://") for t in good]}
    for name, argv in bad.items():
        with pytest.raises(ValueError):
            render_agent.validate_render({**job, "argv": argv}, sidecar.m, run, engine)
        assert name
    with pytest.raises(ValueError):
        render_agent.validate_render({**job, "env": {**env, "HOME": "/root"}}, sidecar.m, run, engine)


def test_a_refused_job_answers_and_spawns_nothing(sidecar) -> None:
    run = Path(runner.make_run_dir())
    for sub in ("home", "tmp", "storage", "out"):
        os.chmod(run / sub, 0o770)
    job = {"v": 1, "kind": "render", "run_id": run.name, "argv": ["obscura", "--eval", "1"],
           "env": {}, "out": "out/page.html", "deadline_s": 9.0, "rss_mb": 64, "cpu_s": 40,
           "data_mb": 0, "relay_port": dw.RELAY_PORT}
    wall = SimpleNamespace(_run_dir=str(run))
    result = dw.SidecarWall.submit(wall, job, 5.0)  # type: ignore[arg-type]
    assert result is not None and result["refused"] == "argv" and result["spawned"] is False
    assert not (sidecar.root / "fake_seen.jsonl").exists()


def test_no_ready_sidecar_means_unavailable_and_no_job(sidecar) -> None:
    (sidecar.root / dw.HEARTBEAT_NAME).unlink()
    got = _render()
    assert (got.status, got.reason) == ("unavailable", "docker_no_sidecar")
    assert sidecar.started == [] and sidecar.jobs == []


def test_a_silent_sidecar_times_out(sidecar, monkeypatch) -> None:
    monkeypatch.setattr(dw, "PICKUP_S", 0.3)
    monkeypatch.setattr(render_agent, "pending", lambda seen: [])  # the agent never answers
    got = _render(timeout_s=1)
    assert (got.status, got.reason) == ("timeout", "deadline")
    assert got.timings["total_ms"] < 15_000


def test_the_remote_engine_is_never_downloaded_in_docker(sidecar, monkeypatch) -> None:
    """ensure() in Docker: no download; the once-per-boot self-test is a selftest job through
    the agent (relay → this egress child → 403, no app data, DNS fails, engine --version)."""
    def no_dns(*args: object, **kwargs: object) -> list:
        raise OSError("no DNS in tests")

    monkeypatch.setattr(render_agent.socket, "getaddrinfo", no_dns)
    with install._state_lock:
        install._state.clear()
    server = hb.LocalServer({})
    try:
        row = install.ensure(sidecar.m.name, transport=hb.transport_for(server))
    finally:
        server.close()
    assert server.requests == []  # the sidecar holds the engine
    assert (row["phase"], row["reason"], row["last_self_test"]) == ("ready", "", "ok"), row
    assert row["eligible"] is True and row["sandbox"]["kind"] == "sidecar"
    assert row["sandbox"]["verified_at"] is not None


def test_status_says_absent_when_the_sidecar_holds_another_build(sidecar) -> None:
    facts = ready_facts(sidecar.m)
    facts["engine"]["sha256"] = "f" * 64
    dw.write_json(sidecar.root, dw.READY_NAME, facts)
    row = install.status_row(sidecar.m)
    assert (row["phase"], row["eligible"]) == ("absent", False)


def test_the_agent_refuses_to_serve_with_a_network(monkeypatch) -> None:
    """In a container that has a network (this test's), the boot facts are not ok."""
    m = mf.load("obscura")
    monkeypatch.setattr(render_agent, "BOOT_TARGETS", (("127.0.0.1", 9),))
    monkeypatch.setattr(render_agent, "BOOT_UDP", ("127.0.0.1", 9))
    monkeypatch.setattr(render_agent.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no dns in tests")))
    facts = render_agent.boot_facts(m, install.platform_key())
    assert set(facts) == dw.READY_KEYS and facts["ok"] is False
    assert facts["engine"]["verified"] is False  # nothing installed here


def test_up_interfaces_only_lists_interfaces_that_are_up() -> None:
    up = render_agent.up_interfaces()
    assert "lo" in up and "bonding_masters" not in up


def test_kill_strays_never_runs_outside_the_sidecar(monkeypatch) -> None:
    monkeypatch.delenv(render_agent.AGENT_ENV, raising=False)
    assert render_agent.kill_strays() == 0


# --- a LOCAL wall whose endpoint is a Unix socket: the relay replaced relay_pending ------


class RelayWall(walls.Wall):
    """A test wall shaped like the Linux one minus the namespaces: it relays a loopback
    port to the egress Unix socket and runs the fake engine directly."""

    kind = "null"
    endpoint_kind = "unix"

    def __init__(self, socket_path: str) -> None:
        self.port = _free_port()
        self.relay = Relay(self.port, socket_path)

    def proxy_port(self, endpoint: dict) -> int:
        assert endpoint["kind"] == "unix"
        return self.port

    def wrap(self, argv, env):
        return list(argv), dict(env)

    def verify(self, pid_tree):
        return True

    def teardown(self) -> None:
        self.relay.close()


def test_a_unix_endpoint_renders_through_the_walls_relay(tmp_path, monkeypatch) -> None:
    hb.install_fake(monkeypatch, tmp_path)
    runs = _short_dir()
    monkeypatch.setattr(runner, "_RUN_PARENT", str(runs))
    page = hb.PageTarget(PAGE)
    started: list = []
    walls_made: list = []
    monkeypatch.setattr(egress, "start", unix_starter(NAMES, page.port, started))
    monkeypatch.setattr(walls, "endpoint_kind", lambda platform, edir: "unix")

    def prepare(platform, run_dir, engine_dir, endpoint):
        wall = RelayWall(endpoint["path"])
        walls_made.append(wall)
        return wall

    monkeypatch.setattr(walls, "prepare", prepare)
    try:
        got = runner.render(URL, site_policy={"mode": "own"}, identity={"mode": "honest"}, timezone="UTC")
    finally:
        page.close()
        shutil.rmtree(runs, ignore_errors=True)
    assert (got.ok, got.status) == (True, "ok"), got
    assert started[0].endpoint["kind"] == "unix" and walls_made[0].relay.listener.fileno() == -1
    assert {r["site"]: r["tunnels"] for r in got.census}["fixture.test"] == 1


# --- the once-per-boot self-test and eligibility ---------------------------------------


def test_eligible_only_after_a_passing_self_test(tmp_path, monkeypatch) -> None:
    tarball = hb.fake_tarball()
    hb.use_test_engines(monkeypatch, tmp_path, [hb.manifest_doc(tarball)])
    server = hb.LocalServer(hb.release_routes(tarball))
    try:
        m = mf.load("obscura")
        got = install.install(m, install.platform_key(), transport=hb.transport_for(server),
                              self_test=lambda _m, _e: "skipped(wall_unavailable)")
        assert got["ok"]
        row = install.status_row(m)
        assert (row["phase"], row["eligible"]) == ("ready", False)
        assert row["sandbox"] == {"kind": "null", "verified_at": None}
        row = install.ensure("obscura", transport=hb.transport_for(server))  # once per boot
        assert row["eligible"] is True and row["last_self_test"] == "ok"
        assert row["sandbox"]["kind"] == "null" and row["sandbox"]["verified_at"].endswith("Z")
        server.requests.clear()
        install.ensure("obscura", transport=hb.transport_for(server))
        assert server.requests == []
    finally:
        server.close()


def test_a_pass_under_another_wall_kind_does_not_count(tmp_path, monkeypatch) -> None:
    m = hb.install_fake(monkeypatch, tmp_path)
    assert install.status_row(m)["eligible"] is True  # the install's self-test passed (null)
    with install._state_lock:
        install._state[m.name]["sandbox_kind"] = "seatbelt"
    row = install.status_row(m)
    assert row["eligible"] is False and row["sandbox"] == {"kind": "null", "verified_at": None}


def test_a_failed_self_test_records_no_verification(tmp_path, monkeypatch) -> None:
    m = hb.install_fake(monkeypatch, tmp_path)
    with install._state_lock:
        install._state[m.name]["last_self_test"] = "never"
    row = install.ensure(m.name, self_test=lambda _m, _e: "failed:loopback_tcp")
    assert row["phase"] == "error" and row["last_self_test"] == "failed:loopback_tcp"
    assert row["eligible"] is False and row["sandbox"]["verified_at"] is None


def test_canary_file_lives_in_the_data_dir_and_is_private(tmp_path: Path) -> None:
    path, token = base.canary_file(tmp_path / "browsers")
    assert path.parent == tmp_path / "browsers" and (path.stat().st_mode & 0o777) == 0o600
    assert path.read_bytes().strip() == token and token.startswith(b"SMARTBRAIN-CANARY-")
