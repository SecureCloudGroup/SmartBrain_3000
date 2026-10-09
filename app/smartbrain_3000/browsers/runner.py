"""Browser component runner (ni-format §35): one render = one engine ``fetch`` subprocess,
under an OS wall, with the egress child as its only network route.

Flow: validate the request (https URL normalised to IDNA ASCII, site policy, identity,
zone, timeout) → re-hash the installed engine (``install.verify_installed``, every launch)
→ ``walls.check`` (no wall = ``unavailable``, nothing spawned) → the single render slot
(``busy`` is a capacity status, never a failure) → a fresh 0700 run dir (HOME, TMPDIR,
XDG, cwd, ``--storage-dir`` and the output file all inside it; it holds the engine's
cookie file, so it is removed on every path) → the egress child → ``walls.prepare`` with
the egress endpoint → the manifest's argv/env → ``proc.spawn`` → ``proc.watch`` (timeout +
5 s grace, tree RSS, child processes, ``Wall.verify``) → read the output under the cap →
tripwires → ``proc.kill_tree`` (no live member left) → stop the egress child.

Tripwires, in order (fail closed): a child process is ``confinement``; a heap-limit or
script-watchdog kill (exit 0, announced only on stderr — measured) is ``oversize`` /
``timeout``, never ``ok``; a navigation failure naming 403/429/a challenge is ``blocked``;
an off-policy refusal after the main site was reached is ``offsite``; the egress census
MUST show an established tunnel to the main document's site (else ``confinement``); the
final URL the engine reports (``Page loaded: <url>``) must be https on the policy's own
site (else ``offsite``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import netguard
from ..pagegraph import registrable_domain
from . import cmdline, install, proc, walls
from . import egress as egress_mod
from . import manifest as mf

STATUSES = ("ok", "busy", "unavailable", "refused", "timeout", "blocked", "offsite",
            "confinement", "oversize", "engine_error")
_LIVE_RENDERS = threading.Semaphore(1)  # one engine at a time; never on the tick thread
_GRACE_S = 5.0
_ASSETS_FILE_CAP = 2 * 1024 * 1024
_MAX_ASSETS = 500
_MAX_ASSET_LINES = 20_000
_MAX_URL = 2048
_MAX_SITES = 12
_MAX_CARD_ID = 128
_PROBE_TIMEOUT_S = 15.0
_PROBE_OUTPUT_CAP = 4096
_HARD_TIMEOUT_RC = 124  # the engine's own "hard timeout exceeded; forcing exit"
_RUN_PARENT: str | None = None  # None = the system temp dir; tests point it at tmp_path
_BLOCKED_RE = re.compile(r"\b(?:403|429)\b|forbidden|too many requests|challenge|captcha",
                         re.IGNORECASE)
_URL_RE = re.compile(r"\S+://\S+")
_LOADED_RE = re.compile(r"^page loaded:\s+(\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    status: str
    reason: str
    html: bytes | None
    final_url: str | None
    assets: list[str] | None
    engine: str
    version: str
    identity: dict
    timings: dict
    census: list[dict]


@dataclass(frozen=True)
class _Request:
    m: mf.Manifest
    url: str
    host: str
    port: int
    site: str
    policy: str
    sites: tuple[str, ...]
    identity: dict  # {mode, profile, user_agent, consistent}
    timezone: str
    timeout_s: int
    platform: str
    wall_platform: str


@dataclass
class _Outcome:
    spawned: bool = False
    rc: int | None = None
    verdict: str = ""
    signals: list[str] = field(default_factory=list)
    output: bytes | None = None
    oversize: bool = False

    def clean(self) -> bool:
        assert isinstance(self.signals, list), "signal lines are a list"
        assert self.rc is None or isinstance(self.rc, int), "exit code is an int"
        return (self.spawned and self.rc == 0 and self.verdict == "exited"
                and not self.oversize and not _stderr_kill(self.signals))


def _ms(since: float) -> int:
    assert since > 0, "a monotonic start is required"
    assert time.monotonic() >= since, "monotonic time never goes back"
    return max(0, int((time.monotonic() - since) * 1000))


def _early(engine: str, version: str, status: str, reason: str, started: float) -> RenderResult:
    """A result produced before anything ran (refused / unavailable)."""
    assert status in STATUSES and status != "ok", "early results are never ok"
    assert isinstance(reason, str) and reason, "early results carry a reason"
    return RenderResult(ok=False, status=status, reason=reason, html=None, final_url=None,
                        assets=None, engine=engine, version=version,
                        identity={"mode": "", "profile": None, "user_agent": None,
                                  "consistent": False},
                        timings={"egress_ms": 0, "render_ms": 0, "total_ms": _ms(started)},
                        census=[])


def _result(req: _Request, status: str, reason: str, started: float, *,
            outcome: _Outcome | None = None, assets: list[str] | None = None,
            census: list[dict] | None = None, egress_ms: int = 0,
            render_ms: int = 0) -> RenderResult:
    assert status in STATUSES, f"unknown status {status}"
    assert (status == "ok") == (reason == ""), "only ok results carry no reason"
    final = _final_url(outcome.signals) if outcome is not None else None
    return RenderResult(
        ok=status == "ok", status=status, reason=reason,
        html=outcome.output if status == "ok" and outcome is not None else None,
        final_url=final[1] if final is not None and final[1] else None,
        assets=assets if status == "ok" else None, engine=req.m.name, version=req.m.version,
        identity=dict(req.identity),
        timings={"egress_ms": egress_ms, "render_ms": render_ms, "total_ms": _ms(started)},
        census=list(census or []))


def render(url: str, *, engine: str = "obscura", site_policy: dict, identity: dict,
           timezone: str, timeout_s: int | None = None,
           want_assets: bool = False) -> RenderResult:
    """Render ``url`` with ``engine`` (see the module docstring). Never raises for a page,
    an engine or a policy outcome — every one is a status."""
    started = time.monotonic()
    assert started > 0, "a monotonic start"
    req = _request(url, engine, site_policy, identity, timezone, timeout_s, started)
    assert isinstance(req, (_Request, RenderResult)), "a request or an early result"
    if isinstance(req, RenderResult):
        return req
    if not _LIVE_RENDERS.acquire(blocking=False):
        return _result(req, "busy", "render_busy", started)
    try:
        return _render_locked(req, bool(want_assets), started)
    finally:
        _LIVE_RENDERS.release()


def _request(url: object, engine: str, site_policy: object, identity: object,
             timezone: object, timeout_s: object, started: float) -> _Request | RenderResult:
    """Validate every input before anything runs; refusals are results, not exceptions."""
    assert started > 0, "a monotonic start"
    if engine not in mf.SELECTABLE:
        return _early(str(engine)[:32], "", "refused", "tier_disabled", started)
    try:
        m = mf.load(engine)
    except ValueError:
        return _early(engine, "", "unavailable", "manifest", started)
    if install.disabled():
        return _early(m.name, m.version, "unavailable", "disabled", started)
    try:
        normal, host, port = cmdline.validate_url(url)
        site = registrable_domain(host)
        policy, sites = _policy(site_policy, site)
        who = _identity(m, identity)
        zone = cmdline.validate_timezone(timezone)
        limit = _timeout(m, timeout_s)
    except ValueError as exc:
        return _early(m.name, m.version, "refused", str(exc) or "bad_request", started)
    plat = install.platform_key()
    blocker = install.install_blocker(m, plat)
    if blocker:
        return _early(m.name, m.version, "unavailable", blocker, started)
    assert plat is not None, "an unblocked platform is known"
    return _Request(m=m, url=normal, host=host, port=port, site=site, policy=policy,
                    sites=sites, identity=who, timezone=zone, timeout_s=limit,
                    platform=plat, wall_platform=walls.host_platform())


def _policy(site_policy: object, site: str) -> tuple[str, tuple[str, ...]]:
    """``{mode: own|open|sealed, sites?: [registrable domains]}``; sealed lists 1-12 unique
    registrable domains with the URL's own site first."""
    assert isinstance(site, str), "site required"
    if (not isinstance(site_policy, dict) or "mode" not in site_policy
            or set(site_policy) - {"mode", "sites"}):
        raise ValueError("bad_policy")
    mode, sites = site_policy["mode"], site_policy.get("sites") or []
    if mode not in ("own", "open", "sealed") or not isinstance(sites, list):
        raise ValueError("bad_policy")
    if mode != "sealed":
        if sites:
            raise ValueError("bad_policy")
        return mode, ()
    if not 1 <= len(sites) <= _MAX_SITES or len(set(map(str, sites))) != len(sites):
        raise ValueError("bad_policy")
    if any(not isinstance(s, str) or registrable_domain(s) != s for s in sites):
        raise ValueError("bad_policy")
    if sites[0] != site:
        raise ValueError("bad_policy")
    assert len(sites) <= _MAX_SITES, "sealed sites are capped"
    return "sealed", tuple(sites)


