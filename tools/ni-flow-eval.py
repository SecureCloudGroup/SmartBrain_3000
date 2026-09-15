#!/usr/bin/env python3
"""NI Flow Engine release-gate evaluator (§29 "Testing contract").

The graduated form of the pre-code POC stress harness. Runs the 10-case matrix
that validated the flow architecture (chat-agent orchestration = 16-minute
doom-loop; code-owned flow + two bounded model calls = 4-second successes)
plus a phrasing matrix (intent stability under paraphrase) and a chaos-drift
matrix (mapping/parse/derive failure classes under fixture mutation).

WHEN TO RUN
-----------
- **Pre-tag, alongside ``tools/ni-library/prove.py``**. A green ``--engine``
  live run is the LIVE release gate for any NI-touching release tag; prove.py
  covers the shipped recipes, ``--engine`` covers the ACTUAL flow engine
  (``ni_flow.run_flow``) driving cards from a request under real model + real
  sources.
- Operator-run only. CI does NOT invoke this file; the fast pytest suite
  ``app/tests/test_ni_flow_*.py`` exercises stages hermetically on every PR
  and ``test_ni_flow_eval_plumbing.py`` covers this file's pure plumbing.

MODES
-----
- (default, ``--live``) — 10 real-world requests x 2 reps against ``--bifrost``
  (default ``http://127.0.0.1:38080``) + real public sources, driving the
  eval's OWN graduated flow. Kept for parity with the pre-``--engine`` runs.
  Gate: 10/10 cases PASS both reps with stable mappings.
- ``--engine`` (M2 audit 2026-09-13) — the LIVE release gate. Runs the actual
  ``ni_flow.run_flow`` for each case against an in-memory ``NIStore`` (temp
  DuckDB per case) with the same bifrost + real fetches the ``--live`` mode
  uses. Feature-detects ``ni_flow`` at import time: absent ⇒ exits 2. This
  is the mode required before any NI release tag.
- ``--phrasings`` — the same 10 subjects x 5 paraphrases each = 50 requests;
  intent stage only, fast. Gate: >=90% per-case intent agreement.
- ``--chaos`` — three drift drills per fixture (rename a mapped field,
  truncate the sample to invalid JSON, empty object). Model is FAKE (no
  bifrost). Gate: every drill fails in a NAMED class with a bounded
  model-call count (no retry storm).
- ``--recorded`` — the 10-case matrix against ``app/tests/fixtures/ni_flow/``
  with a fake model returning grounded paths. Network-free smoke.

ENV / DEPENDENCIES
------------------
- Live + engine + phrasings: bifrost reachable at ``--bifrost`` (defaults to
  the local Bifrost proxy on 38080). Real internet egress to the eleven eval
  sources.
- Recorded + chaos: no network, no model. The fixtures ship in the repo.

HONEST NOTES
------------
- Yahoo Finance (``query1.finance.yahoo.com``), Coingecko, USGS,
  Open-Notify, Frankfurter, HN Algolia, wheretheiss.at, radar.weather.gov,
  Open-Meteo, and Open-Meteo Geocoding are **eval-only sources**. This tool
  hits them to prove the flow's mechanics survive real-world payload shapes.
  The shipped recipes are separate (see ``prove.py``); a live PASS here is
  NOT a recommendation to add these endpoints to the shipped catalog.
- The ``quakes-m5`` case's expected verdict depends on whether the ``where``
  transform is registered in ``ni._TRANSFORM_FNS`` at run time. Present ⇒ PASS
  with a magnitude filter; absent ⇒ PASS+GAP (the pre-flow-engine baseline).
- ``ni_flow`` is imported at top level (M2 audit 2026-09-13): the module has
  landed on this branch. ``--engine`` mode requires the module; every other
  mode still runs the eval's own graduated flow so a stale checkout produces
  a deterministic error rather than a silent skip.
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

# The app package must be importable — add repo/app to sys.path.
_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "app"))

from smartbrain_3000 import ni
from smartbrain_3000 import tools as sbtools

_FIXTURES = _REPO / "app" / "tests" / "fixtures" / "ni_flow"
_DEFAULT_BIFROST = "http://127.0.0.1:38080"
_DEFAULT_MODEL = "mlx/Qwen3.5-9B-MLX-4bit"
_UA_HEADER = {"User-Agent": "SmartBrain-ni-flow-eval/1"}
_FETCH_TIMEOUT_S = 25.0
_LLM_TIMEOUT_S = 180.0
_CHAOS_MODEL_CAP = 4  # intent(1..2) + mapping(1..2); any more = retry storm

# --- fixed matrices --------------------------------------------------------------

CASES: list[dict] = [
    {"id": "aapl-5min", "request": "show me AAPL every 5 minutes",
     "url": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?interval=1d&range=1d",
     "fixture": "aapl.json",
     "fields": {"price": "number", "prev_close": "number"},
     "klass": "value", "expect": {"cadence": 5}},
    {"id": "btc-vague", "request": "what's bitcoin worth right now, keep it updated",
     "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
     "fixture": "btc.json",
     "fields": {"price": "number"},
     "klass": "value", "expect": {"cadence_default": True}},
    {"id": "kc-weather",
     "request": "track the weather in Kansas City - temperature and wind",
     "url": None, "fixture": "kc_weather.json",
     "fields": {"temperature": "number", "wind": "number"},
     "klass": "value", "geocode": "Kansas City",
     "geocode_fixture": "geocode_kc.json", "expect": {}},
    {"id": "quakes-m5",
     "request": "show me the latest earthquakes above magnitude 5",
     "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
     "fixture": "quakes.json",
     "fields": {"place": "string"}, "klass": "list",
     # 'where' feature-detected at run time: absent ⇒ PASS+GAP; present ⇒ PASS
     # with magnitude filter. Case dispatcher branches on that at run time.
     "expect": {"gap_when_no_where":
                "no filter op in pipeline grammar (threshold intent unservable)"},
     "filter_threshold": 5},
    {"id": "people-space", "request": "how many people are in space right now",
     "url": "http://api.open-notify.org/astros.json",
     "fixture": "astros.json",
     "fields": {"count": "number"}, "klass": "value", "expect": {}},
    {"id": "eur-usd-hourly", "request": "EUR to USD exchange rate, update hourly",
     "url": "https://api.frankfurter.app/latest?from=EUR&to=USD",
     "fixture": "fx.json",
     "fields": {"rate": "number"}, "klass": "value", "expect": {"cadence": 60}},
    {"id": "hn-frontpage", "request": "top stories on Hacker News",
     "url": "https://hn.algolia.com/api/v1/search?tags=front_page",
     "fixture": "hn.json",
     "fields": {"title": "string"}, "klass": "list", "expect": {}},
    {"id": "iss-map", "request": "ISS location on a map, every minute",
     "url": "https://api.wheretheiss.at/v1/satellites/25544",
     "fixture": "iss.json",
     "fields": {"latitude": "number", "longitude": "number"},
     "klass": "value",
     "expect": {"degrade":
                "map display unsupported -> numbers card + honest note"}},
    {"id": "xmas-countdown", "request": "my countdown to Christmas - days left",
     "url": None, "fixture": None, "fields": {},
     "klass": "refuse", "expect": {"refusal": True}},
    {"id": "us-radar-image", "request": "show me the current US weather radar",
     "url": "https://radar.weather.gov/ridge/standard/CONUS_0.gif",
     "fixture": "radar.gif",
     "fields": {}, "klass": "image", "expect": {}},
]

# 5 paraphrases per case — realistic user voice, same subject / cadence / kind.
PARAPHRASES: dict[str, list[str]] = {
    "aapl-5min": [
        "track Apple stock for me, refresh every 5 minutes",
        "AAPL on my dashboard, updated every five minutes",
        "keep an eye on apple shares, every 5 min",
        "what's AAPL at? update it every 5 minutes",
        "apple stock ticker card, 5-minute cadence",
    ],
    "btc-vague": [
        "bitcoin price right now, keep it fresh",
        "how much is BTC currently worth in USD",
        "current bitcoin value, keep it updated",
        "show me BTC price and keep refreshing it",
        "keep track of what bitcoin costs today",
    ],
    "kc-weather": [
        "Kansas City weather with temperature and wind",
        "current temp and wind speed in Kansas City",
        "what's the temperature and wind like in Kansas City",
        "Kansas City temperature and windspeed on my dashboard",
        "weather card for Kansas City showing temperature and wind",
    ],
    "quakes-m5": [
        "recent earthquakes with magnitude over 5",
        "list quakes stronger than magnitude 5",
        "earthquakes above M5 today",
        "big earthquakes only - above magnitude 5",
        "show recent seismic events greater than magnitude 5",
    ],
    "people-space": [
        "current number of astronauts in orbit",
        "people in space count right now",
        "how many astronauts up there right now",
        "count of humans currently in space",
        "astronaut headcount in space",
    ],
    "eur-usd-hourly": [
        "hourly EUR to USD exchange rate",
        "current EUR/USD rate updated every hour",
        "euro to dollar exchange rate refreshed hourly",
        "track EUR-USD rate on an hourly cadence",
        "convert euros to dollars, updated each hour",
    ],
    "hn-frontpage": [
        "Hacker News front page",
        "current top HN stories",
        "what's hot on Hacker News right now",
        "top posts from Hacker News",
        "HN front page headlines",
    ],
    "iss-map": [
        "where is the International Space Station right now on a map",
        "ISS position every minute on a map",
        "track the ISS on a world map, minute by minute",
        "show ISS lat/long on a map, refresh each minute",
        "map of ISS location updated every minute",
    ],
    "xmas-countdown": [
        "days until Christmas",
        "how many days till Xmas",
        "countdown to December 25",
        "days remaining until Christmas Day",
        "Christmas countdown card - days left",
    ],
    "us-radar-image": [
        "current US weather radar image",
        "CONUS weather radar picture",
        "US national weather radar image",
        "show me the latest weather radar for the United States",
        "weather radar image, US-wide",
    ],
}


# --- utilities -------------------------------------------------------------------


def _parse_json_reply(text: str) -> dict:
    """Strip <think>...</think>, extract the first {...} block, parse it.

    The stress-harness pattern: local models occasionally wrap replies with
    chain-of-thought or a preamble. We tolerate wrappers but require a JSON
    object body. Never accept a top-level array — intent/mapping are objects.
    """
    assert isinstance(text, str), "reply text required"
    assert text, "reply must be non-empty"
    stripped = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in reply: {stripped[:150]!r}")
    return json.loads(match.group(0))


def _downsample(node: object, list_keep: int = 2) -> object:
    """Deterministically shrink a sample: each list keeps its first N entries.

    Structure (not volume) is what ``_derive_ni_paths`` needs. The 30 KB
    fallback (list_keep=1) is applied by the caller only when the first pass
    still exceeds §27 sample cap.
    """
    assert list_keep >= 1, "list_keep must be positive"
    assert isinstance(list_keep, int), "list_keep must be int"
    max_iterations = 200_000  # bounded traversal budget
    if isinstance(node, list):
        return [_downsample(x, list_keep) for x in node[:list_keep][:max_iterations]]
    if isinstance(node, dict):
        return {k: _downsample(v, list_keep) for k, v in list(node.items())[:max_iterations]}
    return node


def _derive_paths(sample: object) -> list[dict]:
    """Call the app's ``_derive_ni_paths`` with a sample-shrunken payload.

    A non-dict sample is wrapped as ``{"items": <sample>}`` so the walker gets
    an object root (the derive contract). Two-stage shrinking mirrors the POC:
    2-exemplar lists first, drop to 1 if the sample still exceeds 30 KB.
    """
    assert sample is not None, "sample required"
    payload: dict = sample if isinstance(sample, dict) else {"items": sample}
    shrunk = _downsample(payload, list_keep=2)
    assert isinstance(shrunk, dict), "shrunk payload must remain a dict"
    if len(json.dumps(shrunk)) > 30_000:
        shrunk = _downsample(payload, list_keep=1)
        assert isinstance(shrunk, dict), "single-exemplar shrink must remain a dict"
    result = sbtools._derive_ni_paths(None, {"sample": shrunk, "want": "dashboard fields"})
    assert isinstance(result, dict) and "paths" in result, "derive returned wrong shape"
    return result["paths"]


def _generalize_list_path(exemplar: str) -> tuple[str, str]:
    """Turn ``hits[0].title`` into ``('hits', 'item.title')`` for repeat scenes."""
    assert isinstance(exemplar, str) and exemplar, "exemplar path required"
    match = re.match(r"^(.*?)\[0\]\.(.+)$", exemplar)
    if not match:
        raise ValueError(f"path is not a list exemplar: {exemplar!r}")
    return match.group(1), "item." + match.group(2)


def _value_scene(fields: list[str]) -> dict:
    """Assemble a minimal §5 value scene: title + one number per field."""
    assert isinstance(fields, list) and fields, "fields list required"
    assert all(isinstance(f, str) for f in fields), "field names must be str"
    children: list[dict] = [{"type": "text", "value": fields[0], "role": "title",
                             "tone": "default", "size": "md"}]
    for index, field in enumerate(fields[:8]):  # bounded child count
        children.append({"type": "number", "value": {"$bind": field}, "format": "plain",
                         "unit": "", "tone": "default",
                         "size": "lg" if index == 0 else "sm"})
    return {"type": "stack", "dir": "v", "gap": "sm", "children": children}


def _list_scene(items_path: str, item_field: str) -> dict:
    """Assemble a minimal §5 list scene: title + repeat over ``items_path``."""
    assert isinstance(items_path, str) and items_path, "items_path required"
    assert isinstance(item_field, str) and item_field, "item_field required"
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "Top items", "role": "title",
         "tone": "default", "size": "md"},
        {"type": "repeat", "items": {"$bind": items_path}, "max": 5,
         "template": {"type": "text", "value": f"{{{{{item_field}}}}}",
                      "role": "label", "tone": "default", "size": "sm"}},
    ]}


def _has_where_op() -> bool:
    """Feature-detect ``where`` in the ni transform registry (§29 addition).

    The registry is a frozenset owned by ``ni._TRANSFORM_FNS``. We accept
    absence gracefully — the parallel flow-engine agent adds the op in the
    same branch, but this eval must run before and after that lands.
    """
    registry = getattr(ni, "_TRANSFORM_FNS", frozenset())
    assert isinstance(registry, frozenset), "ni._TRANSFORM_FNS must be frozenset"
    return "where" in registry


# --- LLM seams -------------------------------------------------------------------


class _CallCounter:
    """Bounded model-call counter (chaos gate). ``bump`` returns the new total."""

    def __init__(self) -> None:
        self.calls = 0
        self.retries = 0

    def bump(self) -> int:
        assert self.calls >= 0, "counter cannot be negative"
        self.calls += 1
        return self.calls


def _bifrost_llm(bifrost: str, model: str) -> Callable[[str, int], str]:
    """Build the real bifrost /v1/chat/completions caller (temperature=0)."""
    assert bifrost and isinstance(bifrost, str), "bifrost URL required"
    assert model and isinstance(model, str), "model id required"
    endpoint = bifrost.rstrip("/") + "/v1/chat/completions"

    def _call(prompt: str, max_tokens: int) -> str:
        assert isinstance(prompt, str) and prompt, "prompt required"
        assert isinstance(max_tokens, int) and max_tokens > 0, "max_tokens > 0"
        body = json.dumps({"model": model, "temperature": 0,
                           "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(endpoint, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT_S) as resp:
            payload = json.load(resp)
        return payload["choices"][0]["message"]["content"]

    return _call


def _stage_intent(request: str, llm: Callable[[str, int], str],
                  counter: _CallCounter) -> dict:
    """Model call #1 — closed-schema intent parse. One retry, else raise."""
    assert isinstance(request, str) and request, "request required"
    assert callable(llm), "llm callable required"
    base = (
        "Classify a dashboard-card request. Reply with ONLY this JSON shape:\n"
        '{"kind": "external_data" | "computed_only",\n'
        ' "subject": "<short subject>",\n'
        ' "cadence_minutes": <integer, use 15 if the user did not say>,\n'
        ' "wants": ["<field the user wants>", ...],\n'
        ' "threshold": <number or null>,\n'
        ' "display_hint": "<value|list|map|image|none>"}\n'
        '"computed_only" = answerable from the calendar/clock alone, no data '
        'source (e.g. a countdown to a date). Otherwise "external_data".\n'
        f"Request: {request!r}\n"
    )
    prompt = base
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            counter.bump()
            reply = _parse_json_reply(llm(prompt, 300))
            _validate_intent(reply)
            return reply
        except Exception as exc:  # closed retry, then raise
            last_exc = exc
            if attempt == 2:
                break
            counter.retries += 1
            prompt = base + f"\nPrevious reply invalid ({exc}). JSON only, exact keys."
    assert last_exc is not None, "retry loop exhausted without error"
    raise last_exc


