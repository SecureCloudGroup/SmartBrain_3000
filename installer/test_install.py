"""Unit tests for installer gate logic (D1/D2/D3) — hermetic, no Docker, never touches a live stack.

install.py is stdlib-only. These tests point REPO_ROOT at a throwaway tree and mock _compose_cmd +
subprocess.run, so the gate DECISIONS are exercised in isolation and a real `compose up` is never
invoked. Run on the host (installer/ is not in the app image):
    <venv>/bin/python -m pytest installer/test_install.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_INSTALL_PY = Path(__file__).resolve().parent / "install.py"
_spec = importlib.util.spec_from_file_location("install_under_test", _INSTALL_PY)
assert _spec and _spec.loader, "could not load install.py"
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


class _Res:
    returncode = 0


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A throwaway repo tree with a compose/ dir; docker is never probed or run."""
    (tmp_path / "compose").mkdir()
    (tmp_path / "data" / "certs").mkdir(parents=True)
    monkeypatch.setattr(install, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(install, "_compose_cmd", lambda: ["docker", "compose"])
    calls: list[list] = []
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: (calls.append(a[0]), _Res())[1])
    return tmp_path, calls


def _ran_up(calls: list[list]) -> bool:
    return any("up" in c for c in calls)


# --- D1: wireguard up is gated on a TLS cert (else it crash-loops the app) ---

def test_wireguard_up_refused_without_cert(repo):
    _, calls = repo  # no data/certs/cert.pem present
    assert install.cmd_wireguard("up") == 2  # early, non-zero exit
    assert not _ran_up(calls)  # the crash-looping LAN/TLS overlay was never started


def test_wireguard_up_proceeds_with_cert(repo):
    tmp, calls = repo
    (tmp / "data" / "certs" / "cert.pem").write_text("x")
    assert install.cmd_wireguard("up") == 0
    assert _ran_up(calls)


# --- D2: webrtc up/status read compose/.env (what compose loads), not just os.environ ---

def test_webrtc_up_refused_when_unconfigured(repo, monkeypatch):
    _, calls = repo
    monkeypatch.delenv("SMARTBRAIN_SIGNALING_URL", raising=False)
    assert install.cmd_webrtc("up") == 2
    assert not _ran_up(calls)


def test_webrtc_up_proceeds_from_compose_env(repo, monkeypatch):
    tmp, calls = repo
    monkeypatch.delenv("SMARTBRAIN_SIGNALING_URL", raising=False)  # NOT in the shell env
    (tmp / "compose" / ".env").write_text("SMARTBRAIN_SIGNALING_URL=wss://rtc.example\n")
    assert install.cmd_webrtc("up") == 0  # reads compose/.env, so it proceeds
    assert _ran_up(calls)


# --- D3: cmd_webrtc up persists shell-set vars into compose/.env (survives update/restart) ---

def test_persist_compose_env_upserts_without_duplicates(repo, monkeypatch):
    tmp, _ = repo
    env = tmp / "compose" / ".env"
    env.write_text("SMARTBRAIN_SIGNALING_URL=OLD\nACME_EMAIL=a@b.c\n")
    monkeypatch.setenv("SMARTBRAIN_SIGNALING_URL", "wss://new")
    monkeypatch.setenv("SMARTBRAIN_TURN_SECRET", "sekret")
    install._persist_compose_env(install._WEBRTC_ENV_KEYS)
    text = env.read_text()
    assert text.count("SMARTBRAIN_SIGNALING_URL=") == 1  # replaced in place, not duplicated
    assert "SMARTBRAIN_SIGNALING_URL=wss://new" in text
    assert "SMARTBRAIN_TURN_SECRET=sekret" in text
    assert "ACME_EMAIL=a@b.c" in text  # unrelated lines preserved
    assert (env.stat().st_mode & 0o777) == 0o600  # may hold a token -> mode 600


def test_persist_compose_env_noop_when_nothing_set(repo, monkeypatch):
    tmp, _ = repo
    for k in install._WEBRTC_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    install._persist_compose_env(install._WEBRTC_ENV_KEYS)
    assert not (tmp / "compose" / ".env").exists()  # nothing to write -> no file created


# --- D5: a failed `update` rebuild restarts the previous version, else says the app is down ---

@pytest.fixture()
def repo_update(tmp_path, monkeypatch):
    """cmd_update with the slow/irreversible bits mocked: no real backup, git pull, or Docker."""
    monkeypatch.setattr(install, "REPO_ROOT", tmp_path)  # no .git -> the git-pull branch is skipped
    monkeypatch.setattr(install, "_compose_cmd", lambda: ["docker", "compose"])
    # The from-source commands now refuse to run on top of a live Docker-free install. The
    # developer machine running these tests may well have one, so answer for the gate here.
    monkeypatch.setattr(install, "_native_install_running", lambda: False)
    monkeypatch.setattr(install, "_backup_db", lambda: True)  # pretend the pre-update backup succeeded
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    calls: list[list] = []
    return calls