def _identity(m: mf.Manifest, identity: object) -> dict:
    """``{mode?, profile?, card?}`` → ``{mode, profile, user_agent, consistent}``.

    ``mimic`` (the manifest default) pins one built-in profile: ``profile`` directly, or
    ``card`` → sha256(card id) mod pool. ``rotate`` draws a fresh profile per run.
    ``honest`` sends SmartBrain's own UA — the engine's JS surface still claims Chrome
    (measured), so the result records ``consistent: False``."""
    assert isinstance(m, mf.Manifest), "manifest required"
    assert m.identity_mode in mf.IDENTITY_MODES, "the manifest default is closed"
    if not isinstance(identity, dict) or set(identity) - {"mode", "profile", "card"}:
        raise ValueError("bad_identity")
    mode = identity.get("mode", m.identity_mode)
    profile, card = identity.get("profile"), identity.get("card")
    if mode == "honest" and profile is None and card is None:
        ua = cmdline.validate_user_agent(netguard.USER_AGENT)
        return {"mode": "honest", "profile": None, "user_agent": ua, "consistent": False}
    if mode not in ("mimic", "rotate"):
        raise ValueError("bad_identity")
    if m.pool_size < 1:
        raise ValueError("identity_unavailable")
    if mode == "rotate" and profile is None and card is None:
        pick = secrets.randbelow(m.pool_size)
    elif mode == "mimic" and card is not None and profile is None:
        if not isinstance(card, str) or not 1 <= len(card) <= _MAX_CARD_ID:
            raise ValueError("bad_identity")
        pick = cmdline.card_profile(card, m.pool_size)
    elif (mode == "mimic" and card is None and isinstance(profile, int)
          and not isinstance(profile, bool) and 0 <= profile < m.pool_size):
        pick = profile
    else:
        raise ValueError("bad_identity")
    return {"mode": mode, "profile": pick, "user_agent": None, "consistent": True}


