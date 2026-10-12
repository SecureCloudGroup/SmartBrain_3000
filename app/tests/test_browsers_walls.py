"""The confinement walls (§35, B2): the pure parts of every wall, the reason codes per
platform family, and — where this machine can show it — the real failure modes. The real
Seatbelt wall needs macOS and the real namespaces need a host that allows unprivileged user
namespaces: those rows run in the SG legs (tools/browsers-sg.py), and are skipped here with
the reason."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from smartbrain_3000.browsers import walls
from smartbrain_3000.browsers.walls import base, darwin, docker, linux, userns_boot
from tests import _browsers as hb

IS_MAC = sys.platform == "darwin"
HAS_PROC_NS = os.path.isdir("/proc/self/ns")


# --- base ------------------------------------------------------------------------------


def test_reasons_and_kinds_are_closed() -> None:
    assert walls.REASONS == {"platform", "docker_no_sidecar", "apparmor_userns", "userns_blocked",
                             "missing_libs", "sandbox"}
    assert walls.KINDS == {"none", "null", "seatbelt", "userns", "sidecar"}
    with pytest.raises(AssertionError):
        walls.Unavailable("sandbox_pending")  # B1's placeholder reasons are gone


@pytest.mark.parametrize("bad", ['/tmp/a"b', "/tmp/a(b", "/tmp/a;b", "/tmp/a\nb", "/tmp/a\\b",
                                 "/tmp/a=b", "/tmp/é", "relative/x"])
def test_checked_dir_refuses_characters_outside_the_closed_set(bad: str) -> None:
    with pytest.raises(ValueError):
        base.checked_dir(bad)


def test_checked_dir_canonicalises_and_keeps_spaces(tmp_path: Path) -> None:
    real = tmp_path / "Application Support" / "x"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert base.checked_dir(link) == os.path.realpath(real)
    assert " " in base.checked_dir(real)
    with pytest.raises(ValueError):
        base.checked_dir("/" + "a" * 1100)


def test_verdict_names_the_first_failure_and_any_missing_row() -> None:
    rows = [base.row("a", True), base.row("b", False, "x"), base.row("c", False)]
    assert base.verdict(rows, ("a", "b", "c")) == "failed:b"
    assert base.verdict([base.row("a", True)], ("a", "z")) == "failed:z"  # vacuous = failure
    assert base.verdict([base.row("a", True), base.row("z", True)], ("a", "z")) == "ok"


def test_canaries_count_hits_on_their_own_listeners() -> None:
    can = base.Canaries()
    try:
        assert can.hit_count() == 0
        socket.create_connection(("127.0.0.1", can.ports["tcp4"]), timeout=2).close()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.sendto(b"x", ("127.0.0.1", can.ports["udp4"]))
        udp.close()
        for _ in range(50):
            if can.hit_count() >= 2:
                break
            time.sleep(0.02)
        assert sorted(can.hits) == ["tcp4", "udp4"]
    finally:
        can.close()


# --- family dispatch -------------------------------------------------------------------


@pytest.mark.parametrize(("family", "reason"), [("windows", "platform"), ("other", "platform")])
def test_families_without_a_wall_report_platform(family: str, reason: str) -> None:
    assert walls.check(family, None) == walls.Unavailable(reason)
    assert walls.available_kind(family, None) == "none"


def test_endpoint_kinds_per_family() -> None:
    kinds = {f: walls.endpoint_kind(f, None) for f in ("darwin", "linux", "docker", "windows", "other")}
    assert kinds == {"darwin": "tcp", "linux": "unix", "docker": "unix", "windows": "tcp", "other": "tcp"}


def test_the_fake_engine_never_runs_under_a_local_real_wall(tmp_path: Path, monkeypatch) -> None:
    edir = tmp_path / "engine"
    edir.mkdir()
    exe = edir / "obscura"
    exe.write_bytes(hb.engine_bytes())
    exe.chmod(0o755)
    monkeypatch.delenv(walls.NULLWALL_ENV, raising=False)
    for family in ("darwin", "linux"):
        assert walls.check(family, edir) == walls.Unavailable("sandbox")
    monkeypatch.setenv(walls.NULLWALL_ENV, "1")
    assert walls.check("darwin", edir) is None and walls.available_kind("linux", edir) == "null"


def test_remote_is_docker_without_the_test_wall(monkeypatch) -> None:
    monkeypatch.delenv(walls.NULLWALL_ENV, raising=False)
    assert walls.remote("docker") and not walls.remote("linux") and not walls.remote("darwin")
    monkeypatch.setenv(walls.NULLWALL_ENV, "1")
    assert not walls.remote("docker")


def test_prepare_needs_the_family_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(walls, "check", lambda platform, engine_dir: None)
    run = tmp_path / "run"
    run.mkdir()
    assert walls.prepare("darwin", str(run), str(tmp_path), None) == walls.Unavailable("sandbox")
    assert walls.prepare("darwin", str(run), str(tmp_path), {"kind": "unix", "path": "/x"}) == \
        walls.Unavailable("sandbox")
    assert walls.prepare("linux", str(run), str(tmp_path), {"kind": "tcp", "port": 9}) == \
        walls.Unavailable("sandbox")


# --- darwin (pure parts everywhere; the real wall only on macOS) -------------------------


def test_the_shipped_profile_is_the_measured_five_rules() -> None:
    raw = darwin.PROFILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == darwin.PROFILE_SHA256
    rules = [ln for ln in raw.decode().splitlines() if ln.startswith("(")]
    assert rules[:2] == ["(version 1)", "(deny default)"]
    assert [r for r in rules if r.startswith("(allow")] == [
        '(allow process-exec (literal (string-append engine-dir "/obscura")))',
        '(allow sysctl-read (sysctl-name "hw.pagesize_compat"))',
        '(allow file-read-data (literal "/"))',
        "(allow file-read* file-write* (subpath run-dir))",
        '(allow network-outbound (remote tcp (string-append "localhost:" proxy-port)))']


def test_a_changed_profile_is_not_the_wall(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(darwin, "SANDBOX_EXEC", "/bin/sh")  # an executable stand-in
    assert darwin.profile_ok()
    tampered = tmp_path / "obscura.sb"
    tampered.write_text(darwin.PROFILE.read_text() + "\n(allow network*)\n")
    monkeypatch.setattr(darwin, "PROFILE", tampered)
    assert not darwin.profile_ok()


@pytest.mark.parametrize("port", [0, 65536, -1, True, "443", None])
def test_profile_params_refuse_a_bad_port(tmp_path: Path, port: object) -> None:
    with pytest.raises(ValueError):
        darwin.profile_params(tmp_path / "e", tmp_path / "r", port)


def test_profile_params_are_canonical_and_ordered(tmp_path: Path) -> None:
    (tmp_path / "e").mkdir()
    (tmp_path / "r").mkdir()
    got = darwin.profile_params(tmp_path / "e", tmp_path / "r", 4242)
    real = os.path.realpath(tmp_path)
    assert got == ["-D", f"ENGINE_DIR={real}/e", "-D", f"RUN_DIR={real}/r", "-D", "PROXY_PORT=4242"]
    with pytest.raises(ValueError):
        darwin.profile_params(tmp_path / "r" / "e", tmp_path / "r", 4242)  # engine inside the run dir


def test_seatbelt_wrap_prefixes_sandbox_exec_and_runs_only_its_engine(tmp_path: Path) -> None:
    (tmp_path / "e").mkdir()
    (tmp_path / "r").mkdir()
    wall = darwin.SeatbeltWall(tmp_path / "e", tmp_path / "r", 5555)
    real = os.path.realpath(tmp_path)
    argv, env = wall.wrap([f"{tmp_path}/e/obscura", "fetch", "https://x.test/"], {"PATH": "/bin"})
    assert argv == ["/usr/bin/sandbox-exec", "-f", str(darwin.PROFILE), "-D", f"ENGINE_DIR={real}/e",
                    "-D", f"RUN_DIR={real}/r", "-D", "PROXY_PORT=5555", f"{real}/e/obscura", "fetch",
                    "https://x.test/"]
    assert env == {"PATH": "/bin"} and wall.proxy_port({"kind": "tcp", "port": 5555}) == 5555
    with pytest.raises(ValueError):
        wall.wrap(["/bin/sh", "-c", "true"], {})


def test_confined_reads_sandbox_check_for_every_pid(monkeypatch) -> None:
    states = {11: 1, 12: 0, 13: 0, 14: -1}
    monkeypatch.setattr(darwin, "sandbox_check", lambda pid: states[pid])
    monkeypatch.setattr(darwin, "_proc_path", lambda pid: darwin.SANDBOX_EXEC if pid == 13 else "/x")
    monkeypatch.setattr(darwin, "_alive", lambda pid: False)
    assert darwin.confined([11]) and darwin.confined([11, 13, 14])
    assert not darwin.confined([11, 12])  # one unconfined PID = the watchdog's wall verdict


@pytest.mark.skipif(IS_MAC, reason="off macOS the Seatbelt wall must be unavailable")
def test_darwin_check_off_macos_is_sandbox(monkeypatch) -> None:
    monkeypatch.setattr(darwin, "_cache", {})
    assert darwin.check(None) == walls.Unavailable("sandbox")


@pytest.mark.skipif(not IS_MAC, reason="the real Seatbelt wall needs macOS (SG leg: macos-latest)")
def test_darwin_check_on_macos(monkeypatch) -> None:
    monkeypatch.setattr(darwin, "_cache", {})
    assert darwin.check(None) is None


# --- linux (pure parts + the real failure mode where namespaces are blocked) --------------


def test_mount_plan_masks_home_and_the_data_dir(tmp_path: Path) -> None:
    eng = tmp_path / "data" / "browsers" / "obscura" / "0.2.4"
    eng.mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir()
    plan = linux.mount_plan(eng, run, tmp_path / "data")
    assert plan["masks"][:8] == list(linux.BASE_MASKS)
    real = os.path.realpath(tmp_path)
    covered = real.startswith(("/tmp/", "/home/", "/root/", "/run/"))
    assert plan["masks"][8:] == ([] if covered else [f"{real}/data"])
    assert (plan["engine_dir"], plan["run_dir"]) == (os.path.realpath(eng), os.path.realpath(run))
    with pytest.raises(ValueError):
        linux.mount_plan(run / "e", run, tmp_path / "data")
    with pytest.raises(ValueError):
        linux.mount_plan(eng, run, run)


def test_data_dir_follows_the_install_layout(tmp_path: Path) -> None:
    eng = tmp_path / "d" / "browsers" / "obscura" / "0.2.4"
    assert linux.data_dir_of(eng) == tmp_path / "d"
    with pytest.raises(AssertionError):
        linux.data_dir_of(tmp_path / "x" / "y" / "z")


def test_boot_config_is_the_bootstraps_closed_key_set(tmp_path: Path) -> None:
    plan = {"masks": ["/home"], "engine_dir": "/e", "run_dir": str(tmp_path)}
    cfg = linux.boot_config("engine", plan, f"{tmp_path}/egress.sock", None)
    assert set(cfg) == userns_boot.CONFIG_KEYS and cfg["uid"] == os.getuid()
    assert cfg["relay_port"] == linux.RELAY_PORT and cfg["mode"] == "engine"
    argv = linux.boot_argv(cfg, ["/e/obscura", "fetch", "https://x.test/"])
    assert argv[:4] == [sys.executable, "-I", "-S", str(linux.BOOT)]
    assert json.loads(argv[4]) == cfg and argv[5:] == ["--", "/e/obscura", "fetch", "https://x.test/"]
    assert userns_boot.parse(argv[4:]) == (cfg, ["/e/obscura", "fetch", "https://x.test/"])
    with pytest.raises(ValueError):
        linux.boot_config("engine", plan, "/elsewhere/egress.sock", None)


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"v": 2}, {"mode": "serve"}, {"relay_port": 0}, {"socket": "/other/egress.sock"},
    {"masks": ["relative"]}, {"canary": {"tcp4": 1}},
])
def test_the_bootstrap_refuses_a_bad_config(tmp_path: Path, change: dict) -> None:
    plan = {"masks": ["/home"], "engine_dir": "/e", "run_dir": str(tmp_path)}
    cfg = {**linux.boot_config("engine", plan, f"{tmp_path}/egress.sock", None), **change}
    with pytest.raises(ValueError):
        userns_boot.parse([json.dumps(cfg), "--", "/e/obscura"])
    with pytest.raises(ValueError):  # engine mode without an engine argv
        userns_boot.parse([json.dumps(linux.boot_config("engine", plan, None, None))])


@pytest.mark.skipif(not hasattr(os, "ST_RELATIME"), reason="Linux statvfs flags")
@pytest.mark.parametrize(("f_flag", "must", "must_not"), [
    (0, ("MS_NOSUID", "MS_NODEV", "MS_STRICTATIME"), ("MS_RELATIME", "MS_NOATIME", "MS_RDONLY")),
    ("ST_RELATIME", ("MS_RELATIME",), ("MS_STRICTATIME", "MS_NOATIME")),
    ("ST_NOATIME", ("MS_NOATIME",), ("MS_STRICTATIME", "MS_RELATIME")),
    ("ST_NOEXEC|ST_RDONLY|ST_NODIRATIME|ST_RELATIME", ("MS_NOEXEC", "MS_RDONLY", "MS_NODIRATIME"), ()),
])
def test_a_bind_remount_keeps_the_sources_locked_flags(f_flag: object, must: tuple, must_not: tuple) -> None:
    bits = 0 if f_flag == 0 else sum(getattr(os, name) for name in str(f_flag).split("|"))
    got = userns_boot.kept_flags(bits)
    assert all(got & getattr(userns_boot, name) for name in must)
    assert not any(got & getattr(userns_boot, name) for name in must_not)


def test_parse_line_accepts_only_closed_result_lines() -> None:
    good = {"ok": True, "rows": [{"row": "dns", "ok": True, "detail": ""}]}
    assert linux.parse_line(json.dumps(good).encode()) == good
    assert linux.parse_line(b'{"ok": false, "stage": "unshare", "errno": 1}')["stage"] == "unshare"
    for bad in (b"", b"[]", b'{"ok": 1}', b'{"ok": true, "x": 1}',
                b'{"ok": true, "rows": [{"row": "a", "ok": true}]}'):
        assert linux.parse_line(bad) == {}


def test_ns_confined_needs_other_user_and_net_namespaces(monkeypatch) -> None:
    facts = {21: ("user:[2]", "net:[2]", 2), 22: ("user:[1]", "net:[2]", 2),
             23: ("user:[2]", "net:[1]", 0), 24: None, 25: ("user:[1]", "net:[1]", 0)}
    monkeypatch.setattr(linux, "_ns", lambda pid: facts[pid])
    monkeypatch.setattr(linux, "_is_boot", lambda pid: pid == 25)
    assert linux.ns_confined([21, 24], "user:[1]", "net:[1]")
    assert not linux.ns_confined([21, 22], "user:[1]", "net:[1]")  # same user namespace
    assert not linux.ns_confined([21, 23], "user:[1]", "net:[1]")  # same network namespace
    assert linux.ns_confined([25], "user:[1]", "net:[1]")  # the lone bootstrap before unshare
    assert not linux.ns_confined([25, 21], "user:[1]", "net:[1]")


@pytest.mark.skipif(not HAS_PROC_NS, reason="namespace ids need Linux /proc")
def test_this_process_is_not_confined() -> None:
    app = linux._ns("self")
    assert app is not None and app[2] >= 0
    assert not linux.ns_confined([os.getpid()], app[0], app[1])


@pytest.mark.parametrize(("got", "sysctls", "reason"), [
    ({"ok": True}, {}, None),
    ({"ok": False, "stage": "unshare", "errno": 1}, {}, "userns_blocked"),
    ({"ok": False, "stage": "mask", "errno": 1}, {}, "sandbox"),
    ({"ok": False, "stage": "mask", "errno": 13}, {"kernel/apparmor_restrict_unprivileged_userns": "1"},
     "apparmor_userns"),
    ({}, {"kernel/apparmor_restrict_unprivileged_userns": "1"}, "apparmor_userns"),
    ({"ok": True}, {"kernel/unprivileged_userns_clone": "0"}, "userns_blocked"),
    ({"ok": True}, {"user/max_user_namespaces": "0"}, "userns_blocked"),
])
def test_linux_check_maps_the_probe_to_a_reason(monkeypatch, tmp_path: Path, got: dict,
                                                sysctls: dict, reason: str | None) -> None:
    monkeypatch.setattr(linux, "_cache", {})
    monkeypatch.setattr(linux, "_run_boot", lambda argv: got)
    monkeypatch.setattr(linux, "_sysctl", lambda name: sysctls.get(name, ""))
    monkeypatch.setattr(linux.os, "geteuid", lambda: 1000)
    eng = tmp_path / "d" / "browsers" / "obscura" / "0.2.4"
    eng.mkdir(parents=True)
    assert linux.check(eng) == (walls.Unavailable(reason) if reason else None)


def test_linux_check_refuses_root(monkeypatch) -> None:
    monkeypatch.setattr(linux, "_cache", {})
    monkeypatch.setattr(linux.os, "geteuid", lambda: 0)
    assert linux.check(None) == walls.Unavailable("sandbox")


def _probe_line(tmp_path: Path) -> dict:
    plan = linux.mount_plan(tmp_path / "e", tmp_path / "r", tmp_path / "d")
    return linux._run_boot(linux.boot_argv(linux.boot_config("probe", plan, None, None)))


@pytest.mark.skipif(not HAS_PROC_NS or os.geteuid() == 0, reason="needs Linux and a non-root uid")
def test_the_real_bootstrap_fails_closed_where_namespaces_are_blocked(tmp_path: Path) -> None:
    for sub in ("e", "r", "d"):
        (tmp_path / sub).mkdir()
    probe = _probe_line(tmp_path)
    if probe.get("ok") is True:
        pytest.skip("user namespaces work here; the SG userns leg proves the wall")
    assert probe["ok"] is False and probe["stage"] in ("unshare", "uid_map", "gid_map", "setgroups",
                                                       "private", "mask", "bind", "bind_ro", "proc",
                                                       "lo", "init")
    marker = tmp_path / "r" / "ran"
    fake = tmp_path / "e" / "obscura"
    fake.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    fake.chmod(0o755)
    plan = linux.mount_plan(tmp_path / "e", tmp_path / "r", tmp_path / "d")
    res = subprocess.run(linux.boot_argv(linux.boot_config("engine", plan, None, None), [str(fake)]),
                         capture_output=True, timeout=30, check=False)
    assert res.returncode == userns_boot.EXIT_SETUP and not marker.exists()  # never exec'd


# --- docker (pure parts; the runner seam is in test_browsers_sidecar.py) -------------------


def _ready(**over: object) -> dict:
    facts = {"v": 1, "boot": "b00t", "at": "2026-10-09T00:00:00+00:00", "uid": 65532, "gid": 10001,
             "interfaces": ["lo"], "netns": "net:[1]", "readonly_root": True, "ok": True,
             "canaries": [{"target": t, "outcome": "Network is unreachable"} for t in ("a", "b", "c")],
             "engine": {"name": "obscura", "version": "0.2.4", "platform": "linux-aarch64",
                        "sha256": "0" * 64, "verified": True}}
    return {**facts, **over}


@pytest.mark.parametrize(("over", "ok"), [
    ({}, True), ({"interfaces": ["eth0", "lo"]}, False), ({"readonly_root": False}, False),
    ({"uid": 0}, False), ({"ok": False}, False),
    ({"canaries": [{"target": "a", "outcome": "connected"}] * 3}, False),
    ({"canaries": [{"target": "a", "outcome": "x"}]}, False),
    ({"engine": {"name": "obscura", "version": "0.2.4", "platform": "p", "sha256": "", "verified": False}}, False),
])
def test_ready_facts_must_prove_the_wall(over: dict, ok: bool) -> None:
    assert docker.valid_ready(_ready(**over)) is ok


def test_docker_check_needs_a_fresh_ready_sidecar(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(docker, "RENDER_ROOT", tmp_path / "absent")
    assert docker.check() == walls.Unavailable("docker_no_sidecar")
    monkeypatch.setattr(docker, "RENDER_ROOT", tmp_path)
    assert docker.check() == walls.Unavailable("docker_no_sidecar")
    docker.write_json(tmp_path, docker.READY_NAME, _ready())
    docker.write_json(tmp_path, docker.HEARTBEAT_NAME, {"v": 1, "boot": "b00t", "ts": time.time() - 60})
    assert docker.check() == walls.Unavailable("docker_no_sidecar")  # stale
    docker.write_json(tmp_path, docker.HEARTBEAT_NAME, {"v": 1, "boot": "other", "ts": time.time()})
    assert docker.check() == walls.Unavailable("docker_no_sidecar")  # another boot
    docker.write_json(tmp_path, docker.HEARTBEAT_NAME, {"v": 1, "boot": "b00t", "ts": time.time()})
    assert docker.check() is None and docker.run_parent() == str(tmp_path)


def test_read_file_never_follows_a_link_at_any_component(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "out").mkdir(parents=True)
    (run / "out" / "page.html").write_bytes(b"page")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "page.html").write_bytes(b"SECRET")
    assert docker.read_file(run, "out/page.html", 100) == (b"page", False)
    assert docker.read_file(run, "out/page.html", 3) == (None, True)
    (run / "out" / "page.html").unlink()
    (run / "out" / "page.html").symlink_to(secret / "page.html")
    assert docker.read_file(run, "out/page.html", 100) == (None, False)
    (run / "out" / "page.html").unlink()
    (run / "out").rmdir()
    (run / "out").symlink_to(secret)
    assert docker.read_file(run, "out/page.html", 100) == (None, False)


def test_write_json_replaces_a_planted_link_without_writing_through_it(tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.write_text("untouched")
    (tmp_path / "job.json").symlink_to(target)
    docker.write_json(tmp_path, "job.json", {"v": 1})
    assert target.read_text() == "untouched"
    assert not (tmp_path / "job.json").is_symlink()
    assert json.loads((tmp_path / "job.json").read_text()) == {"v": 1}


def test_sidecar_wall_only_takes_a_run_dir_on_the_volume(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(docker, "RENDER_ROOT", tmp_path)
    run = tmp_path / "smartbrain-render-x"
    for sub in ("", "home", "tmp", "storage", "out"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    sock = run / "egress.sock"
    sock.write_text("")
    wall = docker.SidecarWall(str(run), {"kind": "unix", "path": str(sock)})
    assert wall.remote and wall.proxy_port({"kind": "unix", "path": str(sock)}) == docker.RELAY_PORT
    assert oct(run.stat().st_mode & 0o7777) == "0o1770" and oct(sock.stat().st_mode & 0o777) == "0o660"
    assert wall.wrap(["/e/obscura"], {}) == (["/e/obscura"], {}) and wall.verify([])
    elsewhere = tmp_path / "other" / "smartbrain-render-y"
    elsewhere.mkdir(parents=True)
    with pytest.raises(ValueError):
        docker.SidecarWall(str(elsewhere), {"kind": "unix", "path": str(elsewhere / "egress.sock")})
    with pytest.raises(ValueError):
        docker.SidecarWall(str(run), {"kind": "unix", "path": str(tmp_path / "egress.sock")})


@pytest.mark.parametrize(("over", "ok"), [
    ({}, True), ({"run_id": "smartbrain-render-other"}, False), ({"rc": "0"}, False),
    ({"rc": True}, False), ({"verdict": "pwned"}, False), ({"refused": "maybe"}, False),
    ({"signals": ["x" * 3000]}, False), ({"signals": ["x"] * 65}, False),
    ({"rows": [{"row": "a", "ok": "yes", "detail": ""}]}, False), ({"spawned": 1}, False),
])
def test_results_are_closed_and_typed(over: dict, ok: bool) -> None:
    result = {"v": 1, "run_id": "smartbrain-render-x", "spawned": True, "rc": 0, "verdict": "exited",
              "signals": ["Page loaded: https://x.test/"], "refused": "", "rows": [], **over}
    assert docker.valid_result(result, "smartbrain-render-x") is ok


# --- proc.watch: a wall's own helper processes are not engine children --------------------


class _HelperWall(walls.Wall):
    kind = "null"

    def __init__(self, helpers: int) -> None:
        self.helpers = helpers

    def verify(self, pid_tree: list[int]) -> bool:
        return True


def _family(children: int) -> subprocess.Popen:
    """A process group of 1 + ``children`` sleepers (like bootstrap → init → engine)."""
    code = ("import subprocess,sys,time\n"
            f"kids=[subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']) for _ in range({children})]\n"
            "time.sleep(30)\n")
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)


@pytest.mark.parametrize(("helpers", "verdict"), [(2, "timeout"), (1, "children")])
def test_watch_counts_the_walls_helpers(helpers: int, verdict: str) -> None:
    from smartbrain_3000.browsers import proc

    child = _family(2)
    try:
        for _ in range(100):  # the two grandchildren are up
            if len(proc.group_members(child.pid)) == 3:
                break
            time.sleep(0.05)
        assert proc.watch(child, _HelperWall(helpers), rss_mb=4096, deadline_s=1.0) == verdict
    finally:
        assert proc.kill_tree(child)
