"""Claude Code (local CLI) chat backend — drives the user's own ``claude`` binary.

This provider is configured like a local model server (Settings → Local models)
but it is NOT local in the privacy sense: every prompt goes to Anthropic under
the user's own Claude Code login. It is therefore deliberately absent from
``gateway._LOCAL_PROVIDER_NAMES`` — ``is_local()`` must stay False so the
self-review privacy gate refuses it and metrics/costing treat it as cloud.

Containment (test-enforced, documented in docs/02-models.md): the CLI runs
headless (``-p``) with a custom agent whose tool set is EMPTY (verified live:
the model then has no Bash/file/web access), ``--no-session-persistence`` (no
transcript on disk), and ``--setting-sources ""`` (no user CLAUDE.md/settings
reach the session). SmartBrain's approval-gated tools ride the same fenced-JSON
text protocol local models use (``agent._extract_text_tool_calls`` recovers it).
Nothing conversation-derived goes on the process argv — system prompt, tool
specs, and transcript all travel over stdin (argv is visible in ``ps``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from collections import deque
from collections.abc import Iterator

from . import gateway, runtime

log = logging.getLogger(__name__)

PROVIDER = "claudecode"
_PREFIX = "claudecode/"
# Model aliases the CLI resolves to the current generation — stable across CLI
# updates, unlike dated model ids.
MODELS = ("opus", "sonnet", "haiku")
# Where the official installer / npm / brew put the binary. shutil.which covers a
# terminal PATH; a GUI-launched app inherits launchd's minimal PATH (the Docker
# lesson in launcher/stack/stack.go EnsureDockerPath), so probe these too.
# (Windows installs live under %LOCALAPPDATA%/%APPDATA% and are found via PATH
# only — acceptable until this provider sees real Windows use.)
_BIN_CANDIDATES = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)
_AGENT_NAME = "smartbrain"
# Static by design: argv is world-readable (`ps`), so the agent definition must
# never carry the real system prompt — that goes over stdin (see chat_stream).
_AGENT_PROMPT = ("You are SmartBrain's language-model backend. The user message starts "
                 "with '## System instructions' — follow them exactly. The '## Conversation' "
                 "transcript follows; reply as the Assistant per those instructions.")
_MIN_MAJOR = 2  # --agents/--setting-sources floor; older CLIs fail with "unknown option"
# A CLI turn is a whole model answer, not one HTTP hop: the gateway branches floor their
# per-call timeout to this so the interactive default (60s, sized for Bifrost round-trips)
# can't kill a healthy long answer at exactly one minute (2026-09 audit).
MIN_TIMEOUT = 300.0
_PROBE_TIMEOUT = 15.0
_PROBE_TTL = 5.0  # GET /api/local-models is polled — don't fork+exec on every call
_UPDATE_TIMEOUT = 300.0
_MAX_STREAM_LINES = 40000  # fixed bound on CLI output lines per call (P10 #2)
_MAX_MESSAGES = 500  # bound on transcript flattening (the NEWEST turns are what's kept)
_MAX_SCAN = 2000  # bound on scanning the raw message list at all
_OUTPUT_TAIL = 2000  # chars of CLI output kept for error surfaces
_NOISE_KEEP = 20  # non-JSON output lines kept for diagnostics (stderr is merged in)
# Transcript-forgery guard (2026-09 audit): content is flattened under markdown headings,
# so a pasted message could open a line with "### Tool result" and fake an action. Any
# content line that impersonates one of OUR sentinels gets quoted ("> ") — visually intact,
# structurally inert. Tool results are json.dumps single-liners, so this targets the raw
# user/assistant bodies where real newlines survive.
_HEADING_FORGERY = re.compile(
    r"^(\s{0,3})(#{1,6}\s*(?:System instructions|Conversation|User|Assistant|Tool result)\b)",
    re.IGNORECASE | re.MULTILINE)

_probe_lock = threading.Lock()
_probe_cache: tuple[float, dict] | None = None
# Turn-scoped session continuity (docs/internal/ni-format.md §28): step 1 opens a session
# with a known UUID (via --session-id), steps 2..N resume it (via --resume) sending only
# the delta content, and TurnSession.close() removes both the per-turn cwd and the CLI's
# session-file mirror at ~/.claude/projects/<slug>. The session id is a uuid4 (not
# conversation-derived) — safe to carry on argv alongside the static agent definition.
_SESSION_CWD_PREFIX = "smartbrain-claudecli-turn-"
_SLUG_SAFE = re.compile(r"[^A-Za-z0-9]+")

# Last plan-window report from the CLI's rate_limit_event (emitted per chat run).
# Single-reference swap under CPython — read/written whole, never mutated in place.
_rate_limit: dict | None = None


def rate_limit_status() -> dict | None:
    """The most recent Claude plan-window report, or None before any chat has run.

    ``{status, resets_at (epoch seconds), using_overage, captured_at}`` — the UI's
    honest answer to "am I burning my Claude quota?" without guessing plan tiers."""
    return dict(_rate_limit) if _rate_limit is not None else None


def _capture_rate_limit(event: dict) -> None:
    """Record a rate_limit_event's plan-window fields (best-effort)."""
    assert isinstance(event, dict), "event must be a dict"
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return
    global _rate_limit
    _rate_limit = {"status": str(info.get("status") or ""),
                   "resets_at": info.get("resetsAt"),
                   "using_overage": bool(info.get("isUsingOverage")),
                   "captured_at": time.time()}

