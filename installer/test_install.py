"""Unit tests for installer gate logic (D1/D2/D3) — hermetic, no Docker, never touches a live stack.

install.py is stdlib-only. These tests point REPO_ROOT at a throwaway tree and mock _compose_cmd +
subprocess.run, so the gate DECISIONS are exercised in isolation and a real `compose up` is never
invoked. Run on the host (installer/ is not in the app image):
    <venv>/bin/python -m pytest installer/test_install.py -q
"""

from __future__ import annotations

import importlib.util
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
