"""The bounded research loop (plan 6.1): the Library, plus the open web on the cloud tier (S5b).
The model only chooses tools; code runs every one.

Two shapes share one ``Outcome``: ``run_research`` (the cloud tool loop) and ``run_closed_world`` (the local
model picks a ranked Library row and fills a recipe from its sample; no loop, never the web). Both end at a
``propose_card`` success (``proposed``) or in an honest ``declined`` with reasons and up to three links:
budget exhaustion, a model that stopped calling tools, or ``propose_card`` failing its checks twice.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import gateway, ni_master, usage
from . import prompt, schemas, tools
from .ctx import ResearchCtx, error, one_line

log = logging.getLogger(__name__)

ChatFn = Callable[[list, "list | None"], dict]   # (messages, tools or None) -> an OpenAI-shaped response
AcceptFn = Callable[[dict], "str | None"]        # a proposal -> why it cannot land, or None
MAX_CALLS_PER_TURN = 3
MAX_NUDGES = 2
MAX_VERIFY_FAILS = 2
MAX_LINKS = 3
MAX_REASONS = 5
MAX_LOCAL_ROWS = 2
MAX_LOCAL_FILLS = 3
MAX_JSON_DEPTH = 64  # model JSON nested deeper is refused before decoding (a looping model can emit "[[[[...")
_STRUCTURED = frozenset({"json", "csv", "feed", "xml"})


@dataclass
class Outcome:
    """How a run ended. ``proposal`` is ``propose_card``'s sealed proposal (spec, tier, preview)."""

    status: str                      # proposed | declined
    mode: str                        # cloud | local
    proposal: dict | None = None
    reasons: list = field(default_factory=list)
    links: list = field(default_factory=list)
    ending: str = ""                 # a declined run's row of ``ni_master.ENDINGS`` (how the flow ends it)
    usage: dict = field(default_factory=lambda: {"calls": 0, "prompt": 0, "completion": 0, "cache_read": 0,
                                                 "cache_write": 0})


def _links(ctx: ResearchCtx) -> list[dict]:
    """Up to three sources the run saw (title and address; Library rows first, then search results), for the honest decline."""
    assert MAX_LINKS > 0, "a link cap"
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    rows = [{"title": str(v.get("title") or "")[:120], "url": str(v.get("page") or k)} for kind in ("library", "search")
            for k, v in ctx.urls.items() if v.get("kind") == kind]
    return rows[:MAX_LINKS]


def _decline(ctx: ResearchCtx, out: Outcome, reasons: list, ending: str = "research_declined_budget") -> Outcome:
    assert isinstance(reasons, list), "reasons are a list"
    assert out.status in ("declined", "proposed"), "a status"
    assert ni_master.ENDINGS[ending][0] == "declined", "an ending that declines"
    out.ending = ending
    out.status = "declined"
    out.proposal = None
    out.reasons = [one_line(r, 200) for r in reasons if r][:MAX_REASONS] or ["the research did not finish"]
    out.links = _links(ctx)
    return out


