"""NI Flow Engine (§29) — code orchestrates, models fill exactly two blanks.

Field verdict 2026-09-13: a chat model orchestrating the NI lifecycle produced a
16-minute doom-loop; the same model doing two bounded jobs inside a code-owned
state machine produced correct cards in seconds. This module is that state
machine. Every model call is closed-schema, retry-once, fail-clean; every other
stage is pure code (recipe scoring, sample fetch + downsample, deterministic
path derivation, type-filtered mapping menu, template-based scene assembly,
typed verification, handoff into the same store internals ``create_ni_item``
already uses).

Stages per §29: intent (M#1) → source (C) → sampling (C) → mapping (M#2) →
assembly (C) → handoff (C). Consent moments are preserved: a recipe match rides
the vetted-catalog URL; a user-named URL rides ``start_ni_flow`` args (REVIEWED);
no source ⇒ the flow pauses at ``source`` for chat-supplied candidates and
resumes via ``resume_ni_flow`` (REVIEWED) once the user picks.

Freeform ``update_ni_item`` on flow- or recipe-born items is closed at the tool
surface (§29): fixes re-enter the flow at Sampling via ``remap_ni_item``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from . import gateway as _gateway_mod
from . import netguard as _netguard_mod
from . import ni

log = logging.getLogger("smartbrain.ni.flow")

# ---- flow state machine + slot layout ------------------------------------

FLOW_STATES: frozenset[str] = frozenset({
    "intent", "source", "confirm_source", "sampling", "mapping", "assembling",
    "awaiting_credential", "ready", "unsupported", "failed",
})
# C2/C3/H3 (audit 2026-09-13): host-free error MARKERS the frontend labels
# ("Waiting for you to pick a source in chat" / "Waiting for you to approve
# the source"). Sealed alongside a paused non-terminal state (source /
# confirm_source), where a normal error string would be a lie.
AWAITING_SOURCE_PICK = "awaiting_pick"
AWAITING_SOURCE_CONFIRM = "awaiting_confirm"
# M1 (audit 2026-09-13): §29 door-closure marker sealed into the spec at
# creation. is_flow_or_recipe_born reads this FIRST, journal is a fallback for
# pre-M1 items whose seal predates the key.
BORN_MARKERS: frozenset[str] = frozenset({"flow", "recipe", "chat"})
_BORN_KEY = "_born"
# H2 (audit 2026-09-13): the terminal flow states — a `flow` slot in one of
# these must never mask a renderable payload, and each is a clear path (remap /
# confirm / resume / next successful anything overwrites).
_TERMINAL_STATES: frozenset[str] = frozenset({"failed", "unsupported"})
# M3 (audit 2026-09-13): a non-terminal flow older than this at unlock time is
# a stranded shell (the worker died between ticks). The boot-time sweep in
# ``sweep_stranded_flows`` fails those to ``failed(stale)`` so the shell never
# renders "Preparing card…" indefinitely.
_STRANDED_HOURS = 1
_MAX_REQUEST = 2000
_MAX_NOTES = 10
_MAX_NOTE = 200
_MAX_ERROR = 200

# Single-flight registry: at most _MAX_CONCURRENT worker threads and at most
# one per item id (a second call for the same id is dropped, mirroring the
# _L2_WORKER_LOCK precedent).
_MAX_CONCURRENT = 2
_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()

# Downsampling caps (§29 sampling): trim every list to 2 exemplars; if the
# downsampled JSON still exceeds 30KB, re-trim to 1 exemplar.
_DOWNSAMPLE_LIST_KEEP = 2
_DOWNSAMPLE_MAX_BYTES = 30_000
_MAPPING_MENU_CAP = 45          # POC used 45; keep parity for the recorded case corpus
_LLM_MAX_TOKENS_INTENT = 400
_LLM_MAX_TOKENS_MAPPING = 500
_FLOW_MODEL_TIMEOUT_S = 60.0

# Word → (field name, type) mapping (§29 mapping stage): the model chose "°C"
# over 25.7 until the menu was type-filtered — this table drives the filter.
# String-natured field words are kept as ``string``; everything else defaults
# to ``number`` (the useful case for scene binding). Bounded to 4 fields.
_MAX_INTENT_FIELDS = 4
_STRING_FIELD_WORDS: frozenset[str] = frozenset({
    "title", "name", "place", "headline", "subject", "author", "location",
    "city", "state", "country", "region", "message", "status", "label",
    "description", "summary", "text", "url", "link", "when", "time",
})
# C2 (audit 2026-09-13): the POC's ``\b[A-Z]{1,5}\b`` matched single letters
# ("I", "A") and every ALL-CAPS shout ("HN", "US"), then rode a category-blind
# +5 bump — "show me AAPL every 5 minutes" matched fx-usd-eur as easily as
# stock-quote-finnhub. Fix: min 2 chars, explicit stop-word list, and the bump
# ONLY fires when a category keyword also lands (see _score_recipe below).
_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")
_TICKER_STOPWORDS: frozenset[str] = frozenset({
    "AND", "THE", "FOR", "PRO", "PRE", "MAX", "MIN", "USA", "USD", "EUR",
    "GBP", "JPY", "CNY", "HN", "US", "UK", "EU", "OK", "TV", "AM", "PM",
    "ISS", "NASA", "USGS", "SF", "NYC", "LA", "II", "III", "IV", "IX", "XI",
    "GET", "SET", "PUT", "API", "URL", "JSON", "HTTP", "CSS", "HTML",
})

# Recipe scoring (§29 source stage): category+keyword scoring, with the ticker
# bump gated on category corroboration (C2). Threshold at 2 keeps the fx / hn
# / iss reproductions from the audit at NONE.
_RECIPE_SCORE_MIN = 2

# Display class per §29: value / list / (map/image degraded). ``value`` scenes
# stack a title + one primary number + smaller siblings; ``list`` scenes use a
# repeat over a generalized list path.
_DISPLAY_VALUE = "value"
_DISPLAY_LIST = "list"

# Regex for list exemplar generalization: ``hits[0].title`` → (``hits``,
# ``item.title``). Only the FIRST list index is expanded to a repeat root.
_LIST_EXEMPLAR_RE = re.compile(r"^(.*?)\[0\]\.(.+)$")

# Cadence defaults per §29 intent stage.
_DEFAULT_CADENCE = 15
_MIN_CADENCE = 1
_MAX_CADENCE = 10080

# Model-reply parsing (POC): strip an optional ``<think>`` block, then grab
# the outermost ``{...}`` object. Keeps parity with the POC harness.
_THINK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


# ---- flow-slot helpers ---------------------------------------------------

def _now_iso() -> str:
    """Return a UTC ISO-8601 timestamp for the flow slot's ``updated_at`` field."""
    assert True, "invariant: datetime.now(UTC) returns tz-aware"
    stamp = datetime.now(UTC).isoformat()
    assert isinstance(stamp, str) and stamp.endswith("+00:00"), "iso stamp"
    return stamp


def _flow_read(store: ni.NIStore, item_id: str) -> dict | None:
    """Return the current sealed ``flow`` slot payload, or None when absent.

    Reads through ``NIStore.read_snapshot`` (the same path other slots use);
    the sealed payload is a plain dict with the §29 fields we set on write.
    """
    assert store is not None and item_id, "store + id required"
    snap = store.read_snapshot(item_id, "flow")
    if snap is None:
        return None
    payload = snap.get("payload") if isinstance(snap, dict) else None
    return payload if isinstance(payload, dict) else None


def _flow_write(store: ni.NIStore, item_id: str, record: dict) -> None:
    """Seal a full flow record into the ``flow`` slot (upsert).

    ``ok`` is always True on the flow slot — it is a status record, not a
    payload success signal (the board's latest-if-ok-else-last_good fallback
    never consults this slot).
    """
    assert store is not None and item_id, "store + id required"
    assert isinstance(record, dict) and "state" in record, "record must carry a state"
    if record["state"] not in FLOW_STATES:
        raise ValueError(f"flow state must be one of {sorted(FLOW_STATES)}")
    store.write_snapshot(item_id, "flow", record, ok=True)


def _make_record(request: str, state: str, *, source_url: str | None = None,
                 intent: dict | None = None, error: str | None = None,
                 notes: list[str] | None = None) -> dict:
    """Build a flow-slot record with the closed-key shape used by the board."""
    assert isinstance(request, str) and isinstance(state, str), "request + state"
    if state not in FLOW_STATES:
        raise ValueError(f"flow state must be one of {sorted(FLOW_STATES)}")
    out: dict = {
        "state": state,
        "request": request[:_MAX_REQUEST],
        "updated_at": _now_iso(),
        "notes": list(notes or [])[-_MAX_NOTES:],
    }
    if source_url is not None:
        out["source_url"] = source_url[:ni._MAX_URL]
    if intent is not None:
        assert isinstance(intent, dict), "intent must be a dict"
        out["intent"] = intent
    if error is not None:
        out["error"] = error[:_MAX_ERROR]
    return out


