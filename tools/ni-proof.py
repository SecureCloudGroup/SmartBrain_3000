#!/usr/bin/env python3
"""Observe one Neural Interface ask on a running SmartBrain, end to end, over its local API.

Postmortem 2026-10-31 (rule A1, mechanism M13): a fix is done only when the proof ask has run on a real install and
been observed, with the evidence kept. 0.27.1, 0.27.2 and 0.27.3 were each called fixed from tests, and the first
real run of the NVDA ask was T's, after the tag. This tool is the observation. It types the ask into the running app
(``POST /api/ni/intake``), answers what the card asks only the way the run was told to, polls the card until it
ends, and writes what it saw, as JSON and Markdown:

* every state the card passed through, with the seconds since the ask, its progress line and what it asked;
* each source the build tried and why it moved on, when the card lists them, and the reason the card ended on;
* the card's reading (its bound values), its engine runs, one refresh when asked, and the card's next scheduled
  run when asked to wait for it (``--watch MINUTES``: the ask's own cadence is the claim being proved);
* the model calls each Neural Interface step made, per model, from the app's own usage view;
* the audit rows for the card (what was given, tapped, fetched), the app's version, and the model each step runs on.

What it answers, only as told: the cloud-model consent (``--consent allow``, or ``local``; ``stop`` reports the
question and stops), a key the card asks for from an environment variable named per host (``--key HOST=ENV``),
the same-host key the card offers for one tap (``--reuse``), skipping a Library source it has no key for when
the card offers the skip (``--skip-keyless``), the card's YES on its reading (``--yes``) and one refresh through
the engine (``--refresh``).
Anything else the card asks is reported, and the run waits for a person to answer it on the card, up to
``--timeout``. A built card meets ``--expect built`` only with a number on it, and, when the run asked for them,
a refresh that read the source and a scheduled run inside the watch: the first run on T's Mac (2026-10-31) built
the card, its refresh was refused, and the run still passed; the report now names each shortfall.

Keys: a key goes from its environment variable to the card's key route and nowhere else. Nothing printed or written
carries a key or the local API token: the report is refused, and nothing written, if any of their text is in it.
The token is sent to a loopback address only, never through a proxy.

Usage:
    python3 tools/ni-proof.py --ask "NVDA stock price every 30 minutes" --consent allow \\
        --key www.alphavantage.co=SB_PROOF_KEY_ALPHAVANTAGE --key finnhub.io=SB_PROOF_KEY_FINNHUB \\
        --yes --refresh --watch 33 --expect built --out ~/Documents/claude_working_folder/ni-proof

    --base        the app's address (default $SMARTBRAIN_PROOF_BASE, set by tools/ni-scratch.py, else
                  http://127.0.0.1:33000, the launcher's port)
    --token-file  the local API token (default $SMARTBRAIN_LOCAL_TOKEN, else the launcher's local-api.token in its
                  config directory)

Exit: 0 the card ended as ``--expect`` says (default: any end), 1 it ended otherwise, 2 a setup or API error,
3 the timeout came first. The report names the end either way.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

DEFAULT_BASE = "http://127.0.0.1:33000"   # launcher/stack/stack.go DefaultPort
TOKEN_ENV = "SMARTBRAIN_LOCAL_TOKEN"
TOKEN_FILE = "local-api.token"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
TERMINAL = frozenset({"failed", "unsupported", "declined"})   # ni_master.TERMINAL_STATES
PAST_THE_BUILD = frozenset({"commissioning", "live", "degraded", "failing", "broken", "paused"})
ENDS = ("built", "declined", "failed", "unsupported", "stopped", "timeout", "error")
EXPECT = ("any", "built", "declined", "not-built")
MAX_POLLS = 20_000       # the watch loop's own bound beside the timeout
MAX_EVENTS = 300
AUDIT_LIMIT = 200        # /api/audit's newest rows read back for the card
TEXT_MAX = 400
WATCH_MAX_S = 3 * 3600.0   # --watch's ceiling
WATCH_POLL_MAX_S = 30.0    # the watch reads the card at most this far apart (the app's own tick is 30 s)

Api = Callable[[str, str, "dict | None"], "tuple[int, object]"]


class ProofError(RuntimeError):
    """A setup or API answer the run can't go on from (exit 2); its text never carries a key or the token."""