def _count(ctx: ResearchCtx, out: Outcome, response: object) -> None:
    """Add one model reply's usage to the outcome and the ctx token budget (cached reads are not new tokens)."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert isinstance(out, Outcome), "an outcome"
    body = response.get("usage") if isinstance(response, dict) else None
    out.usage["calls"] += 1
    if not isinstance(body, dict):
        return
    try:
        created, read = usage.cache_tokens(body)
        total, done = int(usage.prompt_total(body)), int(body.get("completion_tokens") or 0)
    except (TypeError, ValueError):  # a malformed usage block counts as a call with unknown tokens
        return
    out.usage["prompt"] += total
    out.usage["completion"] += done
    out.usage["cache_read"] += read
    out.usage["cache_write"] += created
    ctx.add_tokens(max(total - read, 0) + max(done, 0))


def _budget_stop(ctx: ResearchCtx) -> str | None:
    """The budget that is used up (seconds, tokens or turns), as a reason; None while the run may go on."""
    assert ctx.max_turns > 0, "a turn budget"
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    for name, used, cap in (("time", ctx.elapsed(), ctx.max_seconds), ("model tokens", ctx.tokens, ctx.token_cap),
                            ("tool calls", ctx.turns, ctx.max_turns)):  # bounded: three budgets
        if used >= cap:
            return f"the {name} budget ran out"
    return None


def _message(response: object) -> dict:
    """``choices[0].message`` of a response, or an empty dict."""
    assert isinstance(MAX_REASONS, int), "a reason cap"
    assert MAX_CALLS_PER_TURN > 0, "a call cap"
    choices = response.get("choices") if isinstance(response, dict) else None
    msg = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    return msg if isinstance(msg, dict) else {}


def _too_deep(text: str, cap: int = MAX_JSON_DEPTH) -> bool:
    """True when ``text`` nests brackets deeper than ``cap`` outside strings (one linear pass): model JSON that
    deep is refused before the decoder can recurse on it."""
    assert isinstance(text, str), "text required"
    assert cap > 0, "a positive cap"
    depth, in_str, esc = 0, False, False
    for ch in text:  # bounded by len(text)
        if in_str:
            esc, in_str = (False, in_str) if esc else (ch == "\\", ch != '"')
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > cap:
                return True
        elif ch in "]}":
            depth = max(depth - 1, 0)
    return False


def _calls(msg: dict) -> list[dict]:
    """The assistant's tool calls as ``{id, name, args}`` (``args`` None when the arguments are not a JSON object)."""
    assert isinstance(msg, dict), "a message"
    assert MAX_CALLS_PER_TURN > 0, "a call cap"
    out = []
    for i, tc in enumerate((msg.get("tool_calls") or [])[:8]):  # bounded: a few calls per reply
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        raw = fn.get("arguments") or "{}"
        try:
            args = None if not isinstance(raw, str) or _too_deep(raw) else json.loads(raw)
        except (ValueError, TypeError):
            args = None
        out.append({"id": str(tc.get("id") or f"call_{i}")[:64], "name": str(fn.get("name") or ""),
                    "args": args if isinstance(args, dict) else None})
    return out


def _echo(msg: dict, calls: list[dict]) -> dict:
    """The assistant turn as it goes back to the model: text and the calls it made, nothing else."""
    assert isinstance(msg, dict), "a message"
    assert isinstance(calls, list), "calls required"
    return {"role": "assistant", "content": str(msg.get("content") or ""),
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["args"] or {}, sort_keys=True)}}
                           for c in calls]}


def _left(ctx: ResearchCtx) -> dict:
    assert ctx.max_turns >= ctx.turns >= 0, "turns within budget"
    assert ctx.max_fetches >= 0, "a fetch cap"
    return {"turns_left": ctx.max_turns - ctx.turns, "fetches_left": ctx.max_fetches - ctx.fetches,
            "seconds_left": max(int(ctx.max_seconds - ctx.elapsed()), 0)}


PROPOSE_TOOLS = ("propose_card", "propose_compare")   # the tools that end a run with a proposal


def _proposal_result(ctx: ResearchCtx, result: dict, accept: AcceptFn | None, tool: str = "propose_card") -> dict:
    """A propose tool's result after the caller's own landing check (a proposal that cannot land is a verify failure)."""
    assert ctx.max_turns > 0, "a turn budget"
    assert isinstance(result, dict), "a result"
    if not result.get("ok") or accept is None:
        return result
    why = accept(result["proposal"])
    if not why:
        return result
    ctx.note(tool, "draft", f"cannot land: {why}")
    return {**error("verify_failed", why), "reasons": [one_line(why, 200)]}


def _bounded(run, ctx: ResearchCtx, out: Outcome) -> Outcome:
    """The one boundary around a research run: whatever a model reply or a tool does, the run ends in an honest
    decline (with the usage counted so far), never a crash that seals the card failed."""
    assert callable(run), "a run"
    assert isinstance(out, Outcome), "an outcome"
    try:
        return run(out)
    except Exception as exc:  # the boundary: any failure is a decline
        log.warning("ni_research: run stopped: %s", type(exc).__name__)
        return _decline(ctx, out, [f"the model reply could not be handled ({type(exc).__name__})"])