def _transition(store: ni.NIStore, item_id: str, state: str, **fields: Any) -> dict:
    """Update the flow slot to ``state`` carrying forward request / intent / notes.

    Merges the current sealed record with any overriding fields so a note or
    an error appended by a stage lands on the same record the next stage sees.
    Returns the new record.
    """
    assert store is not None and item_id and isinstance(state, str), "args required"
    current = _flow_read(store, item_id) or {}
    request = str(fields.pop("request", current.get("request", "")))
    source_url = fields.pop("source_url", current.get("source_url"))
    intent = fields.pop("intent", current.get("intent"))
    error = fields.pop("error", None)
    notes = list(current.get("notes") or [])
    extra_note = fields.pop("note", None)
    if isinstance(extra_note, str) and extra_note:
        notes.append(extra_note[:_MAX_NOTE])
    record = _make_record(request, state, source_url=source_url, intent=intent,
                          error=error, notes=notes[-_MAX_NOTES:])
    _flow_write(store, item_id, record)
    return record


def _append_note(store: ni.NIStore, item_id: str, note: str) -> None:
    """Append one honest-degradation note to the flow record (bounded)."""
    assert store is not None and item_id and isinstance(note, str), "args required"
    current = _flow_read(store, item_id)
    if current is None:
        return
    _transition(store, item_id, current.get("state", "intent"), note=note)


# ---- shell item + provenance helpers -------------------------------------

def _empty_shell_spec(request: str, cadence: int) -> dict:
    """Assemble the tiny sealed spec used for a flow's DRAFT shell item.

    The user's request words ride verbatim as ``goal`` (§2 rule). Source is a
    ``model`` no-op — the shell never runs; commissioning is deferred to the
    flow's Handoff stage which replaces the spec via ``update_spec``. Scene is
    a single one-line title so the preview render never fails.
    """
    assert isinstance(request, str) and request, "request required"
    # Minor (audit 2026-09-13): a whitespace-only first line was landing an
    # empty title; fall back to "New card" so the shell's scene bind never
    # renders a blank tile heading and ``validate_spec`` never trips
    # ``spec.title required``.
    first = request.splitlines()[0].strip() if request else ""
    title_line = first if first else "New card"
    return {
        "version": 1,
        "title": title_line[:ni._MAX_TITLE],
        "goal": request[:ni._MAX_GOAL],
        "params": {},
        "source": {"type": "model", "instruction": "flow shell placeholder"},
        "pipeline": [],
        "scene": {
            "type": "stack", "dir": "v", "gap": "sm", "children": [
                {"type": "text", "value": "Preparing card…", "role": "title",
                 "tone": "muted", "size": "md"},
            ],
        },
        "display": {"size": "small"},
        "contract": None,
        "repair_policy": {"l1": True, "l2_frontier": False},
        "model": None,
        "interval_minutes": max(_MIN_CADENCE, int(cadence)),
    }


def is_flow_or_recipe_born(store: ni.NIStore, item_id: str) -> bool:
    """§29 door closure: True when the item was created via the flow OR a recipe.

    M1 (audit 2026-09-13): the primary signal is the sealed spec's ``_born``
    marker — a spec-shape field validated by ``ni.validate_spec`` and refused
    by ``ni_library.parse_pack`` on template import, so a chat model has no
    surface to forge or clear it. Journal fallback for items sealed before
    M1 landed (their ``_born`` key is absent, but the deterministic
    ``created via flow`` / ``recipe`` journal entries still ride).
    """
    assert store is not None and item_id, "store + id required"
    item = store.get_item(item_id)
    if item is not None:
        born = (item["spec"] or {}).get(_BORN_KEY)
        if born in ("flow", "recipe"):
            return True
        if born == "chat":
            return False
    entries = store.read_journal(item_id)
    for entry in entries:  # bounded by ni._MAX_JOURNAL_ENTRIES
        kind = entry.get("kind") if isinstance(entry, dict) else None
        summary = entry.get("summary") if isinstance(entry, dict) else None
        if kind == "recipe":
            return True
        if kind == "created" and isinstance(summary, str) and "via flow" in summary:
            return True
    return False


# ---- stage 1: intent -----------------------------------------------------

# NB: raw f-string style with a ``__REQUEST__`` sentinel — the JSON braces here
# would clash with ``str.format`` if we used {}-templating, so we ``.replace``.
_INTENT_PROMPT = (
    "Classify a dashboard-card request. Reply with ONLY this JSON shape:\n"
    '{"kind": "external_data" | "computed_only",\n'
    ' "subject": "<short subject>",\n'
    ' "cadence_minutes": <integer, use 15 if the user did not say>,\n'
    ' "wants": ["<field the user wants>", ...],\n'
    ' "threshold": <number or null>,\n'
    ' "display_hint": "<value|list|map|image|none>"}\n'
    '"computed_only" = answerable from the calendar/clock alone, no data source '
    '(e.g. a countdown to a date). Otherwise "external_data".\n'
    "Request: __REQUEST__\n"
)


def _parse_json_reply(text: str) -> dict:
    """POC-parity JSON extractor: strip ``<think>`` and find the outermost object.

    A malformed reply raises ``ValueError`` so the retry-once loop can retry.
    """
    assert isinstance(text, str), "reply text required"
    stripped = _THINK_RE.sub("", text)
    match = _JSON_OBJ_RE.search(stripped)
    if match is None:
        raise ValueError(f"no JSON in reply: {stripped[:120]!r}")
    return json.loads(match.group(0))


def _validate_intent(reply: dict) -> dict:
    """POC-parity closed-schema validation for the intent reply. Raises ValueError."""
    assert isinstance(reply, dict), "reply must be a dict"
    if reply.get("kind") not in ("external_data", "computed_only"):
        raise ValueError("intent.kind must be external_data or computed_only")
    cadence = reply.get("cadence_minutes")
    if not (isinstance(cadence, int) and not isinstance(cadence, bool)
            and _MIN_CADENCE <= cadence <= _MAX_CADENCE):
        raise ValueError("intent.cadence_minutes must be an integer in [1, 10080]")
    wants = reply.get("wants")
    if not (isinstance(wants, list) and wants):
        raise ValueError("intent.wants must be a non-empty list")
    return reply


def stage_intent(request: str, model_call: Callable[[str], str]) -> dict:
    """Stage 1 (M#1): request → closed-schema intent. Retry once on parse/shape failure."""
    assert isinstance(request, str) and request, "request required"
    assert callable(model_call), "model_call required"
    prompt = _INTENT_PROMPT.replace("__REQUEST__", repr(request))
    for attempt in range(2):  # fixed upper bound (P10 #2)
        try:
            reply_text = model_call(prompt)
            return _validate_intent(_parse_json_reply(reply_text))
        except (ValueError, TypeError) as exc:
            if attempt == 1:
                raise ValueError(f"intent stage failed after retry: {exc}") from None
            prompt = prompt + f"\nPrevious reply invalid ({exc}). JSON only, exact keys."
    raise RuntimeError("unreachable — retry loop bounded to 2 attempts")


# ---- stage 2: source (recipe scoring) ------------------------------------

def _ticker_hit(request: str) -> bool:
    """C2 (audit 2026-09-13): a ticker-shaped token that is NOT a stop word.

    Splits by whitespace so ``S&P`` (5 chars but punctuation-fenced) never
    trips the finance path — the regex operates on the original request, but a
    stop-word check on each matched span filters "US", "AND", "HN", etc.
    """
    assert isinstance(request, str), "request required"
    for match in _TICKER_RE.finditer(request):  # bounded by request length
        token = match.group(0)
        if token not in _TICKER_STOPWORDS:
            return True
    return False


def _score_recipe(recipe: dict, request: str, intent: dict) -> int:
    """Deterministic keyword+category scorer.

    Base score: +2 per title word (>=3 chars) present in the request/wants hay,
    +3 when the recipe's category name appears in the hay. Ticker bump lives
    at the caller (match_recipe) and is category-gated per C2.
    """
    assert isinstance(recipe, dict) and isinstance(request, str), "args required"
    haystack_words = (request.lower() + " " + " ".join(
        w for w in intent.get("wants") or [] if isinstance(w, str)
    ).lower()).split()
    hay = set(haystack_words)
    title_words = str(recipe.get("title") or "").lower().split()
    category = str(recipe.get("category") or "").lower()
    score = 0
    for tw in title_words:  # bounded by title length
        if tw in hay and len(tw) >= 3:
            score += 2
    if category and category in hay:
        score += 3
    return score


