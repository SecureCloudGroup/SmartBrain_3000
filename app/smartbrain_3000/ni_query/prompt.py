"""PROMPT (Round 20, Q3a): the query layer's one model call — prompt design v2 of the Q2
measurement (chosen on its DEV split; 52/60 right answers few-shot on the clean TEST rows),
ported verbatim: one system message (role, compact rules, the closed ops and tokens, the
literal JSON skeleton with THIS call's answer names and cell paths) and one user message (the
reference instant with weekday and zone, k = 3 retrieved examples, the ask, the declared-answer
menu with cells as ``path:type:label:unit[codes]``, the parameter menu). The rule examples
are synthetic — none reproduces a labeled ask.

The retry (``llm.chat_json``'s one retry) states the violated RULE only: ``RETRY_SKELETON``
names no answer and ``normalize.structural_errors`` names no answer or cell. The Q2 retry
listed the companion answers and the model took one (7 right answers turned wrong).

``IR_SCHEMA`` is ir.schema.json in ``llm._validate``'s subset: type unions become ``anyOf``;
the token pattern, the id lengths, the limit range and the params bound are checked by
``normalize.structural_errors``. It carries no enum of answer names or cell paths — the
validator's messages quote enums, and they reach the retry.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo

from .decl import cells_of
from .recognize import words

OPS = ("=", "!=", "<", "<=", ">", ">=", "in", "contains", "names")
IR_KEYS = ("answer", "params", "select", "where", "time", "order", "limit", "agg")
POOL_KEYS = frozenset({"id", "kind", "ask", "ref", "menu", "ir"})
K_EXAMPLES = 3
_MAX_POOL = 400
TOKENS_LINE = ("now, today, tonight, tomorrow, yesterday, weekend, this_week, next_week, this_month, "
               "next_days:N, past_days:N, next_hours:N, past_hours:N, dow:mon, dow:tue, dow:wed, dow:thu, "
               "dow:fri, dow:sat, dow:sun, date:YYYY-MM-DD, month:YYYY-MM, year:YYYY")

RULES = """You turn a person's ask into one query object over a declared answer.
The data source is already chosen and its parameters (place, station, team, state, airport, dates) are already filled by code. You only decide which declared answer serves the ask and how its rows are filtered, cut in time, ordered, limited and counted.

Reply with ONE JSON object and nothing else: no prose, no code fences. It always has exactly these eight keys: answer, params, select, where, time, order, limit, agg.
Start from the default object below. Most asks change only answer and time. Change another key only when a rule below says the ask requires it.

RULES
answer: the ONE declared answer whose cells carry the asked quantity. A declared answer that already fits the ask by its name, label, filter or window wins over a general list plus where/time: a value answer with window tomorrow for "... tomorrow", an answer named next_departure for "when is the next train".
params: stays {}. Only a parameter marked "from text" may take words from the ask (for example {"topic": "rust"}); place, resolver, clock, default and source parameters are never written here.
select: stays [] unless the ask names one specific quantity and the chosen list/columns answer carries several quantities; then the cell path(s) of the named quantity only, in the asked order ("high temperatures" -> the high temperature cell). Never the time or date cell, never every cell, never the answer's own name. For a value answer: only other VALUE answer names the ask also asks for ("latitude and longitude" -> answer latitude, select ["longitude"]).
where: stays [] unless the ask states a filter the rows must meet:
  comparators: "cheaper than 50 dollars" -> {"col": "price", "op": "<", "value": 50}
  type codes: "only outbound trains" -> {"col": "direction", "op": "=", "value": "O"}
  subjects the parameters do not already fetch (a team, line, station on a league-wide or network-wide list): "Lakers" -> {"col": "*", "op": "names", "value": "Lakers"}
  categories: "severe thunderstorm warnings" -> {"col": "<the cell holding the alert name>", "op": "contains", "value": "Severe Thunderstorm Warning"}
  The place, station, team or state a parameter of this source already resolves is applied: never repeat it in where. A value answer has no cells: no where, no order.
