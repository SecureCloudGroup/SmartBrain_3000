#!/usr/bin/env python3
"""Tiny mock OpenAI-compatible gateway for deterministic, synthetic GIF demos.

Stands in for Bifrost (SMARTBRAIN_LLM_GATEWAY_URL) AND a host Ollama (port 11434),
so the real app renders canned chat/tool-call/model-list responses in full
isolation — no real provider keys, no touching a real Bifrost. See README.md.

Endpoints:
  GET  /reset                      -> clear configured providers (use before clip 02)
  GET  /v1/models                  -> catalog for the currently "configured" providers
  POST /v1/chat/completions        -> stream (SSE) or JSON; emits add_task/kb_search tool calls, and
                                      the Neural Interface's canned JSON turns (clip 12, NI_ASKS)
  POST /v1/embeddings              -> a fixed vector (reindex + semantic search work)
  POST /api/providers              -> mark a provider configured (register_ollama/set_provider)
  POST /api/providers/<n>/keys     -> 200
  DELETE /api/providers/<n>        -> unconfigure
  GET  /api/tags  (port 11434)     -> Ollama detection ("Found Ollama running")

Run:  python3 mock_gateway.py 38099   (also binds :11434 for Ollama detection)
"""
import ast
import json
import re
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"providers": set()}

OLLAMA_MODEL = {"id": "ollama/llama3.1:8b", "name": "Llama 3.1 8B", "supported_methods": ["chat"]}
EMBED_MODEL = {"id": "ollama/nomic-embed-text:v1.5", "name": "nomic-embed-text:v1.5", "supported_methods": ["embed"]}
ANTHROPIC_MODEL = {"id": "anthropic/claude-3-5-sonnet-latest", "name": "Claude 3.5 Sonnet", "supported_methods": ["chat"]}


def models_payload():
    data = []
    if "ollama" in STATE["providers"]:
        data += [OLLAMA_MODEL, EMBED_MODEL]
    if "anthropic" in STATE["providers"]:
        data.append(ANTHROPIC_MODEL)
    return {"object": "list", "data": data}


def _tomorrow():
    return (date.today() + timedelta(days=1)).isoformat()


# ---- Neural Interface (clip 12) -------------------------------------------------------------------
# The NI flow's model turns are closed JSON shapes; each branch below keys on the turn's own prompt
# marker and replies with valid JSON for exactly the asks clip 12 types (an unknown ask gets a plain
# intent, the query floor and the floor's design). Everything else — locate, the address, the fetch,
# the build, the form design, the checks — is the app's own code.
NI_INTENT = "Classify a dashboard-card request."                       # ni_flow._INTENT_PROMPT
NI_QUERY = "You turn a person's ask into one query object"             # ni_query.prompt.RULES
NI_PRESENT = "You choose how a personal dashboard card presents live data."   # ni_forms.present
NI_FIT = "You check whether the chosen answers actually answer"        # ni_forms.verdict


def _ir(answer, **clauses):
    """A query IR: the prompt's default object with the clauses its RULES give this ask."""
    return {"answer": answer, "params": {}, "select": [], "where": [], "time": None, "order": [],
            "limit": None, "agg": "none", **clauses}


NI_ASKS = {  # the ask as clip 12 types it (lowercased) -> what a local model returns at each turn
    "uv index in phoenix this afternoon": {
        "intent": {"subject": "UV index in Phoenix", "wants": ["uv index"], "threshold": None,
                   "place": "Phoenix", "names": ["Phoenix"], "display_hint": "value"},
        "query": _ir("uv_max_today", time={"from": "today", "to": "today"}),
        "form": None},
    "most recent earthquakes near los angeles above magnitude 3": {
        "intent": {"subject": "Earthquakes near Los Angeles", "wants": ["earthquakes", "magnitude"],
                   "threshold": 3, "place": "Los Angeles", "names": ["Los Angeles"],
                   "display_hint": "list"},
        "query": _ir("quakes", where=[{"col": "properties.mag", "op": ">", "value": 3}],
                     order=[{"col": "properties.time", "dir": "desc"}]),
        "form": "ranked_list"},
    "what time does the sun set in denver tomorrow": {
        "intent": {"subject": "Sunset in Denver", "wants": ["sunset time"], "threshold": None,
                   "place": "Denver", "names": ["Denver"], "display_hint": "value"},
        "query": _ir("sunset_tomorrow", time={"from": "tomorrow", "to": "tomorrow"}),
        "form": None},
}