# Serve-time consent gate (2026-09 audit): the enabled flag must gate SERVING, not just the
# catalog — otherwise a route/schedule/explicit model id ships chats to Anthropic for a user
# who never clicked Connect (never saw the red warning), and Disconnect wouldn't stop them.
# Synced from the encrypted store at unlock/enable/disable; a fresh (locked) process is off.
_enabled = False
_work_dir: str | None = None


def set_enabled(value: bool) -> None:
    """Sync the serve-time gate with the stored enabled flag (unlock/enable/disable)."""
    assert isinstance(value, bool), "enabled must be a bool"
    global _enabled
    _enabled = value


def _private_cwd() -> str:
    """A per-process private (0700) working dir for the CLI — never the shared /tmp,
    where another local user could pre-place .claude/agents or .mcp.json for us to trip on."""
    global _work_dir
    if _work_dir is None or not os.path.isdir(_work_dir):
        _work_dir = tempfile.mkdtemp(prefix="smartbrain-claudecli-")
    assert os.path.isdir(_work_dir), "work dir must exist"
    return _work_dir


def _slug_for(path: str) -> str:
    """The CLI's ``~/.claude/projects/<slug>/`` directory name for a given cwd.

    Verified live against the CLI (v2.1.148): the slug is the RESOLVED absolute path
    with every non-alnum run collapsed to a single ``-``. ``/var`` symlinks to
    ``/private/var`` on macOS, so ``os.path.realpath`` is required — without it we'd
    wipe the wrong directory (or none) on close.
    """
    assert isinstance(path, str) and path, "path required"
    resolved = os.path.realpath(path)
    assert resolved.startswith("/"), "session cwd must be absolute"
    return _SLUG_SAFE.sub("-", resolved)


def _wipe_session_dirs(cwd: str, session_id: str) -> None:
    """Delete the per-turn cwd + the CLI's session-file mirror (best-effort, idempotent).

    Split from ``TurnSession.close`` so ``weakref.finalize`` can drive it too — a
    session forgotten past its finally still gets wiped at GC / interpreter exit.
    """
    assert isinstance(cwd, str) and cwd, "cwd required"
    assert isinstance(session_id, str), "session id required"
    slug_dir = os.path.join(os.path.expanduser("~/.claude/projects"), _slug_for(cwd))
    for path in (cwd, slug_dir):  # exactly two known paths (P10 #2: fixed bound)
        shutil.rmtree(path, ignore_errors=True)