def _timeout(m: mf.Manifest, timeout_s: object) -> int:
    assert m.limits.timeout_s > 0, "manifest timeout required"
    if timeout_s is None:
        return m.limits.timeout_s
    if (not isinstance(timeout_s, int) or isinstance(timeout_s, bool)
            or not 1 <= timeout_s <= m.limits.timeout_s):
        raise ValueError("bad_timeout")
    assert 1 <= timeout_s <= m.limits.timeout_s, "timeout within the manifest cap"
    return timeout_s


def _render_locked(req: _Request, want_assets: bool, started: float) -> RenderResult:
    """Under the render slot: hash check, wall pre-check, then one guarded run."""
    assert isinstance(req, _Request), "a validated request"
    assert started > 0, "a monotonic start"
    ok, why = install.verify_installed(req.m.name, manifest_obj=req.m, platform=req.platform)
    if not ok:
        return _result(req, "unavailable", why, started)
    edir = install.engine_dir(req.m.name, req.m.version)
    blocked = walls.check(req.wall_platform, edir)
    if blocked is not None:
        return _result(req, "unavailable", blocked.reason, started)
    run_dir = make_run_dir()
    try:
        return _execute(req, edir, run_dir, want_assets, started)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _execute(req: _Request, edir: Path, run_dir: str, want_assets: bool,
             started: float) -> RenderResult:
    """Egress child → wall → engine run(s) → census → tripwires; always tears down."""
    assert edir.name == req.m.version, "the committed version runs"
    assert os.path.isdir(run_dir), "run dir must exist"
    handle: egress_mod.Egress | None = None
    wall: walls.Wall | walls.Unavailable | None = None
    try:
        t0 = time.monotonic()
        handle = egress_mod.start(
            run_dir, kind=walls.endpoint_kind(req.wall_platform, edir), policy=req.policy,
            sites=list(req.sites), main_host=req.host, main_port=req.port,
            max_life_s=float(min(600, 2 * (req.timeout_s + _GRACE_S) + 30)))
        egress_ms = _ms(t0)
        wall = walls.prepare(req.wall_platform, run_dir, str(edir), handle.endpoint)
        if isinstance(wall, walls.Unavailable):
            return _result(req, "unavailable", wall.reason, started, egress_ms=egress_ms)
        if handle.endpoint["kind"] != "tcp":  # the in-namespace relay arrives with the walls
            return _result(req, "unavailable", "relay_pending", started, egress_ms=egress_ms)
        t1 = time.monotonic()
        outcome = _run_engine(req, wall, handle.endpoint, run_dir, edir, want_assets=False)
        assets = _assets(req, wall, handle.endpoint, run_dir, edir) \
            if want_assets and outcome.clean() else None
        render_ms = _ms(t1)
        current, handle = handle, None  # stopped exactly once, even when the census is bad
        rows, _dropped = current.stop()  # the census is complete once the engine is gone
        status, reason = _classify(req, outcome, rows)
        return _result(req, status, reason, started, outcome=outcome, assets=assets,
                       census=rows, egress_ms=egress_ms, render_ms=render_ms)
    except egress_mod.EgressError as exc:
        status = "unavailable" if exc.reason in ("spawn", "start") else "confinement"
        return _result(req, status, f"egress_{exc.reason}", started)
    finally:
        if handle is not None:
            try:
                handle.stop()
            except egress_mod.EgressError:
                pass
        if isinstance(wall, walls.Wall):
            wall.teardown()