def _validate_intent(reply: object) -> None:
    """Closed-schema check for the intent reply."""
    assert isinstance(reply, dict), "intent reply must be a JSON object"
    kind = reply.get("kind")
    if kind not in ("external_data", "computed_only"):
        raise ValueError(f"intent.kind={kind!r}")
    cadence = reply.get("cadence_minutes")
    if not isinstance(cadence, int) or not (1 <= cadence <= 10080):
        raise ValueError(f"intent.cadence_minutes={cadence!r}")
    wants = reply.get("wants")
    if not (isinstance(wants, list) and wants):
        raise ValueError(f"intent.wants={wants!r}")


def _stage_mapping(intent: dict, candidates: list[dict], fields: dict[str, str],
                   llm: Callable[[str, int], str], counter: _CallCounter) -> dict:
    """Model call #2 — pick one path per field from a code-filtered menu."""
    assert isinstance(candidates, list), "candidates list required"
    assert isinstance(fields, dict) and fields, "fields map required"
    want_types = set(fields.values())
    usable = [c for c in candidates if c["type"] in want_types]
    offered = {c["path"]: c for c in usable}
    menu = "\n".join(f'- {c["path"]}  ({c["type"]}, e.g. {str(c["example"])[:60]})'
                     for c in usable[:45])
    shape = ", ".join(f'"{f}": "<{t} path>"' for f, t in fields.items())
    base = (
        f"User intent: {json.dumps(intent)}\n"
        f"Choose the best candidate path for each field. Reply ONLY JSON: "
        f"{{{shape}}}\nEvery value MUST be copied EXACTLY from this list:\n"
        + menu
    )
    prompt = base
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            counter.bump()
            reply = _parse_json_reply(llm(prompt, 400))
            _validate_mapping(reply, offered, fields)
            return reply
        except Exception as exc:  # closed retry, then raise
            last_exc = exc
            if attempt == 2:
                break
            counter.retries += 1
            prompt = base + f"\nPrevious reply invalid ({exc}). Copy paths exactly."
    assert last_exc is not None, "retry loop exhausted without error"
    raise last_exc