def _category_corroborated(recipe: dict, request: str, intent: dict) -> bool:
    """C2 (audit 2026-09-13): True iff the recipe's category (or a category-
    keyword synonym for finance) appears in the request/wants hay.

    Requiring corroboration means "show me AAPL every 5 minutes" — with no
    "stock" / "quote" / "price" / "finance" word — reaches NO recipe (the
    audit's stated verdict). "show me AAPL stock" corroborates and matches
    the stock-quote-finnhub recipe.
    """
    assert isinstance(recipe, dict) and isinstance(request, str), "args required"
    hay = (request.lower() + " " + " ".join(
        w for w in intent.get("wants") or [] if isinstance(w, str)
    ).lower())
    category = str(recipe.get("category") or "").lower()
    if category and category in hay:
        return True
    # A small per-category synonym set — narrow on purpose so a stray word does
    # not smuggle a match. Only finance today (the one bump gated on this rule).
    # ``price`` is deliberately absent (too generic — every crypto/weather want
    # carries it); the corroboration needs an explicitly finance-shaped word.
    if category == "finance":
        return any(w in hay for w in ("stock", "quote", "shares", "ticker",
                                       "equity", "share"))
    return False


def match_recipe(catalog: list[dict], request: str, intent: dict) -> dict | None:
    """§29 source stage: score every catalog entry; ticker heuristic → finance.

    C2 (audit 2026-09-13): the ticker bump (+2, was +5) fires ONLY when the
    recipe's category is corroborated in the request text — a bare ALL-CAPS
    token never carries a match on its own. Returns the winning recipe
    (deep copy is caller's responsibility) or None when nothing clears
    ``_RECIPE_SCORE_MIN``.
    """
    assert isinstance(catalog, list) and isinstance(request, str), "args required"
    assert isinstance(intent, dict), "intent must be a dict"
    ticker_hit = _ticker_hit(request)
    best: dict | None = None
    best_score = 0
    for recipe in catalog:  # bounded by ni_catalog._MAX_SOURCES
        assert isinstance(recipe, dict), "catalog entries must be dicts"
        s = _score_recipe(recipe, request, intent)
        if (ticker_hit
                and str(recipe.get("category") or "").lower() == "finance"
                and _category_corroborated(recipe, request, intent)):
            s += 2
        if s > best_score:
            best_score = s
            best = recipe
    if best is None or best_score < _RECIPE_SCORE_MIN:
        return None
    return best


# ---- stage 3: sampling (fetch + downsample + derive) --------------------

def downsample(node: object, list_keep: int = _DOWNSAMPLE_LIST_KEEP) -> object:
    """POC-parity: shrink a sample by keeping the first `list_keep` items of every list.

    Non-recursive: a fixed for-loop over an explicit work stack (P10 #2). The
    walk is bounded by the JSON size the fetcher already capped at 2 MB via
    netguard — well below a pathological deep tree.
    """
    assert isinstance(list_keep, int) and list_keep >= 1, "list_keep >= 1"
    if not isinstance(node, (dict, list)):
        return node
    root: object = _deep_copy_json(node)
    stack: list[object] = [root]
    for _ in range(4096):  # fixed upper bound (P10 #2)
        if not stack:
            break
        cur = stack.pop()
        if isinstance(cur, dict):
            for key in list(cur.keys()):  # bounded by dict size
                val = cur[key]
                if isinstance(val, list):
                    trimmed = val[:list_keep]
                    cur[key] = trimmed
                    stack.extend(v for v in trimmed if isinstance(v, (dict, list)))
                elif isinstance(val, dict):
                    stack.append(val)
        elif isinstance(cur, list):
            # A raw list root — the caller wraps roots in ``{"items": ...}``
            # before downsampling, so hitting one here is either a nested list
            # or an unusual input. Trim in place.
            del cur[list_keep:]
            for v in list(cur):  # bounded by list_keep
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return root


def _deep_copy_json(value: object) -> object:
    """Round-trip through json for a safe deep-copy (bounded by _DOWNSAMPLE_MAX_BYTES)."""
    assert value is not None, "value must not be None"
    return json.loads(json.dumps(value))


def derive_paths(sample: object) -> list[dict]:
    """POC-parity: wrap a bare-list root, downsample, then call tools.walker.

    Late import of ``tools`` avoids the ``ni_flow → tools → ni_flow`` cycle at
    module import time — this function is only ever called from inside a flow
    run, so a defer is safe.
    """
    assert sample is not None, "sample required"
    from . import tools as _sbtools
    wrapped = sample if isinstance(sample, dict) else {"items": sample}
    obj = downsample(wrapped)
    if len(json.dumps(obj)) > _DOWNSAMPLE_MAX_BYTES:
        obj = downsample(obj, list_keep=1)
    result = _sbtools._derive_ni_paths(None, {"sample": obj, "want": "dashboard fields"})
    assert isinstance(result, dict) and "paths" in result, "walker must return {paths, ...}"
    return list(result["paths"])


# ---- stage 4: mapping (M#2, type-filtered menu) --------------------------

def infer_fields(intent: dict) -> dict:
    """Deterministically derive a ``{field_name: type}`` map from ``intent.wants``.

    §29 mapping stage: the menu shown to the model is FILTERED by type. This
    function generalizes the POC's per-case fields into a small table: a
    ``wants`` word listed in ``_STRING_FIELD_WORDS`` maps to ``string``,
    everything else maps to ``number``. Cap _MAX_INTENT_FIELDS entries. Names
    are lowercased snake_case tails so an extract stage can name them.
    """
    assert isinstance(intent, dict), "intent must be a dict"
    wants = intent.get("wants") or []
    out: dict = {}
    for want in wants:  # bounded by intent shape
        if not isinstance(want, str) or not want:
            continue
        key = _slugify_field_name(want)
        if not key or key in out:
            continue
        vtype = "string" if key in _STRING_FIELD_WORDS else "number"
        out[key] = vtype
        if len(out) >= _MAX_INTENT_FIELDS:
            break
    if not out:
        out["value"] = "number"
    assert 1 <= len(out) <= _MAX_INTENT_FIELDS, "fields count in [1, 4]"
    return out


def _slugify_field_name(raw: str) -> str:
    """Lower-case, strip to `[a-z0-9_]`. Never returns a reserved output name.

    Minor (audit 2026-09-13): a want like "24h volume" used to slug to
    "24h_volume", which fails ``ni._KEY_RE`` (extract output names must start
    with a letter or ``_``). Prefix a leading digit with ``f_`` so the mapping
    stage never lands a spec that would fail ``validate_spec`` post-approval.
    """
    assert isinstance(raw, str), "raw must be a string"
    lowered = raw.lower().strip()
    keep = re.sub(r"[^a-z0-9_]+", "_", lowered).strip("_")
    if keep and keep[0].isdigit():
        keep = "f_" + keep
    if keep in ni._RESERVED_OUTPUT_NAMES:
        keep = keep + "_field"
    return keep[:60]


def _path_tail_slug(path: str) -> str:
    """Last key of a derive path, slug-normalized for name comparison.

    ``features[0].properties.time`` → ``time``; ``wind-speed`` → ``wind_speed``.
    """
    assert isinstance(path, str), "path must be a string"
    tail = path.rsplit(".", 1)[-1]
    tail = re.sub(r"\[\d+\]$", "", tail)
    return tail.lower().replace("-", "_")


def _names_match(field: str, tail: str) -> bool:
    """Field/tail name affinity: equal, or one contains the other (len >= 3)."""
    assert isinstance(field, str) and isinstance(tail, str), "args required"
    if not field or not tail:
        return False
    if field == tail:
        return True
    return (len(field) >= 3 and len(tail) >= 3
            and (field in tail or tail in field))


def reconcile_field_types(fields: dict, candidates: list[dict]) -> dict:
    """Ground the word-table field types in the ACTUAL sampled payload.

    ``infer_fields`` types a want from ``_STRING_FIELD_WORDS`` alone — but a
    source is free to disagree (USGS serves ``properties.time`` as an epoch
    NUMBER; the word table says ``time`` is a string). Left alone, the model
    picks the semantically right path and the strict verifier rejects it
    twice — a self-inflicted dead end (quakes-m5, engine gate 2026-09-13).

    Deterministic rule, pure code over the sample: a field's type flips to the
    other scalar type ONLY when (a) no candidate of the inferred type
    name-matches the field, AND (b) at least one candidate of the other type
    does. No name-match on either side keeps the inferred type untouched.
    """
    assert isinstance(fields, dict) and isinstance(candidates, list), "args required"
    tails: dict[str, set[str]] = {"string": set(), "number": set()}
    for cand in candidates:  # bounded by derive walker cap
        if not isinstance(cand, dict):
            continue
        ctype = cand.get("type")
        if ctype in tails and isinstance(cand.get("path"), str):
            tails[ctype].add(_path_tail_slug(cand["path"]))
    out: dict = {}
    for name, vtype in fields.items():  # bounded by _MAX_INTENT_FIELDS
        other = "number" if vtype == "string" else "string"
        if (vtype in tails and other in tails
                and not any(_names_match(name, t) for t in tails[vtype])
                and any(_names_match(name, t) for t in tails[other])):
            out[name] = other
        else:
            out[name] = vtype
    return out


