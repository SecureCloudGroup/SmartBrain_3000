#!/usr/bin/env python3
# smartbrain-test-fake-engine
"""A stand-in for the Obscura binary in the browser-component tests (stdlib only).

It parses the same flags the manifest renders (and refuses any other, like clap does —
``--quiet`` included, which the manifests forbid), REQUIRES ``--proxy``, and reaches the
page the way the real engine does: ``CONNECT host:port`` through the proxy, then a
plain-text GET inside the tunnel (the local test target speaks no TLS). Like the real
engine without ``--quiet`` it prints ``Fetching <url>...`` and ``Page loaded: <url> -
"<title>"`` on stderr (tracing lines carry ANSI colour). ``--version`` prints
``obscura 9.9.9``.

Behaviour is chosen by ``fake_mode.json`` beside the run dir (the run dir is
``dirname(--storage-dir)``; tests point the runner's run-dir parent at their tmp_path and
write the mode file there). Every run appends its argv and the OBSCURA_*/HOME/TMPDIR/PATH
environment to ``fake_seen.jsonl`` in the same place.

Modes: happy (default) · hang · huge · crash · linger · sigkill_self · memory ·
ignore_proxy · blocked · navigate_fail · offsite · no_output · hard_timeout · heap_warn ·
watchdog_warn · final_url.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

VERSION = "9.9.9"
VALUE_FLAGS = {"--proxy", "--storage-dir", "--timeout", "--dump", "--output", "--wait-until",
               "--v8-flags", "--user-agent"}
BOOL_FLAGS = {"--stealth"}
WARN = "\x1b[2m2026-10-09T11:27:25.392162Z\x1b[0m \x1b[33m WARN\x1b[0m \x1b[2mobscura_js::runtime\x1b[0m: "


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    sys.exit(code)


def parse(argv):
    args, rest = {}, list(argv)
    for _ in range(len(argv) + 1):
        if not rest:
            break
        tok = rest.pop(0)
        if tok in VALUE_FLAGS:
            if not rest:
                fail(f"error: a value is required for '{tok}'", 2)
            args[tok] = rest.pop(0)
        elif tok in BOOL_FLAGS:
            args[tok] = True
        elif tok == "fetch" and "fetch" not in args:
            if not rest:
                fail("error: fetch needs a URL", 2)
            args["fetch"] = rest.pop(0)
        else:
            fail(f"error: unexpected argument '{tok}' found", 2)
    return args


def side_dir(args):
    return os.path.dirname(os.path.dirname(args["--storage-dir"]))


def record(args, argv):
    seen = {"argv": argv, "env": {k: v for k, v in os.environ.items()
                                  if k.startswith("OBSCURA_") or k in ("HOME", "TMPDIR", "PATH")}}
    with open(os.path.join(side_dir(args), "fake_seen.jsonl"), "a") as fh:
        fh.write(json.dumps(seen) + "\n")


def load_mode(args):
    try:
        with open(os.path.join(side_dir(args), "fake_mode.json")) as fh:
            return json.load(fh)
    except OSError:
        return {}


def proxy_address(args):
    proxy = urlsplit(args["--proxy"])
    return proxy.hostname, proxy.port


def tunnel(args, host, port):
    """CONNECT through the proxy; returns the socket or None when refused."""
    sock = socket.create_connection(proxy_address(args), timeout=10)
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head = b""
    for _ in range(8192):
        chunk = sock.recv(1)
        if not chunk:
            break
        head += chunk
        if head.endswith(b"\r\n\r\n"):
            break
    if not head.startswith(b"HTTP/1.1 200"):
        sock.close()
        return None
    return sock


def get_page(sock, host, path):
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    data = b""
    for _ in range(100_000):
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    sock.close()
    return data.partition(b"\r\n\r\n")[2]


def navigate_error(url, why):
    fail(f"Error: Failed to navigate to {url}: Network error: {why}")


def linger(cfg):
    """A grandchild in the same process group that outlives this process."""
    code = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(300)"
    subprocess.Popen([sys.executable, "-c", code, cfg["pid_file"]])
    for _ in range(200):
        if os.path.exists(cfg["pid_file"]) and os.path.getsize(cfg["pid_file"]):
            return
        time.sleep(0.02)


def write_output(args, body):
    with open(args["--output"], "wb") as fh:
        fh.write(body)


def main_document(args, cfg):
    url = args["fetch"]
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    if cfg.get("mode") == "ignore_proxy":
        sock = socket.create_connection(("127.0.0.1", int(cfg["direct_port"])), timeout=10)
    else:
        sock = tunnel(args, host, port)
    if sock is None:
        navigate_error(url, f"https://{host}/: error sending request")
    return get_page(sock, host, parts.path or "/")


def run(argv):
    if argv == ["--version"]:
        print(f"obscura {VERSION}")
        return 0
    args = parse(argv)
    if "--proxy" not in args or "fetch" not in args or "--storage-dir" not in args:
        fail("Error: the fake engine requires --proxy, --storage-dir and fetch <url>", 2)
    record(args, argv)
    cfg = load_mode(args)
    mode = cfg.get("mode", "happy")
    url = args["fetch"]
    sys.stderr.write(f"Fetching {url}...\n")
    if mode == "blocked":
        fail(f"Error: Failed to navigate to {url}: HTTP 403 Forbidden (challenge page)")
    if mode == "navigate_fail":
        navigate_error(url, "connection refused")
    if mode == "hard_timeout":
        fail("obscura: hard timeout exceeded (18s); forcing exit", 124)
    if mode in ("linger", "sigkill_self"):
        linger(cfg)
    if mode == "sigkill_self":
        os.kill(os.getpid(), signal.SIGKILL)
    body = main_document(args, cfg)
    if mode == "offsite" and tunnel(args, cfg["offsite_host"], 443) is None:
        navigate_error(url, "redirected away")
    if mode == "hang":
        time.sleep(600)
    if mode == "memory":
        hoard = []
        for _ in range(int(cfg.get("alloc_mb", 1024)) // 8):
            hoard.append(bytearray(os.urandom(1)) * (8 * 1024 * 1024))
            time.sleep(0.02)
        time.sleep(600)
    if mode == "crash":
        os.kill(os.getpid(), signal.SIGSEGV)
    if mode == "no_output":
        return 0
    if args.get("--dump") == "assets":
        lines = [json.dumps({"url": u, "type": "script"}) for u in cfg.get("assets", [])]
        write_output(args, ("\n".join(lines) + "\n").encode())
        return 0
    if mode == "huge":
        body = b"x" * int(cfg["size"])
    write_output(args, body)
    if mode == "heap_warn":
        sys.stderr.write(WARN + "V8 heap limit reached: terminated the current JavaScript task\n")
    if mode == "watchdog_warn":
        sys.stderr.write(WARN + "V8 watchdog fired: terminated a synchronous overrun\n")
    sys.stderr.write(f'Page loaded: {cfg.get("final_url", url)} - "Fixture"\n')
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
