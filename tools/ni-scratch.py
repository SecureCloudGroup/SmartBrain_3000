#!/usr/bin/env python3
"""A throwaway SmartBrain from this checkout, set up like an install, for one observed run.

Postmortem 2026-10-31, done gate step 3: before anything is tagged, the proof ask runs against the checkout on T's
Mac with T's routes, observed with ``tools/ni-proof.py``. This starts the checkout's app on a free loopback port
with its own data folder, local API token and passphrase, sharing nothing with the install on the same computer:

* its database, Library pack, key store and log live under ``--dir`` (``SMARTBRAIN_DB_PATH``);
* the model gateway address is a port nothing listens on: the app writes its provider settings to the gateway at
  start-up and unlock, and a test run once wiped a live gateway's providers that way (2026-09-24). Claude Code
  routes run through the local ``claude`` CLI and need no gateway; a route through the gateway fails honestly;
* remote access, the phone relay, Library votes, the voice download and the page browser are off; no setting is
  inherited from the environment (every ``SMARTBRAIN_*`` variable is dropped first);
* the ``claude`` CLI the app runs for a Claude Code route sees the environment an installed app gives it: the
  ``CLAUDE*`` and ``ANTHROPIC_*`` variables of a Claude Code session this is started from are dropped, so the CLI
  runs on the user's own login and configuration, not the calling session's;
* no ``SB_*`` variable reaches the app either: a key the proof run is given for a card's key pause
  (``SB_PROOF_KEY_*``) goes to the card's key route only, never into the app's environment or the CLI's.

It unlocks the scratch app with a passphrase it keeps nowhere, turns the Claude Code provider on when a route
needs it, sets the routes and the Neural Interface step settings (``--route`` / ``--step``, or copied read-only
from the install with ``--copy-from``), runs the command after ``--`` with ``SMARTBRAIN_PROOF_BASE`` and
``SMARTBRAIN_LOCAL_TOKEN`` set for it, then stops the app. The data folder stays for inspection unless ``--clean``.

Usage (from the checkout, with its venv's python):
    .venv/bin/python tools/ni-scratch.py --dir ~/Documents/claude_working_folder/sb-scratch \\
        --copy-from http://127.0.0.1:33000 -- \\
        .venv/bin/python tools/ni-proof.py --ask "NVDA stock price every 30 minutes" --consent allow \\
        --key www.alphavantage.co=SB_PROOF_KEY_ALPHAVANTAGE --yes --refresh --expect built --out <dir>

Exit: the command's own exit code; 2 when the scratch app could not be started or set up.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
DEAD_GATEWAY = "http://127.0.0.1:9"     # discard: a refused connection, as the test suite uses
HEALTH_WAIT_S = 90.0
STOP_WAIT_S = 20.0
DROPPED = ("SMARTBRAIN_", "SB_", "CLAUDE", "ANTHROPIC_")  # the install's; the proof run's keys; a calling session's
SCRATCH_ENV = {
    "SMARTBRAIN_SIGNALING_URL": "",          # remote access off: no routing id registered for a scratch app
    "SMARTBRAIN_WEBRTC_ENABLED": "0",
    "SMARTBRAIN_LIBRARY_API": "",            # never vote in the shared Library from a scratch app
    "SMARTBRAIN_NO_VOICE_PREFETCH": "1",
    "SMARTBRAIN_NO_BROWSER": "1",
    "SMARTBRAIN_LLM_GATEWAY_URL": DEAD_GATEWAY,
    "SMARTBRAIN_HOST": "127.0.0.1",
}


def _proof():
    """``tools/ni-proof.py`` as a module: its loopback-only transport and token reader."""
    spec = importlib.util.spec_from_file_location("ni_proof_for_scratch", REPO / "tools" / "ni-proof.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def scratch_env(base_env: dict, data_dir: pathlib.Path, port: int, token: str) -> dict:
    """The scratch app's environment: the caller's, minus every SmartBrain setting and the calling Claude Code
    session's variables, plus the scratch's own."""
    assert data_dir.is_absolute() and 1024 <= port <= 65535 and len(token) >= 32, "a folder, a port, a token"
    env = {k: v for k, v in base_env.items() if not k.startswith(DROPPED)}
    env.update(SCRATCH_ENV)
    env.update({"SMARTBRAIN_DB_PATH": str(data_dir / "smartbrain.duckdb"), "SMARTBRAIN_PORT": str(port),
                "SMARTBRAIN_LOCAL_TOKEN": token, "PYTHONPATH": str(REPO / "app")})
    return env


def start(env: dict, log_path: pathlib.Path) -> subprocess.Popen:
    """The checkout's app under uvicorn (``smartbrain_3000.serve``), its output to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        return subprocess.Popen([sys.executable, "-m", "smartbrain_3000.serve"], cwd=str(REPO / "app"), env=env,
                                stdout=log, stderr=subprocess.STDOUT)