def run_research(ctx: ResearchCtx, chat: ChatFn, *, place: str = "", accept: AcceptFn | None = None) -> Outcome:
    """The cloud loop: ask the model for tool calls, run each in code, end at ``propose_card``, budget or two failed
    proposals. ``chat(messages, tools)`` is the only model seam (the gateway's tool path, or a cassette)."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert callable(chat), "a chat seam"
    return _bounded(lambda out: _research_turns(ctx, chat, out, place, accept), ctx, Outcome("declined", "cloud"))


def _research_turns(ctx: ResearchCtx, chat: ChatFn, out: Outcome, place: str, accept: AcceptFn | None) -> Outcome:
    assert isinstance(out, Outcome), "an outcome"
    assert callable(chat), "a chat seam"
    base, turns = prompt.initial_messages(ctx, place), []
    fails, nudges, why = 0, 0, []
    for _ in range(ctx.max_turns + MAX_NUDGES + MAX_VERIFY_FAILS + 1):  # bounded: tool calls + nudges + retries
        stop = _budget_stop(ctx)
        if stop:
            return _decline(ctx, out, [stop, *why])
        try:
            response = chat(prompt.build_messages(base, turns), prompt.openai_tools("search" in ctx.allowed_origins))
        except Exception as exc:  # the model route failed: an honest decline, never a crash
            log.warning("ni_research: model call failed: %s", type(exc).__name__)
            return _decline(ctx, out, [f"the research model was unavailable ({type(exc).__name__})", *why])
        _count(ctx, out, response)
        msg, calls = _message(response), _calls(_message(response))
        if not calls:
            nudges += 1
            if nudges > MAX_NUDGES:
                return _decline(ctx, out, ["the model stopped without proposing a card", *why])
            turns += [{"role": "assistant", "content": str(msg.get("content") or "")[:2000]},
                      {"role": "user", "content": "Reply with a tool call (library_search first, or propose_card)."}]
            continue
        turns.append(_echo(msg, calls[:MAX_CALLS_PER_TURN]))
        for n, call in enumerate(calls[:MAX_CALLS_PER_TURN]):  # bounded by the per-reply cap
            result = (error("invalid_args", "arguments must be a JSON object") if call["args"] is None
                      else schemas.run_tool(ctx, call["name"], call["args"]))
            if call["name"] in PROPOSE_TOOLS:
                result = _proposal_result(ctx, result, accept, call["name"])
                if result.get("ok"):
                    out.status, out.proposal, out.reasons = "proposed", result["proposal"], []
                    return out
                if result.get("error") == "verify_failed":
                    fails += 1
                    why = [one_line(r, 200) for r in result.get("reasons") or [result.get("detail")]]
                    if fails >= MAX_VERIFY_FAILS:
                        return _decline(ctx, out, ["the card failed its checks twice", *why], "research_declined_checks")
            shown = {**result, "budget": _left(ctx)} if n < MAX_CALLS_PER_TURN else result
            turns.append({"role": "tool", "tool_call_id": call["id"], "content": prompt.result_text(shown)})
        for call in calls[MAX_CALLS_PER_TURN:]:  # a call beyond the cap still gets its (refusal) result
            turns.append({"role": "tool", "tool_call_id": call["id"],
                          "content": prompt.result_text(error("too_many_calls", f"{MAX_CALLS_PER_TURN} per reply"))})
    return _decline(ctx, out, ["the research ran out of steps", *why])


def _json_object(text: str) -> dict | None:
    """The first JSON object in a model's reply (fences and prose around it are ignored), else None."""
    assert text is not None, "text required"
    assert isinstance(text, str), "text required"
    dec = json.JSONDecoder()
    text = text[:20000]
    if _too_deep(text):
        return None
    for i, ch in enumerate(text):  # bounded
        if ch != "{":
            continue
        try:
            obj, _end = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _reply(chat: ChatFn, messages: list, ctx: ResearchCtx, out: Outcome) -> str:
    """One tool-less local completion as text ('' when the call fails)."""
    assert isinstance(messages, list) and messages, "messages required"
    assert callable(chat), "a chat seam"
    try:
        response = chat(messages, None)
    except Exception as exc:
        log.warning("ni_research: local model call failed: %s", type(exc).__name__)
        return ""
    _count(ctx, out, response)
    return gateway.completion_text(response)


