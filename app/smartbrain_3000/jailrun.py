"""Subprocess jail for parsing hostile input (§16).

Today's only tenant is ``http_page`` extraction: HTML parsers (lxml + trafilatura's
extractor stack) are historically vulnerable to malformed markup, and running them
inline would risk taking the whole engine down on one bad host. The jail models
its process hygiene on ``claudecli.py``:

- stripped env (``SMARTBRAIN_``/``ANTHROPIC_`` scrubbed, minimal PATH),
- private cwd created 0700 (``tempfile.mkdtemp``),
- ``start_new_session=True`` so the child owns a fresh process group,
- stdin fed from a helper thread (a full pipe would otherwise deadlock against
  an un-drained stdout, claudecli precedent),
- stdout capped at ``_MAX_OUTPUT_BYTES``,
- stderr sent to devnull (an unread pipe would fill and
  wedge the child, claudecli precedent),
- watchdog ``threading.Timer`` kills the process GROUP via ``os.killpg`` at the
  deadline (never a bare kill — a helper process could otherwise hold the stdout
  pipe open past the SIGKILL, wedging the reader),
- cwd is always ``rmtree``-d, child always reaped.

Contract: ``run_extractor(html, url_hint) -> {"text": str, "title": str}``. Any
timeout, non-zero exit, malformed JSON, or oversize output raises ``JailError``;
the caller (``ni._fetch_http_page``) maps it to ``NIError('extract_jail', <class>)``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

log = logging.getLogger("smartbrain.jail")

_MAX_OUTPUT_BYTES = 1 * 1024 * 1024   # capped read on the child's stdout (1 MB)
_MAX_INPUT_BYTES = 2 * 1024 * 1024    # cap on bytes we ever write to the child
_DEFAULT_TIMEOUT_S = 20.0             # wall-clock ceiling per extraction
_REAP_TIMEOUT_S = 5.0                 # bounded wait for the child to exit after kill
# Minimal PATH — enough for ``sys.executable`` to find its own libs / entry, nothing
# more (claudecli's ``_cli_env`` stripping stance: an inherited PATH could point at
# an attacker-writable directory, and the child doesn't need arbitrary tools).
_MINIMAL_PATH_POSIX = "/usr/local/bin:/usr/bin:/bin"
_MINIMAL_PATH_WIN = "C:\\Windows\\System32;C:\\Windows"
# Parent dir of the ``smartbrain_3000`` package as it lives on THIS parent process.
# The child cwd is a fresh tempdir, so ``sys.path[0] == ''`` resolves there (empty).
# We thread this into ``PYTHONPATH`` so the child imports the SAME code the parent
# is running (source tree in tests + editable installs, site-packages in prod).
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Child bootstrap: import the dedicated entry module and run its main(). Keeping the
# real code in ``jail_extract`` (not this ``-c`` string) means the child is properly
# importable + testable and the ``-c`` payload stays a stable one-liner.
_CHILD_BOOTSTRAP = "from smartbrain_3000.jail_extract import main; main()"


class JailError(Exception):
    """A structured jail failure. ``reason`` is a short host-free class the caller
    can pass verbatim into ``NIError('extract_jail', reason)``.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        assert reason, "reason required"
        assert isinstance(detail, str), "detail must be a string"
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _jail_env() -> dict[str, str]:
    """Explicit env for the child: minimal PATH + interpreter-visibility variables,
    plus locale defaults. Every ``SMARTBRAIN_*`` / ``ANTHROPIC_*`` / auth-shaped key
    is dropped (claudecli ``_cli_env`` stripping stance).
    """
    minimal_path = _MINIMAL_PATH_WIN if os.name == "nt" else _MINIMAL_PATH_POSIX
    env: dict[str, str] = {"PATH": minimal_path, "LC_ALL": "C.UTF-8",
                            "LANG": "C.UTF-8"}
    # Prepend the parent-of-package path so the child imports SmartBrain from exactly
    # the same tree the parent is running (source tree in tests, site-packages in
    # prod). The parent's own ``PYTHONPATH`` (when set) is appended so a caller that
    # already threads an install path keeps it — the parent's is the ground truth.
    inherited = os.environ.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _PACKAGE_PARENT if not inherited
        else _PACKAGE_PARENT + os.pathsep + inherited
    )
    if os.environ.get("SYSTEMROOT"):  # Windows: needed for socket/urllib imports
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    assert "ANTHROPIC_API_KEY" not in env, "api key must never reach the child"
    for k in env:
        assert not k.startswith("SMARTBRAIN_"), f"SMARTBRAIN_ leak: {k}"
    return env


def _feed_stdin(proc: subprocess.Popen, blob: bytes) -> threading.Thread:
    """Write ``blob`` on a helper thread + close stdin (claudecli deadlock lesson).

    A large HTML page written inline would deadlock against the child's un-drained
    stdout: both sides blocked on full OS pipe buffers. Off-thread the write races
    the reader instead.
    """
    assert proc.stdin is not None, "stdin pipe must exist"
    assert isinstance(blob, bytes), "blob must be bytes"

    def _write() -> None:
        try:
            proc.stdin.write(blob)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # child died first — the reader loop / exit code surfaces the error

    t = threading.Thread(target=_write, name="jail-stdin", daemon=True)
    t.start()
    return t