def test_update_rebuild_failure_restarts_previous_version(repo_update, monkeypatch):
    calls = repo_update
    rcs = iter([1, 0])  # `up --build` fails, then the fallback `up -d` succeeds
    monkeypatch.setattr(install, "_compose", lambda args: (calls.append(args), next(rcs))[1])
    assert install.cmd_update() == 1
    assert ["up", "-d", "--build"] in calls  # the rebuild was attempted
    assert ["up", "-d"] in calls             # ...then the previous image was brought back up (not left down)


def test_update_rebuild_and_fallback_both_fail_reports_stopped(repo_update, monkeypatch, capsys):
    calls = repo_update
    monkeypatch.setattr(install, "_compose", lambda args: (calls.append(args), 1)[1])  # everything fails
    assert install.cmd_update() == 1
    assert ["up", "-d"] in calls  # the fallback was still attempted
    assert "STOPPED" in capsys.readouterr().out  # user is told the app is down + how to recover


# =====================================================================================
# doctor.py — the diagnose-and-fix tool for the Docker-free install.
#
# Every test below builds a fake app-data tree in tmp_path and replaces the four ways the
# doctor touches the outside world (HTTP, processes, ports, subprocesses). Nothing here
# reads or writes a real install, and no test starts, stops or signals anything.
# =====================================================================================

_DOCTOR_PY = Path(__file__).resolve().parent / "doctor.py"
_dspec = importlib.util.spec_from_file_location("doctor_under_test", _DOCTOR_PY)
assert _dspec and _dspec.loader, "could not load doctor.py"
doctor = importlib.util.module_from_spec(_dspec)
# Registered BEFORE exec: @dataclass resolves annotations through sys.modules, and a module
# that is not there yet fails to build its fields.
sys.modules[_dspec.name] = doctor
_dspec.loader.exec_module(doctor)

APP_HEALTH = "http://127.0.0.1:33000/api/health"
ACCOUNT = "http://127.0.0.1:33000/api/account/status"
SERVICE_WORKER = "http://127.0.0.1:33000/service-worker.js"
PROVIDERS = "http://127.0.0.1:38080/api/providers"


class World:
    """The outside world, as each test chooses to describe it."""

    def __init__(self):
        self.http: dict[str, tuple[int, object]] = {}
        self.alive: dict[int, str] = {}          # pid -> its command line
        self.matches: dict[str, list[int]] = {}  # marker -> pids found by a process search
        self.listeners: dict[int, list] = {}     # port -> [(command, pid)]
        self.commands: list[list[str]] = []      # every subprocess the doctor tried to run
        self.command_result: tuple[int, str] = (127, "")


@pytest.fixture()
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(doctor, "http_json",
                        lambda url, timeout=3.0, headers=None, method="GET": w.http.get(url))
    monkeypatch.setattr(doctor, "pid_alive", lambda pid, system: pid in w.alive)
    monkeypatch.setattr(doctor, "pid_command", lambda pid, system: w.alive.get(pid, ""))
    monkeypatch.setattr(doctor, "matching_pids", lambda marker, system: w.matches.get(marker, []))
    monkeypatch.setattr(doctor, "port_listeners", lambda port, system: w.listeners.get(port, []))
    monkeypatch.setattr(doctor, "run_cmd",
                        lambda cmd: (w.commands.append(cmd), w.command_result)[1])
    monkeypatch.setattr(doctor, "_response_headers", lambda url: {"cache-control": "no-cache"})
    monkeypatch.setattr(doctor, "APPLICATIONS_DIR", Path("/nonexistent-for-tests"))
    return w


def _machine(tmp_path, system="Darwin", arch="arm64"):
    launcher = tmp_path / "SmartBrain"
    return doctor.Machine(
        system=system, arch=arch, home=tmp_path,
        launcher_dir=launcher, data_dir=launcher / "data",
        app_port=33000, gateway_port=38080,
        native_supported=doctor._native_supported(system, arch), env={},
    )


def _assemble(m, version: str, *, complete: bool = True, select: bool = False) -> Path:
    """Create a version directory shaped exactly like a real assembly."""
    vdir = m.versions_dir / version
    (vdir / "python" / "bin").mkdir(parents=True)
    (vdir / "python" / "bin" / "python3").write_text("#!/bin/sh\n")
    (vdir / f"smartbrain-wheelhouse-{version}-macos-arm64").mkdir()
    (vdir / "bifrost-http").write_text("binary")
    if complete:
        (vdir / ".complete").write_text(version + "\n")
    if select:
        m.native_dir.mkdir(parents=True, exist_ok=True)
        m.current_file.write_text(version + "\n")
    return vdir