def _ni_ask(text, pattern):
    """The canned turns for the ask the prompt quotes last (the query prompt's few-shot examples
    quote other asks first), or None for an ask clip 12 doesn't type."""
    quoted = re.findall(pattern, text, re.MULTILINE)
    try:
        ask = str(ast.literal_eval(quoted[-1])) if quoted else ""
    except (ValueError, SyntaxError):
        ask = quoted[-1].strip("'\"")
    return ask, NI_ASKS.get(ask.strip().rstrip("?.").lower())


def _ni_intent(text):
    """Stage 1: the closed intent for the ask on the prompt's ``Request:`` line."""
    ask, known = _ni_ask(text, r"^Request: (.+)$")
    intent = known["intent"] if known else {"subject": ask[:60], "wants": [ask[:40] or "value"],
                                            "threshold": None, "place": None, "names": [],
                                            "display_hint": "value"}
    return json.dumps({"kind": "external_data", "cadence_minutes": 15, **intent})


def _ni_query(text):
    """The query layer: the IR the prompt's RULES give the ask; an unknown ask gets a non-JSON line,
    so the deterministic rules floor plans it (ni_query.plan, the documented no-model path)."""
    _ask, known = _ni_ask(text, r"^Ask: (.+)$")
    return json.dumps(known["query"]) if known else "(demo gateway: no query model here)"


def _between(text, start, end):
    """The substring of ``text`` after the first ``start`` and before the next ``end`` ("" when absent)."""
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i)
    return text[i:j] if j >= 0 else ""


def _ni_present(text):
    """PRESENT: the ask's form when the menu offers it, else c0 — the rules floor's top-ranked
    candidate (enumerate ranks before it names ids, so c0 is stable however the menu is shuffled) —
    with every key the skeleton names."""
    _ask, known = _ni_ask(text, r"^Ask: (.+)$")
    # plain string slicing, not regexes: the skeleton is machine-written and CodeQL flags a
    # backtracking pattern over a prompt-sized string (py/polynomial-redos)
    ids = _between(text, '"pick": "<one of ', '>"').split("|")
    opts = re.search(r"^Options: (\[.*\])$", text, re.MULTILINE)
    menu = json.loads(opts.group(1)) if opts else []
    pick = next((o["id"] for o in menu if known and o["form"] == known["form"]),
                "c0" if "c0" in ids else sorted(ids)[0])
    labels = _between(text, '"labels": {', "}")
    keys = [part.split(":", 1)[0].strip().strip('"')
            for part in labels.split(",") if '"<key or ask>"' in part]
    fits = [{"cand": i, "answers_ask": "yes" if i == pick else "partly"} for i in ids]
    return json.dumps({"intent": next((o["intent"] for o in menu if o["id"] == pick), "now"), "fits": fits,
                       "pick": pick, "second": None, "primary_field": None,
                       "labels": {k: "key" for k in keys}, "uncovered_wants": [], "none_fits": False})


def _ni_reply(messages):
    """The canned NI turn for this conversation, or None when it is not an NI prompt."""
    text = "\n\n".join(str(m.get("content") or "") for m in messages)
    if NI_INTENT in text:
        return _ni_intent(text)
    if NI_QUERY in text:
        return _ni_query(text)
    if NI_PRESENT in text:
        return _ni_present(text)
    if NI_FIT in text:  # the card passed every code check; the advisory verdict agrees
        return json.dumps({"answers_ask": "yes", "missing": [], "wrong": [], "evidence": []})
    return None