_MAPPING_PROMPT = (
    "User intent: {intent_json}\n"
    "Choose the best candidate path for each field. Reply ONLY JSON: {{{shape}}}\n"
    "Every value MUST be copied EXACTLY from this list:\n{menu}"
)


def build_mapping_menu(candidates: list[dict], fields: dict) -> tuple[list[dict], str]:
    """Filter the derive output to candidates matching the requested field types.

    Returns ``(usable_candidates, menu_string)`` — the menu is the only content
    the model ever sees for path choice, so a type mismatch cannot ride the
    reply. Bounded to _MAPPING_MENU_CAP lines (parity with the POC).
    """
    assert isinstance(candidates, list) and isinstance(fields, dict), "args required"
    want_types = set(fields.values())
    usable = [c for c in candidates if isinstance(c, dict) and c.get("type") in want_types]
    usable = usable[:_MAPPING_MENU_CAP]
    lines = [
        f"- {c['path']}  ({c['type']}, e.g. {_neutralize_example(c.get('example'))})"
        for c in usable
    ]
    return usable, "\n".join(lines)


def _neutralize_example(value: object) -> str:
    """Minor (audit 2026-09-13): collapse newlines/tabs in a fetched example
    string so a JSON body with an embedded ``\\n`` cannot break the M#2 menu
    into a fake "extra line" the model might parse as a candidate path. Cheap
    per-line pass; length still bounded to 60 chars.
    """
    text = str(value if value is not None else "")
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return text[:60]


def stage_mapping(intent: dict, candidates: list[dict], fields: dict,
                  model_call: Callable[[str], str]) -> dict:
    """Stage 4 (M#2): the model picks paths from the type-filtered menu. Retry once."""
    assert isinstance(intent, dict) and isinstance(fields, dict), "args required"
    assert isinstance(candidates, list) and callable(model_call), "args required"
    usable, menu = build_mapping_menu(candidates, fields)
    if not usable:
        raise ValueError("mapping stage: no candidates match the intent's field types")
    offered = {c["path"]: c for c in usable}
    shape = ", ".join(f'"{name}": "<{ftype} path>"' for name, ftype in fields.items())
    prompt = _MAPPING_PROMPT.format(intent_json=json.dumps(intent), shape=shape, menu=menu)
    for attempt in range(2):  # fixed upper bound (P10 #2)
        try:
            reply = _parse_json_reply(model_call(prompt))
            _verify_mapping(reply, offered, fields)
            return reply
        except (ValueError, TypeError, KeyError) as exc:
            if attempt == 1:
                raise ValueError(f"mapping stage failed after retry: {exc}") from None
            prompt = prompt + f"\nPrevious reply invalid ({exc}). Copy paths exactly."
    raise RuntimeError("unreachable — retry loop bounded to 2 attempts")


def _verify_mapping(reply: dict, offered: dict, fields: dict) -> None:
    """Strict-set check: keys match fields; each value is offered AND correctly typed."""
    assert isinstance(reply, dict), "reply must be a dict"
    if set(reply) != set(fields):
        raise ValueError(f"keys {sorted(reply)} != {sorted(fields)}")
    for name, path in reply.items():  # bounded by fields dict size (<=4)
        if not isinstance(path, str) or path not in offered:
            raise ValueError(f"{name}={path!r} not offered")
        if offered[path]["type"] != fields[name]:
            raise ValueError(
                f"{name}={path!r} is {offered[path]['type']}, want {fields[name]}"
            )


# ---- stage 5: assembly (scenes + pipeline + typed verify) ----------------

def _generalize_list_path(exemplar: str) -> tuple[str, str]:
    """``hits[0].title`` → (``hits``, ``item.title``)."""
    assert isinstance(exemplar, str), "exemplar required"
    match = _LIST_EXEMPLAR_RE.match(exemplar)
    if not match:
        raise ValueError(f"not a list exemplar path: {exemplar!r}")
    return match.group(1), "item." + match.group(2)


def value_scene(fields: list[str]) -> dict:
    """Value-class scene: title + one primary number + smaller siblings."""
    assert isinstance(fields, list) and fields, "fields required"
    children: list[dict] = [
        {"type": "text", "value": fields[0], "role": "title",
         "tone": "default", "size": "md"},
    ]
    for i, field in enumerate(fields):  # bounded by _MAX_INTENT_FIELDS
        children.append({
            "type": "number", "value": {"$bind": field}, "format": "plain",
            "unit": "", "tone": "default", "size": "lg" if i == 0 else "sm",
        })
    return {"type": "stack", "dir": "v", "gap": "sm", "children": children}


def list_scene(items_path: str, item_field: str) -> dict:
    """List-class scene: repeat over a generalized list path (up to 5 rows)."""
    assert isinstance(items_path, str) and items_path, "items_path required"
    assert isinstance(item_field, str) and item_field.startswith("item."), "item_field required"
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "Top items", "role": "title",
         "tone": "default", "size": "md"},
        {"type": "repeat", "items": {"$bind": items_path}, "max": 5,
         "template": {"type": "text", "value": f"{{{{{item_field}}}}}",
                      "role": "label", "tone": "default", "size": "sm"}},
    ]}


def assemble_from_mapping(mapping: dict, fields: dict, klass: str,
                          fresh_sample: object) -> dict:
    """Build the (pipeline, scene, preview_payload) triple + verify types.

    Runs the pipeline against ``fresh_sample`` and binds the scene — the same
    round-trip ``ni.validate_spec`` / ``bind_scene`` would run at handoff.
    Returns ``{"pipeline", "scene", "preview_payload"}`` on success; raises
    ValueError with a class-tagged message on typed-verification failure so
    the flow record can honestly say what went wrong.
    """
    assert isinstance(mapping, dict) and isinstance(fields, dict), "args required"
    assert klass in (_DISPLAY_VALUE, _DISPLAY_LIST), "klass must be value or list"
    payload = fresh_sample if isinstance(fresh_sample, dict) else {"items": fresh_sample}
    if klass == _DISPLAY_LIST:
        first_field = next(iter(fields))
        items_path, item_field = _generalize_list_path(mapping[first_field])
        stages = [{"op": "extract", "paths": {"rows": items_path}}]
        scene = list_scene("rows", item_field)
        preview = ni.run_pipeline(stages, payload)
    else:
        stages = [{"op": "extract", "paths": dict(mapping)}]
        scene = value_scene(list(fields))
        preview = ni.run_pipeline(stages, payload)
    _typed_verify(preview, fields, klass)
    return {"pipeline": stages, "scene": scene, "preview_payload": preview}


def _typed_verify(preview: dict, fields: dict, klass: str) -> None:
    """POC-parity typed verification against the freshly-extracted preview."""
    assert isinstance(preview, dict) and isinstance(fields, dict), "args required"
    if klass == _DISPLAY_LIST:
        rows = preview.get("rows")
        if not (isinstance(rows, list) and rows):
            raise ValueError("mapping: list stage produced an empty rows list")
        return
    for name, ftype in fields.items():  # bounded by _MAX_INTENT_FIELDS
        value = preview.get(name)
        if ftype == "number":
            if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
                raise ValueError(f"mapping: {name!r} is not a number (got {type(value).__name__})")
        elif ftype == "string" and not isinstance(value, str):
            raise ValueError(f"mapping: {name!r} is not a string (got {type(value).__name__})")


# ---- stage 6: handoff (create item = ready draft/commissioning) ---------

def _has_source_type(name: str) -> bool:
    """Feature-detect a source type before building a spec that would use it.

    A parallel agent adds ``computed`` to ``ni._SOURCE_TYPES``; this flow
    module never references it before checking membership so a partial merge
    still boots cleanly (§29 refusal path).
    """
    assert isinstance(name, str), "name must be a string"
    return name in ni._SOURCE_TYPES


def _has_transform_fn(name: str) -> bool:
    """Feature-detect a transform op (``where`` lands from a parallel agent)."""
    assert isinstance(name, str), "name must be a string"
    return name in ni._TRANSFORM_FNS


def _pick_display_class(intent: dict) -> str:
    """Value / list decision. Map + image are DEGRADED to value (§29 policy)."""
    assert isinstance(intent, dict), "intent required"
    hint = str(intent.get("display_hint") or "").lower()
    if hint == "list":
        return _DISPLAY_LIST
    return _DISPLAY_VALUE


