#!/usr/bin/env python3
"""SmartBrain doctor — find out what is wrong with this install, and fix what is safe to fix.

SmartBrain runs as two ordinary processes on this computer: the app on 127.0.0.1:33000
and its model gateway on 127.0.0.1:38080. Both are started from a versioned install the
launcher assembles under your user data folder. There are no containers, so this tool
looks at files, processes, ports and the two loopback APIs — nothing else.

Design rules, in the order they matter:

* It must work when the app is DOWN. That is when people run it. Every check degrades to
  a useful answer instead of an exception, and nothing here needs the app to be alive.
* It is READ-ONLY unless you pass --fix. Then each repair is described in full and
  confirmed one at a time. Your database is never touched by any of them, and none of
  them kills or signals a process — the one that restarts SmartBrain does it by asking
  SmartBrain, through the same button the app itself offers, and says so before it runs.
* It never mentions Docker on a machine that runs the Docker-free build — except in the
  one case where a leftover container is genuinely holding a port we need, and on the
  platforms (Intel Macs, ARM Linux) that have no native build at all.

Run it directly (``python3 installer/doctor.py``) or through the installer
(``python3 installer/install.py doctor``). ``--fix`` offers repairs; ``-v`` prints the
checks that passed as well as the ones that did not.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

# --- Fixed facts about the product (mirrored from the code that owns them) ---------
# launcher/native/native.go: AppPort / BifrostPort.
APP_PORT = 33000
GATEWAY_PORT = 38080
# launcher/native/native.go: the command-line fragments that identify our own children.
APP_MARKER = "smartbrain_3000.serve"
GATEWAY_MARKER = "bifrost-http"
# launcher/native/migrate.go: a database under this is a stub, not a vault.
MIN_DB_BYTES = 4096
# An assembly unpacks a Python runtime, a wheelhouse and a ~120 MB gateway binary. Two
# generations plus the download itself want room; below this an update will fail midway.
LOW_DISK_BYTES = 5 * 1024 * 1024 * 1024
CRITICAL_DISK_BYTES = 1024 * 1024 * 1024
# Nothing rotates the native logs (they are opened O_APPEND and never touched again).
BIG_LOG_BYTES = 200 * 1024 * 1024
# How much of a log to read back. Generous on purpose: the launcher health-probes every 30
# seconds and uvicorn logs every one of them, so a few hundred kilobytes is only an hour or
# two — not enough to still contain the morning's failure.
LOG_TAIL_BYTES = 2 * 1024 * 1024
# What uvicorn prints when the app starts. Everything before the last one is a previous run.
LOG_BOOT_MARKER = "Started server process"
# app/smartbrain_3000/gateway.py: the local providers and the default embedding tag.
LOCAL_PROVIDERS = ("ollama", "mlx", "mlxe")
EMBED_MODEL_TAG = "nomic-embed-text:v1.5"
# launcher/update: a self-update stages the replacement here before swapping it in.
APPLICATIONS_DIR = Path("/Applications")
# app/smartbrain_3000/gateway.py: the privacy flags the gateway data dir must carry.
GATEWAY_PRIVACY_CONFIG = (
    '{"logs_store":{"enabled":false},'
    '"client":{"enable_logging":false,"disable_content_logging":true}}\n'
)

_HTTP_TIMEOUT = 3.0
_CMD_TIMEOUT = 10


# --- Findings and repairs ----------------------------------------------------------

OK, NOTE, WARN, FAIL = "ok", "note", "warn", "fail"
_LABEL = {OK: "ok  ", NOTE: "note", WARN: "warn", FAIL: "FAIL"}
_COLOR = {OK: "32", NOTE: "36", WARN: "33", FAIL: "31"}


@dataclass
class Fix:
    """A repair the doctor can perform: what it is called, what it will do, and the act.

    ``explain`` is shown IN FULL before the confirmation prompt — a person must be able to
    decide without trusting the label. ``run`` returns the sentence to print on success and
    raises on failure; it must never touch the user's data directory.
    """

    label: str
    explain: str
    run: Callable[[], str]


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""
    advice: tuple[str, ...] = ()
    fix: Fix | None = None


@dataclass
class Section:
    name: str
    findings: list[Finding] = field(default_factory=list)


# --- Where things live -------------------------------------------------------------


def _launcher_dir(system: str, home: Path, env: dict[str, str]) -> Path:
    """The launcher's per-user folder — Go's os.UserConfigDir() + "SmartBrain".

    This is the tree the native install lives in: versions/, current, run/, bifrost-data/,
    the native-mode marker. It is NOT always where the database lives (see _data_dir).
    """
    if system == "Darwin":
        return home / "Library" / "Application Support" / "SmartBrain"
    if system == "Windows":
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "SmartBrain"
    xdg = env.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / "SmartBrain"


def _data_dir(system: str, home: Path, env: dict[str, str]) -> Path:
    """Where the app keeps its encrypted database (app/smartbrain_3000/runtime.py).

    On macOS and Windows this sits inside the launcher folder; on Linux it deliberately
    does not (config vs data), which is why the two are computed separately.
    """
    if system == "Darwin":
        return home / "Library" / "Application Support" / "SmartBrain" / "data"
    if system == "Windows":
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "SmartBrain" / "data"
    xdg = env.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "smartbrain" / "data"


def true_arch(system: str, reported: str) -> str:
    """The processor this COMPUTER has, not the one this interpreter was built for.

    On Apple Silicon an x86_64 Python (Homebrew's Intel build, or anything under Rosetta)
    reports "x86_64" for the machine — and believing it would tell the owner of a perfectly
    good Apple Silicon Mac that they need Docker. macOS answers the real question directly.
    """
    if system != "Darwin" or reported.lower() in ("arm64", "aarch64"):
        return reported
    code, out = run_cmd(["sysctl", "-n", "hw.optional.arm64"])
    return "arm64" if code == 0 and out.strip() == "1" else reported


def _native_supported(system: str, arch: str) -> bool:
    """True where the Docker-free build actually exists (launcher/native: currentPlatform).

    Intel Macs and ARM Linux have no pinned runtime, so those machines legitimately run
    Docker and are the only place this tool talks about it.
    """
    a = arch.lower()
    if system == "Darwin":
        return a in ("arm64", "aarch64")
    if system == "Linux":
        return a in ("x86_64", "amd64")
    if system == "Windows":
        return a in ("amd64", "x86_64")
    return False


def _port_from_url(url: str, fallback: int) -> int:
    """The port in a URL, or ``fallback`` when there is none to read."""
    try:
        port = urllib.parse.urlsplit(url).port
    except ValueError:
        return fallback
    return port or fallback


@dataclass
class Machine:
    """Everything the checks read about this computer. Built once; tests construct it directly."""

    system: str
    arch: str
    home: Path
    launcher_dir: Path
    data_dir: Path
    app_port: int
    gateway_port: int
    native_supported: bool
    env: dict[str, str]

    @classmethod
    def detect(cls, env: dict[str, str] | None = None) -> Machine:
        env = dict(os.environ if env is None else env)
        system = platform.system()
        arch = true_arch(system, platform.machine())
        home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
        return cls(
            system=system,
            arch=arch,
            home=home,
            launcher_dir=_launcher_dir(system, home, env),
            data_dir=_data_dir(system, home, env),
            app_port=int(env.get("SMARTBRAIN_PORT") or APP_PORT),
            # The app resolves its gateway from this variable too, so an install that has
            # been pointed somewhere else is checked where it actually looks.
            gateway_port=_port_from_url(env.get("SMARTBRAIN_LLM_GATEWAY_URL", ""), GATEWAY_PORT),
            native_supported=_native_supported(system, arch),
            env=env,
        )

    # The native tree. Names match launcher/native/native.go exactly.
    @property
    def native_dir(self) -> Path:
        return self.launcher_dir / "native"

    @property
    def versions_dir(self) -> Path:
        return self.native_dir / "versions"

    @property
    def current_file(self) -> Path:
        return self.native_dir / "current"

    @property
    def run_dir(self) -> Path:
        return self.native_dir / "run"

    @property
    def bifrost_data(self) -> Path:
        return self.native_dir / "bifrost-data"

    @property
    def native_marker(self) -> Path:
        return self.launcher_dir / "native-mode"

    @property
    def db_path(self) -> Path:
        override = self.env.get("SMARTBRAIN_DB_PATH")
        return Path(override) if override else self.data_dir / "smartbrain.duckdb"

    @property
    def app_log(self) -> Path:
        return self.run_dir / "app.log"

    @property
    def gateway_log(self) -> Path:
        return self.run_dir / "bifrost.log"

    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.app_port}"

    def gateway_url(self) -> str:
        return f"http://127.0.0.1:{self.gateway_port}"


# --- The outside world (one place, so tests can replace it) ------------------------


def http_json(url: str, timeout: float = _HTTP_TIMEOUT, headers: dict[str, str] | None = None,
              method: str = "GET") -> tuple[int, object] | None:
    """GET/POST a loopback URL; return (status, parsed-json-or-raw-text), or None if nothing answered.

    HTTPS is tried once as a fallback: a from-source install can be running behind the
    LAN/TLS overlay, and reporting "not running" at such a machine would be simply wrong.
    """
    assert url.startswith("http"), "loopback url required"
    for candidate in (url, url.replace("http://", "https://", 1)):
        req = urllib.request.Request(candidate, headers=headers or {}, method=method)
        ctx = ssl._create_unverified_context() if candidate.startswith("https") else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 - fixed loopback URL
                raw = resp.read(1 << 20)
                status = resp.status
        except urllib.error.HTTPError as exc:  # answered, just not with 200 — that IS an answer
            return exc.code, exc.read(1 << 16).decode("utf-8", "replace")
        except Exception:
            continue
        try:
            return status, json.loads(raw.decode("utf-8"))
        except Exception:
            return status, raw.decode("utf-8", "replace")
    return None


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    """Run a short command; return (exit code, combined output). 127 when it is not installed."""
    assert cmd, "command must be non-empty"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_CMD_TIMEOUT, check=False)
    except FileNotFoundError:
        return 127, ""
    except (subprocess.TimeoutExpired, OSError):
        return 124, ""
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def pid_alive(pid: int) -> bool:
    """Does a process with this pid exist? Advisory only — pids are recycled."""
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # someone else's process, but alive
    except OSError:
        return False
    except AttributeError:  # pragma: no cover - os.kill exists everywhere we run
        return False
    return True


def pid_command(pid: int, system: str) -> str:
    """A pid's command line, or "" when it cannot be determined.

    Unknown is not the same as wrong: like the launcher, every caller treats an
    unverifiable pid as "cannot say", never as "not ours".
    """
    if pid <= 1:
        return ""
    if system == "Windows":
        code, out = run_cmd(["powershell", "-NoProfile", "-Command",
                             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"])
        return out if code == 0 else ""
    code, out = run_cmd(["ps", "-p", str(pid), "-o", "command="])
    return out if code == 0 else ""


def matching_pids(marker: str, system: str) -> list[int]:
    """Every live pid whose command line contains ``marker`` ([] when we cannot look)."""
    if system == "Windows":
        return []
    code, out = run_cmd(["pgrep", "-f", marker])
    if code != 0:
        return []
    return [int(p) for p in out.split() if p.isdigit()]


def port_listeners(port: int, system: str) -> list[tuple[str, int]]:
    """Who is listening on ``port``: a list of (command, pid). Empty when nothing, or unknown."""
    if system == "Windows":
        code, out = run_cmd(["netstat", "-ano", "-p", "TCP"])
        if code != 0:
            return []
        found = []
        for line in out.splitlines():
            parts = line.split()
            if (len(parts) >= 5 and parts[-1].isdigit() and parts[1].endswith(f":{port}")
                    and parts[-2].upper() == "LISTENING"):
                pid = int(parts[-1])
                found.append((pid_command(pid, system).strip()[:60] or f"pid {pid}", pid))
        return found
    # +c 0: without it lsof truncates COMMAND to nine characters, so Docker Desktop's
    # listener reads as "com.docke" and every attempt to recognise it fails.
    code, out = run_cmd(["lsof", "-nP", "+c", "0", f"-iTCP:{port}", "-sTCP:LISTEN"])
    if code != 0 or not out:
        return []
    listeners = []
    for line in out.splitlines()[1:]:  # skip the header
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            listeners.append((parts[0], int(parts[1])))
    return listeners


# --- One snapshot of the machine, taken before any check runs ----------------------


@dataclass
class PidRecord:
    """What ``run/<name>.pid`` claims, and what is actually true of it."""

    name: str
    path: Path
    raw: str = ""
    pid: int = 0
    alive: bool = False
    command: str = ""
    verified: bool = False   # the live process really is ours
    unverifiable: bool = False  # we could not read its command line at all


@dataclass
class Snapshot:
    m: Machine
    current: str = ""                       # the version `current` names, "" when none
    current_complete: bool = False          # ...and it is a finished assembly
    current_missing_parts: tuple[str, ...] = ()
    complete_versions: tuple[str, ...] = ()  # every finished assembly, oldest first
    partial_dirs: tuple[Path, ...] = ()
    app: PidRecord | None = None
    gateway: PidRecord | None = None
    app_health: dict | None = None          # parsed /api/health, None when nothing answered
    app_port_answered: bool = False         # something answered, ours or not
    app_port_body: str = ""
    account: dict | None = None
    gateway_answered: bool = False
    providers: list[dict] | None = None
    log_tail: str = ""

    @property
    def app_ok(self) -> bool:
        return bool(self.app_health and self.app_health.get("status") == "ok")

    @property
    def running_version(self) -> str:
        return str((self.app_health or {}).get("version") or "")


def _read_pid(m: Machine, name: str, marker: str) -> PidRecord:
    rec = PidRecord(name=name, path=m.run_dir / f"{name}.pid")
    try:
        rec.raw = rec.path.read_text(encoding="utf-8").strip()
    except OSError:
        return rec
    if not rec.raw.isdigit():
        return rec
    rec.pid = int(rec.raw)
    rec.alive = pid_alive(rec.pid)
    if not rec.alive:
        return rec
    rec.command = pid_command(rec.pid, m.system)
    rec.unverifiable = not rec.command
    rec.verified = marker in rec.command
    return rec


def _version_parts(m: Machine, version: str) -> tuple[bool, tuple[str, ...]]:
    """Is this assembly finished, and if not, which pieces are missing?"""
    vdir = m.versions_dir / version
    python = "python/python.exe" if m.system == "Windows" else "python/bin/python3"
    gateway = "bifrost-http.exe" if m.system == "Windows" else "bifrost-http"
    missing = [name for name in (".complete", python, gateway) if not (vdir / name).exists()]
    if not any((vdir / p).is_dir() for p in _listdir(vdir) if p.startswith("smartbrain-wheelhouse")):
        missing.append("the app's wheelhouse")
    return not missing, tuple(missing)


def _listdir(path: Path) -> list[str]:
    try:
        return sorted(p.name for p in path.iterdir())
    except OSError:
        return []


def _version_key(version: str) -> tuple:
    """Sort key that orders 0.8.9 before 0.8.10 (string order gets this backwards)."""
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", version)[:4])


def _read_log_tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    """The end of a log, trimmed to the CURRENT run of the app.

    Everything before the last start belongs to a problem that has already been restarted
    past. Reporting it would mean warning about "address already in use" for as long as the
    line stayed in the file — which is how a diagnostic becomes noise people stop reading.
    Uvicorn announces each start, so there is an exact place to cut.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            text = fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""
    start = text.rfind(LOG_BOOT_MARKER)
    return text[start:] if start >= 0 else text


def gather(m: Machine) -> Snapshot:
    """Take one honest reading of the machine. Nothing here writes, prompts, or blocks for long."""
    snap = Snapshot(m=m)

    try:
        snap.current = m.current_file.read_text(encoding="utf-8").strip()
    except OSError:
        snap.current = ""
    versions = [name for name in _listdir(m.versions_dir) if not name.startswith(".")]
    complete = [v for v in versions if _version_parts(m, v)[0]]
    snap.complete_versions = tuple(sorted(complete, key=_version_key))
    if snap.current:
        snap.current_complete, snap.current_missing_parts = _version_parts(m, snap.current)
    snap.partial_dirs = tuple(
        m.versions_dir / name for name in _listdir(m.versions_dir) if name.startswith(".tmp-")
    )

    snap.app = _read_pid(m, "app", APP_MARKER)
    snap.gateway = _read_pid(m, "bifrost", GATEWAY_MARKER)

    health = http_json(m.app_url() + "/api/health")
    if health is not None:
        snap.app_port_answered = True
        status, body = health
        if status == 200 and isinstance(body, dict):
            snap.app_health = body
        else:
            # Whatever it was, keep a scrap of it: it is the only evidence of WHO answered.
            snap.app_port_body = json.dumps(body)[:200] if isinstance(body, dict) else str(body)[:200]
    if snap.app_ok:
        account = http_json(m.app_url() + "/api/account/status")
        if account and isinstance(account[1], dict):
            snap.account = account[1]

    providers = http_json(m.gateway_url() + "/api/providers")
    if providers is not None:
        snap.gateway_answered = True
        body = providers[1]
        if isinstance(body, dict) and isinstance(body.get("providers"), list):
            # Keep only entries shaped the way we read them. The gateway is another program's
            # API; a diagnostic that crashes on an unexpected reply is worse than useless.
            snap.providers = [p for p in body["providers"] if isinstance(p, dict)]

    snap.log_tail = _read_log_tail(m.app_log)
    return snap


# --- Checks ------------------------------------------------------------------------
# Each returns findings for one section. They read the snapshot; they never probe again,
# so the whole report describes a single moment rather than a moving target.


def check_install(m: Machine, s: Snapshot) -> list[Finding]:
    out: list[Finding] = []
    where = f"{m.system} {m.arch}"
    if not m.native_supported:
        # The ONE place Docker belongs on the happy path: this machine has no native build.
        out.append(Finding(
            NOTE, f"{where}: SmartBrain runs in Docker here",
            "There is no Docker-free build for this processor, so the launcher uses Docker on purpose.",
            ("Check that Docker Desktop (or Colima) is running, then use Restart in the SmartBrain menu.",),
        ))
    else:
        out.append(Finding(OK, f"{where}: the Docker-free build runs here", f"Install folder: {m.launcher_dir}"))

    if not m.launcher_dir.exists():
        out.append(Finding(
            FAIL, "No SmartBrain install found",
            f"Nothing exists at {m.launcher_dir}.",
            ("If you have never started SmartBrain on this computer, open it once — it sets itself up.",
             "If you expected an install here, you may be running as a different user."),
        ))
        return out

    if not m.native_supported:
        return out  # the Docker path owns everything below; do not invent native complaints

    out.extend(_check_selected_version(m, s))
    out.extend(_check_partial_downloads(m, s))
    out.extend(_check_native_marker(m, s))
    return out


def _check_selected_version(m: Machine, s: Snapshot) -> list[Finding]:
    """Does `current` name a version that is really, completely there?"""
    if not s.current:
        detail = ("No version has finished assembling yet."
                  if not s.complete_versions
                  else f"Assembled versions are present ({', '.join(s.complete_versions)}) but none is selected.")
        return [Finding(
            FAIL, "No version is selected to run", detail,
            ("Open SmartBrain from the menu bar / system tray. It downloads and selects a version by itself.",
             "A first assembly is a few hundred megabytes and takes a few minutes."),
            fix=_fix_point_current(m, s),
        )]
    if s.current_complete:
        return [Finding(OK, f"Version {s.current} is assembled and complete")]
    missing_entirely = not (m.versions_dir / s.current).exists()
    return [Finding(
        FAIL, f"The selected version ({s.current}) is not a complete install",
        "There is no folder for it — it was removed, or a download never finished."
        if missing_entirely else
        "Missing: " + ", ".join(s.current_missing_parts) + ". A download was interrupted.",
        ("SmartBrain cannot start from a half-finished install.",),
        fix=_fix_point_current(m, s),
    )]


def _check_partial_downloads(m: Machine, s: Snapshot) -> list[Finding]:
    if not s.partial_dirs:
        return []
    return [Finding(
        WARN, "Leftover pieces from an interrupted download",
        ", ".join(p.name for p in s.partial_dirs),
        fix=Fix(
            "Delete the interrupted downloads",
            "Removes only the .tmp-* scratch folders under\n"
            f"  {m.versions_dir}\n"
            "They are the working area of a download that was killed part way. Nothing runs from\n"
            "them and nothing reads them; a future update simply starts a fresh one.",
            lambda: _delete_all(s.partial_dirs),
        ),
    )]


def _check_native_marker(m: Machine, s: Snapshot) -> list[Finding]:
    if m.native_marker.exists():
        return [Finding(OK, "Docker-free mode is remembered across restarts")]
    if not s.complete_versions:
        return []  # nothing native has ever run here, so there is nothing to remember
    return [Finding(
        WARN, "The Docker-free marker is missing",
        f"{m.native_marker} does not exist, but a native install does.",
        ("Without it, a plain relaunch of the launcher can try to start the Docker version instead,",
         "which then collides with the running app and blames the network."),
        fix=Fix(
            "Write the Docker-free marker",
            f"Creates the one-byte file\n  {m.native_marker}\n"
            "It is exactly what the launcher writes itself after a successful native start, and\n"
            "deleting it again is how you would deliberately go back to Docker.",
            lambda: _write_marker(m),
        ),
    )]


def check_processes(m: Machine, s: Snapshot) -> list[Finding]:
    out: list[Finding] = []
    for rec, marker, human in ((s.app, APP_MARKER, "app"), (s.gateway, GATEWAY_MARKER, "gateway")):
        assert rec is not None, "snapshot must carry both pid records"
        out.append(_pid_finding(m, rec, human))
        # A process search asks a different question from the pid file, and the two disagree
        # in two distinct ways: a genuine double start, and a survivor nothing has a record of
        # (which is what makes a later Stop quietly stop nothing).
        found = matching_pids(marker, m.system)
        unrecorded = [p for p in found if not (rec.verified and p == rec.pid)]
        if not unrecorded:
            continue
        if rec.verified:
            out.append(Finding(
                FAIL, f"More than one {human} process is running",
                f"pids {rec.pid} and {', '.join(str(p) for p in unrecorded)}.",
                ("Two copies fight over the same port and the same database, and stopping one leaves the other.",
                 "Use Stop in the SmartBrain menu, wait a moment, then Restart."),
            ))
        else:
            article = "An" if human[0] in "aeiou" else "A"
            out.append(Finding(
                WARN, f"{article} {human} process is running that SmartBrain has no record of",
                f"pid{'s' if len(unrecorded) > 1 else ''} {', '.join(str(p) for p in unrecorded)}.",
                ("Stop in the menu will not stop it, because the record it reads does not name it.",
                 "Use Stop, then Restart — starting refuses while a survivor holds the port, which is",
                 "the signal to close it by hand."),
            ))

    if m.system == "Darwin":
        launcher_running = bool(matching_pids("SmartBrain.app/Contents/MacOS", "Darwin"))
        if s.app_ok and not launcher_running:
            out.append(Finding(
                NOTE, "SmartBrain is running without its menu-bar launcher",
                "The app keeps running when you quit the launcher — that is by design.",
                ("Nothing will notice a crash or find updates until the launcher is open again.",),
                fix=Fix(
                    "Open the SmartBrain menu-bar launcher",
                    "Runs: open -a SmartBrain\n"
                    "The launcher adopts the SmartBrain that is already running — it does not start a\n"
                    "second copy and does not interrupt what you are doing.",
                    _open_launcher,
                ),
            ))
    return out


def _pid_finding(m: Machine, rec: PidRecord, human: str) -> Finding:
    if not rec.raw:
        return Finding(NOTE, f"The {human} is not recorded as running",
                       f"There is no {rec.path.name}. That is normal when SmartBrain is stopped.")
    if not rec.pid:
        return Finding(WARN, f"The {human}'s record is unreadable", f"{rec.path} contains {rec.raw!r}.",
                       fix=_fix_drop_pid(rec))
    if not rec.alive:
        return Finding(WARN, f"The {human}'s record points at a process that is gone",
                       f"{rec.path.name} names pid {rec.pid}, which no longer exists.",
                       ("Left behind by a crash or a reboot. Harmless, but it makes Stop look like it did nothing.",),
                       fix=_fix_drop_pid(rec))
    if rec.unverifiable:
        return Finding(NOTE, f"The {human} is recorded as pid {rec.pid} (identity could not be checked)")
    if not rec.verified:
        return Finding(FAIL, f"The {human}'s record names somebody else's process",
                       f"pid {rec.pid} is running, but it is not SmartBrain: {rec.command.strip()[:90]}",
                       ("The pid was reused after a crash. SmartBrain would refuse to start, or Stop would",
                        "kill the wrong thing — so the safe move is to drop the record, not to act on it."),
                       fix=_fix_drop_pid(rec))
    return Finding(OK, f"The {human} is running (pid {rec.pid})")


def check_ports(m: Machine, s: Snapshot) -> list[Finding]:
    out: list[Finding] = []
    app_holders = port_listeners(m.app_port, m.system)
    gateway_holders = port_listeners(m.gateway_port, m.system)

    if s.app_ok:
        out.append(Finding(OK, f"The app is answering on {m.app_url()}",
                           f"It reports version {s.running_version}."))
    elif s.app_port_answered:
        out.append(Finding(
            FAIL, f"Something else is answering on port {m.app_port}",
            _holder_detail(app_holders) or f"It replied, but not as SmartBrain: {s.app_port_body[:80]}",
            ("SmartBrain cannot start while another program holds this port.",
             "Close that program, then use Restart in the SmartBrain menu."),
            fix=_fix_docker_leftovers(app_holders + gateway_holders),
        ))
    elif app_holders:
        out.append(Finding(
            FAIL, f"Port {m.app_port} is held by something that is not answering",
            _holder_detail(app_holders),
            ("A wedged process on this port stops SmartBrain from starting.",),
            fix=_fix_docker_leftovers(app_holders),
        ))
    elif s.app and s.app.verified:
        out.append(Finding(
            FAIL, "SmartBrain is running but not answering",
            f"pid {s.app.pid} is alive, yet {m.app_url()}/api/health is silent.",
            ("It may still be starting — give it a minute on a first run.",
             f"If it stays silent, read the end of {m.app_log}.",
             "Then use Stop and Restart in the SmartBrain menu."),
        ))
    else:
        out.append(Finding(
            NOTE, "SmartBrain is not running",
            f"Nothing is listening on {m.app_url()}.",
            ("Open SmartBrain from the menu bar / system tray to start it.",),
        ))

    if s.gateway_answered and s.providers is not None:
        out.append(Finding(OK, f"The model gateway is answering on {m.gateway_url()}"))
    elif s.gateway_answered:
        out.append(Finding(
            FAIL, f"Something else is answering on port {m.gateway_port}",
            _holder_detail(gateway_holders) or "The reply was not the SmartBrain gateway.",
            ("The gateway is how every model request leaves the app.",),
            fix=_fix_docker_leftovers(gateway_holders),
        ))
    elif s.app_ok:
        # The launcher's supervisor only watches the APP port, so a dead gateway is invisible
        # to it: the menu keeps saying Running while every single chat fails.
        out.append(Finding(
            FAIL, "The app is running but its model gateway is not",
            f"Nothing answers {m.gateway_url()}. Every chat, every search and every model call goes through it.",
            ("Nothing else notices this on its own.",
             "Use Stop in the SmartBrain menu, wait for it to finish, then Restart.",
             f"The gateway's own log is {m.gateway_log}."),
        ))
    else:
        out.append(Finding(NOTE, "The model gateway is not running", "Expected while SmartBrain is stopped."))
    return out


def check_app_state(m: Machine, s: Snapshot) -> list[Finding]:
    return _check_running_version(m, s) + _check_vault(m, s) + _check_database(m, s)


def _check_running_version(m: Machine, s: Snapshot) -> list[Finding]:
    """Is the app running the version that is selected — or is an update sitting on disk?"""
    if not (s.app_ok and s.current and s.running_version):
        return []
    if s.running_version == s.current:
        return [Finding(OK, f"Running the version that is selected ({s.current})")]
    if _version_key(s.current) > _version_key(s.running_version):
        return [Finding(
            NOTE, f"Version {s.current} is downloaded and waiting",
            f"You are running {s.running_version}. The update installs the next time SmartBrain starts.",
            ("Nothing is wrong. It applies on the next start, or from the menu now.",),
            fix=Fix(
                f"Install version {s.current} now",
                "Asks the desktop launcher to restart SmartBrain onto the version already downloaded.\n"
                "Nothing is killed: this is the same request the app's own update button makes.\n"
                "SmartBrain stops and starts again (a few seconds), and — as after any restart — you\n"
                "will need to unlock it with your passphrase. Your data is not touched, and the\n"
                "previous version stays on disk.",
                lambda: _request_install(m),
            ),
        )]
    return [Finding(
        WARN, "The running version is newer than the selected one",
        f"Running {s.running_version}; `current` says {s.current}.",
        ("Usually means the app was started by hand. A Stop + Restart from the menu settles it.",),
    )]


def _check_vault(m: Machine, s: Snapshot) -> list[Finding]:
    """Locked is the normal state after every start — and the commonest "it is broken"."""
    if s.account is None:
        return []
    if not s.account.get("initialized"):
        return [Finding(NOTE, "First-run setup has not been completed",
                        f"Open {m.app_url()} and choose a passphrase.")]
    if not s.account.get("unlocked"):
        return [Finding(
            NOTE, "SmartBrain is locked",
            "This is the normal state after every start, and it looks a lot like being broken.",
            (f"Unlock it at {m.app_url()}.",
             "Your model providers are registered at unlock, so chat and search stay unavailable until then."),
        )]
    out = [Finding(OK, "The vault is unlocked")]
    if not s.account.get("has_recovery"):
        out.append(Finding(WARN, "No Emergency Kit has been saved",
                           "Without it, a forgotten passphrase means the data cannot be read again.",
                           (f"Create one at {m.app_url()}/settings/account.",)))
    return out


def _check_database(m: Machine, s: Snapshot) -> list[Finding]:
    """Report the database and NEVER offer to touch it. Everything here is read-only, always."""
    db = m.db_path
    out: list[Finding] = []
    if not db.exists():
        out.append(Finding(WARN, "The database is not where it was expected", f"Looked for {db}.")
                   if s.app_ok else
                   Finding(NOTE, "No database yet", f"None at {db} — normal before the first run."))
    elif db.stat().st_size < MIN_DB_BYTES:
        out.append(Finding(
            FAIL, "The database file looks like a stub, not a vault",
            f"{db} is {db.stat().st_size} bytes.",
            ("This is what a copy that failed part way looks like.",
             "Do not delete it. Restore your most recent backup, or ask for help before touching it."),
        ))
    else:
        out.append(Finding(OK, "Your database is in place", f"{db} ({_human_bytes(db.stat().st_size)})"))
    if Path(str(db) + ".restore-pending").exists():
        out.append(Finding(NOTE, "A restore is staged and applies on the next start",
                           f"{db.name}.restore-pending is waiting beside your database."))
    return out


def check_models(m: Machine, s: Snapshot) -> list[Finding]:
    out: list[Finding] = []
    if s.providers is None:
        return out  # the gateway is down; check_ports has already said so

    names = [str(p.get("name") or "") for p in s.providers]
    unlocked = bool((s.account or {}).get("unlocked"))
    if not names and unlocked:
        out.append(Finding(
            FAIL, "The gateway has no model providers registered",
            "Every chat and every search will fail with no model available.",
            ("Lock and unlock SmartBrain — unlocking re-registers everything from your saved settings.",
             "If that does not help, re-save the provider under Settings."),
        ))
    elif not names:
        out.append(Finding(NOTE, "No model providers are registered yet",
                           "They are registered when you unlock SmartBrain."))
    else:
        out.append(Finding(OK, "Model providers registered", ", ".join(sorted(names))))

    for prov in s.providers:
        name = str(prov.get("name") or "")
        url = str(((prov.get("network_config") or {}).get("base_url")) or "")
        if not url:
            continue
        if "host.docker.internal" in url and m.native_supported:
            out.append(Finding(
                FAIL, f"'{name}' still points at a Docker-only address",
                f"{url} — that name only exists inside a container and cannot resolve here.",
                ("Open Settings -> Local models and save the server again; it is rewritten correctly on save.",),
            ))
            continue
        if name in LOCAL_PROVIDERS:
            status = _probe_model_server(name, url)
            if status is None:
                out.append(Finding(
                    FAIL, f"Local model server '{name}' is not answering", url,
                    ("Start the model server, or change the address under Settings -> Local models.",
                     "Until then, models from this provider are unavailable."),
                ))
                continue
            if status == 200:
                out.append(Finding(OK, f"Local model server '{name}' is answering", url))
            else:
                # An answer of any kind proves the server is up. A 401 in particular is this
                # tool being turned away without the key the gateway holds — not a fault.
                out.append(Finding(OK, f"Local model server '{name}' is answering",
                                   f"{url} (it replied {status} to an unauthenticated check)"))
            if name == "ollama":
                out.extend(_check_embedding_model(url))

    if "LOADING the model for this request" in s.log_tail:
        line = _last_line_containing(s.log_tail, "LOADING the model for this request")
        out.append(Finding(
            WARN, "A model server is reloading its model on every request",
            line[:220],
            ("This is a setting on the model server, not SmartBrain being slow — it throws the model",
             "out of memory between requests and pays the load cost every time.",
             "Look for a draft / speculative-decoding option pointed at an incompatible model, or an",
             "idle-unload (keep-alive) setting set to zero."),
        ))
    return out


def _check_embedding_model(url: str) -> list[Finding]:
    """Ollama is registered — is the default embedding model actually pulled?"""
    answer = http_json(url.rstrip("/") + "/api/tags")
    if not answer or not isinstance(answer[1], dict):
        return []
    models = answer[1].get("models")
    tags = [str(mod.get("name") or "") for mod in models if isinstance(mod, dict)] if isinstance(models, list) else []
    if any(tag == EMBED_MODEL_TAG for tag in tags):
        return [Finding(OK, f"The embedding model is installed ({EMBED_MODEL_TAG})")]
    return [Finding(
        WARN, f"The default embedding model is missing ({EMBED_MODEL_TAG})",
        "Knowledge search falls back to keyword matching without it.",
        ("The exact tag matters — plain 'nomic-embed-text' does not resolve.",),
        fix=Fix(
            f"Download {EMBED_MODEL_TAG} into Ollama",
            f"Runs: ollama pull {EMBED_MODEL_TAG}\n"
            "About 270 MB over the network. It adds a model to Ollama and changes nothing about\n"
            "SmartBrain; you can remove it again with: ollama rm " + EMBED_MODEL_TAG,
            _pull_embed_model,
        ),
    )]


def check_housekeeping(m: Machine, s: Snapshot) -> list[Finding]:
    return (_check_disk(m) + _check_old_versions(m, s) + _check_launcher_staging(m)
            + _check_gateway_privacy(m, s) + _check_log_sizes(m, s)
            + _check_log_for_known_trouble(m, s) + _check_stale_browser_cache(m, s))


def _check_disk(m: Machine) -> list[Finding]:
    try:
        free = shutil.disk_usage(m.launcher_dir if m.launcher_dir.exists() else m.home).free
    except OSError:
        return []  # an unreadable volume is not a finding we can stand behind
    if free < CRITICAL_DISK_BYTES:
        return [Finding(FAIL, "The disk is nearly full", f"{_human_bytes(free)} free.",
                        ("SmartBrain cannot download an update, and the database may fail to write.",))]
    if free < LOW_DISK_BYTES:
        return [Finding(WARN, "Not much room left for an update", f"{_human_bytes(free)} free.",
                        ("A version download needs a couple of gigabytes while it unpacks.",))]
    return [Finding(OK, "Enough disk space", f"{_human_bytes(free)} free")]


def _check_old_versions(m: Machine, s: Snapshot) -> list[Finding]:
    """Nothing in the product ever prunes these; one machine reached 4.1 GB of them."""
    prunable = _prunable_versions(s)
    if not prunable:
        return []
    total = sum(_dir_size(m.versions_dir / v) for v in prunable)
    return [Finding(
        WARN, f"{len(prunable)} old version{'s' if len(prunable) != 1 else ''} still on disk",
        f"{', '.join(prunable)} — about {_human_bytes(total)}.",
        ("Every update leaves its predecessor behind and nothing removes them.",),
        fix=Fix(
            "Remove the old versions",
            "Deletes these folders and nothing else:\n"
            + "\n".join(f"  {m.versions_dir / v}" for v in prunable)
            + f"\nThe version you run ({s.current}) and the one before it are kept, so you can still\n"
            "roll back. Each folder holds only downloaded parts; any of them can be fetched again.",
            lambda: _delete_all([m.versions_dir / v for v in prunable]),
        ),
    )]


def _check_launcher_staging(m: Machine) -> list[Finding]:
    """A self-update killed part way leaves its half-unpacked copy beside the app."""
    if m.system != "Darwin":
        return []
    staging = sorted(APPLICATIONS_DIR.glob(".smartbrain-update-*"))
    if not staging:
        return []
    return [Finding(
        WARN, "A launcher update was interrupted", ", ".join(p.name for p in staging),
        fix=Fix(
            "Remove the leftover launcher staging folders",
            "Deletes only these half-unpacked copies:\n"
            + "\n".join(f"  {p}" for p in staging)
            + "\nThe installed launcher and the /Applications/SmartBrain.app.previous rollback are\n"
            "left exactly as they are.",
            lambda: _delete_all(staging),
        ),
    )]


def _check_log_sizes(m: Machine, s: Snapshot) -> list[Finding]:
    """Nothing rotates these: they are opened append-only and never looked at again."""
    stopped = not ((s.app and s.app.verified) or (s.gateway and s.gateway.verified) or s.app_ok)
    out: list[Finding] = []
    for log in (m.app_log, m.gateway_log):  # fixed, bounded
        if not log.exists() or log.stat().st_size <= BIG_LOG_BYTES:
            continue
        fix = None
        if stopped:  # a running process holds the file open; renaming it under itself helps nobody
            fix = Fix(
                f"Set {log.name} aside and start a new one",
                f"Renames\n  {log}\nto {log.name}.old (replacing any earlier .old) so the file starts fresh.\n"
                "Nothing is deleted and the old contents remain readable. Only offered while SmartBrain\n"
                "is stopped, because the running process holds the file open.",
                lambda log=log: _rotate_log(log),
            )
        out.append(Finding(WARN, f"{log.name} has grown large", _human_bytes(log.stat().st_size),
                           () if stopped else ("It can be rotated once SmartBrain is stopped.",), fix=fix))
    return out


def _check_gateway_privacy(m: Machine, s: Snapshot) -> list[Finding]:
    """The gateway must not keep a plaintext log of every prompt and answer.

    The launcher rewrites this on every start, so a running install repairs itself with a
    restart; a stopped one can be repaired here.
    """
    if not m.bifrost_data.exists():
        return []
    config = m.bifrost_data / "config.json"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        text = ""
    disabled = '"logs_store"' in text and '"enabled":false' in text.replace(" ", "")
    leftovers = [m.bifrost_data / n for n in ("logs.db", "logs.db-wal", "logs.db-shm")
                 if (m.bifrost_data / n).exists()]
    if disabled and not leftovers:
        return [Finding(OK, "The gateway keeps no record of your prompts")]

    running = s.gateway_answered or bool(s.gateway and s.gateway.verified)
    detail = "The gateway's request log is not switched off at the source."
    if leftovers:
        detail += " A plaintext log file is present: " + ", ".join(p.name for p in leftovers) + "."
    advice = ("Restarting SmartBrain rewrites this by itself.",) if running else ()
    fix = None
    if not running:
        fix = Fix(
            "Switch the gateway's request log off",
            f"Writes\n  {config}\nwith the logging store disabled, and deletes any logs.db beside it.\n"
            "This is byte for byte what the launcher writes on every start. It contains no settings\n"
            "of yours; your database and your provider keys are untouched.",
            lambda: _write_gateway_privacy(m),
        )
    return [Finding(WARN, "The gateway's prompt logging is not disabled", detail, advice, fix=fix)]


# Strings the app and the runtime print when something specific has gone wrong. Each one
# was a real incident; matching on them turns "read 10 MB of log" into one sentence.
_LOG_TROUBLE = (
    ("address already in use", "Something else was holding SmartBrain's port when it tried to start."),
    ("no space left on device", "The disk filled up while SmartBrain was writing."),
    ("Conflicting lock is held", "Another SmartBrain process is holding the database open."),
    ("database is locked", "Another SmartBrain process is holding the database open."),
    ("Database is newer than this app", "This database was written by a newer version; let SmartBrain update first."),
    ("database missing after copy", "A copy of your data out of Docker did not finish."),
)


def _check_log_for_known_trouble(m: Machine, s: Snapshot) -> list[Finding]:
    out: list[Finding] = []
    seen: set[str] = set()
    for needle, meaning in _LOG_TROUBLE:
        if needle.lower() not in s.log_tail.lower() or meaning in seen:
            continue
        seen.add(meaning)
        out.append(Finding(
            WARN, "The log records a problem", meaning,
            (f"Most recent line: {_last_line_containing(s.log_tail, needle)[:180]}",
             f"Full log: {m.app_log}"),
        ))
    return out


def _check_stale_browser_cache(m: Machine, s: Snapshot) -> list[Finding]:
    """A browser holding an old copy of the app is the classic 'I fixed it and it is still broken'.

    Nothing outside the browser can read its cache, so this reports the two things that CAN
    be seen from here: whether the app is telling browsers not to cache the shell, and
    whether an old shell is asking for files this version no longer has.
    """
    if not s.app_ok:
        return []
    out: list[Finding] = []
    answer = http_json(m.app_url() + "/service-worker.js")
    if answer and answer[0] == 200:
        headers = _response_headers(m.app_url() + "/service-worker.js")
        if "no-cache" not in headers.get("cache-control", ""):
            out.append(Finding(
                WARN, "The app's service worker is being served as cacheable",
                "Browsers may keep serving an old copy of SmartBrain after an update.",
            ))
    stale_asset = re.search(r'GET (/_app/immutable/\S+) HTTP/[\d.]+" 404', s.log_tail)
    if stale_asset:
        out.append(Finding(
            WARN, "A browser is asking for files this version does not have",
            f"404 on {stale_asset.group(1)}",
            ("That is an old copy of the page held in a browser cache, not a broken install.",
             "In the tab showing SmartBrain, hold Shift and reload — or close the tab, quit the",
             "browser, and open it again.",
             "If a phone or an installed app shows it too, it needs the same reload."),
        ))
    return out


def _response_headers(url: str) -> dict[str, str]:
    """A loopback response's headers, with the same http-then-https attempt as http_json.

    Without the fallback an install behind the LAN/TLS overlay reads as "no headers", which
    the caller would report as a caching problem that does not exist.
    """
    for candidate in (url, url.replace("http://", "https://", 1)):
        ctx = ssl._create_unverified_context() if candidate.startswith("https") else None
        try:
            with urllib.request.urlopen(candidate, timeout=_HTTP_TIMEOUT, context=ctx) as resp:  # noqa: S310 - fixed loopback URL
                return {k.lower(): v for k, v in resp.headers.items()}
        except Exception:
            continue
    return {}


# --- Fixes -------------------------------------------------------------------------
# Every one of these is small, is printed in full before it runs, and stays away from the
# data directory. None of them stops a running app except the staged-update install, which
# says so.


def _fix_drop_pid(rec: PidRecord) -> Fix:
    return Fix(
        f"Forget the stale {rec.name} record",
        f"Deletes the single file\n  {rec.path}\n"
        "It holds one number: the process id SmartBrain last started. No process is signalled\n"
        "and nothing is stopped. SmartBrain writes it again the next time it starts.",
        lambda: _delete_all([rec.path]),
    )


def _fix_point_current(m: Machine, s: Snapshot) -> Fix | None:
    """Select the newest complete assembly when the pointer names a broken or missing one."""
    usable = [v for v in s.complete_versions if v != s.current]
    if not usable:
        return None
    newest = usable[-1]
    return Fix(
        f"Run version {newest} instead",
        f"Writes '{newest}' into\n  {m.current_file}\n"
        f"That version is already downloaded and complete. The broken folder is left on disk\n"
        "untouched, so nothing is lost, and the launcher will download a newer version again on\n"
        "its next check.",
        lambda: _write_current(m, newest),
    )


def _fix_docker_leftovers(holders: Iterable[tuple[str, int]]) -> Fix | None:
    """The single case where Docker may be named on a native machine: its container has our port.

    Only offered when the listener really does look like Docker AND a SmartBrain container
    really does exist. Otherwise this stays silent — a native install has no business
    talking about containers.
    """
    if not any("docker" in cmd.lower() or "vpnkit" in cmd.lower() for cmd, _ in holders):
        return None
    code, out = run_cmd(["docker", "ps", "-a", "--format", "{{.Names}}"])
    if code != 0:
        return None
    present = [n for n in ("smartbrain_3000", "smartbrain_bifrost") if n in out.split()]
    if not present:
        return None
    return Fix(
        "Remove the leftover SmartBrain containers",
        "Runs: docker rm -f " + " ".join(present) + "\n"
        "These are containers from the older Docker version of SmartBrain, still holding the\n"
        "port the app needs. Removing a container does NOT remove its data: the Docker volumes\n"
        "(smartbrain_smartbrain_data and smartbrain_bifrost_data) are left exactly as they are.",
        lambda: _remove_containers(present),
    )


def _delete_all(paths: Iterable[Path]) -> str:
    removed = 0
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RuntimeError(f"could not remove {path}: {exc}") from exc
        removed += 1
    return f"Removed {removed} item{'s' if removed != 1 else ''}."


def _write_marker(m: Machine) -> str:
    m.launcher_dir.mkdir(parents=True, exist_ok=True)
    m.native_marker.write_text("1\n", encoding="utf-8")
    return f"Wrote {m.native_marker}."


def _write_current(m: Machine, version: str) -> str:
    assert version, "version required"
    m.native_dir.mkdir(parents=True, exist_ok=True)
    tmp = m.current_file.with_suffix(".tmp")
    tmp.write_text(version + "\n", encoding="utf-8")
    tmp.replace(m.current_file)  # write-then-rename, so a crash cannot leave it torn
    return f"Selected version {version}. Start SmartBrain from the menu."


def _write_gateway_privacy(m: Machine) -> str:
    m.bifrost_data.mkdir(parents=True, exist_ok=True)
    for name in ("logs.db", "logs.db-wal", "logs.db-shm"):
        try:
            (m.bifrost_data / name).unlink()
        except OSError:
            pass
    (m.bifrost_data / "config.json").write_text(GATEWAY_PRIVACY_CONFIG, encoding="utf-8")
    return "Prompt logging is off and any stored log has been removed."


def _rotate_log(log: Path) -> str:
    target = log.with_suffix(log.suffix + ".old")
    log.replace(target)
    return f"Moved it to {target.name}."


def _remove_containers(names: list[str]) -> str:
    code, out = run_cmd(["docker", "rm", "-f", *names])
    if code != 0:
        raise RuntimeError(out or "docker refused to remove them")
    return "Removed " + ", ".join(names) + ". Now use Restart in the SmartBrain menu."


def _open_launcher() -> str:
    code, out = run_cmd(["open", "-a", "SmartBrain"])
    if code != 0:
        raise RuntimeError(out or "could not open SmartBrain")
    return "The launcher is opening; it will adopt the app that is already running."


def _pull_embed_model() -> str:
    if shutil.which("ollama") is None:
        raise RuntimeError("the ollama command is not installed on this computer")
    code = subprocess.run(["ollama", "pull", EMBED_MODEL_TAG], check=False, timeout=900).returncode
    if code != 0:
        raise RuntimeError("the download did not finish — is Ollama running?")
    return f"{EMBED_MODEL_TAG} is ready. Open Knowledge and choose Reindex."


def _request_install(m: Machine) -> str:
    answer = http_json(m.app_url() + "/api/update/install", headers={"x-sb-local": "1"}, method="POST")
    if not answer or answer[0] not in (200, 201):
        raise RuntimeError("SmartBrain did not accept the request — install it from the menu instead")
    return "Asked the launcher to install it. SmartBrain restarts within about half a minute."


# --- Small helpers -----------------------------------------------------------------


def _probe_model_server(name: str, url: str) -> int | None:
    """The status a local model server gives back, or None when nothing is there at all."""
    path = "/api/tags" if name == "ollama" else "/v1/models"
    answer = http_json(url.rstrip("/") + path, timeout=4.0)
    return answer[0] if answer else None


def _prunable_versions(s: Snapshot) -> list[str]:
    """Assemblies that are neither in use nor the one immediately before it."""
    if not s.current or not s.current_complete:
        return []  # never prune while the running version is in doubt
    keep = {s.current}
    others = [v for v in s.complete_versions if v != s.current]
    if others:
        keep.add(others[-1])  # the rollback
    return [v for v in s.complete_versions if v not in keep]


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover - unreachable, the loop returns


def _last_line_containing(text: str, needle: str) -> str:
    match = ""
    for line in text.splitlines():
        if needle.lower() in line.lower():
            match = line.strip()
    return match


def _holder_detail(holders: list[tuple[str, int]]) -> str:
    if not holders:
        return ""
    return "Held by " + ", ".join(f"{cmd} (pid {pid})" for cmd, pid in holders) + "."


# --- Output ------------------------------------------------------------------------


def _paint(code: str, text: str) -> str:
    if not sys.stdout.isatty() or platform.system() == "Windows":
        return text
    return f"\033[{code}m{text}\033[0m"


def render(sections: list[Section], verbose: bool) -> list[str]:
    """Turn the findings into the lines a worried person reads. Quiet by default."""
    lines: list[str] = []
    for section in sections:
        shown = [f for f in section.findings if verbose or f.level != OK]
        if not shown:
            continue
        lines.append("")
        lines.append(section.name)
        for finding in shown:
            lines.append(f"  {_paint(_COLOR[finding.level], _LABEL[finding.level])}  {finding.title}")
            if finding.detail:
                lines.append(f"        {finding.detail}")
            for note in finding.advice:
                lines.append(f"        {note}")
            if finding.fix is not None:
                lines.append(f"        Can be fixed: {finding.fix.label}")
    return lines


def summarise(sections: list[Section], fixable: int, can_fix: bool) -> tuple[list[str], int]:
    """The last paragraph, and the exit code. Failures fail; things worth tidying do not."""
    findings = [f for s in sections for f in s.findings]
    fails = [f for f in findings if f.level == FAIL]
    warns = [f for f in findings if f.level == WARN]
    lines = [""]
    if fails:
        subject = "1 problem needs" if len(fails) == 1 else f"{len(fails)} problems need"
        lines.append(_paint("31", f"{subject} attention."))
    elif warns:
        lines.append(_paint("32", "SmartBrain looks healthy.")
                     + f" {len(warns)} thing{'s' if len(warns) != 1 else ''} worth tidying up.")
    else:
        lines.append(_paint("32", "SmartBrain looks healthy."))
    if fixable and not can_fix:
        what = "One of these can be repaired" if fixable == 1 else f"{fixable} of these can be repaired"
        lines.append(f"{what} here — re-run with --fix to be offered each one.")
    return lines, (1 if fails else 0)


def headline(m: Machine, s: Snapshot) -> str:
    """One orienting line: what this tool looked at. A silent report should still say where it looked."""
    mode = "Docker-free install" if m.native_supported else "Docker install"
    if s.app_ok:
        state = f"version {s.running_version} running"
    elif s.current:
        state = f"version {s.current} installed, not running"
    else:
        state = "nothing installed yet"
    return f"{mode} · {state} · {m.launcher_dir}"


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def apply_fixes(sections: list[Section]) -> None:
    """Offer each repair in turn: what it does, in full, then a yes/no. Default is no."""
    offered = [f for s in sections for f in s.findings if f.fix is not None]
    if not offered:
        print("\nThere is nothing here that this tool can repair for you.")
        return
    print("\nRepairs")
    print("  Each one is described before it runs, and nothing happens without a yes.")
    print("  Your database is never touched.")
    for finding in offered:  # bounded by the number of findings
        fix = finding.fix
        assert fix is not None, "only findings with a fix reach here"
        print("")
        print(f"  {finding.title}")
        print(f"  -> {fix.label}")
        for line in fix.explain.splitlines():
            print(f"     {line}")
        if not _confirm("  Do this?"):
            print("     Skipped.")
            continue
        try:
            print("     " + fix.run())
        except Exception as exc:
            print(f"     Could not: {exc}")


def diagnose(m: Machine, snap: Snapshot | None = None) -> tuple[list[Section], Snapshot]:
    """Run every check against ONE snapshot, so the whole report describes a single moment."""
    snap = gather(m) if snap is None else snap
    plan = (
        ("Install", check_install),
        ("Processes", check_processes),
        ("Ports", check_ports),
        ("App", check_app_state),
        ("Models", check_models),
        ("Housekeeping", check_housekeeping),
    )
    sections = []
    for name, check in plan:  # fixed, bounded
        sections.append(Section(name, check(m, snap)))
    return sections, snap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doctor.py",
        description="Check a SmartBrain install and, with --fix, repair what is safe to repair.")
    parser.add_argument("--fix", action="store_true", help="offer to repair what can be repaired")
    parser.add_argument("-v", "--verbose", action="store_true", help="also list the checks that passed")
    args = parser.parse_args(argv)

    m = Machine.detect()
    print(_paint("1", "\nSmartBrain doctor"))
    sections, snap = diagnose(m)
    print(headline(m, snap))
    for line in render(sections, args.verbose):
        print(line)

    fixable = sum(1 for s in sections for f in s.findings if f.fix is not None)
    can_fix = args.fix and sys.stdin.isatty()
    lines, code = summarise(sections, fixable, can_fix)
    for line in lines:
        print(line)
    if args.fix and not sys.stdin.isatty():
        print("Repairs need a terminal to confirm them — run this by hand to be offered them.")
    elif can_fix:
        apply_fixes(sections)
        print("\nRun the doctor again to see where things stand.")
    return code


if __name__ == "__main__":
    sys.exit(main())