def _read_capped(proc: subprocess.Popen, cap: int) -> tuple[bytes, bool]:
    """Read stdout up to ``cap`` bytes; return (buf, overflowed).

    An oversize reply short-circuits at ``cap + 1`` bytes so a runaway child cannot
    drive the parent's memory past the ceiling.
    """
    assert proc.stdout is not None, "stdout pipe must exist"
    assert cap > 0, "cap must be positive"
    buf = proc.stdout.read(cap + 1)
    assert isinstance(buf, (bytes, bytearray)), "stdout must yield bytes"
    if len(buf) > cap:
        return bytes(buf[:cap]), True
    return bytes(buf), False


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's process group (POSIX) — a helper process could otherwise
    keep the stdout pipe open past a direct kill and wedge the reader (claudecli
    lesson). Falls back to killing the direct child on non-POSIX / ProcessLookupError.
    """
    assert proc is not None, "process required"
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid via start_new_session
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # group already gone — direct kill below
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _reap(proc: subprocess.Popen) -> None:
    """Bounded wait so no zombie outlives the request (claudecli precedent)."""
    assert proc is not None, "process required"
    try:
        proc.wait(timeout=_REAP_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        _kill_group(proc)
        try:
            proc.wait(timeout=_REAP_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError):
            log.warning("jail: child failed to exit after kill (pid=%s)", proc.pid)


def _spawn(cwd: str, env: dict[str, str], url_hint: str) -> subprocess.Popen:
    """Start the child interpreter with ``-c`` bootstrap in a fresh process group.

    ``url_hint`` rides argv (under ``-c``, ``sys.argv[1:]`` carries trailing args)
    so trafilatura gets the page URL for extraction quality — argv is ps-visible,
    which is fine: the URL is user-consented spec content, never a secret.
    """
    assert cwd and os.path.isdir(cwd), "cwd must exist"
    assert env and "PATH" in env, "env must include a PATH"
    assert isinstance(url_hint, str), "url_hint must be a string"
    return subprocess.Popen(
        # ``-s`` skips user site-packages so a user-writable ``~/.local`` can't inject
        # imports into the child; we deliberately do NOT pass ``-I`` (isolated mode)
        # because it also strips PYTHONPATH, and a source-tree runner (tests, editable
        # install) needs it to import ``smartbrain_3000.jail_extract``.
        [sys.executable, "-s", "-c", _CHILD_BOOTSTRAP, url_hint],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        # DEVNULL, not STDOUT: a merged stream means one dependency warning on
        # stderr corrupts the JSON parse forever; devnull can't wedge the child
        # the way an unread PIPE would.
        stderr=subprocess.DEVNULL,
        cwd=cwd, env=env,
        start_new_session=(os.name == "posix"),
    )


def _validate_payload(text: str) -> dict:
    """Parse + shape-check the child's stdout; raise JailError on any deviation."""
    assert isinstance(text, str), "text must be a string"
    stripped = text.strip()
    if not stripped:
        raise JailError("empty_output")
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        raise JailError("malformed_json") from None
    if not isinstance(parsed, dict) or set(parsed) != {"text", "title"}:
        raise JailError("bad_shape")
    for name in ("text", "title"):
        if not isinstance(parsed[name], str):
            raise JailError("bad_field_type", name)
    return parsed


def run_extractor(html: bytes, url_hint: str, *,
                  timeout_s: float = _DEFAULT_TIMEOUT_S) -> dict:
    """Run one jailed extraction; return ``{"text": str, "title": str}``.

    Every failure — timeout, non-zero exit, malformed / oversize stdout, or a spawn
    error — raises ``JailError``. The caller maps to ``NIError('extract_jail', ...)``.
    Never propagates a raw subprocess exception past this boundary.
    """
    assert isinstance(html, (bytes, bytearray)), "html must be bytes"
    assert isinstance(url_hint, str), "url hint must be a string"
    if len(html) > _MAX_INPUT_BYTES:
        raise JailError("input_too_large", f"{len(html)}")
    if timeout_s <= 0:
        raise JailError("bad_timeout", f"{timeout_s}")
    cwd = tempfile.mkdtemp(prefix="smartbrain-jail-")
    try:
        os.chmod(cwd, 0o700)
    except OSError:
        pass  # best-effort: mkdtemp already yields 0o700 on POSIX
    try:
        try:
            proc = _spawn(cwd, _jail_env(), url_hint)
        except (OSError, ValueError) as exc:
            raise JailError("spawn_failed", exc.__class__.__name__) from None
        return _drive_jailed_child(proc, bytes(html), timeout_s)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def _drive_jailed_child(proc: subprocess.Popen, blob: bytes,
                        timeout_s: float) -> dict:
    """Feed ``blob``, read stdout under a watchdog, validate the payload.

    Split out of ``run_extractor`` so ``proc`` has a non-None type inside every
    branch — the timer callback closes over a live ``Popen``, and the finally
    clause knows the child exists. All raised ``JailError`` classes and cleanup
    (kill + reap) live here so the caller only owns the cwd.
    """
    assert proc is not None, "process required"
    assert isinstance(blob, bytes), "blob must be bytes"
    assert timeout_s > 0, "timeout must be positive"
    timed_out = threading.Event()

    def _expire() -> None:
        timed_out.set()
        _kill_group(proc)

    watchdog = threading.Timer(timeout_s, _expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        _feed_stdin(proc, blob)
        raw, overflowed = _read_capped(proc, _MAX_OUTPUT_BYTES)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            _kill_group(proc)
        _reap(proc)
    if timed_out.is_set():
        raise JailError("timeout")
    if overflowed:
        raise JailError("oversize_output")
    if proc.returncode not in (0, None):
        raise JailError("nonzero_exit", str(proc.returncode))
    return _validate_payload(raw.decode("utf-8", errors="replace"))