def _healthy(tmp_path, world, version="0.8.13"):
    """A complete, running, unlocked install — the machine the doctor must call healthy."""
    m = _machine(tmp_path)
    _assemble(m, version, select=True)
    m.native_marker.write_text("1\n")
    m.run_dir.mkdir(parents=True)
    (m.run_dir / "app.pid").write_text("100\n")
    (m.run_dir / "bifrost.pid").write_text("200\n")
    m.data_dir.mkdir(parents=True, exist_ok=True)
    (m.data_dir / "smartbrain.duckdb").write_bytes(b"x" * 50_000)
    # Command lines as the launcher spawns them: both children run out of
    # <versions_dir>/<v>/..., which is how check_processes scopes to this install.
    vdir = m.versions_dir / version
    world.alive = {
        100: f"{vdir}/python/bin/python3 -m smartbrain_3000.serve",
        200: f"{vdir}/bifrost-http -app-dir {m.bifrost_data} -host 127.0.0.1 -port 38080",
    }
    world.matches = {doctor.APP_MARKER: [100], doctor.GATEWAY_MARKER: [200],
                     "SmartBrain.app/Contents/MacOS": [3]}
    world.http = {
        APP_HEALTH: (200, {"status": "ok", "version": version}),
        ACCOUNT: (200, {"initialized": True, "unlocked": True, "has_recovery": True}),
        PROVIDERS: (200, {"providers": [
            {"name": "mlx", "network_config": {"base_url": "http://127.0.0.1:8888"}}]}),
        "http://127.0.0.1:8888/v1/models": (200, {"data": [{"id": "qwen"}]}),
        SERVICE_WORKER: (200, "self.addEventListener"),
    }
    return m


def _levels(sections, level):
    return [f for s in sections for f in s.findings if f.level == level]


def _titles(sections):
    return [f.title for s in sections for f in s.findings]


def _text(sections) -> str:
    parts = []
    for s in sections:
        for f in s.findings:
            parts += [f.title, f.detail, *f.advice]
            if f.fix is not None:
                parts += [f.fix.label, f.fix.explain]
    return "\n".join(parts)


# --- N1: a healthy native install reports healthy, and never mentions Docker ---------

def test_healthy_install_has_no_failures(tmp_path, world):
    m = _healthy(tmp_path, world)
    sections, _ = doctor.diagnose(m)
    assert _levels(sections, doctor.FAIL) == [], _titles(sections)
    assert doctor.summarise(sections, 0, False)[1] == 0


def test_healthy_install_never_says_docker(tmp_path, world):
    """The rule: a native machine with no container in the way is never told about Docker.

    Naming the mode ("the Docker-free build") is the one allowed use, so the assertion is
    that every mention is that phrase — not that the letters never appear.
    """
    m = _healthy(tmp_path, world)
    sections, _ = doctor.diagnose(m)
    stripped = _text(sections).lower().replace("docker-free", "")
    assert "docker" not in stripped


def test_unsupported_platform_is_the_one_place_docker_belongs(tmp_path, world):
    m = _machine(tmp_path, system="Darwin", arch="x86_64")
    m.launcher_dir.mkdir(parents=True)
    sections, _ = doctor.diagnose(m)
    assert "Docker" in _text(sections)
    assert not _levels(sections, doctor.FAIL)  # an Intel Mac on Docker is not broken


def test_diagnose_changes_nothing_on_disk(tmp_path, world):
    """Read-only by default: a plain run must not create, delete or rewrite anything."""
    m = _healthy(tmp_path, world)
    before = {p: p.stat().st_mtime_ns for p in sorted(tmp_path.rglob("*"))}
    doctor.diagnose(m)
    after = {p: p.stat().st_mtime_ns for p in sorted(tmp_path.rglob("*"))}
    assert before == after
    assert world.commands == []  # and it started no subprocess of its own


# --- N2: the install itself ----------------------------------------------------------

def test_incomplete_assembly_fails_and_offers_a_complete_one(tmp_path, world):
    m = _healthy(tmp_path, world)
    _assemble(m, "0.8.12")                       # an older, complete version is still here
    broken = _assemble(m, "0.9.0", complete=False, select=True)
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "not a complete install" in f.title)
    assert finding.fix is not None
    finding.fix.run()
    assert m.current_file.read_text().strip() == "0.8.13"  # the newest COMPLETE one
    assert broken.exists()  # the broken folder is left alone, not deleted


def test_no_assembly_at_all_is_a_failure(tmp_path, world):
    m = _machine(tmp_path)
    m.launcher_dir.mkdir(parents=True)
    sections, _ = doctor.diagnose(m)
    assert any("No version is selected" in f.title for f in _levels(sections, doctor.FAIL))


def test_missing_install_directory_is_reported_once(tmp_path, world):
    m = _machine(tmp_path)
    sections, _ = doctor.diagnose(m)
    assert [f.title for f in _levels(sections, doctor.FAIL)] == ["No SmartBrain install found"]


def test_partial_download_is_offered_for_removal(tmp_path, world):
    m = _healthy(tmp_path, world)
    scratch = m.versions_dir / ".tmp-0.9.0"
    scratch.mkdir()
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "interrupted download" in f.title)
    finding.fix.run()
    assert not scratch.exists()
    assert (m.versions_dir / "0.8.13").exists()  # only the scratch folder went