@dataclass(frozen=True)
class Plan:
    """What the run may answer for the user, and how long it watches."""

    ask: str
    consent: str = "allow"                                  # allow | local | stop
    keys: dict[str, str] = field(default_factory=dict)      # host -> the environment variable holding its key
    reuse: bool = False
    skip_keyless: bool = False
    yes: bool = False
    refresh: bool = False
    watch_s: float = 0.0                                    # after the refresh: wait this long for a scheduled run
    timeout_s: float = 900.0
    poll_s: float = 2.0

    def __post_init__(self) -> None:
        assert 3 <= len(self.ask.strip()) <= 2000, "an ask the composer takes (3 to 2000 characters)"
        assert self.consent in ("allow", "local", "stop"), "consent: allow, local or stop"
        assert self.timeout_s > 0 and self.poll_s > 0, "a timeout and a poll interval"
        assert 0 <= self.watch_s <= WATCH_MAX_S, "a watch of at most three hours"


@dataclass(frozen=True)
class Tap:
    """One answer the run gives on the card: its name (each is made once), the route, and what the report says."""

    name: str
    method: str
    path: str
    body: dict | None
    note: str


# ---- transport ---------------------------------------------------------------------------------------------------

def http_api(base: str, token: str, timeout_s: float = 30.0) -> Api:
    """The local API over HTTP: the token as a bearer, to a loopback address only, with no proxy in the way."""
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    if host not in LOOPBACK:
        raise ProofError(f"--base must be this computer's address (127.0.0.1, localhost or ::1), not {host!r}")
    assert token and not any(c.isspace() for c in token), "a token"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        assert path.startswith("/api/"), "the app's API only"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ProofError(f"{method} {path}: the app did not answer ({type(exc).__name__})") from None
    return call


def _parse(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"text": raw[:TEXT_MAX].decode("utf-8", "replace")}


def _detail(body: object) -> str:
    """The app's own words for a refusal (FastAPI's ``detail``), bounded."""
    if isinstance(body, dict):
        return " ".join(str(body.get("detail") or body.get("text") or "").split())[:TEXT_MAX]
    return ""


def read_token(token_file: str | None, environ: dict) -> str:
    """The local API token: ``--token-file``, else ``$SMARTBRAIN_LOCAL_TOKEN``, else the launcher's file in its config
    directory (Go's ``os.UserConfigDir``/SmartBrain). Never printed."""
    env = str(environ.get(TOKEN_ENV) or "").strip()
    if env and not token_file:
        return env
    path = pathlib.Path(token_file).expanduser() if token_file else _config_dir(environ) / "SmartBrain" / TOKEN_FILE
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise ProofError(f"no local API token at {path} (pass --token-file, or set {TOKEN_ENV})") from None
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ProofError(f"{path} does not hold a local API token")
    return token


def _config_dir(environ: dict) -> pathlib.Path:
    home = pathlib.Path(str(environ.get("HOME") or pathlib.Path.home()))
    if platform.system() == "Darwin":
        return home / "Library" / "Application Support"
    if platform.system() == "Windows":
        return pathlib.Path(str(environ.get("APPDATA") or home))
    return pathlib.Path(str(environ.get("XDG_CONFIG_HOME") or home / ".config"))


# ---- what the card shows -----------------------------------------------------------------------------------------

def summary(view: dict) -> dict:
    """The part of one poll a person would read on the card: states, the progress line, what it asks, what it
    tried, why it ended, its actions. Two polls with the same summary are one event."""
    flow = view.get("flow") if isinstance(view.get("flow"), dict) else None
    out: dict = {"card": str(view.get("state") or "")}
    if flow is not None:
        out["flow"] = str(flow.get("state") or "")
        for name in ("error", "reason"):
            if flow.get(name):
                out[name] = " ".join(str(flow[name]).split())[:TEXT_MAX]
        question = flow.get("question") if isinstance(flow.get("question"), dict) else {}
        if question.get("kind"):
            out["asks"] = str(question["kind"])
        out.update(_progress_and_access(flow))
        tried = [t for t in flow.get("tried") or [] if isinstance(t, dict)]
        if tried:
            out["tried"] = [f"{t.get('host')}: {t.get('why')}" for t in tried]
        decline = flow.get("decline") if isinstance(flow.get("decline"), dict) else None
        if decline is not None:
            out["decline"] = {"reason": decline.get("reason"), "line": decline.get("line"),
                              "links": [str(link.get("url") or "") for link in decline.get("links") or []
                                        if isinstance(link, dict)],
                              "key_wall": (decline.get("key_wall") or {}).get("host")}
    yes = view.get("awaiting_yes")
    if isinstance(yes, dict):
        out["awaiting_yes"] = {k: yes.get(k) for k in ("from", "host", "title", "used_key") if k in yes}
    out["actions"] = [str(a) for a in view.get("actions") or []][:16]
    return out