def make_run_dir() -> str:
    """A fresh 0700 run dir with home/ tmp/ storage/ out/ inside it."""
    run_dir = tempfile.mkdtemp(prefix="smartbrain-render-", dir=_RUN_PARENT)
    os.chmod(run_dir, 0o700)
    for sub in ("home", "tmp", "storage", "out"):  # fixed set
        os.mkdir(os.path.join(run_dir, sub), 0o700)
    assert os.path.isdir(run_dir), "run dir must exist"
    assert os.stat(run_dir).st_mode & 0o077 == 0, "the run dir is private"
    return run_dir


def _run_engine(req: _Request, wall: walls.Wall, endpoint: dict, run_dir: str, edir: Path, *,
                want_assets: bool) -> _Outcome:
    """One engine process from spawn to a dead group; never raises for engine behaviour."""
    assert endpoint["kind"] == "tcp", "the engine dials a loopback port"
    assert isinstance(want_assets, bool), "want_assets is a flag"
    out_path = os.path.join(run_dir, "out", "assets.ndjson" if want_assets else "page.html")
    argv = cmdline.render_argv(req.m, engine_path=str(edir / req.m.executable), url=req.url,
                               run_dir=run_dir, proxy_port=int(endpoint["port"]),
                               timeout_s=req.timeout_s, output_path=out_path,
                               user_agent=req.identity["user_agent"], want_assets=want_assets)
    env = cmdline.render_env(req.m, run_dir=run_dir, timezone=req.timezone,
                             profile=req.identity["profile"])
    argv, env = wall.wrap(argv, env)
    outcome = _Outcome()
    try:
        child = proc.spawn(argv, env, run_dir, cpu_s=int(4 * (req.timeout_s + _GRACE_S)),
                           data_mb=req.m.limits.rlimit_data_mb)
    except (OSError, ValueError):
        return outcome
    outcome.spawned = True
    tap = proc.StderrTap(child)
    tap.start()
    try:
        outcome.verdict = proc.watch(child, wall, req.m.limits.rss_mb, req.timeout_s + _GRACE_S)
    finally:
        proc.kill_tree(child)
        tap.join(_GRACE_S)
    outcome.rc = child.returncode
    outcome.signals = tap.signals()
    if outcome.rc == 0 and outcome.verdict == "exited":
        cap = _ASSETS_FILE_CAP if want_assets else req.m.limits.html_cap_bytes
        outcome.output, outcome.oversize = proc.read_capped_file(out_path, cap)
    return outcome


def _assets(req: _Request, wall: walls.Wall, endpoint: dict, run_dir: str,
            edir: Path) -> list[str] | None:
    """Second run with ``--dump assets``: https sub-resource URLs, deduped, ≤ 500."""
    assert "assets" in req.m.capabilities, "the engine dumps assets"
    outcome = _run_engine(req, wall, endpoint, run_dir, edir, want_assets=True)
    if not outcome.clean() or outcome.output is None:
        return None
    urls: list[str] = []
    for index, line in enumerate(outcome.output.splitlines()):  # bounded below
        if index >= _MAX_ASSET_LINES or len(urls) >= _MAX_ASSETS:
            break
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        url = obj.get("url") if isinstance(obj, dict) else None
        if (isinstance(url, str) and url.startswith("https://") and len(url) <= _MAX_URL
                and not re.search(r"[\s\x00-\x1f\x7f]", url) and url not in urls):
            urls.append(url)
    assert len(urls) <= _MAX_ASSETS, "assets are capped"
    return urls