class TurnSession:
    """One CLI session, scoped to a single agent turn.

    First call: full ``_flatten`` prompt with ``--session-id <uuid>`` (persistence ON,
    but confined — the session file lands under our unique per-turn cwd's slug dir,
    which only this turn owns). Subsequent calls: ``--resume <uuid>`` sending only the
    delta rendered by ``_flatten_delta``. ``close()`` wipes both the cwd and the CLI's
    slug directory. On a resume failure, ``disable()`` flips the session inactive so the
    remainder of the turn falls back to stateless — never a user-facing failure mode.
    """

    def __init__(self) -> None:
        self.session_id = str(uuid.uuid4())
        cwd = tempfile.mkdtemp(prefix=_SESSION_CWD_PREFIX)
        os.chmod(cwd, 0o700)
        self.cwd = cwd
        self.sent_count = 0  # messages already delivered to this session
        self.active = True   # cleared on close() or on fallback via disable()
        self._closed = False
        # Finalizer wipes the dirs at GC / interpreter exit if close() was missed;
        # detached in close() so the wipe runs exactly once.
        self._finalizer = weakref.finalize(self, _wipe_session_dirs, self.cwd, self.session_id)
        assert self.session_id, "session id required"
        assert os.path.isdir(self.cwd), "session cwd must exist"

    def disable(self) -> None:
        """Mark this session inactive after a resume failure (fallback path)."""
        assert not self._closed, "cannot disable a closed session"
        assert self.active, "session already inactive"
        self.active = False

    def close(self) -> None:
        """Wipe the per-turn cwd + the CLI's session-file mirror (idempotent)."""
        assert isinstance(self._closed, bool), "close flag must be a bool"
        if self._closed:
            return
        self._closed = True
        self.active = False
        self._finalizer.detach()
        _wipe_session_dirs(self.cwd, self.session_id)


_ORPHAN_MAX_AGE_S = 86400.0  # sweep session dirs older than 1 day (stale by any measure)
_ORPHAN_SCAN_CEILING = 4096  # fixed upper bound on the tempdir listing (P10 #2)