def _ranked(ctx: ResearchCtx) -> tuple[list[dict], str]:
    """Code's ranked candidate rows for the ask (keyless, readable formats only), or ``([], why)``."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.request, "an ask"
    found = tools.library_search(ctx, ctx.request)
    if not found.get("ok"):
        return [], str(found.get("detail") or found.get("error"))
    rows = [c for c in found["candidates"] if not c["needs_access"] and c.get("format") in _STRUCTURED]
    return rows, ("" if rows else "no Library source for this ask can be read without a key")


def _pick(chat: ChatFn, ctx: ResearchCtx, rows: list[dict], out: Outcome) -> list[dict]:
    """The rows in the order to try: the model's pick first (an unreadable reply keeps the code's rank)."""
    assert isinstance(out, Outcome), "an outcome"
    assert rows, "rows required"
    reply = _json_object(_reply(chat, prompt.local_pick_messages(ctx, rows), ctx, out)) or {}
    number = reply.get("pick")
    if number is None and "pick" in reply:
        return []   # the model says no row can answer: an honest decline, not a guess
    index = number - 1 if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= len(rows) else 0
    return [rows[index], *(r for i, r in enumerate(rows) if i != index)][:MAX_LOCAL_ROWS]


def _draft(ctx: ResearchCtx, url: str, fmt: str, reply: str) -> dict | None:
    """The model's pipeline and scene as a whole draft: code fills source, title, goal, params and display."""
    assert isinstance(reply, str), "a reply"
    assert url and fmt, "url and format required"
    body = _json_object(reply)
    if not body or not isinstance(body.get("pipeline"), list) or not isinstance(body.get("scene"), dict):
        return None
    source = {"type": "http_json", "url": url, **({"format": fmt} if fmt != "json" else {})}
    title = str(ctx.question.get("subject") or ctx.request)[:80]
    return {"version": 1, "title": title, "goal": ctx.request[:400], "params": {}, "source": source,
            "pipeline": body["pipeline"], "scene": body["scene"], "display": {"size": "small"}, "model": None}


def _fill(chat: ChatFn, ctx: ResearchCtx, row: dict, out: Outcome, accept: AcceptFn | None, fills: int
          ) -> tuple[dict | None, list[str], int]:
    """Fetch the row's sample and let the model fill the recipe, retrying on the verifier's reasons:
    ``(proposal, reasons, fills used)``."""
    assert MAX_VERIFY_FAILS > 0, "a retry bound"
    assert isinstance(row, dict), "a row"
    sample = tools.fetch_sample(ctx, row["url"])
    if not sample.get("ok"):
        return None, [f"{row['url'][:80]}: {sample.get('detail') or sample.get('error')}"], fills
    reasons: list[str] = []
    for _ in range(MAX_VERIFY_FAILS):  # bounded: a fill and one retry
        if fills >= MAX_LOCAL_FILLS or _budget_stop(ctx):
            break
        fills += 1
        draft = _draft(ctx, sample["url"], sample["format"],
                       _reply(chat, prompt.local_fill_messages(ctx, sample["digest"], reasons), ctx, out))
        got = tools.try_recipe(ctx, draft) if draft else error("invalid_spec", "the model gave no pipeline and scene")
        reasons = got.get("reasons") or ([] if got.get("ok") else [str(got.get("detail"))])
        if got.get("ok") and got.get("verdict") == "pass":
            done = tools.propose_card(ctx, draft)
            done = _proposal_result(ctx, done, accept)
            if done.get("ok"):
                return done["proposal"], [], fills
            reasons = done.get("reasons") or [str(done.get("detail"))]
    return None, reasons, fills


def run_closed_world(ctx: ResearchCtx, chat: ChatFn, *, accept: AcceptFn | None = None) -> Outcome:
    """Local mode: code ranks the Library rows; the model picks one and fills the recipe from its sample. No loop, no web."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert callable(chat), "a chat seam"
    return _bounded(lambda out: _closed_world(ctx, chat, out, accept), ctx, Outcome("declined", "local"))


def _closed_world(ctx: ResearchCtx, chat: ChatFn, out: Outcome, accept: AcceptFn | None) -> Outcome:
    assert isinstance(out, Outcome), "an outcome"
    assert callable(chat), "a chat seam"
    reasons, fills = [], 0
    if len(ctx.question.get("entities") or []) >= 2:   # S7: comparing needs one search per thing; the local tier has none
        return _decline(ctx, out, ["the local tier compares only through the Library; no source reads them side by side"],
                        "compare_unbuildable")
    rows, why = _ranked(ctx)
    if not rows:
        return _decline(ctx, out, [why or "the Library has no source for this ask"], "research_declined_no_source")
    order = _pick(chat, ctx, rows, out)
    if not order:
        return _decline(ctx, out, ["no listed Library source can answer this ask"], "research_declined_no_source")
    for row in order:  # bounded by MAX_LOCAL_ROWS
        proposal, reasons, fills = _fill(chat, ctx, row, out, accept, fills)
        if proposal is not None:
            out.status, out.proposal, out.reasons = "proposed", proposal, []
            return out
        if _budget_stop(ctx) or fills >= MAX_LOCAL_FILLS:
            break
    return _decline(ctx, out, ["the local model could not build a card that passes the checks", *reasons],
                    "research_declined_checks")