time: stays null unless the ask has a time word. "latest", "recent", "newest", "current", "history", "trend", "forecast" alone are not time words.
  "right now", "at the moment", "currently", "now" -> {"from": "now", "to": "now"}
  "today" -> {"from": "today", "to": "today"}; the same token on both ends for "tonight", "tomorrow", "yesterday" ("last night" is yesterday)
  "this weekend", "the weekend" -> {"from": "weekend", "to": "weekend"} (never dow:sat/dow:sun)
  "this week" on a forecast -> {"from": "today", "to": "next_days:7"}; "this week" on a list of past events -> {"from": "past_days:7", "to": "today"}
  "over the past 90 days" -> {"from": "past_days:90", "to": "today"}; "this year" -> year:YYYY of the reference instant on both ends; "in 2030" -> {"from": "year:2030", "to": "year:2030"}
  "on Tuesday" -> {"from": "dow:tue", "to": "dow:tue"}; "this month" -> {"from": "this_month", "to": "this_month"}
  "next ..." on a list -> {"from": "now", "to": null}; a value answer that already is the next one (next game, next period) takes no time
order: stays [] except: rankings ("top 3 by points", "biggest", "largest", "most") -> the ranked cell "desc"; "latest", "newest", "recent" lists -> the time cell "desc"; "next ..." on a list -> the time cell "asc". A trend, history or forecast takes no order.
limit: stays null except a number in the ask ("top 3" -> 3) or the single next or last one on a list ("next ...", "last night's result" -> 1).
agg: stays "none" except "how many" / "number of" asks: then "count" with the LIST answer, never a declared count value.

OPS: "=", "!=", "<", "<=", ">", ">=", "in", "contains", "names"
  contains: the text cell contains the value. names: the row names this entity; col "*" (any text cell) or one text cell. in: the value is a list.
TOKENS: """ + TOKENS_LINE + """
Copy answer names and cell paths exactly as listed under THIS CALL'S IDS; never invent one."""
DEFAULT_OBJECT = ('{"answer": "%s", "params": {}, "select": [], "where": [], "time": null, "order": [], '
                  '"limit": null, "agg": "none"}')
RETRY_SKELETON = DEFAULT_OBJECT % "<one declared answer name>"
_RETRIEVAL_STOP = frozenset({"the", "a", "an", "in", "of", "on", "at", "for", "to", "is", "it", "are", "what", "whats",
                             "s", "how", "do", "does", "did", "when", "will", "be", "any", "there", "this", "from",
                             "by", "and", "me"})
_TOKEN_OR_NULL = {"anyOf": [{"type": "null"}, {"type": "string"}]}
IR_SCHEMA: dict = {
    "type": "object", "additionalProperties": False, "required": list(IR_KEYS),
    "properties": {
        "answer": {"type": "string"},
        "params": {"type": "object"},
        "select": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "where": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False, "required": ["col", "op", "value"],
            "properties": {"col": {"type": "string"}, "op": {"type": "string", "enum": list(OPS)},
                           "value": {"anyOf": [{"type": "number"}, {"type": "string"},
                                               {"type": "array", "maxItems": 12,
                                                "items": {"anyOf": [{"type": "number"}, {"type": "string"}]}}]}}}},
        "time": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
                                              "required": ["from", "to"],
                                              "properties": {"from": _TOKEN_OR_NULL, "to": _TOKEN_OR_NULL}}]},
        "order": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False, "required": ["col", "dir"],
            "properties": {"col": {"type": "string"}, "dir": {"type": "string", "enum": ["asc", "desc"]}}}},
        "limit": {"anyOf": [{"type": "null"}, {"type": "integer"}]},
        "agg": {"type": "string", "enum": ["none", "count"]}}}


def _cell_str(cell: dict) -> str:
    """``path:type:label`` (+ ``:unit``, + ``[codes]``) — one menu cell."""
    assert isinstance(cell, dict) and cell.get("path"), "a cell with a path"
    assert isinstance(cell["path"], str), "path must be a str"
    s = f'{cell["path"]}:{cell.get("type", "")}:{cell.get("label", "")}'
    if cell.get("unit"):
        s += f':{cell["unit"]}'
    return s + (f'[{cell["codes"]}]' if cell.get("codes") else "")