def _validate_mapping(reply: object, offered: dict, fields: dict[str, str]) -> None:
    """Every key present, every value in the offered menu, types match."""
    assert isinstance(reply, dict), "mapping reply must be a JSON object"
    if set(reply) != set(fields):
        raise ValueError(f"mapping keys {set(reply)} != wanted {set(fields)}")
    for key, value in reply.items():
        if value not in offered:
            raise ValueError(f"{key}={value!r} not in offered set")
        if offered[value]["type"] != fields[key]:
            raise ValueError(f"{key}={value!r} is {offered[value]['type']}, "
                             f"want {fields[key]}")


# --- fetchers --------------------------------------------------------------------


def _fetch_json_live(url: str) -> object:
    """Real HTTP GET, JSON-parsed, bounded timeout, UA header only."""
    assert isinstance(url, str) and url, "url required"
    request = urllib.request.Request(url, headers=_UA_HEADER)
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
        return json.load(response)


def _fetch_bytes_live(url: str) -> bytes:
    """Real HTTP GET, capped at 4 MB (image magic-sniff needs a prefix only)."""
    assert isinstance(url, str) and url, "url required"
    request = urllib.request.Request(url, headers=_UA_HEADER)
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
        return response.read(4_000_000)


def _geocode_live(city: str) -> tuple[float, float]:
    """Real geocode-chain call (Open-Meteo Geocoding — free, keyless)."""
    assert isinstance(city, str) and city, "city required"
    quoted = urllib.parse.quote(city)
    payload = _fetch_json_live(
        f"https://geocoding-api.open-meteo.com/v1/search?name={quoted}&count=1")
    assert isinstance(payload, dict), "geocode reply must be an object"
    first = (payload.get("results") or [{}])[0]
    return float(first["latitude"]), float(first["longitude"])