def _stderr_kill(signals: list[str]) -> str:
    """``heap`` / ``script_watchdog`` when stderr announces an engine-side kill."""
    assert isinstance(signals, list), "signal lines required"
    assert all(isinstance(s, str) for s in signals), "signal lines are text"
    text = "\n".join(signals).lower()
    if "v8 heap limit reached" in text:
        return "heap"
    return "script_watchdog" if "v8 watchdog fired" in text else ""


def _final_url(signals: list[str]) -> tuple[str, str] | None:
    """The ``Page loaded: <url>`` the engine announced: ``(raw, normalised-or-"")``."""
    assert isinstance(signals, list), "signal lines required"
    assert all(isinstance(s, str) for s in signals), "signal lines are text"
    for line in signals:  # bounded: ≤ 64 lines
        match = _LOADED_RE.match(line)
        if match:
            raw = match.group(1)[:_MAX_URL]
            try:
                return raw, cmdline.validate_url(raw)[0]
            except ValueError:
                return raw, ""
    return None


def _final_on_policy(req: _Request, normal: str) -> bool:
    """https, and on the request's own site unless the policy is ``open``."""
    assert isinstance(normal, str), "url required"
    assert req.policy in ("own", "open", "sealed"), "policy is closed"
    if not normal:
        return False
    if req.policy == "open":
        return True
    home = req.sites[0] if req.policy == "sealed" else req.site
    return registrable_domain(cmdline.validate_url(normal)[1]) == home


def _classify(req: _Request, outcome: _Outcome, rows: list[dict]) -> tuple[str, str]:
    """The tripwires, in order. Stderr is matched with every URL removed first."""
    assert isinstance(rows, list), "census rows required"
    assert outcome.verdict in ("", "exited", "timeout", "memory", "wall", "children"), \
        "watch verdicts are closed"
    main = any(r["site"] == req.site and r["tunnels"] >= 1 for r in rows)
    kill = _stderr_kill(outcome.signals)
    final = _final_url(outcome.signals)
    if not outcome.spawned:
        return "engine_error", "spawn"
    verdicts = {"children": ("confinement", "child_spawned"), "wall": ("confinement", "wall_verify"),
                "memory": ("oversize", "rss"), "timeout": ("timeout", "deadline")}
    if outcome.verdict in verdicts:
        return verdicts[outcome.verdict]
    if outcome.rc == _HARD_TIMEOUT_RC or kill == "script_watchdog":
        return "timeout", "deadline" if outcome.rc == _HARD_TIMEOUT_RC else kill
    if kill == "heap":
        return "oversize", "heap"
    if outcome.rc != 0:
        text = _URL_RE.sub("", "\n".join(outcome.signals)).lower()
        if "failed to navigate" in text and _BLOCKED_RE.search(text):
            return "blocked", "refused"
        if main and any(r["verdict"] == "offsite" for r in rows):
            return "offsite", "redirect"
        if outcome.rc is not None and outcome.rc < 0:
            return "engine_error", "crash"
        return "engine_error", "navigate" if "failed to navigate" in text else "exit"
    if not main:
        return "confinement", "no_main_connect"
    if final is not None and not _final_on_policy(req, final[1]):
        return "offsite", "final_url"
    if outcome.oversize:
        return "oversize", "html_cap"
    return ("ok", "") if outcome.output is not None else ("engine_error", "no_output")


def probe_version(m: mf.Manifest, engine_dir: Path) -> str:
    """The install self-test: ``<engine> --version`` under the wall, no network at all.
    ``ok`` when it exits 0 and prints the pinned version."""
    assert isinstance(engine_dir, Path), "engine dir required"
    assert m.executable and m.version, "manifest names the executable and version"
    run_dir = make_run_dir()
    try:
        wall = walls.prepare(walls.host_platform(), run_dir, str(engine_dir), None)
        if isinstance(wall, walls.Unavailable):
            return "skipped(wall_unavailable)"
        env = cmdline.render_env(m, run_dir=run_dir, timezone="UTC", profile=None)
        argv, env = wall.wrap([str(engine_dir / m.executable), "--version"], env)
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=run_dir, env=env,
                                     close_fds=True, start_new_session=True)
        except OSError:
            return "failed:spawn"
        try:
            out, _ = child.communicate(timeout=_PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return "failed:timeout"
        finally:
            proc.kill_tree(child)
            wall.teardown()
        if child.returncode != 0:
            return "failed:exit"
        printed = out[:_PROBE_OUTPUT_CAP].decode("ascii", "replace")
        return "ok" if m.version in printed else "failed:output"
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