def _progress_and_access(flow: dict) -> dict:
    out: dict = {}
    progress = flow.get("progress") if isinstance(flow.get("progress"), dict) else None
    if progress is not None:
        line = f"{progress.get('step')}/{progress.get('of')} {progress.get('label')}"
        trying = progress.get("try") if isinstance(progress.get("try"), dict) else None
        if trying is not None:
            line += f" ({trying.get('host')}, {trying.get('n')} of {trying.get('of')})"
        out["progress"] = line
    access = flow.get("access") if isinstance(flow.get("access"), dict) else None
    if access is not None:
        key = access.get("key") if isinstance(access.get("key"), dict) else None
        out["access"] = {"host": str(access.get("host") or ""), "provider": str(access.get("provider") or ""),
                         "key": key is not None, "offers_reuse": bool(key and key.get("reuse")),
                         "contact": bool(access.get("contact"))}
    return out


def asks(view: dict) -> str:
    """The kind of question the card asks now (``model_consent``, ...), or empty."""
    flow = view.get("flow") if isinstance(view.get("flow"), dict) else {}
    question = flow.get("question") if isinstance(flow.get("question"), dict) else {}
    return str(question.get("kind") or "")


def end_of(view: dict) -> str | None:
    """How the card ended, or None while it builds or waits on the user: a terminal flow state, or ``built`` once
    the flow is gone and the card holds a reading (waiting for the YES, or past it)."""
    flow = view.get("flow") if isinstance(view.get("flow"), dict) else None
    if flow is not None:
        state = str(flow.get("state") or "")
        return state if state in TERMINAL else None
    if isinstance(view.get("awaiting_yes"), dict) or view.get("state") in PAST_THE_BUILD:
        return "built"
    return None


def tap_for(view: dict, plan: Plan, done: set[str], environ: dict) -> Tap | None:
    """The one answer the plan gives to what the card asks now, or None (nothing to answer, or a person's to make).
    Each answer is made once: a card that asks the same thing again is reported and left to a person."""
    item = str(view.get("id") or "")
    flow = view.get("flow") if isinstance(view.get("flow"), dict) else {}
    question = flow.get("question") if isinstance(flow.get("question"), dict) else {}
    actions = set(view.get("actions") or [])
    if flow.get("state") == "unsupported" and asks(view) == "model_consent":
        if plan.consent == "stop" or "consent" in done:
            return None
        named = question.get("model") if plan.consent == "allow" else question.get("local")
        return Tap("consent", "POST", f"/api/ni/items/{item}/flow/answer", {"kind": "model_consent",
                   "value": plan.consent}, f"answered the model question: {plan.consent} ({named or 'no model named'})")
    access = flow.get("access") if isinstance(flow.get("access"), dict) else None
    if flow.get("state") == "awaiting_access" and access is not None:
        return _access_tap(item, access, actions, plan, done, environ)
    yes = view.get("awaiting_yes")
    if isinstance(yes, dict) and plan.yes and "yes" not in done:
        return Tap("yes", "POST", f"/api/ni/items/{item}/validate", {"ok": True},
                   f"said YES to the reading from {yes.get('host') or 'its source'}")
    return None


def _access_tap(item: str, access: dict, actions: set, plan: Plan, done: set[str], environ: dict) -> Tap | None:
    host = str(access.get("host") or "")
    key = access.get("key") if isinstance(access.get("key"), dict) else None
    if key is not None and host in plan.keys and f"key:{host}" not in done:
        name = plan.keys[host]
        value = str(environ.get(name) or "").strip()
        if not value:
            raise ProofError(f"the card asks for {host}'s key and ${name} is empty")
        return Tap(f"key:{host}", "POST", f"/api/ni/items/{item}/flow/access", {"key": value},
                   f"gave the key for {host} from ${name}")
    if key is not None and key.get("reuse") and plan.reuse and f"reuse:{host}" not in done:
        return Tap(f"reuse:{host}", "POST", f"/api/ni/items/{item}/flow/access", {"reuse": True},
                   f"used the key already given for {host}")
    if plan.skip_keyless and "skip_source" in actions and f"skip:{host}" not in done and host not in plan.keys:
        return Tap(f"skip:{host}", "POST", f"/api/ni/items/{item}/flow/skip-source", None,
                   f"skipped {host}: no key for it")
    return None


# ---- the run -----------------------------------------------------------------------------------------------------