def wait_healthy(api, proc: subprocess.Popen, timeout_s: float = HEALTH_WAIT_S) -> dict:
    deadline = time.monotonic() + timeout_s
    for _ in range(int(timeout_s * 4) + 1):  # bounded by the deadline
        if proc.poll() is not None:
            raise RuntimeError(f"the scratch app exited at start-up (code {proc.returncode}); see its log")
        try:
            status, body = api("GET", "/api/health", None)
        except RuntimeError:  # the transport's refusal: not listening yet
            status, body = 0, None
        if status == 200 and isinstance(body, dict):
            return body
        if time.monotonic() > deadline:
            break
        time.sleep(0.25)
    raise RuntimeError(f"the scratch app did not answer within {timeout_s:.0f} s")


def copied(install_api) -> tuple[dict, dict]:
    """The install's routes and NI step settings, read with two GETs (nothing on the install is changed)."""
    status, routes = install_api("GET", "/api/routes", None)
    if status != 200 or not isinstance(routes, dict):
        raise RuntimeError(f"reading the install's routes failed: HTTP {status}")
    status, steps = install_api("GET", "/api/ni/step-models", None)
    if status != 200 or not isinstance(steps, dict):
        raise RuntimeError(f"reading the install's step settings failed: HTTP {status}")
    return (dict(routes.get("routes") or {}),
            {s["step"]: s["setting"] for s in steps.get("steps") or [] if isinstance(s, dict) and s.get("step")})


def set_up(api, routes: dict, steps: dict) -> dict:
    """Unlock with a fresh passphrase (kept nowhere), Claude Code on when a route uses it, routes, step settings.
    Returns what the scratch app now says it runs each step on."""
    status, body = api("POST", "/api/account/setup", {"passphrase": secrets.token_urlsafe(24)})
    if status != 200:
        raise RuntimeError(f"unlocking the scratch app failed: HTTP {status} {body}")
    if any(str(m).startswith("claudecode/") for m in routes.values()):
        status, body = api("PUT", "/api/local-models/claudecode", {})
        if status != 200:
            raise RuntimeError(f"turning Claude Code on failed: HTTP {status} {body}")
    if routes:
        status, body = api("PUT", "/api/routes", {"routes": routes})
        if status != 200:
            raise RuntimeError(f"setting the routes failed: HTTP {status} {body}")
    if steps:
        status, body = api("PUT", "/api/ni/step-models", {"steps": steps})
        if status != 200:
            raise RuntimeError(f"setting the step models failed: HTTP {status} {body}")
    status, body = api("GET", "/api/ni/step-models", None)
    return body if status == 200 and isinstance(body, dict) else {}


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=STOP_WAIT_S)


def _pairs(values: list[str], what: str) -> dict:
    out = {}
    for pair in values:  # bounded: the arguments
        name, sep, value = pair.partition("=")
        if not sep or not name.strip() or not value.strip():
            raise SystemExit(f"ni-scratch: --{what} takes NAME=VALUE, got {pair!r}")
        out[name.strip()] = value.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="the scratch app's data folder (created; kept unless --clean)")
    ap.add_argument("--copy-from", help="an install's address to copy routes and step settings from (read only)")
    ap.add_argument("--install-token-file", help="that install's local API token (default: the launcher's)")
    ap.add_argument("--route", action="append", default=[], metavar="CAPABILITY=MODEL")
    ap.add_argument("--step", action="append", default=[], metavar="STEP=SETTING")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command to run against the scratch app")
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("ni-scratch: give the command to run after --")
    proof = _proof()
    data_dir = pathlib.Path(args.dir).expanduser().resolve()
    routes, steps = _pairs(args.route, "route"), _pairs(args.step, "step")
    try:
        if args.copy_from:
            install = proof.http_api(args.copy_from, proof.read_token(args.install_token_file, dict(os.environ)))
            got_routes, got_steps = copied(install)
            routes, steps = {**got_routes, **routes}, {**got_steps, **steps}
    except (RuntimeError, proof.ProofError) as exc:
        print(f"ni-scratch: {exc}", file=sys.stderr)
        return 2
    return _run(proof, data_dir, routes, steps, command, clean=args.clean)


def _run(proof, data_dir: pathlib.Path, routes: dict, steps: dict, command: list[str], *, clean: bool) -> int:
    port, token = free_port(), secrets.token_urlsafe(32)
    base = f"http://127.0.0.1:{port}"
    proc = start(scratch_env(dict(os.environ), data_dir, port, token), data_dir / "server.log")
    try:
        api = proof.http_api(base, token)
        health = wait_healthy(api, proc)
        now = set_up(api, routes, steps)
        print(f"scratch SmartBrain {health.get('version')} at {base}, data in {data_dir}")
        for step in now.get("steps") or []:  # bounded: the NI steps
            print(f"  {step.get('step')}: {step.get('model') or 'none'} ({step.get('setting')})")
        child_env = {**os.environ, "SMARTBRAIN_PROOF_BASE": base, "SMARTBRAIN_LOCAL_TOKEN": token}
        return subprocess.run(command, env=child_env, check=False).returncode
    except (RuntimeError, proof.ProofError) as exc:
        print(f"ni-scratch: {exc} (log: {data_dir / 'server.log'})", file=sys.stderr)
        return 2
    finally:
        stop(proc)
        if clean:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