def answer_line(answer: dict) -> str:
    """One line of the declared-answer menu (the Q2 harness's ``answer_line``)."""
    assert isinstance(answer, dict) and answer.get("name"), "a named answer"
    assert answer.get("kind") in ("value", "list", "columns"), "a closed kind"
    parts = [f'- {answer["name"]} [{answer["kind"]}] "{answer.get("label", "")}"']
    if answer.get("window"):
        parts.append(f'window: {answer["window"]}')
    if answer.get("filter"):
        flt = answer["filter"]
        parts.append(f'filter: {flt["path"]} = {json.dumps(flt["equals"])}')
    if answer.get("axis"):
        parts.append(f'axis: {answer["axis"]["cell"]} ({answer["axis"].get("step", "")})')
    if answer.get("limit"):
        parts.append(f'limit: {answer["limit"]}')
    if answer["kind"] == "value":
        parts.append(f'type: {answer.get("type", "")}' + (f', unit: {answer["unit"]}' if answer.get("unit") else ""))
    else:
        parts.append("cells: " + ", ".join(_cell_str(c) for c in cells_of(answer)))
    return " | ".join(parts)


def param_line(param: dict) -> str:
    """One line of the parameter menu (design v2: code-filled parameters say so)."""
    assert isinstance(param, dict) and param.get("name"), "a named parameter"
    fill = param.get("fill") or {}
    assert isinstance(fill, dict), "fill must be a dict"
    src = fill.get("from", "")
    s = f'- {param["name"]} ({param.get("kind", "")}, from {src}'
    if src == "default":
        s += f' = {json.dumps(fill.get("value"))}'
    return s + (")" if src == "text" else ") - filled by code")


def text_params(params: list[dict]) -> list[str]:
    """Names of the parameters the ask itself may fill (``fill.from == "text"``)."""
    assert isinstance(params, list), "params must be a list"
    assert len(params) <= 40, "params bounded"
    return [p["name"] for p in params if isinstance(p, dict) and (p.get("fill") or {}).get("from") == "text"]


def abbreviated_menu(answers: list[dict]) -> str:
    """An example's menu: answer names, kinds, cell paths and declared filters."""
    assert isinstance(answers, list) and answers, "answers required"
    assert all(isinstance(a, dict) for a in answers), "answers must be dicts"
    out = []
    for a in answers:  # bounded by the declared answers
        if a["kind"] == "value":
            out.append(f'{a["name"]} [value]')
            continue
        s = f'{a["name"]} [{a["kind"]}: ' + ", ".join(c["path"] for c in cells_of(a))
        if a.get("filter"):
            s += f'; filter {a["filter"]["path"]} = {json.dumps(a["filter"]["equals"])}'
        out.append(s + "]")
    return "; ".join(out)


def skeleton(answers: list[dict], params: list[dict]) -> str:
    """The literal default object + THIS call's ids by role (design v2's ``skeleton_v2``)."""
    assert isinstance(answers, list) and answers, "answers required"
    assert isinstance(params, list), "params must be a list"
    values = [a["name"] for a in answers if a["kind"] == "value"]
    rows = [a for a in answers if a["kind"] != "value"]
    tp = text_params(params)
    lines = ["THE DEFAULT OBJECT for this call (all eight keys):",
             DEFAULT_OBJECT % f"<one of: {'|'.join(a['name'] for a in answers)}>", "THIS CALL'S IDS:"]
    if values:
        lines.append("- value answers (no cells: with one of these, where and order stay [] and limit stays null): "
                     + "|".join(values))
    lines += [f"- {a['kind']} answer {a['name']}, cell paths: {'|'.join(c['path'] for c in cells_of(a))}" for a in rows]
    for a in answers:  # bounded by the declared answers
        lst = next((r for r in rows if r.get("path") == a.get("path")), None)
        if a["kind"] == "value" and a.get("type") == "count" and lst:
            lines.append(f"- {a['name']} is the declared count of {lst['name']}: for \"how many\" use answer "
                         f"{lst['name']} with agg \"count\"")
    lines.append(f"- params: the text parameter {'|'.join(tp)} may hold words from the ask; every other parameter "
                 "is filled by code" if tp else "- params: this source has no text parameter, so params is {}")
    lines += ["Filled forms of the keys:",
              '"select": ["<a cell path of the chosen answer, or another value answer name>"]',
              '"where": [{"col": "<a cell path of the chosen answer, or *>", "op": "<one of: ' + "|".join(OPS)
              + '>", "value": <a number, "text" or ["a", "b"]>}]',
              '"time": {"from": "<token or null>", "to": "<token or null>"}',
              '"order": [{"col": "<a cell path of the chosen answer>", "dir": "<asc|desc>"}]',
              '"limit": <an integer>', '"agg": "count"']
    return "\n".join(lines)


