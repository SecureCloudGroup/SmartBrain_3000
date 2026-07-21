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

from . import agent, consent, docsummaries, gateway, search, tools, usage
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
    messages: list[dict] = Field(min_length=1)
    model: str | None = None
    capability: str = "chat"
    conversation_id: str | None = None


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


@router.get("/api/agent/pending")
def list_pending(request: Request) -> dict:
    """List actions awaiting approval (args redacted for the tile)."""
    approvals = _approvals(request)
    pending = [
        {"id": p["id"], "tool": p["tool"], "tier": p["tier"], "created_at": p["created_at"],
         "turn_id": p.get("turn_id"), "conversation_id": p.get("conversation_id"),
         "args": tools.redact(p["args"])}
        for p in approvals.list_pending()
    ]
    return {"pending": pending}


@router.post("/api/agent/pending/{pid}/approve")
def approve(request: Request, pid: str, body: ApproveIn) -> dict:
    """Approve + execute a pending action (audited). IRREVERSIBLE needs confirm_tool."""
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
    return {"status": "executed", "result": result}


@router.post("/api/agent/pending/{pid}/deny")
def deny(request: Request, pid: str) -> dict[str, bool]:
    """Deny a pending action (audited; never executes)."""
    ctx, audit = _context(request)
    approvals = _approvals(request)
    row = approvals.get(pid)
    if row is None:
        raise HTTPException(status_code=404, detail="pending action not found")
    if not approvals.deny(pid):
        raise HTTPException(status_code=409, detail=f"already {row['status']}")
    audit.append("user", row["tool_name"], row["tier"], "denied", True, args_summary=tools.summarize(row["args"]))
    return {"ok": True}


@router.get("/api/agent/remembered")
def list_remembered(request: Request) -> dict:
    """Tools the user has remembered (auto-approved writes); IRREVERSIBLE is never here."""
    _approvals(request)  # unlocked gate
    return {"tools": sorted(consent.remembered(request.app.state.dbx))}


@router.delete("/api/agent/remembered/{name}")
def forget_remembered(request: Request, name: str) -> dict[str, bool]:
    """Forget a remembered tool so it prompts for approval again."""
    _approvals(request)
    consent.forget(request.app.state.dbx, name)
    return {"ok": True}


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

    def sink(used_model: str, response: object) -> None:  # record spend as the turn runs
        usage.record_response(conn, used_model, response)

    try:
        return agent.run_turn(
            ctx, audit, approvals, messages=messages, model=model,
            conversation_id=body.conversation_id, turn_id=uuid.uuid4().hex, usage_sink=sink,
            auto_approve=consent.remembered(conn), timeout=_INTERACTIVE_TIMEOUT,
            result_cap=gateway.result_cap_for(conn, model),
        )
    except gateway.GatewayError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from None
    except Exception as exc:  # gateway unreachable — match the plain-chat path's 502
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc


# Overall bound on one streamed agent turn: 6 model round-trips at the interactive
# per-call timeout, plus slack for tool executions between them.
_STREAM_TURN_DEADLINE = 6 * _INTERACTIVE_TIMEOUT + 120.0


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

    def sink(used_model: str, response: object) -> None:  # worker thread -> per-thread cursor
        usage.record_response(conn, used_model, response)

    frames: queue.Queue = queue.Queue(maxsize=256)  # bounded: a runaway emitter blocks, not OOMs

    def worker() -> None:
        try:
            result = agent.run_turn(
                ctx, audit, approvals, messages=messages, model=model,
                conversation_id=body.conversation_id, turn_id=uuid.uuid4().hex, usage_sink=sink,
                auto_approve=consent.remembered(conn), timeout=_INTERACTIVE_TIMEOUT,
                result_cap=gateway.result_cap_for(conn, model),
                on_event=lambda ev: frames.put(("tool", ev)),
            )
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
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def _stream_first_response(
    messages: list[dict], model: str, conversation_id: str | None, client: httpx.Client,
    tools_spec: list[dict],
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
    saw_tools = False
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
                    if chunk.get("tool_calls"):  # model started a tool turn — bail to the fallback path
                        saw_tools = True
                        break
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
            yield _sse_event("pending", {"detail": "tool turn — fall back to /api/agent/turn", "model": model})
            return
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
    # Streaming uses its OWN httpx.Client (not the gateway pool): a long-lived SSE
    # stream holds a connection for the whole response, so reusing the shared pool
    # would block sibling /api/chat calls behind the stream's connection.
    stream_client = httpx.Client(base_url=gateway.gateway_url(), timeout=_INTERACTIVE_TIMEOUT)
    return StreamingResponse(
        _stream_first_response(messages, model, body.conversation_id, stream_client, tools.openai_tools_spec()),
        media_type="text/event-stream",
    )


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
