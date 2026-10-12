"""The browser runner (§35) end to end with the FAKE ENGINE (tests/fixtures/browsers/
fake_obscura.py) installed through the real installer, the test-only NullWall, and the real
egress server (in-process, injected resolver/dialer; one test uses the real child). Every
status class, the tripwires, the slot, the walls gate, the identity modes, the assets run,
and the process/run-dir hygiene are exercised; nothing touches the network."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from smartbrain_3000 import netguard
from smartbrain_3000.browsers import cmdline, egress, install, runner, walls
from tests import _browsers as hb

PAGE = b"<html><head><title>Fixture</title></head><body><p>rendered fixture page</p></body></html>"
NAMES = {"fixture.test": [hb.PUBLIC_IP], "www.fixture.test": [hb.PUBLIC_IP],
         "other.test": [hb.PUBLIC_IP]}
URL = "https://fixture.test/page"


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    m = hb.install_fake(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(runner, "_RUN_PARENT", str(runs))
    page = hb.PageTarget(PAGE)
    started: list = []
    real_start = egress.start
    monkeypatch.setattr(egress, "start", hb.egress_starter(NAMES, page.port, started))

    def mode(cfg: dict) -> None:
        (runs / "fake_mode.json").write_text(json.dumps(cfg))

    def seen() -> list[dict]:
        path = runs / "fake_seen.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    yield SimpleNamespace(m=m, runs=runs, page=page, started=started, mode=mode, seen=seen,
                          tmp=tmp_path, real_start=real_start)
    page.close()


def _render(rig, url: str = URL, **kw: object) -> runner.RenderResult:
    args = {"site_policy": {"mode": "own"}, "identity": {"mode": "honest"}, "timezone": "UTC"}
    args.update(kw)
    got = runner.render(url, **args)
    assert [p.name for p in rig.runs.iterdir() if p.name.startswith("smartbrain-render-")] == [], \
        "the run dir (cookie file included) is removed on every path"
    return got


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


def test_happy_path(rig) -> None:
    got = _render(rig)
    assert (got.ok, got.status, got.reason) == (True, "ok", "")
    assert got.html == PAGE and got.assets is None
    assert got.final_url == URL  # announced by the engine's "Page loaded:" line
    assert (got.engine, got.version) == ("obscura", hb.FAKE_VERSION)
    assert got.identity == {"mode": "honest", "profile": None,
                            "user_agent": netguard.USER_AGENT, "consistent": False}
    assert set(got.timings) == {"egress_ms", "render_ms", "total_ms"}
    rows = {r["site"]: r for r in got.census}
    assert rows["fixture.test"]["tunnels"] == 1 and rows["fixture.test"]["down"] > len(PAGE)
    seen = rig.seen()[-1]
    argv, env = seen["argv"], seen["env"]
    assert argv[:2] == ["--v8-flags", "--max-old-space-size=128"]  # global, before fetch
    port = rig.started[0].endpoint["port"]
    assert argv[argv.index("--proxy") + 1] == f"http://127.0.0.1:{port}"
    assert argv[argv.index("fetch") + 1] == URL and "--quiet" not in argv
    assert argv[argv.index("--user-agent") + 1] == netguard.USER_AGENT
    assert env["OBSCURA_TIMEZONE"] == "UTC" and "OBSCURA_PROFILE" not in env
    assert env["OBSCURA_SCRIPT_DEADLINE_MS"] == "3000"
    assert env["HOME"].startswith(str(rig.runs)) and env["TMPDIR"].startswith(str(rig.runs))
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def _cases(rig) -> dict:
    return {
        "hang": ({"mode": "hang"}, {"timeout_s": 1}, "timeout", "deadline"),
        "hard_timeout": ({"mode": "hard_timeout"}, {}, "timeout", "deadline"),
        "script_watchdog": ({"mode": "watchdog_warn"}, {}, "timeout", "script_watchdog"),
        "heap_limit": ({"mode": "heap_warn"}, {}, "oversize", "heap"),
        "html_cap": ({"mode": "huge", "size": 200_001}, {}, "oversize", "html_cap"),
        "rss": ({"mode": "memory", "alloc_mb": 512}, {}, "oversize", "rss"),
        "crash": ({"mode": "crash"}, {}, "engine_error", "crash"),
        "blocked": ({"mode": "blocked"}, {}, "blocked", "refused"),
        "navigate": ({"mode": "navigate_fail"}, {}, "engine_error", "navigate"),
        "no_output": ({"mode": "no_output"}, {}, "engine_error", "no_output"),
        "offsite_redirect": ({"mode": "offsite", "offsite_host": "other.test"}, {},
                             "offsite", "redirect"),
        "final_offsite": ({"mode": "final_url", "final_url": "https://other.test/x"}, {},
                          "offsite", "final_url"),
        "final_http": ({"mode": "final_url", "final_url": "http://fixture.test/x"}, {},
                       "offsite", "final_url"),
        "final_file": ({"mode": "final_url", "final_url": "file:///etc/passwd"}, {},
                       "offsite", "final_url"),
        "ignore_proxy": ({"mode": "ignore_proxy", "direct_port": rig.page.port}, {},
                         "confinement", "no_main_connect"),
    }


@pytest.mark.parametrize("case", ["hang", "hard_timeout", "script_watchdog", "heap_limit",
                                  "html_cap", "rss", "crash", "blocked", "navigate", "no_output",
                                  "offsite_redirect", "final_offsite", "final_http", "final_file",
                                  "ignore_proxy"])
def test_every_status_class(rig, case: str) -> None:
    cfg, kw, status, reason = _cases(rig)[case]
    rig.mode(cfg)
    got = _render(rig, **kw)
    assert (got.ok, got.status, got.reason) == (False, status, reason), got
    assert got.html is None and got.assets is None
    assert got.timings["total_ms"] < 15_000
    if case == "offsite_redirect":
        assert {r["site"]: r["verdict"] for r in got.census}["other.test"] == "offsite"


def test_a_child_process_is_confinement_and_never_outlives_the_render(rig) -> None:
    pid_file = rig.tmp / "grandchild.pid"
    rig.mode({"mode": "linger", "pid_file": str(pid_file)})
    got = _render(rig)
    assert (got.status, got.reason) == ("confinement", "child_spawned")
    assert not _alive(int(pid_file.read_text()))


def test_orphan_free_after_sigkill_of_the_engine(rig) -> None:
    pid_file = rig.tmp / "orphan.pid"
    rig.mode({"mode": "sigkill_self", "pid_file": str(pid_file)})
    got = _render(rig)
    assert (got.status, got.reason) == ("confinement", "child_spawned")  # the orphan is seen
    assert not _alive(int(pid_file.read_text()))


def test_output_cap_boundary(rig) -> None:
    rig.mode({"mode": "huge", "size": 200_000})
    got = _render(rig)
    assert got.ok and len(got.html) == 200_000


@pytest.mark.parametrize(("kw", "reason"), [
    ({"url": "http://fixture.test/"}, "bad_url"),
    ({"url": "file:///etc/passwd"}, "bad_url"),
    ({"url": "https://127.0.0.1/"}, "ip_literal"),
    ({"url": "https://fixture.test/ --eval"}, "bad_url"),
    ({"site_policy": {"mode": "sealed", "sites": ["other.test"]}}, "bad_policy"),
    ({"site_policy": {"mode": "own", "sites": ["fixture.test"]}}, "bad_policy"),
    ({"site_policy": {"mode": "sealed", "sites": ["fixture.test", "a.b.other.test"]}}, "bad_policy"),
    ({"identity": {"mode": "mimic"}}, "bad_identity"),
    ({"identity": {"mode": "mimic", "profile": 8}}, "bad_identity"),
    ({"identity": {"mode": "honest", "profile": 1}}, "bad_identity"),
    ({"identity": {"mode": "rotate", "card": "c"}}, "bad_identity"),
    ({"identity": {"mode": "stealthy"}}, "bad_identity"),
    ({"identity": {"mode": "mimic", "card": "c", "ua": "x"}}, "bad_identity"),
    ({"timezone": "Not/AZone"}, "bad_timezone"),
    ({"timeout_s": 999}, "bad_timeout"),
    ({"engine": "obscura-stealth"}, "tier_disabled"),
])
def test_refusals_spawn_nothing(rig, kw: dict, reason: str) -> None:
    got = _render(rig, **kw)
    assert (got.ok, got.status, got.reason) == (False, "refused", reason)
    assert rig.started == [] and rig.seen() == []


def test_identity_unavailable_without_a_profile_pool(tmp_path, monkeypatch) -> None:
    hb.install_fake(monkeypatch, tmp_path, pool_size=0, mode="honest")
    got = runner.render(URL, site_policy={"mode": "own"}, identity={"mode": "mimic", "profile": 0},
                        timezone="UTC")
    assert (got.status, got.reason) == ("refused", "identity_unavailable")


def test_identity_modes_build_the_right_argv_and_env(rig) -> None:
    got = _render(rig, identity={"mode": "mimic", "profile": 3})
    seen = rig.seen()[-1]
    assert got.ok and got.identity == {"mode": "mimic", "profile": 3, "user_agent": None,
                                       "consistent": True}
    assert "--user-agent" not in seen["argv"] and seen["env"]["OBSCURA_PROFILE"] == "3"
    got = _render(rig, identity={"card": "card-42"})  # mimic is the manifest default
    assert got.identity["profile"] == cmdline.card_profile("card-42", 8)
    assert rig.seen()[-1]["env"]["OBSCURA_PROFILE"] == str(got.identity["profile"])
    got = _render(rig, identity={"mode": "rotate"})
    assert got.identity["mode"] == "rotate" and 0 <= got.identity["profile"] <= 7
    assert rig.seen()[-1]["env"]["OBSCURA_PROFILE"] == str(got.identity["profile"])
    assert "--user-agent" not in rig.seen()[-1]["argv"]


def test_busy_while_the_slot_is_taken(rig) -> None:
    assert runner._LIVE_RENDERS.acquire(blocking=False)
    try:
        got = _render(rig)
    finally:
        runner._LIVE_RENDERS.release()
    assert (got.status, got.reason) == ("busy", "render_busy")
    assert rig.started == []
    assert _render(rig).ok  # the slot is free again


def test_no_spawn_when_the_wall_is_unavailable(rig, monkeypatch) -> None:
    monkeypatch.delenv(walls.NULLWALL_ENV)
    got = _render(rig)
    assert got.status == "unavailable" and got.reason in walls.REASONS
    assert rig.started == [] and rig.seen() == []


def test_disabled_component_spawns_nothing(rig, monkeypatch) -> None:
    monkeypatch.setenv(install.DISABLE_ENV, "1")
    got = _render(rig)
    assert (got.status, got.reason) == ("unavailable", "disabled")
    assert rig.started == []


def test_a_modified_engine_is_never_launched(rig) -> None:
    exe = install.engine_dir("obscura", hb.FAKE_VERSION) / "obscura"
    data = bytearray(exe.read_bytes())
    data[-3] ^= 0x01
    exe.write_bytes(bytes(data))
    got = _render(rig)
    assert (got.status, got.reason) == ("unavailable", "modified")
    assert rig.started == [] and rig.seen() == []


def test_assets_are_https_only_deduped_and_capped(rig) -> None:
    many = [f"https://cdn.fixture.test/{i}.js" for i in range(600)]
    rig.mode({"assets": ["https://a.test/1.js", "http://b.test/x.js", "file:///etc/passwd",
                         "data:text/javascript,1", "https://a.test/1.js", "https://c.test/ a.js",
                         "javascript:alert(1)"] + many})
    got = _render(rig, want_assets=True)
    assert got.ok and got.html == PAGE
    assert got.assets[0] == "https://a.test/1.js" and len(got.assets) == 500
    assert all(u.startswith("https://") and " " not in u for u in got.assets)
    assert len(set(got.assets)) == len(got.assets)
    runs = rig.seen()
    assert [r["argv"][r["argv"].index("--dump") + 1] for r in runs[-2:]] == ["html", "assets"]


def test_an_unconfined_pid_is_confinement(rig, monkeypatch) -> None:
    monkeypatch.setattr(walls.NullWall, "verify", lambda self, tree: False)
    rig.mode({"mode": "hang"})
    got = _render(rig, timeout_s=2)
    assert (got.status, got.reason) == ("confinement", "wall_verify")
    assert got.timings["total_ms"] < 5000


def test_a_broken_census_is_confinement(rig, monkeypatch) -> None:
    def broken(self):
        self.server.close()
        raise egress.EgressError("census")

    monkeypatch.setattr(hb.InProcessEgress, "stop", broken)
    got = _render(rig)
    assert (got.status, got.reason) == ("confinement", "egress_census")


def test_a_broken_census_from_the_real_child_is_confinement(rig, monkeypatch) -> None:
    """The real handle refuses a second stop(): a bad census must still end as a result."""
    def bad_census(line: bytes):
        raise egress.EgressError("census")

    monkeypatch.setattr(egress, "start", rig.real_start)
    monkeypatch.setattr(egress, "parse_census", bad_census)
    got = _render(rig, url="https://localhost/")
    assert (got.status, got.reason) == ("confinement", "egress_census")


def test_an_egress_that_cannot_start_spawns_no_engine(rig, monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise egress.EgressError("start")

    monkeypatch.setattr(egress, "start", refuse)
    got = _render(rig)
    assert (got.status, got.reason) == ("unavailable", "egress_start")
    assert rig.seen() == []


def test_through_the_real_egress_child(rig, monkeypatch) -> None:
    """The real child refuses the main site (localhost → loopback, via /etc/hosts — no
    network needed); the engine cannot navigate; the census says why."""
    monkeypatch.setattr(egress, "start", rig.real_start)
    got = _render(rig, url="https://localhost/")
    assert (got.status, got.reason) == ("engine_error", "navigate")
    assert got.census == [{"site": "localhost", "tunnels": 0, "up": 0, "down": 0,
                           "verdict": "unsafe"}]


def test_the_slot_and_the_group_are_released_after_a_crash(rig) -> None:
    rig.mode({"mode": "crash"})
    assert _render(rig).status == "engine_error"
    assert runner._LIVE_RENDERS.acquire(blocking=False)
    runner._LIVE_RENDERS.release()
    rig.mode({})
    assert _render(rig).ok