def test_missing_native_marker_is_offered_and_written(tmp_path, world):
    m = _healthy(tmp_path, world)
    m.native_marker.unlink()
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "marker is missing" in f.title)
    finding.fix.run()
    assert m.native_marker.exists()


# --- N3: processes — verify, and never act on an unverified pid ----------------------

def test_dead_pid_record_is_dropped(tmp_path, world):
    m = _healthy(tmp_path, world)
    (m.run_dir / "app.pid").write_text("999\n")  # not in world.alive
    world.matches[doctor.APP_MARKER] = []
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "process that is gone" in f.title)
    finding.fix.run()
    assert not (m.run_dir / "app.pid").exists()


def test_recycled_pid_is_a_failure_and_nothing_is_killed(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.alive[100] = "/usr/bin/some-unrelated-program"
    world.matches[doctor.APP_MARKER] = []
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "somebody else's process" in f.title)
    finding.fix.run()
    assert not (m.run_dir / "app.pid").exists()
    assert world.commands == []  # the fix removes a record; it signals no process


def test_two_app_processes_is_a_failure(tmp_path, world):
    m = _healthy(tmp_path, world)
    vdir = m.versions_dir / "0.8.13"
    world.alive[101] = f"{vdir}/python/bin/python3 -m smartbrain_3000.serve"
    world.matches[doctor.APP_MARKER] = [100, 101]
    sections, _ = doctor.diagnose(m)
    assert any("More than one app process" in f.title for f in _levels(sections, doctor.FAIL))


def test_an_unrecorded_survivor_is_reported(tmp_path, world):
    """The pid records were poisoned twice in one day; a survivor nobody records is the tell."""
    m = _healthy(tmp_path, world)
    (m.run_dir / "app.pid").unlink()          # no record at all...
    world.matches[doctor.APP_MARKER] = [100]  # ...but the app is very much running
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "no record of" in f.title)
    assert "100" in finding.detail
    assert "will not stop it" in " ".join(finding.advice)


def test_marker_match_outside_this_install_is_ignored(tmp_path, world):
    """The bug from the field: a marker hit belonging to ANOTHER install is not our survivor.

    Multi-user machines, or a from-source dev stack alongside a native install, cause pgrep
    to return SmartBrain processes that are not this install's. Reporting them as
    unrecorded survivors of this install (and advising Stop + Restart on them) is what
    put this scoping in.
    """
    m = _healthy(tmp_path, world)
    (m.run_dir / "app.pid").unlink()          # this install has NO app recorded...
    world.alive[4177] = (                     # ...and the foreign process runs from
        "/Users/other/Library/Application Support/SmartBrain/native/versions/0.8.13/"
        "python/bin/python3 -m smartbrain_3000.serve"
    )
    world.matches[doctor.APP_MARKER] = [4177]
    sections, _ = doctor.diagnose(m)
    assert not any("no record of" in f.title for f in _levels(sections, doctor.WARN))
    assert not any("More than one" in f.title for f in _levels(sections, doctor.FAIL))


def test_survivor_from_older_version_of_this_install_is_still_reported(tmp_path, world):
    """The whole point of the check: a survivor from an earlier version of THIS install.

    After an update flips ``current``, an older-version process still counts as a survivor
    of this install — its command line still contains this install's versions dir. This
    fixture also plants a foreign SmartBrain pid so the assertion catches both directions:
    scope too tight (miss the older-version survivor) OR scope too loose (name the foreign).
    """
    m = _healthy(tmp_path, world, version="0.8.13")
    _assemble(m, "0.8.12")                                 # an older assembly of this install
    (m.run_dir / "app.pid").unlink()                       # no pid record for the survivor
    old_vdir = m.versions_dir / "0.8.12"
    world.alive[555] = f"{old_vdir}/python/bin/python3 -m smartbrain_3000.serve"
    world.alive[4177] = (                                  # a foreign install's process
        "/Users/other/Library/Application Support/SmartBrain/native/versions/0.8.13/"
        "python/bin/python3 -m smartbrain_3000.serve"
    )
    world.matches[doctor.APP_MARKER] = [555, 4177]
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "no record of" in f.title)
    assert "555" in finding.detail                          # ours is named
    assert "4177" not in finding.detail                     # the foreign one is not


def test_unreadable_command_line_is_treated_as_not_ours(tmp_path, world, monkeypatch):
    """Permission-denied on another user's process reads as foreign, never as our survivor."""
    m = _healthy(tmp_path, world)
    (m.run_dir / "app.pid").unlink()
    world.matches[doctor.APP_MARKER] = [8888]  # a pid pgrep saw...
    # ...but whose command line ps refuses to reveal (another user's, on a locked-down box).
    monkeypatch.setattr(doctor, "pid_command", lambda pid, system: "")
    sections, _ = doctor.diagnose(m)
    assert not any("no record of" in f.title for f in _levels(sections, doctor.WARN))