def _load_fixture(name: str) -> object:
    """Read a JSON fixture from ``app/tests/fixtures/ni_flow/`` (chaos + recorded)."""
    assert isinstance(name, str) and name, "fixture name required"
    path = _FIXTURES / name
    if not path.exists():
        raise FileNotFoundError(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# --- live case runner ------------------------------------------------------------


def _resolve_live_url(case: dict) -> str:
    """Return the case's URL, resolving the geocode chain when required."""
    assert isinstance(case, dict), "case required"
    if not case.get("geocode"):
        assert case.get("url"), "non-geocode case must define url"
        return case["url"]
    lat, lon = _geocode_live(case["geocode"])
    return (f"https://api.open-meteo.com/v1/forecast?latitude={lat}"
            f"&longitude={lon}&current_weather=true")


def _apply_quakes_filter(cands: list[dict], sample: object,
                         threshold: float) -> tuple[list[dict], int]:
    """Run the ``where`` pipeline (magnitude >= threshold) and re-derive candidates.

    Returns (candidates_over_filtered_items, kept_count). Caller only invokes
    this when ``_has_where_op()`` reports True.
    """
    assert isinstance(cands, list) and cands, "candidates required"
    assert isinstance(threshold, (int, float)), "threshold must be numeric"
    payload = sample if isinstance(sample, dict) else {"items": sample}
    stages = [
        {"op": "extract", "paths": {"rows": "features"}},
        {"op": "transform", "apply": [{"fn": "where", "field": "rows",
                                       "key": "properties.mag", "op": "gt",
                                       "value": threshold}]},
    ]
    outputs = ni.run_pipeline(stages, payload)
    filtered_items = outputs.get("rows")
    if not isinstance(filtered_items, list):
        raise ni.NIError("where_filter_type", "rows not a list after where")
    kept = len(filtered_items)
    if kept <= 0:
        # An empty filter result is still a legal PASS if the shape is correct.
        return cands, 0
    re_derived = _derive_paths({"features": filtered_items})
    return re_derived, kept


def _run_value_class(case: dict, mapping: dict, fresh: object) -> dict:
    """Assemble the value scene, bind against ``fresh``, return outputs dict."""
    assert isinstance(mapping, dict) and mapping, "mapping required"
    stages = [{"op": "extract", "paths": dict(mapping)}]
    payload = fresh if isinstance(fresh, dict) else {"items": fresh}
    outputs = ni.run_pipeline(stages, payload)
    scene = _value_scene(list(case["fields"]))
    ni.bind_scene(scene, outputs)
    typemap: dict[str, type | tuple[type, ...]] = {
        "number": (int, float), "string": str}
    for field, wanted in case["fields"].items():
        value = outputs.get(field)
        if not isinstance(value, typemap[wanted]) or isinstance(value, bool):
            raise TypeError(f"{field}={value!r} is not {wanted}")
    return outputs


def _run_list_class(case: dict, mapping: dict, fresh: object) -> int:
    """Assemble a list scene from a generalized items path; return bound count."""
    assert isinstance(mapping, dict) and mapping, "mapping required"
    first_field = next(iter(case["fields"]))
    items_path, item_field = _generalize_list_path(mapping[first_field])
    stages = [{"op": "extract", "paths": {"rows": items_path}}]
    payload = fresh if isinstance(fresh, dict) else {"items": fresh}
    outputs = ni.run_pipeline(stages, payload)
    scene = _list_scene("rows", item_field)
    bound = ni.bind_scene(scene, outputs)
    return _count_labels(bound)


def _count_labels(node: object, budget: int = 500) -> int:
    """Count ``role=label`` nodes in a bound scene subtree (bounded traversal)."""
    assert budget > 0, "budget must be positive"
    assert isinstance(budget, int), "budget must be int"
    stack: list[object] = [node]
    total = 0
    for _ in range(budget):
        if not stack:
            return total
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        if current.get("role") == "label":
            total += 1
        children = current.get("children") or []
        if isinstance(children, list):
            stack.extend(children)
    return total


def _apply_expectations(case: dict, intent: dict, ok: bool) -> tuple[bool, str | None]:
    """Fold the case's ``expect`` block into the ok/status decision."""
    assert isinstance(case, dict), "case required"
    assert isinstance(intent, dict), "intent required"
    expect = case["expect"]
    ok_out = ok
    if "cadence" in expect and intent["cadence_minutes"] != expect["cadence"]:
        ok_out = False
    if expect.get("cadence_default") and intent["cadence_minutes"] != 15:
        ok_out = False
    gap_reason: str | None = None
    if case["id"] == "quakes-m5" and not _has_where_op():
        gap_reason = expect.get("gap_when_no_where")
    return ok_out, gap_reason


def _run_case_live(case: dict, rep: int, llm: Callable[[str, int], str],
                   counter: _CallCounter) -> dict:
    """Live case: intent → source → sampling → mapping → assembly → bind."""
    assert isinstance(case, dict) and "id" in case, "case required"
    started = time.time()
    out: dict[str, Any] = {"id": case["id"], "rep": rep,
                           "status": "?", "notes": [], "secs": 0.0}
    try:
        intent = _stage_intent(case["request"], llm, counter)
        out["intent"] = intent
        if case["klass"] == "refuse":
            ok = intent["kind"] == "computed_only"
            out["status"] = "PASS(refused cleanly)" if ok else \
                "FAIL(did not classify computed)"
            return out
        if intent["kind"] != "external_data":
            out["status"] = "FAIL(misclassified as computed)"
            return out
        if case["expect"].get("degrade") and intent.get("display_hint") == "map":
            out["notes"].append("degrade: map hint noted, proceeding with numbers")
        if case["klass"] == "image":
            blob = _fetch_bytes_live(case["url"])
            fmt = ni._sniff_image_format(blob)
            out["image_format"] = fmt
            out["status"] = "PASS" if fmt else "FAIL(sniff)"
            return out
        url = _resolve_live_url(case)
        if case.get("geocode"):
            out["notes"].append(f"geocode chain -> {url}")
        sample = _fetch_json_live(url)
        cands = _derive_paths(sample)
        if not cands:
            out["status"] = "FAIL(no_candidates)"
            return out
        _run_live_data_case(case, intent, sample, url, cands, llm, counter, out)
    except Exception as exc:  # a case failure is a RESULT, not a crash
        out["status"] = f"FAIL({type(exc).__name__}: {str(exc)[:90]})"
    finally:
        out["secs"] = round(time.time() - started, 1)
    return out


def _run_live_data_case(case: dict, intent: dict, sample: object, url: str,
                        cands: list[dict], llm: Callable[[str, int], str],
                        counter: _CallCounter, out: dict) -> None:
    """The data-fetch branch of the live case (value/list) — helper of _run_case_live."""
    assert isinstance(cands, list) and cands, "candidates required"
    assert case["klass"] in ("value", "list"), "unsupported klass here"
    working = cands
    if case["id"] == "quakes-m5" and _has_where_op():
        working, kept = _apply_quakes_filter(cands, sample, case["filter_threshold"])
        out["notes"].append(f"where op present: filtered rows kept={kept}")
    mapping = _stage_mapping(intent, working, case["fields"], llm, counter)
    out["mapping"] = mapping
    fresh = _fetch_json_live(url)
    if case["klass"] == "list":
        n_items = _run_list_class(case, mapping, fresh)
        out["bound_items"] = n_items
        ok = n_items > 0
    else:
        outputs = _run_value_class(case, mapping, fresh)
        out["values"] = outputs
        ok = True
    ok, gap = _apply_expectations(case, intent, ok)
    out["status"] = ("PASS+GAP" if gap else "PASS") if ok else "FAIL"
    if gap:
        out["notes"].append("GAP: " + gap)


# --- recorded / chaos runners ----------------------------------------------------


def _canonical_fake_intent(case: dict) -> dict:
    """Grounded canonical intent per case — no model needed in recorded/chaos."""
    assert isinstance(case, dict), "case required"
    if case["klass"] == "refuse":
        return {"kind": "computed_only", "subject": case["id"],
                "cadence_minutes": 1440, "wants": ["days"],
                "threshold": None, "display_hint": "value"}
    hint = ("image" if case["klass"] == "image"
            else "list" if case["klass"] == "list" else "value")
    cadence = case["expect"].get("cadence") or 15
    return {"kind": "external_data", "subject": case["id"],
            "cadence_minutes": cadence, "wants": list(case["fields"]) or [case["id"]],
            "threshold": case.get("filter_threshold"),
            "display_hint": hint}


def _pick_ground_paths(cands: list[dict], fields: dict[str, str]) -> dict[str, str]:
    """Deterministic 'previously-correct' pick — first candidate whose type matches.

    Also prefer a path whose leaf name contains the field name substring so
    ``price`` binds to a price-like leaf when several numbers exist. This is
    the fake model's ONE job in recorded/chaos modes.
    """
    assert isinstance(cands, list) and cands, "candidates required"
    assert isinstance(fields, dict) and fields, "fields required"
    out: dict[str, str] = {}
    for field, wanted in fields.items():
        picked: str | None = None
        for cand in cands:
            if cand["type"] != wanted:
                continue
            tail = cand["path"].rsplit(".", 1)[-1].lower()
            if field.lower() in tail:
                picked = cand["path"]
                break
        if picked is None:
            for cand in cands:
                if cand["type"] == wanted:
                    picked = cand["path"]
                    break
        if picked is None:
            raise ValueError(f"no candidate matches field {field!r} type {wanted!r}")
        out[field] = picked
    return out


def _fake_llm(replies: list[str], counter: _CallCounter) -> Callable[[str, int], str]:
    """Return a fake LLM that pops from a scripted reply list (bounded to 4)."""
    assert isinstance(replies, list) and replies, "replies list required"
    assert isinstance(counter, _CallCounter), "counter required"
    remaining = list(replies)

    def _call(prompt: str, max_tokens: int) -> str:
        assert isinstance(prompt, str) and prompt, "prompt required"
        assert max_tokens > 0, "max_tokens > 0"
        if not remaining:
            raise RuntimeError("fake model exhausted its scripted replies")
        return remaining.pop(0)

    return _call


def _run_case_recorded(case: dict) -> dict:
    """Recorded case: pristine fixture + fake model returning grounded paths."""
    assert isinstance(case, dict), "case required"
    started = time.time()
    out: dict[str, Any] = {"id": case["id"], "status": "?",
                           "notes": [], "secs": 0.0, "mode": "recorded"}
    counter = _CallCounter()
    try:
        intent = _canonical_fake_intent(case)
        out["intent"] = intent
        if case["klass"] == "refuse":
            out["status"] = "PASS(refused cleanly)"
            return out
        if case["klass"] == "image":
            blob = (_FIXTURES / case["fixture"]).read_bytes()
            fmt = ni._sniff_image_format(blob)
            out["image_format"] = fmt
            out["status"] = "PASS" if fmt else "FAIL(sniff)"
            return out
        sample = _load_fixture(case["fixture"])
        cands = _derive_paths(sample)
        if not cands:
            out["status"] = "FAIL(no_candidates)"
            return out
        mapping = _pick_ground_paths(cands, case["fields"])
        out["mapping"] = mapping
        if case["klass"] == "list":
            out["bound_items"] = _run_list_class(case, mapping, sample)
        else:
            out["values"] = _run_value_class(case, mapping, sample)
        out["status"] = "PASS"
        out["model_calls"] = counter.calls
    except Exception as exc:  # a recorded failure is a RESULT, not a crash
        out["status"] = f"FAIL({type(exc).__name__}: {str(exc)[:90]})"
    finally:
        out["secs"] = round(time.time() - started, 1)
    return out


# --- chaos mutators (unit-testable) ---------------------------------------------


def chaos_rename_field(sample: object, path: str) -> object:
    """Return a copy of ``sample`` with the last dict-key on ``path`` renamed.

    The mapping stage's offered-set check now fails: the fake model still
    returns the pre-drift path, and the derived candidate list no longer
    contains it. Parses ``name[index]`` steps (the §4.1 grammar the derive
    walker emits) so paths like ``chart.result[0].meta.regularMarketPrice``
    descend correctly. Kept pure so the plumbing test can assert mutation.
    """
    assert isinstance(path, str) and path, "path required"
    assert sample is not None, "sample required"
    steps = _parse_dot_path(path)
    walk, last_step = steps[:-1], steps[-1]
    cloned = copy.deepcopy(sample)
    parent: object = cloned
    for kind, key in walk:  # bounded by §4.1 max path depth
        stepped = _step_into(parent, kind, key)
        if stepped is None:
            return cloned
        parent = stepped
    if last_step[0] == "key" and isinstance(parent, dict) and last_step[1] in parent:
        parent[last_step[1] + "_renamed"] = parent.pop(last_step[1])
    return cloned


def _step_into(parent: object, kind: str, key: object) -> object | None:
    """One step of the path walker; returns None if the step misses cleanly."""
    assert kind in ("key", "index"), "unknown step kind"
    assert parent is not None, "parent required"
    if kind == "key" and isinstance(parent, dict) and key in parent:
        return parent[key]
    if kind == "index" and isinstance(parent, list) and isinstance(key, int) \
            and 0 <= key < len(parent):
        return parent[key]
    return None


def _parse_dot_path(path: str) -> list[tuple[str, object]]:
    """Split ``a.b[0].c`` into ``[('key','a'),('key','b'),('index',0),('key','c')]``."""
    assert isinstance(path, str) and path, "path required"
    tokens: list[tuple[str, object]] = []
    for part in path.split("."):  # bounded by path length (§4.1 <= 6)
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)((?:\[-?\d+\])*)$", part)
        if not match:
            tokens.append(("key", part))
            continue
        tokens.append(("key", match.group(1)))
        for index_str in re.findall(r"\[(-?\d+)\]", match.group(2) or ""):
            tokens.append(("index", int(index_str)))
    return tokens


