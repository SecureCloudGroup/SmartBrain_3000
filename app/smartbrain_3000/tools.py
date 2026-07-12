"""Assistant tool registry + the single execution chokepoint (H4).

Tools let the assistant take actions. The registry is STATIC and frozen at
import (NASA P10 #3 — no post-init mutation), mirroring db._MIGRATIONS. Each
tool declares a risk Tier; the tier is read ONLY from the registry server-side
(never supplied by the model/request). ``run`` is the one place a handler is
ever called: it validates args, re-derives the tier, and audits every attempt.

Credential firewall by construction: ToolContext carries only the unlocked
domain stores a tool needs — never the secret store, master key, or an HTTP
client — so no handler can reach a raw secret.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from . import gateway, ingest, netguard, search

log = logging.getLogger(__name__)


class Tier(str, Enum):
    """Risk tier. OBSERVE auto-runs; the others require user approval (H4b)."""

    OBSERVE = "observe"
    REVIEWED = "reviewed"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class ToolContext:
    """Unlocked capabilities a handler may use — never the raw secret store.

    Each entry encapsulates the credentials it needs (the domain stores hold the
    master key; ``email`` holds the OAuth token) and exposes action methods, so a
    handler acts on the user's behalf without being handed the secret store to
    enumerate every secret. This is the same posture as ``kb``/``planner``/
    ``memory`` already holding the master key: encapsulation here is by
    first-party-handler discipline (the static registry), not Python access
    control — a handler *could* reach a private attribute, so handlers must never
    return one. Defense-in-depth: ``redact`` masks secret-named keys in audit /
    tile / model output. ``email`` is None until a Gmail account is connected.
    """

    kb: object | None = None
    planner: object | None = None
    memory: object | None = None
    email: object | None = None
    schedules: object | None = None


@dataclass(frozen=True)
class Tool:
    """A declared tool: name, JSON-schema params, risk tier, handler, egress."""

    name: str
    description: str
    params_schema: dict
    tier: Tier
    handler: Callable[[ToolContext, dict], dict]
    egress: bool = False


_MAX_STR = 8000  # cap on any string arg
_MAX_ARGS = 32  # cap on number of args (bounds validation loop)
_SUMMARY_CAP = 2000  # cap on an audited summary string
_MAX_SCHEDULE_MINUTES = 525600  # one year — mirrors schedule_routes._MAX_INTERVAL (clamp, don't 500)
# Argument key names whose values are redacted before reaching the model / a
# tile / the audit body (defense-in-depth on top of the structural firewall).
_REDACT_KEYS = ("api_key", "apikey", "token", "password", "passphrase", "secret", "recovery_key", "authorization")


def _kb_search(ctx: ToolContext, args: dict) -> dict:
    """OBSERVE: search the knowledge base for documents matching the query.

    ALWAYS runs a keyword (lexical) scan over every stored document's content, so a quick
    search reaches ANY saved document — even one not yet in the semantic index (reindex is a
    trickle, or the doc predates configuring an embed model). When an embedding model is
    reachable it ALSO ranks by MEANING (cosine) and merges those hits in for extra recall.
    Embedding runs through the same internal gateway the chat turn uses (not external egress).
    Degrades to keyword-only and flags ``degraded`` when no embed model is reachable — never silent.
    """
    assert ctx.kb is not None, "knowledge base unavailable"
    assert args.get("query"), "query required"
    limit = min(max(int(args.get("limit", 5)), 1), 20)
    lexical = ctx.kb.search(args["query"], limit=limit)  # keyword scan of full content — reaches every doc
    model = gateway.embed_model(getattr(ctx.kb, "conn", None))
    try:
        vector = gateway.embed(args["query"], model)
    except Exception as exc:  # embed model unavailable — keyword-only, observably
        log.warning("kb_search: semantic unavailable, keyword-only: %s", exc)
        return {"results": lexical, "degraded": True}
    semantic = ctx.kb.semantic_search(vector, model, limit=limit)
    return {"results": _merge_hits(lexical, semantic, limit), "degraded": False}


def _merge_hits(primary: list[dict], extra: list[dict], limit: int) -> list[dict]:
    """Union two KB hit lists by doc id (``primary`` first), so meaning-based hits augment the
    keyword hits without hiding them. Keeps a keyword match the semantic index missed (an
    un-embedded doc) visible. Bounded by ``limit``."""
    seen = {h["id"] for h in primary}
    merged = list(primary)
    for h in extra:  # bounded by len(extra) <= limit
        if h["id"] not in seen:
            merged.append(h)
            seen.add(h["id"])
    return merged[:limit]


def _remember_fact(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: store a fact the assistant should remember."""
    assert ctx.memory is not None, "memory unavailable"
    assert args.get("text"), "text required"
    return {"id": ctx.memory.add_memory(args["text"])}