def build_final_spec(request: str, intent: dict, source: dict, cadence: int,
                     pipeline: list[dict], scene: dict) -> dict:
    """Assemble a §2-shaped spec ready for ``ni.validate_spec`` + store write."""
    assert isinstance(request, str) and isinstance(intent, dict), "args required"
    assert isinstance(source, dict) and isinstance(pipeline, list), "args required"
    assert isinstance(scene, dict), "scene required"
    first = request.splitlines()[0].strip() if request else ""
    title = str(intent.get("subject") or first or "New card")
    return {
        "version": 1,
        "title": title[:ni._MAX_TITLE],
        "goal": request[:ni._MAX_GOAL],
        "params": {},
        "source": source,
        "pipeline": pipeline,
        "scene": scene,
        "display": {"size": "small"},
        "contract": None,
        "repair_policy": {"l1": True, "l2_frontier": False},
        "model": None,
        "interval_minutes": max(_MIN_CADENCE, int(cadence)),
    }


# ---- shell item + create-with-flow helpers ------------------------------

def create_shell_item(store: ni.NIStore, request: str, *,
                      cadence: int = _DEFAULT_CADENCE,
                      allow_duplicate: bool = False) -> str:
    """§29 handoff parity: create a DRAFT shell so the card shows progress immediately.

    The shell's sealed spec is replaced in place by ``_finalize`` — no re-mint,
    same ``item_id`` — so the card the user is watching becomes the real card
    when the flow completes. The initial ``created`` journal entry marks the
    item flow-born (§29 door closure: ``update_ni_item`` refuses source/pipeline
    changes on flow-born items and points at ``remap_ni_item``); the shell is
    stamped ``_born: "flow"`` (M1 audit 2026-09-13) so a journal churn cannot
    defeat the door before finalize replaces the spec.

    M4 (audit 2026-09-13): enforces the same case-insensitive title guard
    ``create_ni_item`` uses — prevalidate has no store, so the check runs at
    execute time (parity with ``_check_duplicate_title``). ``allow_duplicate``
    escapes the guard when the caller explicitly opts in.
    """
    assert store is not None and isinstance(request, str), "store + request required"
    if not request.strip():
        raise ValueError("request required (non-empty)")
    if len(request) > _MAX_REQUEST:
        raise ValueError(f"request exceeds {_MAX_REQUEST} chars")
    shell = _empty_shell_spec(request, cadence)
    if not allow_duplicate:
        _check_shell_title_duplicate(store, str(shell.get("title") or ""))
    shell[_BORN_KEY] = "flow"
    preview = _preview_for_shell(shell)
    item_id = store.add_item(shell, preview, origin="agent")
    _flow_write(store, item_id, _make_record(request, "intent",
                                             notes=["shell item created"]))
    _try_journal(store, item_id, "created",
                 "created via flow; source pending selection")
    return item_id


def _check_shell_title_duplicate(store: ni.NIStore, title: str) -> None:
    """M4 (audit 2026-09-13): refuse a case-insensitive title match at shell
    creation — mirrors ``tools._check_duplicate_title`` for the flow entry
    point. Duplicate-title guard error names the existing item so the model
    can point the user at the right card or pass ``allow_duplicate: true``.
    """
    assert store is not None and isinstance(title, str), "args required"
    needle = title.strip().lower()
    if not needle:
        return
    for item in store.list_items():  # bounded by ni._MAX_ITEMS
        other = str(item["spec"].get("title") or "").strip().lower()
        if other == needle:
            raise ValueError(
                f"a card named {item['spec'].get('title')!r} already exists "
                f"(id={item['id']!r}) — pass allow_duplicate: true to keep two "
                "with the same title, or ask me to update the existing card"
            )


def _preview_for_shell(shell: dict) -> dict:
    """A tiny preview payload matching the shell scene (never rendered post-handoff)."""
    assert isinstance(shell, dict), "shell required"
    return {}


def _try_journal(store: ni.NIStore, item_id: str, kind: str, summary: str) -> None:
    """Best-effort journal append — a failing write never blocks a flow transition."""
    assert store is not None and item_id, "args required"
    try:
        store.append_journal(item_id, kind, summary[:ni._MAX_JOURNAL_SUMMARY])
    except Exception as exc:  # bookkeeping must never mask the flow outcome
        log.warning("ni_flow journal append (%s) failed for %s: %s", kind, item_id, exc)


# ---- run_flow: single synchronous entry point ---------------------------

def run_flow(store: ni.NIStore, item_id: str, *,
             gateway_call: Callable[[str, str], str] | None = None,
             fetcher: Callable[[str], object] | None = None,
             catalog: list[dict] | None = None,
             ni_route_model: str | None = None,
             source_url: str | None = None) -> dict:
    """§29 flow engine — SYNCHRONOUS main entry, drives the state machine end to end.

    Reads the existing flow record for ``item_id`` and advances it to a terminal
    state (``ready`` / ``unsupported`` / ``failed``). Never raises: a failure is
    always recorded on the flow slot with a host-free error class + honest note.

    Every seam accepts an override:
    ``gateway_call(model, prompt)`` returns the model reply text; defaults to
    ``gateway.chat`` on the LIVE-resolved model (see ``_resolve_flow_model``).
    ``fetcher(url)`` returns a Python-decoded sample; defaults to
    ``netguard.safe_fetch_json``. ``catalog`` defaults to
    ``ni_catalog.entries()``. ``ni_route_model`` bypasses live resolution for
    tests; production callers leave it None.
    """
    assert store is not None and item_id, "store + id required"
    record = _flow_read(store, item_id)
    if record is None:
        raise ValueError("no flow record for item; call start_flow first")
    request = str(record.get("request") or "")
    known_url = source_url or record.get("source_url")

    def default_model(model: str, prompt: str) -> str:
        """Route through the process-wide gateway. Bounded timeout."""
        assert isinstance(model, str) and isinstance(prompt, str), "args required"
        data = _gateway_mod.chat(
            [{"role": "user", "content": prompt}], model,
            timeout=_FLOW_MODEL_TIMEOUT_S,
        )
        return _gateway_mod.completion_text(data)

    def default_fetcher(url: str) -> object:
        """Fetch a JSON sample under the netguard SSRF/redirect discipline."""
        assert isinstance(url, str) and url, "url required"
        return _netguard_mod.safe_fetch_json(url)

    call = gateway_call if gateway_call is not None else default_model
    do_fetch = fetcher if fetcher is not None else default_fetcher
    catalog_rows = catalog if catalog is not None else _load_catalog()
    resolved_model = ni_route_model if ni_route_model else _resolve_flow_model(store)
    if not resolved_model:
        return _fail(store, item_id, "intent", "no chat/ni/agent model route configured")

    def call_model(prompt: str) -> str:
        assert isinstance(prompt, str), "prompt required"
        return call(resolved_model, prompt)

    # H2 (audit 2026-09-13): a remap flow enters at ``sampling`` and re-uses the
    # item's own frozen source — never re-enter intent/source, never re-match a
    # recipe (the audit's "failed remap masked a working card" defect).
    if record.get("_remap"):
        return _run_remap(store, item_id, record, call_model, do_fetch)
    try:
        intent = _run_intent(store, item_id, request, call_model)
    except ValueError as exc:
        return _fail(store, item_id, "intent", str(exc))

    if intent.get("kind") == "computed_only":
        return _handle_computed(store, item_id, request, intent)
    return _run_external_flow(store, item_id, request, intent, known_url,
                              call_model, do_fetch, catalog_rows)


def _resolve_flow_model(store: ni.NIStore) -> str | None:
    """C1 (audit 2026-09-13): live model resolution mirroring ``ni._fetch_model``.

    Preference order:
      1. ``gateway.resolve_model("ni", routes)`` — the item-level route.
      2. Fall back to the ``chat`` capability, then ``agent``.
      3. §29 local-preferred: if the ``ni``-resolved model is CLOUD but a local
         model is configured on ``chat`` / ``agent``, prefer the local one.
         Flow model calls are frequent + cheap (two bounded turns per card);
         a local-first posture keeps the operator's data on-box when a local
         provider is present. Cloud stays fine when it's all the user has.
    """
    assert store is not None, "store required"
    routes: dict = {}
    if hasattr(_gateway_mod, "load_routes"):
        routes = _gateway_mod.load_routes(store.conn)
    ni_model = _gateway_mod.resolve_model("ni", routes)
    chat_model = _gateway_mod.resolve_model("chat", routes)
    agent_model = _gateway_mod.resolve_model("agent", routes)
    if ni_model and _gateway_mod.is_local(ni_model):
        return ni_model
    for candidate in (chat_model, agent_model):  # bounded to 2 (P10 #2)
        if candidate and _gateway_mod.is_local(candidate):
            return candidate
    return ni_model or chat_model or agent_model