def sweep_orphan_sessions() -> int:
    """Delete stale per-turn CLI session dirs left behind by crashed processes.

    A ``TurnSession`` normally wipes its cwd + the CLI's slug mirror in ``close()``
    (and the ``weakref.finalize`` backstops that at GC), but a hard interpreter
    exit or a container kill can leave a ``smartbrain-claudecli-turn-*`` dir behind
    forever — the CLI's ``~/.claude/projects/<slug>/`` mirror grows unbounded on a
    long-lived host. Called lazily from ``open_turn_session`` (cheap: at most a
    listdir + a stat + a couple of shutil.rmtree calls per stale dir).

    Ignores dirs younger than ``_ORPHAN_MAX_AGE_S`` so we never race a
    still-running turn. Returns the count of dirs wiped (0 on a clean host).
    Best-effort — never raises past this function's boundary.
    """
    root = tempfile.gettempdir()
    try:
        entries = os.listdir(root)
    except OSError as exc:
        log.debug("orphan sweep: listdir failed: %s", exc)
        return 0
    now = time.time()
    wiped = 0
    for count, name in enumerate(entries):  # bounded (P10 #2)
        if count >= _ORPHAN_SCAN_CEILING:
            break
        if not name.startswith(_SESSION_CWD_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue  # vanished between listdir and stat — someone else's win
        if age < _ORPHAN_MAX_AGE_S:
            continue
        # No session_id to pass — pass "" so ``_wipe_session_dirs`` still drops the
        # cwd + its slug mirror (the mirror path is derived from cwd, not from id).
        _wipe_session_dirs(path, "")
        wiped += 1
    return wiped


def open_turn_session() -> TurnSession:
    """Open a fresh per-turn CLI session (see ``TurnSession`` for lifecycle).

    Piggy-backs an orphan-session sweep here (§28 audit): a hard interpreter
    exit or container kill can leave stale ``smartbrain-claudecli-turn-*`` dirs
    behind, and this is the one place every turn passes through. Sweep failures
    never block the new session.
    """
    try:
        sweep_orphan_sessions()
    except Exception as exc:  # never fail an actual turn on a housekeeping hiccup
        log.debug("claudecli orphan sweep skipped: %s", exc)
    session = TurnSession()
    assert session.active and session.sent_count == 0, "new session must be active + empty"
    assert os.path.isdir(session.cwd), "session cwd must exist"
    return session


def _cli_env() -> dict[str, str]:
    """Subprocess env: inherited minus SmartBrain secrets and Anthropic key overrides,
    plus the CLI's own telemetry kill-switch.

    Dropping ANTHROPIC_API_KEY/AUTH_TOKEN keeps auth on the user's own `claude` login
    (an inherited key would silently switch billing to the API). The telemetry switch
    backs the docs' claim that the conversation is the traffic this feature adds.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SMARTBRAIN_") and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    assert "ANTHROPIC_API_KEY" not in env, "api key must never reach the CLI"
    return env


def is_claudecode(model: str) -> bool:
    """True when ``model`` routes to the Claude Code CLI provider."""
    assert isinstance(model, str), "model must be a string"
    return model.startswith(_PREFIX)


def binary_path() -> str | None:
    """Locate the ``claude`` binary (PATH first, then known install dirs)."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _BIN_CANDIDATES:  # fixed, bounded
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _run_cli(args: list[str], timeout: float, stdin_text: str = "") -> subprocess.CompletedProcess:
    """Run the CLI once, captured, from a neutral cwd (never the app's own tree)."""
    assert args, "args must be non-empty"
    assert timeout > 0, "timeout must be positive"
    return subprocess.run(
        args, input=stdin_text, capture_output=True, text=True,
        timeout=timeout, check=False, cwd=_private_cwd(), env=_cli_env(),
    )


def _version_ok(version: str | None) -> bool:
    """True when the reported version is new enough for the flags this module uses."""
    assert version is None or isinstance(version, str), "version must be a string or None"
    if not version:
        return False
    head = version.split(".", 1)[0].strip()
    return head.isdigit() and int(head) >= _MIN_MAJOR


def probe(*, timeout: float = _PROBE_TIMEOUT, force: bool = False) -> dict:
    """Return ``{supported, installed, path, version, version_ok, logged_in, reachable}``.

    Never raises, and never generates model traffic — ``auth status`` is a purely
    local check. Cached for ``_PROBE_TTL`` seconds: the status endpoint is polled,
    and two fork+execs per poll would be rude to the host. ``force`` busts the
    cache — the UI's explicit "Check again" must never show a stale answer.
    """
    assert timeout > 0, "timeout must be positive"
    global _probe_cache
    with _probe_lock:
        if (not force and _probe_cache is not None
                and time.monotonic() - _probe_cache[0] < _PROBE_TTL):
            return dict(_probe_cache[1])
        out = _probe_fresh(timeout)
        _probe_cache = (time.monotonic(), dict(out))
    return out


def _probe_fresh(timeout: float) -> dict:
    """The uncached probe body (see ``probe``)."""
    assert timeout > 0, "timeout must be positive"
    out = {"supported": not runtime.in_container(), "installed": False, "path": None,
           "version": None, "version_ok": False, "logged_in": False, "reachable": False}
    if not out["supported"]:
        return out  # the CLI lives on the host; a container cannot exec it
    path = binary_path()
    if not path:
        return out
    out["installed"], out["path"] = True, path
    try:
        ver = _run_cli([path, "--version"], timeout)
        out["version"] = (ver.stdout or "").strip().split("\n")[0] or None
        auth = _run_cli([path, "auth", "status"], timeout)
        info = json.loads(auth.stdout or "{}")
        out["logged_in"] = bool(isinstance(info, dict) and info.get("loggedIn"))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("claude probe incomplete: %s", exc)
        return out  # partially filled — installed but not confirmably ready
    out["version_ok"] = _version_ok(out["version"])
    out["reachable"] = out["logged_in"] and out["version_ok"]
    return out


def update(*, timeout: float = _UPDATE_TIMEOUT) -> dict:
    """Run ``claude update`` (checks and installs); return output + fresh version."""
    assert timeout > 0, "timeout must be positive"
    global _probe_cache
    path = binary_path()
    if not path:
        return {"ok": False, "output": "Claude Code is not installed."}
    try:
        proc = _run_cli([path, "update"], timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "The update did not finish in time."}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "output": str(exc)[:_OUTPUT_TAIL]}
    with _probe_lock:
        _probe_cache = None  # an update changes exactly what the cache holds
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return {"ok": proc.returncode == 0, "output": text[-_OUTPUT_TAIL:],
            "version": probe()["version"]}


def catalog_models() -> list[dict]:
    """Catalog entries for the fixed alias models (mirrors list_models' shape)."""
    out = []
    for name in MODELS:  # fixed, bounded
        mid = _PREFIX + name
        out.append({"id": mid, "name": mid, "provider": PROVIDER, "context_length": None,
                    "pricing": None, "chat": True, "embed": False})
    assert out, "catalog must not be empty"
    return out