def test_stack_without_its_launcher_is_a_note_not_a_failure(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.matches["SmartBrain.app/Contents/MacOS"] = []
    sections, _ = doctor.diagnose(m)
    assert not _levels(sections, doctor.FAIL)
    assert any("without its menu-bar launcher" in f.title for f in _levels(sections, doctor.NOTE))


# --- N4: ports — is anything answering, and is it ours? ------------------------------

def test_stranger_on_the_app_port_is_a_failure(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http[APP_HEALTH] = (200, "<html>not us</html>")
    world.http.pop(ACCOUNT)
    world.listeners[33000] = [("some-server", 4242)]
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "Something else is answering" in f.title)
    assert "some-server" in finding.detail


def test_a_docker_container_holding_the_port_may_be_named_and_removed(tmp_path, world):
    """The single exception to the no-Docker rule — and only when a container really is there."""
    m = _healthy(tmp_path, world)
    world.http[APP_HEALTH] = (200, "<html>not us</html>")
    world.http.pop(ACCOUNT)
    world.listeners[33000] = [("com.docker.backend", 77)]
    world.command_result = (0, "smartbrain_3000\nsmartbrain_bifrost\n")
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "Something else is answering" in f.title)
    assert finding.fix is not None
    assert "docker rm -f smartbrain_3000 smartbrain_bifrost" in finding.fix.explain
    assert "volumes" in finding.fix.explain  # says out loud that the data survives


def test_no_docker_offer_when_no_container_exists(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http[APP_HEALTH] = (200, "<html>not us</html>")
    world.http.pop(ACCOUNT)
    world.listeners[33000] = [("com.docker.backend", 77)]
    world.command_result = (0, "unrelated_container\n")
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "Something else is answering" in f.title)
    assert finding.fix is None


def test_dead_gateway_under_a_live_app_is_caught(tmp_path, world):
    """The launcher's supervisor only watches the app port, so nothing else notices this."""
    m = _healthy(tmp_path, world)
    world.http.pop(PROVIDERS)
    sections, _ = doctor.diagnose(m)
    assert any("model gateway is not" in f.title for f in _levels(sections, doctor.FAIL))


def test_a_stopped_install_is_not_reported_as_broken(tmp_path, world):
    """It must work when the app is down — and being down is not, by itself, a fault."""
    m = _healthy(tmp_path, world)
    world.http.clear()
    world.alive.clear()
    world.matches = {"SmartBrain.app/Contents/MacOS": []}
    (m.run_dir / "app.pid").unlink()
    (m.run_dir / "bifrost.pid").unlink()
    sections, _ = doctor.diagnose(m)
    assert _levels(sections, doctor.FAIL) == [], _titles(sections)
    assert any("SmartBrain is not running" in f.title for f in _levels(sections, doctor.NOTE))


def test_alive_but_silent_app_is_a_failure(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http.clear()  # the process is up, the port says nothing
    sections, _ = doctor.diagnose(m)
    assert any("running but not answering" in f.title for f in _levels(sections, doctor.FAIL))


# --- N5: app state — updates, locks, and the database --------------------------------

def test_staged_update_is_a_note_with_an_install_offer(tmp_path, world):
    m = _healthy(tmp_path, world, version="0.8.12")
    _assemble(m, "0.8.13", select=True)
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.NOTE) if "waiting" in f.title)
    assert "0.8.13" in finding.title
    assert finding.fix is not None
    assert "unlock" in finding.fix.explain  # says what the restart will cost you


def test_locked_vault_is_explained_not_failed(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http[ACCOUNT] = (200, {"initialized": True, "unlocked": False, "has_recovery": True})
    sections, _ = doctor.diagnose(m)
    assert not _levels(sections, doctor.FAIL)
    assert any("SmartBrain is locked" in f.title for f in _levels(sections, doctor.NOTE))


def test_stub_database_is_flagged_and_never_offered_for_deletion(tmp_path, world):
    m = _healthy(tmp_path, world)
    (m.data_dir / "smartbrain.duckdb").write_bytes(b"tiny")
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "stub" in f.title)
    assert finding.fix is None
    assert "Do not delete it" in " ".join(finding.advice)


def test_no_fix_anywhere_touches_the_data_directory(tmp_path, world):
    """The one promise that must hold for every repair, present and future."""
    m = _healthy(tmp_path, world)
    m.native_marker.unlink()
    (m.versions_dir / ".tmp-0.9.0").mkdir()
    (m.run_dir / "bifrost.pid").write_text("999\n")
    for old in ("0.8.4", "0.8.6", "0.8.7"):
        _assemble(m, old)
    db = m.data_dir / "smartbrain.duckdb"
    before = db.read_bytes()
    sections, _ = doctor.diagnose(m)
    fixes = [f.fix for s in sections for f in s.findings if f.fix is not None]
    assert fixes, "this fixture is supposed to produce repairs"
    for fix in fixes:
        assert str(m.data_dir) not in fix.explain
        fix.run()
    assert db.read_bytes() == before
    assert sorted(p.name for p in m.data_dir.iterdir()) == ["smartbrain.duckdb"]


