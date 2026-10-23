"""The research tools as Anthropic tool-use definitions, byte-stable so the loop can mark them ``cache_control``.

``TOOLS_JSON`` is the canonical text (tools sorted by name, keys sorted, no spaces); ``tool_schemas()`` parses it,
so the list handed to the gateway always serialises to the same bytes. Changing any text here changes the
cached prefix on purpose and trips ``test_ni_research_tools``'s pinned hash.
"""
from __future__ import annotations

import hashlib
import json

from . import tools
from .ctx import MAX_MEMO, MAX_REPLAYS, ResearchCtx, error

_DRAFT = {"type": "object",
          "description": "A card spec: source (one http_json URL you fetched), pipeline (extract then transform) and scene. "
                         "No llm stage, no $secret, no interval, no headers."}


def _tool(name: str, description: str, prop: str, schema: dict) -> dict:
    assert name and description, "name and description required"
    assert prop and schema, "one named property"
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": {prop: schema}, "required": [prop],
                             "additionalProperties": False}}


_COMPARE = {"type": "object", "additionalProperties": False, "required": ["metric", "things"],
            "description": "One card for an ask that compares named things: the same number for each, side by side.",
            "properties": {
                "metric": {"type": "string", "maxLength": 40, "description": "What is compared, in a few words."},
                "unit": {"type": "string", "maxLength": 20, "description": "The unit of the number (optional)."},
                "things": {"type": "array", "minItems": 2, "maxItems": 4, "items": {
                    "type": "object", "additionalProperties": False, "required": ["name", "url", "path"],
                    "properties": {
                        "name": {"type": "string", "maxLength": 80, "description": "Which asked thing this is."},
                        "url": {"type": "string", "maxLength": 2000, "description": "The fetched address for this thing."},
                        "path": {"type": "string", "maxLength": 200, "description": "Path to the number in its sample."}}}}}}

_TOOLS = [
    _tool("fetch_sample", "Fetch one data sample (JSON, CSV, feed, XML or a web page's data tables) with a plain GET and read a fenced digest. "
          "Only a URL library_search or web_search returned, exactly as returned. At most 8 per run.",
          "url", {"type": "string", "maxLength": 2000}),
    _tool("library_search", "Find vetted Library sources for a question. Highest trust: use first; "
          "their URLs may then be fetched.", "query", {"type": "string", "maxLength": tools.MAX_QUERY}),
    _tool("propose_card", "Finish: re-run the draft on the sample and, when every check passes, seal it "
          "as the proposal. The run ends here.", "draft_spec", _DRAFT),
    _tool("web_search", "Search the web for a published data source (a JSON, RSS, CSV or XML address) when the "
          "Library has none. Returns a fenced list of titles, snippets and addresses; they may then be fetched. "
          "At most 3 per run, cloud tier only.", "query", {"type": "string", "maxLength": tools.MAX_QUERY}),
    _tool("propose_compare", "Finish an ask that compares two to four named things: one fetched address per thing "
          "(each found by a library_search or web_search for that thing alone, then fetched) and the path to the same "
          "number in each sample. The program names the rows from the question, checks every thing is shown with its "
          "own value and seals the card. The run ends here.", "compare", _COMPARE),
    _tool("resolve_entity", "Resolve a place or named entity to the Library's canonical entry (no network).",
          "text", {"type": "string", "maxLength": 120}),
    _tool("try_recipe", "Run a draft spec on the last fetched sample (no network). Returns the card rows and "
          "the verifier's reasons, or a typed error.", "draft_spec", _DRAFT),
]
TOOLS_JSON = json.dumps(sorted(_TOOLS, key=lambda t: t["name"]), sort_keys=True, separators=(",", ":"))
_DISPATCH = {"fetch_sample": ("url", tools.fetch_sample), "library_search": ("query", tools.library_search),
             "propose_card": ("draft_spec", tools.propose_card), "resolve_entity": ("text", tools.resolve_entity),
             "propose_compare": ("compare", tools.propose_compare),
             "try_recipe": ("draft_spec", tools.try_recipe),
             "web_search": ("query", tools.web_search)}


_MEMO_TOOLS = frozenset({"web_search", "library_search", "resolve_entity", "try_recipe"})   # deterministic on the ctx


def tool_schemas() -> list[dict]:
    """The tool definitions, key-sorted and name-ordered, ready for the gateway."""
    assert TOOLS_JSON, "the canonical text"
    out = json.loads(TOOLS_JSON)
    assert [t["name"] for t in out] == sorted(_DISPATCH), "the schemas and the dispatch table agree"
    return out


def run_tool(ctx: ResearchCtx, name: str, args: object) -> dict:
    """Run the tool the model named with its arguments; an unknown tool or a wrong argument set is a typed error."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    entry = _DISPATCH.get(name) if isinstance(name, str) else None
    if entry is None:
        return error("unknown_tool", str(name)[:60])
    prop, fn = entry
    if not isinstance(args, dict) or set(args) != {prop}:
        return error("invalid_args", f"{name} takes exactly {prop}")
    if name not in _MEMO_TOOLS:
        return fn(ctx, args[prop])
    dump = json.dumps(args[prop], sort_keys=True, default=str)   # the WHOLE canonical text, digested: never a prefix
    key = (name, hashlib.sha256(dump.encode("utf-8", "replace")).hexdigest(), tuple(ctx.samples))
    prior = ctx.memo.get(key)
    if prior is not None:   # the same call on the same samples answers the same: no search, no turn (a few times)
        if ctx.replays >= MAX_REPLAYS:
            out = ctx.spend(name)
            if out is not None:
                return out
        ctx.replays += 1
        return {**prior, "repeat": "identical to an earlier call; the result is unchanged, so change something"}
    got = fn(ctx, args[prop])
    if got.get("error") not in ("budget_exhausted", "search_unavailable") and len(ctx.memo) < MAX_MEMO:
        ctx.memo[key] = got
    return got
