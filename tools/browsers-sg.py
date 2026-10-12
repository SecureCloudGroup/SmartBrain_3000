#!/usr/bin/env python3
"""SG canary harness for the browser walls (ni-format §35; P3 design §8) — the security gate
every CI leg runs and the native measurement uses. It drives the REAL pinned engine under the
REAL wall of this machine; it never runs the engine without one.

Modes:
- default (a wall is claimed here): ``probe`` (``walls.check``), ``self_test`` (the wall's
  canary program + ``eligible`` through ``install.ensure``), ``broken_config`` (a tampered
  wall refuses to render; ``verify`` rejects an unconfined process), ``canary_proxy`` and
  ``canary_noproxy`` (a file:// canary page firing fetch/img/WebSocket/beacon at loopback,
  ::1, localhost and its aliases, 0.0.0.0, the LAN address, 169.254.169.254, the app port,
  Bifrost's 38080, a model port and a rebinding name — 0 hits with AND without ``--proxy``),
  ``no_state``, ``about_blank`` (no CONNECT), ``no_listener`` (no listening socket in the
  engine tree), ``kill_child`` and ``kill_parent`` (no orphans), ``caps_memory`` and
  ``caps_cpu`` (a heap bomb and a CPU loop end), ``render_https`` (``--net``: a real render
  through the runner). In Docker (the sidecar wall) the rows are ``probe``, ``self_test``,
  ``broken_config``, ``render_https`` and ``round_trip``.
- ``--expect <reason>``: the wall must be unavailable with exactly that reason, and a render
  must be refused without starting an engine or an egress child.

Every row prints one JSON line; the run fails if any row fails or if the executed row set is
not exactly the mode's set (zero rows = failure: the vacuous-pass lesson).

    PYTHONPATH=app python3 tools/browsers-sg.py [--expect REASON] [--net] [--tarball PATH]
        [--ports APP,GATEWAY,MODEL] [--json OUT]

``--tarball`` installs the pinned asset from a local copy (the size and sha256 checks and the
unpack are still the real installer's); without it the installer downloads from GitHub.
``--ports`` names the app/Bifrost/model ports to target (default 33000,38080,11434; a port
already in use is targeted without a listener). On a machine running SmartBrain, pass spare
ports.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time

_SPEC = importlib.util.spec_from_file_location(
    "browsers_sg_lib", pathlib.Path(__file__).resolve().with_name("browsers_sg_lib.py"))
lib = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lib)

from smartbrain_3000.browsers import (
    egress,
    install,
    release,
    runner,
    walls,
)
from smartbrain_3000.browsers import manifest as mf
from smartbrain_3000.browsers.walls import darwin, docker, linux

LOCAL_ROWS = ("probe", "self_test", "broken_config", "canary_proxy", "canary_noproxy", "no_state",
              "about_blank", "no_listener", "kill_child", "kill_parent", "caps_memory", "caps_cpu")
REMOTE_ROWS = ("probe", "self_test", "broken_config")
EXPECT_ROWS = ("reason", "render_refused", "status_row")
NET_ROWS = ("render_https",)
BUSY_PAGE = "(function(){for(;;){}})();"  # holds the engine until its script watchdog (~15 s)
ROWS: list[dict] = []
LISTEN: dict = {"samples": 0, "found": []}  # every walled run's listener samples


def record(name: str, ok: bool, detail: object = "", **extra: object) -> dict:
    row = {"row": name, "ok": bool(ok), "detail": detail, **extra}
    ROWS.append(row)
    print(json.dumps(row, default=str), flush=True)
    return row


class FileStream:
    def __init__(self, path: str) -> None:
        self._fh = open(path, "rb")  # noqa: SIM115 - closed by close()
        self.status_code = 200
        self.headers = {"content-length": str(os.path.getsize(path))}

    def iter_bytes(self, chunk_size: int):
        for _ in range(1_000_000):
            block = self._fh.read(chunk_size)
            if not block:
                return
            yield block

    def close(self) -> None:
        self._fh.close()


def setup(root: pathlib.Path, tarball: str | None) -> tuple[mf.Manifest, pathlib.Path]:
    """A private data dir, the component enabled, the pinned engine installed (local)."""
    os.environ["SMARTBRAIN_DB_PATH"] = str(root / "data" / "sb.duckdb")
    os.environ.pop(install.DISABLE_ENV, None)
    os.environ.pop(walls.NULLWALL_ENV, None)
    m = mf.load("obscura")
    plat = install.platform_key()
    edir = install.engine_dir(m.name, m.version)
    if walls.remote(walls.host_platform()) or plat is None:
        return m, edir
    if not install.verify_installed(m.name, manifest_obj=m, platform=plat)[0]:
        url = mf.source_url(m, plat)

        def transport(asked: str) -> object:
            assert asked == url, "only the pinned URL is served from the local copy"
            return FileStream(tarball)  # type: ignore[arg-type]

        got = install.install(m, plat, transport=transport if tarball else release.PinnedStream,
                              self_test=lambda _m, _e: "skipped(wall_unavailable)")
        assert got["ok"], f"install failed: {got}"
    assert install.verify_installed(m.name, manifest_obj=m, platform=plat)[0], "engine verified"
    return m, edir


def row_probe(platform: str, edir: pathlib.Path) -> None:
    t0 = time.monotonic()
    got = walls.check(platform, edir)
    record("probe", got is None, str(got), kind=walls.available_kind(platform, edir),
           secs=round(time.monotonic() - t0, 3))


def row_self_test(platform: str, m: mf.Manifest, edir: pathlib.Path) -> None:
    t0 = time.monotonic()
    result, rows = walls.self_test(platform, m, edir, install.browsers_root())
    status = install.ensure(m.name)  # once per boot: records the pass, makes the row eligible
    record("self_test", result == "ok" and status["eligible"] is True, result, rows=rows,
           secs=round(time.monotonic() - t0, 2), sandbox=status["sandbox"])


def row_broken_darwin(m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path) -> None:
    """A tampered profile is refused before anything runs; port 0 is refused; ``verify``
    rejects an unconfined process (a plain ``sleep`` — the engine never runs unwalled)."""
    tampered = root / "tampered.sb"
    tampered.write_text(darwin.PROFILE.read_text() + "\n(allow network*)\n")
    saved, darwin.PROFILE = darwin.PROFILE, tampered
    darwin._cache.pop("probe", None)
    try:
        refused = walls.check("darwin", edir)
        got = runner.render("https://example.com/", site_policy={"mode": "own"},
                            identity={"mode": "mimic", "profile": 0}, timezone="UTC")
    finally:
        darwin.PROFILE = saved
        darwin._cache.pop("probe", None)
    try:
        darwin.profile_params(edir, root, 0)
        port_zero = False
    except ValueError:
        port_zero = True
    sleeper = subprocess.Popen(["/bin/sleep", "5"])
    unconfined_rejected = not darwin.confined([sleeper.pid])
    sleeper.kill()
    sleeper.wait(5)
    ok = (refused is not None and got.status == "unavailable" and got.reason == "sandbox"
          and port_zero and unconfined_rejected and walls.check("darwin", edir) is None)
    record("broken_config", ok, f"check={refused} render={got.status}/{got.reason} port0={port_zero} "
           f"verify_unconfined_rejected={unconfined_rejected}")


def row_broken_linux(m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path) -> None:
    """A bootstrap whose setup fails never runs the engine (fail closed); ``verify`` rejects
    an unconfined process."""
    broken = root / "broken_boot.py"
    broken.write_text("import os\nos._exit(125)\n")
    saved, linux.BOOT = linux.BOOT, broken
    before = lib.engine_processes(str(edir / m.executable))
    try:
        got = runner.render("https://example.com/", site_policy={"mode": "own"},
                            identity={"mode": "mimic", "profile": 0}, timezone="UTC")
    finally:
        linux.BOOT = saved
    sleeper = subprocess.Popen(["/bin/sleep", "5"])
    app = linux._ns("self")
    unconfined_rejected = app is not None and not linux.ns_confined([sleeper.pid], app[0], app[1])
    sleeper.kill()
    sleeper.wait(5)
    ok = got.status != "ok" and not before and unconfined_rejected
    record("broken_config", ok, f"render={got.status}/{got.reason} "
           f"verify_unconfined_rejected={unconfined_rejected}")


def row_broken_remote(root: pathlib.Path) -> None:
    """No sidecar answering (an empty volume), then a stale heartbeat: both refuse."""
    saved = docker.RENDER_ROOT
    empty = root / "render-empty"
    empty.mkdir()
    stale = root / "render-stale"
    stale.mkdir()
    ready = docker.read_json(saved, docker.READY_NAME, docker.READY_KEYS) or {}
    if ready:
        docker.write_json(stale, docker.READY_NAME, ready)
    docker.write_json(stale, docker.HEARTBEAT_NAME, {"v": 1, "boot": ready.get("boot", ""),
                                                     "ts": time.time() - 3600})
    try:
        docker.RENDER_ROOT = empty
        no_sidecar = walls.check("docker", None)
        docker.RENDER_ROOT = stale
        stale_beat = walls.check("docker", None)
    finally:
        docker.RENDER_ROOT = saved
    ok = (str(no_sidecar) == str(stale_beat) == "Unavailable(reason='docker_no_sidecar')"
          and walls.check("docker", None) is None and bool(ready))
    record("broken_config", ok, f"empty={no_sidecar} stale={stale_beat}")


def canary_run(platform: str, m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path,
               lst: object, *, proxy: bool) -> dict:
    """The canary page under the wall, through the in-process egress (resolver seam)."""
    sink = lib.Sink()
    run_dir = lib.make_run_dir(root)
    kind = walls.endpoint_kind(platform, edir)
    server, endpoint = lib.egress(kind, run_dir, sink)
    tok = lib.token()
    page = pathlib.Path(run_dir) / "canary.html"
    page.write_text(lib.canary_page(tok, lib.targets(lst, tok)))
    try:
        got = lib.run_under_wall(platform, m, edir, run_dir, lambda port: lib.test_argv(
            m, str(edir / m.executable), run_dir, f"file://{page}", port, proxy=proxy,
            timeout_s=15), endpoint, deadline_s=25.0)
    finally:
        server.close()
        sink.close()
        lib.cleanup(run_dir)
    got["census"] = server.census()["census"]
    got["page"] = lib.page_results(got.pop("output"))
    return got


def row_canaries(platform: str, m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path,
                 ports: dict[str, int]) -> None:
    lst = lib.Listeners(ports)
    try:
        first = canary_run(platform, m, edir, root, lst, proxy=True)
        hits_first = lst.count()
        second = canary_run(platform, m, edir, root, lst, proxy=False)
        time.sleep(0.5)
        hits = list(lst.hits)
    finally:
        lst.close()
    census = {r["site"]: r for r in first["census"]}
    page1, page2 = first["page"] or {}, second["page"] or {}
    ran1 = page1.get("ran") is True and page1.get("attempts", 0) >= 10
    control = census.get("canary.test", {}).get("tunnels", 0) >= 1
    rebind = census.get("rebind.test", {}).get("verdict") == "unsafe"
    record("canary_proxy", ran1 and control and rebind and hits_first == 0 and first["rc"] == 0,
           f"hits={hits_first} ran={ran1} attempts={page1.get('attempts')} control={control} "
           f"rebind_refused={rebind} rc={first['rc']} verdict={first['verdict']}",
           targets=sorted(lib.targets(lst, "x")), unbound=lst.unbound, census=first["census"])
    ran2 = page2.get("ran") is True
    record("canary_noproxy", ran2 and not hits and second["census"] == [],
           f"hits={len(hits)} ran={ran2} attempts={page2.get('attempts')} rc={second['rc']}")
    fresh = page2.get("prior") in (None, "n/a") and page2.get("cookie", "") in ("", "n/a")
    record("no_state", ran2 and fresh, f"second run prior={page2.get('prior')!r} "
           f"cookie={page2.get('cookie')!r} first set prior={page1.get('prior')!r}")


def row_about_blank(platform: str, m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path) -> None:
    sink = lib.Sink()
    run_dir = lib.make_run_dir(root)
    server, endpoint = lib.egress(walls.endpoint_kind(platform, edir), run_dir, sink)
    try:
        got = lib.run_under_wall(platform, m, edir, run_dir, lambda port: lib.test_argv(
            m, str(edir / m.executable), run_dir, "about:blank", port, proxy=True, timeout_s=10),
            endpoint, deadline_s=20.0)
    finally:
        server.close()
        sink.close()
        lib.cleanup(run_dir)
    census = server.census()["census"]
    record("about_blank", got["rc"] == 0 and census == [] and sink.accepted == 0,
           f"rc={got['rc']} census={census}")


def hanging_page(root: pathlib.Path, run_dir: str, body: str) -> str:
    page = pathlib.Path(run_dir) / "page.html"
    page.write_text(f"<html><head><title>hold</title></head><body><script>{body}</script></body></html>")
    assert page.is_file(), "page written"
    return f"file://{page}"


def row_kill_child(platform: str, m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path) -> None:
    sink = lib.Sink()
    run_dir = lib.make_run_dir(root)
    server, endpoint = lib.egress(walls.endpoint_kind(platform, edir), run_dir, sink)
    url = hanging_page(root, run_dir, BUSY_PAGE)
    engine = str(edir / m.executable)
    try:
        got = lib.run_under_wall(platform, m, edir, run_dir, lambda port: lib.test_argv(
            m, engine, run_dir, url, port, proxy=True, timeout_s=20), endpoint, deadline_s=30.0,
            kill_after_s=1.0)
    finally:
        server.close()
        sink.close()
        lib.cleanup(run_dir)
    left = lib.engine_processes(engine)
    record("kill_child", got["killed"] and got["tree_clean"] and not left,
           f"killed={got['killed']} tree_clean={got['tree_clean']} left={left} secs={got['secs']}")


def row_kill_parent(platform: str, m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path) -> None:
    run_dir = lib.make_run_dir(root)
    url = hanging_page(root, run_dir, BUSY_PAGE)
    cfg = {"engine": m.name, "edir": str(edir), "run_dir": run_dir, "platform": platform,
           "url": url, "timeout_s": 8}
    parent = subprocess.Popen([sys.executable, str(pathlib.Path(lib.__file__)), "--parent", json.dumps(cfg)],
                              stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    group = json.loads(parent.stdout.readline())["group"]
    time.sleep(1.0)
    alive = len(lib.engine_pids([p for p, _ in lib.proc.group_members(group)], str(edir / m.executable)))
    os.kill(parent.pid, signal.SIGKILL)
    parent.wait(5)
    t0 = time.monotonic()
    bound = 4.0 if platform == "linux" else 8 + 25.0  # Linux: PDEATHSIG; macOS: the engine's own deadline
    gone = False
    for _ in range(int(bound / 0.2) + 1):
        if not lib.proc.group_members(group):
            gone = True
            break
        time.sleep(0.2)
    lifetime = round(time.monotonic() - t0, 1)
    if not gone:
        os.killpg(group, signal.SIGKILL)
    lib.cleanup(run_dir)
    record("kill_parent", gone and alive > 0, f"engine alive at the kill={alive}; group gone "
           f"{lifetime}s after the parent's SIGKILL (bound {bound}s)", lifetime_s=lifetime)


def row_caps(platform: str, m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path) -> None:
    for name, body, timeout_s in (
            ("caps_memory", "(function(){var a=[];for(;;){a.push(new Array(1e6).fill(1))}})();", 15),
            ("caps_cpu", "(function(){for(;;){}})();", 5)):
        sink = lib.Sink()
        run_dir = lib.make_run_dir(root)
        server, endpoint = lib.egress(walls.endpoint_kind(platform, edir), run_dir, sink)
        url = hanging_page(root, run_dir, body)
        try:
            got = lib.run_under_wall(
                platform, m, edir, run_dir,
                lambda port, u=url, t=timeout_s, r=run_dir: lib.test_argv(
                    m, str(edir / m.executable), r, u, port, proxy=True, timeout_s=t),
                endpoint, deadline_s=timeout_s + 20.0)
        finally:
            server.close()
            sink.close()
            lib.cleanup(run_dir)
        kill = runner._stderr_kill(got["signals"])
        ended = got["verdict"] in ("exited", "memory", "timeout") and got["tree_clean"]
        wanted = (kill == "heap" or got["verdict"] == "memory") if name == "caps_memory" else (
            kill == "script_watchdog" or got["rc"] == 124 or got["verdict"] == "timeout")
        record(name, ended and wanted, f"verdict={got['verdict']} rc={got['rc']} kill={kill!r} "
               f"secs={got['secs']}")


def row_render_https(platform: str) -> None:
    t0 = time.monotonic()
    got = runner.render("https://example.com/", site_policy={"mode": "own"},
                        identity={"mode": "mimic", "profile": 0}, timezone="UTC")
    rows = {r["site"]: r for r in got.census}
    ok = got.ok and rows.get("example.com", {}).get("tunnels", 0) >= 1 and bool(got.html)
    record("render_https", ok, f"{got.status}/{got.reason} html={len(got.html or b'')}B",
           timings=got.timings, secs=round(time.monotonic() - t0, 2), census=got.census)


def row_round_trip() -> None:
    """Sidecar job round trip: render timing and no run dir left on the volume."""
    t0 = time.monotonic()
    got = runner.render("https://example.com/", site_policy={"mode": "own"},
                        identity={"mode": "rotate"}, timezone="UTC")
    left = [p.name for p in docker.RENDER_ROOT.iterdir() if p.name.startswith(docker.RUN_PREFIX)]
    record("round_trip", got.ok and not left, f"{got.status}/{got.reason} left={left}",
           timings=got.timings, secs=round(time.monotonic() - t0, 2))


def expect_rows(platform: str, m: mf.Manifest, edir: pathlib.Path, reason: str) -> None:
    got = walls.check(platform, edir)
    record("reason", got is not None and got.reason == reason, str(got))
    started: list[str] = []
    real_start = egress.start

    def counting(*args: object, **kwargs: object) -> object:
        started.append("egress")
        return real_start(*args, **kwargs)

    egress.start = counting
    try:
        before = lib.engine_processes(str(edir / m.executable))
        res = runner.render("https://example.com/", site_policy={"mode": "own"},
                            identity={"mode": "mimic", "profile": 0}, timezone="UTC")
    finally:
        egress.start = real_start
    record("render_refused", res.status == "unavailable" and res.reason == reason and not started
           and not before, f"{res.status}/{res.reason} egress_started={len(started)}")
    row = install.status_row(m)
    record("status_row", row["eligible"] is False and row["reason"] == reason
           and row["sandbox"] == {"kind": "none", "verified_at": None}, json.dumps(row))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--expect", default="")
    ap.add_argument("--net", action="store_true")
    ap.add_argument("--tarball", default=None)
    ap.add_argument("--ports", default="33000,38080,11434")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    ports = dict(zip(("app", "gateway", "model"), (int(p) for p in args.ports.split(",")), strict=True))
    platform = walls.host_platform()
    lib.LISTEN_SINK = LISTEN
    root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="smartbrain-sg-")))
    print(json.dumps({"platform": platform, "expect": args.expect, "net": args.net}), flush=True)
    try:
        m, edir = setup(root, args.tarball)
        expected = run_mode(platform, m, edir, root, args, ports)
    finally:
        lib.cleanup(str(root))
    executed = [r["row"] for r in ROWS]
    failed = [r["row"] for r in ROWS if not r["ok"]]
    complete = sorted(executed) == sorted(expected) and len(executed) > 0
    summary = {"platform": platform, "rows": len(executed), "failed": failed,
               "missing": sorted(set(expected) - set(executed)), "complete": complete}
    print(json.dumps({"summary": summary}), flush=True)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({"summary": summary, "rows": ROWS}, default=str))
    return 0 if complete and not failed else 1


def run_mode(platform: str, m: mf.Manifest, edir: pathlib.Path, root: pathlib.Path,
             args: argparse.Namespace, ports: dict[str, int]) -> tuple[str, ...]:
    if args.expect:
        expect_rows(platform, m, edir, args.expect)
        return EXPECT_ROWS
    row_probe(platform, edir)
    row_self_test(platform, m, edir)
    if walls.remote(platform):
        row_broken_remote(root)
        if args.net:
            row_render_https(platform)
            row_round_trip()
        return REMOTE_ROWS + ((*NET_ROWS, "round_trip") if args.net else ())
    (row_broken_darwin if platform == "darwin" else row_broken_linux)(m, edir, root)
    row_canaries(platform, m, edir, root, ports)
    row_about_blank(platform, m, edir, root)
    row_kill_child(platform, m, edir, root)
    row_kill_parent(platform, m, edir, root)
    row_caps(platform, m, edir, root)
    record("no_listener", LISTEN["samples"] >= 5 and not LISTEN["found"],
           f"samples={LISTEN['samples']} found={LISTEN['found'][:5]}")
    if args.net:
        row_render_https(platform)
    return LOCAL_ROWS + (NET_ROWS if args.net else ())


if __name__ == "__main__":
    sys.exit(main())
