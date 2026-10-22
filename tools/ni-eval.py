#!/usr/bin/env python3
"""NI eval harness (plan S0): question -> card quality, scored against a gold file, record/replay.

Subcommands
  convert   rebuild app/tests/ni_eval/gold/gold.json from the committed blind sets + verdicts
  score     score a results file (list of run dicts, as ni-live-e2e writes) against the gold
  record    run asks LIVE (real model, real network) and save every model reply and fetch response
            into a cassette directory (one <id>.json per ask) next to the results
  replay    run the same asks OFFLINE from a cassette: deterministic, no model, no network

Seams (nothing in app/smartbrain_3000 is edited): model replies are recorded at the ``gateway_call``
the flow already accepts; fetch responses at ``netguard.safe_fetch_json / safe_fetch_text /
safe_fetch / safe_fetch_feed / safe_post_json``, patched for the span of one ask. Headers (keys)
are never recorded and secret-looking query parameters are redacted from the cassette key.
Classes per ask: right / partial / wrong / links / declined / waiting / error (see ``score_ask``). A declined card
(the v2 honest "I couldn't build a reliable card for this") is its own class: "right" when the gold expects a decline
(``expected_outcome: decline``), else "declined", which is neither right nor wrong.
``--flow v1|v2`` on record and replay sets the ``ni_flow`` flag (env ``SMARTBRAIN_NI_FLOW``) for the run.
``--research cloud|local|off`` (S4c, v2 only) sets the researcher tier for the run (env ``SMARTBRAIN_NI_RESEARCH``)
and routes its model turns through the cassette like every other model reply: ``cloud`` uses ``--research-model``
(default anthropic/claude-sonnet-5-5) with the eval operator's consent, ``local`` the closed-world mode on
``--model``, ``off`` no researcher. Each ask's cassette gains a ``research`` list (the raw model responses, usage
included) and the run a ``research`` block (calls, tokens, cache reads, cost) the summary prints.
Open web (S5b): the cloud tier may also ``web_search`` and fetch pages. Each search is recorded in the cassette's
``search`` list (the service's results, or its failure) and served from it on replay, keyed by the query like a
fetch; the page fetches ride the netguard patch above. ``--research-web on|off`` (default on; env
``SMARTBRAIN_NI_RESEARCH_WEB``) switches the web off for a Library-only comparison run. Local mode never has the web.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import time
from collections.abc import Callable, Iterator

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

FIXTURES = REPO / "app" / "tests" / "fixtures"
EVAL_DIR = REPO / "app" / "tests" / "ni_eval"
GOLD_PATH = EVAL_DIR / "gold" / "gold.json"
CLASSES = ("right", "partial", "wrong", "links", "declined", "waiting", "error")
_RANK = {"right": 0, "partial": 1, "wrong": 2}
_OUTCOME = {
    "live": "card", "ready": "card", "awaiting-yes": "card",
    "links": "links", "source": "links", "needs-key": "links", "needs-email": "links",
    "no-source": "links", "unsupported": "declined", "declined": "declined",
    "waiting-pick": "waiting",  # a run parked at the source pick (state source + awaiting_pick)
    "asks": "waiting",          # a run stopped on a question the user answers (build with a model? what date?)
}
SOURCE_REVIEW = "built source not in the gold's acceptable sources"
DECLINE_RIGHT = "declined, as the gold expects"
_SECRET_Q = re.compile(r"(?i)(key|token|secret|auth|sig|appid|password)")


# ---------------------------------------------------------------------------------------------
# Scoring (pure)
# ---------------------------------------------------------------------------------------------

def outcome_class(run: dict) -> str:
    """card | links | declined | waiting | error for one run result (anything unknown is an error)."""
    assert isinstance(run, dict), "run result required"
    out = str(run.get("outcome") or "")
    assert isinstance(out, str), "outcome is text"
    return _OUTCOME.get(out, "error")


def expects_decline(gold: dict) -> bool:
    """True when the gold wants an honest decline: ``expected_outcome: decline``, or a card is a forbidden outcome."""
    assert isinstance(gold, dict), "gold required"
    return gold.get("expected_outcome") == "decline" or "card" in (gold.get("forbidden_outcomes") or [])


def flow_outcome(rec: dict) -> str:
    """The outcome word of a finished flow record: ``live`` (ready), ``declined`` (the v2 declined card), ``asks`` (a
    terminal that holds a question for the user: waiting, not a decline), else the state itself."""
    assert isinstance(rec, dict), "record required"
    state = str(rec.get("state") or "")
    if state == "ready":
        return "live"
    if state == "unsupported" and isinstance(rec.get("_question"), dict):
        return "asks"
    return state


def card_text(run: dict) -> str:
    """Lower-cased words a reader sees: card text, preview values, tapped source labels."""
    assert isinstance(run, dict), "run result required"
    parts: list[str] = [str(t) for t in run.get("scene_text") or []]
    preview = run.get("preview")
    parts.append(json.dumps(preview, sort_keys=True, default=str) if preview else "")
    for tap in run.get("tapped") or []:
        parts += [str(tap.get("title") or ""), str(tap.get("label") or "")]
    assert all(isinstance(p, str) for p in parts), "text parts"
    return " ".join(parts).lower()


def built_source(run: dict) -> str:
    """Lower-cased words naming the ONE source the card was built from (its address, filled params, row
    label), the same in v1 and v2: the last tapped row (a v1 tap that failed, or a row tried before it, is
    not the source), or the web page / dataset an open-path card says it came from. With neither, the run's
    own source string (the url-mode harness)."""
    assert isinstance(run, dict), "run result required"
    origin = run.get("from") or {}
    taps = run.get("tapped") or []
    if origin and origin.get("kind") != "library":
        bits = [str(origin.get("host") or ""), str(origin.get("title") or "")]
    elif taps:
        tap = taps[-1]
        bits = [str(tap.get("url") or ""), json.dumps(tap.get("params") or {}, sort_keys=True),
                str(tap.get("label") or "")]
    else:
        bits = [str(run.get("source") or "")]
    assert all(isinstance(b, str) for b in bits), "text parts"
    return " ".join(bits).lower()


def _evidence(run: dict) -> str:
    """Request-side words: the built source's address and params, and the displayed card text (not the
    raw preview payload, whose many numbers would satisfy any threshold)."""
    assert isinstance(run, dict), "run result required"
    bits = [built_source(run), *(str(t) for t in run.get("scene_text") or [])]
    return " ".join(bits).lower()


def _found(names: list[str], text: str) -> int:
    assert isinstance(names, list) and isinstance(text, str), "names + text"
    return sum(1 for n in names if str(n).lower() in text)


def run_checks(run: dict, gold: dict) -> dict:
    """Constraint checks: True pass, False fail, None unknowable from this run's data.

    ``places`` is a (found, asked) pair. ``threshold`` looks for the number in the request or the
    card; ``compare_n`` for that many asked entities on the card; ``source`` for an acceptable
    host / Library id in the source the card was built from (``built_source``).
    """
    assert isinstance(run, dict) and isinstance(gold, dict), "run + gold required"
    cons = gold.get("constraints") or {}
    text, evid = card_text(run), _evidence(run)
    have = bool(text.strip())
    checks: dict = {"places": None, "threshold": None, "compare_n": None, "units": None, "source": None}
    places = cons.get("places") or []
    if places and have:
        checks["places"] = (_found(places, text), len(places))
    if cons.get("threshold") is not None and have:
        num = re.escape(f"{cons['threshold']:g}")
        checks["threshold"] = re.search(rf"(?<![\d.]){num}(?:\.0+)?(?![\d])", evid) is not None
    ents = cons.get("entities") or []
    if cons.get("compare_n") and ents and have:
        checks["compare_n"] = _found(ents, text) >= int(cons["compare_n"])
    if cons.get("units") and have:
        checks["units"] = str(cons["units"]).lower() in text
    if gold.get("acceptable_sources"):
        built = built_source(run)
        checks["source"] = any(str(s).lower() in built for s in gold["acceptable_sources"])
    return checks


def _carried(run: dict, gold: dict) -> str | None:
    """The human verdict stands only for the very card the human read: same source (and the same
    card text when the baseline recorded its hash)."""
    assert isinstance(run, dict) and isinstance(gold, dict), "run + gold required"
    verdict, base = gold.get("human_verdict"), gold.get("baseline") or {}
    if verdict not in _RANK:
        return None
    sha = base.get("card_sha")
    if sha:
        return verdict if _sha(card_text(run)) == sha else None
    squash = lambda t: re.sub(r"[^a-z0-9]", "", str(t).lower())
    key = squash(base.get("source_id") or base.get("source_host") or "")
    return verdict if key and key in squash(_evidence(run)) else None


def _verdict_from_checks(checks: dict) -> tuple[str, str]:
    assert isinstance(checks, dict), "checks required"
    failed = [k for k in ("threshold", "compare_n", "units") if checks.get(k) is False]
    if failed:
        return "wrong", "failed " + ",".join(failed)
    if checks.get("source") is False:  # a narrow gold list is no evidence the card is wrong: a human looks
        return "partial", SOURCE_REVIEW
    if checks["places"] is not None:
        got, total = checks["places"]
        if got == 0:  # a station / grid label may not name the place: not proof of a wrong card
            return "partial", "no asked place named on the card"
        if got < total:
            return "partial", f"{got}/{total} asked places on the card"
    if any(v is not None for v in checks.values()):
        return "right", "constraints pass"
    return "partial", "unchecked"


def score_ask(run: dict, gold: dict) -> dict:
    """Class of one run: {id, kind, cls, reason, checks, needs_review}. Pure.

    Not a card: links (links, needs key), declined, waiting (parked at the source pick), or error. A card is wrong if the
    gold forbids a card or a checkable constraint fails; partial if only some asked places show, or the
    built source is not in the gold's (possibly narrow) acceptable list (needs_review);
    right if checks ran and passed. With nothing checkable it is "partial / unchecked" (needs a
    human). The same card a human already judged (same source, same text) takes that verdict.
    """
    assert isinstance(run, dict) and isinstance(gold, dict), "run + gold required"
    res = {"id": gold.get("id"), "kind": gold.get("kind"), "checks": {}, "needs_review": False}
    oc = outcome_class(run)
    if oc != "card":
        cls = {"links": "links", "declined": "declined", "waiting": "waiting"}.get(oc, "error")
        if cls == "declined" and expects_decline(gold):  # the honest answer to an ask whose gold is a decline
            return {**res, "cls": "right", "reason": DECLINE_RIGHT}
        return {**res, "cls": cls, "reason": str(run.get("outcome") or "")}
    if expects_decline(gold):
        return {**res, "cls": "wrong", "reason": "forbidden outcome: card"}
    checks = run_checks(run, gold)
    cls, reason = _verdict_from_checks(checks)
    carried = _carried(run, gold)
    if carried:  # a human read this very card: that verdict is authoritative over the heuristics
        cls, reason = carried, "human verdict carried (same card)"
    review = reason in ("unchecked", SOURCE_REVIEW)  # a carried human verdict has been reviewed already
    return {**res, "cls": cls, "reason": reason, "checks": checks, "needs_review": review}


def aggregate(scored: list[dict]) -> dict:
    """Counts and R/P/W/L/D per kind plus an ALL row. R counts a decline the gold expects (``decl_ok`` says how many of
    the right ones were that); L = links + declined, the plan's "links/declined" line; D = declined alone (an ask the
    gold left open or expected a card for: neither right nor wrong); errors are counted apart; ``needs_review``
    counts the runs flagged for a human look, whatever their class."""
    assert isinstance(scored, list), "scored list required"
    table: dict[str, dict] = {}
    for item in scored:
        assert item["cls"] in CLASSES, f"unknown class {item['cls']}"
        for key in (str(item.get("kind") or "unknown"), "ALL"):
            row = table.setdefault(key, {"n": 0, "needs_review": 0, "decl_ok": 0, **{c: 0 for c in CLASSES}})
            row["n"] += 1
            row[item["cls"]] += 1
            row["decl_ok"] += item.get("reason") == DECLINE_RIGHT
            row["needs_review"] += bool(item.get("needs_review"))  # a card a human still has to look at
    for row in table.values():
        n = row["n"]
        row["R"], row["P"], row["W"] = (row[c] / n for c in ("right", "partial", "wrong"))
        row["L"] = (row["links"] + row["declined"]) / n
        row["D"] = row["declined"] / n
    return table


def format_table(table: dict) -> str:
    assert isinstance(table, dict), "table required"
    lines = [f"{'kind':<14}{'n':>4}{'right':>7}{'part':>6}{'wrong':>7}{'review':>7}{'links':>7}{'decl':>6}{'wait':>6}{'err':>5}{'dok':>5}"]
    for key in sorted(table, key=lambda k: (k == "ALL", k)):
        r = table[key]
        lines.append(f"{key:<14}{r['n']:>4}{r['right']:>7}{r['partial']:>6}{r['wrong']:>7}"
                     f"{r['needs_review']:>7}{r['links']:>7}{r['declined']:>6}{r['waiting']:>6}{r['error']:>5}{r['decl_ok']:>5}")
    assert lines, "header at least"
    return "\n".join(lines)


def _sha(text: str) -> str:
    assert isinstance(text, str), "text required"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------
# Record / replay of fetches and model replies
# ---------------------------------------------------------------------------------------------

class CassetteMiss(Exception):
    """Replay was asked for something the cassette never recorded."""


def redact_url(url: str) -> str:
    """Drop the value of secret-looking query parameters so a key never lands in a cassette."""
    assert isinstance(url, str), "url required"
    if "?" not in url:
        return url
    head, query = url.split("?", 1)
    out = []
    for part in (p for p in query.split("&") if p):
        name = part.split("=", 1)[0]
        out.append(f"{name}=REDACTED" if "=" in part and _SECRET_Q.search(name) else part)
    return head + "?" + "&".join(out)


def fetch_key(fn: str, url: str, payload: object = None) -> str:
    assert fn and isinstance(url, str), "fn + url required"
    extra = _sha(json.dumps(payload, sort_keys=True, default=str)) if payload is not None else ""
    return f"{fn} {redact_url(url)} {extra}".strip()


class Session:
    """One ask's cassette. ``recording`` appends; replay reads. Plain data, JSON on disk."""

    def __init__(self, ask_id: str, data: dict | None = None, recording: bool = False) -> None:
        assert ask_id, "ask id required"
        self.ask_id, self.recording = ask_id, recording
        self.data = data or {"ask_id": ask_id, "meta": {}, "fetches": [], "model": []}
        self.data.setdefault("research", [])
        self.data.setdefault("search", [])
        self._search_seen: dict[str, int] = {}
        self._fetch_seen: dict[str, int] = {}
        self._model_i = 0
        self._research_i = 0
        self.drift = 0
        self.unrecorded = 0

    def model_call(self, prompt: str, real: Callable[[str], str] | None = None) -> str:
        """Record the live reply, or serve the next recorded one (a changed prompt is counted)."""
        assert isinstance(prompt, str), "prompt required"
        if self.recording:
            assert real is not None, "recording needs the real model"
            reply = real(prompt)
            self.data["model"].append({"sha": _sha(prompt), "reply": reply})
            return reply
        if self._model_i >= len(self.data["model"]):
            raise CassetteMiss(f"{self.ask_id}: model call #{self._model_i + 1} not in the cassette")
        rec = self.data["model"][self._model_i]
        self._model_i += 1
        if rec.get("sha") and rec["sha"] != _sha(prompt):
            self.drift += 1  # the prompt changed since recording: served anyway, reported
        return str(rec["reply"])

    def research_turn(self, model: str, messages: list, tools: list | None, real: Callable | None = None) -> dict:
        """One researcher model turn: record the live response (usage included) or serve the next recorded one.
        A changed prompt (the hash of the messages) is counted as drift and served anyway, like ``model_call``."""
        assert isinstance(messages, list) and model, "messages + model required"
        sha = _sha(json.dumps(messages, sort_keys=True, default=str))
        if self.recording:
            assert real is not None, "recording needs the real model"
            reply = real(model, messages, tools)
            self.data["research"].append({"sha": sha, "model": model, "reply": reply})
            return reply
        if self._research_i >= len(self.data["research"]):
            raise CassetteMiss(f"{self.ask_id}: research turn #{self._research_i + 1} not in the cassette")
        rec = self.data["research"][self._research_i]
        self._research_i += 1
        if rec.get("sha") and rec["sha"] != sha:
            self.drift += 1
        return json.loads(json.dumps(rec["reply"]))

    def research_search(self, query: str, limit: int, real: Callable | None = None) -> dict:
        """One researcher web search: record the live service's answer (or its failure), or serve the recorded one
        by query (a repeat gets the next recorded answer, then the last). A miss is a ``CassetteMiss``."""
        assert isinstance(query, str) and query, "query required"
        assert limit > 0, "a positive limit"
        if self.recording:
            assert real is not None, "recording needs the real search"
            try:
                value = real(query, limit)
            except Exception as exc:
                self.data["search"].append({"query": query, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                raise
            self.data["search"].append({"query": query, "ok": True, "value": value})
            return value
        rows = [r for r in self.data["search"] if r["query"] == query]
        if not rows:
            raise CassetteMiss(f"{self.ask_id}: search {query!r} not in the cassette")
        n = self._search_seen.get(query, 0)
        self._search_seen[query] = n + 1
        row = rows[min(n, len(rows) - 1)]
        if not row["ok"]:
            from smartbrain_3000 import search
            raise search.SearchError(str(row.get("error") or "recorded failure"))
        return json.loads(json.dumps(row["value"]))

    def research_stats(self) -> dict:
        """Calls and tokens the researcher used on this ask, from the recorded responses' ``usage`` blocks."""
        from smartbrain_3000 import usage
        stats = {"calls": 0, "prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0}
        turns = self.data.get("research") or []
        for turn in turns if self.recording else turns[: self._research_i]:  # replay counts what was served
            body = (turn.get("reply") or {}).get("usage")
            stats["calls"] += 1
            if not isinstance(body, dict):
                continue
            created, read = usage.cache_tokens(body)
            stats["prompt"] += usage.prompt_total(body)
            stats["completion"] += int(body.get("completion_tokens") or 0)
            stats["cache_read"] += read
            stats["cache_write"] += created
            stats["cost_usd"] += float(body.get("cost_usd") or 0.0)
        return stats

    def fetch(self, fn: str, url: str, real: Callable | None, payload: object = None, **kw) -> object:
        """Record the live response (or failure), or replay it by key; repeats get the last one."""
        key = fetch_key(fn, url, payload)
        if self.recording:
            return self._record_fetch(key, url, real, payload, kw)
        rows = [r for r in self.data["fetches"] if r["key"] == key]
        if not rows:
            raise CassetteMiss(f"{self.ask_id}: no recorded response for {key}")
        n = self._fetch_seen.get(key, 0)
        self._fetch_seen[key] = n + 1
        row = rows[min(n, len(rows) - 1)]
        if row["ok"]:
            return json.loads(json.dumps(row["value"]))
        from smartbrain_3000 import netguard
        err = row["error"]
        raise netguard.FetchError(err["msg"] or "recorded failure", status=err.get("status"),
                                  kind=err.get("kind"))

    def _record_fetch(self, key: str, url: str, real: Callable | None, payload: object, kw: dict):
        assert real is not None, "recording needs the real fetcher"
        assert isinstance(kw, dict), "kwargs dict"
        try:
            value = real(url, **kw) if payload is None else real(url, payload, **kw)
        except Exception as exc:
            err = {"type": type(exc).__name__, "msg": str(exc)[:300],
                   "kind": getattr(exc, "kind", None), "status": getattr(exc, "status", None)}
            self.data["fetches"].append({"key": key, "ok": False, "error": err})
            raise
        self.data["fetches"].append({"key": key, "ok": True, "value": value})
        return value


_WRAPPED = {
    "safe_fetch_json": False, "safe_fetch_text": False, "safe_fetch": False,
    "safe_fetch_feed": False, "safe_post_json": True,
}
_BLOCKED = ("safe_fetch_page", "safe_fetch_image", "safe_fetch_bytes")


def _text_kwargs(name: str, args: tuple) -> dict:
    """Map ``safe_fetch_text(url, fmt)``'s positional format to a keyword so keys stay uniform."""
    assert isinstance(args, tuple), "args tuple"
    return {"fmt": args[0]} if name == "safe_fetch_text" and args else {}


@contextlib.contextmanager
def patched_netguard(session: Session) -> Iterator[Session]:
    """Route netguard's fetch functions through the session for the span of one ask.

    Page / image / bytes fetches are not replayable: recording passes them through (counted in
    ``session.unrecorded``), replay refuses them.
    """
    from smartbrain_3000 import netguard
    assert session is not None, "session required"
    saved = {name: getattr(netguard, name) for name in (*_WRAPPED, *_BLOCKED)}

    def make(name: str, has_payload: bool) -> Callable:
        real = saved[name]
        if has_payload:
            return lambda url, payload, **kw: session.fetch(name, url, real, payload, **kw)
        return lambda url, *a, **kw: session.fetch(name, url, real, None, **{**kw, **_text_kwargs(name, a)})

    def blocked(name: str) -> Callable:
        def _call(*a, **kw):
            if session.recording:
                session.unrecorded += 1
                return saved[name](*a, **kw)
            raise CassetteMiss(f"{session.ask_id}: {name} is not replayable")
        return _call

    try:
        for name, has_payload in _WRAPPED.items():
            setattr(netguard, name, make(name, has_payload))
        for name in _BLOCKED:
            setattr(netguard, name, blocked(name))
        yield session
    finally:
        for name, fn in saved.items():
            setattr(netguard, name, fn)


def save_session(root: pathlib.Path, session: Session) -> pathlib.Path:
    assert root is not None and session.ask_id, "root + id required"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{session.ask_id}.json"
    path.write_text(json.dumps(session.data, indent=1, sort_keys=True, default=str) + "\n")
    return path


def load_session(root: pathlib.Path, ask_id: str) -> Session:
    assert ask_id, "ask id required"
    path = pathlib.Path(root) / f"{ask_id}.json"
    if not path.is_file():
        raise CassetteMiss(f"no cassette for {ask_id} in {root}")
    return Session(ask_id, json.loads(path.read_text()), recording=False)


# ---------------------------------------------------------------------------------------------
# Driving the real flow
# ---------------------------------------------------------------------------------------------

def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / filename)
    assert spec is not None and spec.loader is not None, f"cannot load {filename}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def drive_url(ask: str, source_url: str | None, call: Callable[[str, str], str]) -> dict:
    """Library-free drive: the real ``run_flow`` on a fresh in-memory store, source given."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni, ni_flow
    from smartbrain_3000.secrets import gen_master_key
    assert ask, "ask required"
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = ni.NIStore(conn, gen_master_key())
    out: dict = {"ask": ask, "outcome": "", "source": source_url or "", "detail": "", "preview": None,
                 "tapped": [], "links": [], "notes": [], "scene_text": []}
    try:
        item_id = ni_flow.create_shell_item(store, ask)
        rec = ni_flow.run_flow(store, item_id, gateway_call=call, ni_route_model="mlx/eval-local",
                               source_url=source_url)
        state = str(rec.get("state") or "")
        out["notes"] = [str(n) for n in rec.get("notes") or []]
        out["detail"] = str(rec.get("error") or "")[:160]
        out["outcome"] = flow_outcome(rec)
        if state == "declined":  # the closed reason and the links ride the run, so a report can tell declines apart
            from smartbrain_3000 import ni_master
            out["decline"] = ni_master.decline_view(rec)
        snap = store.read_snapshot(item_id, "preview_data") if state == "ready" else None
        out["preview"] = snap["payload"] if snap else None
        if source_url:
            out["tapped"] = [{"title": "", "url": source_url, "label": "", "params": {}}]
    except Exception as exc:
        out["outcome"], out["detail"] = "crash", f"{type(exc).__name__}: {str(exc)[:160]}"
    return out


RESEARCH_MODES = ("cloud", "local", "off")
DEFAULT_CLOUD_MODEL = "anthropic/claude-sonnet-5-5"
LOCAL_STAND_IN = "mlx/eval-local"


class StaticLibrary:
    """A Library of fixed candidate rows (the CI research ask): no declared answers, so every row is a link row
    the auto-pick cannot build and the researcher is the only way to a card."""

    def __init__(self, rows: list[dict]) -> None:
        assert isinstance(rows, list), "rows required"
        self.rows = rows

    def candidates(self, ask: str, limit: int = 3, hint: dict | None = None):
        assert isinstance(ask, str), "ask required"
        return [dict(r) for r in self.rows[:limit]], []

    def answers(self, source_id: str) -> list:
        assert isinstance(source_id, str), "id required"
        return []

    def get(self, source_id: str) -> dict | None:
        assert isinstance(source_id, str), "id required"
        return None


@contextlib.contextmanager
def _env(values: dict[str, str]) -> Iterator[None]:
    """Set environment variables for one ask, then restore them exactly."""
    assert isinstance(values, dict), "values required"
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@contextlib.contextmanager
def research_seam(session: Session, mode: str, *, local_model: str, cloud_model: str,
                  real_chat: Callable | None = None, real_search: Callable | None = None) -> Iterator[None]:
    """Route the researcher's model turns and web searches through ``session`` for one ask. ``cloud`` stands for
    the operator's consent (the eval runs no UI to tap it); ``local`` offers no cloud route at all."""
    from smartbrain_3000 import ni_research_flow
    assert mode in RESEARCH_MODES, "a known research mode"
    if mode == "off":
        yield
        return
    ni_research_flow.set_research_chat(
        lambda model, messages, tools: session.research_turn(model, messages, tools, real_chat),
        cloud=cloud_model if mode == "cloud" else None, local=local_model, consented=mode == "cloud")
    ni_research_flow.set_research_search(lambda query, limit: session.research_search(query, limit, real_search))
    try:
        yield
    finally:
        ni_research_flow.set_research_chat(None)
        ni_research_flow.set_research_search(None)


def drive_research(ask: str, rows: list[dict], call: Callable[[str, str], str]) -> dict:
    """The v2 flow on a fresh in-memory store with a fixed Library: what a research ask does end to end."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni, ni_flow
    from smartbrain_3000.secrets import gen_master_key
    assert ask and rows, "ask + library rows required"
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = ni.NIStore(conn, gen_master_key())
    out: dict = {"ask": ask, "outcome": "", "source": "", "detail": "", "preview": None, "tapped": [],
                 "links": [], "notes": [], "scene_text": []}
    ni_flow.set_library_provider(lambda: StaticLibrary(rows))
    try:
        with _env({"SMARTBRAIN_NI_FLOW": "v2"}):
            item_id = ni_flow.create_shell_item(store, ask)
            rec = ni_flow.run_flow(store, item_id, gateway_call=call, ni_route_model=LOCAL_STAND_IN)
        state = str(rec.get("state") or "")
        out["notes"] = [str(n) for n in rec.get("notes") or []]
        out["detail"] = str(rec.get("error") or "")[:160]
        out["outcome"] = flow_outcome(rec)
        if state == "declined":  # the closed reason and the links ride the run, so a report can tell declines apart
            from smartbrain_3000 import ni_master
            out["decline"] = ni_master.decline_view(rec)
        item = store.get_item(item_id)
        if state == "ready":
            snap = store.read_snapshot(item_id, "preview_data")
            out["preview"] = snap["payload"] if snap else None
            built = item["spec"].get("_built_from") or {}
            out["source"] = item["spec"]["source"].get("url", "")
            out["tapped"] = [{"title": built.get("title") or "", "url": out["source"], "label": "", "params": {}}]
            if ni.awaits_yes(item):
                out["outcome"] = "awaiting-yes"
                tier = (item["spec"].get("recipe") or {}).get("tier") or ""
                out["tier"] = tier  # curated = a Library row; verified_endpoint / page_reading = found on the web
                out["from"] = {"kind": "dataset" if tier not in ("", "curated") else "library",
                               "host": built.get("host") or "", "title": built.get("title") or ""}
    except Exception as exc:
        out["outcome"], out["detail"] = "crash", f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        ni_flow.set_library_provider(None)
    return out


def run_entry(entry: dict, session: Session, *, llm: Callable[[str], str] | None = None,
              model: str = "", pack_dir: pathlib.Path | None = None, research: str | None = None,
              research_model: str = DEFAULT_CLOUD_MODEL, real_chat: Callable | None = None,
              real_search: Callable | None = None, research_web: str | None = None) -> dict:
    """Run one gold entry under ``session`` (record if ``llm`` is the live model, else replay).

    ``harness.mode == "url"`` drives ``run_flow`` with a given source (no Library); anything else
    is the product path through ``ni-live-e2e.run_one`` (needs the Library pack dir).
    """
    assert entry.get("ask") and session is not None, "entry + session required"
    harness = entry.get("harness") or {}
    started = time.time()

    def call(_model: str, prompt: str) -> str:
        return session.model_call(prompt, llm)

    mode = research or harness.get("research")
    assert mode in (None, *RESEARCH_MODES), "research is cloud, local or off"
    assert research_web in (None, "on", "off"), "research web is on or off"
    env = {"SMARTBRAIN_NI_RESEARCH": mode, "SMARTBRAIN_NI_RESEARCH_WEB": research_web or "on"} if mode else {}
    local_model = model if model and "/" in model else LOCAL_STAND_IN
    with patched_netguard(session), _env(env), research_seam(session, mode or "off", local_model=local_model,
                                                             cloud_model=research_model, real_chat=real_chat,
                                                             real_search=real_search):
        if harness.get("mode") == "url":
            run = drive_url(entry["ask"], harness.get("source_url"), call)
        elif harness.get("mode") == "research":
            run = drive_research(entry["ask"], harness["library_rows"], call)
        else:
            assert pack_dir is not None, "library-mode asks need --pack-dir"
            e2e = _load_tool("ni_live_e2e", "ni-live-e2e.py")
            run = e2e.run_one(entry["ask"], e2e._pack(pack_dir), lambda p, _n: call(model, p), model)
    run.update({"id": entry["id"], "cassette_drift": session.drift, "secs": round(time.time() - started, 1)})
    stats = session.research_stats()
    if stats["calls"]:
        run["research"] = stats
    return run


def load_gold(path: pathlib.Path = GOLD_PATH) -> list[dict]:
    assert path is not None, "path required"
    data = json.loads(pathlib.Path(path).read_text())
    assert isinstance(data, list) and data, "gold must be a non-empty list"
    return data


def replay_all(gold: list[dict], cassette_dir: pathlib.Path, pack_dir: pathlib.Path | None = None,
               research: str | None = None, research_model: str = DEFAULT_CLOUD_MODEL, model: str = "",
               research_web: str | None = None) -> list[dict]:
    """Replay every gold entry that has a cassette; others are skipped (not errors)."""
    assert isinstance(gold, list), "gold list required"
    runs = []
    for entry in gold:
        if not (pathlib.Path(cassette_dir) / f"{entry['id']}.json").is_file():
            continue
        runs.append(run_entry(entry, load_session(cassette_dir, entry["id"]), pack_dir=pack_dir, research=research,
                              research_model=research_model, model=model, research_web=research_web))
    return runs


def score_all(gold: list[dict], runs: list[dict]) -> tuple[list[dict], dict]:
    assert isinstance(gold, list) and isinstance(runs, list), "lists required"
    by_id = {g["id"]: g for g in gold}
    scored = [score_ask(r, by_id[r["id"]]) for r in runs if r.get("id") in by_id]
    return scored, aggregate(scored)


# ---------------------------------------------------------------------------------------------
# Gold conversion
# ---------------------------------------------------------------------------------------------

_WINDOWS = ("tonight", "last night", "right now", "at the moment", "this afternoon", "this weekend",
            "this week", "this year", "last month", "tomorrow", "today", "now")
_PLACE = re.compile(r"\b(?:in|at|for|near|from)\s+((?:[A-Z][\w.'-]*)(?:\s+[A-Z][\w.'-]*)*)")
_THRESH = re.compile(r"\b(?:above|over|greater than|at least|below|under)\s+(\d+(?:\.\d+)?)")
_CAPS = re.compile(r"[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*")
# the four wrong blind-50 cards and the defect class review.md names for each (by review row #)
_WRONG_CLASS = {10: "window cut", 19: "threshold on a count", 20: "category too coarse",
                23: "compare unsupported"}


def norm_verdict(raw: str | None) -> str | None:
    """right | partial | wrong from a reviewer's free text; None when it is not one of those."""
    assert raw is None or isinstance(raw, str), "text or None"
    low = (raw or "").strip().lower()
    for word, verdict in (("wrong", "wrong"), ("partial", "partial"), ("right", "right"),
                          ("honest empty", "right")):
        if low.startswith(word):
            return verdict
    return None


def derive_constraints(ask: str) -> dict:
    """Constraints stated IN the ask text only (never invented): places, window, threshold, compare."""
    assert ask, "ask required"
    low = ask.lower()
    compare = bool(re.search(r"\bvs\.?\b|\bcompare\b", low))
    ents = _CAPS.findall(ask) if compare else []
    thr = _THRESH.search(ask)
    return {
        "places": [] if compare else [m.strip() for m in _PLACE.findall(ask)],
        "window": next((w for w in _WINDOWS if w in low), None),
        "threshold": float(thr.group(1)) if thr else None,
        "compare_n": 2 if compare and len(ents) >= 2 else None,
        "units": None,
        "entities": ents,
    }


def _record(set_id: str, n: int, ask: str, kind: str, raw: str | None, source_id: str | None,
            baseline: dict, defect: str | None = None) -> dict:
    verdict = norm_verdict(raw)
    return {
        "id": f"{set_id}{n}", "set": set_id, "ask": ask, "kind": kind,
        "constraints": derive_constraints(ask),
        "acceptable_sources": [source_id] if verdict == "right" and source_id else None,
        "expected_outcome": "card" if verdict == "right" else None,
        "forbidden_outcomes": [], "human_verdict": verdict, "human_verdict_raw": raw,
        "defect_class": defect, "baseline": baseline, "harness": None,
    }


def _review_rows(path: pathlib.Path) -> dict[str, tuple[int, str]]:
    """ask -> (row number, verdict text) from the `| # | kind | ask | source | verdict |` table."""
    rows: dict[str, tuple[int, str]] = {}
    for line in path.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].isdigit():
            rows[cells[2]] = (int(cells[0]), cells[4])
    assert rows, "no review rows parsed"
    return rows


def _review_for(review: dict[str, tuple[int, str]], ask: str) -> tuple[int, str | None]:
    """The review row for an ask: exact text, else the one row whose (shortened) ask it contains."""
    assert isinstance(review, dict) and ask, "review + ask required"
    if ask in review:
        return review[ask]
    hits = [v for k, v in review.items() if k.lower() in ask.lower()]
    return hits[0] if len(hits) == 1 else (0, None)


def convert_blind50() -> list[dict]:
    """Blind-50 (2026-10-05): all 50 asks; verdicts for the 23 live cards from review.md."""
    base = FIXTURES / "ni_blind" / "2026-10-05"
    asks = json.loads((base / "asks.json").read_text())
    results = json.loads((base / "results.json").read_text())
    review = _review_rows(base / "review.md")
    out = []
    for i, (a, r) in enumerate(zip(asks, results, strict=True), start=1):
        assert a["ask"] == r["ask"], "asks and results out of step"
        num, raw = _review_for(review, a["ask"])
        tap = (r.get("tapped") or [{}])[0].get("url") or ""
        host = re.sub(r"^https?://([^/]+)/?.*$", r"\1", tap) if tap else None
        info = {"outcome": r["outcome"], "source": r.get("source"), "source_host": host,
                "card_sha": _sha(card_text(r)) if r["outcome"] == "live" else None}
        defect = _WRONG_CLASS.get(num) if norm_verdict(raw) == "wrong" else None
        out.append(_record("A", i, a["ask"], a["kind"], raw, host, info, defect))
    return out


def convert_query_gold() -> list[dict]:
    """Sets B-E (gold.jsonl) and F (gold_f.jsonl): every recorded live card with its verdict."""
    out = []
    for name in ("gold.jsonl", "gold_f.jsonl"):
        for line in (FIXTURES / "ni_query" / name).read_text().splitlines():
            row = json.loads(line)
            if row["set"] == "A":
                continue  # blind-50 comes from its own review.md, the authority
            info = {"outcome": "live", "source": None, "source_id": row["source_id"],
                    "params": row.get("params_used"), "card_sha": None}
            out.append(_record(row["set"], row["n"], row["ask"], row["kind"], row["verdict"],
                               row["source_id"], info, row.get("note") or None))
    return out


def convert_set_g() -> list[dict]:
    """Set G (2026-10-12): asks only; its review records defect fixes, not verdicts, so null."""
    asks = json.loads((FIXTURES / "ni_blind" / "live_2026-10-12g" / "asks.json").read_text())
    return [_record("G", int(a["id"][1:]), a["ask"], a["kind"], None, None, {"outcome": None})
            for a in asks]


def convert_all() -> list[dict]:
    recs = convert_blind50() + convert_query_gold() + convert_set_g()
    ids = [r["id"] for r in recs]
    assert len(ids) == len(set(ids)), "gold ids must be unique"
    return recs


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def _select(gold: list[dict], args: argparse.Namespace) -> list[dict]:
    assert isinstance(gold, list), "gold list required"
    rows = [g for g in gold if not args.set or g["set"] in args.set.split(",")]
    if args.only:
        rows = [g for g in rows if args.only.lower() in g["ask"].lower() or args.only == g["id"]]
    return rows


def _set_flow(args: argparse.Namespace) -> None:
    """``--flow v1|v2``: set the ni_flow flag for this run through its env var (no-op when absent)."""
    flow = getattr(args, "flow", None)
    assert flow in (None, "v1", "v2"), "flow is v1 or v2"
    if flow:
        os.environ["SMARTBRAIN_NI_FLOW"] = flow


def bifrost_chat(bifrost: str, timeout: float = 300.0) -> Callable[[str, list, list | None], dict]:
    """The real researcher transport: the gateway's own wire form (cache markers on anthropic routes, sorted
    tool keys) posted to bifrost's /v1/chat/completions; returns the parsed response with its usage block."""
    import urllib.request

    from smartbrain_3000 import gateway
    assert bifrost and isinstance(bifrost, str), "bifrost URL required"
    endpoint = bifrost.rstrip("/") + "/v1/chat/completions"

    def _call(model: str, messages: list, tools: list | None) -> dict:
        assert model and messages, "model + messages required"
        payload: dict = {"model": model, "messages": gateway._wire_messages(messages, model)}
        if tools:
            payload.update(tools=gateway._wire_tools(tools, model), tool_choice="auto", max_tokens=4096)
        else:
            payload.update(temperature=0, max_tokens=2048)
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    return _call


def live_search(query: str, limit: int) -> dict:
    """The real researcher search for a LIVE record: the app's keyless service (DuckDuckGo behind netguard)."""
    from smartbrain_3000 import search
    assert query and limit > 0, "query + limit required"
    return search.SearchService().search(query, limit=limit)


def cmd_record(args: argparse.Namespace) -> int:
    """LIVE: real model through the bifrost gateway, real network. Saves cassette + results."""
    _set_flow(args)
    ev = _load_tool("ni_flow_eval", "ni-flow-eval.py")
    llm = ev._bifrost_llm(args.bifrost, args.model)
    chat = bifrost_chat(args.bifrost) if getattr(args, "research", None) in ("cloud", "local") else None
    search_live = live_search if getattr(args, "research", None) == "cloud" else None
    gold, runs = load_gold(args.gold), []
    for entry in _select(gold, args):
        sess = Session(entry["id"], recording=True)
        sess.data["meta"] = {"model": args.model, "ask": entry["ask"], "research": getattr(args, "research", None),
                             "research_model": getattr(args, "research_model", None),
                             "research_web": getattr(args, "research_web", None) or "on",
                             "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        run = run_entry(entry, sess, llm=lambda p: llm(p, 600), model=args.model, pack_dir=args.pack_dir,
                        research=getattr(args, "research", None), real_chat=chat, real_search=search_live,
                        research_web=getattr(args, "research_web", None),
                        research_model=getattr(args, "research_model", None) or DEFAULT_CLOUD_MODEL)
        save_session(args.cassette_dir, sess)
        runs.append(run)
        print(f"[{run['outcome']:>12}] {entry['id']:<5} {entry['ask'][:50]}", flush=True)
    return _finish(args, gold, runs)


def cmd_replay(args: argparse.Namespace) -> int:
    _set_flow(args)
    gold = _select(load_gold(args.gold), args)
    runs = replay_all(gold, args.cassette_dir, args.pack_dir, research=getattr(args, "research", None),
                      research_model=getattr(args, "research_model", None) or DEFAULT_CLOUD_MODEL,
                      research_web=getattr(args, "research_web", None))
    return _finish(args, gold, runs)


def _finish(args: argparse.Namespace, gold: list[dict], runs: list[dict]) -> int:
    assert isinstance(runs, list), "runs required"
    scored, table = score_all(gold, runs)
    print(format_table(table))
    summary = research_summary(runs)
    if summary:
        print(summary)
    if args.out:
        args.out.write_text(json.dumps({"runs": runs, "scored": scored, "table": table}, indent=1, default=str))
    return 0


def research_summary(runs: list[dict]) -> str:
    """One line on what the researcher cost: asks that used it, median seconds per ask, tokens and cache reads,
    cost when the responses carried one. "" when no run used the researcher."""
    assert isinstance(runs, list), "runs required"
    used = [r for r in runs if r.get("research")]
    if not used:
        return ""
    secs = sorted(float(r.get("secs") or 0.0) for r in used)
    total = {k: sum(r["research"][k] for r in used) for k in ("calls", "prompt", "completion", "cache_read", "cache_write", "cost_usd")}
    n = len(used)
    reads = total["cache_read"] / total["prompt"] if total["prompt"] else 0.0
    return (f"research: {n} asks, median {secs[n // 2]:.1f}s, {total['calls'] / n:.1f} model calls/ask, "
            f"prompt {total['prompt'] / n:.0f} (cache read {reads:.0%}, write {total['cache_write'] / n:.0f}) "
            f"+ completion {total['completion'] / n:.0f} tokens/ask, cost ${total['cost_usd'] / n:.4f}/ask")


def cmd_score(args: argparse.Namespace) -> int:
    runs = json.loads(args.results.read_text())
    gold = load_gold(args.gold)
    by_ask = {g["ask"]: g["id"] for g in gold}
    for r in runs:
        r.setdefault("id", by_ask.get(r.get("ask")))
    return _finish(args, gold, runs)


def cmd_convert(args: argparse.Namespace) -> int:
    recs = convert_all()
    args.gold.parent.mkdir(parents=True, exist_ok=True)
    args.gold.write_text(json.dumps(recs, indent=1, ensure_ascii=False) + "\n")
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["set"]] = counts.get(r["set"], 0) + 1
    print(f"wrote {len(recs)} gold records to {args.gold}: {counts}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("convert", cmd_convert), ("score", cmd_score), ("record", cmd_record),
                     ("replay", cmd_replay)):
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        p.add_argument("--gold", type=pathlib.Path, default=GOLD_PATH)
        p.add_argument("--out", type=pathlib.Path)
        if name in ("record", "replay"):
            p.add_argument("--cassette-dir", type=pathlib.Path, required=True)
            p.add_argument("--pack-dir", type=pathlib.Path)
            p.add_argument("--set", default="", help="comma list of set letters, e.g. A,B")
            p.add_argument("--only", default="")
            p.add_argument("--flow", choices=("v1", "v2"), help="set the ni_flow flag for this run")
            p.add_argument("--research", choices=RESEARCH_MODES,
                           help="v2 researcher tier: cloud (consented), local (closed-world) or off")
            p.add_argument("--research-model", default=None, help=f"cloud route (default {DEFAULT_CLOUD_MODEL})")
            p.add_argument("--research-web", choices=("on", "off"), default=None,
                           help="cloud researcher may search and read the open web (default on); off = Library only")
        if name == "record":
            p.add_argument("--model", default="mlx/Qwen3.5-9B-MLX-4bit")
            p.add_argument("--bifrost", default="http://127.0.0.1:38080")
        if name == "score":
            p.add_argument("results", type=pathlib.Path)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