def decide(messages):
    """Return {'text': ...} or {'tool': {'name','arguments'}} from the conversation."""
    ni = _ni_reply(messages)
    if ni is not None:
        return {"text": ni}
    last_user, has_tool_result, last_tool = "", False, ""
    for m in messages:
        if m.get("role") == "user":
            last_user = m.get("content") or ""
        if m.get("role") == "tool":
            has_tool_result = True
        if m.get("role") == "assistant" and m.get("tool_calls"):
            last_tool = (m["tool_calls"][-1].get("function") or {}).get("name", "")
    lu = last_user.lower()
    if has_tool_result:
        if last_tool == "kb_search" or "lease" in json.dumps(messages).lower():
            return {"text": "Your apartment lease runs 12 months at $1,800/month, due on the 1st, "
                            "with 60 days' notice to vacate (landlord: Pat Rivera)."}
        if last_tool == "add_task":
            return {"text": "Done — I’ve added that to your planner."}
        return {"text": "All set."}
    if "milk" in lu or "dentist" in lu or ("add" in lu and "task" in lu):
        title = "Buy milk" if "milk" in lu else ("Call the dentist" if "dentist" in lu else "New task")
        args = {"title": title}
        if "dentist" in lu or "tomorrow" in lu:
            args["due_date"] = _tomorrow()
        return {"tool": {"name": "add_task", "arguments": args}}
    if "lease" in lu or "knowledge" in lu:
        return {"tool": {"name": "kb_search", "arguments": {"query": "apartment lease terms"}}}
    if "help" in lu or "what can you do" in lu:
        return {"text": "I can chat, search your private Knowledge, track tasks in the Planner, and run "
                        "scheduled briefings. Anything that changes data or reaches out waits for your approval first."}
    if "summar" in lu and "task" in lu:
        return {"text": "You have 3 open tasks: Call the dentist (today), Submit expense report (this week), "
                        "and Buy birthday gift (no date)."}
    return {"text": "Sure — happy to help. Ask me anything, or try the suggestions above."}


def _tool_call_obj(name, args):
    return {"index": 0, "id": "call_demo1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path.startswith("/reset"):
            STATE["providers"].clear()
            return self._json({"ok": True})
        if self.path.rstrip("/") == "/v1/models":
            return self._json(models_payload())
        if self.path.startswith("/api/tags"):  # Ollama detection
            return self._json({"models": [{"name": "llama3.1:8b"}, {"name": "nomic-embed-text:v1.5"}]})
        return self._json({"ok": True})

    def do_DELETE(self):
        parts = self.path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "providers":
            STATE["providers"].discard(parts[2])
        return self._json({"ok": True})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/api/providers":
            name = body.get("provider")
            if name:
                STATE["providers"].add(name)
            return self._json({"id": name or "p", "provider": name}, 201)
        if path.startswith("/api/providers/") and path.endswith("/keys"):
            return self._json({"id": "key_demo", "name": body.get("name", "k")}, 201)
        if path == "/v1/embeddings":
            return self._json({"object": "list", "model": body.get("model", "mock"),
                               "data": [{"object": "embedding", "index": 0, "embedding": [0.0123] * 768}]})
        if path == "/v1/chat/completions":
            return self._chat(body)
        return self._json({"ok": True})

    def _chat(self, body):
        messages = body.get("messages") or []
        has_tools = bool(body.get("tools"))
        stream = bool(body.get("stream"))
        d = decide(messages)
        use_tool = "tool" in d and has_tools
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def send(obj):
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                self.wfile.flush()

            if use_tool:
                tc = _tool_call_obj(d["tool"]["name"], d["tool"]["arguments"])
                send({"choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]})
                send({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            else:
                for word in (d.get("text") or "").split(" "):
                    send({"choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]})
                send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        # non-streaming (chat_with_tools / chat)
        if use_tool:
            msg = {"role": "assistant", "content": None,
                   "tool_calls": [_tool_call_obj(d["tool"]["name"], d["tool"]["arguments"])]}
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": d.get("text") or "Sure."}
            finish = "stop"
        return self._json({"id": "chatcmpl-demo", "object": "chat.completion",
                           "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                           "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})


def serve(port):
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    gw = int(sys.argv[1]) if len(sys.argv) > 1 else 38099
    threading.Thread(target=serve, args=(11434,), daemon=True).start()  # Ollama detection
    print(f"mock gateway on :{gw} + ollama on :11434", flush=True)
    serve(gw)