def _add_task(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: add a planner task (reversible). Forwards due_time/priority/recur too.

    Idempotent: a flaky local model sometimes emits add_task twice (in one step or by re-running
    it on a follow-up). If an OPEN task with the same title + due already exists, return THAT
    instead of creating a duplicate row.
    """
    assert ctx.planner is not None, "planner unavailable"
    title = args.get("title")
    assert title, "title required"
    due_date = args.get("due_date") or None
    due_time = args.get("due_time") or None
    norm = title.strip().lower()
    for t in ctx.planner.list_tasks():
        if (t["status"] != "done" and t["title"].strip().lower() == norm
                and t["due_date"] == due_date and t["due_time"] == due_time):
            return {"id": t["id"], "duplicate": True}  # no-op: identical open task already exists
    return {"id": ctx.planner.add_task(
        title, args.get("notes", ""), due_date,
        due_time=due_time,
        priority=args.get("priority", "medium"),
        recur=args.get("recur", "none"),
    )}


def _list_tasks(ctx: ToolContext, args: dict) -> dict:
    """OBSERVE: read-only list of the user's planner tasks (open first, by due date)."""
    assert ctx.planner is not None, "planner unavailable"
    assert isinstance(args, dict), "args must be a dict"
    return {"tasks": ctx.planner.list_tasks()}


def _complete_task(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: mark a task done (reversible; a recurring task rolls forward, stays open)."""
    assert ctx.planner is not None, "planner unavailable"
    assert args.get("task_id"), "task_id required"
    ctx.planner.set_status(args["task_id"], "done")
    return {"ok": True}


def _update_task(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: change fields of an existing task (reschedule, retitle, priority, recurrence)."""
    assert ctx.planner is not None, "planner unavailable"
    assert args.get("task_id"), "task_id required"
    existing = ctx.planner.get_task(args["task_id"])
    if existing is None:
        raise ValueError("task not found")
    ctx.planner.update_task(
        args["task_id"],
        args.get("title", existing["title"]),
        args.get("notes", existing["notes"]),
        args.get("due_date", existing["due_date"]),
        due_time=args.get("due_time", existing["due_time"]),
        priority=args.get("priority", existing["priority"]),
        recur=args.get("recur", existing["recur"]),
        tags=existing["tags"],  # tags aren't agent-editable (flat-scalar schema) — preserve them
    )
    return {"ok": True, "id": args["task_id"]}


def _email_list(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: list recent inbox messages (id, from, subject, date, snippet — no bodies)."""
    if ctx.email is None:
        raise ValueError("no email account connected")
    assert isinstance(args, dict), "args must be a dict"
    limit = min(max(int(args.get("limit", 10)), 1), 25)
    return {"messages": ctx.email.list_recent(max_results=limit)}


def _email_read(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: read one email's full body by id (ids come from email_list)."""
    if ctx.email is None:
        raise ValueError("no email account connected")
    assert args.get("message_id"), "message_id required"
    return ctx.email.read_message(args["message_id"])


def _web_fetch(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: fetch a public URL behind the SSRF guard (no store access)."""
    assert args.get("url"), "url required"
    return netguard.safe_fetch(args["url"])


def _kb_ingest_url(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: fetch a URL (SSRF-guarded), extract its text, add it to the knowledge base."""
    assert ctx.kb is not None, "knowledge base unavailable"
    assert args.get("url"), "url required"
    return ingest.ingest_url(ctx.kb, args["url"])


def _web_search(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: search the web (DuckDuckGo); returns result titles, URLs, snippets."""
    assert args.get("query"), "query required"
    limit = min(max(int(args.get("limit", 5)), 1), 10)
    return {"results": search.web_search(args["query"], limit)}


def _delete_task(ctx: ToolContext, args: dict) -> dict:
    """IRREVERSIBLE: permanently delete a planner task."""
    assert ctx.planner is not None, "planner unavailable"
    assert args.get("task_id"), "task_id required"
    ctx.planner.delete_task(args["task_id"])
    return {"ok": True}


def _email_send(ctx: ToolContext, args: dict) -> dict:
    """IRREVERSIBLE: send an email via the connected Gmail account (no creds returned)."""
    if ctx.email is None:
        raise ValueError("no email account connected")
    assert args.get("to") and "@" in args["to"], "a valid recipient is required"
    assert "subject" in args and "body" in args, "subject + body required"
    return ctx.email.send(args["to"], args["subject"], args["body"])


def _clamp_minutes(value: object) -> int:
    """Clamp a minutes arg into [0, one year] so a bad value can't 500 add/update_schedule."""
    return min(max(int(value), 0), _MAX_SCHEDULE_MINUTES)


def _list_schedules(ctx: ToolContext, args: dict) -> dict:
    """OBSERVE: read-only list of the user's recurring schedules (id/title/prompt/cadence/enabled)."""
    assert ctx.schedules is not None, "schedules unavailable"
    assert isinstance(args, dict), "args must be a dict"
    return {"schedules": ctx.schedules.list_schedules()}


def _read_schedule_output(ctx: ToolContext, args: dict) -> dict:
    """OBSERVE: recent scheduled-run output (newest first). ``schedule_id`` filters to one schedule."""
    assert ctx.schedules is not None, "schedules unavailable"
    limit = min(max(int(args.get("limit", 10)), 1), 50)
    sid = args.get("schedule_id")
    if sid:
        if ctx.schedules.get_schedule(sid) is None:
            raise ValueError("schedule not found")
        return {"runs": ctx.schedules.list_runs(sid, limit=limit)}
    return {"runs": ctx.schedules.recent_runs(limit)}


def _create_schedule(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: create a recurring schedule (reversible — can be disabled or deleted)."""
    assert ctx.schedules is not None, "schedules unavailable"
    title, prompt = args.get("title"), args.get("prompt")
    assert title and prompt, "title + prompt required"
    sid = ctx.schedules.add_schedule(
        title.strip(), prompt,
        _clamp_minutes(args.get("interval_minutes", 0)),
        _clamp_minutes(args.get("start_in_minutes", 0)),
        args.get("model") or None,
    )
    return {"id": sid}


def _update_schedule(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: edit an existing schedule by id; omitted fields are left unchanged."""
    assert ctx.schedules is not None, "schedules unavailable"
    sid = args.get("schedule_id")
    assert sid, "schedule_id required"
    existing = ctx.schedules.get_schedule(sid)
    if existing is None:
        raise ValueError("schedule not found")
    title = args.get("title", existing["title"]).strip()
    prompt = args.get("prompt", existing["prompt"])
    if not title or not prompt:  # clean ValueError, not the store's AssertionError (would 502)
        raise ValueError("title and prompt cannot be empty")
    interval = _clamp_minutes(args["interval_minutes"]) if "interval_minutes" in args else existing["interval_minutes"]
    model = args.get("model", existing.get("model")) or None  # explicit "" clears it
    ctx.schedules.update_schedule(sid, title, prompt, interval, model)
    return {"ok": True, "id": sid}


def _set_schedule_enabled(ctx: ToolContext, args: dict) -> dict:
    """REVIEWED: enable or disable a schedule by id (reversible)."""
    assert ctx.schedules is not None, "schedules unavailable"
    sid = args.get("schedule_id")
    assert sid, "schedule_id required"
    assert "enabled" in args, "enabled required"
    if ctx.schedules.get_schedule(sid) is None:
        raise ValueError("schedule not found")
    ctx.schedules.set_enabled(sid, bool(args["enabled"]))
    return {"ok": True, "id": sid, "enabled": bool(args["enabled"])}


def _delete_schedule(ctx: ToolContext, args: dict) -> dict:
    """IRREVERSIBLE: permanently delete a schedule and its run history."""
    assert ctx.schedules is not None, "schedules unavailable"
    sid = args.get("schedule_id")
    assert sid, "schedule_id required"
    ctx.schedules.delete_schedule(sid)
    return {"ok": True}


_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="kb_search",
        description="Search the user's saved documents (knowledge base) by keyword AND meaning; finds "
                    "any stored document whose title or content matches. Use to look up, quote, or "
                    "answer from something the user saved.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        tier=Tier.OBSERVE,
        handler=_kb_search,
        egress=False,
    ),
    Tool(
        name="remember_fact",
        description="Remember a fact about the user (used to ground future chats).",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        tier=Tier.REVIEWED,
        handler=_remember_fact,
        egress=False,
    ),
    Tool(
        name="add_task",
        description="Add a task to the planner. due_date is YYYY-MM-DD, due_time is HH:MM "
                    "(both optional); priority is low/medium/high; recur is none/daily/weekly.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "due_date": {"type": "string"},
                "due_time": {"type": "string"},
                "priority": {"type": "string"},
                "recur": {"type": "string"},
            },
            "required": ["title"],
        },
        tier=Tier.REVIEWED,
        handler=_add_task,
        egress=False,
    ),
    Tool(
        name="list_tasks",
        description="List the user's planner tasks (open first, by due date, each with "
                    "id/due_date/status/priority). Use THIS to answer what tasks or to-dos "
                    "exist or what is due — NOT kb_search, which searches saved documents.",
        params_schema={"type": "object", "additionalProperties": False, "properties": {}},
        tier=Tier.OBSERVE,
        handler=_list_tasks,
        egress=False,
    ),
    Tool(
        name="complete_task",
        description="Mark a planner task done by its id (from list_tasks). Reversible; a "
                    "recurring task rolls forward to its next date. Use this to COMPLETE a "
                    "task — never delete_task, which permanently removes it.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        tier=Tier.REVIEWED,
        handler=_complete_task,
        egress=False,
    ),
    Tool(
        name="update_task",
        description="Change fields of an existing task by id (from list_tasks): reschedule "
                    "(due_date YYYY-MM-DD / due_time HH:MM), retitle, edit notes, set "
                    "priority (low/medium/high) or recur (none/daily/weekly). Omitted "
                    "fields are left unchanged.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "due_date": {"type": "string"},
                "due_time": {"type": "string"},
                "priority": {"type": "string"},
                "recur": {"type": "string"},
            },
            "required": ["task_id"],
        },
        tier=Tier.REVIEWED,
        handler=_update_task,
        egress=False,
    ),
    Tool(
        name="web_search",
        description="Search the web and return result titles, URLs, and snippets. Use to find current information or the right page before fetching it.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
        tier=Tier.REVIEWED,
        handler=_web_search,
        egress=True,
    ),
    Tool(
        name="web_fetch",
        description="Fetch a public web page or JSON URL (returns truncated text).",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        tier=Tier.REVIEWED,
        handler=_web_fetch,
        egress=True,
    ),
    Tool(
        name="kb_ingest_url",
        description="Add a web page or PDF to the user's knowledge base by URL (fetches, extracts the text, and saves it). Use when the user asks to add/save a link or PDF to their knowledge.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        tier=Tier.REVIEWED,
        handler=_kb_ingest_url,
        egress=True,
    ),
    Tool(
        name="delete_task",
        description="Permanently delete a planner task by id. Cannot be undone.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        tier=Tier.IRREVERSIBLE,
        handler=_delete_task,
        egress=False,
    ),
    Tool(
        name="email_send",
        description="Send a plain-text email from the user's connected Gmail account.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
        tier=Tier.IRREVERSIBLE,
        handler=_email_send,
        egress=True,
    ),
    Tool(
        name="email_list",
        description="List recent inbox emails (id, from, subject, date, snippet). Use to "
                    "triage or summarize the inbox, or to find a message id before email_read.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer"}},
        },
        tier=Tier.REVIEWED,
        handler=_email_list,
        egress=True,
    ),
    Tool(
        name="email_read",
        description="Read one email's full body by its id (from email_list). Use to read or "
                    "summarize a specific message before replying.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        tier=Tier.REVIEWED,
        handler=_email_read,
        egress=True,
    ),
    Tool(
        name="list_schedules",
        description="List the user's recurring SCHEDULES (automated tasks that run on a timer), each "
                    "with id/title/prompt/interval_minutes/enabled/next_run. Use THIS for questions about "
                    "scheduled or recurring/automated items — not list_tasks (one-off to-dos) or kb_search.",
        params_schema={"type": "object", "additionalProperties": False, "properties": {}},
        tier=Tier.OBSERVE,
        handler=_list_schedules,
        egress=False,
    ),
    Tool(
        name="read_schedule_output",
        description="Read recent OUTPUT from scheduled runs (what the schedules produced), newest first. "
                    "Pass schedule_id (from list_schedules) to see one schedule's runs, or omit it for the "
                    "combined feed across all schedules. Use to answer 'what did my schedules find/report'.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"schedule_id": {"type": "string"}, "limit": {"type": "integer"}},
        },
        tier=Tier.OBSERVE,
        handler=_read_schedule_output,
        egress=False,
    ),
    Tool(
        name="create_schedule",
        description="Create a recurring SCHEDULE that runs a prompt on a timer. interval_minutes is the "
                    "cadence (0 = run once); start_in_minutes delays the first run (0 = next tick); model is "
                    "optional. Reversible (can be disabled or deleted). Use for 'every morning…' / 'each week…'.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "prompt": {"type": "string"},
                "interval_minutes": {"type": "integer"},
                "start_in_minutes": {"type": "integer"},
                "model": {"type": "string"},
            },
            "required": ["title", "prompt"],
        },
        tier=Tier.REVIEWED,
        handler=_create_schedule,
        egress=False,
    ),
    Tool(
        name="update_schedule",
        description="Edit an existing schedule by id (from list_schedules): change title, prompt, "
                    "interval_minutes, or model. Omitted fields are left unchanged. Use to retitle, reword, "
                    "or change how often a schedule runs — not to enable/disable it (use set_schedule_enabled).",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "schedule_id": {"type": "string"},
                "title": {"type": "string"},
                "prompt": {"type": "string"},
                "interval_minutes": {"type": "integer"},
                "model": {"type": "string"},
            },
            "required": ["schedule_id"],
        },
        tier=Tier.REVIEWED,
        handler=_update_schedule,
        egress=False,
    ),
    Tool(
        name="set_schedule_enabled",
        description="Enable or disable a schedule by id (from list_schedules). Set enabled=false to PAUSE it "
                    "(keeps it, stops it running) or enabled=true to resume. Reversible. Prefer this over "
                    "delete_schedule when the user wants to pause/stop rather than permanently remove.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"schedule_id": {"type": "string"}, "enabled": {"type": "boolean"}},
            "required": ["schedule_id", "enabled"],
        },
        tier=Tier.REVIEWED,
        handler=_set_schedule_enabled,
        egress=False,
    ),
    Tool(
        name="delete_schedule",
        description="Permanently delete a schedule (and its run history) by id. Cannot be undone. To just "
                    "pause a schedule, use set_schedule_enabled with enabled=false instead.",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"schedule_id": {"type": "string"}},
            "required": ["schedule_id"],
        },
        tier=Tier.IRREVERSIBLE,
        handler=_delete_schedule,
        egress=False,
    ),
)