def observe(api: Api, plan: Plan, *, environ: dict | None = None, clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print) -> dict:
    """Type the ask, watch the card to its end, gather the evidence. Returns the report (no key or token in it)."""
    env = dict(os.environ if environ is None else environ)
    empty = [f"${name}" for name in plan.keys.values() if not str(env.get(name) or "").strip()]
    if empty:
        raise ProofError(f"no key in {', '.join(empty)}: nothing was asked")
    started, started_at = clock(), datetime.now(UTC)
    report: dict = {"ask": plan.ask, "started_at": started_at.isoformat(timespec="seconds"),
                    "plan": {**asdict(plan), "keys": dict(plan.keys)}, "app": _app_facts(api),
                    "events": [], "taps": []}
    status, created = api("POST", "/api/ni/intake", {"request": plan.ask, "allow_duplicate": True})
    if status != 200 or not isinstance(created, dict) or not created.get("id"):
        raise ProofError(f"the ask was refused: HTTP {status} {_detail(created)}")
    item_id = str(created["id"])
    report["item_id"] = item_id
    say(f"asked: {plan.ask!r} -> card {item_id}")
    try:
        report["end"] = _watch(api, plan, item_id, report, started, env, clock, sleep, say)
        report["seconds"] = round(clock() - started, 1)
        report["card"] = _card(api, item_id)
        card = report["card"]
        if plan.refresh and report["end"] == "built" and (card.get("c2_ok") or card.get("state") != "commissioning"):
            report["refresh"] = _refresh(api, item_id)
            say(f"refresh: HTTP {report['refresh']['status']}")
            report["card"] = _card(api, item_id)
        if plan.watch_s > 0 and report["end"] == "built":
            report["watch"] = _watch_runs(api, item_id, report["card"], plan, clock, sleep, say)
            report["card"] = _card(api, item_id)
        report["model_calls"] = _model_calls(api, started_at)
        report["audit"] = _audit(api, item_id, started_at)
    except ProofError as exc:  # the app stopped answering mid-run: keep what was seen
        report.update(end="error", error=str(exc), seconds=round(clock() - started, 1))
        report.setdefault("card", {})
    say(f"end: {report['end']} after {report['seconds']} s")
    return report


def _watch(api: Api, plan: Plan, item_id: str, report: dict, started: float, env: dict,
           clock: Callable[[], float], sleep: Callable[[float], None], say: Callable[[str], None]) -> str:
    """Poll the card until it ends or the timeout; make the plan's answers as the card asks."""
    last: dict | None = None
    done: set[str] = set()
    waiting_said = ""
    for _ in range(MAX_POLLS):  # bounded; the timeout ends it first
        if clock() - started > plan.timeout_s:
            return "timeout"
        status, view = api("GET", f"/api/ni/items/{item_id}/flow", None)
        if status != 200 or not isinstance(view, dict):
            raise ProofError(f"reading the card failed: HTTP {status} {_detail(view)}")
        seen = summary(view)
        if seen != last and len(report["events"]) < MAX_EVENTS:
            report["events"].append({"t": round(clock() - started, 1), **seen})
            say(f"{round(clock() - started, 1):>7}s  {_line(seen)}")
            last = seen
        tap = tap_for(view, plan, done, env)
        if tap is not None:
            done.add(tap.name)
            status, answer = api(tap.method, tap.path, tap.body)
            report["taps"].append({"t": round(clock() - started, 1), "tap": tap.name, "note": tap.note,
                                   "status": status, "answer": answer if status < 400 else _detail(answer)})
            say(f"{round(clock() - started, 1):>7}s  tap: {tap.note} -> HTTP {status}")
            continue
        end = end_of(view)
        if end == "unsupported" and asks(view) == "model_consent" and plan.consent == "stop":
            return "stopped"
        if end is None and view.get("flow") is None and view.get("state") == "draft":
            end = "built" if _card(api, item_id).get("payload") is not None else None  # a draft card to Activate
        if end is not None:
            return end
        waiting = _waiting_on_a_person(view)
        if waiting and waiting != waiting_said:
            say(f"{'':>9}waiting for you on the card: {waiting}")
            waiting_said = waiting
        sleep(plan.poll_s)
    return "timeout"


def _watch_runs(api: Api, item_id: str, card: dict, plan: Plan, clock: Callable[[], float],
                sleep: Callable[[float], None], say: Callable[[str], None]) -> dict:
    """Wait up to ``plan.watch_s`` for the card's next engine run, the one its schedule makes, reading the card only
    (no tap, no run asked for); stop at the first run that read the source. Every new run is kept with the reading
    the card showed after it, and every state the card passed through."""
    seen = {r.get("ts") for r in card.get("runs") or []}
    every = max(plan.poll_s, min(WATCH_POLL_MAX_S, plan.watch_s / 20))
    out: dict = {"minutes": round(plan.watch_s / 60, 1), "runs": [], "states": [card.get("state")]}
    say(f"watching for the card's next scheduled run, up to {out['minutes']} min")
    started = clock()
    for _ in range(MAX_POLLS):  # bounded; the watch window ends it first
        if clock() - started >= plan.watch_s:
            break
        sleep(min(every, max(0.0, plan.watch_s - (clock() - started))))
        now = _card(api, item_id)
        if now.get("state") != out["states"][-1] and len(out["states"]) < MAX_EVENTS:
            out["states"].append(now.get("state"))
        new = sorted((r for r in now.get("runs") or [] if r.get("ts") not in seen), key=lambda r: str(r.get("ts")))
        for run in new[:MAX_EVENTS]:  # bounded: the runs one read shows
            seen.add(run.get("ts"))
            out["runs"].append({"after_s": round(clock() - started, 1), **run, "summary": now.get("summary")})
            say(f"{'':>9}scheduled run {run.get('ts')}: {run.get('status')} {run.get('error') or ''}".rstrip())
        if any(r.get("status") == "ok" for r in new):
            break
    out["ok_runs"] = sum(1 for r in out["runs"] if r.get("status") == "ok")
    out["waited_s"] = round(clock() - started, 1)
    return out