def _tool_instructions(tools_spec: list[dict]) -> str:
    """Render OpenAI tool specs into the fenced-JSON text protocol the agent recovers."""
    assert tools_spec, "tools_spec must be non-empty"
    lines = ["", "## Tools",
             "You may use these SmartBrain tools (no other tools exist):"]
    for spec in tools_spec[:64]:  # bounded
        fn = spec.get("function") or {}
        schema = json.dumps(fn.get("parameters") or {}, separators=(",", ":"))
        lines.append(f"- {fn.get('name')}: {fn.get('description') or ''} Parameters: {schema}")
    lines += [
        "",
        'To call a tool, reply with ONLY one fenced block — no other text before or after:',
        '```json', '{"name": "<tool_name>", "arguments": { ... }}', '```',
        'One tool call per reply. A "### Tool result" entry in the transcript is that call\'s '
        'output — read it and continue. When no tool is needed, reply normally in plain text '
        'and never mention this protocol.',
    ]
    return "\n".join(lines)


def _neutralize(text: str) -> str:
    """Quote any content line that impersonates a transcript sentinel heading."""
    assert isinstance(text, str), "text must be a string"
    return _HEADING_FORGERY.sub(r"\1> \2", text)


def _render_assistant(msg: dict) -> str:
    """Render an assistant turn, re-emitting recovered tool calls as fenced JSON."""
    assert msg.get("role") == "assistant", "assistant message required"
    body = _neutralize(str(msg.get("content") or ""))
    for tc in (msg.get("tool_calls") or [])[:16]:  # bounded
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        call = json.dumps({"name": fn.get("name"), "arguments": args})
        body = (body + "\n" if body else "") + f"```json\n{call}\n```"
    return body


def _flatten(messages: list[dict], tools_spec: list[dict] | None) -> str:
    """Build the single stdin prompt: system instructions + conversation transcript.

    Everything conversation-derived rides stdin (never argv): system messages and
    the tool protocol under '## System instructions', then the transcript under
    '## Conversation' with ### role headings.
    """
    assert messages, "messages must be non-empty"
    bounded = messages[:_MAX_SCAN]  # bounded scan of the raw list
    sys_parts = [str(m.get("content") or "") for m in bounded if m.get("role") == "system"]
    # Keep the NEWEST turns: past the cap, it's the oldest history that must go —
    # dropping the tail would silently answer stale context (2026-09 audit).
    recent = [m for m in bounded if m.get("role") != "system"][-_MAX_MESSAGES:]
    turns: list[str] = []
    for msg in recent:  # bounded by _MAX_MESSAGES
        role = msg.get("role")
        if role == "assistant":
            turns.append("### Assistant\n" + _render_assistant(msg))
        elif role == "tool":
            turns.append("### Tool result\n" + _neutralize(str(msg.get("content") or "")))
        else:  # user (and anything unrecognized reads safest as user input)
            turns.append("### User\n" + _neutralize(str(msg.get("content") or "")))
    system = "\n\n".join(p for p in sys_parts if p)
    system += ("\n\nReply as the Assistant: output ONLY the reply itself — no role "
               "headings, no transcript markup.")
    if tools_spec:
        system += _tool_instructions(tools_spec)
    return "## System instructions\n" + system + "\n\n## Conversation\n" + "\n\n".join(turns)


def _flatten_delta(new_messages: list[dict]) -> str:
    """Render only the trailing NEW turn segments for a --resume call (no scaffolding).

    The session already holds the ``## System instructions`` block, the tool protocol,
    and every prior turn — sending them again would double-count. Assistant messages
    are skipped because the CLI generated them itself in-session; we re-send only the
    tool results / new user turns the caller has appended since the last CLI call.
    Same ``### Role`` headings and heading-forgery neutralization as ``_flatten``, so
    the session sees a uniform transcript.
    """
    assert isinstance(new_messages, list), "messages must be a list"
    assert new_messages, "delta requires at least one new message"
    turns: list[str] = []
    for msg in new_messages[:_MAX_MESSAGES]:  # bounded (P10 #2)
        role = msg.get("role")
        if role in ("assistant", "system"):
            continue  # CLI's own output / already in-session — never re-send
        heading = "### Tool result" if role == "tool" else "### User"
        turns.append(heading + "\n" + _neutralize(str(msg.get("content") or "")))
    return "\n\n".join(turns)