# OBSERVE tools must be read-only + no egress; this allowlist is the structural
# safety invariant checked at import.
_OBSERVE_READONLY = frozenset({"kb_search", "list_tasks", "list_schedules", "read_schedule_output"})

# REVIEWED tools that MUTATE schedules. A schedule creates/rewrites/re-enables an autonomous
# agent turn, so these must NEVER auto-run (via remembered consent) inside a schedule-executed
# turn — that would let an injected background prompt spawn self-perpetuating schedules with no
# human at the tile. The scheduler strips these from its auto_approve set so they always park.
# (delete_schedule is IRREVERSIBLE and already always parks, so it isn't needed here.)
SCHEDULE_WRITE_TOOLS = frozenset({"create_schedule", "update_schedule", "set_schedule_enabled"})


def _build_registry(tools: tuple[Tool, ...]) -> dict[str, Tool]:
    """Build + validate the registry once at import (fail loud on a bad tool)."""
    assert tools, "at least one tool required"
    registry: dict[str, Tool] = {}
    for tool in tools:  # bounded by the fixed _TOOLS tuple
        assert tool.name.isidentifier() and tool.name.islower(), "tool name must be snake_case"
        assert tool.name not in registry, "duplicate tool name"
        assert isinstance(tool.tier, Tier), "tier must be a Tier"
        schema = tool.params_schema
        assert schema.get("type") == "object" and schema.get("additionalProperties") is False, "schema must be a closed object"
        if tool.tier is Tier.OBSERVE:
            assert tool.egress is False and tool.name in _OBSERVE_READONLY, "OBSERVE tools must be read-only + no egress"
        if tool.egress:
            assert tool.tier is not Tier.OBSERVE, "egress tools cannot be OBSERVE"
        registry[tool.name] = tool
    return registry


