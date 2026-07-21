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

_MAX_STEPS = 8  # model round-trips per turn (raised 6->8 once the finalize rescue existed:
                # multi-document questions were routinely two steps short, and an exhausted
                # budget now degrades to an answer instead of an apology)
_MAX_TOOL_CALLS = 8  # tool executions per turn (carried across a pause)
_MAX_SOURCES = 20  # citations per turn (P10 #2 — a runaway hit list must not flood the UI)
# The citation fields a knowledge-tool result may carry (mirrors kb.KnowledgeBase._hit).
_SOURCE_KEYS = ("id", "title", "source", "page", "page_label", "offset")
# Only this turn's appended messages can hold tool results (the client transcript is
# plain role/content), and a turn appends at most _MAX_STEPS assistant + _MAX_TOOL_CALLS
# tool messages — so scanning this tail window always covers them, with slack.
_SOURCE_SCAN_WINDOW = 64
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
# Key spellings models use in a text-emitted call: the callee and its arguments. Used only by
# the strict JSON probe below — prose that merely contains these words never matches.
_CALLEE_KEYS = ("function", "tool", "tool_call", "name")
_ARGS_KEYS = ("arguments", "parameters", "args")
# APPENDED (not replacing — this shape is too loose to safely hide the reply) when the model
# printed a tool-call-shaped blob we can neither run nor attribute to a known tool. Without it
# the user sees raw JSON and assumes the app is broken rather than the model.
_TOOL_TEXT_NOTICE = (
    "\n\n> ⚠️ This model tried to use a tool as plain text — it likely doesn't support tool "
    "calling. For document/task requests, pick a tool-capable Chat model under "
    "Settings → Model routing (coder-tuned models often can't)."
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


def _looks_like_tool_attempt(content: str) -> bool:
    """True if content carries a tool-call-shaped JSON blob we can't run or attribute.

    Catches the shapes the recovery paths above miss — e.g. Qwen2.5-Coder's
    ``{"function": "read_document", "arguments": {...}}`` (a "function"/"tool" key instead
    of "name"), or an unknown tool name — so the reply gets a guidance note instead of
    looking like a broken app. Strict ``json.loads`` on fenced/whole-message blobs only;
    a normal prose answer (even one discussing tools) never matches.
    """
    assert isinstance(content, str), "content must be a string"
    if not any(f'"{k}"' in content for k in _ARGS_KEYS):
        return False  # cheap gate: a call blob always carries a quoted arguments-ish key
    candidates = _TOOL_CALL_FENCE.findall(content)
    stripped = content.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    for raw in candidates[:_MAX_TOOL_CALLS]:  # bounded (P10 #2)
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            inner = obj.get("tool_call") or obj.get("function")
            if isinstance(inner, dict):  # one envelope deep only, no recursion (P10 #1):
                obj = inner              # e.g. {"tool_call": {"name": ..., "arguments": ...}}
            if (any(isinstance(obj.get(k), str) for k in _CALLEE_KEYS)
                    and any(k in obj for k in _ARGS_KEYS)):
                return True
    return False


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


def _citations_from(tool_name: str, result_str: str) -> list[dict]:
    """Citations carried by ONE tool result, or [] — extracted from the result JSON,
    never from model prose, so they are deterministic and work with any model (a model
    can neither fabricate nor omit them). Malformed/error results yield [] rather than
    raising: a failed tool already fed its error back to the model.
    """
    assert isinstance(tool_name, str) and isinstance(result_str, str), "tool name + result string required"
    try:
        result = json.loads(result_str)
    except (ValueError, TypeError):
        return []
    if not isinstance(result, dict):
        return []
    if tool_name == "kb_search":
        hits = result.get("results")
        if not isinstance(hits, list):
            return []  # error/degraded-to-nothing result — nothing to cite
        out: list[dict] = []
        for hit in hits[:_MAX_SOURCES]:  # bounded (P10 #2)
            if isinstance(hit, dict) and hit.get("id"):
                out.append({k: hit.get(k) for k in _SOURCE_KEYS})
        return out
    if tool_name in ("read_document", "summarize_document"):
        if not result.get("id"):
            return []
        # One citation for the whole document (offset None -> Knowledge opens it at the
        # top): a read/summary is grounded in the document, not one matched passage —
        # and a fixed offset lets multiple reads of the same document dedupe to one chip.
        return [{"id": result["id"], "title": result.get("title") or "", "offset": None}]
    return []  # other tools (tasks, email, web…) ground nothing in the knowledge base


def _collect_sources(messages: list[dict]) -> list[dict]:
    """Citations for every knowledge-tool result in this turn, deduped by (id, offset).

    Scanned from the MESSAGES (the deterministic record of what actually ran) rather
    than accumulated in loop state, so a turn that parked for approval still cites the
    searches that ran before the pause — turn_state carries the messages across it.
    Tool messages only say WHICH call answered (tool_call_id); the assistant messages'
    tool_calls provide the name, so the scan joins the two.
    """
    assert isinstance(messages, list), "messages must be a list"
    names: dict[str, str] = {}  # tool_call_id -> tool name
    sources: list[dict] = []
    seen: set[tuple] = set()
    for msg in messages[-_SOURCE_SCAN_WINDOW:]:  # bounded (P10 #2); this turn's appends sit at the tail
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in (msg.get("tool_calls") or [])[:_MAX_TOOL_CALLS]:  # bounded (P10 #2)
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if tc.get("id") and isinstance(fn, dict) and fn.get("name"):
                    names[tc["id"]] = fn["name"]
        elif msg.get("role") == "tool":
            name = names.get(msg.get("tool_call_id") or "")
            if not name:
                continue  # a result we can't attribute to a tool can't be cited safely
            for cite in _citations_from(name, msg.get("content") or ""):
                key = (cite.get("id"), cite.get("offset"))
                if key in seen or len(sources) >= _MAX_SOURCES:
                    continue
                seen.add(key)
                sources.append(cite)
    return sources


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


def _notify(on_event, payload: dict) -> None:
    """Deliver a progress event to an attached listener; a listener bug never breaks the turn."""
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception:
        log.warning("turn event listener failed", exc_info=True)


def _tool_detail(name: str, args: dict | None) -> str:
    """A short, REDACTED, human-readable argument (query/url/title) for an activity line."""
    if not isinstance(args, dict):
        return ""
    red = tools.redact(args)
    for key in ("query", "url", "title", "doc_id"):  # first recognizable handle wins
        val = red.get(key)
        if isinstance(val, str) and val:
            return val[:80]
    return ""


def run_turn(ctx, audit, approvals, *, messages, model, conversation_id, turn_id, start_step=0, start_calls=0, usage_sink=None, auto_approve=frozenset(), timeout=60.0, result_cap=_RESULT_CAP, on_event=None) -> dict:
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
    # Turn-level CONTEXT budget: the step budget alone let full-window tool results
    # stack past the model's context — every later round-trip re-prefilled a huge,
    # partly-truncated prompt (a live turn ran 10+ minutes this way). Once gathered
    # results reach the finalize budget, asking for MORE tools is pure waste: the
    # model can't fit what it has. Stop and answer.
    context_budget = int(result_cap * _FINALIZE_BUDGET_FACTOR)
    gathered = start_calls and sum(  # a resumed turn re-counts its existing tool results
        len(m.get("content") or "") for m in messages if m.get("role") == "tool") or 0
    for step in range(start_step, _MAX_STEPS):  # fixed upper bound (P10 #2)
        if gathered >= context_budget:
            return _finalize_exhausted(messages, model, timeout=timeout, usage_sink=usage_sink,
                                       reason="context budget reached", steps=step,
                                       result_cap=result_cap, on_event=on_event)
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
                # sources is always [] here: this path only exists when NO tool ran.
                return {"status": "complete", "message": _first_message(plain).get("content") or "", "degraded": True, "sources": []}
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
            elif _looks_like_tool_attempt(content):
                content += _TOOL_TEXT_NOTICE  # keep the reply, but explain the odd JSON blob
        if not tool_calls:
            # Citations ship with every completed answer (possibly []) so the UI can
            # always trust the field — extracted from tool results, never model prose.
            return {"status": "complete", "message": content, "degraded": False, "steps": step + 1,
                    "sources": _collect_sources(messages)}
        if calls + len(tool_calls) > _MAX_TOOL_CALLS:
            return _finalize_exhausted(messages, model, timeout=timeout, usage_sink=usage_sink,
                                       reason="tool-call budget exceeded", steps=step + 1,
                                       result_cap=result_cap, on_event=on_event)
        calls += len(tool_calls)
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        parked, inline = _classify(tool_calls, auto_approve)
        for tc in inline:  # bounded by _MAX_TOOL_CALLS
            tname, targs, _tcid = _tool_call_parts(tc)
            _notify(on_event, {"kind": "tool", "state": "start", "tool": tname,
                               "detail": _tool_detail(tname, targs)})
            result_msg = _execute_inline(ctx, audit, tc, conversation_id, auto_approve, result_cap)
            _notify(on_event, {"kind": "tool", "state": "done", "tool": tname,
                               "ok": not result_msg["content"].startswith('{"error"')})
            messages.append(result_msg)
            gathered += len(result_msg["content"])
        if parked:
            turn_state = {"model": model, "messages": messages, "step": step, "calls": calls, "conversation_id": conversation_id}
            return {"status": "awaiting_approval", "turn_id": turn_id, "pending": _park(approvals, audit, parked, conversation_id, turn_id, turn_state)}
    return _finalize_exhausted(messages, model, timeout=timeout, usage_sink=usage_sink,
                               reason="step budget exhausted", steps=_MAX_STEPS,
                               result_cap=result_cap, on_event=on_event)


_EXHAUSTED_NUDGE = (
    "You have used every tool step available for this turn — do not request any more "
    "tools. Answer the user's question NOW, using everything the tool results above "
    "already contain. If something is genuinely missing, say plainly what you could "
    "not finish."
)
# Finalize-prompt budget as a multiple of the per-result cap. The cap is ~30% of the
# model's context in chars (gateway._RESULT_FRACTION), so 2.4x ≈ 72% of the context —
# leaving headroom for the system prompt, the question, the nudge, and the answer.
_FINALIZE_BUDGET_FACTOR = 2.4


def _fit_for_finalize(messages, budget_chars: int) -> list[dict]:
    """A fresh, in-budget prompt for the finalize call.

    The transcript at exhaustion can exceed the model's context — that is often WHY
    the budget died (seen live: five full-window pages of a 170k-char document fed a
    32k-token model). Handing it back verbatim makes the rescue call fail exactly when
    it's needed. Rebuild instead: the system prompt + the user's question + the FIRST
    tool result (a paged document's framing lives up front) + the newest results that
    fit, flattened into one message — a plain chat call, so tool-role pairing rules
    never apply and templates never see orphaned tool messages.
    """
    assert budget_chars > 0, "positive budget required"
    head = next((m for m in messages if m.get("role") == "system"), None)
    question = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    tool_chunks = [m.get("content") or "" for m in messages if m.get("role") == "tool"]
    used = sum(len(m.get("content") or "") for m in (head, question) if m)
    kept: list[str] = []
    if tool_chunks:  # the first result anchors the answer (search hits / page one)
        first = tool_chunks[0][: max(1, budget_chars // 3)]
        kept.append(first)
        used += len(first)
    for chunk in reversed(tool_chunks[1:]):  # then newest first — the most refined work
        if used + len(chunk) > budget_chars:
            room = budget_chars - used
            if room > 500:  # a truncated tail beats dropping the result entirely
                kept.insert(1, chunk[:room])
            break
        kept.insert(1, chunk)
        used += len(chunk)
    out = [m for m in (head, question) if m]
    if kept:
        out.append({"role": "system", "content":
                    "Tool results gathered this turn (middle pages may be omitted to fit):\n\n"
                    + "\n\n---\n\n".join(kept)})
    out.append({"role": "system", "content": _EXHAUSTED_NUDGE})
    return out


def _finalize_exhausted(messages, model, *, timeout, usage_sink, reason: str, steps: int,
                        result_cap: int, on_event=None) -> dict:
    """One tools-disabled model call that turns an exhausted budget into an answer.

    Without this, the internal counter leaked into chat as the entire reply ("step
    budget exhausted" — seen live after a long document was diligently paged five
    times and the budget died before a word of summary). The gathered tool results
    are usually more than enough to answer, so ask the model to answer from them —
    from a prompt REBUILT to fit its context — and the raw reason survives only if
    that final call itself fails or leaks tool JSON.
    """
    _notify(on_event, {"kind": "phase", "state": "answering"})  # the last long call is visible too
    try:
        prompt = _fit_for_finalize(messages, int(result_cap * _FINALIZE_BUDGET_FACTOR))
        data = gateway.chat(prompt, model, timeout=timeout)
    except Exception:
        return {"status": "max_steps", "message": reason, "steps": steps}
    _emit_usage(usage_sink, model, data)
    content = _first_message(data).get("content") or ""
    if not content.strip() or _looks_like_tool_call(content):
        return {"status": "max_steps", "message": reason, "steps": steps}
    return {"status": "complete", "message": content, "degraded": False, "steps": steps,
            "sources": _collect_sources(messages)}


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