def _load_catalog() -> list[dict]:
    """Late-import the catalog to keep the module's import graph shallow."""
    from . import ni_catalog
    return ni_catalog.entries()


def continue_from_recipe_confirm(store: ni.NIStore, item_id: str,
                                  confirmed_url: str) -> dict:
    """C3 (audit 2026-09-13): resume a ``confirm_source`` flow after the operator
    approved the recipe's url_template via ``confirm_ni_flow_source``.

    Reads the flow record's ``_recipe_id`` + ``source_url``; refuses when the
    confirmed URL does NOT equal the recipe's url_template (a mismatched URL
    is a code defect, not a user error — the frontend renders the recipe URL
    verbatim on the approval card). Then runs ``_handoff_from_recipe`` using
    the sealed intent, so the flow completes without ever asking the user a
    second question about the source they already saw.

    Never raises past this boundary: a bad state or missing recipe returns a
    ``failed`` record, keeping parity with every other flow terminal.
    """
    assert store is not None and item_id and isinstance(confirmed_url, str), "args required"
    record = _flow_read(store, item_id)
    if record is None:
        raise ValueError("no active flow on this item")
    if str(record.get("state") or "") != "confirm_source":
        raise ValueError(
            "flow is not awaiting a source confirmation (state != confirm_source)")
    expected = str(record.get("source_url") or "")
    if not expected or expected != confirmed_url:
        raise ValueError(
            "confirmed URL does not match the pending recipe URL — refuse rather "
            "than seal a source the operator never saw")
    recipe_id = str(record.get("_recipe_id") or "")
    if not recipe_id:
        return _fail(store, item_id, "confirm",
                     "flow record missing recipe id; retry via start_ni_flow")
    from . import ni_catalog
    recipe = ni_catalog.get_recipe(recipe_id)
    if recipe is None:
        return _fail(store, item_id, "confirm",
                     f"catalog no longer serves recipe {recipe_id!r}")
    intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
    request = str(record.get("request") or "")
    return _handoff_from_recipe(store, item_id, request, intent, recipe)


def _run_intent(store: ni.NIStore, item_id: str, request: str,
                call_model: Callable[[str], str]) -> dict:
    """Wrap stage_intent with flow-slot transitions on entry + success."""
    assert store is not None and callable(call_model), "args required"
    _transition(store, item_id, "intent", request=request)
    intent = stage_intent(request, call_model)
    _transition(store, item_id, "source", intent=intent, request=request)
    return intent


def _handle_computed(store: ni.NIStore, item_id: str, request: str,
                     intent: dict) -> dict:
    """§29 computed-only branch: build the ``computed`` spec if the source landed.

    When the parallel agent's ``computed`` source is present in
    ``ni._SOURCE_TYPES`` the flow constructs a ``days_until`` spec against a
    date parsed from the request (YYYY-MM-DD literal); otherwise it terminates
    ``unsupported(computed_only)`` honestly.

    Minor (audit 2026-09-13): the computed preview computes the REAL day count
    at finalize (the shipped preview shape must not lie by rendering ``0`` on a
    real future date).
    """
    assert store is not None and isinstance(intent, dict), "args required"
    if not _has_source_type("computed"):
        return _terminate_unsupported(store, item_id,
                                       "computed-only requests need the computed source (not enabled)")
    date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", request)
    if not date_match:
        return _terminate_unsupported(store, item_id,
                                       "computed-only requires an explicit YYYY-MM-DD date in the request")
    date_str = date_match.group(1)
    source = {"type": "computed", "compute": "days_until", "date": date_str}
    pipeline: list[dict] = []
    scene = value_scene(["days"])
    cadence_raw = intent.get("cadence_minutes")
    cadence = int(cadence_raw) if isinstance(cadence_raw, int) else _DEFAULT_CADENCE
    spec = build_final_spec(request, intent, source, cadence, pipeline, scene)
    preview = {"days": _days_until(date_str)}
    return _finalize(store, item_id, spec, preview,
                     note="computed source used", born="flow")


def _days_until(date_str: str) -> int:
    """Return the days between today (UTC) and ``date_str`` (YYYY-MM-DD).

    Bounded to a non-negative int — a past date reads as 0 rather than a
    negative preview number that would fail a scene bind against ``number``.
    Malformed input falls back to 0 (validator already checked the shape at
    the caller; this is defence-in-depth).
    """
    assert isinstance(date_str, str), "date_str required"
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return 0
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, (target - today).days)


def _run_external_flow(store: ni.NIStore, item_id: str, request: str,
                       intent: dict, known_url: str | None,
                       call_model: Callable[[str], str],
                       do_fetch: Callable[[str], object],
                       catalog_rows: list[dict]) -> dict:
    """External-data branch: recipe match → sampling → mapping → assembly → handoff.

    C2/C3 ordering (audit 2026-09-13):
      1. If ``known_url`` is present (start_ni_flow / resume_ni_flow with an
         explicit user URL), the user has ALREADY consented — recipe matching
         is SKIPPED, we sample directly against that URL, and an invariant
         assertion after finalize confirms the frozen spec.source.url equals
         the fetched URL (never a silent recipe-URL swap).
      2. Otherwise, try to match a catalog recipe — a hit pauses in the new
         ``confirm_source`` state carrying the recipe's url_template + title
         (`confirm_ni_flow_source` resumes with the operator's approval).
      3. Neither known_url nor a recipe hit ⇒ pause in ``source`` state with
         ``AWAITING_SOURCE_PICK`` so the chat can present candidates.
    """
    assert isinstance(intent, dict), "intent required"
    if known_url:
        return _sample_and_map(store, item_id, request, intent, known_url,
                                call_model, do_fetch)
    recipe = match_recipe(catalog_rows, request, intent)
    if recipe is not None:
        return _pause_for_recipe_confirm(store, item_id, intent, recipe)
    _transition(store, item_id, "source",
                error=AWAITING_SOURCE_PICK,
                note="paused: awaiting a source URL (resume_ni_flow with source_url)")
    return _flow_read(store, item_id) or {}


def _pause_for_recipe_confirm(store: ni.NIStore, item_id: str, intent: dict,
                               recipe: dict) -> dict:
    """C3 (audit 2026-09-13): pause a recipe-matched flow at ``confirm_source``.

    The recipe's url_template + title are stamped on the flow record so the
    chat model can name the exact host it's proposing to fetch and the
    frontend renders the ``AWAITING_SOURCE_CONFIRM`` marker. The recipe id
    stays sealed too so ``confirm_ni_flow_source`` can look the recipe up
    without re-running the scorer against a potentially mutated request.
    """
    assert isinstance(intent, dict) and isinstance(recipe, dict), "args required"
    template = recipe.get("spec_template") or {}
    src = template.get("source") if isinstance(template, dict) else {}
    url = str((src or {}).get("url") or recipe.get("url_template") or "")
    title = str(recipe.get("title") or "")
    recipe_id = str(recipe.get("id") or "")
    _transition(store, item_id, "confirm_source",
                source_url=url,
                error=AWAITING_SOURCE_CONFIRM,
                intent=intent,
                note=(f"awaiting confirmation of {title!r} "
                      f"(confirm_ni_flow_source with source_url)"))
    record = _flow_read(store, item_id) or {}
    # Extra sealed fields — the base record shape stays closed, so widen via a
    # second write that includes ``_recipe_id`` alongside the standard record.
    record["_recipe_id"] = recipe_id[:80]
    record["_recipe_title"] = title[:_MAX_NOTE]
    _flow_write(store, item_id, record)
    return _flow_read(store, item_id) or {}


def _run_remap(store: ni.NIStore, item_id: str, record: dict,
               call_model: Callable[[str], str],
               do_fetch: Callable[[str], object]) -> dict:
    """H2 (audit 2026-09-13): the remap path — sampling only, on the item's
    OWN frozen source. Never re-enters intent OR recipe matching (the audit's
    "failed remap masked a working card" defect).

    Intent is derived from the stored spec (title → subject, interval → cadence,
    pipeline's extract paths → wants). The fetch runs against ``record.source_url``
    verbatim; a failure here writes ``failed(remap_fetch)`` and returns — the
    item's existing payload keeps rendering because ``board_flow_field`` hides
    terminal flow records once a renderable payload is present.
    """
    assert isinstance(record, dict), "record required"
    url = str(record.get("source_url") or "")
    if not url:
        return _fail(store, item_id, "remap", "item has no source URL to remap")
    item = store.get_item(item_id)
    if item is None:
        return _fail(store, item_id, "remap", "item not found for remap")
    intent = _remap_intent_from_spec(item["spec"], record.get("request") or "")
    request = str(record.get("request") or item["spec"].get("goal") or "remap")
    return _sample_and_map(store, item_id, request, intent, url,
                            call_model, do_fetch, remap=True)