REGISTRY: dict[str, Tool] = _build_registry(_TOOLS)


def get_tool(name: str) -> Tool | None:
    """Return the registered tool, or None for an unknown name."""
    assert isinstance(name, str), "name must be a string"
    return REGISTRY.get(name)


def openai_tools_spec() -> list[dict]:
    """Project the registry to the OpenAI tools schema for the gateway."""
    assert REGISTRY, "registry must be non-empty"
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.params_schema}}
        for t in REGISTRY.values()
    ]


def _coerce_scalar(expected: str, value: object) -> object:
    """Coerce a stringified scalar to its schema type, else return it unchanged.

    Models (esp. local ones) routinely emit numeric/boolean args as strings —
    ``"limit": "3"``. Only clean, unambiguous strings convert; genuine garbage
    falls through to ``_type_ok`` below and is still rejected.
    """
    assert isinstance(expected, str), "expected type name required"
    if not isinstance(value, str):
        return value
    s = value.strip()
    if expected == "integer":
        body = s[1:] if s[:1] in "+-" else s
        return int(s) if body.isdigit() else value
    if expected == "number":
        try:
            return float(s)
        except ValueError:
            return value
    if expected == "boolean" and s.lower() in ("true", "false"):
        return s.lower() == "true"
    return value


def _type_ok(expected: str, value: object) -> bool:
    """Scalar type check (bool is NOT an int/number here)."""
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return False