def _waiting_on_a_person(view: dict) -> str:
    """What the card waits for that this run will not answer (empty while it builds)."""
    flow = view.get("flow") if isinstance(view.get("flow"), dict) else {}
    state = str(flow.get("state") or "")
    if state.startswith("awaiting_"):
        access = flow.get("access") if isinstance(flow.get("access"), dict) else {}
        return f"{state} {access.get('host') or ''}".strip()
    if state == "source" and str(flow.get("error") or "") == "awaiting_pick":
        return "a source pick"
    if isinstance(view.get("awaiting_yes"), dict):
        return "your YES on the reading"
    return ""


def _line(seen: dict) -> str:
    parts = [f"card={seen.get('card')}"]
    for name in ("flow", "progress", "asks", "reason"):
        if seen.get(name):
            parts.append(f"{name}={seen[name]}")
    if seen.get("access"):
        parts.append(f"needs={'key' if seen['access']['key'] else 'contact'} for {seen['access']['host']}")
    if seen.get("tried"):
        parts.append(f"tried={len(seen['tried'])}")
    if seen.get("awaiting_yes"):
        parts.append(f"reading from {seen['awaiting_yes'].get('host')}, waits for YES")
    return "  ".join(parts)


def _app_facts(api: Api) -> dict:
    """The app's version and the model each Neural Interface step runs on now (Settings' own view)."""
    status, health = api("GET", "/api/health", None)
    if status != 200 or not isinstance(health, dict):
        raise ProofError(f"the app is not answering at its health check: HTTP {status}")
    status, steps = api("GET", "/api/ni/step-models", None)
    if status == 423:
        raise ProofError("the app is locked: unlock it, then run again")
    if status != 200 or not isinstance(steps, dict):
        raise ProofError(f"reading the step models failed: HTTP {status} {_detail(steps)}")
    return {"version": health.get("version"),
            "steps": [{k: s.get(k) for k in ("step", "setting", "where", "model", "off", "fallback")}
                      for s in steps.get("steps") or [] if isinstance(s, dict)]}


def _card(api: Api, item_id: str) -> dict:
    """The card as the board shows it now, and its engine runs."""
    status, board = api("GET", "/api/ni/board", None)
    rows = board.get("items") if status == 200 and isinstance(board, dict) else None
    row = next((r for r in rows or [] if isinstance(r, dict) and r.get("id") == item_id), None)
    if row is None:
        return {"missing": True}
    out = {k: row.get(k) for k in ("title", "state", "last_status", "last_checked", "consecutive_failures",
                                   "interval_minutes", "shell", "c2_ok", "awaiting_yes", "payload_at", "payload_ok",
                                   "payload", "interpreted", "fit")}
    out.update(reading_of(row.get("payload")))
    status, item = api("GET", f"/api/ni/items/{item_id}", None)
    if status == 200 and isinstance(item, dict):
        out["runs"] = [{k: r.get(k) for k in ("ts", "status", "duration_ms", "error")}
                       for r in item.get("runs") or [] if isinstance(r, dict)][:20]
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        source = spec.get("source") if isinstance(spec.get("source"), dict) else None
        if source is not None:
            out["source"] = {"type": source.get("type"),
                             "host": (urllib.parse.urlsplit(str(source.get("url") or "")).hostname or None)}
        built = spec.get("_built_from") if isinstance(spec.get("_built_from"), dict) else None
        if built is not None:
            out["built_from"] = {k: built.get(k) for k in ("path", "host", "title")}
    return out


_PAIR = re.compile(r"(?P<label>[A-Za-z][^:;]{0,60}?):\s*(?P<num>[-+]?[$\u20ac\u00a3]?[\d,]*\.?\d+)")