def _remap_intent_from_spec(spec: dict, request: str) -> dict:
    """Reconstruct a minimal intent dict from an already-sealed spec (remap path).

    The intent shape is what stage_mapping + assembly consume; every field is
    read from the stored spec so the remap runs against the SAME semantics as
    the original create (§29 door closure — no re-imagined intents).
    """
    assert isinstance(spec, dict), "spec must be a dict"
    subject = str(spec.get("title") or request)[:200]
    cadence_raw = spec.get("interval_minutes")
    cadence = int(cadence_raw) if isinstance(cadence_raw, int) else _DEFAULT_CADENCE
    wants: list[str] = []
    for stage in (spec.get("pipeline") or []):  # bounded by ni._MAX_PIPELINE_STAGES
        if isinstance(stage, dict) and stage.get("op") == "extract":
            paths = stage.get("paths") or {}
            if isinstance(paths, dict):
                wants.extend([str(k) for k in list(paths.keys())[:_MAX_INTENT_FIELDS]])
                break
    if not wants:
        wants = ["value"]
    scene = spec.get("scene") or {}
    display_hint = _DISPLAY_LIST if _scene_has_repeat(scene) else _DISPLAY_VALUE
    return {"kind": "external_data", "subject": subject,
            "cadence_minutes": max(_MIN_CADENCE, min(cadence, _MAX_CADENCE)),
            "wants": wants[:_MAX_INTENT_FIELDS], "threshold": None,
            "display_hint": display_hint}


def _scene_has_repeat(scene: object) -> bool:
    """True when ``scene`` (or any bounded descendant) is a repeat node."""
    assert scene is not None, "scene required"
    stack: list[object] = [scene]
    for _ in range(200):  # fixed upper bound (P10 #2)
        if not stack:
            return False
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get("type") == "repeat":
                return True
            children = cur.get("children")
            if isinstance(children, list):
                stack.extend(children)
    return False


def _handoff_from_recipe(store: ni.NIStore, item_id: str, request: str,
                          intent: dict, recipe: dict) -> dict:
    """Deep-copy the recipe's spec_template and hand off.

    C3 (audit 2026-09-13): callable ONLY from ``confirm_ni_flow_source`` after
    the operator confirms the recipe's url_template — the promoted "no fetch
    until one is confirmed" line becomes literally true. The seal stamps
    ``_born: "recipe"`` per M1.
    """
    assert isinstance(recipe, dict), "recipe required"
    spec_template = recipe.get("spec_template")
    if not isinstance(spec_template, dict):
        return _fail(store, item_id, "assembly", "recipe spec_template missing")
    spec = json.loads(json.dumps(spec_template))
    spec["title"] = str(intent.get("subject") or spec.get("title") or "New card")[:ni._MAX_TITLE]
    spec["goal"] = request[:ni._MAX_GOAL]
    cadence_raw = intent.get("cadence_minutes")
    cadence = int(cadence_raw) if isinstance(cadence_raw, int) else _DEFAULT_CADENCE
    spec["interval_minutes"] = max(_MIN_CADENCE, cadence)
    spec["repair_policy"] = {"l1": True, "l2_frontier": False}
    preview = recipe.get("preview_payload") or {}
    if not isinstance(preview, dict):
        return _fail(store, item_id, "assembly", "recipe preview_payload malformed")
    _transition(store, item_id, "assembling",
                note=f"matched recipe {recipe.get('id')!r}", intent=intent)
    result = _finalize(store, item_id, spec, preview,
                       note=f"handoff from recipe {recipe.get('id')!r}",
                       born="recipe")
    _try_journal(store, item_id, "recipe",
                 f"flow matched recipe {recipe.get('id')!r}")
    return result


def _sample_and_map(store: ni.NIStore, item_id: str, request: str,
                    intent: dict, url: str,
                    call_model: Callable[[str], str],
                    do_fetch: Callable[[str], object],
                    *, remap: bool = False) -> dict:
    """Freeform branch: one consented fetch → derive → mapping → assemble.

    ``remap`` (H2 audit 2026-09-13) signals the caller is re-entering at
    Sampling on the item's own consented URL — the finalize path stamps the
    ``_born`` marker only when the item was NOT flow-born originally.
    """
    assert isinstance(url, str) and url, "url required"
    _transition(store, item_id, "sampling", source_url=url,
                note=f"fetching consented source ({_host_hint(url)})")
    try:
        sample = do_fetch(url)
    except Exception as exc:
        return _fail(store, item_id, "fetch", f"sample fetch failed: {type(exc).__name__}")
    try:
        cands = derive_paths(sample)
    except Exception as exc:  # walker errors carry a ValueError message
        return _fail(store, item_id, "derive", f"derive failed: {exc}")
    if not cands:
        return _fail(store, item_id, "derive", "no candidate paths in sample")
    fields = reconcile_field_types(infer_fields(intent), cands)
    _transition(store, item_id, "mapping", source_url=url,
                note=f"{len(cands)} candidates; mapping to {sorted(fields)}")
    try:
        mapping = stage_mapping(intent, cands, fields, call_model)
    except ValueError as exc:
        return _fail(store, item_id, "mapping", str(exc))
    klass = _pick_display_class(intent)
    hint = str(intent.get("display_hint") or "").lower()
    degrade_note = None
    if hint in ("map", "image") and klass == _DISPLAY_VALUE:
        degrade_note = f"display_hint {hint!r} unsupported; proceeding with value card"
    # Minor (audit 2026-09-13): a list-class scene binds ONE list exemplar
    # path (the repeat root) — extra fields the model picked are dropped.
    # Say so honestly on the flow record so the user knows only the first
    # field rides the tile.
    if klass == _DISPLAY_LIST and len(fields) > 1:
        dropped = list(fields.keys())[1:]
        extra = f"list-class scene keeps the first field; dropped: {dropped}"
        degrade_note = f"{degrade_note}; {extra}" if degrade_note else extra
    _transition(store, item_id, "assembling",
                note=degrade_note or "assembling scene + pipeline")
    try:
        built = assemble_from_mapping(mapping, fields, klass, sample)
    except ValueError as exc:
        return _fail(store, item_id, "assembly", str(exc))
    source = {"type": "http_json", "url": url}
    spec = build_final_spec(request, intent, source, intent["cadence_minutes"],
                            built["pipeline"], built["scene"])
    # C2 (audit 2026-09-13): the frozen source URL MUST equal the URL we
    # actually fetched — a mismatch is a code defect (someone rewrote the URL
    # between fetch and seal), not a user-facing failure.
    assert spec["source"]["url"] == url, "frozen source.url must match fetched url"
    born = "flow" if not remap else None
    return _finalize(store, item_id, spec, built["preview_payload"],
                     note=degrade_note or "handoff from freeform mapping",
                     born=born)


def _finalize(store: ni.NIStore, item_id: str, spec: dict, preview: dict,
              *, note: str, born: str | None = None) -> dict:
    """Rewrite the shell item's spec in place; write preview + preview_data; commission.

    Landing rule mirrors ``_initial_ni_state`` from tools.py: a spec declaring
    any secret-kind param stays ``draft`` (the flow has no SecretStore); every
    other spec is commissioned via ``NIStore.commission`` (draft → commissioning).
    Journal entry names the honest origin (``recipe`` or ``updated`` for a remap).

    ``born`` (M1 audit 2026-09-13): stamp the sealed ``_born`` marker so
    ``is_flow_or_recipe_born`` reads a spec-shape truth instead of the prunable
    journal (a 25-entry churn used to defeat the §29 door). None on remap
    (the item's existing ``_born`` value stays intact).
    """
    assert store is not None and item_id, "args required"
    assert born is None or born in BORN_MARKERS, "born marker must be closed"
    if born is not None:
        spec[_BORN_KEY] = born
    try:
        ni.validate_spec(spec)
    except ValueError as exc:
        return _fail(store, item_id, "assembly", f"final spec invalid: {exc}")
    try:
        bound = ni.bind_scene(spec["scene"], preview,
                              history=ni._seed_history(spec),
                              image_ref=ni._preview_image_ref(spec, item_id))
    except (ni.NIError, ValueError) as exc:
        return _fail(store, item_id, "assembly", f"preview bind failed: {exc}")
    assert isinstance(bound, dict), "bind_scene must return a dict"
    try:
        store.update_spec(item_id, spec, origin="agent")
        store.write_snapshot(item_id, "preview", bound, ok=True)
        store.write_snapshot(item_id, "preview_data", preview, ok=True)
    except (ValueError, ni.NIError) as exc:
        return _fail(store, item_id, "assembly", f"store update failed: {exc}")
    landing = _landing_state(spec)
    if landing == "commissioning":
        try:
            store.commission(item_id)
        except ValueError as exc:
            # commission refuses non-draft states; the shell landed draft so this
            # should be unreachable — record and continue if the store disagrees.
            _append_note(store, item_id, f"commission skipped: {exc}")
    else:
        _append_note(store, item_id, "awaiting_credential: secret param unfilled")
        _transition(store, item_id, "awaiting_credential", note=note)
        return _flow_read(store, item_id) or {}
    _transition(store, item_id, "ready", note=note)
    return _flow_read(store, item_id) or {}