def validate_args(tool: Tool, args: dict) -> dict:
    """Validate args against the tool's flat-scalar schema; raise ValueError.

    The load-bearing gate between an untrusted model and a real handler.
    """
    assert isinstance(tool, Tool), "tool required"
    assert isinstance(args, dict), "args must be a dict"
    if len(args) > _MAX_ARGS:
        raise ValueError("too many arguments")
    props = tool.params_schema["properties"]
    for key in tool.params_schema.get("required", []):  # bounded by schema
        if key not in args:
            raise ValueError(f"missing required argument: {key}")
    out: dict = {}
    for key, value in args.items():  # bounded by _MAX_ARGS
        if key not in props:
            raise ValueError(f"unknown argument: {key}")
        value = _coerce_scalar(props[key]["type"], value)  # tolerate stringified ints/bools
        if not _type_ok(props[key]["type"], value):
            raise ValueError(f"argument '{key}' must be {props[key]['type']}")
        if isinstance(value, str) and len(value) > _MAX_STR:
            raise ValueError(f"argument '{key}' too long")
        out[key] = value
    return out


def redact(obj: object) -> object:
    """Return a copy with values of secret-ish keys replaced (defense-in-depth)."""
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in _REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj[:_MAX_ARGS]]
    return obj


def summarize(value: object) -> str:
    """JSON-stringify (redacted) and cap length for the audit body."""
    text = value if isinstance(value, str) else json.dumps(redact(value), default=str)
    return text[:_SUMMARY_CAP]