# --- N6: the gateway and the model servers -------------------------------------------

def test_unlocked_with_no_providers_is_a_failure(tmp_path, world):
    """"Every chat is dead" used to be invisible from outside — it is the gateway with nothing in it."""
    m = _healthy(tmp_path, world)
    world.http[PROVIDERS] = (200, {"providers": []})
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "no model providers" in f.title)
    assert "unlock" in " ".join(finding.advice).lower()


def test_locked_with_no_providers_is_only_a_note(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http[PROVIDERS] = (200, {"providers": []})
    world.http[ACCOUNT] = (200, {"initialized": True, "unlocked": False, "has_recovery": True})
    sections, _ = doctor.diagnose(m)
    assert not _levels(sections, doctor.FAIL)


def test_container_era_model_url_is_a_failure(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http[PROVIDERS] = (200, {"providers": [
        {"name": "ollama", "network_config": {"base_url": "http://host.docker.internal:11434"}}]})
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "Docker-only address" in f.title)
    assert "Settings" in " ".join(finding.advice)


def test_unreachable_local_model_server_is_a_failure(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http.pop("http://127.0.0.1:8888/v1/models")  # the registered server has gone away
    sections, _ = doctor.diagnose(m)
    assert any("is not answering" in f.title for f in _levels(sections, doctor.FAIL))


def test_model_server_that_wants_a_key_still_counts_as_answering(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http[PROVIDERS] = (200, {"providers": [
        {"name": "mlx", "network_config": {"base_url": "http://127.0.0.1:8888"}}]})
    world.http["http://127.0.0.1:8888/v1/models"] = (401, {"error": "API key required"})
    sections, _ = doctor.diagnose(m)
    assert not _levels(sections, doctor.FAIL)
    assert any("is answering" in f.title for f in _levels(sections, doctor.OK))


def test_missing_embedding_model_is_offered_for_download(tmp_path, world):
    m = _healthy(tmp_path, world)
    world.http[PROVIDERS] = (200, {"providers": [
        {"name": "ollama", "network_config": {"base_url": "http://127.0.0.1:11434"}}]})
    world.http["http://127.0.0.1:11434/api/tags"] = (200, {"models": [{"name": "qwen2.5:7b"}]})
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "embedding model is missing" in f.title)
    assert doctor.EMBED_MODEL_TAG in finding.fix.explain


def test_per_request_model_reload_is_surfaced(tmp_path, world):
    """The 4.5-seconds-a-turn misconfiguration that hid in the log for five days."""
    m = _healthy(tmp_path, world)
    m.app_log.write_text(
        "INFO boot\nWARNING mlx/qwen: the model server spent 4.5s LOADING the model for this request.\n")
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "reloading its model" in f.title)
    assert "4.5s" in finding.detail
    assert "idle-unload" in " ".join(finding.advice)


# --- N7: housekeeping -----------------------------------------------------------------

def test_old_versions_are_pruned_but_current_and_rollback_are_kept(tmp_path, world):
    m = _healthy(tmp_path, world)
    for old in ("0.8.4", "0.8.6", "0.8.9", "0.8.10", "0.8.12"):
        _assemble(m, old)
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "old version" in f.title)
    finding.fix.run()
    assert sorted(p.name for p in m.versions_dir.iterdir()) == ["0.8.12", "0.8.13"]


def test_nothing_is_pruned_while_the_running_version_is_in_doubt(tmp_path, world):
    m = _healthy(tmp_path, world)
    _assemble(m, "0.8.4")
    _assemble(m, "0.9.0", complete=False, select=True)
    sections, _ = doctor.diagnose(m)
    assert not any("old version" in f.title for f in _levels(sections, doctor.WARN))


def test_a_full_disk_is_a_failure(tmp_path, world, monkeypatch):
    m = _healthy(tmp_path, world)
    monkeypatch.setattr(doctor.shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 100 * 1024 * 1024})())
    sections, _ = doctor.diagnose(m)
    assert any("disk is nearly full" in f.title for f in _levels(sections, doctor.FAIL))


def test_gateway_prompt_logging_is_repaired_only_while_stopped(tmp_path, world):
    m = _healthy(tmp_path, world)
    m.bifrost_data.mkdir(parents=True)
    (m.bifrost_data / "logs.db").write_text("prompts and answers in the clear")
    running, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(running, doctor.WARN) if "prompt logging" in f.title)
    assert finding.fix is None  # the launcher rewrites it on the next start
    assert "Restarting" in " ".join(finding.advice)

    world.http.clear()  # now stop everything
    world.alive.clear()
    stopped, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(stopped, doctor.WARN) if "prompt logging" in f.title)
    finding.fix.run()
    assert not (m.bifrost_data / "logs.db").exists()
    assert '"enabled":false' in (m.bifrost_data / "config.json").read_text()


def test_known_trouble_in_the_log_is_translated_into_a_sentence(tmp_path, world):
    m = _healthy(tmp_path, world)
    m.app_log.write_text("OSError: [Errno 48] address already in use\n")
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "log records a problem" in f.title)
    assert "holding SmartBrain's port" in finding.detail


