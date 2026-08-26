"""Tools / approvals / audit HTTP API (requires unlock).

OBSERVE tools run inline (audited). REVIEWED / IRREVERSIBLE tools are PARKED as
pending approvals and only run after the user approves (the agentic loop is H4c).
Approval is the single gate: the route CASes pending->approved, then the
executor claims approved->executed (single-use) before the handler runs, so a
dangerous tool can run at most once and never without approval.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Iterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from . import (
    agent,
    consent,
    docsummaries,
    gateway,
    metrics,
    optimizer,
    scheduler,
    search,
    tools,
    usage,
)
from .chat_routes import _with_memory

router = APIRouter()
log = logging.getLogger(__name__)

_STREAM_DELTA_BUDGET = 20000  # max delta chunks forwarded per SSE response (P10 #2)
# Interactive turns get a longer per-request budget than the old 60s: a big LOCAL model (e.g. MLX
# gemma-4 26B) can take well over a minute to cold-load + generate a detailed answer. Cutting it
# short made the app abandon the request while the model kept running, so a retry then collided
# with it ("model is busy…"). Cloud models answer in seconds and never approach this ceiling.
_INTERACTIVE_TIMEOUT = 180.0


class InvokeIn(BaseModel):
    name: str = Field(min_length=1)
    args: dict = Field(default_factory=dict)
    conversation_id: str | None = None


class ApproveIn(BaseModel):
    confirm_tool: str | None = None  # required to equal the tool name for IRREVERSIBLE
    remember: bool = False  # remember consent for this (REVIEWED) tool; ignored for IRREVERSIBLE


class TurnIn(BaseModel):
    # One-time token from a stream's "pending" frame: lets this request REUSE the first
    # model response the stream already paid for instead of asking again. Absent/stale
    # simply means the model is asked, exactly as before.
    primed: str | None = None
    messages: list[dict] = Field(min_length=1)
    model: str | None = None
    capability: str = "chat"
    conversation_id: str | None = None
    # Spoken replies: the client asks for a length ("short" | "medium" | "long"). Applied as
    # a TRAILING system note (the optimizer-guidance slot) so the static prompt head and the
    # conversation prefix stay byte-stable for the local model's prefix cache.
    reply_length: str | None = None


_REPLY_LENGTH_NOTES = {
    "short": "This reply will be read aloud. Answer in one to three sentences, plainly, "
             "with no headings or lists; offer more detail only if the user asks for it.",
    "medium": "This reply will be read aloud. Keep it to a short paragraph or two, plainly, "
              "with no headings or lists.",
}


def reply_length_note(value: str | None) -> dict | None:
    """The trailing system note for a requested spoken-reply length; None = no constraint
    ("long", unset, or an unknown value — never a 4xx over a preference)."""
    text = _REPLY_LENGTH_NOTES.get((value or "").strip().lower())
    return {"role": "system", "content": text} if text else None


def _context(request: Request) -> tuple[tools.ToolContext, object]:
    """Return (ToolContext, audit) for the unlocked app, or raise 423."""
    audit = getattr(request.app.state, "audit", None)
    if audit is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    state = request.app.state
    # The user is HERE: background model work (the summary-tree builder) must stand
    # aside — oMLX serves one request at a time, and a 30s map call in flight when a
    # chat arrives reads as "SmartBrain hung" (seen live).
    state.last_interactive = time.monotonic()
    secret_store = getattr(state, "secret_store", None)
    master_key = getattr(state, "master_key", None)
    return tools.ToolContext(
        kb=state.kb, planner=state.planner, memory=state.memory,
        email=getattr(state, "email", None), schedules=getattr(state, "schedules", None),
        vaults=getattr(state, "vaults", None),  # so KB tools can tag imported-vault content
        # Provider keys stay inside the service (ctx.email posture) — resolved here, once.
        websearch=search.service_from(state.dbx, secret_store.get) if secret_store else None,
        summaries=docsummaries.SummaryStore(state.dbx, master_key) if master_key else None,
    ), audit


def _approvals(request: Request):
    """Return the unlocked ApprovalStore, or raise 423."""
    approvals = getattr(request.app.state, "approvals", None)
    if approvals is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return approvals


@router.get("/api/tools")
def list_tools(request: Request) -> dict:
    """List the available tools (name, description, tier)."""
    _context(request)
    return {"tools": [{"name": t.name, "description": t.description, "tier": t.tier.value} for t in tools.REGISTRY.values()]}


@router.post("/api/tools/invoke")
def invoke_tool(request: Request, body: InvokeIn) -> dict:
    """Run an OBSERVE tool inline; park a REVIEWED/IRREVERSIBLE tool for approval."""
    ctx, audit = _context(request)
    tool = tools.get_tool(body.name)
    if tool is None:
        raise HTTPException(status_code=404, detail="unknown tool")
    if tool.tier is tools.Tier.OBSERVE:
        # summarize_document/read_document size to the model's context — give the direct-invoke path the
        # chat model so ctx.model is set (the agent loop sets it per turn; here there's no turn model).
        chat_model = gateway.resolve_model("chat", gateway.load_routes(request.app.state.dbx))
        ctx = dataclasses.replace(ctx, model=chat_model)
        try:
            return {"status": "done", "result": tools.run(ctx, audit, body.name, body.args, actor="user", conversation_id=body.conversation_id)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"tool failed: {exc}") from None
    # Dangerous: validate then park for approval (never runs here).
    approvals = _approvals(request)
    try:
        validated = tools.validate_args(tool, body.args)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    pid = approvals.create_pending(body.name, tool.tier.value, validated, conversation_id=body.conversation_id)
    audit.append("user", body.name, tool.tier.value, "proposed", True, conversation_id=body.conversation_id, args_summary=tools.summarize(validated))
    return {"status": "awaiting_approval", "pending_id": pid, "tool": body.name, "tier": tool.tier.value}


def _pending_tile(p: dict) -> dict:
    """Shape a pending row for the UI tile: redacted args + Always-allow hints.

    ``remember_mode`` tells the UI which consent shape applies — ``"tool"`` for
    whole-tool consent, ``"site"`` for the URL tools (per-host), None when consent
    refuses to remember at all. ``remember_host`` carries the parsed host of the
    pending URL so the UI can label the button "Always allow <host>". Site-mode
    with an unparseable URL surfaces as non-rememberable so no button appears.
    """
    tool = p["tool"]
    args = p["args"] if isinstance(p["args"], dict) else {}
    mode = consent.remember_mode(tool)
    host: str | None = None
    if mode == "site":
        url = args.get("url")
        host = consent.host_from_url(url) if isinstance(url, str) else None
    rememberable = mode == "tool" or (mode == "site" and host is not None)
    return {
        "id": p["id"], "tool": tool, "tier": p["tier"], "created_at": p["created_at"],
        "turn_id": p.get("turn_id"), "conversation_id": p.get("conversation_id"),
        "args": tools.redact(args),
        # rememberable stays true for either mode: the phone bundle reads only this
        # flag to decide whether to show the "Always allow" button; the newer web UI
        # additionally reads remember_mode/remember_host to label it precisely.
        "rememberable": rememberable,
        "remember_mode": mode,
        "remember_host": host,
    }


@router.get("/api/agent/pending")
def list_pending(request: Request) -> dict:
    """List actions awaiting approval (args redacted for the tile)."""
    approvals = _approvals(request)
    return {"pending": [_pending_tile(p) for p in approvals.list_pending()]}


# Idempotency guard for the scheduled auto-resume (issue: the user approves the
# LAST of two same-turn pendings while a background resume for the first is still
# in flight — both would try to drive the same tail). A turn_id enters the set
# when its resume starts and leaves when it returns; a second attempt for a
# turn_id already in the set is a no-op.
_RESUME_LOCK = threading.Lock()
_RESUMING_TURNS: set[str] = set()


def _all_pending_resolved(approvals, turn_id: str) -> bool:
    """True iff no pending/approved rows remain for the turn — safe to auto-resume."""
    assert approvals is not None and turn_id, "approvals + turn id required"
    rows = approvals.list_for_turn(turn_id)
    assert isinstance(rows, list), "list_for_turn must return a list"
    return bool(rows) and not any(r["status"] in ("pending", "approved") for r in rows)


def _scheduled_origin(row: dict) -> dict | None:
    """Return the scheduled-origin marker from a pending row, or None if not scheduled.

    Chat turns (owned by the chat page's resume flow) return None so this endpoint
    never double-drives them. Only the marker put in by ``scheduler.run_schedule``
    counts — a missing turn_state or a missing/mismatched kind is treated as not
    scheduled, so an old parked turn without an origin cannot trigger auto-resume.
    """
    assert isinstance(row, dict), "row must be a dict"
    state = row.get("turn_state") if isinstance(row.get("turn_state"), dict) else None
    origin = state.get("origin") if state else None
    if not isinstance(origin, dict):
        return None
    return origin if origin.get("kind") == "scheduled" and origin.get("schedule_id") else None


def _start_resume_thread(target, args, name) -> None:
    """Spawn the auto-resume worker on a daemon thread. Isolated + module-level so
    tests can monkeypatch it and run the worker synchronously without a thread race."""
    assert callable(target) and name, "target callable + name required"
    threading.Thread(target=target, args=args, name=name, daemon=True).start()


def _launch_scheduled_resume(request: Request, turn_id: str, schedule_id: str) -> bool:
    """Claim + spawn the scheduled auto-resume; returns False if already in flight."""
    assert turn_id and schedule_id, "turn + schedule id required"
    assert request is not None, "request required"
    with _RESUME_LOCK:
        if turn_id in _RESUMING_TURNS:
            return False  # another approval already kicked off the resume
        _RESUMING_TURNS.add(turn_id)
    ctx, audit = _context(request)
    approvals = _approvals(request)
    state = request.app.state
    schedules = getattr(state, "schedules", None)
    conn = state.dbx  # ThreadLocalConn: safe from the worker thread (per-thread cursors)
    _start_resume_thread(
        _resume_scheduled_worker,
        (ctx, audit, approvals, conn, schedules, turn_id, schedule_id),
        f"sched-resume-{turn_id[:8]}",
    )
    return True


def _resume_scheduled_worker(ctx, audit, approvals, conn, schedules, turn_id: str, schedule_id: str) -> None:
    """Background: resume a scheduled turn and record its final outcome. Never raises.

    Guards mirror ``scheduler.run_schedule``: schedule-writing tools are stripped
    from ``auto_approve`` (an injected prompt must not self-modify schedules mid-
    resume) and the timeout matches the tick's per-turn budget so a cold-loading
    local model has room to finish. A re-park (status ``awaiting_approval``) is
    NOT recorded as a new run — the next approval cycle will resume again and
    the terminal outcome ends up in the feed.
    """
    assert turn_id and schedule_id, "turn + schedule id required"
    assert approvals is not None and audit is not None, "unlocked stores required"

    def sink(used_model: str, response: object) -> None:
        usage.record_response(conn, used_model, response)

    try:
        auto_approve = consent.remembered(conn) - tools.SCHEDULE_WRITE_TOOLS
        result = agent.resume_turn(
            ctx, audit, approvals, turn_id, conn=conn, usage_sink=sink,
            auto_approve=auto_approve, timeout=scheduler._AGENT_TURN_TIMEOUT,
        )
    except Exception as exc:
        log.warning("scheduled resume for %s failed: %s", turn_id, exc)
        if schedules is not None:
            try:
                schedules.record_run(schedule_id, "error", message="", error=str(exc))
            except Exception as rec_exc:
                log.warning("failed to record resumed scheduled run: %s", rec_exc)
        with _RESUME_LOCK:
            _RESUMING_TURNS.discard(turn_id)
        return
    with _RESUME_LOCK:
        _RESUMING_TURNS.discard(turn_id)
    if result is None:
        log.warning("scheduled resume for %s found no parked state", turn_id)
        return
    status = str(result.get("status") or "complete")
    if status == "awaiting_approval":
        return  # re-parked — the next approval cycle carries the outcome
    if schedules is None:
        return
    try:
        schedules.record_run(schedule_id, status, message=str(result.get("message", "")))
    except Exception as exc:
        log.warning("failed to record resumed scheduled run: %s", exc)


@router.post("/api/agent/pending/{pid}/approve")
def approve(request: Request, pid: str, body: ApproveIn) -> dict:
    """Approve + execute a pending action (audited). IRREVERSIBLE needs confirm_tool.

    When the resolved pending was the last unresolved action of a SCHEDULED turn,
    the turn's tail is resumed on a background thread and its final answer lands
    in the Scheduled updates feed — no more scheduled runs left dangling on
    "Awaiting your approval". Chat parks (owned by the chat page's resume flow)
    are untouched.
    """
    ctx, audit = _context(request)
    approvals = _approvals(request)
    row = approvals.get(pid)
    if row is None:
        raise HTTPException(status_code=404, detail="pending action not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"already {row['status']}")
    if row["expired"]:
        raise HTTPException(status_code=409, detail="approval expired")
    tool = tools.get_tool(row["tool_name"])
    if tool is None:
        raise HTTPException(status_code=409, detail="tool no longer available")
    if tool.tier is tools.Tier.IRREVERSIBLE and body.confirm_tool != row["tool_name"]:
        raise HTTPException(status_code=409, detail="irreversible action requires confirm_tool")
    if not approvals.approve(pid):
        raise HTTPException(status_code=409, detail="could not approve (resolved or expired)")
    audit.append("user", row["tool_name"], row["tier"], "approved", True, args_summary=tools.summarize(row["args"]))
    if body.remember:  # remember consent (no-op for IRREVERSIBLE — those always re-ask)
        # Site-mode tools (web_fetch, kb_ingest_url) remember one HOST, not the whole
        # tool: the URL the model composed for this pending call becomes the allowed
        # destination; a future call to a DIFFERENT host still parks for approval.
        row_args = row["args"] if isinstance(row["args"], dict) else {}
        if consent.remember_mode(row["tool_name"]) == "site":
            consent.remember_site(request.app.state.dbx, row["tool_name"], row_args.get("url", ""))
        else:
            consent.remember(request.app.state.dbx, row["tool_name"])
    try:
        result = tools.run(ctx, audit, row["tool_name"], row["args"], actor="user", claim=lambda: approvals.claim(pid))
    except PermissionError:
        raise HTTPException(status_code=409, detail="approval already consumed") from None
    except Exception as exc:
        # The claim was consumed (status=executed) but the handler failed — store
        # the error so a parked agent turn resumes with the truth, not a success.
        approvals.store_result(pid, {"error": str(exc)})
        raise HTTPException(status_code=502, detail=f"tool failed: {exc}") from None
    approvals.store_result(pid, result)  # so a parked agent turn can resume with it
    resumed = _maybe_finish_scheduled_turn(request, row)
    return {"status": "executed", "result": result, "resumed_turn": resumed}


@router.post("/api/agent/pending/{pid}/deny")
def deny(request: Request, pid: str) -> dict[str, bool]:
    """Deny a pending action (audited; never executes).

    A deny that resolves the LAST pending of a scheduled turn still auto-resumes
    the turn so its "couldn't do X" answer lands in the Scheduled updates feed
    instead of the turn dangling forever. Chat parks are left to the chat page.
    The response shape is unchanged (the auto-resume is a side effect).
    """
    ctx, audit = _context(request)
    approvals = _approvals(request)
    row = approvals.get(pid)
    if row is None:
        raise HTTPException(status_code=404, detail="pending action not found")
    if not approvals.deny(pid):
        raise HTTPException(status_code=409, detail=f"already {row['status']}")
    audit.append("user", row["tool_name"], row["tier"], "denied", True, args_summary=tools.summarize(row["args"]))
    _maybe_finish_scheduled_turn(request, row)  # side effect: scheduled turn tail finishes into Info
    return {"ok": True}


def _maybe_finish_scheduled_turn(request: Request, row: dict) -> bool:
    """Kick off a scheduled auto-resume when this resolution finished the turn.

    ``row`` is the pending fetched BEFORE resolution — its ``turn_state.origin``
    tells us whether the turn came from ``scheduler.run_schedule``, and its
    ``turn_id`` links every sibling pending in the same turn. No-op for chat
    parks (origin None) or when other pendings still block the resume.
    """
    assert request is not None and isinstance(row, dict), "request + row required"
    origin = _scheduled_origin(row)
    if origin is None:
        return False
    schedule_id = origin.get("schedule_id")
    turn_id = row.get("turn_id")
    if not isinstance(schedule_id, str) or not isinstance(turn_id, str):
        return False  # a scheduled park always carries both; missing means malformed state
    approvals = _approvals(request)
    if not _all_pending_resolved(approvals, turn_id):
        return False
    return _launch_scheduled_resume(request, turn_id, schedule_id)


@router.get("/api/agent/remembered")
def list_remembered(request: Request) -> dict:
    """Auto-approved consent: whole-tool ``tools`` + per-host ``sites``.

    ``tools`` stays a list of names (the phone bundle reads it that way); ``sites``
    carries the URL tools' remembered hosts as ``{"tool", "host"}`` records so the
    UI can list them and DELETE any one entry via the same route.
    """
    _approvals(request)  # unlocked gate
    entries = consent.remembered(request.app.state.dbx)
    tool_names: list[str] = []
    sites: list[dict[str, str]] = []
    for entry in entries:  # bounded by the meta row's size
        head, sep, host = entry.partition("@")
        if sep and host:
            sites.append({"tool": head, "host": host})
        else:
            tool_names.append(entry)
    tool_names.sort()
    sites.sort(key=lambda s: (s["tool"], s["host"]))
    return {"tools": tool_names, "sites": sites}


@router.delete("/api/agent/remembered/{name}")
def forget_remembered(request: Request, name: str, host: str | None = None) -> dict[str, bool]:
    """Forget a remembered entry so it prompts for approval again.

    Without ``host`` this drops the WHOLE-tool consent for ``name``. With ``host`` it
    drops just that one site entry (the two URL tools remember per-host, so the same
    tool can have several — deleting one leaves the rest alone).
    """
    _approvals(request)
    if host:
        consent.forget_site(request.app.state.dbx, name, host)
    else:
        consent.forget(request.app.state.dbx, name)
    return {"ok": True}


def _record_turn_metric(conn, model, conversation_id, started, tally, result, *, ttft_ms=None):
    """Best-effort per-turn speed/quality row from a run_turn result (Phase-1 telemetry)."""
    if not isinstance(result, dict):
        return
    status = result.get("status") or ""
    metrics.record_turn(
        conn, model=model, is_local=gateway.is_local(model),
        duration_ms=int((time.monotonic() - started) * 1000),
        prompt_tokens=tally.prompt_tokens, completion_tokens=tally.completion_tokens,
        conversation_id=conversation_id, ttft_ms=ttft_ms,
        steps=result.get("steps"), degraded=bool(result.get("degraded")),
        hit_max_steps=(status == "max_steps"), outcome=status,
    )


@router.post("/api/agent/turn")
def agent_turn(request: Request, body: TurnIn) -> dict:
    """Run a bounded agentic tool-calling turn (OBSERVE auto, dangerous parks)."""
    ctx, audit = _context(request)
    approvals = _approvals(request)
    routes = gateway.load_routes(request.app.state.dbx)
    model = body.model or gateway.resolve_model(body.capability, routes)
    if not model:
        raise HTTPException(status_code=400, detail=f"no model mapped for capability '{body.capability}'")
    messages = _with_memory(request, list(body.messages))  # server-side identity/memory injection
    conn = request.app.state.dbx
    # Optimizer shadow observation (Phase 5): classify + count, content-free, fail-open.
    optimizer.observe_turn(conn, body.messages, body.conversation_id)
    # Live steering (Phase 6): an ACTIVE strategy rides a TRAILING system note (the
    # _time_line slot — the static head and conversation prefix stay byte-stable, so
    # steering never costs the prompt cache). Fail-open: no strategy = baseline.
    guidance = optimizer.apply_directive(conn, getattr(request.app.state, "master_key", None),
                                         body.messages)
    if guidance:
        messages = [*messages, guidance["note"]]
    if (length_note := reply_length_note(body.reply_length)) is not None:
        messages = [*messages, length_note]

    def sink(used_model: str, response: object) -> None:  # record spend as the turn runs
        usage.record_response(conn, used_model, response)

    tally = metrics._TokenTally(sink)  # sum tokens for one turn_metrics row (still records spend)
    started = time.monotonic()
    try:
        result = agent.run_turn(
            ctx, audit, approvals, messages=messages, model=model,
            conversation_id=body.conversation_id, turn_id=uuid.uuid4().hex, usage_sink=tally,
            auto_approve=consent.remembered(conn), timeout=_INTERACTIVE_TIMEOUT,
            result_cap=gateway.result_cap_for(conn, model),
        )
    except gateway.GatewayError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from None
    except Exception as exc:  # gateway unreachable — match the plain-chat path's 502
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
    _record_turn_metric(conn, model, body.conversation_id, started, tally, result)
    if guidance:  # transparency: the chip shows which guidance shaped this answer
        result["guidance"] = {"request_type": guidance["request_type"],
                              "directive": guidance["directive"]}
    return result


# Overall bound on one streamed agent turn: 6 model round-trips at the interactive
# per-call timeout, plus slack for tool executions between them.
_STREAM_TURN_DEADLINE = 6 * _INTERACTIVE_TIMEOUT + 120.0
# SSE liveness (the plain-chat stream): emit a comment frame whenever the producer has
# been quiet this long, so a slow first token can never look like a dead connection.
_SSE_HEARTBEAT_SECONDS = 5.0
_SSE_QUEUE_FRAMES = 256  # bounded producer->consumer handoff
# How long the producer waits for a full queue before concluding the client is gone.
# Generous (a live client drains instantly; only a vanished one ever hits this).
_SSE_PRODUCER_PUT_TIMEOUT = 30.0


@router.post("/api/agent/turn/events")
def agent_turn_events(request: Request, body: TurnIn) -> StreamingResponse:
    """The agent turn as SSE: live ``tool`` activity frames, then ONE terminal frame.

    Distinct from ``/api/agent/turn/stream`` (which streams the FIRST model response's
    text deltas and bails to this flow when tools appear): this endpoint runs the whole
    tool loop, narrating it.

    Terminal frames carry exactly what POST /api/agent/turn returns — ``event: final``
    with the result dict, or ``event: error`` with {detail} — so the client treats the
    last frame as the POST response. ``run_turn`` executes in one worker thread whose
    DB access stays sequential (the same profile as any other request thread, via the
    per-thread cursor wrapper); this generator only drains a queue and holds NOTHING
    across yields (the gateway-serialization wedge taught that lesson).
    """
    ctx, audit = _context(request)
    approvals = _approvals(request)
    routes = gateway.load_routes(request.app.state.dbx)
    model = body.model or gateway.resolve_model(body.capability, routes)
    if not model:
        raise HTTPException(status_code=400, detail=f"no model mapped for capability '{body.capability}'")
    messages = _with_memory(request, list(body.messages))
    conn = request.app.state.dbx
    # Live steering (Phase 6) — same trailing-note contract as /api/agent/turn. No shadow
    # observation here (the stream endpoint already counted this ask before bailing out).
    guidance = optimizer.apply_directive(conn, getattr(request.app.state, "master_key", None),
                                         body.messages)
    if guidance:
        messages = [*messages, guidance["note"]]
    if (length_note := reply_length_note(body.reply_length)) is not None:
        messages = [*messages, length_note]

    def sink(used_model: str, response: object) -> None:  # worker thread -> per-thread cursor
        usage.record_response(conn, used_model, response)

    tally = metrics._TokenTally(sink)
    primed = _take_primed(body.primed, list(body.messages))  # claimed once, or None
    frames: queue.Queue = queue.Queue(maxsize=256)  # bounded: a runaway emitter blocks, not OOMs

    def worker() -> None:
        started = time.monotonic()
        try:
            result = agent.run_turn(
                ctx, audit, approvals, messages=messages, model=model,
                conversation_id=body.conversation_id, turn_id=uuid.uuid4().hex, usage_sink=tally,
                auto_approve=consent.remembered(conn), timeout=_INTERACTIVE_TIMEOUT,
                result_cap=gateway.result_cap_for(conn, model),
                on_event=lambda ev: frames.put(("tool", ev)),
                primed=primed,  # the stream already paid for this turn's first model response
            )
            _record_turn_metric(conn, model, body.conversation_id, started, tally, result)
            if guidance:  # transparency chip data on the terminal frame
                result["guidance"] = {"request_type": guidance["request_type"],
                                      "directive": guidance["directive"]}
            frames.put(("final", result))
        except gateway.GatewayError as exc:
            frames.put(("error", {"detail": exc.message}))
        except Exception as exc:  # match the JSON endpoint's 502 detail shape
            frames.put(("error", {"detail": f"gateway unreachable: {exc}"}))

    def events() -> Iterator[bytes]:
        threading.Thread(target=worker, name="turn-stream", daemon=True).start()
        deadline = time.monotonic() + _STREAM_TURN_DEADLINE
        while True:  # bounded by the deadline below (P10 #2)
            try:
                kind, payload = frames.get(timeout=5.0)
            except queue.Empty:
                if time.monotonic() > deadline:
                    yield _sse_event("error", {"detail": "turn timed out"})
                    return
                yield b": keepalive\n\n"  # SSE comment frame keeps proxies from idling out
                continue
            if kind == "tool":
                yield _sse_event("tool", payload)
                continue
            yield _sse_event(kind, payload)  # "final" or "error" — terminal either way
            return

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


def _sse_event(event: str, payload: dict) -> bytes:
    """Format one SSE frame (``event:`` + ``data:`` + blank line). Bytes for ASGI."""
    assert event, "sse event name required"
    assert isinstance(payload, dict), "sse payload must be a dict"
    body = json.dumps(payload, default=str)
    return f"event: {event}\ndata: {body}\n\n".encode()


# A streamed tool call arrives in FRAGMENTS: the name in one chunk, the JSON arguments split
# across several more, keyed by index. Rebuilding them is what lets an action turn reuse its
# first model round-trip instead of paying for it twice (measured: 4.18s + 3.88s on an identical
# 4,007-token prompt). Bounded like every other loop here.
_MAX_STREAMED_TOOL_CALLS = 16
_MAX_STREAMED_ARG_CHARS = 100_000


def _assemble_tool_calls(fragments: list[list[dict]]) -> list[dict] | None:
    """Rebuild complete tool calls from streamed fragments, or None if they are not sound.

    Returns None on ANYTHING doubtful — a missing name, arguments that do not parse as a JSON
    object, too many calls, absurd argument length. That is deliberate: the caller then re-runs
    the turn exactly as it does today, so a mis-assembled call can never reach a tool. Wrong
    arguments would be worse than a slow answer — some tools run without asking.
    """
    assert isinstance(fragments, list), "fragments must be a list"
    by_index: dict[int, dict] = {}
    for chunk in fragments:  # bounded by the caller's delta budget
        if not isinstance(chunk, list):
            return None
        for frag in chunk:
            if not isinstance(frag, dict):
                return None
            idx = frag.get("index", 0)
            if not isinstance(idx, int) or len(by_index) >= _MAX_STREAMED_TOOL_CALLS:
                return None
            call = by_index.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if frag.get("id"):
                call["id"] = str(frag["id"])
            fn = frag.get("function") or {}
            if not isinstance(fn, dict):
                return None
            if fn.get("name"):
                call["name"] = str(fn["name"])
            piece = fn.get("arguments")
            if piece:
                call["arguments"] += str(piece)
                if len(call["arguments"]) > _MAX_STREAMED_ARG_CHARS:
                    return None
    if not by_index:
        return None
    out: list[dict] = []
    for idx in sorted(by_index):  # stable order: the model's own call order
        call = by_index[idx]
        if not call["name"]:
            return None
        try:
            parsed = json.loads(call["arguments"] or "{}")
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        out.append({
            "id": call["id"] or f"call_{idx}_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(parsed)},
        })
    return out


# The first model response of an ACTION turn, kept just long enough for the follow-up
# /events request to claim it. Without this the turn pays for that response twice: the stream
# path discards it and run_turn re-asks the model with the same 4,000-token prompt (measured
# 4.18s + 3.88s). Bounded and short-lived: a handful of entries, seconds of life, claimed once.
_PRIMED_TTL_SECONDS = 120.0
_MAX_PRIMED = 8
_primed_lock = threading.Lock()
_primed: dict[str, tuple[float, list[dict], dict]] = {}


def _conversation_key(messages: list[dict]) -> str:
    """Identity of the CLIENT's conversation (not the server-built prompt, which carries a
    per-request time note that can tick between the two requests)."""
    return json.dumps(messages, sort_keys=True, default=str)


def _stash_primed(messages: list[dict], response: dict) -> str:
    """Park a first response for the follow-up request; returns its one-time token."""
    token = uuid.uuid4().hex
    now = time.monotonic()
    with _primed_lock:
        for key in [k for k, (exp, _m, _r) in _primed.items() if exp <= now]:  # bounded by _MAX_PRIMED
            _primed.pop(key, None)
        while len(_primed) >= _MAX_PRIMED:
            _primed.pop(next(iter(_primed)), None)  # oldest out; this is a cache, never a queue
        _primed[token] = (now + _PRIMED_TTL_SECONDS, messages, response)
    return token


def _take_primed(token: str | None, messages: list[dict]) -> dict | None:
    """Claim a parked first response — once, unexpired, and only for the SAME conversation.

    A mismatch or a miss simply returns None and the turn calls the model as it always did,
    so nothing here can put a stale answer in front of a user.
    """
    if not token:
        return None
    with _primed_lock:
        entry = _primed.pop(token, None)
    if entry is None:
        return None
    expiry, stashed, response = entry
    if time.monotonic() > expiry or _conversation_key(stashed) != _conversation_key(messages):
        return None
    return response


def _stream_first_response(
    messages: list[dict], model: str, conversation_id: str | None, client: httpx.Client,
    tools_spec: list[dict], conn=None, client_messages: list[dict] | None = None,
) -> Iterator[bytes]:
    """Stream the FIRST model response as SSE; emit ``done`` on text or ``pending`` on tools.

    The model is offered ``tools_spec`` (tool_choice auto) — WITHOUT it the model can't
    call a tool and would narrate actions it never performs (the "claimed task added" bug).
    Streams ``event: delta`` frames while text arrives. If any chunk carries tool_calls,
    abort streaming and emit a terminal ``event: pending`` so the client falls back to
    /api/agent/turn for the approval/resume flow. If the model rejects the tools field, we
    retry once as a plain (no-tools) stream. Errors emit ``event: error`` and end the
    stream (the response is already 200). The dedicated ``client`` is closed on exit.
    """
    assert messages and model, "messages + model required"
    assert client is not None and tools_spec, "stream requires its own client + a tools spec"
    text_parts: list[str] = []
    started = time.monotonic()  # for TTFT + duration telemetry (recorded on the plain-answer done)
    ttft_ms: int | None = None
    saw_tools = False
    tool_fragments: list[list[dict]] = []  # streamed tool-call pieces, reassembled below
    saw_finish = False  # did the model actually FINISH? (see the completeness gate below)
    # A model may print a tool call as TEXT (```json / a bare {…}). We hold deltas until the
    # first non-whitespace char: if it opens a code fence or JSON object, SUPPRESS the stream
    # and bail to /api/agent/turn, where run_turn recovers the tool call — so raw JSON is
    # never shown. Otherwise we commit to streaming plain text live as before.
    decided = False
    suppress = False
    chunks = 0
    spec: list[dict] | None = tools_spec  # drops to None if the model rejects tools
    try:
        for attempt in range(2):  # at most: with tools, then without (P10 #2 bounded)
            try:
                for chunk in gateway.chat_stream(messages, model, client=client, tools_spec=spec):
                    chunks += 1
                    if chunks > _STREAM_DELTA_BUDGET:  # fixed upper bound (P10 #2)
                        yield _sse_event("error", {"detail": "stream exceeded delta budget"})
                        return
                    if chunk.get("tool_calls"):  # the model started a tool turn
                        # Keep reading instead of bailing on the first fragment: a streamed tool
                        # call arrives in pieces, and collecting them lets the follow-up request
                        # REUSE this response instead of paying the same prefill again.
                        saw_tools = True
                        tool_fragments.append(chunk["tool_calls"])
                        if chunk.get("finish_reason"):
                            saw_finish = True
                            break
                        continue
                    delta = chunk.get("delta") or ""
                    if not delta:
                        continue
                    if suppress:
                        continue  # tool-call-as-text: swallow until the stream ends, then go pending
                    text_parts.append(delta)
                    if not decided:
                        lead = "".join(text_parts).lstrip()
                        if not lead:
                            continue  # only whitespace so far — keep buffering before deciding
                        decided = True
                        if lead[0] in ("`", "{"):  # opens a fence/JSON object — likely a text tool call
                            suppress = True
                            continue
                        if ttft_ms is None:
                            ttft_ms = int((time.monotonic() - started) * 1000)  # first visible token
                        yield _sse_event("delta", {"text": "".join(text_parts)})  # flush buffered prefix
                        continue
                    yield _sse_event("delta", {"text": delta})
                break  # stream finished (or broke on a tool turn)
            except gateway.GatewayError as exc:
                if spec is not None and getattr(exc, "tools_unsupported", False) and not text_parts:
                    spec = None  # this model can't take tools — retry once as a plain stream
                    continue
                yield _sse_event("error", {"status": exc.status_code, "detail": exc.message})
                return
        if saw_tools or suppress:  # tool turn (structured or text-emitted) — resolve via run_turn
            # Hand the assembled first response to the follow-up request when it is sound.
            # _assemble_tool_calls returns None on anything doubtful, and a missing/expired
            # token simply means the model is asked again — exactly today's behavior.
            payload = {"detail": "tool turn — fall back to /api/agent/turn", "model": model}
            # COMPLETENESS GATE. Only a stream that reached its terminal finish_reason may be
            # reused. A truncated one assembles arguments that are empty but VALID — and seven
            # tools (list_documents, list_tasks, email_list, read_schedule_output, …) require no
            # arguments and run inline WITHOUT approval, so a half-received call would execute.
            # Without a finish_reason we simply re-ask the model, exactly as before.
            calls = _assemble_tool_calls(tool_fragments) if (tool_fragments and saw_finish) else None
            if calls is not None and client_messages is not None:
                payload["primed"] = _stash_primed(
                    client_messages,
                    {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": calls}}]},
                )
            yield _sse_event("pending", payload)
            return
        # Plain streamed answer completed here (no tool fallback) — record ONE turn_metrics row
        # with the true time-to-first-token. Streamed deltas carry no usage block, so tokens are 0.
        metrics.record_turn(
            conn, model=model, is_local=gateway.is_local(model),
            duration_ms=int((time.monotonic() - started) * 1000), ttft_ms=ttft_ms,
            conversation_id=conversation_id, steps=0, outcome="complete",
        )
        yield _sse_event("done", {"message": "".join(text_parts), "conversation_id": conversation_id, "model": model})
    except Exception as exc:  # gateway unreachable — surface, never crash the stream
        log.warning("stream aborted: %s", exc)
        yield _sse_event("error", {"detail": f"gateway unreachable: {exc}"})
    finally:
        client.close()  # owned for the lifetime of this generator


@router.post("/api/agent/turn/stream")
def agent_turn_stream(request: Request, body: TurnIn) -> StreamingResponse:
    """SSE-stream the first model response; on a tool turn, signal the client to fall back.

    The non-streaming /api/agent/turn handles tool approval + resume; streaming is
    a fast path for plain-text answers only.
    """
    _context(request)  # 423 gate
    _approvals(request)  # 423 gate (same as /api/agent/turn)
    routes = gateway.load_routes(request.app.state.dbx)
    model = body.model or gateway.resolve_model(body.capability, routes)
    if not model:
        raise HTTPException(status_code=400, detail=f"no model mapped for capability '{body.capability}'")
    messages = _with_memory(request, list(body.messages))  # same grounding as the non-streaming path
    # Optimizer shadow observation (Phase 5). /events is deliberately NOT hooked: the SPA
    # reaches it via THIS endpoint's tool-turn bail-out, and two hooks would count one ask twice.
    optimizer.observe_turn(request.app.state.dbx, body.messages, body.conversation_id)
    # Live steering (Phase 6): trailing note (cache-safe), plus a leading ``meta`` SSE
    # frame so the streamed answer can show the transparency chip too. Clients that
    # don't know ``meta`` ignore unknown SSE event names by contract.
    guidance = optimizer.apply_directive(request.app.state.dbx,
                                         getattr(request.app.state, "master_key", None),
                                         body.messages)
    if guidance:
        messages = [*messages, guidance["note"]]
    if (length_note := reply_length_note(body.reply_length)) is not None:
        messages = [*messages, length_note]
    # Streaming uses its OWN httpx.Client (not the gateway pool): a long-lived SSE
    # stream holds a connection for the whole response, so reusing the shared pool
    # would block sibling /api/chat calls behind the stream's connection.
    stream_client = httpx.Client(base_url=gateway.gateway_url(), timeout=_INTERACTIVE_TIMEOUT)

    def _frames() -> Iterator[bytes]:
        # KEEP THE STREAM WARM. A local model takes ~8s to first token on a plain "hi"
        # (measured; p90 of a real turn is ~60s), and this generator used to emit NOTHING
        # for that whole time: no first bytes, no heartbeat, no Cache-Control. Safari drops
        # an idle streamed body — the server completed the turn and recorded it while the
        # browser painted "Couldn't reach SmartBrain" (2026-07-29). The sibling /events
        # endpoint has always kept its stream warm; this one now does the same, which
        # requires a worker thread: heartbeats must flow WHILE the gateway call blocks on
        # the first token.
        yield b": open\n\n"  # bytes on the wire immediately — the body is never idle-from-birth
        if guidance:
            yield _sse_event("meta", {"guidance": {"request_type": guidance["request_type"],
                                                   "directive": guidance["directive"]}})
        frames: queue.Queue = queue.Queue(maxsize=_SSE_QUEUE_FRAMES)  # bounded: a fast producer blocks, not OOMs

        def producer() -> None:
            # gen.close() is deterministic cleanup: it throws GeneratorExit into the
            # producer generator so its finally blocks run NOW — releasing the local-model
            # semaphore and closing the stream client — instead of at some later GC. A
            # semaphore left held by an abandoned stream wedges every local model call.
            gen = _stream_first_response(messages, model, body.conversation_id, stream_client,
                                         tools.openai_tools_spec(), conn=request.app.state.dbx,
                                         client_messages=list(body.messages))
            try:
                for frame in gen:  # bounded by the inner generator's own delta budget
                    try:
                        frames.put(frame, timeout=_SSE_PRODUCER_PUT_TIMEOUT)
                    except queue.Full:
                        return  # consumer vanished (client disconnected) — abandon, never block forever
            except Exception as exc:  # the inner generator handles its own errors; this is a backstop
                log.warning("stream producer failed: %s", exc)
                try:
                    frames.put(_sse_event("error", {"detail": f"gateway unreachable: {exc}"}), timeout=1.0)
                except queue.Full:
                    pass
            finally:
                gen.close()
                try:
                    frames.put(None, timeout=1.0)  # EOF sentinel
                except queue.Full:
                    pass

        threading.Thread(target=producer, name="chat-stream", daemon=True).start()
        deadline = time.monotonic() + _STREAM_TURN_DEADLINE
        while True:  # bounded by the deadline
            try:
                frame = frames.get(timeout=_SSE_HEARTBEAT_SECONDS)
            except queue.Empty:
                if time.monotonic() > deadline:
                    yield _sse_event("error", {"detail": "turn timed out"})
                    return
                yield b": keepalive\n\n"  # SSE comment frame: keeps browsers/proxies from dropping an idle body
                continue
            if frame is None:
                return
            yield frame

    return StreamingResponse(_frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.post("/api/agent/resume/{turn_id}")
def agent_resume(request: Request, turn_id: str) -> dict:
    """Continue a parked turn after its approvals are resolved (server-reconstructed)."""
    ctx, audit = _context(request)
    approvals = _approvals(request)
    conn = request.app.state.dbx

    def sink(used_model: str, response: object) -> None:
        usage.record_response(conn, used_model, response)

    try:
        result = agent.resume_turn(ctx, audit, approvals, turn_id, conn=conn, usage_sink=sink, auto_approve=consent.remembered(conn), timeout=_INTERACTIVE_TIMEOUT)
    except gateway.GatewayError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from None
    except Exception as exc:  # gateway unreachable
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="unknown turn")
    return result


@router.get("/api/audit")
def get_audit(request: Request, limit: int = 100) -> dict:
    """Return recent audit entries (newest first)."""
    _, audit = _context(request)
    return {"entries": audit.list(min(max(limit, 1), 500))}