def _delta_text_or_empty(messages: list[dict], sent_count: int) -> str:
    """Delta text for a --resume call; ``""`` when nothing new user/tool to send.

    Returns empty when only assistant messages were appended since the last call —
    the caller then falls through to a full stateless call for THIS step (rare).
    """
    assert isinstance(messages, list), "messages must be a list"
    assert sent_count >= 0, "sent_count must be non-negative"
    delta = messages[sent_count:]
    if not delta:
        return ""
    return _flatten_delta(delta)


def _command(model: str, *, session: TurnSession | None = None, resume: bool = False) -> list[str]:
    """Build the headless CLI command: empty-toolset agent, no persistence, no settings.

    Argv carries NOTHING conversation-derived — only static flags, the static agent
    definition, and (in session mode) the per-turn session UUID. UUIDs are random
    and unrelated to conversation content, so they are safe on argv (which is
    world-readable via ``ps``). The real content goes over stdin. ``--setting-sources
    ""`` loads no user/project settings: verified live that without it the CLI
    injects the user's global CLAUDE.md. (The CLI still tells the model the current
    date and the signed-in account's own email — benign, documented in
    docs/02-models.md.)

    Session mode (see ``TurnSession``): ``--no-session-persistence`` is DROPPED
    (persistence is required for --resume), replaced by ``--session-id <uuid>`` on
    the first call and ``--resume <uuid>`` on subsequent calls in the same turn.
    Containment flags (--setting-sources, --strict-mcp-config, --agents/--agent)
    stay on EVERY call so the second turn can't quietly widen access.
    """
    assert is_claudecode(model), "model must be claudecode/<alias>"
    assert session is not None or not resume, "resume requires a session"
    if not _enabled:
        raise gateway.GatewayError(403, "Claude Code isn't connected — enable it under "
                                        "Settings → Local models first.")
    path = binary_path()
    if not path:
        raise gateway.GatewayError(503, "Claude Code is not installed (Settings → Local models).")
    agents = json.dumps({_AGENT_NAME: {
        "description": "SmartBrain chat backend", "prompt": _AGENT_PROMPT, "tools": []}})
    base = [path, "-p", "--verbose", "--output-format", "stream-json",
            "--include-partial-messages",
            "--setting-sources", "", "--strict-mcp-config",
            "--model", model.split("/", 1)[1],
            "--agents", agents, "--agent", _AGENT_NAME]
    if session is None:
        return base + ["--no-session-persistence"]
    if resume:
        return base + ["--resume", session.session_id]
    return base + ["--session-id", session.session_id]


def _fail(returncode: int | None, tail: str) -> gateway.GatewayError:
    """Map a failed CLI run to a GatewayError with an actionable message."""
    lowered = tail.lower()
    if "unknown option" in lowered:
        return gateway.GatewayError(502, "This Claude Code version is too old for SmartBrain — "
                                         "press Update Claude Code under Settings → Local models.")
    if "log in" in lowered or "logged out" in lowered or "authentication" in lowered:
        return gateway.GatewayError(401, "Claude Code is not logged in — run `claude` in a "
                                         "terminal and sign in, then try again.")
    return gateway.GatewayError(502, (tail or f"claude exited with code {returncode}")[-_OUTPUT_TAIL:])