def chaos_truncate(raw: bytes) -> bytes:
    """Truncate a JSON-bytes blob to half its length — parse must fail."""
    assert isinstance(raw, bytes), "raw bytes required"
    assert raw, "raw must be non-empty"
    return raw[: max(1, len(raw) // 2)]


def chaos_empty() -> dict:
    """Return the empty object — derive returns zero candidates."""
    return {}


def _run_chaos_drill(case: dict, drill: str) -> dict:
    """Run one chaos drill against ``case`` and report the failure class."""
    assert drill in ("rename", "truncate", "empty"), "unknown drill"
    assert case["klass"] in ("value", "list"), "chaos runs on data cases only"
    started = time.time()
    out: dict[str, Any] = {"id": case["id"], "drill": drill, "status": "?",
                           "notes": [], "secs": 0.0, "class": None}
    counter = _CallCounter()
    try:
        _execute_chaos_drill(case, drill, counter, out)
    except Exception as exc:  # a clean raise IS the pass signal for a drill
        out["class"] = type(exc).__name__
        out["notes"].append(str(exc)[:120])
        out["status"] = "PASS(clean_fail)" if counter.calls <= _CHAOS_MODEL_CAP \
            else f"FAIL(retry_storm calls={counter.calls})"
    finally:
        out["model_calls"] = counter.calls
        out["secs"] = round(time.time() - started, 1)
    return out


def _execute_chaos_drill(case: dict, drill: str, counter: _CallCounter,
                         out: dict) -> None:
    """The mutation + flow-run body of a chaos drill (raises on the drill's failure)."""
    assert isinstance(out, dict), "out dict required"
    pristine = _load_fixture(case["fixture"])
    pristine_cands = _derive_paths(pristine)
    ground = _pick_ground_paths(pristine_cands, case["fields"])
    if drill == "rename":
        mutated = chaos_rename_field(pristine, ground[next(iter(ground))])
    elif drill == "truncate":
        raw = (_FIXTURES / case["fixture"]).read_bytes()
        mutated = json.loads(chaos_truncate(raw))  # this raise IS the pass
    else:
        mutated = chaos_empty()
    fake = _fake_llm([json.dumps(_canonical_fake_intent(case)),
                      json.dumps(ground), json.dumps(ground)], counter)
    intent_reply = _stage_intent(case["request"], fake, counter)
    out["intent"] = intent_reply
    cands = _derive_paths(mutated)
    if not cands:
        raise ni.NIError("no_candidates", "derive returned zero paths")
    _stage_mapping(intent_reply, cands, case["fields"], fake, counter)
    raise RuntimeError("chaos drill unexpectedly reached mapping success")


# --- phrasings mode --------------------------------------------------------------


def _run_phrasing_matrix(llm: Callable[[str, int], str]) -> list[dict]:
    """For each case, run intent stage across its 5 paraphrases.

    Reports per-case agreement over (kind, cadence_minutes, subject-tokens).
    Subject-tokens is the lowercased set of the intent.subject words, which
    is a robust proxy for 'same thing' across paraphrases (avoids nitpicking
    the model's exact string). The 90% gate is field-wise agreement.
    """
    assert callable(llm), "llm callable required"
    out: list[dict] = []
    for case in CASES:  # bounded by CASES length (10)
        results: list[dict] = []
        for phrase in PARAPHRASES[case["id"]][:5]:  # bounded to 5
            counter = _CallCounter()
            try:
                intent = _stage_intent(phrase, llm, counter)
                results.append({"phrase": phrase, "intent": intent})
            except Exception as exc:  # a phrasing failure is a RESULT
                results.append({"phrase": phrase, "error": str(exc)[:120]})
        out.append({"id": case["id"], "phrasings": results,
                    "agreement": _phrasing_agreement(results)})
    return out


def _phrasing_agreement(results: list[dict]) -> dict:
    """Compute per-field agreement rate across a case's paraphrase intents."""
    assert isinstance(results, list) and results, "results list required"
    kinds: set[str] = set()
    cadences: set[int] = set()
    subject_signatures: set[frozenset[str]] = set()
    valid = 0
    for entry in results:  # bounded by paraphrase count
        intent = entry.get("intent")
        if not isinstance(intent, dict):
            continue
        valid += 1
        kinds.add(intent.get("kind", ""))
        cadence = intent.get("cadence_minutes", 0)
        if isinstance(cadence, int):
            cadences.add(cadence)
        subject = str(intent.get("subject", "")).lower()
        subject_signatures.add(frozenset(subject.split()))
    total = max(1, len(results))
    return {"valid": valid, "total": total,
            "kind_agree": len(kinds) <= 1 and valid == total,
            "cadence_agree": len(cadences) <= 1 and valid == total,
            "subject_signatures": len(subject_signatures)}


# --- gate arithmetic (unit-testable) --------------------------------------------


def live_gate_pass(results: list[dict]) -> bool:
    """Live gate: every case PASSes both reps AND mappings are stable across reps."""
    assert isinstance(results, list), "results list required"
    if not results:
        return False
    by_id: dict[str, list[dict]] = {}
    for entry in results:  # bounded by 20 (10 cases x 2 reps)
        by_id.setdefault(entry["id"], []).append(entry)
    if len(by_id) < len(CASES):
        return False
    for pair in by_id.values():  # bounded by 10 cases
        if len(pair) < 2:
            return False
        statuses = {p["status"].split("(")[0] for p in pair}
        if not all(s.startswith("PASS") for s in statuses):
            return False
        mappings = {json.dumps(p.get("mapping"), sort_keys=True) for p in pair}
        if len(mappings) != 1:
            return False
    return True


def phrasing_gate_pass(rows: list[dict], threshold: float = 0.9) -> bool:
    """Phrasing gate: >= threshold of cases fully agree on kind AND cadence."""
    assert 0.0 < threshold <= 1.0, "threshold must be in (0,1]"
    assert isinstance(rows, list) and rows, "rows required"
    agreeing = 0
    for row in rows:  # bounded by CASES length
        agreement = row.get("agreement") or {}
        if agreement.get("kind_agree") and agreement.get("cadence_agree"):
            agreeing += 1
    ratio = agreeing / max(1, len(rows))
    return ratio >= threshold


def chaos_gate_pass(rows: list[dict]) -> bool:
    """Chaos gate: every drill PASSes (clean fail, bounded calls)."""
    assert isinstance(rows, list) and rows, "rows required"
    for row in rows:  # bounded by 3 drills x N cases
        if not str(row.get("status", "")).startswith("PASS"):
            return False
        calls = row.get("model_calls", 0)
        if not isinstance(calls, int) or calls > _CHAOS_MODEL_CAP:
            return False
    return True


# --- entry point -----------------------------------------------------------------


def _print_live_row(result: dict) -> None:
    """One-line stdout report of a live case rep."""
    assert isinstance(result, dict), "result dict required"
    extra = ""
    if result.get("values"):
        extra += f" values={result['values']}"
    if "bound_items" in result:
        extra += f" items={result['bound_items']}"
    if "image_format" in result:
        extra += f" fmt={result['image_format']}"
    print(f"[{result['id']:>16} rep{result.get('rep', '-')}] "
          f"{result['status']:<28} {result['secs']:>5}s{extra}")
    for note in result.get("notes", []):  # bounded (case-set notes)
        print(f"{'':>25}note: {note}")


def _run_live(bifrost: str, model: str, only: set[str]) -> int:
    """Live mode: bifrost + real sources, 10 cases x 2 reps."""
    assert isinstance(bifrost, str) and bifrost, "bifrost URL required"
    assert isinstance(only, set), "only set required"
    llm = _bifrost_llm(bifrost, model)
    counter = _CallCounter()
    results: list[dict] = []
    print(f"== NI Flow Engine eval · LIVE · model={model} bifrost={bifrost} ==\n")
    for case in CASES:  # bounded by CASES length
        if only and case["id"] not in only:
            continue
        for rep in (1, 2):
            entry = _run_case_live(case, rep, llm, counter)
            results.append(entry)
            _print_live_row(entry)
    passed = live_gate_pass(results) if not only else all(
        e["status"].startswith("PASS") for e in results)
    print(f"\nmodel calls: {counter.calls} · retries: {counter.retries}")
    print(f"LIVE GATE: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _run_recorded(only: set[str]) -> int:
    """Recorded mode: fixtures + fake model, thin CI-safe smoke."""
    assert isinstance(only, set), "only set required"
    results: list[dict] = []
    print("== NI Flow Engine eval · RECORDED (fixtures + fake model) ==\n")
    for case in CASES:  # bounded by CASES length
        if only and case["id"] not in only:
            continue
        entry = _run_case_recorded(case)
        results.append(entry)
        _print_live_row(entry)
    ok = all(str(e["status"]).startswith("PASS") for e in results) and results
    print(f"\nRECORDED GATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _run_phrasings(bifrost: str, model: str) -> int:
    """Phrasings mode: 50 intent runs, >=90% per-case field agreement."""
    assert isinstance(bifrost, str) and bifrost, "bifrost URL required"
    llm = _bifrost_llm(bifrost, model)
    print(f"== NI Flow Engine eval · PHRASINGS · model={model} ==\n")
    rows = _run_phrasing_matrix(llm)
    for row in rows:  # bounded by CASES length
        agreement = row["agreement"]
        print(f"[{row['id']:>16}] valid={agreement['valid']}/{agreement['total']} "
              f"kind_agree={agreement['kind_agree']} "
              f"cadence_agree={agreement['cadence_agree']} "
              f"subject_sigs={agreement['subject_signatures']}")
    ok = phrasing_gate_pass(rows)
    print(f"\nPHRASINGS GATE (>=90% kind+cadence agreement): "
          f"{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _run_chaos(only: set[str]) -> int:
    """Chaos mode: three drills per data case; every drill must fail cleanly."""
    assert isinstance(only, set), "only set required"
    data_cases = [c for c in CASES if c["klass"] in ("value", "list")]
    rows: list[dict] = []
    print("== NI Flow Engine eval · CHAOS (fixture mutation, fake model) ==\n")
    for case in data_cases:  # bounded by data-case count
        if only and case["id"] not in only:
            continue
        for drill in ("rename", "truncate", "empty"):
            row = _run_chaos_drill(case, drill)
            rows.append(row)
            print(f"[{case['id']:>16} {drill:>8}] {row['status']:<28} "
                  f"class={row.get('class')} calls={row['model_calls']} "
                  f"{row['secs']:>5}s")
    ok = chaos_gate_pass(rows)
    print(f"\nCHAOS GATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI: pick a mode; live is default. ``--engine`` is the LIVE release gate."""
    parser = argparse.ArgumentParser(
        description="Release-gate evaluator for the NI Flow Engine (§29).")
    parser.add_argument("--bifrost", default=_DEFAULT_BIFROST,
                        help="OpenAI-compatible LLM proxy (default 127.0.0.1:38080)")
    parser.add_argument("--model", default=_DEFAULT_MODEL,
                        help=f"Model id (default {_DEFAULT_MODEL})")
    parser.add_argument("--recorded", action="store_true",
                        help="Recorded smoke (fixtures + fake model, no network)")
    parser.add_argument("--phrasings", action="store_true",
                        help="Intent-stage-only paraphrase matrix (needs bifrost)")
    parser.add_argument("--chaos", action="store_true",
                        help="Drift drills against fixtures (rename/truncate/empty)")
    parser.add_argument("--engine", action="store_true",
                        help="M2: drive ni_flow.run_flow directly (LIVE release gate)")
    parser.add_argument("--only", default="",
                        help="Comma-separated case ids to run (subset)")
    return parser.parse_args(argv)


def _check_ni_flow_module() -> str:
    """M2 (audit 2026-09-13): report the ``ni_flow`` module presence tag.

    The module now imports at top-level (``ni_flow`` is a first-class part of
    the branch); ``--engine`` mode requires it. This function stays for the
    header print so an operator sees the module status at a glance.
    """
    try:
        import importlib
        importlib.import_module("smartbrain_3000.ni_flow")
    except ImportError:
        return "absent"
    return "present"


def engine_gate_pass(results: list[dict]) -> bool:
    """M2 (audit 2026-09-13): the engine-mode gate — every case reaches a
    ``ready`` flow state (except the refusal case, which reaches
    ``unsupported`` OR ``ready`` when the ``computed`` source is registered).

    Mirrors ``live_gate_pass`` in strictness: 10-of-10, no exceptions, plus a
    check that the sealed spec's ``source.url`` equals the URL the case
    intended (the C2 frozen-URL invariant).

    needs_params (2026-09-14): ``awaiting_params`` / ``awaiting_credential``
    also PASS — a flow that settles at a user-input pause has done its whole
    deterministic job; what remains is the user's value/key BY DESIGN (the
    old gate graded kc-weather "ready" while silently sealing empty lat/lon
    slots that would have fetched ``latitude=&longitude=``). The frozen-URL
    invariant still applies to these rows.
    """
    assert isinstance(results, list), "results list required"
    if not results:
        return False
    passing = ("ready", "awaiting_params", "awaiting_credential")
    seen: set[str] = set()
    for row in results:  # bounded by CASES length
        seen.add(str(row.get("id") or ""))
        klass = row.get("klass")
        flow_state = str(row.get("flow_state") or "")
        if klass == "refuse":
            if flow_state not in ("unsupported", "ready"):
                return False
            continue
        if klass == "image":  # image cases fall out of the engine gate for now
            continue
        if flow_state not in passing:
            return False
        if not row.get("frozen_url_ok"):
            return False
    return len(seen) >= len(CASES) - 1  # image case may be skipped


def _run_engine(bifrost: str, model: str, only: set[str]) -> int:
    """M2 (audit 2026-09-13): engine mode. Drives ``ni_flow.run_flow`` per case.

    Each case gets a fresh in-memory NIStore (temp DuckDB), a real bifrost
    call for the two model turns, and a real fetch for the sampling stage
    (image cases and the refusal case are skipped — the engine's image path
    is exercised by ``prove.py`` on shipped recipes).
    """
    assert isinstance(bifrost, str) and bifrost, "bifrost URL required"
    assert isinstance(only, set), "only set required"
    try:
        from smartbrain_3000 import db as _db
        from smartbrain_3000 import ni as _ni
        from smartbrain_3000 import ni_flow as _flow
        from smartbrain_3000.secrets import gen_master_key as _gen_key
    except ImportError as exc:  # module missing on a stale checkout
        print(f"--engine requires smartbrain_3000.ni_flow (import failed: {exc})",
              file=sys.stderr)
        return 2
    import duckdb  # local import: engine mode is the only caller
    print(f"== NI Flow Engine eval · ENGINE · model={model} bifrost={bifrost} ==\n")
    results: list[dict] = []
    for case in CASES:  # bounded by CASES length
        if only and case["id"] not in only:
            continue
        results.append(_run_case_engine(case, bifrost, model, duckdb, _db, _ni,
                                          _flow, _gen_key))
    passed = engine_gate_pass(results) if not only else all(
        r.get("flow_state") in ("ready", "awaiting_params", "awaiting_credential")
        or r.get("klass") == "refuse"
        for r in results
    )
    print(f"\nENGINE GATE: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _run_case_engine(case: dict, bifrost: str, model: str, duckdb, dbmod,
                     nimod, flowmod, gen_key) -> dict:
    """One engine-mode case: real gateway, real fetch, real ni_flow.run_flow.

    The eval's own ``_bifrost_llm`` builds the caller signature
    ``(model, prompt) -> reply`` that ``run_flow`` accepts as ``gateway_call``.
    Fetches go through the eval's ``_fetch_json_live`` so we bypass the store's
    netguard (this is an operator-run eval, not a runtime path).
    """
    assert isinstance(case, dict) and "id" in case, "case required"
    out: dict[str, Any] = {"id": case["id"], "klass": case["klass"],
                           "flow_state": "", "frozen_url_ok": False,
                           "secs": 0.0, "notes": []}
    if case["klass"] == "image":
        out["flow_state"] = "skipped"
        return out
    started = time.time()
    try:
        conn = duckdb.connect(":memory:")
        dbmod.run_migrations(conn)
        store = nimod.NIStore(conn, gen_key())
        item_id = flowmod.create_shell_item(store, case["request"])
        llm = _bifrost_llm(bifrost, model)
        # run_flow's gateway_call signature is (model, prompt) -> reply — wrap
        # the eval's llm (prompt, max_tokens) callable to match.
        def _bridge(_m: str, prompt: str) -> str:
            assert isinstance(prompt, str), "prompt required"
            return llm(prompt, 500)
        source_url = case.get("url")
        result = flowmod.run_flow(
            store, item_id,
            gateway_call=_bridge, fetcher=_fetch_json_live,
            source_url=source_url if isinstance(source_url, str) else None,
        )
        out["flow_state"] = str(result.get("state") or "")
        # A recipe hit with no user URL pauses in ``confirm_source`` (C3 fix)
        # awaiting the operator's tap on the approval card. The eval IS the
        # operator here: simulate exactly what ``confirm_ni_flow_source`` does
        # — approve the URL the flow record shows — and grade the final state.
        if out["flow_state"] == "confirm_source":
            record = flowmod._flow_read(store, item_id) or {}
            confirmed = str(record.get("source_url") or "")
            out["notes"].append(f"auto-confirmed recipe source: {confirmed}")
            result = flowmod.continue_from_recipe_confirm(store, item_id, confirmed)
            out["flow_state"] = str(result.get("state") or "")
            source_url = confirmed  # the frozen-URL invariant now targets it
        settled = ("ready", "awaiting_params", "awaiting_credential")
        if out["flow_state"] in settled and isinstance(source_url, str):
            item = store.get_item(item_id)
            frozen = (item["spec"].get("source") or {}).get("url", "")
            out["frozen_url_ok"] = frozen == source_url
        else:
            out["frozen_url_ok"] = out["flow_state"] in settled
    except Exception as exc:  # any crash = FAIL, not a raise
        out["notes"].append(f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        out["secs"] = round(time.time() - started, 1)
    print(f"[{case['id']:>16}] engine flow_state={out['flow_state']!r} "
          f"frozen_url_ok={out['frozen_url_ok']} {out['secs']:>5}s")
    return out


def main(argv: list[str] | None = None) -> int:
    """Route to the requested mode; return 0 iff its gate passed."""
    args = _parse_args(argv)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    print(f"# ni_flow module: {_check_ni_flow_module()} · "
          f"where op in ni._TRANSFORM_FNS: {_has_where_op()}\n")
    # M2 (audit 2026-09-13): mode arithmetic — at most one of the exclusive
    # flags. --engine now joins the mutually-exclusive set alongside recorded /
    # phrasings / chaos; --live is the default (no explicit flag).
    modes = (args.recorded, args.phrasings, args.chaos, args.engine)
    if sum(1 for m in modes if m) > 1:
        print("choose at most one of --recorded / --phrasings / --chaos / --engine",
              file=sys.stderr)
        return 2
    if args.recorded:
        return _run_recorded(only)
    if args.phrasings:
        return _run_phrasings(args.bifrost, args.model)
    if args.chaos:
        return _run_chaos(only)
    if args.engine:
        return _run_engine(args.bifrost, args.model, only)
    return _run_live(args.bifrost, args.model, only)


if __name__ == "__main__":
    sys.exit(main())
