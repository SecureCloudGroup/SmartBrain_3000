"""Neural Interface (NI) items — deterministic mini-apps rendered from a closed grammar.

An NI item is a template instantiation: params declare the slots (some are secret names),
a source declares WHERE the data comes from (guarded HTTP JSON, a model call on the ``ni``
route, or the newest ``schedule_runs`` row), a pipeline extracts + transforms the fetched
payload into named outputs, and a scene binds those outputs into a strictly typed tree of
nodes. Adding an item is an explicit act in the UI (approving the create card is the
egress consent, mirrored on the feed model); background refreshes fetch that host
without per-fetch approval and the agent gains no new tool.

Design laws (from docs/internal/ni-format.md, decided 2026-09-08):
- Deterministic core, AI at the edges. Models run at design/commissioning/repair time
  only; the runtime path — fetch, extract, transform, contract-check, bind — is pure
  Python. The same payload always produces the same scene.
- Authorization is not trust. The user's choice of a source authorizes the connection;
  the fetched bytes stay untrusted forever. Every model output that was derived from
  fetched bytes lands as a closed-schema, egress-inert artifact (scene trees, extract
  paths, transform ops — never URLs, headers, schedules, or free-form markup).
- Nothing renders that did not validate. The scene validator and the payload binder run
  server-side on every snapshot write; malformed data can never reach the page.

Design bounds, stated where they bind below: bounded item + snapshot + revision + run
counts; capped scene depth / node count / text length / repeat expansion; closed source
types, transform functions, node types, tones, roles, sizes; secrets are host-bound at
storage and never substituted outside header $secret refs; per-item errors isolated with
a host-free status string (feeds law).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote as _url_quote
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("smartbrain.ni")

# Cheap race mitigation for the sealed-spec read-modify-write sites (update_spec,
# record_validation, and the C1 contract re-seal in _transition_on_success). A parallel
# tick + a route write could otherwise lose either write's fields when both decrypt the
# same starting spec. This is a module-level Lock (not a per-instance one) because a route
# thread and a tick thread hold DIFFERENT NIStore instances (different DuckDB cursors) —
# a per-instance lock would not serialize them. Held only across the encrypt+UPDATE (never
# across a fetch, model call, or the whole run pipeline), so contention stays negligible.
_SPEC_LOCK = threading.Lock()

_NONCE_BYTES = 12
_MAX_ITEMS = 200                 # bound on ni_items rows (per-key decrypt scan)
_MAX_ITEMS_PER_PASS = 3          # engine tick fires at most this many due items (feeds precedent)
_MAX_REVISIONS = 10              # per-item revision history kept in ni_revisions
_MAX_RUNS = 50                   # per-item ni_runs telemetry retained (pruned in code)
_MAX_TITLE = 300
_MAX_GOAL = 5000
_MAX_URL = 2000
_MAX_INSTRUCTION = 5000          # model-source instruction upper bound
_MAX_STATUS = 200                # host-free plaintext ni_items.last_status column
_MAX_PARAM_NAME = 60
_MAX_PARAM_VALUE = 2000
_MAX_PARAMS = 20
_MAX_HEADERS = 20
_MAX_HEADER_NAME = 100
_MAX_HEADER_VALUE = 2000
_MAX_PIPELINE_STAGES = 10
_MAX_EXTRACT_PATHS = 40          # per extract op
_MAX_TRANSFORM_APPLY = 40        # per transform op
_MAX_TOP_N = 50
_MAX_SCENE_DEPTH = 8
_MAX_SCENE_NODES = 100
_MAX_SCENE_NODES_PRE_EXPAND = 100
_MAX_TEXT_CHARS = 2000
_MAX_REPEAT_MAX = 50
_INTERVAL_FLOOR = 1
_INTERVAL_CEILING_MINUTES = 24 * 60          # effective-interval cap on failing items
_FAILING_THRESHOLD = 3                       # >= this many consecutive fails -> failing
_BROKEN_FAILURE_COUNT = 8                    # 8 failures in >= 7 days -> broken
_BROKEN_MIN_DAYS = 7                         # vault_sync escalation rule
_MAX_PAYLOAD_BYTES = 256 * 1024              # bound on the JSON-serialized bound scene (bind → snapshot)
# Reserved output names: repeat.item.<...> binds against the current list element, so an
# extract named "item" would collide with the repeat root at bind time. "history" is the
# read-only namespace exposed by §11 (history.<series-name>), so a pipeline output named
# "history" would shadow the history bind root. Both refused at spec time.
_RESERVED_OUTPUT_NAMES: frozenset[str] = frozenset({"item", "history"})
# Icon names: literal-only, lowercase kebab shape (Lucide-subset convention). No {{}} or $bind.
_ICON_NAME_RE = re.compile(r"^[a-z0-9-]{1,60}$")
# Auth-shaped literal header names are refused (see _validate_http_json_source): a literal
# value in one of these carries plaintext auth in the sealed spec; the only allowed form is
# {"$secret": "..."} so the credential lives in the SecretStore (host-bound at storage).
_AUTH_HEADER_LITERAL_NAMES: frozenset[str] = frozenset({
    "authorization", "proxy-authorization", "cookie", "x-api-key", "api-key",
})
_AUTH_HEADER_TOKEN_SUBSTRINGS: tuple[str, ...] = ("token", "secret", "key")

# Closed vocabularies — v1 refuses anything else, so old clients refuse new nodes rather
# than mis-render them (the "reject reserved types" contract in ni-format §5).
_SOURCE_TYPES: frozenset[str] = frozenset({"http_json", "model", "internal.schedule"})
_PARAM_KINDS: frozenset[str] = frozenset({"string", "number", "secret"})
_DISPLAY_SIZES: frozenset[str] = frozenset({"small", "wide"})
_STATES: frozenset[str] = frozenset(
    {"draft", "commissioning", "live", "degraded", "failing", "broken", "paused"}
)
_SLOTS: frozenset[str] = frozenset(
    {"latest", "last_good", "preview", "history", "alert_state"}
)
_REVISION_ORIGINS: frozenset[str] = frozenset(
    {"user", "agent", "repair_l1", "repair_l2", "template"}
)
_TRANSFORM_FNS: frozenset[str] = frozenset(
    {"round", "scale", "rename", "pick", "sort_by", "top_n",
     "sum", "avg", "min", "max", "count", "delta_prev"}
)
# v2 aggregate fns that fail with "empty_aggregate" on an empty input list (count does not).
_AGGREGATE_FNS: frozenset[str] = frozenset({"sum", "avg", "min", "max"})
_SORT_DIRS: frozenset[str] = frozenset({"asc", "desc"})
_SCENE_TYPES: frozenset[str] = frozenset(
    {"stack", "grid", "divider", "text", "number", "chip", "bar", "icon", "repeat",
     "spark", "gauge"}
)
# Reserved for later phases — validators MUST reject in v1 so old apps refuse new scenes.
# v2 promoted spark + gauge (§5 Added in v2) out of the reserved set into _SCENE_TYPES.
_RESERVED_SCENE_TYPES: frozenset[str] = frozenset({"image", "on_tap"})
_STACK_DIRS: frozenset[str] = frozenset({"v", "h"})
_STACK_GAPS: frozenset[str] = frozenset({"sm", "md"})
_TEXT_ROLES: frozenset[str] = frozenset({"title", "label", "value", "caption"})
_TEXT_TONES: frozenset[str] = frozenset(
    {"default", "muted", "accent", "ok", "warn", "danger"}
)
_TEXT_SIZES: frozenset[str] = frozenset({"sm", "md", "lg"})
_NUM_FORMATS: frozenset[str] = frozenset({"plain", "compact", "percent", "currency"})
_CHIP_KINDS: frozenset[str] = frozenset({"", "accent", "ok", "warn", "danger"})
_SPARK_KINDS: frozenset[str] = frozenset({"line", "bars"})

# v2 caps + enums (§5 spark, §5 Conditions, §11 History, §12 Alerts).
_MAX_SPARK_POINTS = 500          # bound on bound spark.points (list or history series)
_MAX_HISTORY_SERIES = 4          # spec-level history.track series count (§11)
_MAX_HISTORY_POINTS = 500        # per-series point ceiling; max_points is clamped to this
_DEFAULT_HISTORY_POINTS = 100    # per-series default when max_points is unset
_MAX_WHEN_RULES = 5              # per §5 Conditions cap
_WHEN_OPS: frozenset[str] = frozenset({"lt", "le", "gt", "ge", "eq", "ne"})
_ORDER_OPS: frozenset[str] = frozenset({"lt", "le", "gt", "ge"})
_MAX_ALERTS = 5                  # per-item alerts cap (§12)
_MAX_ALERT_NAME = 40             # slug length ceiling
_ALERT_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")  # slug charset per §12
_MAX_ALERT_MESSAGE = 500         # bound message length (post-interpolation)
_MIN_ALERT_COOLDOWN = 5          # clamp floor (§12 "≥ 5")
_DEFAULT_ALERT_COOLDOWN = 60     # default cooldown when the spec omits it
_MAX_GAUGE_LABEL = 200           # M3 client parity (web/src/lib/ni/scene.ts MAX_LABEL_CHARS)
# Heading-forgery guard (H1, audit 2026-09-09): an alert message renders inside the
# chat notice's own markdown ``### Scheduled Item ...`` heading, so any fetched string
# carrying a newline plus a spoofed ``###`` line could forge a fake notice boundary
# with a clickable phishing link. Modeled on claudecli.py's ``_HEADING_FORGERY`` /
# ``_neutralize`` precedent (transcript-forgery guard from the 2026-09 audit).
_ALERT_NEWLINE_RUN = re.compile(r"[\r\n]+")

# Path grammar (§4.1) — one regex per production. `__proto__` is denied by name even
# though it matches ``_KEY_RE`` (JS-prototype-pollution style names are never a data path
# in a payload we shipped ourselves).
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_INDEX_RE = re.compile(r"^-?\d+$")
_SLICE_RE = re.compile(r"^\d*:\d*$")
_DENIED_PATH_KEYS: frozenset[str] = frozenset({"__proto__", "constructor", "prototype"})
_PARAM_PLACEHOLDER = re.compile(r"\{\{param:([A-Za-z_][A-Za-z0-9_-]*)\}\}")
_BIND_INTERP = re.compile(r"\{\{([^{}]+)\}\}")


class NIError(Exception):
    """A short host-free class string for the failure — never the URL, host, or payload
    bytes. ``kind`` is the operational class stored in ni_runs.error and shown on health.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        assert kind, "kind required"
        assert isinstance(detail, str), "detail must be a string"
        super().__init__(kind if not detail else f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


# --- validators (pure) -----------------------------------------------------

def parse_path(path: str) -> list[tuple]:
    """Parse a §4.1 path string into a list of resolution steps.

    Each step is ``("key", name)``, ``("index", int)``, or ``("slice", start|None, stop|None)``.
    Anything outside the grammar (empty, spaces, denied keys like ``__proto__``, missing
    brackets, malformed slice) raises ValueError. Shared by extract paths and $bind /
    ``{{path}}`` interpolations — one grammar, one implementation.
    """
    assert isinstance(path, str), "path must be a string"
    if not path:
        raise ValueError("path may not be empty")
    if any(ch.isspace() for ch in path):
        raise ValueError("path may not contain whitespace")
    out: list[tuple] = []
    segments = path.split(".")
    assert len(segments) < 100, "path segment count unreasonable"
    for raw in segments:
        if not raw:
            raise ValueError("empty path segment")
        key, subs = _split_segment(raw)
        if key in _DENIED_PATH_KEYS:
            raise ValueError(f"denied path key: {key}")
        if not _KEY_RE.match(key):
            raise ValueError(f"bad path key: {key!r}")
        out.append(("key", key))
        out.extend(subs)
    assert out, "parsed path must be non-empty"
    return out


def _split_segment(raw: str) -> tuple[str, list[tuple]]:
    """Split ``key[a][b:c]`` into (key, [subscript ops])."""
    assert raw, "segment required"
    if "[" not in raw:
        return raw, []
    key, _, rest = raw.partition("[")
    subs: list[tuple] = []
    remaining = "[" + rest
    for _ in range(8):  # bounded: subscripts per segment
        if not remaining:
            break
        if not remaining.startswith("["):
            raise ValueError(f"bad subscript in {raw!r}")
        close = remaining.find("]")
        if close < 0:
            raise ValueError(f"unclosed subscript in {raw!r}")
        inner = remaining[1:close]
        subs.append(_parse_subscript(inner, raw))
        remaining = remaining[close + 1:]
    if remaining:
        raise ValueError(f"trailing characters after subscript in {raw!r}")
    assert subs, "at least one subscript when [ present"
    return key, subs


def _parse_subscript(inner: str, raw: str) -> tuple:
    """One ``[..]`` body: an index, a slice, or malformed."""
    assert isinstance(inner, str), "inner must be a string"
    assert raw, "raw segment required"
    if _INDEX_RE.match(inner):
        return ("index", int(inner))
    if _SLICE_RE.match(inner):
        left, _, right = inner.partition(":")
        start = int(left) if left else None
        stop = int(right) if right else None
        return ("slice", start, stop)
    raise ValueError(f"bad subscript {inner!r} in {raw!r}")


def _require_dict(value: object, what: str) -> dict:
    """Every closed-schema check starts here — ``value`` must be a dict."""
    assert what, "what required"
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object")  # noqa: TRY004 — one exception class per validator (task contract)
    return value


def _require_str(value: object, what: str, *, max_len: int) -> str:
    """A closed-schema string check with a size cap (mirrors feeds field caps)."""
    assert what and max_len > 0, "what + positive max required"
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    if len(value) > max_len:
        raise ValueError(f"{what} exceeds {max_len} chars")
    return value


def _closed_keys(node: dict, allowed: set[str], what: str) -> None:
    """Refuse unknown keys — the whole point of a closed schema."""
    assert allowed, "allowed set required"
    assert what, "what required"
    extra = set(node) - allowed
    if extra:
        raise ValueError(f"{what} has unknown keys: {sorted(extra)}")


def validate_spec(spec: object) -> dict:
    """Validate an item spec (§2) end-to-end; return the validated dict.

    Refuses unknown top-level keys, checks every enum, delegates §3/§4/§5 shape to their
    own helpers, and refuses a system-set ``contract`` from the caller (the engine writes
    it at C1/C2 — see run_item). Raises ValueError with a short, precise message.
    """
    body = _require_dict(spec, "spec")
    # interval_minutes is a first-class spec field (``_clamp_interval`` reads it from here on
    # add_item + update_spec); it lives in the sealed body so a revision captures a cadence
    # change atomically with the source/scene it goes with.
    allowed = {"version", "title", "goal", "params", "source", "pipeline", "scene",
               "display", "contract", "repair_policy", "model", "_c2_ok",
               "interval_minutes", "history", "alerts"}
    _closed_keys(body, allowed, "spec")
    if body.get("version") != 1:
        raise ValueError("spec.version must be 1")
    _require_str(body.get("title"), "spec.title", max_len=_MAX_TITLE)
    _require_str(body.get("goal"), "spec.goal", max_len=_MAX_GOAL)
    _validate_params(body.get("params") or {})
    _validate_source(body.get("source"))
    outputs = _validate_pipeline(body.get("pipeline") or [])
    validate_scene(body.get("scene"))
    _validate_display(body.get("display") or {})
    _validate_repair_policy(body.get("repair_policy") or {})
    _validate_model_override(body.get("model"))
    if "history" in body and body["history"] is not None:
        _validate_history_spec(body["history"], outputs)
    if "alerts" in body and body["alerts"] is not None:
        _validate_alerts_spec(body["alerts"])
    # contract is system-written at commissioning; refuse a caller-supplied one so a spec
    # cannot self-attest its shape (the whole point of the C1/C2 verdict).
    if body.get("contract") is not None:
        _require_dict(body["contract"], "spec.contract")
    return body


def _validate_params(params: object) -> None:
    """§2 params map: name -> {label, kind, value}. Enums closed, sizes bounded."""
    node = _require_dict(params, "spec.params")
    if len(node) > _MAX_PARAMS:
        raise ValueError(f"spec.params exceeds {_MAX_PARAMS}")
    for name, raw in node.items():
        if not isinstance(name, str) or not _KEY_RE.match(name):
            raise ValueError(f"spec.params key {name!r} malformed")
        if len(name) > _MAX_PARAM_NAME:
            raise ValueError(f"spec.params.{name} name too long")
        p = _require_dict(raw, f"spec.params.{name}")
        _closed_keys(p, {"label", "kind", "value"}, f"spec.params.{name}")
        _require_str(p.get("label"), f"spec.params.{name}.label", max_len=200)
        if p.get("kind") not in _PARAM_KINDS:
            raise ValueError(f"spec.params.{name}.kind must be one of {sorted(_PARAM_KINDS)}")
        value = p.get("value")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError(f"spec.params.{name}.value must be a string or number")  # noqa: TRY004
        if isinstance(value, str) and len(value) > _MAX_PARAM_VALUE:
            raise ValueError(f"spec.params.{name}.value too long")


def _validate_source(source: object) -> None:
    """§3 sources — three closed types, each with its own shape."""
    s = _require_dict(source, "spec.source")
    stype = s.get("type")
    if stype not in _SOURCE_TYPES:
        raise ValueError(f"spec.source.type must be one of {sorted(_SOURCE_TYPES)}")
    if stype == "http_json":
        _validate_http_json_source(s)
    elif stype == "model":
        _validate_model_source(s)
    else:
        _validate_internal_schedule_source(s)


def _validate_http_json_source(s: dict) -> None:
    """§3 http_json: {type, url, headers}. URL is user-consented; $secret refs live here.

    URL structure is frozen at consent time: the raw template's scheme + authority must be
    literal (no ``{{param:``), so a filled-in param value can never rewrite the host or port
    the user approved. Placeholders are allowed only inside path/query. Plain header values
    are literals or ``$secret`` refs — never templated — so a param value can't smuggle
    auth-shaped bytes into a header. Auth-shaped literal header names are refused unless
    the value is a $secret ref (the credential belongs in the SecretStore).
    """
    _closed_keys(s, {"type", "url", "headers"}, "spec.source (http_json)")
    url = _require_str(s.get("url"), "spec.source.url", max_len=_MAX_URL)
    _validate_http_json_url_shape(url)
    headers = s.get("headers") or {}
    hdrs = _require_dict(headers, "spec.source.headers")
    if len(hdrs) > _MAX_HEADERS:
        raise ValueError(f"spec.source.headers exceeds {_MAX_HEADERS}")
    for name, value in hdrs.items():
        _require_str(name, "spec.source.headers key", max_len=_MAX_HEADER_NAME)
        is_secret = isinstance(value, dict)
        if is_secret:
            _closed_keys(value, {"$secret"}, f"spec.source.headers.{name}")
            ref = _require_str(value.get("$secret"), f"spec.source.headers.{name}.$secret",
                               max_len=_MAX_HEADER_VALUE)
            if not ref.startswith("ni:"):
                raise ValueError(
                    f"spec.source.headers.{name}.$secret must start with 'ni:' (item-scoped)"
                )
        else:
            literal = _require_str(value, f"spec.source.headers.{name}",
                                   max_len=_MAX_HEADER_VALUE)
            # Templating a plain header value would let a param inject bytes into auth-
            # shaped headers. Header literals are strings OR $secret refs — nothing else.
            if "{{param:" in literal:
                raise ValueError(
                    f"spec.source.headers.{name} may not use {{{{param:}}}} — "
                    "use a $secret ref or a plain literal"
                )
        if _is_auth_shaped_header(name) and not is_secret:
            raise ValueError(
                f"spec.source.headers.{name}: auth-shaped header requires a "
                "{\"$secret\": \"ni:<item>:<name>\"} value (never a plain literal)"
            )


def _validate_http_json_url_shape(url: str) -> None:
    """Refuse ``{{param:`` in the URL's scheme/authority; require literal http(s)://host.

    This runs on the RAW template BEFORE any param substitution, so a placeholder in the
    scheme, userinfo, host, or port can never move the request off the host the user
    consented to. Placeholders may appear only inside the path or query.
    """
    assert isinstance(url, str), "url must be a string"
    lowered = url.lower()
    if not lowered.startswith(("https://", "http://")):
        raise ValueError("spec.source.url must start with http:// or https:// (literal scheme)")
    scheme_end = url.find("://") + 3
    # Locate the end of the authority: first ``/`` (path), ``?`` (query), or ``#``.
    tail = url[scheme_end:]
    stops = [len(tail)] + [tail.find(ch) for ch in "/?#" if tail.find(ch) != -1]
    authority_end = scheme_end + min(stops)
    header = url[:authority_end]
    if "{{param:" in header:
        raise ValueError(
            "spec.source.url may only contain {{param:...}} in the path or query "
            "(scheme + host must be literal)"
        )
    # The authority must have SOMETHING (a host) — a raw "https:///path" is malformed.
    if authority_end == scheme_end:
        raise ValueError("spec.source.url must include a host after the scheme")


def _is_auth_shaped_header(name: str) -> bool:
    """True when a plain-literal value in this header would smuggle auth (K4)."""
    assert isinstance(name, str) and name, "header name required"
    low = name.lower()
    if low in _AUTH_HEADER_LITERAL_NAMES:
        return True
    return any(sub in low for sub in _AUTH_HEADER_TOKEN_SUBSTRINGS)


def _validate_model_source(s: dict) -> None:
    """§3 model: {type, instruction}. User-visible spec content."""
    _closed_keys(s, {"type", "instruction"}, "spec.source (model)")
    _require_str(s.get("instruction"), "spec.source.instruction", max_len=_MAX_INSTRUCTION)


def _validate_internal_schedule_source(s: dict) -> None:
    """§3 internal.schedule: {type, schedule_id}. Zero egress; reads schedule_runs."""
    _closed_keys(s, {"type", "schedule_id"}, "spec.source (internal.schedule)")
    _require_str(s.get("schedule_id"), "spec.source.schedule_id", max_len=100)


def _validate_pipeline(pipeline: object) -> set[str]:
    """§4 pipeline stages — extract / transform, each with its own shape.

    Returns the set of final top-level output names (used by history validation to
    refuse collisions per §11). Extract stages REPLACE the outputs (as ``_apply_extract``
    does at runtime); transforms mutate them in place (rename, aggregate ``as``)."""
    if not isinstance(pipeline, list):
        raise ValueError("spec.pipeline must be a list")  # noqa: TRY004
    if len(pipeline) > _MAX_PIPELINE_STAGES:
        raise ValueError(f"spec.pipeline exceeds {_MAX_PIPELINE_STAGES} stages")
    outputs: set[str] = set()
    for i, stage in enumerate(pipeline):
        st = _require_dict(stage, f"spec.pipeline[{i}]")
        op = st.get("op")
        if op == "extract":
            _validate_extract_stage(st, i)
            outputs = set((st.get("paths") or {}).keys())
        elif op == "transform":
            _validate_transform_stage(st, i, outputs)
        else:
            raise ValueError(f"spec.pipeline[{i}].op must be 'extract' or 'transform'")
    return outputs


def _validate_extract_stage(st: dict, i: int) -> None:
    """One extract stage: closed keys, path grammar per named output. ``item`` is reserved
    (repeat template roots resolve ``item.<...>`` against the current list element — an
    extract named ``item`` would collide with that bind root)."""
    _closed_keys(st, {"op", "paths"}, f"spec.pipeline[{i}]")
    paths = _require_dict(st.get("paths"), f"spec.pipeline[{i}].paths")
    if not paths or len(paths) > _MAX_EXTRACT_PATHS:
        raise ValueError(f"spec.pipeline[{i}].paths must be 1..{_MAX_EXTRACT_PATHS} entries")
    for name, path in paths.items():
        if not isinstance(name, str) or not _KEY_RE.match(name):
            raise ValueError(f"spec.pipeline[{i}].paths key {name!r} malformed")
        if name in _RESERVED_OUTPUT_NAMES:
            raise ValueError(
                f"spec.pipeline[{i}].paths.{name}: '{name}' is reserved (bind root)"
            )
        if not isinstance(path, str):
            raise ValueError(f"spec.pipeline[{i}].paths.{name} must be a string")  # noqa: TRY004
        parse_path(path)  # raises ValueError on any grammar violation


def _validate_transform_stage(st: dict, i: int, outputs: set[str]) -> None:
    """One transform stage: closed function set, per-fn required args.

    ``outputs`` is mutated in place as ops declare new names (rename ``to`` /
    aggregate + delta_prev ``as``) so a later op's aggregate can refuse an
    ``as`` name that already exists earlier in the pipeline (§4.2 collision rule).
    """
    _closed_keys(st, {"op", "apply"}, f"spec.pipeline[{i}]")
    apply = st.get("apply")
    if not isinstance(apply, list) or not apply or len(apply) > _MAX_TRANSFORM_APPLY:
        raise ValueError(f"spec.pipeline[{i}].apply must be 1..{_MAX_TRANSFORM_APPLY} ops")
    for j, op in enumerate(apply):
        _validate_transform_op(op, i, j, outputs)


def _validate_transform_op(op: object, i: int, j: int, outputs: set[str]) -> None:
    """One transform apply entry — closed fn set + required args per fn.

    ``outputs`` tracks running top-level names so v2 aggregates + delta_prev can refuse
    an ``as`` that collides with an existing output (§4.2). It is mutated in place
    (rename/aggregate/delta_prev add or move names).
    """
    node = _require_dict(op, f"spec.pipeline[{i}].apply[{j}]")
    fn = node.get("fn")
    if fn not in _TRANSFORM_FNS:
        raise ValueError(f"spec.pipeline[{i}].apply[{j}].fn must be one of {sorted(_TRANSFORM_FNS)}")
    field = node.get("field")
    if not isinstance(field, str) or not _KEY_RE.match(field):
        raise ValueError(f"spec.pipeline[{i}].apply[{j}].field malformed")
    where = f"spec.pipeline[{i}].apply[{j}]"
    if fn == "round":
        _closed_keys(node, {"fn", "field", "digits"}, where)
        if not isinstance(node.get("digits"), int) or isinstance(node.get("digits"), bool):
            raise ValueError(f"{where}.digits must be int")
    elif fn == "scale":
        _closed_keys(node, {"fn", "field", "factor"}, where)
        if not isinstance(node.get("factor"), (int, float)) or isinstance(node.get("factor"), bool):
            raise ValueError(f"{where}.factor must be number")
    elif fn == "rename":
        _closed_keys(node, {"fn", "field", "to"}, where)
        to = node.get("to")
        if not isinstance(to, str) or not _KEY_RE.match(to):
            raise ValueError(f"{where}.to malformed")
        if to in _RESERVED_OUTPUT_NAMES:
            raise ValueError(f"{where}.to: {to!r} is reserved (bind root)")
        outputs.discard(field)
        outputs.add(to)
    elif fn == "pick":
        _closed_keys(node, {"fn", "field", "keys"}, where)
        keys = node.get("keys")
        if not isinstance(keys, list) or not keys or not all(
                isinstance(k, str) and _KEY_RE.match(k) for k in keys):
            raise ValueError(f"{where}.keys must be a non-empty key list")
    elif fn == "sort_by":
        _closed_keys(node, {"fn", "field", "key", "dir"}, where)
        if "key" in node and not (isinstance(node["key"], str) and _KEY_RE.match(node["key"])):
            raise ValueError(f"{where}.key malformed")
        if node.get("dir") not in _SORT_DIRS:
            raise ValueError(f"{where}.dir must be 'asc' or 'desc'")
    elif fn == "top_n":
        _closed_keys(node, {"fn", "field", "n"}, where)
        n = node.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > _MAX_TOP_N:
            raise ValueError(f"{where}.n must be 1..{_MAX_TOP_N}")
    elif fn == "count":
        _closed_keys(node, {"fn", "field", "as"}, where)
        _validate_transform_as(node.get("as"), where, outputs)
    elif fn in _AGGREGATE_FNS:
        _closed_keys(node, {"fn", "field", "key", "as"}, where)
        key = node.get("key")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ValueError(f"{where}.key malformed")
        _validate_transform_as(node.get("as"), where, outputs)
    else:  # delta_prev
        _closed_keys(node, {"fn", "field", "series", "as"}, where)
        series = node.get("series")
        if not isinstance(series, str) or not _KEY_RE.match(series):
            raise ValueError(f"{where}.series malformed")
        _validate_transform_as(node.get("as"), where, outputs)


def _validate_transform_as(name: object, where: str, outputs: set[str]) -> None:
    """Shared ``as`` guard for v2 aggregates + count + delta_prev (§4.2 collision rule).

    Refuses malformed names, reserved names, and collisions with existing outputs; on
    success, mutates ``outputs`` in place so a later op's ``as`` sees the new name.
    """
    assert isinstance(where, str) and where, "where required"
    assert isinstance(outputs, set), "outputs must be a set"
    if not isinstance(name, str) or not _KEY_RE.match(name):
        raise ValueError(f"{where}.as malformed")
    if name in _RESERVED_OUTPUT_NAMES:
        raise ValueError(f"{where}.as: {name!r} is reserved (bind root)")
    if name in outputs:
        raise ValueError(f"{where}.as: {name!r} collides with an existing pipeline output")
    outputs.add(name)


def _validate_display(display: object) -> None:
    node = _require_dict(display, "spec.display")
    _closed_keys(node, {"size"}, "spec.display")
    if node.get("size") not in _DISPLAY_SIZES:
        raise ValueError(f"spec.display.size must be one of {sorted(_DISPLAY_SIZES)}")


def _validate_repair_policy(policy: object) -> None:
    node = _require_dict(policy, "spec.repair_policy")
    _closed_keys(node, {"l1", "l2_frontier"}, "spec.repair_policy")
    for k in ("l1", "l2_frontier"):
        if not isinstance(node.get(k), bool):
            raise ValueError(f"spec.repair_policy.{k} must be bool")  # noqa: TRY004


def _validate_model_override(model: object) -> None:
    if model is None:
        return
    if not isinstance(model, str) or "/" not in model:
        raise ValueError("spec.model must be 'provider/model' or null")


def _validate_history_spec(history: object, outputs: set[str]) -> None:
    """§11 history: {track: {name: path}, max_points}. ≤4 series; name obeys the output
    namespace rules (not ``item``, not ``history``, no collision with pipeline outputs)."""
    assert isinstance(outputs, set), "outputs must be a set"
    node = _require_dict(history, "spec.history")
    _closed_keys(node, {"track", "max_points"}, "spec.history")
    track = _require_dict(node.get("track"), "spec.history.track")
    if not track or len(track) > _MAX_HISTORY_SERIES:
        raise ValueError(f"spec.history.track must be 1..{_MAX_HISTORY_SERIES} series")
    for name, path in track.items():
        if not isinstance(name, str) or not _KEY_RE.match(name):
            raise ValueError(f"spec.history.track key {name!r} malformed")
        if name in _RESERVED_OUTPUT_NAMES:
            raise ValueError(f"spec.history.track.{name}: {name!r} is reserved")
        if name in outputs:
            raise ValueError(
                f"spec.history.track.{name}: collides with pipeline output {name!r}"
            )
        if not isinstance(path, str):
            raise ValueError(f"spec.history.track.{name} path must be a string")  # noqa: TRY004
        parse_path(path)
    if "max_points" in node:
        mp = node["max_points"]
        if not isinstance(mp, int) or isinstance(mp, bool) or mp < 1:
            raise ValueError("spec.history.max_points must be a positive integer")


def _validate_alerts_spec(alerts: object) -> None:
    """§12 alerts: ≤5 rules; slug names unique; when-shaped left/op/right; message template
    ≤500 chars; cooldown_minutes clamped ≥5 at engine time (bounds re-checked below)."""
    if not isinstance(alerts, list):
        raise ValueError("spec.alerts must be a list")  # noqa: TRY004
    if len(alerts) > _MAX_ALERTS:
        raise ValueError(f"spec.alerts exceeds {_MAX_ALERTS} rules")
    seen: set[str] = set()
    for i, rule in enumerate(alerts):
        where = f"spec.alerts[{i}]"
        r = _require_dict(rule, where)
        _closed_keys(r, {"name", "left", "op", "right", "message", "cooldown_minutes"}, where)
        name = r.get("name")
        if not isinstance(name, str) or not _ALERT_NAME_RE.match(name):
            raise ValueError(f"{where}.name must match [a-z0-9-]{{1,{_MAX_ALERT_NAME}}}")
        if name in seen:
            raise ValueError(f"{where}.name {name!r} is a duplicate")
        seen.add(name)
        _validate_when_operand(r.get("left"), f"{where}.left")
        _validate_when_operand(r.get("right"), f"{where}.right")
        if r.get("op") not in _WHEN_OPS:
            raise ValueError(f"{where}.op must be one of {sorted(_WHEN_OPS)}")
        message = r.get("message")
        if not isinstance(message, str) or not message or len(message) > _MAX_ALERT_MESSAGE:
            raise ValueError(f"{where}.message must be 1..{_MAX_ALERT_MESSAGE} chars")
        # Grammar-check every {{path}} in the template (excluding the {{title}} macro).
        for match in _BIND_INTERP.finditer(message):
            token = match.group(1)
            if token == "title":
                continue
            try:
                parse_path(token)
            except ValueError as exc:
                raise ValueError(f"{where}.message: bad {{{{path}}}} {token!r}: {exc}") from None
        if "cooldown_minutes" in r:
            cd = r["cooldown_minutes"]
            if not isinstance(cd, int) or isinstance(cd, bool):
                raise ValueError(f"{where}.cooldown_minutes must be an integer")


def validate_scene(scene: object) -> dict:
    """§5 scene grammar — closed node set, tone/role/size enums, PRE-EXPANSION caps.

    Rejects the v1-reserved node types (spark/gauge/image/when/on_tap) so an older client
    refuses a new-scene design rather than mis-rendering it. The post-expansion 100-node
    cap is re-checked in ``bind_scene`` after ``repeat`` is materialized.
    """
    node = _require_dict(scene, "spec.scene")
    counter = _NodeCounter()
    _validate_scene_node(node, depth=1, counter=counter)
    if counter.count > _MAX_SCENE_NODES_PRE_EXPAND:
        raise ValueError(f"scene has {counter.count} nodes (max {_MAX_SCENE_NODES_PRE_EXPAND})")
    return node


class _NodeCounter:
    """Mutable counter shared across the recursion-free tree walk (a plain int wouldn't
    survive parameter passing — closures are avoided per project style)."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


def _validate_scene_node(node: object, depth: int, counter: _NodeCounter) -> None:
    """Iterative pre-order walk (no recursion, POW10 #1). Each node validates its own
    shape via a dispatch table; children are pushed onto the pending stack.

    ``in_repeat`` propagates down a repeat's template: a second repeat encountered while
    in_repeat is True is refused (K5 nested-repeat guard — the binder's repeat expander
    can't safely resolve nested ``item.<...>`` chains against two different list roots)."""
    assert counter is not None, "counter required"
    assert depth >= 1, "depth must start at 1"
    pending: list[tuple[object, int, bool]] = [(node, depth, False)]
    for _ in range(2 * _MAX_SCENE_NODES_PRE_EXPAND):  # bounded (POW10 #2); the cap is the real bound
        if not pending:
            return
        current, d, in_repeat = pending.pop()
        if d > _MAX_SCENE_DEPTH:
            raise ValueError(f"scene depth exceeds {_MAX_SCENE_DEPTH}")
        n = _require_dict(current, "scene node")
        ntype = n.get("type")
        if ntype in _RESERVED_SCENE_TYPES:
            raise ValueError(f"scene node type {ntype!r} is reserved and refused in v1")
        if ntype not in _SCENE_TYPES:
            raise ValueError(f"scene node type {ntype!r} unknown")
        if ntype == "repeat" and in_repeat:
            raise ValueError("scene repeat may not be nested inside another repeat template")
        counter.count += 1
        children = _validate_scene_shape(n)
        # Anything under a repeat's template inherits in_repeat=True (so a stack-wrapped nested
        # repeat is still caught). A top-level sibling repeat under the same stack is fine.
        child_in_repeat = in_repeat or ntype == "repeat"
        for child in reversed(children):  # bounded by the current node's children
            pending.append((child, d + 1, child_in_repeat))
    raise ValueError("scene traversal exceeded bound")


def _validate_scene_shape(node: dict) -> list[object]:
    """Per-type shape check; returns the child list to push (empty when leaf)."""
    dispatch = {
        "stack": _validate_stack, "grid": _validate_grid, "divider": _validate_divider,
        "text": _validate_text, "number": _validate_number, "chip": _validate_chip,
        "bar": _validate_bar, "icon": _validate_icon, "repeat": _validate_repeat,
        "spark": _validate_spark, "gauge": _validate_gauge,
    }
    fn = dispatch[node["type"]]
    return fn(node)


def _validate_stack(node: dict) -> list[object]:
    _closed_keys(node, {"type", "dir", "gap", "children"}, "scene stack")
    if node.get("dir") not in _STACK_DIRS:
        raise ValueError("scene stack.dir must be 'v' or 'h'")
    if node.get("gap") not in _STACK_GAPS:
        raise ValueError("scene stack.gap must be 'sm' or 'md'")
    children = node.get("children")
    if not isinstance(children, list):
        raise ValueError("scene stack.children must be a list")  # noqa: TRY004
    return list(children)


def _validate_grid(node: dict) -> list[object]:
    _closed_keys(node, {"type", "cols", "children"}, "scene grid")
    cols = node.get("cols")
    if not isinstance(cols, int) or isinstance(cols, bool) or cols < 2 or cols > 4:
        raise ValueError("scene grid.cols must be 2..4")
    children = node.get("children")
    if not isinstance(children, list):
        raise ValueError("scene grid.children must be a list")  # noqa: TRY004
    return list(children)


def _validate_divider(node: dict) -> list[object]:
    _closed_keys(node, {"type"}, "scene divider")
    return []


def _validate_text(node: dict) -> list[object]:
    _closed_keys(node, {"type", "value", "role", "tone", "size", "when"}, "scene text")
    _validate_bindable(node.get("value"), "scene text.value", allow_string=True, max_len=_MAX_TEXT_CHARS)
    if node.get("role") not in _TEXT_ROLES:
        raise ValueError(f"scene text.role must be one of {sorted(_TEXT_ROLES)}")
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene text.tone must be one of {sorted(_TEXT_TONES)}")
    if node.get("size") not in _TEXT_SIZES:
        raise ValueError(f"scene text.size must be one of {sorted(_TEXT_SIZES)}")
    _validate_when(node, "scene text")
    return []


def _validate_number(node: dict) -> list[object]:
    _closed_keys(node, {"type", "value", "format", "unit", "tone", "size", "when"}, "scene number")
    _validate_bindable(node.get("value"), "scene number.value", allow_string=False, max_len=0)
    if node.get("format") not in _NUM_FORMATS:
        raise ValueError(f"scene number.format must be one of {sorted(_NUM_FORMATS)}")
    unit = node.get("unit")
    if unit is not None:
        if not isinstance(unit, str) or len(unit) > 20:
            raise ValueError("scene number.unit must be a short string or null")
        # bind runs _bind_value over every non-children prop, so unit can carry {{path}}
        # interpolations. Grammar-check them up-front (G1) so bind_scene never raises a
        # raw ValueError from parse_path on this field.
        _check_interp_grammar(unit, "scene number.unit")
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene number.tone must be one of {sorted(_TEXT_TONES)}")
    if node.get("size") not in _TEXT_SIZES:
        raise ValueError(f"scene number.size must be one of {sorted(_TEXT_SIZES)}")
    _validate_when(node, "scene number")
    return []


def _check_interp_grammar(value: str, what: str) -> None:
    """Validate every ``{{path}}`` inside a string at validation time (G1)."""
    assert isinstance(value, str), "value must be a string"
    assert what, "what required"
    for match in _BIND_INTERP.finditer(value):
        token = match.group(1)
        if token.startswith("param:"):
            continue  # param substitution runs pre-bind; grammar checked elsewhere
        try:
            parse_path(token)
        except ValueError as exc:
            raise ValueError(f"{what}: bad {{{{path}}}} interpolation: {exc}") from None


def _validate_chip(node: dict) -> list[object]:
    _closed_keys(node, {"type", "value", "kind", "when"}, "scene chip")
    _validate_bindable(node.get("value"), "scene chip.value", allow_string=True, max_len=_MAX_TEXT_CHARS)
    if node.get("kind") not in _CHIP_KINDS:
        raise ValueError(f"scene chip.kind must be one of {sorted(_CHIP_KINDS)}")
    _validate_when(node, "scene chip")
    return []


def _validate_bar(node: dict) -> list[object]:
    _closed_keys(node, {"type", "value", "max", "tone", "when"}, "scene bar")
    _validate_bindable(node.get("value"), "scene bar.value", allow_string=False, max_len=0)
    _validate_bindable(node.get("max"), "scene bar.max", allow_string=False, max_len=0)
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene bar.tone must be one of {sorted(_TEXT_TONES)}")
    _validate_when(node, "scene bar")
    return []


def _validate_icon(node: dict) -> list[object]:
    _closed_keys(node, {"type", "name", "tone", "when"}, "scene icon")
    name = _require_str(node.get("name"), "scene icon.name", max_len=60)
    # Literal-only (K1): no {{...}} or $bind. An icon name is a design token, not data —
    # binder-driven names would let a payload string reach the renderer's icon dispatcher.
    if not _ICON_NAME_RE.match(name):
        raise ValueError(
            "scene icon.name must be lowercase kebab (a-z, 0-9, -) 1..60 chars, "
            "literal (no {{}} or $bind)"
        )
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene icon.tone must be one of {sorted(_TEXT_TONES)}")
    _validate_when(node, "scene icon")
    return []


def _validate_spark(node: dict) -> list[object]:
    """§5 spark: {points ($bind or literal list of numbers / {t,v}), kind, tone, when}."""
    _closed_keys(node, {"type", "points", "kind", "tone", "when"}, "scene spark")
    points = node.get("points")
    if isinstance(points, dict):
        _closed_keys(points, {"$bind"}, "scene spark.points")
        path = points.get("$bind")
        if not isinstance(path, str):
            raise ValueError("scene spark.points.$bind must be a string")  # noqa: TRY004
        parse_path(path)
    elif isinstance(points, list):
        if len(points) > _MAX_SPARK_POINTS:
            raise ValueError(f"scene spark.points literal exceeds {_MAX_SPARK_POINTS}")
        for i, p in enumerate(points):  # bounded by _MAX_SPARK_POINTS
            _validate_spark_literal_point(p, i)
    else:
        raise ValueError("scene spark.points must be a $bind object or a literal list")  # noqa: TRY004
    if node.get("kind") not in _SPARK_KINDS:
        raise ValueError(f"scene spark.kind must be one of {sorted(_SPARK_KINDS)}")
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene spark.tone must be one of {sorted(_TEXT_TONES)}")
    _validate_when(node, "scene spark")
    return []


def _validate_spark_literal_point(p: object, i: int) -> None:
    """A literal spark point is a bare number OR a closed-shape {t: str, v: number}."""
    assert i >= 0, "index must be non-negative"
    if isinstance(p, (int, float)) and not isinstance(p, bool):
        return
    if isinstance(p, dict):
        _closed_keys(p, {"t", "v"}, f"scene spark.points[{i}]")
        if not isinstance(p.get("t"), str):
            raise ValueError(f"scene spark.points[{i}].t must be a string")  # noqa: TRY004
        v = p.get("v")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"scene spark.points[{i}].v must be a number")  # noqa: TRY004
        return
    raise ValueError(f"scene spark.points[{i}] must be a number or {{t,v}} object")


def _validate_gauge(node: dict) -> list[object]:
    """§5 gauge: {value/min/max/tone/label, when}. Literal max > literal min (§5 rule)."""
    _closed_keys(node, {"type", "value", "min", "max", "tone", "label", "when"}, "scene gauge")
    _validate_bindable(node.get("value"), "scene gauge.value", allow_string=False, max_len=0)
    _validate_bindable(node.get("min"), "scene gauge.min", allow_string=False, max_len=0)
    _validate_bindable(node.get("max"), "scene gauge.max", allow_string=False, max_len=0)
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene gauge.tone must be one of {sorted(_TEXT_TONES)}")
    label = node.get("label")
    if not isinstance(label, str) or len(label) > _MAX_TEXT_CHARS:
        raise ValueError(f"scene gauge.label must be a string <= {_MAX_TEXT_CHARS} chars")
    _check_interp_grammar(label, "scene gauge.label")
    # max > min when both are literal numbers (bind-time values can't be checked at spec).
    lit_min = _literal_number(node.get("min"))
    lit_max = _literal_number(node.get("max"))
    if lit_min is not None and lit_max is not None and lit_max <= lit_min:
        raise ValueError("scene gauge.max must be > min when both are literal numbers")
    _validate_when(node, "scene gauge")
    return []


def _literal_number(value: object) -> float | None:
    """Return ``value`` as a float when it is a literal number, else None ($bind counts as None)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _validate_when(node: dict, what: str) -> None:
    """§5 Conditions: content-node ``when`` list. Missing key = OK, empty list = OK too."""
    assert isinstance(what, str) and what, "what required"
    if "when" not in node:
        return
    rules = node["when"]
    if not isinstance(rules, list):
        raise ValueError(f"{what}.when must be a list")  # noqa: TRY004
    if len(rules) > _MAX_WHEN_RULES:
        raise ValueError(f"{what}.when exceeds {_MAX_WHEN_RULES} rules")
    ntype = node.get("type") if isinstance(node.get("type"), str) else None
    for i, rule in enumerate(rules):  # bounded by _MAX_WHEN_RULES
        _validate_when_rule(rule, f"{what}.when[{i}]", node_type=ntype)


def _validate_when_rule(rule: object, where: str, *, node_type: str | None = None) -> None:
    """One §5 rule: {left, op, right, set} — set carries tone and/or hidden:true.

    LOW#3 (audit 2026-09-09): a chip node has ``kind`` (not ``tone``); a ``set.tone``
    rule on a chip would never take effect. Refuse it at validation time — the caller
    threads ``node_type`` in from ``_validate_when``.
    """
    assert isinstance(where, str) and where, "where required"
    r = _require_dict(rule, where)
    _closed_keys(r, {"left", "op", "right", "set"}, where)
    _validate_when_operand(r.get("left"), f"{where}.left")
    _validate_when_operand(r.get("right"), f"{where}.right")
    if r.get("op") not in _WHEN_OPS:
        raise ValueError(f"{where}.op must be one of {sorted(_WHEN_OPS)}")
    s = _require_dict(r.get("set"), f"{where}.set")
    _closed_keys(s, {"tone", "hidden"}, f"{where}.set")
    if not s:
        raise ValueError(f"{where}.set must set at least tone or hidden")
    if "tone" in s and s["tone"] not in _TEXT_TONES:
        raise ValueError(f"{where}.set.tone must be one of {sorted(_TEXT_TONES)}")
    if "tone" in s and node_type == "chip":
        raise ValueError(
            f"{where}.set.tone refused on chip nodes (chips carry 'kind', not 'tone')"
        )
    if "hidden" in s and s["hidden"] is not True:
        raise ValueError(f"{where}.set.hidden must be true when present")


def _validate_when_operand(value: object, where: str) -> None:
    """A when operand is a $bind object OR a JSON scalar (str/int/float/bool/None)."""
    assert isinstance(where, str) and where, "where required"
    if isinstance(value, dict):
        _closed_keys(value, {"$bind"}, where)
        path = value.get("$bind")
        if not isinstance(path, str):
            raise ValueError(f"{where}.$bind must be a string")  # noqa: TRY004
        parse_path(path)
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    raise ValueError(f"{where} must be a $bind object or JSON scalar")


def _validate_repeat(node: dict) -> list[object]:
    _closed_keys(node, {"type", "items", "max", "template"}, "scene repeat")
    items = node.get("items")
    if not (isinstance(items, dict) and "$bind" in items):
        raise ValueError("scene repeat.items must be a $bind object")
    _validate_bindable(items, "scene repeat.items", allow_string=False, max_len=0)
    max_n = node.get("max")
    if not isinstance(max_n, int) or isinstance(max_n, bool) or max_n < 1 or max_n > _MAX_REPEAT_MAX:
        raise ValueError(f"scene repeat.max must be 1..{_MAX_REPEAT_MAX}")
    template = node.get("template")
    if not isinstance(template, dict):
        raise ValueError("scene repeat.template must be a node")  # noqa: TRY004
    return [template]  # validated as a child; item. paths deferred to bind time


def _validate_bindable(value: object, what: str, *, allow_string: bool, max_len: int) -> None:
    """A scene value slot: JSON literal or ``{"$bind": path}``. Interpolation only on strings."""
    assert what, "what required"
    if isinstance(value, dict):
        _closed_keys(value, {"$bind"}, what)
        path = value.get("$bind")
        if not isinstance(path, str):
            raise ValueError(f"{what}.$bind must be a string")  # noqa: TRY004
        parse_path(path)  # grammar check up-front; missing at bind time = run failure
        return
    if allow_string and isinstance(value, str):
        if len(value) > max_len:
            raise ValueError(f"{what} exceeds {max_len} chars")
        # {{path}} interpolations validate their grammar now; resolution is bind-time.
        for match in _BIND_INTERP.finditer(value):
            token = match.group(1)
            if not token.startswith("param:"):
                parse_path(token)
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return
    if allow_string:
        raise ValueError(f"{what} must be string, number, or $bind")
    raise ValueError(f"{what} must be a number or $bind")


# --- pipeline executor (pure) ---------------------------------------------

def _resolve_path(payload: object, steps: list[tuple]) -> Any:
    """Apply parsed path ops to ``payload``; raise NIError('extract_miss') on any miss."""
    assert isinstance(steps, list) and steps, "steps required"
    cur: Any = payload
    for op in steps:  # bounded by parse_path
        if op[0] == "key":
            if not isinstance(cur, dict) or op[1] not in cur:
                raise NIError("extract_miss", f"key {op[1]!r}")
            cur = cur[op[1]]
        elif op[0] == "index":
            if not isinstance(cur, list):
                raise NIError("extract_miss", "index on non-list")
            try:
                cur = cur[op[1]]
            except IndexError:
                raise NIError("extract_miss", "index out of range") from None
        else:  # slice
            if not isinstance(cur, list):
                raise NIError("extract_miss", "slice on non-list")
            cur = cur[op[1]:op[2]]
    return cur


def run_pipeline(stages: list[dict], payload: object,
                 *, history: dict | None = None) -> dict:
    """Execute the pipeline (§4); return the named-outputs dict. Raises NIError on any
    failure (missing path, transform type mismatch — never coercion).

    ``history`` (§11) is passed through to the transform executor so delta_prev can
    read the last completed run's series. Default None = empty series (first run).
    """
    assert isinstance(stages, list), "stages must be a list"
    assert payload is not None, "payload required"
    hist = history if history is not None else {}
    current: object = payload
    for stage in stages:  # bounded by _MAX_PIPELINE_STAGES
        op = stage.get("op")
        if op == "extract":
            current = _apply_extract(stage.get("paths") or {}, current)
        elif op == "transform":
            current = _apply_transform(stage.get("apply") or [], current, history=hist)
        else:
            raise NIError("pipeline_bad_stage", str(op))
    if not isinstance(current, dict):
        raise NIError("pipeline_bad_output", "final output is not an object")
    return current


def _apply_extract(paths: dict, payload: object) -> dict:
    """Resolve each named path against ``payload``; missing = NIError."""
    out: dict = {}
    for name, path in paths.items():  # bounded by _MAX_EXTRACT_PATHS
        steps = parse_path(path)
        out[name] = _resolve_path(payload, steps)
    return out


def _apply_transform(apply: list[dict], payload: object, *, history: dict) -> dict:
    """Apply each transform op to the named-outputs dict, in order."""
    if not isinstance(payload, dict):
        raise NIError("transform_needs_dict", "transform requires an extract dict")
    assert isinstance(history, dict), "history must be a dict (may be empty)"
    current = dict(payload)  # copy: transforms MUST NOT mutate caller state
    for op in apply:  # bounded by _MAX_TRANSFORM_APPLY
        current = _apply_transform_op(op, current, history=history)
    return current


def _apply_transform_op(op: dict, payload: dict, *, history: dict) -> dict:
    """Dispatch one transform apply entry against the current dict."""
    fn = op["fn"]
    field = op["field"]
    if field not in payload and fn != "rename":
        raise NIError("transform_miss", f"field {field!r}")
    out = dict(payload)
    if fn == "round":
        out[field] = _txf_round(payload[field], op["digits"])
    elif fn == "scale":
        out[field] = _txf_scale(payload[field], op["factor"])
    elif fn == "rename":
        out = _txf_rename(out, field, op["to"])
    elif fn == "pick":
        out[field] = _txf_pick(payload[field], op["keys"])
    elif fn == "sort_by":
        out[field] = _txf_sort(payload[field], op.get("key"), op["dir"])
    elif fn == "top_n":
        out[field] = _txf_top_n(payload[field], op["n"])
    elif fn == "count":
        out[op["as"]] = _txf_count(payload[field])
    elif fn in _AGGREGATE_FNS:
        out[op["as"]] = _txf_aggregate(fn, payload[field], op["key"])
    else:  # delta_prev
        out[op["as"]] = _txf_delta_prev(payload[field], op["series"], history)
    return out


def _txf_round(value: object, digits: int) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise NIError("transform_type", "round needs a number")
    assert isinstance(digits, int), "digits already validated as int"
    return round(float(value), digits)


def _txf_scale(value: object, factor: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise NIError("transform_type", "scale needs a number")
    assert isinstance(factor, (int, float)) and not isinstance(factor, bool), \
        "factor already validated as number"
    return float(value) * float(factor)


def _txf_rename(payload: dict, field: str, to: str) -> dict:
    if field not in payload:
        raise NIError("transform_miss", f"rename source {field!r}")
    assert to and isinstance(to, str), "to already validated as key"
    payload[to] = payload.pop(field)
    return payload


def _txf_pick(value: object, keys: list) -> list:
    if not isinstance(value, list):
        raise NIError("transform_type", "pick needs a list")
    assert isinstance(keys, list) and keys, "keys already validated as non-empty"
    out: list = []
    for entry in value:  # bounded by input length
        if not isinstance(entry, dict):
            raise NIError("transform_type", "pick needs a list of objects")
        out.append({k: entry[k] for k in keys if k in entry})
    return out


def _txf_sort(value: object, key: str | None, direction: str) -> list:
    if not isinstance(value, list):
        raise NIError("transform_type", "sort_by needs a list")
    assert direction in _SORT_DIRS, "direction already validated"
    reverse = direction == "desc"
    try:
        if key is None:
            return sorted(value, reverse=reverse)
        return sorted(value, key=lambda x: x[key], reverse=reverse)
    except (TypeError, KeyError) as exc:
        raise NIError("transform_type", f"sort failed: {exc.__class__.__name__}") from None


def _txf_top_n(value: object, n: int) -> list:
    if not isinstance(value, list):
        raise NIError("transform_type", "top_n needs a list")
    assert 1 <= n <= _MAX_TOP_N, "n already validated"
    return value[:n]


def _txf_count(value: object) -> int:
    """v2 count(field, as): length of a list. Empty list counts 0 (never fails)."""
    if not isinstance(value, list):
        raise NIError("transform_type", "count needs a list")
    assert isinstance(value, list), "invariant: value is a list"
    return len(value)


def _txf_aggregate(fn: str, value: object, key: str) -> float:
    """v2 sum/avg/min/max(field, key, as) over a list-of-objects; empty list = failure."""
    assert fn in _AGGREGATE_FNS, "fn already validated"
    if not isinstance(value, list):
        raise NIError("transform_type", f"{fn} needs a list")
    if not value:
        raise NIError("empty_aggregate", f"{fn} on empty list")
    numbers: list[float] = []
    for entry in value:  # bounded by input length
        if not isinstance(entry, dict) or key not in entry:
            raise NIError("transform_type", f"{fn} needs list of objects with {key!r}")
        v = entry[key]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise NIError("transform_type", f"{fn}: non-numeric {key!r}")
        numbers.append(float(v))
    if fn == "sum":
        return float(sum(numbers))
    if fn == "avg":
        return float(sum(numbers) / len(numbers))
    if fn == "min":
        return float(min(numbers))
    return float(max(numbers))


def _txf_delta_prev(value: object, series: str, history: dict) -> dict:
    """v2 delta_prev(field, series, as): current field minus last point of history series.

    First-run rule (§4.2): when the referenced series is empty (or absent), write
    ``{value: 0, direction: "flat"}`` — never a failure, so commissioning still passes.
    Direction is ``up`` / ``down`` / ``flat`` from the numeric delta's sign.
    """
    assert isinstance(series, str) and series, "series required"
    assert isinstance(history, dict), "history must be a dict"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise NIError("transform_type", "delta_prev needs a number in field")
    points = history.get(series) or []
    if not isinstance(points, list) or not points:
        return {"value": 0, "direction": "flat"}
    last = points[-1]
    if isinstance(last, dict) and "v" in last:
        prev = last["v"]
    else:
        prev = last
    if not isinstance(prev, (int, float)) or isinstance(prev, bool):
        raise NIError("transform_type", "delta_prev: previous point not numeric")
    delta = float(value) - float(prev)
    if delta > 0:
        direction = "up"
    elif delta < 0:
        direction = "down"
    else:
        direction = "flat"
    return {"value": delta, "direction": direction}


# --- param substitution + bind (pure) -------------------------------------

def substitute_params(spec: dict) -> dict:
    """Return a copy of ``spec`` with ``{{param:X}}`` filled from ``spec.params`` values.

    Rejects a secret-kind param appearing anywhere — secrets attach only through
    ``{"$secret": "..."}`` in headers, resolved at fetch time. String and number params
    substitute inline; everything else passes through unchanged. Values landing inside
    ``source.url`` are percent-encoded (D2) so a param value like ``../..`` or
    ``&admin=1`` can never rewrite URL structure the user consented to.
    """
    assert isinstance(spec, dict), "spec must be a dict"
    params = spec.get("params") or {}
    assert isinstance(params, dict), "params must be a dict"
    result = json.loads(json.dumps(spec))  # deep copy — the spec dict must not be mutated
    _substitute_in(result, params, path="spec")
    return result


def _substitute_in(node: object, params: dict, *, path: str) -> None:
    """Walk ``node`` in place; substitute {{param:X}} in every string except $secret bodies.

    Any string reached at ``spec.source.url`` is filled with the URL-encoded variant so
    param values cannot inject URL structure (see ``_resolve_param_string``).
    """
    assert path, "path required for error context"
    assert params is not None, "params required"
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str):
                node[k] = _resolve_param_string(
                    v, params, path=f"{path}.{k}",
                    url_encode=(path == "spec.source" and k == "url"),
                )
            elif isinstance(v, (dict, list)):
                if isinstance(v, dict) and set(v.keys()) == {"$secret"}:
                    continue  # $secret bodies are opaque names, resolved at fetch time
                _substitute_in(v, params, path=f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = _resolve_param_string(v, params, path=f"{path}[{i}]",
                                                url_encode=False)
            elif isinstance(v, (dict, list)):
                _substitute_in(v, params, path=f"{path}[{i}]")


def _resolve_param_string(value: str, params: dict, *, path: str,
                          url_encode: bool = False) -> str:
    """Return ``value`` with every ``{{param:X}}`` filled; secret refs refused.

    ``url_encode`` (D2): when substituting into ``source.url``, each value is
    percent-encoded (``safe=""``) so ``?``/``&``/``/``/``#`` in a value can never
    reshape the URL. The literal template around the placeholder stays untouched.
    """
    assert isinstance(value, str), "value must be a string"
    assert path, "path required"

    def _one(m: re.Match) -> str:
        name = m.group(1)
        if name not in params:
            raise ValueError(f"{path}: unknown param {name!r}")
        p = params[name]
        if not isinstance(p, dict) or p.get("kind") not in _PARAM_KINDS:
            raise ValueError(f"{path}: param {name!r} malformed")
        if p["kind"] == "secret":
            raise ValueError(f"{path}: secret param {name!r} may not be substituted inline")
        raw = str(p.get("value"))
        return _url_quote(raw, safe="") if url_encode else raw

    return _PARAM_PLACEHOLDER.sub(_one, value)


def bind_scene(scene: dict, data: dict, *, history: dict | None = None) -> dict:
    """Return the scene with $bind + {{path}} resolved and repeat nodes expanded.

    Enforces the post-expansion caps (nodes <= 100, depth <= 8, text <= 2000). Any
    unresolved binding, type mismatch, or cap violation raises NIError.

    ``history`` (§11) is exposed to the binder as the read-only ``history.<name>``
    namespace: internally we merge ``{"history": history}`` into ``data`` so a
    ``$bind: history.price`` walks the standard resolver. The pipeline guarantees no
    top-level output is named ``history`` (see ``_RESERVED_OUTPUT_NAMES``), so the
    merge cannot shadow user data.
    """
    assert isinstance(scene, dict), "scene must be a dict"
    assert isinstance(data, dict), "data must be a dict"
    merged: dict = dict(data)
    if history is not None:
        assert isinstance(history, dict), "history must be a dict"
        merged["history"] = history
    counter = _NodeCounter()
    bound = _bind_node(scene, merged, depth=1, item=None, counter=counter)
    if bound is None:
        raise NIError("bind_type", "scene root cannot be hidden by when")
    if counter.count > _MAX_SCENE_NODES:
        raise NIError("bind_scene_too_large", f"{counter.count} nodes (max {_MAX_SCENE_NODES})")
    return bound


def _bind_node(node: object, data: dict, *, depth: int, item: Any,
               counter: _NodeCounter) -> dict | None:
    """Bind one scene node; returns None when a ``when`` rule set ``hidden: true``.

    Callers with a ``children`` list filter None entries so the hidden node is dropped
    from the bound payload entirely (§5 Conditions). ``when`` never survives binding —
    it is consumed here and no ``when`` key is copied into the output.
    """
    assert counter is not None, "counter required"
    if not isinstance(node, dict):
        raise NIError("bind_bad_node", "scene node must be a dict")
    if depth > _MAX_SCENE_DEPTH:
        raise NIError("bind_depth", f"exceeds {_MAX_SCENE_DEPTH}")
    tone_override: str | None = None
    if "when" in node:
        applied = _apply_when(node.get("when") or [], data, item=item)
        if applied.get("hidden") is True:
            return None
        tone_override = applied.get("tone")
    counter.count += 1
    if counter.count > _MAX_SCENE_NODES:
        raise NIError("bind_scene_too_large", f"{counter.count} nodes (max {_MAX_SCENE_NODES})")
    ntype = node.get("type")
    if ntype == "repeat":
        return _bind_repeat(node, data, depth=depth, counter=counter)
    out: dict = {"type": ntype}
    for key, value in node.items():
        if key in ("type", "when"):
            continue
        if key == "children":
            child_nodes: list[dict] = []
            for c in (value or []):  # bounded by scene node cap
                bound_child = _bind_node(c, data, depth=depth + 1, item=item, counter=counter)
                if bound_child is not None:
                    child_nodes.append(bound_child)
            out[key] = child_nodes
        else:
            out[key] = _bind_value(value, data, item=item)
    if tone_override is not None:
        out["tone"] = tone_override
    return out


def _bind_repeat(node: dict, data: dict, *, depth: int, counter: _NodeCounter) -> dict:
    """Expand a repeat into a stack of template clones bound to each list element.

    A ``when`` rule that hides a template clone drops that clone from the expansion
    (its slot is removed from the stack's children entirely, matching the §5 rule).
    """
    items_bind = node.get("items") or {}
    steps = parse_path(items_bind["$bind"])
    items = _resolve_path(data, steps)
    if not isinstance(items, list):
        raise NIError("bind_type", "repeat.items must resolve to a list")
    max_n = int(node.get("max", _MAX_REPEAT_MAX))
    template = node["template"]
    children: list[dict] = []
    for entry in items[:max_n]:  # bounded by max_n <= _MAX_REPEAT_MAX
        bound_child = _bind_node(template, data, depth=depth + 1, item=entry, counter=counter)
        if bound_child is not None:
            children.append(bound_child)
    return {"type": "stack", "dir": "v", "gap": "sm", "children": children}


def _bind_value(value: object, data: dict, *, item: Any) -> object:
    """Resolve one leaf value: literal, $bind, or a string with {{path}} interpolation."""
    assert data is not None, "data required"
    if isinstance(value, dict) and set(value.keys()) == {"$bind"}:
        steps = parse_path(value["$bind"])
        return _resolve_bind(steps, data, item=item)
    if isinstance(value, str):
        return _interpolate_string(value, data, item=item)
    return value


def _resolve_bind(steps: list[tuple], data: dict, *, item: Any) -> object:
    """Follow a $bind path; ``item.<x>`` starts at the current repeat element."""
    assert steps, "steps required"
    root: Any = data
    if steps[0] == ("key", "item"):
        if item is None:
            raise NIError("bind_miss", "item.* outside a repeat")
        root = item
        steps = steps[1:]
        if not steps:
            return root
    return _resolve_path(root, steps)


def _apply_when(rules: list, data: dict, *, item: Any) -> dict:
    """Evaluate a content node's ``when`` rules in order (§5 Conditions).

    Returns ``{"hidden": True}`` on the first match with ``set.hidden=True`` (short-circuits;
    the caller drops the node). Otherwise ``{"tone": <token>}`` with the LAST matching
    tone (later rules override earlier ones — §5 "later tone wins").
    """
    assert isinstance(rules, list), "rules must be a list"
    assert isinstance(data, dict), "data required"
    result: dict = {}
    for rule in rules:  # bounded by _MAX_WHEN_RULES
        left = _resolve_when_operand(rule.get("left"), data, item=item)
        right = _resolve_when_operand(rule.get("right"), data, item=item)
        if _eval_when(left, rule.get("op"), right):
            set_body = rule.get("set") or {}
            if set_body.get("hidden") is True:
                return {"hidden": True}
            if "tone" in set_body:
                result["tone"] = set_body["tone"]
    return result


def _resolve_when_operand(value: object, data: dict, *, item: Any) -> object:
    """Resolve a when/alerts operand: $bind against outputs, otherwise pass the scalar."""
    if isinstance(value, dict) and "$bind" in value:
        steps = parse_path(value["$bind"])
        return _resolve_bind(steps, data, item=item)
    return value


def _eval_when(left: object, op: object, right: object) -> bool:
    """Evaluate one when/alerts comparison. Ordering ops require numbers on both sides
    (else NIError('when_type')); eq/ne compare scalars strictly (no coercion).

    LOW#1 (audit 2026-09-09): booleans are a subclass of int, so ``True == 1`` is
    natively True — refused explicitly here (eq/ne bool-vs-number => unequal). A NaN
    operand on either side is treated as unequal (eq => False, ne => True) for both
    ops; Python's native ``!=`` on NaN already returns True, but the explicit guard
    documents the contract.
    """
    if op in _ORDER_OPS:
        if not _is_finite_number(left) or not _is_finite_number(right):
            raise NIError("when_type", f"{op} requires numbers on both sides")
        lf, rf = float(left), float(right)  # type: ignore[arg-type]
        if op == "lt":
            return lf < rf
        if op == "le":
            return lf <= rf
        if op == "gt":
            return lf > rf
        return lf >= rf
    left_bool = isinstance(left, bool)
    right_bool = isinstance(right, bool)
    left_num = isinstance(left, (int, float)) and not left_bool
    right_num = isinstance(right, (int, float)) and not right_bool
    if (left_bool and right_num) or (left_num and right_bool):
        return op == "ne"
    if left_num and not math.isfinite(float(left)):
        return op == "ne"
    if right_num and not math.isfinite(float(right)):
        return op == "ne"
    if op == "eq":
        return left == right
    return left != right


def _is_finite_number(x: object) -> bool:
    """True when ``x`` is a finite int/float (bool refused; NaN/inf refused)."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    return math.isfinite(float(x))


def _interpolate_string(value: str, data: dict, *, item: Any) -> str:
    """Replace every ``{{path}}`` in ``value`` with the resolved data (rendered as text)."""
    assert isinstance(value, str), "value must be a string"
    assert data is not None, "data required"

    def _one(m: re.Match) -> str:
        token = m.group(1)
        if token.startswith("param:"):
            return m.group(0)  # spec-level substitution has already happened
        steps = parse_path(token)
        resolved = _resolve_bind(steps, data, item=item)
        return str(resolved)

    out = _BIND_INTERP.sub(_one, value)
    if len(out) > _MAX_TEXT_CHARS:
        raise NIError("bind_text_too_long", f"{len(out)} chars (max {_MAX_TEXT_CHARS})")
    return out


# --- data contract (pure) -------------------------------------------------

def capture_contract(outputs: dict) -> dict:
    """Return the {"shape": {...}} fingerprint captured at commissioning (§7).

    ``shape`` maps every top-level output to its type name; for a list of dicts, the
    first element's fields land under ``<name>[].<key>``. Bounds are not invented in v1
    (the operator/reviewer edits them into the stored contract later).
    """
    assert isinstance(outputs, dict), "outputs must be a dict"
    shape: dict[str, str] = {}
    for name, value in outputs.items():  # bounded by extract paths cap
        shape[name] = _type_of(value)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            for key, sub in value[0].items():  # bounded by first-row keys
                shape[f"{name}[].{key}"] = _type_of(sub)
    return {"shape": shape}


def _type_of(value: object) -> str:
    """Type fingerprint token (matches capture + check)."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "null"


def check_contract(contract: dict, outputs: dict) -> tuple[bool, str]:
    """Verify ``outputs`` against ``contract``. Returns (ok, violation-string).

    Every shape path must resolve, its type must match, and any bounds (min/max) must
    hold (only checked when the value is numeric and the bound is finite). A single
    violation short-circuits — the FIRST failure is what the user sees.
    """
    assert isinstance(contract, dict), "contract must be a dict"
    assert isinstance(outputs, dict), "outputs must be a dict"
    shape = contract.get("shape") or {}
    bounds = contract.get("bounds") or {}
    for path, expected in shape.items():  # bounded by shape size
        ok, actual = _resolve_shape_path(path, outputs)
        if not ok:
            return False, f"shape.{path}: missing"
        if _type_of(actual) != expected:
            return False, f"shape.{path}: expected {expected}, got {_type_of(actual)}"
    for path, spec in bounds.items():  # bounded by bounds size
        if not isinstance(spec, dict):
            continue
        ok, actual = _resolve_shape_path(path, outputs)
        if not ok:
            return False, f"bounds.{path}: missing"
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            continue
        if "min" in spec and float(actual) < float(spec["min"]):
            return False, f"bounds.{path}: {actual} < min {spec['min']}"
        if "max" in spec and float(actual) > float(spec["max"]):
            return False, f"bounds.{path}: {actual} > max {spec['max']}"
    return True, ""


def _resolve_shape_path(path: str, outputs: dict) -> tuple[bool, object]:
    """Resolve a shape path (``name`` or ``name[].key``) — best-effort, never raises."""
    assert isinstance(path, str) and path, "path required"
    if "[]." in path:
        name, _, key = path.partition("[].")
        target = outputs.get(name)
        if not isinstance(target, list) or not target or not isinstance(target[0], dict):
            return False, None
        if key not in target[0]:
            return False, None
        return True, target[0][key]
    if path not in outputs:
        return False, None
    return True, outputs[path]


# --- credential storage (host-bound) --------------------------------------

def put_credential(secrets_store, item_id: str, name: str, value: str, host: str) -> str:
    """Store a secret value under ``ni:<item_id>:<name>`` bound to ``host``.

    Body is JSON {"value":..., "host":...}: the engine refuses to attach the secret when
    a fetch host differs, so a stolen or misrouted credential cannot cross hosts. ``host``
    is IDNA-normalized + lowercased at store time so a mixed-case or IDN spelling matches
    the URL's parsed hostname later (urlparse().hostname is already lowercase). Returns
    the store key name.
    """
    assert secrets_store is not None, "secrets store required"
    assert item_id and name and value, "item_id + name + value required"
    assert isinstance(host, str) and host, "host required (empty = no binding)"
    normalized = _normalize_host(host)
    key = f"ni:{item_id}:{name}"
    secrets_store.put(key, json.dumps({"value": value, "host": normalized}))
    return key


def _normalize_host(host: str) -> str:
    """Lowercase + IDNA-encode ``host`` so binding matches urlparse().hostname (K3)."""
    assert isinstance(host, str) and host, "host required"
    low = host.strip().lower()
    try:
        return low.encode("idna").decode("ascii")
    except UnicodeError:
        # Already ASCII or an unencodable label: keep the lowercased form so a
        # non-IDN literal still stores as-is (matches urlparse().hostname behavior).
        return low


def _load_credential(secrets_store, key: str, expected_host: str,
                     *, item_id: str, request_scheme: str) -> str:
    """Return the stored value iff its bound host matches ``expected_host``; else NIError.

    ``item_id`` + ``key`` are checked together (K2): the loader is the one place the full
    ``ni:{item_id}:name`` prefix is enforced, so a spec whose $secret ref points at another
    item's namespace refuses cleanly. ``request_scheme`` must be ``https`` (K3): a secret
    never rides an http request — the transport would leak it in cleartext.
    """
    assert secrets_store is not None, "secrets store required"
    assert key and expected_host, "key + host required"
    assert item_id, "item_id required for scoped-prefix check"
    expected_prefix = f"ni:{item_id}:"
    if not key.startswith(expected_prefix):
        raise NIError("secret_not_scoped", "credential key outside this item's namespace")
    if request_scheme.lower() != "https":
        raise NIError("secret_requires_https", "a secret may not ride http requests")
    raw = secrets_store.get(key)
    if raw is None:
        raise NIError("secret_missing", key)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        raise NIError("secret_malformed", "not JSON") from None
    if not isinstance(body, dict) or "value" not in body or "host" not in body:
        raise NIError("secret_malformed", "bad shape")
    if body["host"] != expected_host:
        raise NIError("secret_host_mismatch", "credential bound to another host")
    return str(body["value"])


# --- NIStore --------------------------------------------------------------

class NIStore:
    """Encrypted NI item store. The spec is sealed (source URLs and instructions read
    as personally-telling as the data they fetch); operational cadence columns (state,
    enabled, interval_minutes, last_checked, last_status, consecutive_failures) stay
    plaintext so the engine tick finds due rows without the master key."""

    def __init__(self, conn, master_key: bytes) -> None:
        assert conn is not None and master_key, "conn + key required"
        self._conn = conn
        self._aes = AESGCM(master_key)

    @property
    def conn(self):
        """The underlying cursor (turn-cursor invariant: stores for one turn share ONE)."""
        return self._conn

    def add_item(self, spec: dict, preview_payload: dict, *,
                 origin: str = "user") -> str:
        """Create a new item in ``draft`` with a validated preview snapshot.

        ``spec`` is validated first (whole shape), then sealed; the preview payload is
        bound against the scene up-front so the very first snapshot proves the scene
        renders. ``origin`` seeds the initial ni_revisions row.
        """
        assert isinstance(spec, dict), "spec must be a dict"
        assert isinstance(preview_payload, dict), "preview payload must be a dict"
        validated = validate_spec(spec)
        if origin not in _REVISION_ORIGINS:
            raise ValueError(f"origin must be one of {sorted(_REVISION_ORIGINS)}")
        # H3 (audit 2026-09-09): seed empty history so a scene whose spark or delta_prev
        # binds to ``history.<name>`` can render the preview (mirrors C1 first-run seeding).
        bound = bind_scene(validated["scene"], preview_payload,
                           history=_seed_history(validated))  # proves the preview renders
        assert isinstance(bound, dict), "bind_scene must return a dict"
        count = self._conn.execute("SELECT COUNT(*) FROM ni_items;").fetchone()[0]
        if int(count) >= _MAX_ITEMS:
            raise ValueError(f"item limit reached ({_MAX_ITEMS})")
        item_id = str(uuid.uuid4())
        interval = self._clamp_interval(validated)
        nonce, ciphertext = self._seal_item(item_id, validated)
        self._conn.execute(
            "INSERT INTO ni_items (id, enabled, state, interval_minutes, "
            "consecutive_failures, position, spec_rev, nonce, ciphertext) "
            "VALUES (?, ?, ?, ?, 0, ?, 1, ?, ?);",
            [item_id, True, "draft", interval, count, nonce, ciphertext],
        )
        # Store the BOUND preview scene (data inlined), matching latest/last_good — §10
        # says the board returns the "decrypted bound payload"; the preview slot renders
        # to the client through the same reader as a real run's output.
        self.write_snapshot(item_id, "preview", bound, ok=True)
        self._write_revision(item_id, 1, validated, origin)
        return item_id

    def _clamp_interval(self, spec: dict) -> int:
        """Interval floor per §2 (>=1 minute); never an error, silently clamped."""
        raw = spec.get("interval_minutes")
        if not isinstance(raw, int) or isinstance(raw, bool):
            return 60
        return max(_INTERVAL_FLOOR, int(raw))

    def get_item(self, item_id: str) -> dict | None:
        assert item_id, "item id required"
        row = self._conn.execute(
            "SELECT id, enabled, state, interval_minutes, last_checked, last_status, "
            "consecutive_failures, first_failure_at, position, spec_rev, nonce, ciphertext, "
            "created_at, updated_at FROM ni_items WHERE id = ?;",
            [item_id],
        ).fetchone()
        return None if row is None else self._row(row)

    def list_items(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, enabled, state, interval_minutes, last_checked, last_status, "
            "consecutive_failures, first_failure_at, position, spec_rev, nonce, ciphertext, "
            "created_at, updated_at FROM ni_items ORDER BY position ASC, created_at ASC LIMIT ?;",
            [_MAX_ITEMS],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        return [self._row(r) for r in rows]  # bounded by _MAX_ITEMS

    def update_spec(self, item_id: str, new_spec: dict, *, origin: str = "user") -> int:
        """Validate + reseal + bump spec_rev; append a revision row and prune to 10.

        Any update ALWAYS strips ``_c2_ok`` and ``contract`` from the sealed spec (A3):
        both are system-attested at commissioning against a specific spec, and once the
        spec changes those attestations no longer describe the item. It also resets the
        streak (consecutive_failures + first_failure_at) so the failure counter measures
        the current spec's behavior, not the prior one's.
        """
        assert item_id, "item id required"
        assert isinstance(new_spec, dict), "spec must be a dict"
        with _SPEC_LOCK:
            current = self.get_item(item_id)
            if current is None:
                raise ValueError("item not found")
            if origin not in _REVISION_ORIGINS:
                raise ValueError(f"origin must be one of {sorted(_REVISION_ORIGINS)}")
            validated = validate_spec(new_spec)
            validated.pop("_c2_ok", None)
            validated["contract"] = None  # keep the key present so validators stay happy
            new_rev = int(current["spec_rev"]) + 1
            interval = self._clamp_interval(validated)
            nonce, ciphertext = self._seal_item(item_id, validated)
            self._conn.execute(
                "UPDATE ni_items SET nonce = ?, ciphertext = ?, spec_rev = ?, "
                "interval_minutes = ?, consecutive_failures = 0, "
                "first_failure_at = NULL, updated_at = now() WHERE id = ?;",
                [nonce, ciphertext, new_rev, interval, item_id],
            )
            self._write_revision(item_id, new_rev, validated, origin)
            self._prune_revisions(item_id)
            return new_rev

    def set_enabled(self, item_id: str, enabled: bool) -> None:
        assert item_id, "item id required"
        assert isinstance(enabled, bool), "enabled must be bool"
        self._conn.execute("UPDATE ni_items SET enabled = ? WHERE id = ?;",
                           [enabled, item_id])

    def set_state(self, item_id: str, state: str) -> None:
        assert item_id, "item id required"
        if state not in _STATES:
            raise ValueError(f"state must be one of {sorted(_STATES)}")
        self._conn.execute("UPDATE ni_items SET state = ? WHERE id = ?;", [state, item_id])

    def set_position(self, item_id: str, position: int) -> None:
        assert item_id, "item id required"
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise ValueError("position must be a non-negative int")
        self._conn.execute("UPDATE ni_items SET position = ? WHERE id = ?;",
                           [position, item_id])

    def delete(self, item_id: str) -> None:
        """Remove the item and cascade its snapshots/revisions/runs (no FK — code cascade)."""
        assert item_id, "item id required"
        self._conn.execute("DELETE FROM ni_snapshots WHERE item_id = ?;", [item_id])
        self._conn.execute("DELETE FROM ni_revisions WHERE item_id = ?;", [item_id])
        self._conn.execute("DELETE FROM ni_runs WHERE item_id = ?;", [item_id])
        self._conn.execute("DELETE FROM ni_items WHERE id = ?;", [item_id])

    def write_snapshot(self, item_id: str, slot: str, payload: dict, ok: bool) -> None:
        """Seal ``payload`` under (item_id, slot); upsert. ``ok`` is plaintext so the
        board picks the right slot without decrypting."""
        assert item_id, "item id required"
        if slot not in _SLOTS:
            raise ValueError(f"slot must be one of {sorted(_SLOTS)}")
        assert isinstance(payload, dict), "payload must be a dict"
        assert isinstance(ok, bool), "ok must be bool"
        nonce, ciphertext = self._seal_snapshot(item_id, slot, payload)
        self._conn.execute(
            "INSERT INTO ni_snapshots (item_id, slot, nonce, ciphertext, ok) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (item_id, slot) DO UPDATE SET "
            "nonce = excluded.nonce, ciphertext = excluded.ciphertext, "
            "ok = excluded.ok, created_at = now();",
            [item_id, slot, nonce, ciphertext, ok],
        )

    def read_snapshot(self, item_id: str, slot: str) -> dict | None:
        assert item_id, "item id required"
        if slot not in _SLOTS:
            raise ValueError(f"slot must be one of {sorted(_SLOTS)}")
        row = self._conn.execute(
            "SELECT nonce, ciphertext, ok, created_at FROM ni_snapshots "
            "WHERE item_id = ? AND slot = ?;",
            [item_id, slot],
        ).fetchone()
        if row is None:
            return None
        aad = f"ni_snapshot:{item_id}:{slot}".encode()
        body = json.loads(self._aes.decrypt(bytes(row[0]), bytes(row[1]), aad).decode("utf-8"))
        return {"payload": body, "ok": bool(row[2]), "created_at": str(row[3])}

    def record_run(self, item_id: str, status: str, *, duration_ms: int,
                   error: str | None, contract_ok: bool | None) -> None:
        """Append one ni_runs row; prune to 50/item. ``error`` must be a host-free class."""
        assert item_id and status, "item id + status required"
        assert isinstance(duration_ms, int) and duration_ms >= 0, "duration_ms >= 0"
        self._conn.execute(
            "INSERT INTO ni_runs (item_id, status, duration_ms, error, contract_ok) "
            "VALUES (?, ?, ?, ?, ?);",
            [item_id, status[:_MAX_STATUS], duration_ms, error, contract_ok],
        )
        self._prune_runs(item_id)

    def list_runs(self, item_id: str, limit: int = 50) -> list[dict]:
        assert item_id, "item id required"
        capped = min(max(int(limit), 1), _MAX_RUNS)
        rows = self._conn.execute(
            "SELECT ts, status, duration_ms, error, contract_ok FROM ni_runs "
            "WHERE item_id = ? ORDER BY ts DESC LIMIT ?;",
            [item_id, capped],
        ).fetchall()
        return [{"ts": str(r[0]), "status": str(r[1]), "duration_ms": int(r[2]),
                 "error": None if r[3] is None else str(r[3]),
                 "contract_ok": None if r[4] is None else bool(r[4])}
                for r in rows]  # bounded by capped

    def due_items(self) -> list[dict]:
        """§8 due query — enabled AND state NOT IN (draft, paused, broken), NULLS FIRST,
        oldest first. Effective interval (failing back-off, cap 24h) is computed here
        because it's an exponential formula: SQL would tangle for no benefit."""
        rows = self._conn.execute(
            "SELECT id, enabled, state, interval_minutes, last_checked, last_status, "
            "consecutive_failures, first_failure_at, position, spec_rev, nonce, ciphertext, "
            "created_at, updated_at FROM ni_items "
            "WHERE enabled AND state NOT IN ('draft', 'paused', 'broken') "
            "ORDER BY last_checked ASC NULLS FIRST LIMIT ?;",
            [_MAX_ITEMS],
        ).fetchall()
        now = datetime.now(UTC)
        out: list[dict] = []
        for r in rows:  # bounded by _MAX_ITEMS
            item = self._row(r)
            if _is_due(item, now):
                out.append(item)
            if len(out) >= _MAX_ITEMS_PER_PASS:
                break
        return out

    def clear_last_checked(self, item_id: str) -> None:
        """Reset ``last_checked`` to NULL so ``due_items`` picks the item up on the next tick.

        Callers: the ``run_ni_item_now`` agent tool + POST /api/ni/items/{id}/run, both
        of which want "refresh at next opportunity" without doing a real fetch inline.
        """
        assert item_id, "item id required"
        self._conn.execute(
            "UPDATE ni_items SET last_checked = NULL WHERE id = ?;", [item_id]
        )

    def mark_checked(self, item_id: str, status: str) -> None:
        """EVERY attempt records a host-free status (feeds law); the tick's own backoff."""
        assert item_id, "item id required"
        assert isinstance(status, str), "status must be a string"
        self._conn.execute(
            "UPDATE ni_items SET last_checked = now(), last_status = ? WHERE id = ?;",
            [status[:_MAX_STATUS], item_id],
        )

    def bump_failure(self, item_id: str, status: str) -> int:
        """Advance consecutive_failures by one and stamp the status; return the new count.

        Also sets ``first_failure_at = now()`` on the FIRST failure of a streak (F): the
        broken escalation measures elapsed time from the streak start, not the item's
        birthday, so a long-lived healthy item that begins failing today can't be classed
        broken immediately just because it's older than the 7-day threshold.
        """
        assert item_id and isinstance(status, str), "item id + status required"
        self._conn.execute(
            "UPDATE ni_items SET consecutive_failures = consecutive_failures + 1, "
            "first_failure_at = COALESCE(first_failure_at, now()), "
            "last_checked = now(), last_status = ? WHERE id = ?;",
            [status[:_MAX_STATUS], item_id],
        )
        row = self._conn.execute(
            "SELECT consecutive_failures FROM ni_items WHERE id = ?;", [item_id]
        ).fetchone()
        return 0 if row is None else int(row[0])

    def clear_failures(self, item_id: str, status: str) -> None:
        """Reset the failure counter on a good run (state transitions layer atop this)."""
        assert item_id and isinstance(status, str), "item id + status required"
        self._conn.execute(
            "UPDATE ni_items SET consecutive_failures = 0, first_failure_at = NULL, "
            "last_checked = now(), last_status = ? WHERE id = ?;",
            [status[:_MAX_STATUS], item_id],
        )

    def get_first_failure_at(self, item_id: str) -> datetime | None:
        """Return the streak marker (F), or None when no active streak exists."""
        assert item_id, "item id required"
        row = self._conn.execute(
            "SELECT first_failure_at FROM ni_items WHERE id = ?;", [item_id]
        ).fetchone()
        return None if row is None or row[0] is None else _to_utc(row[0])

    def commission(self, item_id: str) -> None:
        """draft -> commissioning: also clears any streak marker (A2). Route helper (B)."""
        assert item_id, "item id required"
        with _SPEC_LOCK:
            self._conn.execute(
                "UPDATE ni_items SET state = 'commissioning', "
                "consecutive_failures = 0, first_failure_at = NULL, "
                "updated_at = now() WHERE id = ?;",
                [item_id],
            )

    def get_created_at(self, item_id: str) -> datetime | None:
        assert item_id, "item id required"
        row = self._conn.execute(
            "SELECT created_at FROM ni_items WHERE id = ?;", [item_id]
        ).fetchone()
        return None if row is None or row[0] is None else _to_utc(row[0])

    def record_validation(self, item_id: str, ok: bool, note: str = "") -> None:
        """C2 verdict: user answers "Looks right" / "Something's wrong" on the C1 result.

        ok=True stamps ``_c2_ok=true`` inside the sealed spec (persisted across engine
        restarts) so the next real run can transition to ``live``; ok=False sends the
        item back to ``draft`` for the agent to redraft (the note is not persisted in v1
        — the operator sees it in the UI at verdict time).

        Refuses (ValueError, mapped to 409 by the route) unless the item is currently
        ``commissioning`` (C1 integrity): a verdict against any other state — live,
        broken, draft — has no C1 output to endorse and would silently corrupt the
        state machine.
        """
        assert item_id, "item id required"
        assert isinstance(ok, bool), "ok must be bool"
        assert isinstance(note, str), "note must be a string"
        with _SPEC_LOCK:
            current = self.get_item(item_id)
            if current is None:
                raise ValueError("item not found")
            if current["state"] != "commissioning":
                raise ValueError(
                    f"record_validation refused: state={current['state']!r} (expected 'commissioning')"
                )
            spec = current["spec"]
            assert isinstance(spec, dict), "spec must decrypt to a dict"
            if not ok:
                self.set_state(item_id, "draft")
                return
            spec["_c2_ok"] = True
            nonce, ciphertext = self._seal_item(item_id, spec)
            self._conn.execute(
                "UPDATE ni_items SET nonce = ?, ciphertext = ?, updated_at = now() WHERE id = ?;",
                [nonce, ciphertext, item_id],
            )

    def _write_revision(self, item_id: str, rev: int, spec: dict, origin: str) -> None:
        """Seal one revision under ``ni_revision:<item_id>:<rev>``."""
        assert item_id and rev >= 1, "item id + positive rev required"
        assert origin in _REVISION_ORIGINS, "origin already validated"
        nonce = os.urandom(_NONCE_BYTES)
        aad = f"ni_revision:{item_id}:{rev}".encode()
        ciphertext = self._aes.encrypt(nonce, json.dumps(spec).encode("utf-8"), aad)
        self._conn.execute(
            "INSERT INTO ni_revisions (item_id, rev, nonce, ciphertext, origin) "
            "VALUES (?, ?, ?, ?, ?);",
            [item_id, rev, nonce, ciphertext, origin],
        )

    def _prune_revisions(self, item_id: str) -> None:
        """Keep the newest _MAX_REVISIONS revisions per item (in code, no FK)."""
        assert item_id, "item id required"
        row = self._conn.execute(
            "SELECT rev FROM ni_revisions WHERE item_id = ? ORDER BY rev DESC LIMIT 1 OFFSET ?;",
            [item_id, _MAX_REVISIONS],
        ).fetchone()
        if row is None:
            return
        self._conn.execute(
            "DELETE FROM ni_revisions WHERE item_id = ? AND rev <= ?;",
            [item_id, int(row[0])],
        )

    def _prune_runs(self, item_id: str) -> None:
        """Keep the newest _MAX_RUNS runs per item (rowid-ordered by insertion)."""
        assert item_id, "item id required"
        row = self._conn.execute(
            "SELECT ts FROM ni_runs WHERE item_id = ? ORDER BY ts DESC LIMIT 1 OFFSET ?;",
            [item_id, _MAX_RUNS],
        ).fetchone()
        if row is None:
            return
        self._conn.execute(
            "DELETE FROM ni_runs WHERE item_id = ? AND ts <= ?;",
            [item_id, row[0]],
        )

    def _seal_item(self, item_id: str, spec: dict) -> tuple[bytes, bytes]:
        assert item_id and isinstance(spec, dict), "item + spec required"
        nonce = os.urandom(_NONCE_BYTES)
        aad = f"ni_item:{item_id}".encode()
        return nonce, self._aes.encrypt(nonce, json.dumps(spec).encode("utf-8"), aad)

    def _seal_snapshot(self, item_id: str, slot: str, payload: dict) -> tuple[bytes, bytes]:
        assert item_id and slot, "item + slot required"
        nonce = os.urandom(_NONCE_BYTES)
        aad = f"ni_snapshot:{item_id}:{slot}".encode()
        return nonce, self._aes.encrypt(nonce, json.dumps(payload).encode("utf-8"), aad)

    def _row(self, row: tuple) -> dict:
        item_id = str(row[0])
        aad = f"ni_item:{item_id}".encode()
        spec = json.loads(self._aes.decrypt(bytes(row[10]), bytes(row[11]), aad).decode("utf-8"))
        return {
            "id": item_id, "enabled": bool(row[1]), "state": str(row[2]),
            "interval_minutes": int(row[3]),
            "last_checked": None if row[4] is None else str(row[4]),
            "last_status": str(row[5] or ""),
            "consecutive_failures": int(row[6]),
            "first_failure_at": None if row[7] is None else _to_utc(row[7]),
            "position": int(row[8]),
            "spec_rev": int(row[9]), "spec": spec,
            "created_at": str(row[12]), "updated_at": str(row[13]),
        }


def _to_utc(value) -> datetime:
    """Coerce a DuckDB timestamp value to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


def effective_interval_minutes(base: int, failures: int) -> int:
    """Effective cadence (§6): base until failures>=3, then doubles per extra failure, cap 24h."""
    assert isinstance(base, int) and base >= _INTERVAL_FLOOR, "base >= floor"
    assert isinstance(failures, int) and failures >= 0, "failures >= 0"
    if failures < _FAILING_THRESHOLD:
        return base
    factor = 1 << max(0, failures - _FAILING_THRESHOLD)  # 1, 2, 4, 8, ...
    return min(base * factor, _INTERVAL_CEILING_MINUTES)


def _is_due(item: dict, now: datetime) -> bool:
    """True when the effective-interval has elapsed since ``last_checked`` (or NULL)."""
    assert isinstance(now, datetime) and now.tzinfo is not None, "now must be UTC-aware"
    last = item.get("last_checked")
    interval = effective_interval_minutes(item["interval_minutes"], item["consecutive_failures"])
    if last is None:
        return True
    elapsed = (now - _to_utc(last)).total_seconds()
    return elapsed >= interval * 60


# --- engine ---------------------------------------------------------------

def tick(app, pass_budget_seconds: float = 20.0,
         breaker_open=None) -> dict:
    """Run due NI items — modeled line-for-line on feeds.tick.

    Bails when the vault is locked (sealed specs can't decrypt); owns a per-thread
    cursor (DuckDB cursors are not thread-safe); one bounded try/except per item; a
    wall-clock budget between items so a slow host can't eat the tick.

    ``breaker_open`` (I): a Callable returning True while the gateway breaker is
    suppressing model traffic. Model-source items are SKIPPED (they stay due — no
    ``mark_checked``, so the next tick with a healthy gateway picks them up); http_json
    and internal.schedule items are unaffected (they don't touch the gateway).
    Doc §8 already promises this behavior.

    Returns ``{"checked": int, "alerts": list, "broken": list}``: fired alerts (§12)
    and this-tick broken transitions (§6) are collected here for the scheduler's
    ``_auto_update_ni`` to post to the carrier row (§12 posts every alert + broken
    notice through the NI carrier).
    """
    assert app is not None, "app required"
    assert pass_budget_seconds > 0, "pass budget must be positive"
    key = getattr(app.state, "master_key", None)
    if key is None:
        return {"checked": 0, "alerts": [], "broken": []}  # locked — nothing can decrypt
    from . import (
        gateway as gateway_mod,  # lazy: keep gateway off ni's import graph edges
    )
    from .scheduler import ScheduleStore
    from .secrets import SecretStore

    cursor = app.state.db.cursor()
    checked = 0
    fired: list[dict] = []
    broken: list[dict] = []
    try:
        store = NIStore(cursor, key)
        secrets_store = SecretStore(cursor, key)
        schedules_store = ScheduleStore(cursor, key)
        started = time.monotonic()
        for item in store.due_items():  # bounded by _MAX_ITEMS_PER_PASS
            if time.monotonic() - started > pass_budget_seconds:
                break  # the rest stay due; next tick continues
            if breaker_open is not None and breaker_open():
                source_type = (item["spec"].get("source") or {}).get("type")
                if source_type == "model":
                    continue  # skip; item stays due for the next tick
            prior_state = item["state"]
            try:
                result = run_item(store, item["id"], gateway_mod=gateway_mod,
                                  secrets_store=secrets_store, schedules_store=schedules_store)
                if isinstance(result, dict):
                    fired.extend(result.get("alerts") or [])
            except NIError as exc:
                store.mark_checked(item["id"], exc.kind[:_MAX_STATUS])
            except Exception:  # last-resort net: one bad item must not stop the pass
                log.warning("ni item run failed with unexpected error: item=%s", item["id"])
                store.mark_checked(item["id"], "internal")
            _collect_broken_transition(store, item["id"], prior_state, item, broken)
            checked += 1
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    return {"checked": checked, "alerts": fired, "broken": broken}


def _collect_broken_transition(store: NIStore, item_id: str, prior_state: str,
                               prior_item: dict, broken: list[dict]) -> None:
    """Append a §12 broken notice for an item that transitioned to broken THIS tick."""
    assert store is not None and item_id and prior_item is not None, "args required"
    after = store.get_item(item_id)
    if after is None or after["state"] != "broken" or prior_state == "broken":
        return
    title = str(prior_item["spec"].get("title") or "")
    broken.append({"item_id": item_id, "title": title, "broken": True})


def run_item(store: NIStore, item_id: str, *, gateway_mod, secrets_store,
             schedules_store=None) -> dict:
    """Execute one item end-to-end and apply the state-machine transition.

    Substitute params -> fetch -> pipeline -> optional contract check -> bind -> write
    snapshots (latest always; last_good on success) -> record_run -> transition. Raises
    NIError on a failure (caller records mark_checked + last_status).

    ANY exception from the pipeline / bind / finalize path is wrapped as NIError inside
    the try/except so bookkeeping (mark_checked + ni_runs row + failure bump + latest
    snapshot with ok=False) fires on EVERY failure path (G2/G3 — the prior code let a
    non-NIError from finalize skip the run row entirely).
    """
    assert store is not None and item_id, "store + id required"
    assert gateway_mod is not None and secrets_store is not None, "gateway + secrets required"
    item = store.get_item(item_id)
    if item is None:
        raise NIError("item_missing")
    started = time.monotonic()
    history: dict = {}
    try:
        # History (§11) is loaded ONCE per run, PRE-append: the binder + delta_prev see
        # the last completed run's series so a delta compares against the previous
        # snapshot, not this one. Inside the try so a corrupt slot routes through
        # ``_handle_failure`` instead of leaking a raw exception past bookkeeping.
        history = _load_history_series(store, item_id, item["spec"])
        spec = substitute_params(item["spec"])
        payload = _fetch_source(spec, item_id, gateway_mod, secrets_store,
                                schedules_store, store)
        outputs = run_pipeline(spec.get("pipeline") or [], payload, history=history)
    except NIError as exc:
        _handle_failure(store, item, exc, started)
        raise
    except Exception as exc:
        wrapped = NIError("internal", exc.__class__.__name__)
        _handle_failure(store, item, wrapped, started)
        raise wrapped from None
    # Finalize under the same net: bind / contract / snapshot writes can raise types the
    # prior code didn't wrap (a raw ValueError from a serialize step used to skip the
    # ni_runs row entirely — audit finding G).
    try:
        return _finalize_run(store, item, spec, outputs, started, history=history)
    except NIError:
        raise
    except Exception as exc:
        wrapped = NIError("internal", exc.__class__.__name__)
        _handle_failure(store, item, wrapped, started)
        raise wrapped from None


def _finalize_run(store: NIStore, item: dict, spec: dict, outputs: dict,
                  started: float, *, history: dict) -> dict:
    """Contract-check (if applicable), bind, append history, write snapshots, record
    run + transition, THEN evaluate alerts (order matters — see M1a below).

    Alerts (§12) and history append (§11) both live inside the "successful run" path:
    a bind or contract failure short-circuits before either. An ``alert_bind`` or
    ``history_type`` failure raises like a bind failure — bookkeeping records the
    error and ``last_good`` keeps rendering.

    M1a (audit 2026-09-09): alerts run LAST so ``alert_state`` never commits when a
    preceding step (history append, snapshot write, record_run, transition) fails.
    Previously ``_process_alerts`` ran BEFORE ``_append_history_series``, so a
    ``history_type`` failure could leave ``active=true`` in ``alert_state`` while the
    run itself failed — the next successful run would then skip the edge and never
    re-fire. Order is now: contract → bind/enforce → history append → snapshots →
    record_run → transition → alerts. ``_process_alerts`` writes its snapshot LAST,
    so an internal exception also leaves ``alert_state`` untouched.
    """
    assert isinstance(history, dict), "history required (pre-append)"
    contract = spec.get("contract")
    state = item["state"]
    contract_ok: bool | None = None
    # C3 (commissioning + _c2_ok + contract already captured): the run must satisfy the
    # captured contract before we can move to live. A violation = a run failure, and
    # ``_transition_on_failure`` keeps the item at commissioning per §6.
    check_contract_now = contract is not None and (
        state in ("live", "degraded", "failing")
        or (state == "commissioning" and spec.get("_c2_ok") is True)
    )
    if check_contract_now:
        ok, violation = check_contract(contract, outputs)
        contract_ok = ok
        if not ok:
            exc = NIError("contract_violation", violation)
            _handle_failure(store, item, exc, started, contract_ok=False)
            raise exc
    try:
        bound = bind_scene(spec["scene"], outputs, history=history)
        _enforce_bind_types(spec["scene"], bound)
        _enforce_payload_size(bound)
        _append_history_series(store, item, outputs, history)
    except NIError as exc:
        _handle_failure(store, item, exc, started, contract_ok=contract_ok)
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    store.write_snapshot(item["id"], "latest", bound, ok=True)
    store.write_snapshot(item["id"], "last_good", bound, ok=True)
    store.clear_failures(item["id"], "ok")
    store.record_run(item["id"], "ok", duration_ms=duration_ms, error=None,
                     contract_ok=contract_ok)
    _transition_on_success(store, item, spec, outputs)
    try:
        fired = _process_alerts(store, item, outputs)
    except NIError as exc:
        _handle_failure(store, item, exc, started, contract_ok=contract_ok)
        raise
    return {"status": "ok", "duration_ms": duration_ms, "alerts": fired}


def _seed_history(spec: dict) -> dict:
    """Return ``{name: []}`` for every tracked history series in ``spec`` (§11).

    Shared between ``_load_history_series`` (empty-slot seeding at run time) and the
    tool + store preview-binding paths (H3, audit 2026-09-09), so a spark or
    ``delta_prev`` bound to ``history.<name>`` resolves against a dummy preview
    payload — matching the C1 first-run seeding contract in §11.
    """
    assert isinstance(spec, dict), "spec must be a dict"
    tracked = (spec.get("history") or {}).get("track") or {}
    return {name: [] for name in tracked}


def _load_history_series(store: NIStore, item_id: str, spec: dict | None = None) -> dict:
    """Read the sealed ``history`` slot; empty dict when the slot is absent (first run).

    Tracked series that have no points yet are seeded as empty lists so a spark bound
    to ``history.<name>`` binds to ``[]`` on the very first run (C1) instead of dying
    with ``extract_miss`` — an item charting its own history must be able to commission.

    LOW#2 (audit 2026-09-09): a decrypt/decode failure (corrupt slot) raises
    ``NIError('history_slot')`` so the caller's ``_handle_failure`` records the run
    row + failure counter, instead of leaking a raw ``InvalidTag`` / JSON error past
    bookkeeping.
    """
    assert store is not None and item_id, "store + id required"
    try:
        snap = store.read_snapshot(item_id, "history")
    except Exception as exc:  # corrupt sealed slot — route through _handle_failure
        raise NIError("history_slot", exc.__class__.__name__) from None
    payload = (snap.get("payload") or {}) if snap is not None else {}
    series = payload if isinstance(payload, dict) else {}
    for name in _seed_history(spec or {}):
        series.setdefault(name, [])
    return series


def _append_history_series(store: NIStore, item: dict, outputs: dict,
                           prior: dict) -> None:
    """Append one point per tracked series to the sealed ``history`` slot (§11).

    The binder + delta_prev have ALREADY seen ``prior`` this run; the append writes the
    new point OUT so the NEXT completed run sees this run's number as ``last``. Non-
    numeric values raise NIError('history_type') — the run fails and last_good renders.
    Trims each series to the clamped ``max_points`` (≤500, default 100).

    M4 (audit 2026-09-09): series whose names are no longer in the current track are
    DROPPED on this append (rename/remove is destructive on next success). §11's
    retention promise covers the same names across rewinds — not the old name after
    it was renamed away.
    """
    assert store is not None and item is not None, "store + item required"
    assert isinstance(prior, dict), "prior must be a dict"
    hist_spec = item["spec"].get("history")
    if not hist_spec:
        return
    tracked = hist_spec.get("track") or {}
    raw_cap = hist_spec.get("max_points")
    if not isinstance(raw_cap, int) or isinstance(raw_cap, bool):
        raw_cap = _DEFAULT_HISTORY_POINTS
    max_points = max(1, min(int(raw_cap), _MAX_HISTORY_POINTS))
    now_iso = datetime.now(UTC).isoformat()
    new_slot: dict = {}
    for name, path in tracked.items():  # bounded by _MAX_HISTORY_SERIES
        steps = parse_path(path)
        try:
            value = _resolve_path(outputs, steps)
        except NIError as exc:
            raise NIError("history_type", f"{name}: {exc.kind}") from None
        if not _is_finite_number(value):
            raise NIError("history_type", f"{name} not a finite number")
        series = list(prior.get(name) or [])
        series.append({"t": now_iso, "v": float(value)})
        if len(series) > max_points:
            series = series[-max_points:]
        new_slot[name] = series
    store.write_snapshot(item["id"], "history", new_slot, ok=True)


def _process_alerts(store: NIStore, item: dict, outputs: dict) -> list[dict]:
    """§12 alerts: LIVE items only, edge-triggered, cooldown-aware.

    A rule's per-name state ``{active, last_fired}`` lives in the sealed ``alert_state``
    slot; on a false→true transition beyond cooldown we fire once, record ``last_fired``,
    and stay silent until the condition has been false at least once. Unresolvable operands
    (or numeric ops on non-numbers) raise NIError('alert_bind'), failing the run — alerts
    are part of the contract surface.
    """
    assert store is not None and item is not None, "store + item required"
    if item["state"] != "live":
        return []
    rules = item["spec"].get("alerts") or []
    if not rules:
        return []
    prior_state = _load_alert_state(store, item["id"])
    now = datetime.now(UTC)
    title = str(item["spec"].get("title") or "")
    new_rules: dict = {}
    fired: list[dict] = []
    for rule in rules:  # bounded by _MAX_ALERTS
        name = rule["name"]
        prior = prior_state.get(name) or {"active": False, "last_fired": None}
        try:
            left = _resolve_when_operand(rule.get("left"), outputs, item=None)
            right = _resolve_when_operand(rule.get("right"), outputs, item=None)
            truthy = _eval_when(left, rule.get("op"), right)
        except NIError as exc:
            raise NIError("alert_bind", f"{name}: {exc.kind}") from None
        cooldown = _clamp_alert_cooldown(rule.get("cooldown_minutes"))
        last_fired = None if prior.get("last_fired") is None else _to_utc(prior["last_fired"])
        should_fire = (
            bool(truthy) and not prior.get("active", False)
            and (last_fired is None or (now - last_fired) >= timedelta(minutes=cooldown))
        )
        if should_fire:
            message = _interpolate_alert_message(rule.get("message", ""), outputs, title)
            fired.append({"item_id": item["id"], "title": title, "message": message})
            last_fired = now
        new_rules[name] = {
            "active": bool(truthy),
            "last_fired": None if last_fired is None else last_fired.isoformat(),
        }
    store.write_snapshot(item["id"], "alert_state", {"rules": new_rules}, ok=True)
    return fired


def _load_alert_state(store: NIStore, item_id: str) -> dict:
    """Read the sealed ``alert_state`` slot; returns {name: {active, last_fired}}."""
    assert store is not None and item_id, "store + id required"
    snap = store.read_snapshot(item_id, "alert_state")
    if snap is None:
        return {}
    body = snap.get("payload") or {}
    rules = body.get("rules") if isinstance(body, dict) else None
    return rules if isinstance(rules, dict) else {}


def _clamp_alert_cooldown(raw: object) -> int:
    """Clamp per §12: default 60 when unset/malformed, floor at _MIN_ALERT_COOLDOWN."""
    if not isinstance(raw, int) or isinstance(raw, bool):
        return _DEFAULT_ALERT_COOLDOWN
    return max(_MIN_ALERT_COOLDOWN, int(raw))


def _interpolate_alert_message(template: str, outputs: dict, title: str) -> str:
    """{{title}} + {{path}} interpolation against outputs; cap at _MAX_ALERT_MESSAGE.

    Heading-forgery guard (H1, audit 2026-09-09): each RESOLVED value has its
    ``[\\r\\n]+`` runs collapsed to a single space (so a fetched string carrying
    ``\\n\\n### Scheduled Item Y ###...`` can never forge a chat-notice boundary),
    then the ASSEMBLED message is quoted with ``> `` when it starts with ``#`` —
    after the newline-collapse there is only one line, so the leading-``#`` check
    suffices (claudecli.py's ``_HEADING_FORGERY`` precedent, kept simple). The
    ``_MAX_ALERT_MESSAGE`` cap applies AFTER sanitization.
    """
    assert isinstance(template, str), "template must be a string"
    assert isinstance(outputs, dict) and isinstance(title, str), "outputs + title required"

    def _one(m: re.Match) -> str:
        token = m.group(1)
        if token == "title":
            return _ALERT_NEWLINE_RUN.sub(" ", title)
        try:
            steps = parse_path(token)
            resolved = _resolve_bind(steps, outputs, item=None)
        except NIError as exc:
            raise NIError("alert_bind", f"message {{{{ {token} }}}}: {exc.kind}") from None
        return _ALERT_NEWLINE_RUN.sub(" ", str(resolved))

    out = _BIND_INTERP.sub(_one, template)
    if out.startswith("#"):  # neutralize leading heading — quote it so the ### stays inert
        out = "> " + out
    if len(out) > _MAX_ALERT_MESSAGE:
        raise NIError("alert_bind",
                      f"message length {len(out)} exceeds {_MAX_ALERT_MESSAGE}")
    return out


def _enforce_bind_types(scene: dict, bound: dict) -> None:
    """Post-bind per-prop type check (H): text/chip string, number/bar numeric, repeat list.

    The scene validator constrained the source shape; this re-checks that the BINDER
    honored the same shape after substituting live data, so a bound payload never
    reaches write_snapshot with a wrong-typed leaf value (e.g. a $bind that resolved a
    dict into a text.value slot). Iterative walk (no recursion, POW10 #1)."""
    assert isinstance(scene, dict) and isinstance(bound, dict), "scene + bound required"
    pending: list[dict] = [bound]
    for _ in range(2 * _MAX_SCENE_NODES):  # bounded (POW10 #2); post-expansion cap is the real bound
        if not pending:
            return
        node = pending.pop()
        if not isinstance(node, dict):
            raise NIError("bind_type", "bound node must be a dict")
        ntype = node.get("type")
        if ntype in ("text", "chip"):
            value = node.get("value")
            if not isinstance(value, str) or len(value) > _MAX_TEXT_CHARS:
                raise NIError("bind_type", f"{ntype}.value must be a string <= {_MAX_TEXT_CHARS}")
        elif ntype in ("number", "bar"):
            value = node.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise NIError("bind_type", f"{ntype}.value must be a finite number")
            # math.isfinite matches the "finite number" contract (rejects NaN + inf) without
            # tripping the "compare with self" lint on the classic NaN pattern.
            if not math.isfinite(float(value)):
                raise NIError("bind_type", f"{ntype}.value must be finite")
        elif ntype == "spark":
            _enforce_spark_points(node)
        elif ntype == "gauge":
            _enforce_gauge_bounds(node)
        pending.extend(node.get("children") or [])  # bounded by _MAX_SCENE_NODES
    raise NIError("bind_type", "bound tree exceeded traversal bound")


def _enforce_spark_points(node: dict) -> None:
    """Post-bind check for §5 spark: points list ≤500 of FINITE numbers or {t,v}.

    H2 (audit 2026-09-09): ``json.loads`` accepts NaN/Infinity, and a single non-finite
    value landing in ``latest`` + ``last_good`` snaps ``GET /api/ni/board`` to 500 for
    EVERY item (Starlette re-encodes with ``allow_nan=False``). Enforce ``math.isfinite``
    on bare numbers and on ``v`` at bind time so a bad point never reaches the sealed
    snapshot. Also mirrors ``web/src/lib/ni/scene.ts checkSpark`` (M3): ``t`` must be a
    string when present, and ``{t,v}`` point objects reject extra keys — client-parity so
    a payload that binds server-side also renders on the client without a refusal.
    """
    assert isinstance(node, dict), "node must be a dict"
    points = node.get("points")
    if not isinstance(points, list):
        raise NIError("bind_type", "spark.points must resolve to a list")
    if len(points) > _MAX_SPARK_POINTS:
        raise NIError("bind_type",
                      f"spark.points has {len(points)} points (max {_MAX_SPARK_POINTS})")
    for p in points:  # bounded by _MAX_SPARK_POINTS
        if isinstance(p, (int, float)) and not isinstance(p, bool):
            if not math.isfinite(float(p)):
                raise NIError("bind_type", "spark.points value must be finite")
            continue
        if isinstance(p, dict):
            v = p.get("v")
            if not (isinstance(v, (int, float)) and not isinstance(v, bool)):
                raise NIError("bind_type",
                              "spark.points needs numbers or {t,v} with numeric v")
            if not math.isfinite(float(v)):
                raise NIError("bind_type", "spark.points.v must be finite")
            extras = set(p) - {"t", "v"}
            if extras:
                raise NIError("bind_type",
                              f"spark.points has extra keys {sorted(extras)}")
            if "t" in p and not isinstance(p["t"], str):
                raise NIError("bind_type", "spark.points.t must be a string when present")
            continue
        raise NIError("bind_type", "spark.points needs numbers or {t,v} with numeric v")


def _enforce_gauge_bounds(node: dict) -> None:
    """Post-bind check for §5 gauge: value/min/max are finite numbers, max > min.

    M3 (audit 2026-09-09): also cap ``gauge.label`` at ``_MAX_GAUGE_LABEL`` (200 chars)
    AFTER binding — ``{{path}}`` interpolation can grow the label past the 2000-char
    text cap the validator uses, and the client's ``checkGauge`` refuses > 200.
    """
    assert isinstance(node, dict), "node must be a dict"
    for key in ("value", "min", "max"):
        v = node.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
            raise NIError("bind_type", f"gauge.{key} must be a finite number")
    if float(node["max"]) <= float(node["min"]):
        raise NIError("bind_type", "gauge.max must be > min after binding")
    label = node.get("label")
    if isinstance(label, str) and len(label) > _MAX_GAUGE_LABEL:
        raise NIError("bind_type", f"gauge.label exceeds {_MAX_GAUGE_LABEL} chars")


def _enforce_payload_size(bound: dict) -> None:
    """Refuse a bound payload whose JSON serialization exceeds _MAX_PAYLOAD_BYTES (H)."""
    assert isinstance(bound, dict), "bound must be a dict"
    size = len(json.dumps(bound).encode("utf-8"))
    if size > _MAX_PAYLOAD_BYTES:
        raise NIError("payload_too_large",
                      f"{size} bytes (max {_MAX_PAYLOAD_BYTES})")


def _handle_failure(store: NIStore, item: dict, exc: NIError, started: float, *,
                    contract_ok: bool | None = None) -> None:
    """Bookkeeping for any failing outcome: run row + failure counter + state transition.

    Also writes a ``latest`` snapshot marked ``ok=False`` with an empty payload (G3) so
    the board's ``_pick_board_snapshot`` fallback (latest-if-ok else last_good) sees a
    real failure marker instead of silently keeping the previous latest — otherwise a
    degraded item shows its last-good in the "latest" slot until a manual /run fires.
    """
    assert store is not None and item is not None and exc is not None, "args required"
    duration_ms = int((time.monotonic() - started) * 1000)
    store.record_run(item["id"], "error", duration_ms=duration_ms, error=exc.kind,
                     contract_ok=contract_ok)
    try:
        store.write_snapshot(item["id"], "latest", {}, ok=False)
    except Exception:  # bookkeeping must never mask the original failure
        log.warning("ni latest-snapshot failure marker skipped: item=%s", item["id"])
    new_count = store.bump_failure(item["id"], exc.kind)
    _transition_on_failure(store, item, exc, new_count)


def _transition_on_success(store: NIStore, item: dict, spec: dict, outputs: dict) -> None:
    """§6 success transitions: commissioning->live (C3 after C2 ok, contract satisfied);
    C1 contract capture with one-more-clean-run gate; degraded/failing/live -> live.

    C (audit): on the first C1 pass with ``_c2_ok`` already true, if the sealed spec has
    NO contract yet, capture it now and STAY in commissioning (require one more clean
    run to reach live). If a contract IS present, the pre-bind check in ``_finalize_run``
    already verified it — a pass here moves the item to live.
    """
    assert store is not None and item and spec, "args required"
    state = item["state"]
    if state == "commissioning":
        if spec.get("_c2_ok") is True and item["spec"].get("contract") is not None:
            # C3: contract already captured + verified pre-bind. Move to live.
            store.set_state(item["id"], "live")
            return
        if spec.get("_c2_ok") is True and item["spec"].get("contract") is None:
            # First clean run AFTER C2 verdict — capture the contract and require ONE MORE
            # clean run (with the contract check active) before promoting to live.
            _reseal_capture_contract(store, item, outputs)
            return
        # Pre-C2 (C1): capture the contract on first successful commissioning run;
        # user's C2 verdict still needed before we consider promotion.
        _reseal_capture_contract(store, item, outputs)
        return
    if state in ("live", "degraded", "failing"):
        store.set_state(item["id"], "live")


def _reseal_capture_contract(store: NIStore, item: dict, outputs: dict) -> None:
    """Capture and re-seal the contract WITHOUT bumping spec_rev (system-written)."""
    assert store is not None and item and outputs is not None, "args required"
    contract = capture_contract(outputs)
    with _SPEC_LOCK:
        # Re-read under the lock: another writer could have amended the spec between
        # run_item's initial fetch and now (rev bump + strip _c2_ok on update_spec).
        fresh = store.get_item(item["id"])
        if fresh is None:
            return
        updated = dict(fresh["spec"])
        updated["contract"] = contract
        nonce, ciphertext = store._seal_item(item["id"], updated)
        store.conn.execute(
            "UPDATE ni_items SET nonce = ?, ciphertext = ?, updated_at = now() WHERE id = ?;",
            [nonce, ciphertext, item["id"]],
        )


def _transition_on_failure(store: NIStore, item: dict, exc: NIError, count: int) -> None:
    """§6 failure transitions: degraded -> failing after threshold; broken on the
    escalation rule (8 failures across >=7 days FROM THE STREAK START, F) or a permanent
    refusal.
    """
    assert store is not None and item is not None and exc is not None, "args required"
    if exc.kind == "secret_host_mismatch":
        # A credential host mismatch is a permanent refusal — no schedule of retries
        # will ever resolve it (per §6). Escalate straight to broken.
        store.set_state(item["id"], "broken")
        return
    if count >= _BROKEN_FAILURE_COUNT:
        first = store.get_first_failure_at(item["id"])
        now = datetime.now(UTC)
        if first is not None and (now - first) >= timedelta(days=_BROKEN_MIN_DAYS):
            store.set_state(item["id"], "broken")
            return
    if item["state"] == "commissioning":
        return  # C1 failure stays commissioning per §6 (agent redrafts)
    if count >= _FAILING_THRESHOLD:
        store.set_state(item["id"], "failing")
    else:
        store.set_state(item["id"], "degraded")


def _fetch_source(spec: dict, item_id: str, gateway_mod, secrets_store,
                  schedules_store, store: NIStore) -> dict:
    """Dispatch by source type — each returns the payload the pipeline consumes."""
    assert isinstance(spec, dict) and item_id, "spec + id required"
    assert store is not None, "store required (model routes need its cursor)"
    source = spec.get("source") or {}
    stype = source.get("type")
    if stype == "http_json":
        return _fetch_http_json(source, item_id, secrets_store)
    if stype == "model":
        return _fetch_model(spec, source, gateway_mod, store)
    if stype == "internal.schedule":
        return _fetch_internal_schedule(source, schedules_store)
    raise NIError("source_bad_type", str(stype))


def _fetch_http_json(source: dict, item_id: str, secrets_store) -> dict:
    """Guarded JSON fetch; headers with ``$secret`` are host-bound at storage time.

    Redirects are refused whenever ANY header is attached (E): auth headers must never
    re-send to a rewritten host, and a hostile server could otherwise 302 to itself and
    harvest the credential. When no headers are attached we keep the default redirect
    following (parity with feed/vault fetchers).
    """
    from . import netguard  # lazy: keep netguard off ni's import graph edges

    url = source["url"]
    parsed = urlparse(url)
    host = parsed.hostname or ""
    scheme = (parsed.scheme or "").lower()
    if not host:
        raise NIError("source_bad_url", "no host")
    resolved_headers: dict[str, str] = {}
    for name, value in (source.get("headers") or {}).items():  # bounded by _MAX_HEADERS
        if isinstance(value, dict) and "$secret" in value:
            resolved_headers[name] = _load_credential(
                secrets_store, value["$secret"], host,
                item_id=item_id, request_scheme=scheme,
            )
        elif isinstance(value, str):
            resolved_headers[name] = value
    has_headers = bool(resolved_headers)
    try:
        return netguard.safe_fetch_json(
            url, headers=resolved_headers or None,
            allow_redirects=not has_headers,  # E: refuse hop when carrying any header
        )
    except netguard.FetchError as exc:
        raise NIError("fetch_failed", exc.__class__.__name__) from None


def _fetch_model(spec: dict, source: dict, gateway_mod, store: NIStore) -> dict:
    """Run one chat completion on the ``ni`` route (or item override); return {'text': ...}.

    ``gateway_mod.load_routes`` requires the store's cursor (B): the previous
    ``load_routes(None)`` tripped the module's own ``conn is not None`` assertion.
    """
    assert isinstance(spec, dict) and isinstance(source, dict), "spec + source required"
    assert store is not None, "store required to read model routes"
    routes: dict = {}
    if hasattr(gateway_mod, "load_routes"):
        routes = gateway_mod.load_routes(store.conn)
    override = spec.get("model")
    model = override or gateway_mod.resolve_model("ni", routes) or gateway_mod.resolve_model("chat", routes)
    if not model:
        raise NIError("model_unrouted")
    if gateway_mod.is_local(model) and not gateway_mod.local_available():
        # Yield to a foreground chat — the tick's per-item budget makes this cheap to retry.
        raise NIError("model_busy")
    try:
        data = gateway_mod.chat([{"role": "user", "content": source["instruction"]}], model)
    except gateway_mod.GatewayError as exc:
        raise NIError("model_error", str(exc.status_code)) from None
    text = gateway_mod.completion_text(data)
    if not isinstance(text, str) or not text:
        raise NIError("model_empty")
    return {"text": text}


def _fetch_internal_schedule(source: dict, schedules_store) -> dict:
    """Read the newest schedule_runs row (message + status + ts) for the referenced schedule."""
    if schedules_store is None:
        raise NIError("source_needs_schedules", "schedules store missing")
    sid = source["schedule_id"]
    runs = schedules_store.list_runs(sid, limit=1)
    if not runs:
        raise NIError("schedule_empty", "no runs yet")
    run = runs[0]
    return {"message": run.get("message", ""), "status": run.get("status", ""),
            "ts": run.get("ran_at", "")}


