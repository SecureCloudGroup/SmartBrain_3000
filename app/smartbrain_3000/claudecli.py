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


def _command(model: str) -> list[str]:
    """Build the headless CLI command: empty-toolset agent, no persistence, no settings.

    Argv carries NOTHING conversation-derived — only static flags and the static
    agent definition (argv is world-readable via ``ps``). The real content goes
    over stdin. ``--setting-sources ""`` loads no user/project settings: verified
    live that without it the CLI injects the user's global CLAUDE.md. (The CLI
    still tells the model the current date and the signed-in account's own email —
    benign, documented in docs/02-models.md.)
    """
    assert is_claudecode(model), "model must be claudecode/<alias>"
    if not _enabled:
        raise gateway.GatewayError(403, "Claude Code isn't connected — enable it under "
                                        "Settings → Local models first.")
    path = binary_path()
    if not path:
        raise gateway.GatewayError(503, "Claude Code is not installed (Settings → Local models).")
    agents = json.dumps({_AGENT_NAME: {
        "description": "SmartBrain chat backend", "prompt": _AGENT_PROMPT, "tools": []}})
    return [path, "-p", "--verbose", "--output-format", "stream-json",
            "--include-partial-messages", "--no-session-persistence",
            "--setting-sources", "", "--strict-mcp-config",
            "--model", model.split("/", 1)[1],
            "--agents", agents, "--agent", _AGENT_NAME]


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
                tools_spec: list[dict] | None = None) -> Iterator[dict]:
    """Stream deltas from one CLI turn — same item shape as ``gateway.chat_stream``.

    Tool offers ride the text protocol (see ``_tool_instructions``); a fenced tool
    call streams as text and the agent stream path's suppress-and-recover logic
    handles it, so ``tool_calls`` here is always None.

    Process hygiene: stderr is merged into stdout (a separate unread pipe can fill
    and wedge the child), stdin is fed from a helper thread, and a watchdog timer
    kills the child at the deadline — a blocking readline alone would never notice
    a hung CLI.
    """
    assert messages and model, "messages + model required"
    assert timeout > 0, "timeout must be positive"
    prompt = _flatten(messages, tools_spec)
    proc = subprocess.Popen(_command(model), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=_private_cwd(), env=_cli_env(),
                            start_new_session=(os.name == "posix"))
    timed_out = threading.Event()

    def _expire() -> None:
        timed_out.set()
        _kill_tree(proc)

    watchdog = threading.Timer(timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    noise: deque[str] = deque(maxlen=_NOISE_KEEP)  # non-JSON lines (incl. merged stderr)
    saw_result = False
    try:
        assert proc.stdout is not None, "stdout pipe must exist"
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
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            _kill_tree(proc)
        _reap(proc)


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
         tools_spec: list[dict] | None = None) -> dict:
    """One CLI turn, returned OpenAI-shaped (``choices[0].message``) like ``gateway.chat``.

    With ``tools_spec`` the reply may be a fenced tool call in plain text —
    ``agent._extract_text_tool_calls`` recovers it, exactly as for local models.
    """
    assert messages and model, "messages + model required"
    parts: list[str] = []
    usage: dict | None = None
    for chunk in chat_stream(messages, model, timeout=timeout, tools_spec=tools_spec):
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