def reading_of(payload: object) -> dict:
    """What the card shows. A designed card's payload is its layout: the summary line it reads out (``price:
    257.81``) and its text lines, with the ``label: number`` pairs of the summary as the values. Any other payload is
    data: its numbers, with where each sits."""
    if isinstance(payload, dict) and isinstance(payload.get("clir"), dict):
        layouts = [v for v in payload["clir"].values() if isinstance(v, dict)]
        layout = payload["clir"].get("desktop") if isinstance(payload["clir"].get("desktop"), dict) else \
            (layouts[0] if layouts else {})
        shown = [" ".join(str(x) for x in prim.get("lines") or []) for prim in layout.get("prims") or []
                 if isinstance(prim, dict) and prim.get("k") == "text" and prim.get("lines")][:40]
        line = str(payload.get("summary") or layout.get("summary") or "")
        values = [{"at": m.group("label").strip(), "value": float(re.sub(r"[$\u20ac\u00a3,]", "", m.group("num")))}
                  for m in _PAIR.finditer(line)][:40]
        return {"summary": line[:TEXT_MAX], "shown": shown, "values": values}
    return {"summary": "", "shown": [], "values": numbers_in(payload)}


def numbers_in(payload: object, path: str = "", limit: int = 40) -> list[dict]:
    """The numeric readings in a card's payload, with where each sits (``price``, ``rows[0].close``)."""
    out: list[dict] = []
    stack: list[tuple[str, object]] = [(path, payload)]
    for _ in range(2000):  # bounded walk
        if not stack or len(out) >= limit:
            break
        where, node = stack.pop()
        if isinstance(node, bool):
            continue
        if isinstance(node, (int, float)):
            out.append({"at": where or "(value)", "value": node})
        elif isinstance(node, dict):
            stack.extend((f"{where}.{k}" if where else str(k), v) for k, v in reversed(list(node.items())))
        elif isinstance(node, list):
            stack.extend((f"{where}[{i}]", v) for i, v in reversed(list(enumerate(node[:50]))))
    return out


def _refresh(api: Api, item_id: str) -> dict:
    """One run through the engine, the refresh every card lives on; a refusal keeps its sentence and its code."""
    status, out = api("POST", f"/api/ni/items/{item_id}/run", None)
    if status < 400:
        return {"status": status, "answer": out}
    code = out.get("code") if isinstance(out, dict) else None
    return {"status": status, "answer": {"detail": _detail(out), **({"code": str(code)[:40]} if code else {})}}


def _model_calls(api: Api, since: datetime) -> list[dict]:
    """The Neural Interface steps' model calls since the ask (the app's usage view; other NI work in the same
    window would count too, so run on a quiet install)."""
    stamp = urllib.parse.quote(since.strftime("%Y-%m-%d %H:%M:%S"))
    status, usage = api("GET", f"/api/usage?since={stamp}", None)
    if status != 200 or not isinstance(usage, dict):
        return [{"error": f"HTTP {status} {_detail(usage)}"}]
    return [{k: s.get(k) for k in ("step", "model", "local", "calls", "prompt_tokens", "completion_tokens", "cost",
                                    "plan_value")}
            for s in usage.get("ni_steps") or [] if isinstance(s, dict)]


def _audit(api: Api, item_id: str, since: datetime) -> list[dict]:
    """The audit rows that name the card, since the ask, oldest first."""
    status, audit = api("GET", f"/api/audit?limit={AUDIT_LIMIT}", None)
    if status != 200 or not isinstance(audit, dict):
        return [{"error": f"HTTP {status} {_detail(audit)}"}]
    rows = []
    for entry in audit.get("entries") or []:  # bounded by AUDIT_LIMIT
        if not isinstance(entry, dict):
            continue
        text = json.dumps([entry.get("args_summary"), entry.get("result_summary")], default=str)
        if item_id in text and _after(entry.get("ts"), since):
            rows.append({k: entry.get(k) for k in ("ts", "tool", "decision", "ok", "args_summary", "result_summary",
                                                   "error")})
    return list(reversed(rows))