def test_trouble_from_before_the_last_restart_is_not_reported(tmp_path, world):
    """A line that has already been restarted past must not warn forever."""
    m = _healthy(tmp_path, world)
    m.app_log.write_text(
        "OSError: [Errno 48] address already in use\n"
        f"INFO:     {doctor.LOG_BOOT_MARKER} [100]\n"
        "INFO:     Application startup complete.\n")
    sections, _ = doctor.diagnose(m)
    assert not any("log records a problem" in f.title for f in _levels(sections, doctor.WARN))


def test_a_stale_browser_cache_is_named_as_such(tmp_path, world):
    m = _healthy(tmp_path, world)
    m.app_log.write_text('127.0.0.1 - "GET /_app/immutable/nodes/9.old.js HTTP/1.1" 404\n')
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "browser is asking" in f.title)
    assert "not a broken install" in " ".join(finding.advice)


# --- N8: small pieces that have to be right -------------------------------------------

def test_a_surprising_reply_from_the_gateway_does_not_crash_the_doctor(tmp_path, world):
    """The gateway is another program's API; a diagnostic that dies on it is useless."""
    m = _healthy(tmp_path, world)
    world.http[PROVIDERS] = (200, {"providers": ["not-a-mapping", None, 7]})
    sections, _ = doctor.diagnose(m)
    assert any("no model providers" in f.title for f in _levels(sections, doctor.FAIL))


def test_a_stranger_reply_is_quoted_back(tmp_path, world):
    """Whatever answered the port is the evidence; keep enough of it to name the culprit."""
    m = _healthy(tmp_path, world)
    world.http[APP_HEALTH] = (403, {"detail": "some other program"})
    world.http.pop(ACCOUNT)
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.FAIL) if "Something else is answering" in f.title)
    assert "some other program" in finding.detail


def test_the_install_guard_asks_over_plain_http(monkeypatch, tmp_path):
    """A from-source mkcert cert must not make the guard blind to a running native app."""
    monkeypatch.setattr(install, "_doctor", lambda: doctor)
    monkeypatch.setattr(install, "REPO_ROOT", tmp_path)
    (tmp_path / "data" / "certs").mkdir(parents=True)
    (tmp_path / "data" / "certs" / "cert.pem").write_text("x")  # _health_ok would go https here
    machine = _machine(tmp_path)
    machine.launcher_dir.mkdir(parents=True)
    machine.native_marker.write_text("1\n")
    monkeypatch.setattr(doctor.Machine, "detect", classmethod(lambda cls, env=None: machine))
    asked: list[str] = []
    monkeypatch.setattr(doctor, "http_json",
                        lambda url, **kw: (asked.append(url), (200, {"status": "ok"}))[1])
    assert install._native_install_running() is True
    assert asked == [f"http://127.0.0.1:{machine.app_port}/api/health"]


def test_version_order_is_numeric_not_alphabetic():
    assert sorted(["0.8.9", "0.8.10", "0.8.4"], key=doctor._version_key) == ["0.8.4", "0.8.9", "0.8.10"]


def test_apple_silicon_is_recognised_through_an_intel_python(monkeypatch):
    """An x86_64 interpreter on an M-series Mac must not be told it needs Docker."""
    monkeypatch.setattr(doctor, "run_cmd", lambda cmd: (0, "1"))
    assert doctor.true_arch("Darwin", "x86_64") == "arm64"
    monkeypatch.setattr(doctor, "run_cmd", lambda cmd: (0, "0"))
    assert doctor.true_arch("Darwin", "x86_64") == "x86_64"
    monkeypatch.setattr(doctor, "run_cmd", lambda cmd: (1, ""))
    assert doctor.true_arch("Linux", "x86_64") == "x86_64"


def test_data_directory_differs_from_the_launcher_directory_on_linux():
    """macOS and Windows nest them; Linux deliberately does not, and the doctor must know."""
    home = Path("/home/someone")
    assert doctor._launcher_dir("Linux", home, {}) == home / ".config" / "SmartBrain"
    assert doctor._data_dir("Linux", home, {}) == home / ".local" / "share" / "smartbrain" / "data"
    assert doctor._data_dir("Darwin", home, {}) == doctor._launcher_dir("Darwin", home, {}) / "data"


def test_failures_set_the_exit_code_and_warnings_do_not():
    warn_only = [doctor.Section("x", [doctor.Finding(doctor.WARN, "tidy me")])]
    assert doctor.summarise(warn_only, 0, False)[1] == 0
    assert "healthy" in doctor.summarise(warn_only, 0, False)[0][1]
    broken = [doctor.Section("x", [doctor.Finding(doctor.FAIL, "broken")])]
    assert doctor.summarise(broken, 0, False)[1] == 1


