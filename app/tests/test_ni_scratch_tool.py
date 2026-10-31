"""``tools/ni-scratch.py``: the throwaway app the pre-tag proof run observes (postmortem 2026-10-31, gate step 3).

The scratch app runs on the same computer as the user's install, so what matters most is what it does NOT share:
the database and key store, the model gateway (an app writes its provider settings there at start-up; a test run
once wiped a live gateway that way), remote access, Library votes. These start the real app from this checkout as
the tool does, and read what it was given.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-scratch.py"
pytestmark = pytest.mark.skipif(not TOOL.exists(), reason="the scratch tool ships in the repository only")


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("ni_scratch_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_the_scratch_app_inherits_no_smartbrain_setting_and_reaches_no_gateway(tool, tmp_path) -> None:
    install_like = {"PATH": "/usr/bin", "HOME": "/Users/t", "SMARTBRAIN_LLM_GATEWAY_URL": "http://127.0.0.1:38080",
                    "SMARTBRAIN_DB_PATH": "/Users/t/Library/Application Support/SmartBrain/data/smartbrain.duckdb",
                    "SMARTBRAIN_SIGNALING_URL": "wss://rtc.example", "SMARTBRAIN_LOCAL_TOKEN": "the-install-token" * 3,
                    "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "remote", "CLAUDE_CONFIG_DIR": "/tmp/session",
                    "ANTHROPIC_BASE_URL": "https://proxy.example"}
    env = tool.scratch_env(install_like, tmp_path, 40123, "s" * 43)
    assert env["SMARTBRAIN_LLM_GATEWAY_URL"] == "http://127.0.0.1:9", "a port nothing listens on"
    assert env["SMARTBRAIN_DB_PATH"] == str(tmp_path / "smartbrain.duckdb")
    assert env["SMARTBRAIN_SIGNALING_URL"] == "" and env["SMARTBRAIN_LIBRARY_API"] == ""
    assert env["SMARTBRAIN_LOCAL_TOKEN"] == "s" * 43 and env["SMARTBRAIN_PORT"] == "40123"
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/Users/t", "the CLI and the user's login stay reachable"
    assert not any(v == install_like.get(k) for k, v in env.items() if k.startswith("SMARTBRAIN_")), "none inherited"
    assert not any(k.startswith(("CLAUDE", "ANTHROPIC_")) for k in env), "the CLI runs on the user's own login"


def test_an_install_is_only_read_when_its_settings_are_copied(tool) -> None:
    seen = []

    def install(method, path, body):
        seen.append((method, path, body))
        if path == "/api/routes":
            return 200, {"routes": {"ni": "claudecode/opus", "chat": "claudecode/sonnet"}, "labels": {}}
        return 200, {"steps": [{"step": "build", "setting": "auto"}, {"step": "research", "setting": "cloud"}]}

    routes, steps = tool.copied(install)
    assert routes == {"ni": "claudecode/opus", "chat": "claudecode/sonnet"}
    assert steps == {"build": "auto", "research": "cloud"}
    assert [m for m, _, _ in seen] == ["GET", "GET"] and all(b is None for _, _, b in seen)


def test_the_real_app_starts_set_up_from_this_checkout_and_stops(tool, tmp_path, capfd) -> None:
    """End to end on this machine: the checkout's app starts on a free port with its own folder, is unlocked and
    routed, the command runs against it with its address and token, and the app is stopped after."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys, urllib.request\n"
        "base, token = os.environ['SMARTBRAIN_PROOF_BASE'], os.environ['SMARTBRAIN_LOCAL_TOKEN']\n"
        "req = urllib.request.Request(base + '/api/routes', headers={'Authorization': 'Bearer ' + token})\n"
        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
        "print(json.dumps(json.load(opener.open(req, timeout=10))['routes']))\n"
        "sys.exit(7)\n", encoding="utf-8")
    data = tmp_path / "scratch"
    code = tool.main(["--dir", str(data), "--route", "chat=mlx/test-local", "--step", "research=local", "--",
                      sys.executable, str(probe)])
    out = capfd.readouterr().out   # the command writes to the process's own stdout
    assert code == 7, "the command's own exit code"
    routes = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert routes["chat"] == "mlx/test-local"
    assert (data / "smartbrain.duckdb").exists() and (data / "server.log").exists(), "its own folder"
    assert "  research: " in out and "(local)" in out, out
    with pytest.raises(OSError):
        import urllib.request
        base = next(line.split(" at ")[1].split(",")[0] for line in out.splitlines() if " at http" in line)
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(base + "/api/health", timeout=3)