def _feed_stdin(proc: subprocess.Popen, text: str) -> threading.Thread:
    """Write the prompt on a helper thread — a large prompt would otherwise deadlock
    against the child's un-drained output pipe (both sides blocked on full buffers)."""
    assert proc.stdin is not None, "stdin pipe must exist"

    def _write() -> None:
        try:
            proc.stdin.write(text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # child died first — the reader loop surfaces the real error

    thread = threading.Thread(target=_write, name="claudecli-stdin", daemon=True)
    thread.start()
    return thread


def chat_stream(messages: list[dict], model: str, *, timeout: float = 300.0,
                tools_spec: list[dict] | None = None,
                session: TurnSession | None = None) -> Iterator[dict]:
    """Stream deltas from one CLI call — same item shape as ``gateway.chat_stream``.

    Tool offers ride the text protocol (see ``_tool_instructions``); a fenced tool
    call streams as text and the agent stream path's suppress-and-recover logic
    handles it, so ``tool_calls`` here is always None.

    Session mode (``session`` given): the first call opens the CLI session with
    ``--session-id <uuid>`` and the full ``_flatten`` prompt; every later call in
    the same turn uses ``--resume <uuid>`` and sends only the delta from
    ``_flatten_delta`` (the CLI keeps context in-session, we skip re-ingesting the
    transcript). A resume failure BEFORE the first chunk is a silent fallback: the
    session is disabled and this call restarts stateless. A mid-stream failure
    still raises (a partial stream can't be re-yielded from the fallback).

    Process hygiene: stderr is merged into stdout (a separate unread pipe can fill
    and wedge the child), stdin is fed from a helper thread, and a watchdog timer
    kills the child at the deadline — a blocking readline alone would never notice
    a hung CLI.
    """
    assert messages and model, "messages + model required"
    assert timeout > 0, "timeout must be positive"
    if session is not None and session.active and session.sent_count > 0:
        yielded = yield from _try_resume(messages, model, timeout, session)
        if yielded:
            return  # resume succeeded; sent_count updated inside _try_resume
    # Fresh call: first-of-session (full flatten + --session-id) OR fully stateless.
    use_session = session is not None and session.active
    cwd = session.cwd if use_session else _private_cwd()
    cmd = _command(model, session=(session if use_session else None))
    prompt = _flatten(messages, tools_spec)
    yield from _run_one_stream(cmd, prompt, timeout, cwd)
    if use_session:
        session.sent_count = len(messages)


def _try_resume(messages: list[dict], model: str, timeout: float,
                session: TurnSession) -> Iterator[dict]:
    """Attempt a --resume call; returns True (via ``return``) iff a stream was yielded.

    Empty delta or start-up failure returns False so the caller falls through to a
    full stateless call. A mid-stream error still propagates (a partial stream can't
    be replayed from the fallback path without double-yielding to the caller).
    """
    assert session.active and session.sent_count > 0, "resume needs an active in-session"
    assert isinstance(messages, list), "messages must be a list"
    delta_text = _delta_text_or_empty(messages, session.sent_count)
    if not delta_text.strip():
        return False  # nothing new to send — full call handles this step
    cmd = _command(model, session=session, resume=True)
    yielded = False
    try:
        for chunk in _run_one_stream(cmd, delta_text, timeout, session.cwd):
            yielded = True
            yield chunk
    except gateway.GatewayError as exc:
        if yielded:
            raise  # partial stream already reached the caller — no safe fallback
        log.warning("claudecli session resume failed (%s %s); falling back to stateless "
                    "for the rest of this turn", exc.status_code, exc.message)
        session.disable()
        return False
    session.sent_count = len(messages)
    return True


def _run_one_stream(cmd: list[str], prompt: str, timeout: float, cwd: str) -> Iterator[dict]:
    """Spawn the CLI once, feed ``prompt`` on stdin, yield deltas until 'result' or watchdog.

    Shared body of the fresh and --resume paths. Process hygiene: merged stderr,
    threaded stdin write, watchdog-driven SIGKILL of the whole group — see
    ``chat_stream`` for the why of each.
    """
    assert cmd and prompt, "cmd + prompt required"
    assert timeout > 0 and os.path.isdir(cwd), "positive timeout + existing cwd required"
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, cwd=cwd, env=_cli_env(),
                            start_new_session=(os.name == "posix"))
    timed_out = threading.Event()

    def _expire() -> None:
        timed_out.set()
        _kill_tree(proc)

    watchdog = threading.Timer(timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        yield from _consume_stream(proc, prompt, watchdog, timed_out)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            _kill_tree(proc)
        _reap(proc)


def _consume_stream(proc: subprocess.Popen, prompt: str, watchdog: threading.Timer,
                    timed_out: threading.Event) -> Iterator[dict]:
    """Read the child's stream, yielding OpenAI-shaped chunks; raise on failure."""
    assert proc.stdout is not None, "stdout pipe must exist"
    assert prompt, "prompt required"
    noise: deque[str] = deque(maxlen=_NOISE_KEEP)  # non-JSON lines (incl. merged stderr)
    saw_result = False
    _feed_stdin(proc, prompt)
    for count, line in enumerate(proc.stdout):  # unblocked by the watchdog's kill
        if count > _MAX_STREAM_LINES:
            raise gateway.GatewayError(502, "claude stream exceeded max output lines")
        event = _parse_event(line)
        if event is None:
            if line.strip():
                noise.append(line.strip())
            continue
        if event.get("type") == "rate_limit_event":
            _capture_rate_limit(event)
            continue
        if event.get("type") == "result":
            saw_result = True
            watchdog.cancel()  # the answer is complete — a late fire must not 504 it
            if event.get("is_error"):
                raise _fail(None, "claude reported an error: "
                            + str(event.get("result") or "")[:_OUTPUT_TAIL])
            yield {"delta": "", "tool_calls": None, "finish_reason": "stop",
                   "usage": _event_usage(event)}
            break
        text = _event_text_delta(event)
        if text:
            yield {"delta": text, "tool_calls": None, "finish_reason": None}
    _reap(proc)
    if timed_out.is_set():
        raise gateway.GatewayError(504, "Claude Code took too long to answer — try again.")
    if not saw_result:
        raise _fail(proc.returncode, "\n".join(noise))


def _kill_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (POSIX) so a CLI helper process can't
    hold the stdout pipe open past the kill — an EOF-less pipe would wedge the reader
    thread forever (2026-09 audit). Falls back to killing the direct child."""
    assert proc is not None, "process required"
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid via start_new_session
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # group already gone (or unusable) — direct kill below
    proc.kill()


def _reap(proc: subprocess.Popen) -> None:
    """Wait for the child (bounded) so no zombie outlives the request."""
    assert proc is not None, "process required"
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        _kill_tree(proc)  # wedged in exit — kill the tree; the next wait (or GC) reaps


def _event_usage(event: dict) -> dict | None:
    """Map the result event's token usage to the OpenAI shape (None when absent) —
    without it, Claude Code turns never appear in Usage & cost.

    ``cost_usd`` rides along when the CLI reports ``total_cost_usd``: the API-equivalent
    value of the call, priced by the CLI itself — no price table for us to let drift."""
    assert isinstance(event, dict), "event must be a dict"
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        prompt = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) \
            + int(usage.get("cache_read_input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None
    out = {"prompt_tokens": prompt, "completion_tokens": completion}
    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)) and cost >= 0:
        out["cost_usd"] = float(cost)
    return out


def chat(messages: list[dict], model: str, *, timeout: float = 300.0,
         tools_spec: list[dict] | None = None,
         session: TurnSession | None = None) -> dict:
    """One CLI call, returned OpenAI-shaped (``choices[0].message``) like ``gateway.chat``.

    With ``tools_spec`` the reply may be a fenced tool call in plain text —
    ``agent._extract_text_tool_calls`` recovers it, exactly as for local models.
    ``session`` (optional): turn-scoped continuity — see ``chat_stream`` and
    ``TurnSession``. Same fallback semantics.
    """
    assert messages and model, "messages + model required"
    parts: list[str] = []
    usage: dict | None = None
    for chunk in chat_stream(messages, model, timeout=timeout,
                             tools_spec=tools_spec, session=session):
        parts.append(chunk["delta"])
        usage = chunk.get("usage") or usage
    content = "".join(parts)
    out: dict = {"choices": [{"message": {"role": "assistant", "content": content},
                              "finish_reason": "stop"}]}
    if usage:
        out["usage"] = usage  # usage.record_response feeds Usage & cost from this
    return out


def _parse_event(line: str) -> dict | None:
    """Parse one stream-json line; None for blanks/noise (bounded by the caller)."""
    assert isinstance(line, str), "line must be a string"
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        event = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return event if isinstance(event, dict) else None


def _event_text_delta(event: dict) -> str:
    """Extract a visible text delta from a stream event ('' for thinking/other)."""
    assert isinstance(event, dict), "event must be a dict"
    if event.get("type") != "stream_event":
        return ""
    inner = event.get("event") or {}
    if inner.get("type") != "content_block_delta":
        return ""
    delta = inner.get("delta") or {}
    text = delta.get("text") if delta.get("type") == "text_delta" else ""
    return text if isinstance(text, str) else ""