def _after(ts: object, since: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(str(ts))
    except ValueError:
        return True   # an unreadable time is kept, not hidden
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    return stamp >= since.replace(microsecond=0)


# ---- the report --------------------------------------------------------------------------------------------------

def met(end: str, card: dict, expect: str, *, refreshed: bool | None = None, watched: bool | None = None) -> bool:
    """Whether the end is the one ``--expect`` asked for. ``built`` also needs a reading with a number in it, and
    the refresh and the scheduled run when the run asked for them (``refreshed`` / ``watched``: None, not asked)."""
    assert expect in EXPECT, "a known expectation"
    if expect == "any":
        return end not in ("timeout", "error")
    if expect == "built":
        return end == "built" and bool(card.get("values")) and refreshed is not False and watched is not False
    if expect == "declined":
        return end == "declined"
    return end not in ("built", "timeout", "error")


def asked_for(report: dict, plan: Plan) -> tuple[bool | None, bool | None, list[str]]:
    """For a built card: whether the refresh and the scheduled run the run asked for happened (None: not asked),
    and each shortfall in words."""
    if report.get("end") != "built":
        return None, None, []
    why: list[str] = []
    refreshed = watched = None
    if plan.refresh:
        got = report.get("refresh") or {}
        answer = got.get("answer") if isinstance(got.get("answer"), dict) else {}
        refreshed = got.get("status") == 200 and answer.get("status") == "ok"
        if not got:
            why.append("no refresh was made: the card never got its YES")
        elif not refreshed:
            why.append(f"the refresh answered HTTP {got.get('status')}: {_cell(json.dumps(answer, default=str))}")
    if plan.watch_s > 0:
        seen = report.get("watch") or {}
        watched = bool(seen.get("ok_runs"))
        if not watched:
            runs = "; ".join(f"{r.get('status')} {r.get('error') or ''}".strip() for r in seen.get("runs") or [])
            why.append(f"no scheduled run read the source within {seen.get('minutes', round(plan.watch_s / 60, 1))} "
                       f"min" + (f" (runs: {runs})" if runs else ""))
    return refreshed, watched, why


def markdown(report: dict) -> str:
    """The report for a person: the end first, then the timeline, what it tried, the reading, the calls, the audit."""
    card = report.get("card") or {}
    lines = [f"# NI proof: {report['ask']}", "",
             f"- End: **{report.get('end')}** after {report.get('seconds')} s"
             + (f" (expected {report['expect']}: {'met' if report.get('met') else 'NOT met'})" if report.get("expect")
                else ""),
             f"- Card: `{report.get('item_id')}`, state {card.get('state')}, last status {card.get('last_status')}",
             f"- App {report['app'].get('version')}; asked at {report['started_at']}"]
    lines += [f"- Not met: {why}" for why in report.get("why_not") or []]
    lines += ["", "## Models", ""]
    lines += [f"- {s['step']}: {s.get('model') or 'none'} ({s.get('setting')}, {s.get('where')})"
              + (" off" if s.get("off") else "") for s in report["app"].get("steps") or []]
    lines += ["", "## Timeline", "", "| t (s) | card | flow | progress / asks | reason |", "|---|---|---|---|---|"]
    for e in report.get("events") or []:
        lines.append(f"| {e['t']} | {e.get('card')} | {e.get('flow', '')} | "
                     f"{_cell(e.get('progress') or e.get('asks') or _access_cell(e))} | "
                     f"{_cell(e.get('reason') or e.get('error') or '')} |")
    lines += ["", "## Answers given", ""] + ([f"- {t['t']} s: {t['note']} (HTTP {t['status']})"
                                               for t in report.get("taps") or []] or ["- none"])
    tried = next((e["tried"] for e in reversed(report.get("events") or []) if e.get("tried")), [])
    lines += ["", "## Sources tried", ""] + ([f"- {t}" for t in tried] or ["- the card listed none"])
    decline = next((e["decline"] for e in reversed(report.get("events") or []) if e.get("decline")), None)
    if decline:
        lines += ["", "## Decline", "", f"- {decline.get('reason')}: {decline.get('line')}"]
        lines += [f"- link: {u}" for u in decline.get("links") or []]
    lines += ["", "## Reading", "", f"- Summary: {card.get('summary') or '(none)'}"]
    lines += [f"- {v['at']}: {v['value']}" for v in card.get("values") or []] or ["- no numbers on the card"]
    if card.get("shown"):
        lines += ["- Shown: " + _cell(" / ".join(card["shown"]))]
    elif card.get("payload") is not None:
        lines += ["", "```json", json.dumps(card.get("payload"), indent=2, default=str)[:3000], "```"]
    if report.get("refresh"):
        answer = _cell(json.dumps(report["refresh"]["answer"], default=str))
        lines += ["", "## Refresh", "", f"- HTTP {report['refresh']['status']}: {answer}"]
    if report.get("watch"):
        seen = report["watch"]
        lines += ["", "## Watched for the next scheduled run", "",
                  f"- Up to {seen.get('minutes')} min, watched {seen.get('waited_s')} s; states: "
                  + " -> ".join(str(s) for s in seen.get("states") or [])]
        lines += [f"- after {r.get('after_s')} s: run {r.get('ts')} {r.get('status')} {r.get('error') or ''} "
                  f"({r.get('duration_ms')} ms); card then read: {r.get('summary') or '(none)'}"
                  for r in seen.get("runs") or []] or ["- no run in the window"]
    lines += ["", "## Engine runs", ""] + ([f"- {r.get('ts')}: {r.get('status')} {r.get('error') or ''} "
                                            f"({r.get('duration_ms')} ms)" for r in card.get("runs") or []]
                                           or ["- none"])
    lines += ["", "## Model calls since the ask", ""] + (
        [f"- {c.get('step')} on {c.get('model')}: {c.get('calls')} calls, {c.get('prompt_tokens')} + "
         f"{c.get('completion_tokens')} tokens" for c in report.get("model_calls") or [] if "step" in c]
        or ["- none recorded"])
    lines += ["", "## Audit rows for the card", ""] + (
        [f"- {a.get('ts')} {a.get('tool')} {a.get('decision')} ok={a.get('ok')}: {_cell(str(a.get('args_summary')))}"
         for a in report.get("audit") or [] if "tool" in a] or ["- none"])
    return "\n".join(lines) + "\n"


def _access_cell(event: dict) -> str:
    access = event.get("access")
    return f"needs a {'key' if access['key'] else 'contact'} for {access['host']}" if access else ""


def _cell(text: object) -> str:
    return " ".join(str(text).replace("|", "/").split())[:TEXT_MAX]


def write_report(report: dict, out_dir: pathlib.Path, secrets: list[str]) -> tuple[pathlib.Path, pathlib.Path]:
    """Write ``<slug>-<time>.json`` and ``.md``; refuse (ValueError, nothing written) if a key or the token is in
    either, whatever put it there."""
    as_json = json.dumps(report, indent=2, default=str)
    as_md = markdown(report)
    for secret in secrets:  # bounded: the keys given and the token
        if secret and len(secret) >= 6 and (secret in as_json or secret in as_md):
            raise ValueError("the report would carry a key or the token; nothing was written")
    slug = re.sub(r"[^a-z0-9]+", "-", report["ask"].lower()).strip("-")[:48] or "ask"
    stamp = report["started_at"].replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / f"{slug}-{stamp}.json", out_dir / f"{slug}-{stamp}.md"
    json_path.write_text(as_json + "\n", encoding="utf-8")
    md_path.write_text(as_md, encoding="utf-8")
    return json_path, md_path


def _key_args(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:  # bounded: the arguments
        host, sep, name = pair.partition("=")
        if not sep or not host.strip() or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
            raise ProofError(f"--key takes HOST=ENVIRONMENT_VARIABLE, got {pair!r}")
        out[host.strip().lower()] = name.strip()
    return out


def main(argv: list[str] | None = None, *, api: Api | None = None, environ: dict | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ask", required=True)
    ap.add_argument("--base", help=f"the app's address (default $SMARTBRAIN_PROOF_BASE, else {DEFAULT_BASE})")
    ap.add_argument("--token-file")
    ap.add_argument("--consent", choices=("allow", "local", "stop"), default="allow")
    ap.add_argument("--key", action="append", default=[], metavar="HOST=ENV")
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--skip-keyless", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--watch", type=float, default=0.0, metavar="MINUTES",
                    help="after the refresh, wait this long for the card's next scheduled run (0: don't)")
    ap.add_argument("--expect", choices=EXPECT, default="any")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--out", default="ni-proof")
    args = ap.parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    try:
        plan = Plan(ask=args.ask, consent=args.consent, keys=_key_args(args.key), reuse=args.reuse,
                    skip_keyless=args.skip_keyless, yes=args.yes, refresh=args.refresh,
                    watch_s=max(0.0, args.watch) * 60.0, timeout_s=args.timeout,
                    poll_s=args.poll)
        token = "" if api is not None else read_token(args.token_file, env)
        base = args.base or str(env.get("SMARTBRAIN_PROOF_BASE") or "") or DEFAULT_BASE
        report = observe(api or http_api(base, token), plan, environ=env)
    except ProofError as exc:
        print(f"ni-proof: {exc}", file=sys.stderr)
        return 2
    report["expect"] = args.expect
    refreshed, watched, report["why_not"] = asked_for(report, plan)
    report["met"] = met(report["end"], report.get("card") or {}, args.expect, refreshed=refreshed, watched=watched)
    secrets = [str(env.get(name) or "") for name in plan.keys.values()] + ([token] if token else [])
    try:
        paths = write_report(report, pathlib.Path(args.out).expanduser(), secrets)
    except ValueError as exc:
        print(f"ni-proof: {exc}", file=sys.stderr)
        return 2
    print(f"report: {paths[0]}\n        {paths[1]}")
    if report["end"] == "error":
        print(f"ni-proof: {report.get('error')}", file=sys.stderr)
        return 2
    if report["end"] == "timeout":
        return 3
    return 0 if report["met"] else 1


if __name__ == "__main__":
    sys.exit(main())
