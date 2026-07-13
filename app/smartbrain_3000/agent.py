"""Bounded agentic tool-calling loop (H4c).

The model may propose tool calls; OBSERVE tools run inline and feed back, while
REVIEWED/IRREVERSIBLE calls park the turn for user approval and resume after.
Every loop is range-bounded (P10 #2): at most ``_MAX_STEPS`` model round-trips
and ``_MAX_TOOL_CALLS`` tool executions per turn (the count survives a pause).
All tool execution goes through ``tools.run`` (the audited, tier-gated
chokepoint); this module never calls a handler directly.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import uuid

from . import gateway, tools

log = logging.getLogger(__name__)

_MAX_STEPS = 6  # model round-trips per turn
_MAX_TOOL_CALLS = 8  # tool executions per turn (carried across a pause)
_RESULT_CAP = 20000  # fallback cap on a tool-result string; the routes/scheduler override it per-turn
                     # with gateway.result_cap_for(model) (context-sized). Only direct callers use this.
# A ```json {"name": ..., "arguments": {...}} ``` fenced block (some local models/runtimes
# print tool calls as text instead of structured tool_calls). DOTALL so args span lines.
_TOOL_CALL_FENCE = re.compile(r"```(?:json|tool_call|tool)?\s*(\{.*?\})\s*```", re.DOTALL)
_TOOL_NAME_IN_TEXT = re.compile(r'"name"\s*:\s*"([a-z_]+)"')
# Shown instead of raw JSON when a model leaked a tool-call-shaped blob we can't run.
_TOOL_LEAK_MESSAGE = (
    "I tried to perform that action, but my response wasn't in a format I could run — this can "
    "happen with smaller local models. Please try again, or switch to a model with reliable tool "
    "calling under Settings → Model routing."
)


def _extract_text_tool_calls(content: str) -> list[dict]:
    """Recover tool calls a model emitted as TEXT instead of structured tool_calls.

    Quantized/local models (or runtimes that don't parse tool syntax) print
    ```json {"name": "...", "arguments": {...}} ``` as the message body. Convert a
    KNOWN-tool call into a real tool_call so the action runs and is never shown as JSON.
    Strict ``json.loads`` (no comment stripping — that would corrupt URL args); a malformed
    or example blob simply isn't recovered. Bounded; ignores unrecognized tools.
    """
    assert isinstance(content, str), "content must be a string"
    if '"name"' not in content or '"arguments"' not in content:
        return []
    blocks = _TOOL_CALL_FENCE.findall(content)
    if not blocks:
        stripped = content.strip()
        blocks = [stripped] if stripped.startswith("{") else []
    out: list[dict] = []
    for raw in blocks[:_MAX_TOOL_CALLS]:  # bounded (P10 #2)
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        name = obj.get("name") if isinstance(obj, dict) else None
        args = obj.get("arguments") if isinstance(obj, dict) else None
        if isinstance(name, str) and isinstance(args, dict) and tools.get_tool(name) is not None:
            out.append({"id": f"text_{uuid.uuid4().hex[:8]}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}})
    return out


def _looks_like_tool_call(content: str) -> bool:
    """True if content is a tool-call-shaped blob for a KNOWN tool (so we hide raw JSON)."""
    assert isinstance(content, str), "content must be a string"
    if '"arguments"' not in content:
        return False
    match = _TOOL_NAME_IN_TEXT.search(content)
    return bool(match and tools.get_tool(match.group(1)) is not None)


def _first_message(data: dict) -> dict:
    """Return the first choice's message, or raise on a malformed response."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise gateway.GatewayError(502, "gateway returned no choices")
    return choices[0].get("message") or {}


def _tool_call_parts(tc: dict) -> tuple[str, dict | None, str | None]:
    """Parse (name, parsed-args-or-None, id) from a tool_call; None args = bad JSON."""
    assert isinstance(tc, dict), "tool_call must be a dict"
    fn = tc.get("function") or {}
    name = fn.get("name") or ""
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (ValueError, TypeError):
        args = None
    return name, args if isinstance(args, dict) else None, tc.get("id")


def _args_valid(tool: tools.Tool, args: dict) -> bool:
    """True if args pass the tool's schema (so a dangerous call is safe to park)."""
    try:
        tools.validate_args(tool, args)
        return True
    except ValueError:
        return False


def _execute_inline(ctx: tools.ToolContext, audit, tc: dict, conversation_id: str | None, auto_approve, result_cap: int = _RESULT_CAP) -> dict:
    """Run a non-parked tool call (OBSERVE / remembered write / unknown / invalid)."""
    name, args, tcid = _tool_call_parts(tc)
    tool = tools.get_tool(name) if name else None
    # A REVIEWED write the user chose to remember runs without re-asking. The tier
    # check means an IRREVERSIBLE tool can NEVER auto-run, even if the set is corrupt.
    remembered = tool is not None and tool.tier is tools.Tier.REVIEWED and name in auto_approve
    if args is None or tool is None:
        content = json.dumps({"error": f"cannot run tool '{name}'"})
    elif tool.tier is not tools.Tier.OBSERVE and not remembered:
        # A non-OBSERVE, un-remembered tool only reaches inline with invalid args
        # (else it parks); never call its handler — feed the error back to the model.
        content = json.dumps({"error": "invalid arguments for tool"})
    else:
        try:
            claim = tools.GRANTED if remembered else None  # OBSERVE needs no claim; remembered is pre-authorized
            result = tools.run(ctx, audit, name, args, actor="assistant", conversation_id=conversation_id, claim=claim)
            content = json.dumps(result, default=str)
        except Exception as exc:  # never crash the turn — feed the error back
            content = json.dumps({"error": str(exc)})
    return {"role": "tool", "tool_call_id": tcid, "content": content[:result_cap]}


def _classify(tool_calls: list[dict], auto_approve) -> tuple[list[dict], list[dict]]:
    """Split tool_calls into (parked = valid non-OBSERVE & not remembered, inline = rest)."""
    assert isinstance(tool_calls, list), "tool_calls must be a list"
    parked, inline = [], []
    for tc in tool_calls:  # bounded by _MAX_TOOL_CALLS (checked by the caller)
        name, args, _ = _tool_call_parts(tc)
        tool = tools.get_tool(name) if name else None
        dangerous = tool is not None and tool.tier is not tools.Tier.OBSERVE
        remembered = tool is not None and tool.tier is tools.Tier.REVIEWED and name in auto_approve
        # Park a valid dangerous call unless it's a remembered write (runs inline).
        # Invalid args go inline so the model gets the error and the approve path
        # never wedges on a bad arg.
        if dangerous and not remembered and args is not None and _args_valid(tool, args):
            parked.append(tc)
        else:
            inline.append(tc)
    return parked, inline


def _park(approvals, audit, parked: list[dict], conversation_id: str | None, turn_id: str, turn_state: dict) -> list[dict]:
    """Persist dangerous calls as pending approvals; return the tile list."""
    assert parked, "nothing to park"
    assert turn_state, "turn state required to resume"
    pending: list[dict] = []
    for tc in parked:  # bounded by _MAX_TOOL_CALLS
        name, args, tcid = _tool_call_parts(tc)
        tool = tools.get_tool(name)
        pid = approvals.create_pending(
            name, tool.tier.value, args, conversation_id=conversation_id,
            turn_id=turn_id, tool_call_id=tcid, turn_state=turn_state,
        )
        audit.append("assistant", name, tool.tier.value, "proposed", True, conversation_id=conversation_id, args_summary=tools.summarize(args))
        pending.append({"id": pid, "tool": name, "tier": tool.tier.value, "args": tools.redact(args)})
    return pending


def _emit_usage(usage_sink, model, response) -> None:
    """Forward a model response's token usage to an optional sink (best-effort)."""
    if usage_sink is not None:
        usage_sink(model, response)


def run_turn(ctx, audit, approvals, *, messages, model, conversation_id, turn_id, start_step=0, start_calls=0, usage_sink=None, auto_approve=frozenset(), timeout=60.0, result_cap=_RESULT_CAP) -> dict:
    """Run the bounded loop from ``start_step``; return a terminal/awaiting result.

    ``auto_approve`` is the set of REVIEWED tool names the user has remembered;
    those run inline instead of parking. IRREVERSIBLE tools always park. ``timeout``
    is the per-gateway-call budget — the scheduled path raises it so a cold local-model
    load doesn't fail the turn. ``result_cap`` caps each tool-result string fed back to the
    model; the route sizes it to the model's context so a big-context model can read more.
    """
    assert audit is not None and approvals is not None, "unlocked stores required"
    assert messages and model, "messages + model required"
    ctx = dataclasses.replace(ctx, model=model)  # a handler (summarize) calls the gateway with THIS model
    calls = start_calls
    for step in range(start_step, _MAX_STEPS):  # fixed upper bound (P10 #2)
        try:
            data = gateway.chat_with_tools(messages, model, tools.openai_tools_spec(), timeout=timeout)
            _emit_usage(usage_sink, model, data)
        except gateway.GatewayError as exc:
            if calls == 0:  # nothing ran yet: a model that can't use tools can still answer plainly
                log.warning("tools call failed (%s); trying a plain answer: %s", exc.status_code, exc.message)
                try:
                    plain = gateway.chat(messages, model, timeout=timeout)
                except Exception:
                    raise exc from None  # plain also failed -> a real error; surface the original
                _emit_usage(usage_sink, model, plain)
                return {"status": "complete", "message": _first_message(plain).get("content") or "", "degraded": True}
            raise  # a tool already ran -> fail closed, surface the error
        choice = _first_message(data)
        tool_calls = choice.get("tool_calls") or []
        content = choice.get("content") or ""
        if not tool_calls:  # the gateway didn't parse tools — recover a tool call emitted as TEXT
            recovered = _extract_text_tool_calls(content)
            if recovered:
                tool_calls, content = recovered, ""  # the JSON WAS the call — never surface it
            elif _looks_like_tool_call(content):
                content = _TOOL_LEAK_MESSAGE  # leaked an unrunnable tool blob — hide the raw JSON
        if not tool_calls:
            return {"status": "complete", "message": content, "degraded": False, "steps": step + 1}
        if calls + len(tool_calls) > _MAX_TOOL_CALLS:
            return {"status": "max_steps", "message": "tool-call budget exceeded", "steps": step + 1}
        calls += len(tool_calls)
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        parked, inline = _classify(tool_calls, auto_approve)
        for tc in inline:  # bounded by _MAX_TOOL_CALLS
            messages.append(_execute_inline(ctx, audit, tc, conversation_id, auto_approve, result_cap))
        if parked:
            turn_state = {"model": model, "messages": messages, "step": step, "calls": calls, "conversation_id": conversation_id}
            return {"status": "awaiting_approval", "turn_id": turn_id, "pending": _park(approvals, audit, parked, conversation_id, turn_id, turn_state)}
    return {"status": "max_steps", "message": "step budget exhausted", "steps": _MAX_STEPS}


def resume_turn(ctx, audit, approvals, turn_id: str, *, conn=None, usage_sink=None, auto_approve=frozenset(), timeout=60.0, result_cap=_RESULT_CAP) -> dict | None:
    """Resume a parked turn once its approvals are resolved; None if unknown turn.

    ``conn`` (when given) sizes ``result_cap`` to the resumed turn's model — the route can't know that
    model until the parked turn_state is loaded here. ``ctx.model`` is set by the ``run_turn`` call below.
    """
    assert audit is not None and approvals is not None, "unlocked stores required"
    assert turn_id, "turn id required"
    rows = approvals.list_for_turn(turn_id)
    if not rows:
        return None
    if any(r["status"] in ("pending", "approved") for r in rows):  # not all resolved yet
        still = [{"id": r["id"], "tool": r["tool_name"], "tier": r["tier"], "args": tools.redact(r["args"])} for r in rows if r["status"] == "pending"]
        return {"status": "awaiting_approval", "turn_id": turn_id, "pending": still}
    # A turn may park more than once. Resume from the LATEST park's snapshot (its
    # messages already contain every earlier park's assistant+tool exchange), and
    # answer ONLY that park's tool_calls — so the tool-message sequence stays
    # well-formed and the step/call budget never resets.
    stated = [r for r in rows if r.get("turn_state")]
    assert stated, "resume requires stored turn state"
    latest_step = max(r["turn_state"]["step"] for r in stated)
    batch = [r for r in stated if r["turn_state"]["step"] == latest_step]
    turn_state = batch[0]["turn_state"]
    messages = turn_state["messages"]
    if conn is not None:  # size the cap to the resumed turn's model (known only now, from turn_state)
        result_cap = gateway.result_cap_for(conn, turn_state["model"])
    for r in batch:  # one tool-result message per call in the latest park (server-reconstructed)
        if r["status"] == "executed" and r["result"] is not None:
            content = json.dumps(r["result"], default=str)
        elif r["status"] == "executed":
            content = json.dumps({"error": "tool execution failed"})  # never claim a forged success
        else:
            content = json.dumps({"error": "action was not approved"})
        messages.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": content[:result_cap]})
    return run_turn(
        ctx, audit, approvals, messages=messages, model=turn_state["model"],
        conversation_id=turn_state.get("conversation_id"), turn_id=turn_id,
        start_step=turn_state["step"] + 1, start_calls=turn_state["calls"], usage_sink=usage_sink,
        auto_approve=auto_approve, timeout=timeout, result_cap=result_cap,
    )
