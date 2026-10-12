"""Real-engine rows (§35): the pinned Obscura binary against the manifest's own argv and the
real egress child. Skipped unless ``SMARTBRAIN_BROWSER_REAL_ENGINE=<path to obscura>``; when
it is set, every row must run and the last test fails on zero (or missing) executed rows.

No row needs a network: the targets are a loopback alias the engine's own SSRF guard does
NOT catch (only the egress child stops it), private IP literals the engine refuses before
any CONNECT, a plain ``http://`` page (refused at the egress), and ``about:blank``. There is
no wall here (B2), which is why these drive the binary directly instead of
``runner.render`` — the runner itself refuses to render without one."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from smartbrain_3000.browsers import cmdline, egress, proc, runner
from smartbrain_3000.browsers import manifest as mf

REAL = os.environ.get("SMARTBRAIN_BROWSER_REAL_ENGINE", "")
pytestmark = pytest.mark.skipif(not REAL, reason="set SMARTBRAIN_BROWSER_REAL_ENGINE=<obscura binary>")
ROWS = ("version", "alias_refused_by_egress", "assets_template", "plain_http_refused",
        "private_literals_refused_by_engine", "about_blank_no_connect", "no_proxy_guard")
EXECUTED: list[str] = []


def _loopback_alias() -> str:
    """A name for this machine's loopback/broadcast that is not literally ``localhost``."""
    try:
        lines = Path("/etc/hosts").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] in ("127.0.0.1", "::1", "255.255.255.255"):
            for name in parts[1:]:
                if name != "localhost" and not name.endswith(".localhost"):
                    return name.lower()
    return socket.gethostname().lower()


def _engine_run(argv_tail: list[str], *, main_host: str, policy: str = "own",
                proxy: bool = True) -> tuple[int, list[str], list[dict]]:
    """Run the real binary once (no wall), return (rc, stderr marker lines, census)."""
    run_dir = runner.make_run_dir()
    m = mf.load("obscura")
    handle = egress.start(run_dir, kind="tcp", policy=policy, sites=[], main_host=main_host,
                          main_port=443, max_life_s=60)
    try:
        head = ["--v8-flags", "--max-old-space-size=256"]
        if proxy:
            head += ["--proxy", f"http://127.0.0.1:{handle.endpoint['port']}"]
        env = cmdline.render_env(m, run_dir=run_dir, timezone="UTC", profile=0)
        child = subprocess.Popen([REAL, *head, "--storage-dir", f"{run_dir}/storage", *argv_tail],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, cwd=run_dir, env=env,
                                 start_new_session=True)
        tap = proc.StderrTap(child)
        tap.start()
        try:
            child.wait(timeout=60)
        finally:
            proc.kill_tree(child)
            tap.join(10)
        rows, _ = handle.stop()
        return child.returncode, tap.signals(), rows
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_version_matches_the_manifest() -> None:
    out = subprocess.run([REAL, "--version"], capture_output=True, text=True, timeout=30,
                         check=False)
    assert out.returncode == 0 and out.stdout.strip() == f"obscura {mf.load('obscura').version}"
    EXECUTED.append("version")


def test_a_loopback_alias_the_engine_forwards_is_refused_by_the_egress() -> None:
    host = _loopback_alias()
    url = f"https://{host}/"
    m = mf.load("obscura")
    run_dir = runner.make_run_dir()
    try:  # the manifest's own rendered argv, minus the executable
        argv = cmdline.render_argv(m, engine_path=REAL, url=url, run_dir=run_dir, proxy_port=1,
                              timeout_s=10, output_path=f"{run_dir}/out/page.html",
                              user_agent=None, want_assets=False)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    tail = argv[argv.index("fetch"):]
    rc, signals, census = _engine_run(tail, main_host=host)
    assert rc != 0 and any("failed to navigate" in s.lower() for s in signals)
    rows = {r["site"]: r for r in census}
    assert host in rows, census  # the engine sent the CONNECT by name...
    assert rows[host]["tunnels"] == 0 and rows[host]["verdict"] in ("unsafe", "resolve")  # ...refused
    EXECUTED.append("alias_refused_by_egress")


def test_the_assets_template_is_accepted() -> None:
    host = _loopback_alias()
    rc, _signals, census = _engine_run(["fetch", f"https://{host}/", "--timeout", "10",
                                        "--wait-until", "load", "--dump", "assets",
                                        "--output", "/dev/null"], main_host=host)
    assert rc != 0 and [r["site"] for r in census] == [host]
    EXECUTED.append("assets_template")


def test_a_plain_http_page_reaches_the_egress_only_as_a_refused_get() -> None:
    rc, signals, census = _engine_run(["fetch", "http://plain.example/", "--timeout", "10",
                                       "--dump", "html", "--output", "/dev/null"],
                                      main_host="plain.example")
    assert census == [{"site": "plain.example", "tunnels": 0, "up": 0, "down": 0, "verdict": "method"}]
    assert rc == 0 and any(s.startswith("Page loaded: http://") for s in signals)  # why http is refused up front
    EXECUTED.append("plain_http_refused")


def test_private_literals_are_refused_before_any_connect() -> None:
    for literal in ("127.0.0.1", "[::1]", "10.0.0.1", "169.254.169.254", "192.168.1.1"):
        rc, signals, census = _engine_run(["fetch", f"https://{literal}/", "--timeout", "10",
                                           "--dump", "html", "--output", "/dev/null"],
                                          main_host="example.com")
        assert rc != 0 and census == [], (literal, census)
        assert any("failed to navigate" in s.lower() for s in signals)
    EXECUTED.append("private_literals_refused_by_engine")


def test_about_blank_makes_no_connect() -> None:
    rc, signals, census = _engine_run(["fetch", "about:blank", "--timeout", "10", "--dump", "html",
                                       "--output", "/dev/null"], main_host="example.com")
    assert rc == 0 and census == [] and "Page loaded: about:blank" in " ".join(signals)
    EXECUTED.append("about_blank_no_connect")


def test_without_the_proxy_flag_the_engine_guard_still_refuses_loopback() -> None:
    rc, signals, census = _engine_run(["fetch", "https://localhost:1/", "--timeout", "10",
                                       "--dump", "html", "--output", "/dev/null"],
                                      main_host="example.com", proxy=False)
    assert rc != 0 and census == [] and any("failed to navigate" in s.lower() for s in signals)
    EXECUTED.append("no_proxy_guard")


def test_every_real_engine_row_executed() -> None:
    assert sorted(EXECUTED) == sorted(ROWS), f"executed {EXECUTED} of {ROWS}"