def _retrieval_tokens(text: str) -> set[str]:
    """The harness's retrieval tokens: ASCII letter/digit runs of the lowercased text, stop words out."""
    assert isinstance(text, str), "text must be a str"
    toks = {w for w, _, _ in words(text.lower()) if w.isascii() and w.isalnum()}
    assert all(t for t in toks), "no empty token"
    return toks - _RETRIEVAL_STOP


def retrieve_examples(ask: str, kind: str, pool: list[dict], k: int = K_EXAMPLES) -> list[dict]:
    """k pool entries: same question kind first, then shared ask words (count, then Jaccard),
    then id; never the ask itself."""
    assert isinstance(ask, str) and ask.strip(), "ask required"
    assert isinstance(pool, list) and len(pool) <= _MAX_POOL, "pool bounded"
    mine, same = _retrieval_tokens(ask), ask.strip().casefold()
    scored = []
    for entry in pool:  # bounded by _MAX_POOL
        assert set(entry) >= POOL_KEYS, "a pool entry carries the closed keys"
        if str(entry["ask"]).strip().casefold() == same:
            continue
        theirs = _retrieval_tokens(str(entry["ask"]))
        inter, union = len(mine & theirs), len(mine | theirs)
        scored.append((-(entry["kind"] == kind), -inter, -(inter / union if union else 0.0), str(entry["id"]), entry))
    scored.sort(key=lambda s: s[:4])
    return [s[4] for s in scored[:k]]


def ir_json(ir: dict) -> str:
    """The IR as one JSON line, keys in the closed order."""
    assert isinstance(ir, dict), "ir must be a dict"
    assert all(k in ir for k in IR_KEYS), "ir carries every key"
    return json.dumps({k: ir[k] for k in IR_KEYS}, ensure_ascii=False)


def _day_line(day: dt.date) -> str:
    assert isinstance(day, dt.date), "day must be a date"
    assert day.year >= 1970, "a modern date"
    return f"{day.strftime('%A')} {day.isoformat()}"


def build_messages(ask: str, *, source: dict, answers: list[dict], params: list[dict], now: dt.datetime,
                   zone: str, examples: list[dict]) -> list[dict]:
    """[system, user] exactly as design v2 sent them."""
    assert isinstance(ask, str) and ask.strip(), "ask required"
    assert isinstance(source, dict) and source.get("id"), "source with an id required"
    local = now.astimezone(ZoneInfo(zone))
    parts = [f"Reference instant: {_day_line(local.date())}, zone {zone}"]
    if examples:
        parts.append("\nEXAMPLES (other asks with their objects)")
        for i, e in enumerate(examples, 1):  # bounded by K_EXAMPLES
            parts.append(f"Example {i} (reference {_day_line(dt.date.fromisoformat(str(e['ref'])))})\n"
                         f"Ask: {json.dumps(e['ask'])}\nAnswers: {e['menu']}\nObject: {ir_json(e['ir'])}")
        parts.append("\nNOW THIS ASK")
    menu = "\n".join(answer_line(a) for a in answers)
    pmenu = "\n".join(param_line(p) for p in params) if params else "(none)"
    parts.append(f"Ask: {json.dumps(ask)}\nSource: {source['id']} ({source.get('name', '')})\n"
                 f"Declared answers:\n{menu}\nParameters:\n{pmenu}\nReply with the JSON object only.")
    return [{"role": "system", "content": RULES + "\n\n" + skeleton(answers, params)},
            {"role": "user", "content": "\n".join(parts)}]


def prompt_sha(messages: list[dict]) -> str:
    """The Q2 harness's prompt fingerprint (12 hex): equal messages ⇔ equal sha."""
    assert isinstance(messages, list) and messages, "messages required"
    assert all(isinstance(m, dict) for m in messages), "messages must be dicts"
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