def _landing_state(spec: dict) -> str:
    """Mirror of ``tools._initial_ni_state``: secret param present ⇒ draft."""
    assert isinstance(spec, dict), "spec required"
    params = spec.get("params") or {}
    for decl in params.values():  # bounded by ni._MAX_PARAMS
        if isinstance(decl, dict) and decl.get("kind") == "secret":
            return "draft"
    return "commissioning"


def _fail(store: ni.NIStore, item_id: str, klass: str, detail: str) -> dict:
    """Terminal ``failed(class)`` transition — deterministic, host-free class + detail."""
    assert store is not None and item_id, "args required"
    _transition(store, item_id, "failed", error=f"{klass}: {detail}"[:_MAX_ERROR],
                note=f"failed at {klass}")
    return _flow_read(store, item_id) or {}


def _terminate_unsupported(store: ni.NIStore, item_id: str, reason: str) -> dict:
    """Terminal ``unsupported(reason)`` — the request is honest about what can't be served."""
    assert store is not None and item_id and isinstance(reason, str), "args required"
    _transition(store, item_id, "unsupported", error=reason[:_MAX_ERROR],
                note=f"unsupported: {reason}")
    return _flow_read(store, item_id) or {}


def _host_hint(url: str) -> str:
    """Return the URL host for a flow-slot note (never a full URL — the note is tiny)."""
    assert isinstance(url, str), "url required"
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return str(parsed.hostname or "").lower()[:100]
    except ValueError:
        return ""


# ---- background thread wrapper (single-flight per item) -----------------

def _claim(item_id: str) -> bool:
    """Try to reserve a worker slot for ``item_id``; return False when refused."""
    assert isinstance(item_id, str) and item_id, "id required"
    with _INFLIGHT_LOCK:
        if item_id in _INFLIGHT:
            return False
        if len(_INFLIGHT) >= _MAX_CONCURRENT:
            return False
        _INFLIGHT.add(item_id)
        return True


def _release(item_id: str) -> None:
    """Release the worker slot for ``item_id`` (idempotent)."""
    assert isinstance(item_id, str) and item_id, "id required"
    with _INFLIGHT_LOCK:
        _INFLIGHT.discard(item_id)


def start_flow_worker(store: ni.NIStore, item_id: str, *,
                       gateway_call: Callable[[str, str], str] | None = None,
                       fetcher: Callable[[str], object] | None = None,
                       ni_route_model: str | None = None,
                       source_url: str | None = None) -> bool:
    """Spawn a daemon worker that runs ``run_flow`` for ``item_id``.

    Single-flight per item id, capped at _MAX_CONCURRENT process-wide (the
    L2-worker precedent). Any exception inside the worker is caught and
    recorded on the flow slot as ``failed(worker)``.

    M3 (audit 2026-09-13): a refusal (single-flight collision or cap reached)
    is now VISIBLE — the caller sees ``False`` AND the flow record is
    transitioned to ``failed(busy)`` so the tile stops saying "Preparing…"
    while nothing is running. This closes the "started:false + silent shell"
    stranded-flow class the audit named.
    """
    assert store is not None and item_id, "args required"
    if not _claim(item_id):
        _fail(store, item_id, "busy",
              "another flow worker is already handling this item; retry later")
        return False

    def _run() -> None:
        try:
            run_flow(store, item_id, gateway_call=gateway_call, fetcher=fetcher,
                     ni_route_model=ni_route_model, source_url=source_url)
        except Exception as exc:  # last-resort net; run_flow's own paths seal failures
            log.warning("ni_flow worker for %s crashed: %s", item_id, exc)
            try:
                _fail(store, item_id, "worker", f"crash: {type(exc).__name__}")
            except Exception as inner:  # store may be locked; nothing else to do
                log.warning("ni_flow crash-seal failed for %s: %s", item_id, inner)
        finally:
            _release(item_id)

    try:
        threading.Thread(target=_run, name=f"ni-flow-{item_id[:8]}",
                         daemon=True).start()
    except Exception as exc:  # OS refused; release + fail so nothing is stranded
        _release(item_id)
        log.warning("ni_flow worker spawn refused for %s: %s", item_id, exc)
        _fail(store, item_id, "worker",
              f"OS refused to start the flow worker ({type(exc).__name__}); retry later")
        return False
    return True


def board_flow_field(store: ni.NIStore, item_id: str) -> dict | None:
    """Return the ``{state, error?}`` compact flow view for the board row.

    ``None`` when no flow slot exists OR the flow reached ``ready``. In-progress
    states (intent/source/confirm_source/sampling/mapping/assembling/
    awaiting_credential) always ride.

    H2 (audit 2026-09-13): a TERMINAL flow record (``failed`` / ``unsupported``)
    is HIDDEN when the item has a renderable payload — a failed remap must not
    mask the working card. The board row's own last_status still names the
    failure; the terminal flow slot is cleared on the next successful anything
    (remap/confirm/resume overwrite it, and the delete-item cascade drops the
    ``flow`` slot alongside every other snapshot).
    """
    assert store is not None and item_id, "args required"
    record = _flow_read(store, item_id)
    if record is None:
        return None
    state = str(record.get("state") or "")
    if state == "ready":
        return None
    if state in _TERMINAL_STATES and _item_has_renderable_payload(store, item_id):
        # Terminal flow record hiding: the failure is honestly reported on
        # last_status; the tile keeps rendering its existing payload. Drop the
        # slot so a later poll doesn't recompute this branch every second.
        try:
            store.delete_snapshot(item_id, "flow")
        except Exception as exc:  # bookkeeping only
            log.warning("ni_flow: terminal-slot clear failed for %s: %s",
                        item_id, exc)
        return None
    out: dict = {"state": state}
    error = record.get("error")
    if isinstance(error, str) and error:
        out["error"] = error[:_MAX_ERROR]
    return out


def _item_has_renderable_payload(store: ni.NIStore, item_id: str) -> bool:
    """H2 (audit 2026-09-13): True when the tile can render something WITHOUT
    the flow slot. Preview (draft), latest (ok), or last_good — same fallback
    ladder ``ni_routes._pick_board_snapshot`` uses.
    """
    assert store is not None and item_id, "args required"
    for slot in ("latest", "last_good", "preview"):  # bounded to 3 (P10 #2)
        snap = store.read_snapshot(item_id, slot)
        if snap is not None:
            return True
    return False


def clear_flow_slot(store: ni.NIStore, item_id: str) -> None:
    """H1 (audit 2026-09-13): drop the ``flow`` snapshot slot for ``item_id``.

    Callers: the credential PUT + the commission route, once the flow-authored
    item has satisfied the ``awaiting_credential`` gate. Idempotent; a missing
    slot is a no-op.
    """
    assert store is not None and item_id, "args required"
    try:
        store.delete_snapshot(item_id, "flow")
    except Exception as exc:  # bookkeeping only
        log.warning("ni_flow: clear_flow_slot failed for %s: %s", item_id, exc)


def sweep_stranded_flows(store: ni.NIStore) -> int:
    """M3 (audit 2026-09-13): fail every non-terminal flow older than 1 hour.

    Called from the scheduler tick (budgeted, once per tick) — a worker that
    crashed between ticks leaves a shell in ``intent`` / ``sampling`` forever;
    the sweep reclassifies those as ``failed(stale)`` so the card stops saying
    "Preparing card…" indefinitely. Returns the number of records swept so the
    scheduler can log it. Bounded by ``ni._MAX_ITEMS``.
    """
    assert store is not None, "store required"
    swept = 0
    now = datetime.now(UTC)
    for item in store.list_items():  # bounded by ni._MAX_ITEMS
        record = _flow_read(store, item["id"])
        if record is None:
            continue
        state = str(record.get("state") or "")
        if state in _TERMINAL_STATES or state == "ready":
            continue
        if state == "awaiting_credential":
            continue  # user-gated; not stranded even after an hour
        updated_at = record.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            continue
        try:
            stamp = datetime.fromisoformat(updated_at)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if now - stamp <= timedelta(hours=_STRANDED_HOURS):
            continue
        _fail(store, item["id"], "stale",
              f"flow record older than {_STRANDED_HOURS}h at unlock/sweep")
        swept += 1
    return swept
