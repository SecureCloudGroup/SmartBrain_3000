#!/usr/bin/env python3
"""SmartBrain_3000 installer — prerequisite check, build, start, and verify.

Stdlib only, so it runs on macOS / Linux / Windows with just Python 3. It does
NOT collect secrets: provider API keys, your passphrase, and the Emergency Kit
are set up in the browser at http://localhost:33000 after the stack is running
(and local models in Settings -> Local models). Keeping secrets out of the
installer means they only ever exist inside the app's encrypted store.

Commands:
  install   prereq gate -> build image locally -> start stack -> verify  (default)
  doctor    re-check prerequisites + running stack; report green/red (exit 1 on fail)
  update    back up data -> pull latest -> rebuild + restart -> verify (prompts first)
  certs     generate a local CA + TLS cert (mkcert) for LAN/mobile HTTPS access
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "compose" / "docker-compose.yml"
LAN_FILE = REPO_ROOT / "compose" / "docker-compose.lan.yml"
WG_FILE = REPO_ROOT / "compose" / "docker-compose.wireguard.yml"
WEBRTC_FILE = REPO_ROOT / "compose" / "docker-compose.webrtc.yml"

_HEALTH_DEADLINE = 240  # max seconds to wait for the app to become healthy
_POLL_SECONDS = 3
_RUN_TIMEOUT = 30  # seconds for a probe subprocess (build/up use their own)
# Default embedding model — must match gateway.DEFAULT_EMBED_MODEL ("ollama/<this>").
# The exact tag matters: bare 'nomic-embed-text' 404s, ':v1.5' resolves.
EMBED_MODEL_TAG = "nomic-embed-text:v1.5"
_PULL_TIMEOUT = 600  # the embed model is ~270 MB; allow time on a slow link


def _tls_enabled() -> bool:
    """LAN/TLS overlay is active when an mkcert cert has been generated."""
    return (REPO_ROOT / "data" / "certs" / "cert.pem").exists()


def _remote_enabled() -> bool:
    """WebRTC overlay is active when a signaling broker is configured in compose/.env."""
    env_path = REPO_ROOT / "compose" / ".env"
    if not env_path.exists():
        return False
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("SMARTBRAIN_SIGNALING_URL=") and line.split("=", 1)[1].strip():
            return True
    return False


def _active_overlays() -> list[str]:
    """Compose overlays inferred from local setup, so install/update/restart preserve the
    operator's mode instead of silently dropping it: LAN/TLS (certs present) + WebRTC
    (signaling configured)."""
    files: list[str] = []
    if _tls_enabled():
        files += ["-f", str(LAN_FILE)]
    if _remote_enabled():
        files += ["-f", str(WEBRTC_FILE)]
    return files


def _app_url() -> str:
    """The URL the app serves on locally (https once TLS is set up, else http)."""
    return "https://localhost:33000" if _tls_enabled() else "http://localhost:33000"


def _supports_color() -> bool:
    return sys.stdout.isatty() and platform.system() != "Windows"


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _supports_color() else text


def ok(text: str) -> str:
    return _paint("32", "OK   ") + text


def fail(text: str) -> str:
    return _paint("31", "FAIL ") + text


def note(text: str) -> str:
    return _paint("36", "..   ") + text


def _probe(cmd: list[str], env: dict | None = None) -> tuple[int, str]:
    """Run a short command; return (exit_code, combined_output). 127 if missing."""
    assert cmd, "command must be non-empty"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_RUN_TIMEOUT, check=False, env=env)
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _compose_cmd() -> list[str] | None:
    """Return the Docker Compose invocation (v2 plugin preferred), or None."""
    result: list[str] | None = None
    if shutil.which("docker") and _probe(["docker", "compose", "version"])[0] == 0:
        result = ["docker", "compose"]
    elif shutil.which("docker-compose") and _probe(["docker-compose", "version"])[0] == 0:
        result = ["docker-compose"]
    assert result is None or len(result) >= 1, "compose command must be None or non-empty"
    assert result is None or all(isinstance(p, str) for p in result), "compose parts must be strings"
    return result


def check_prereqs() -> tuple[bool, list[str]]:
    """Run the prerequisite gate; return (all_passed, report_lines)."""
    assert COMPOSE_FILE.parent.name == "compose", "compose file must live under compose/"
    assert COMPOSE_FILE.name.endswith((".yml", ".yaml")), "compose file must be YAML"
    lines = [note(f"Platform: {platform.system()} {platform.machine()} / Python {platform.python_version()}")]
    all_ok = True

    has_docker = shutil.which("docker") is not None
    lines.append((ok if has_docker else fail)("Docker CLI found" if has_docker else
                  "Docker not found — install Docker Desktop (macOS/Windows) or Docker Engine / Colima (Linux/macOS)"))
    all_ok &= has_docker

    daemon = has_docker and _probe(["docker", "info"])[0] == 0
    lines.append((ok if daemon else fail)("Docker daemon is running" if daemon else
                  "Docker daemon not reachable — start Docker Desktop / Colima, then re-run"))
    all_ok &= daemon

    compose = _compose_cmd()
    lines.append((ok if compose else fail)(f"Docker Compose available ({' '.join(compose)})" if compose else
                  "Docker Compose v2 not found — update Docker, or install the compose plugin"))
    all_ok &= compose is not None

    has_compose_file = COMPOSE_FILE.exists()
    lines.append((ok if has_compose_file else fail)(
        f"Compose file present ({COMPOSE_FILE})" if has_compose_file else f"Missing {COMPOSE_FILE}"))
    all_ok &= has_compose_file
    return all_ok, lines


def _compose(args: list[str]) -> int:
    """Run a compose subcommand against our file, streaming output. Return exit code."""
    base = _compose_cmd()
    assert base is not None, "compose must be available (checked in prereqs)"
    cmd = [*base, "-f", str(COMPOSE_FILE), *_active_overlays(), *args]
    print(note(" ".join(cmd)))
    return subprocess.run(cmd, check=False).returncode


def _health_ok() -> bool:
    """True if the app health endpoint returns an ok status (https once TLS is set up)."""
    url = _app_url() + "/api/health"
    assert "localhost:33000/api/health" in url, "health URL must target the loopback health route"
    # Loopback + mkcert: skip cert verification for the local probe (the cert covers
    # localhost but the script's trust store may not include the mkcert CA).
    ctx = ssl._create_unverified_context() if url.startswith("https") else None
    try:
        with urllib.request.urlopen(url, timeout=2, context=ctx) as resp:  # noqa: S310 - fixed loopback URL
            return resp.status == 200 and b'"status":"ok"' in resp.read(256)
    except Exception:
        return False


def wait_healthy() -> bool:
    """Poll the health endpoint until ok or the deadline (bounded loop)."""
    assert _POLL_SECONDS > 0, "poll interval must be positive"
    assert _HEALTH_DEADLINE > 0, "health deadline must be positive"
    attempts = max(1, _HEALTH_DEADLINE // _POLL_SECONDS)
    for i in range(attempts):  # fixed upper bound (P10 #2)
        if _health_ok():
            return True
        print(note(f"waiting for the app to become healthy… ({(i + 1) * _POLL_SECONDS}s)"))
        time.sleep(_POLL_SECONDS)
    return _health_ok()


def _ensure_embed_model() -> None:
    """Pull the default embedding model so Knowledge semantic search works out of the box.

    Best-effort, and only when the Ollama CLI is on the host: the app embeds via
    'ollama/nomic-embed-text:v1.5' by default, and that exact tag must be present or
    semantic search silently degrades to keyword search. Pulling it here means the user
    never meets that gotcha. Skipped (with a note) when Ollama isn't installed.
    """
    assert ":" in EMBED_MODEL_TAG, "embed model tag must be pinned to a version"
    assert _PULL_TIMEOUT > 0, "pull timeout must be positive"
    if shutil.which("ollama") is None:
        print(note("Ollama not found here — skipping embedding-model setup."))
        print(f"      Knowledge semantic search needs it; once Ollama is installed run: ollama pull {EMBED_MODEL_TAG}")
        return
    print(note(f"Preparing semantic search (pulling {EMBED_MODEL_TAG}; first time downloads ~270 MB)…"))
    try:
        code = subprocess.run(["ollama", "pull", EMBED_MODEL_TAG], check=False, timeout=_PULL_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print(note("Still downloading — it'll finish in the background; open Knowledge and Reindex later."))
        return
    print(ok(f"Embedding model ready ({EMBED_MODEL_TAG}).") if code == 0 else
          note(f"Couldn't pull {EMBED_MODEL_TAG} now (is Ollama running?) — pull it later for semantic search."))


def cmd_install(open_browser: bool) -> int:
    """Prereq gate -> build + start the stack -> verify health -> next steps."""
    assert isinstance(open_browser, bool), "open_browser must be a bool"
    print(_paint("1", "\nSmartBrain_3000 installer\n"))
    passed, lines = check_prereqs()
    assert isinstance(passed, bool) and isinstance(lines, list), "prereq report shape"
    for line in lines:
        print(" ", line)
    if not passed:
        print(fail("\nPrerequisites not met — fix the items above and re-run."))
        return 1
    print(note("\nBuilding the image locally and starting the stack…"))
    print(note("First run downloads + builds components — usually a few minutes (longer on a slow link)."))
    if _compose(["up", "-d", "--build"]) != 0:
        print(fail("\nThe build or startup step failed."))
        print(note("Most often Docker isn't running, or it's low on disk space. Try:  install.py doctor"))
        return 1
    if not wait_healthy():
        print(fail(f"\nThe app started but isn't responding at {_app_url()} yet."))
        print(note("On a first run give it another minute, then run:  install.py doctor  (it can restart it for you)"))
        print(note("Or view logs:  " + " ".join([*_compose_cmd(), "-f", str(COMPOSE_FILE), "logs", "smartbrain"])))
        return 1
    print(ok(f"\nSmartBrain_3000 is running at {_app_url()}"))
    print(note("Next: open it in your browser and complete first-run setup —"))
    print("      set your passphrase (save the Emergency Kit!), then add provider")
    print("      API keys and local models under Settings.")
    print(note("Local models: install Ollama (any OS) or MLX (Apple Silicon) on the HOST;"))
    print("      the app reaches them via host.docker.internal and you wire them in Settings -> Local models.")
    if open_browser:
        try:
            webbrowser.open(_app_url())
        except Exception:
            pass
    _ensure_embed_model()  # pull the embed model while the user does first-run setup in the browser
    return 0


def _confirm(prompt: str) -> bool:
    """Ask a yes/no question (default No). Only call when stdin is a TTY."""
    assert prompt, "prompt required"
    assert sys.stdin.isatty(), "_confirm must only run interactively"
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def _try_start_docker() -> None:
    """Best-effort start of the Docker daemon (platform-specific; may be a no-op)."""
    system = platform.system()
    assert system, "platform must be identifiable"
    assert system in ("Darwin", "Linux", "Windows"), f"unsupported platform: {system}"
    if system == "Darwin":
        subprocess.run(["colima", "start"] if shutil.which("colima") else ["open", "-a", "Docker"], check=False)
    elif system == "Linux":
        subprocess.run(["systemctl", "start", "docker"], check=False)
    else:
        print(note("Start Docker Desktop from the Start menu, then re-run doctor."))


def _doctor_fix(healthy: bool) -> None:
    """Offer to fix the common failures: dead daemon, stopped stack, missing embed model."""
    assert isinstance(healthy, bool), "healthy must be a bool"
    daemon = shutil.which("docker") is not None and _probe(["docker", "info"])[0] == 0
    assert isinstance(daemon, bool), "daemon state must be a bool"
    if not daemon:
        if _confirm("Docker doesn't look like it's running — try to start it?"):
            _try_start_docker()
            print(note("Give Docker a moment to start, then re-run:  install.py doctor"))
        return  # nothing else can run without the daemon
    if _compose_cmd() is None:
        return  # can't act on the stack without compose
    if not healthy and _confirm("The app isn't responding — (re)start it now?"):
        _compose(["up", "-d"])
        if wait_healthy() and _confirm("Open it in your browser?"):
            try:
                webbrowser.open(_app_url())
            except Exception:
                pass
    if _confirm(f"Pull the embedding model ({EMBED_MODEL_TAG}) so semantic search works?"):
        _ensure_embed_model()


def cmd_doctor() -> int:
    """Re-check prerequisites + the running stack; report green/red, and (interactively) fix."""
    print(_paint("1", "\nSmartBrain_3000 doctor\n"))
    passed, lines = check_prereqs()
    for line in lines:
        print(" ", line)
    healthy = _health_ok()
    assert isinstance(passed, bool) and isinstance(healthy, bool), "check results must be bools"
    print(" ", (ok if healthy else fail)(f"App responding at {_app_url()}/api/health" if healthy else
              f"App not responding at {_app_url()}/api/health — is the stack up? ('install' or compose up -d)"))
    if passed and healthy:
        print("\n" + ok("All checks passed."))
        return 0
    print("\n" + fail("Some checks failed (see above)."))
    # Offer remediations only when interactive; in CI/non-TTY this stays a report (exit 1).
    if sys.stdin.isatty():
        _doctor_fix(healthy)
        if check_prereqs()[0] and _health_ok():
            print("\n" + ok("Fixed — all checks pass now."))
            return 0
    return 1


def _backup_db() -> bool:
    """Snapshot the encrypted DuckDB to data/backups/ before an update.

    The stack is stopped first so the file is checkpointed + consistent (the running app
    holds a write lock). Returns True if a backup was written or there was nothing to back up.
    """
    db_path = REPO_ROOT / "data" / "smartbrain.duckdb"
    if not db_path.exists():
        print(note("No existing data file yet — nothing to back up."))
        return True
    backups = REPO_ROOT / "data" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups / f"smartbrain_{stamp}.duckdb"
    print(note("Stopping the app for a consistent backup…"))
    _compose(["stop"])  # clean shutdown checkpoints the DB + releases the lock
    try:
        shutil.copy2(db_path, dest)
    except OSError as exc:
        print(fail(f"Backup failed: {exc}"))
        return False
    print(ok(f"Backed up to {dest}"))
    return True


def cmd_update() -> int:
    """Back up, pull the latest source, rebuild + restart the stack, and verify."""
    assert COMPOSE_FILE.parent.name == "compose", "compose file must live under compose/"
    print(_paint("1", "\nSmartBrain_3000 update\n"))
    if _compose_cmd() is None:
        print(fail("Docker Compose not found — run 'doctor' for details."))
        return 1
    print(note("This backs up your encrypted data, pulls the latest version, and restarts."))
    answer = input("Continue? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print(note("Update cancelled."))
        return 0
    if not _backup_db():  # forced backup before touching anything
        print(fail("Aborting update — backup did not succeed."))
        return 1
    # Pull the latest release (fast-forward only; never clobbers local changes).
    if (REPO_ROOT / ".git").exists():
        pulled = subprocess.run(["git", "-C", str(REPO_ROOT), "pull", "--ff-only"], check=False).returncode
        if pulled != 0:
            print(note("Couldn't fast-forward (local changes or diverged) — rebuilding the current source."))
    if _compose(["up", "-d", "--build"]) != 0:
        print(fail("Rebuild/restart failed."))
        return 1
    healthy = wait_healthy()
    assert isinstance(healthy, bool), "health result must be a bool"
    if not healthy:
        print(fail(f"Updated, but {_app_url()}/api/health did not become healthy in time."))
        return 1
    print(ok(f"\nUpdated and running at {_app_url()}"))
    return 0


def cmd_certs(hostnames: list[str]) -> int:
    """Generate a local CA + TLS cert (via mkcert) for LAN/mobile HTTPS.

    mkcert is used (not self-signed) so the CA can be trusted on your phone. The
    cert covers localhost + 127.0.0.1 plus any extra hostnames you pass (e.g. your
    chosen <name>.local and LAN IP). Output goes to data/certs/ (git-ignored).
    """
    assert isinstance(hostnames, list), "hostnames must be a list"
    if shutil.which("mkcert") is None:
        print(fail("mkcert not found — it's needed to make a local CA your devices can trust."))
        print(note("Install it (https://github.com/FiloSottile/mkcert#installation), then re-run."))
        return 1
    names = ["localhost", "127.0.0.1", *hostnames]
    assert names, "at least one hostname required"
    certs_dir = REPO_ROOT / "data" / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    cert, key = certs_dir / "cert.pem", certs_dir / "key.pem"
    # Limit mkcert to the system trust store (skips the Java/NSS stores, which can
    # error out and aren't needed — phones trust the CA via the rootCA.pem below).
    mkcert_env = {**os.environ, "TRUST_STORES": "system"}
    # Issue the cert first — that's the deliverable.
    code, out = _probe(["mkcert", "-cert-file", str(cert), "-key-file", str(key), *names], env=mkcert_env)
    if code != 0:
        print(fail(f"mkcert failed to issue the certificate: {out}"))
        return 1
    print(ok(f"TLS cert written to {certs_dir} for: {', '.join(names)}"))
    # Trust the CA on THIS computer (best effort — needs your password; safe to skip).
    print(note("Trusting the local CA on this computer (you may be prompted for your password)…"))
    if subprocess.run(["mkcert", "-install"], check=False, env=mkcert_env).returncode != 0:
        print(note("(couldn't auto-trust the CA here — fine; trust rootCA.pem manually if a browser warns.)"))
    caroot = _probe(["mkcert", "-CAROOT"], env=mkcert_env)[1].strip()
    print(note(f"Install this CA on your phone to trust the app: {caroot}/rootCA.pem"))
    print(note("Then start the LAN profile and set SMARTBRAIN_ALLOWED_HOSTS —"))
    print("      see docs/dev/mobile-and-remote.md.")
    return 0


def cmd_wireguard(action: str) -> int:
    """Enable/disable remote access via WireGuard (off by default).

    Brings the app up LAN-exposed (HTTPS) alongside a WireGuard server that
    auto-generates your phone's config + QR. Off Wi-Fi, the phone connects the
    tunnel and opens the SAME https://<LAN-IP>:33000 it uses at home.
    """
    assert action in ("up", "down", "status"), "action must be up/down/status"
    base = _compose_cmd()
    if base is None:
        print(fail("Docker Compose not available — see 'doctor'."))
        return 1
    files = ["-f", str(COMPOSE_FILE), "-f", str(LAN_FILE), "-f", str(WG_FILE)]
    peer = REPO_ROOT / "data" / "wireguard" / "peer_phone" / "peer_phone.png"

    if action == "down":
        code = subprocess.run([*base, *files, "stop", "wireguard"], check=False).returncode
        print(ok("Remote access stopped (the app keeps running).") if code == 0 else fail("could not stop WireGuard"))
        return code
    if action == "status":
        subprocess.run([*base, *files, "ps", "wireguard"], check=False)
        print(ok(f"Phone config + QR present: {peer}") if peer.exists()
              else note("No phone config yet — run: install.py wireguard up"))
        return 0

    # up
    print(note("Bringing up the app (LAN/HTTPS) + WireGuard. First, make sure you have run"))
    print("      'install.py certs <name>.local <LAN-IP>' and set SMARTBRAIN_ALLOWED_HOSTS to your LAN IP.")
    if subprocess.run([*base, *files, "up", "-d"], check=False).returncode != 0:
        print(fail("Failed to start WireGuard."))
        print(note("On macOS, Docker Desktop must permit the WireGuard kernel module; see Help -> Remote access."))
        return 1
    print(ok("WireGuard is up."))
    print(note("ONE router step: forward UDP 51820 to THIS computer so your phone can dial in."))
    print(note(f"Scan your phone's QR:  {peer}  (or: {' '.join([*base, *files, 'logs', 'wireguard'])})"))
    print(note("Then off Wi-Fi: connect WireGuard on the phone + open https://<your-LAN-IP>:33000."))
    print("      Full guide: in-app Help -> Remote access.")
    return 0


def cmd_webrtc(action: str) -> int:
    """Enable/disable remote access via WebRTC (off by default; no port-forward).

    'up' restarts the app with the WebRTC overlay (it dials OUT to your signaling
    node — nothing is exposed inbound). 'down' restarts it without the overlay.
    Needs SMARTBRAIN_SIGNALING_URL/_TOKEN (and SMARTBRAIN_ICE_URLS for the relay) set,
    pointing at the public node you run (compose/docker-compose.signaling.yml). Pair a
    phone from the app: Settings -> Remote access.
    """
    assert action in ("up", "down", "status"), "action must be up/down/status"
    base = _compose_cmd()
    if base is None:
        print(fail("Docker Compose not available — see 'doctor'."))
        return 1

    if action == "status":
        subprocess.run([*base, "-f", str(COMPOSE_FILE), "ps", "smartbrain"], check=False)
        on = bool(os.environ.get("SMARTBRAIN_SIGNALING_URL"))
        print(ok("Signaling URL is set; pair a phone at Settings -> Remote access.") if on
              else note("SMARTBRAIN_SIGNALING_URL is unset — remote access is off."))
        return 0

    if action == "down":
        code = subprocess.run([*base, "-f", str(COMPOSE_FILE), "up", "-d"], check=False).returncode
        print(ok("Remote access disabled (app restarted without the WebRTC overlay).") if code == 0
              else fail("could not restart the app"))
        return code

    # up
    if not os.environ.get("SMARTBRAIN_SIGNALING_URL"):
        print(fail("Set SMARTBRAIN_SIGNALING_URL (and _TOKEN / SMARTBRAIN_ICE_URLS) first —"))
        print("      point them at the public node you run (compose/docker-compose.signaling.yml).")
        return 2
    files = ["-f", str(COMPOSE_FILE), "-f", str(WEBRTC_FILE)]
    if subprocess.run([*base, *files, "up", "-d"], check=False).returncode != 0:
        print(fail("Failed to start the app with the WebRTC overlay."))
        return 1
    print(ok("Remote access (WebRTC) is on — no router port-forward needed."))
    print(note("Pair a phone: open the app -> Settings -> Remote access -> scan the QR (on Wi-Fi)."))
    print("      Full guide: in-app Help -> Remote access.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to a command."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # keep our output ordered with subprocess output
    parser = argparse.ArgumentParser(prog="install.py", description="SmartBrain_3000 installer")
    parser.add_argument(
        "command", nargs="?", default="install",
        choices=["install", "doctor", "update", "certs", "wireguard", "webrtc"],
    )
    parser.add_argument("hostnames", nargs="*",
                        help="hostnames for 'certs' (e.g. smartbrain.local 192.168.1.50); up/down/status for 'wireguard'/'webrtc'")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser after install")
    args = parser.parse_args(argv)
    assert args.command in ("install", "doctor", "update", "certs", "wireguard", "webrtc"), "unknown command"
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "update":
        return cmd_update()
    if args.command == "certs":
        return cmd_certs(args.hostnames)
    if args.command in ("wireguard", "webrtc"):
        action = args.hostnames[0] if args.hostnames else "status"
        if action not in ("up", "down", "status"):
            print(fail(f"Usage: install.py {args.command} [up|down|status]"))
            return 2
        return cmd_wireguard(action) if args.command == "wireguard" else cmd_webrtc(action)
    return cmd_install(open_browser=not args.no_open)


if __name__ == "__main__":
    sys.exit(main())