class _StandingClaim:
    """A standing pre-authorization (remembered consent). Valid ONLY for REVIEWED tools.

    The agent passes ``GRANTED`` for a write the user chose to remember. ``run``
    refuses it for IRREVERSIBLE tools, so the irreversible-always-asks invariant
    lives at the chokepoint itself, not in caller discipline.
    """

    def __call__(self) -> bool:
        return True


GRANTED = _StandingClaim()  # the agent's claim for a remembered REVIEWED write


def run(
    ctx: ToolContext,
    audit,
    tool_name: str,
    args: dict,
    *,
    actor: str,
    conversation_id: str | None = None,
    claim: Callable[[], bool] | None = None,
) -> dict:
    """The ONLY path that calls a tool handler. Tier-gated + always audited.

    OBSERVE auto-runs. A non-OBSERVE tool requires a single-use ``claim()``
    (the approval gateway's approved->executed CAS); if it returns False the
    approval was already consumed/revoked and the handler does NOT run. A
    ``GRANTED`` standing claim is accepted for REVIEWED writes (remembered
    consent) but REFUSED for IRREVERSIBLE tools — those always need a per-action
    approval. Every execution attempt — success or handler raise — writes exactly
    one audit row.
    """
    assert audit is not None, "audit log required (locked?)"
    tool = get_tool(tool_name)
    assert tool is not None, "unknown tool"
    tier = tool.tier  # authority is the static registry, never the caller
    if tier is not Tier.OBSERVE:
        assert claim is not None, "non-OBSERVE tools require an approval claim"
        if tier is Tier.IRREVERSIBLE and isinstance(claim, _StandingClaim):
            audit.append(actor, tool_name, tier.value, "errored", False, conversation_id=conversation_id, args_summary=summarize(args), error="irreversible tools require per-action approval")
            raise PermissionError(f"{tool_name}: irreversible tools cannot use a standing claim")
    try:
        validated = validate_args(tool, args)
    except ValueError as exc:  # audit the reject too (no path is unaudited)
        audit.append(actor, tool_name, tier.value, "errored", False, conversation_id=conversation_id, args_summary=summarize(args), error=f"invalid args: {exc}")
        raise
    if tier is not Tier.OBSERVE and not claim():
        audit.append(actor, tool_name, tier.value, "errored", False, conversation_id=conversation_id, args_summary=summarize(validated), error="approval not claimable")
        raise PermissionError(f"{tool_name}: approval not claimable")
    decision = "auto" if tier is Tier.OBSERVE else "executed"
    try:
        result = tool.handler(ctx, validated)
        audit.append(
            actor, tool_name, tier.value, decision, True,
            conversation_id=conversation_id, args_summary=summarize(validated), result_summary=summarize(result),
        )
        return result
    except Exception as exc:
        audit.append(
            actor, tool_name, tier.value, "errored", False,
            conversation_id=conversation_id, args_summary=summarize(validated), error=str(exc),
        )
        raise