def test_report_hides_passing_checks_unless_asked():
    sections = [doctor.Section("x", [doctor.Finding(doctor.OK, "fine"), doctor.Finding(doctor.FAIL, "bad")])]
    quiet = "\n".join(doctor.render(sections, verbose=False))
    assert "bad" in quiet and "fine" not in quiet
    assert "fine" in "\n".join(doctor.render(sections, verbose=True))


# --- N9: the six defects the doctor used to have -------------------------------------


def test_pid_alive_never_calls_os_kill_on_windows(monkeypatch):
    """os.kill on Windows routes to TerminateProcess for anything but CTRL_C/CTRL_BREAK.

    A read-only diagnose that reaches pid_alive would kill the user's live SmartBrain.
    """
    def refuse(pid, sig):  # pragma: no cover - the whole point is that we never call it
        raise AssertionError(f"os.kill was called with ({pid}, {sig}) on Windows")
    monkeypatch.setattr(doctor.os, "kill", refuse)
    monkeypatch.setattr(doctor, "run_cmd",
                        lambda cmd: (0, "python.exe   1234 Console   1  100 K"))
    assert doctor.pid_alive(1234, "Windows") is True
    monkeypatch.setattr(doctor, "run_cmd",
                        lambda cmd: (0, "INFO: No tasks are running which match the specified criteria."))
    assert doctor.pid_alive(1234, "Windows") is False
    monkeypatch.setattr(doctor, "run_cmd", lambda cmd: (1, ""))
    assert doctor.pid_alive(1234, "Windows") is False


def test_matching_pids_distinguishes_unavailable_from_empty(monkeypatch):
    """None means "could not look"; [] means "looked and found nothing"."""
    monkeypatch.setattr(doctor, "run_cmd", lambda cmd: (127, ""))  # pgrep not installed
    assert doctor.matching_pids("smartbrain_3000.serve", "Linux") is None
    monkeypatch.setattr(doctor, "run_cmd", lambda cmd: (1, ""))    # pgrep ran, no match
    assert doctor.matching_pids("smartbrain_3000.serve", "Linux") == []
    monkeypatch.setattr(doctor, "run_cmd", lambda cmd: (0, "42\n7\n"))
    assert doctor.matching_pids("smartbrain_3000.serve", "Linux") == [42, 7]


def test_check_processes_notes_when_the_search_is_unavailable(tmp_path, world, monkeypatch):
    """A silent [] on Windows made the duplicate/survivor check invisible for a whole platform."""
    m = _healthy(tmp_path, world)
    monkeypatch.setattr(doctor, "matching_pids", lambda marker, system: None)
    sections, _ = doctor.diagnose(m)
    assert any("cross-check could not run" in f.title for f in _levels(sections, doctor.NOTE))
    assert not _levels(sections, doctor.FAIL)  # cannot look != healthy, but cannot look != broken


def test_prunable_never_returns_the_running_version(tmp_path, world):
    """A staged update flips ``current`` while the app still runs from the older folder.

    Deleting the running assembly rmtree's the interpreter under lazy imports. Uses
    four complete versions so a prune actually happens (three-version cases collapse
    to an empty prune list once running is protected, and cannot exercise the text).
    """
    m = _healthy(tmp_path, world, version="0.8.10")  # world.http reports 0.8.10 running
    _assemble(m, "0.8.5")                             # ancient; the one that must prune
    _assemble(m, "0.8.12")                            # newer than running; becomes rollback
    _assemble(m, "0.8.13", select=True)               # newest, current, NOT running
    snap = doctor.gather(m)
    assert snap.current == "0.8.13" and snap.running_version == "0.8.10"
    prunable = doctor._prunable_versions(snap)
    assert "0.8.10" not in prunable, f"would delete the running assembly: {prunable}"
    assert prunable == ["0.8.5"], prunable
    # And the confirmation text must name the running version, not just `current`.
    sections, _ = doctor.diagnose(m)
    finding = next(f for f in _levels(sections, doctor.WARN) if "old version" in f.title)
    assert "0.8.10" in finding.fix.explain


def test_docker_leftovers_fix_stays_silent_when_no_native_assembly_exists(tmp_path, world):
    """A Linux compose install is the install — never call it "leftovers"."""
    m = _machine(tmp_path, system="Linux", arch="x86_64")
    m.launcher_dir.mkdir(parents=True)  # exists, but versions_dir is empty
    world.command_result = (0, "smartbrain_3000\nsmartbrain_bifrost\n")
    snap = doctor.gather(m)
    assert snap.complete_versions == ()
    assert doctor._fix_docker_leftovers(snap, [("com.docker.backend", 77)]) is None


def test_headline_does_not_claim_a_running_version_when_no_install_here(tmp_path, world):
    """Multi-user machines: /api/health may belong to another user's install."""
    m = _machine(tmp_path)                        # launcher_dir does NOT exist
    world.http = {APP_HEALTH: (200, {"status": "ok", "version": "0.8.13"})}
    _, snap = doctor.diagnose(m)
    line = doctor.headline(m, snap)
    assert "0.8.13 running" not in line
    assert "no install here" in line.lower()
