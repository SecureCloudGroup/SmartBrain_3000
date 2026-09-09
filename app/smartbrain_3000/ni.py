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
import os
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("smartbrain.ni")

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

# Closed vocabularies — v1 refuses anything else, so old clients refuse new nodes rather
# than mis-render them (the "reject reserved types" contract in ni-format §5).
_SOURCE_TYPES: frozenset[str] = frozenset({"http_json", "model", "internal.schedule"})
_PARAM_KINDS: frozenset[str] = frozenset({"string", "number", "secret"})
_DISPLAY_SIZES: frozenset[str] = frozenset({"small", "wide"})
_STATES: frozenset[str] = frozenset(
    {"draft", "commissioning", "live", "degraded", "failing", "broken", "paused"}
)
_SLOTS: frozenset[str] = frozenset({"latest", "last_good", "preview"})
_REVISION_ORIGINS: frozenset[str] = frozenset(
    {"user", "agent", "repair_l1", "repair_l2", "template"}
)
_TRANSFORM_FNS: frozenset[str] = frozenset(
    {"round", "scale", "rename", "pick", "sort_by", "top_n"}
)
_SORT_DIRS: frozenset[str] = frozenset({"asc", "desc"})
_SCENE_TYPES: frozenset[str] = frozenset(
    {"stack", "grid", "divider", "text", "number", "chip", "bar", "icon", "repeat"}
)
# Reserved for later phases — validators MUST reject in v1 so old apps refuse new scenes.
_RESERVED_SCENE_TYPES: frozenset[str] = frozenset(
    {"spark", "gauge", "image", "when", "on_tap"}
)
_STACK_DIRS: frozenset[str] = frozenset({"v", "h"})
_STACK_GAPS: frozenset[str] = frozenset({"sm", "md"})
_TEXT_ROLES: frozenset[str] = frozenset({"title", "label", "value", "caption"})
_TEXT_TONES: frozenset[str] = frozenset(
    {"default", "muted", "accent", "ok", "warn", "danger"}
)
_TEXT_SIZES: frozenset[str] = frozenset({"sm", "md", "lg"})
_NUM_FORMATS: frozenset[str] = frozenset({"plain", "compact", "percent", "currency"})
_CHIP_KINDS: frozenset[str] = frozenset({"", "accent", "ok", "warn", "danger"})

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
               "interval_minutes"}
    _closed_keys(body, allowed, "spec")
    if body.get("version") != 1:
        raise ValueError("spec.version must be 1")
    _require_str(body.get("title"), "spec.title", max_len=_MAX_TITLE)
    _require_str(body.get("goal"), "spec.goal", max_len=_MAX_GOAL)
    _validate_params(body.get("params") or {})
    _validate_source(body.get("source"))
    _validate_pipeline(body.get("pipeline") or [])
    validate_scene(body.get("scene"))
    _validate_display(body.get("display") or {})
    _validate_repair_policy(body.get("repair_policy") or {})
    _validate_model_override(body.get("model"))
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
    """§3 http_json: {type, url, headers}. URL is user-consented; $secret refs live here."""
    _closed_keys(s, {"type", "url", "headers"}, "spec.source (http_json)")
    _require_str(s.get("url"), "spec.source.url", max_len=_MAX_URL)
    headers = s.get("headers") or {}
    hdrs = _require_dict(headers, "spec.source.headers")
    if len(hdrs) > _MAX_HEADERS:
        raise ValueError(f"spec.source.headers exceeds {_MAX_HEADERS}")
    for name, value in hdrs.items():
        _require_str(name, "spec.source.headers key", max_len=_MAX_HEADER_NAME)
        if isinstance(value, dict):
            _closed_keys(value, {"$secret"}, f"spec.source.headers.{name}")
            _require_str(value.get("$secret"), f"spec.source.headers.{name}.$secret",
                         max_len=_MAX_HEADER_VALUE)
        else:
            _require_str(value, f"spec.source.headers.{name}", max_len=_MAX_HEADER_VALUE)


def _validate_model_source(s: dict) -> None:
    """§3 model: {type, instruction}. User-visible spec content."""
    _closed_keys(s, {"type", "instruction"}, "spec.source (model)")
    _require_str(s.get("instruction"), "spec.source.instruction", max_len=_MAX_INSTRUCTION)


def _validate_internal_schedule_source(s: dict) -> None:
    """§3 internal.schedule: {type, schedule_id}. Zero egress; reads schedule_runs."""
    _closed_keys(s, {"type", "schedule_id"}, "spec.source (internal.schedule)")
    _require_str(s.get("schedule_id"), "spec.source.schedule_id", max_len=100)


def _validate_pipeline(pipeline: object) -> None:
    """§4 pipeline stages — extract / transform, each with its own shape."""
    if not isinstance(pipeline, list):
        raise ValueError("spec.pipeline must be a list")  # noqa: TRY004
    if len(pipeline) > _MAX_PIPELINE_STAGES:
        raise ValueError(f"spec.pipeline exceeds {_MAX_PIPELINE_STAGES} stages")
    for i, stage in enumerate(pipeline):
        st = _require_dict(stage, f"spec.pipeline[{i}]")
        op = st.get("op")
        if op == "extract":
            _validate_extract_stage(st, i)
        elif op == "transform":
            _validate_transform_stage(st, i)
        else:
            raise ValueError(f"spec.pipeline[{i}].op must be 'extract' or 'transform'")


def _validate_extract_stage(st: dict, i: int) -> None:
    """One extract stage: closed keys, path grammar per named output."""
    _closed_keys(st, {"op", "paths"}, f"spec.pipeline[{i}]")
    paths = _require_dict(st.get("paths"), f"spec.pipeline[{i}].paths")
    if not paths or len(paths) > _MAX_EXTRACT_PATHS:
        raise ValueError(f"spec.pipeline[{i}].paths must be 1..{_MAX_EXTRACT_PATHS} entries")
    for name, path in paths.items():
        if not isinstance(name, str) or not _KEY_RE.match(name):
            raise ValueError(f"spec.pipeline[{i}].paths key {name!r} malformed")
        if not isinstance(path, str):
            raise ValueError(f"spec.pipeline[{i}].paths.{name} must be a string")  # noqa: TRY004
        parse_path(path)  # raises ValueError on any grammar violation


def _validate_transform_stage(st: dict, i: int) -> None:
    """One transform stage: closed function set, per-fn required args."""
    _closed_keys(st, {"op", "apply"}, f"spec.pipeline[{i}]")
    apply = st.get("apply")
    if not isinstance(apply, list) or not apply or len(apply) > _MAX_TRANSFORM_APPLY:
        raise ValueError(f"spec.pipeline[{i}].apply must be 1..{_MAX_TRANSFORM_APPLY} ops")
    for j, op in enumerate(apply):
        _validate_transform_op(op, i, j)


def _validate_transform_op(op: object, i: int, j: int) -> None:
    """One transform apply entry — closed fn set + required args per fn."""
    node = _require_dict(op, f"spec.pipeline[{i}].apply[{j}]")
    fn = node.get("fn")
    if fn not in _TRANSFORM_FNS:
        raise ValueError(f"spec.pipeline[{i}].apply[{j}].fn must be one of {sorted(_TRANSFORM_FNS)}")
    field = node.get("field")
    if not isinstance(field, str) or not _KEY_RE.match(field):
        raise ValueError(f"spec.pipeline[{i}].apply[{j}].field malformed")
    if fn == "round":
        _closed_keys(node, {"fn", "field", "digits"}, f"spec.pipeline[{i}].apply[{j}]")
        if not isinstance(node.get("digits"), int) or isinstance(node.get("digits"), bool):
            raise ValueError(f"spec.pipeline[{i}].apply[{j}].digits must be int")
    elif fn == "scale":
        _closed_keys(node, {"fn", "field", "factor"}, f"spec.pipeline[{i}].apply[{j}]")
        if not isinstance(node.get("factor"), (int, float)) or isinstance(node.get("factor"), bool):
            raise ValueError(f"spec.pipeline[{i}].apply[{j}].factor must be number")
    elif fn == "rename":
        _closed_keys(node, {"fn", "field", "to"}, f"spec.pipeline[{i}].apply[{j}]")
        to = node.get("to")
        if not isinstance(to, str) or not _KEY_RE.match(to):
            raise ValueError(f"spec.pipeline[{i}].apply[{j}].to malformed")
    elif fn == "pick":
        _closed_keys(node, {"fn", "field", "keys"}, f"spec.pipeline[{i}].apply[{j}]")
        keys = node.get("keys")
        if not isinstance(keys, list) or not keys or not all(
                isinstance(k, str) and _KEY_RE.match(k) for k in keys):
            raise ValueError(f"spec.pipeline[{i}].apply[{j}].keys must be a non-empty key list")
    elif fn == "sort_by":
        _closed_keys(node, {"fn", "field", "key", "dir"}, f"spec.pipeline[{i}].apply[{j}]")
        if "key" in node and not (isinstance(node["key"], str) and _KEY_RE.match(node["key"])):
            raise ValueError(f"spec.pipeline[{i}].apply[{j}].key malformed")
        if node.get("dir") not in _SORT_DIRS:
            raise ValueError(f"spec.pipeline[{i}].apply[{j}].dir must be 'asc' or 'desc'")
    else:  # top_n
        _closed_keys(node, {"fn", "field", "n"}, f"spec.pipeline[{i}].apply[{j}]")
        n = node.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > _MAX_TOP_N:
            raise ValueError(f"spec.pipeline[{i}].apply[{j}].n must be 1..{_MAX_TOP_N}")


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
    shape via a dispatch table; children are pushed onto the pending stack."""
    assert counter is not None, "counter required"
    assert depth >= 1, "depth must start at 1"
    pending: list[tuple[object, int]] = [(node, depth)]
    for _ in range(2 * _MAX_SCENE_NODES_PRE_EXPAND):  # bounded (POW10 #2); the cap is the real bound
        if not pending:
            return
        current, d = pending.pop()
        if d > _MAX_SCENE_DEPTH:
            raise ValueError(f"scene depth exceeds {_MAX_SCENE_DEPTH}")
        n = _require_dict(current, "scene node")
        ntype = n.get("type")
        if ntype in _RESERVED_SCENE_TYPES:
            raise ValueError(f"scene node type {ntype!r} is reserved and refused in v1")
        if ntype not in _SCENE_TYPES:
            raise ValueError(f"scene node type {ntype!r} unknown")
        counter.count += 1
        children = _validate_scene_shape(n)
        for child in reversed(children):  # bounded by the current node's children
            pending.append((child, d + 1))
    raise ValueError("scene traversal exceeded bound")


def _validate_scene_shape(node: dict) -> list[object]:
    """Per-type shape check; returns the child list to push (empty when leaf)."""
    dispatch = {
        "stack": _validate_stack, "grid": _validate_grid, "divider": _validate_divider,
        "text": _validate_text, "number": _validate_number, "chip": _validate_chip,
        "bar": _validate_bar, "icon": _validate_icon, "repeat": _validate_repeat,
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
    _closed_keys(node, {"type", "value", "role", "tone", "size"}, "scene text")
    _validate_bindable(node.get("value"), "scene text.value", allow_string=True, max_len=_MAX_TEXT_CHARS)
    if node.get("role") not in _TEXT_ROLES:
        raise ValueError(f"scene text.role must be one of {sorted(_TEXT_ROLES)}")
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene text.tone must be one of {sorted(_TEXT_TONES)}")
    if node.get("size") not in _TEXT_SIZES:
        raise ValueError(f"scene text.size must be one of {sorted(_TEXT_SIZES)}")
    return []


def _validate_number(node: dict) -> list[object]:
    _closed_keys(node, {"type", "value", "format", "unit", "tone", "size"}, "scene number")
    _validate_bindable(node.get("value"), "scene number.value", allow_string=False, max_len=0)
    if node.get("format") not in _NUM_FORMATS:
        raise ValueError(f"scene number.format must be one of {sorted(_NUM_FORMATS)}")
    unit = node.get("unit")
    if unit is not None and (not isinstance(unit, str) or len(unit) > 20):
        raise ValueError("scene number.unit must be a short string or null")
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene number.tone must be one of {sorted(_TEXT_TONES)}")
    if node.get("size") not in _TEXT_SIZES:
        raise ValueError(f"scene number.size must be one of {sorted(_TEXT_SIZES)}")
    return []


def _validate_chip(node: dict) -> list[object]:
    _closed_keys(node, {"type", "value", "kind"}, "scene chip")
    _validate_bindable(node.get("value"), "scene chip.value", allow_string=True, max_len=_MAX_TEXT_CHARS)
    if node.get("kind") not in _CHIP_KINDS:
        raise ValueError(f"scene chip.kind must be one of {sorted(_CHIP_KINDS)}")
    return []


def _validate_bar(node: dict) -> list[object]:
    _closed_keys(node, {"type", "value", "max", "tone"}, "scene bar")
    _validate_bindable(node.get("value"), "scene bar.value", allow_string=False, max_len=0)
    _validate_bindable(node.get("max"), "scene bar.max", allow_string=False, max_len=0)
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene bar.tone must be one of {sorted(_TEXT_TONES)}")
    return []


def _validate_icon(node: dict) -> list[object]:
    _closed_keys(node, {"type", "name", "tone"}, "scene icon")
    _require_str(node.get("name"), "scene icon.name", max_len=60)
    if node.get("tone") not in _TEXT_TONES:
        raise ValueError(f"scene icon.tone must be one of {sorted(_TEXT_TONES)}")
    return []


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


def run_pipeline(stages: list[dict], payload: object) -> dict:
    """Execute the pipeline (§4); return the named-outputs dict. Raises NIError on any
    failure (missing path, transform type mismatch — never coercion)."""
    assert isinstance(stages, list), "stages must be a list"
    assert payload is not None, "payload required"
    current: object = payload
    for stage in stages:  # bounded by _MAX_PIPELINE_STAGES
        op = stage.get("op")
        if op == "extract":
            current = _apply_extract(stage.get("paths") or {}, current)
        elif op == "transform":
            current = _apply_transform(stage.get("apply") or [], current)
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


def _apply_transform(apply: list[dict], payload: object) -> dict:
    """Apply each transform op to the named-outputs dict, in order."""
    if not isinstance(payload, dict):
        raise NIError("transform_needs_dict", "transform requires an extract dict")
    current = dict(payload)  # copy: transforms MUST NOT mutate caller state
    for op in apply:  # bounded by _MAX_TRANSFORM_APPLY
        current = _apply_transform_op(op, current)
    return current


def _apply_transform_op(op: dict, payload: dict) -> dict:
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
    else:  # top_n (bounded)
        out[field] = _txf_top_n(payload[field], op["n"])
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


# --- param substitution + bind (pure) -------------------------------------

def substitute_params(spec: dict) -> dict:
    """Return a copy of ``spec`` with ``{{param:X}}`` filled from ``spec.params`` values.

    Rejects a secret-kind param appearing anywhere — secrets attach only through
    ``{"$secret": "..."}`` in headers, resolved at fetch time. String and number params
    substitute inline; everything else passes through unchanged.
    """
    assert isinstance(spec, dict), "spec must be a dict"
    params = spec.get("params") or {}
    assert isinstance(params, dict), "params must be a dict"
    result = json.loads(json.dumps(spec))  # deep copy — the spec dict must not be mutated
    _substitute_in(result, params, path="spec")
    return result


def _substitute_in(node: object, params: dict, *, path: str) -> None:
    """Walk ``node`` in place; substitute {{param:X}} in every string except $secret bodies."""
    assert path, "path required for error context"
    assert params is not None, "params required"
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str):
                node[k] = _resolve_param_string(v, params, path=f"{path}.{k}")
            elif isinstance(v, (dict, list)):
                if isinstance(v, dict) and set(v.keys()) == {"$secret"}:
                    continue  # $secret bodies are opaque names, resolved at fetch time
                _substitute_in(v, params, path=f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = _resolve_param_string(v, params, path=f"{path}[{i}]")
            elif isinstance(v, (dict, list)):
                _substitute_in(v, params, path=f"{path}[{i}]")


def _resolve_param_string(value: str, params: dict, *, path: str) -> str:
    """Return ``value`` with every ``{{param:X}}`` filled; secret refs refused."""
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
        return str(p.get("value"))

    return _PARAM_PLACEHOLDER.sub(_one, value)


def bind_scene(scene: dict, data: dict) -> dict:
    """Return the scene with $bind + {{path}} resolved and repeat nodes expanded.

    Enforces the post-expansion caps (nodes <= 100, depth <= 8, text <= 2000). Any
    unresolved binding, type mismatch, or cap violation raises NIError.
    """
    assert isinstance(scene, dict), "scene must be a dict"
    assert isinstance(data, dict), "data must be a dict"
    counter = _NodeCounter()
    bound = _bind_node(scene, data, depth=1, item=None, counter=counter)
    if counter.count > _MAX_SCENE_NODES:
        raise NIError("bind_scene_too_large", f"{counter.count} nodes (max {_MAX_SCENE_NODES})")
    return bound


def _bind_node(node: object, data: dict, *, depth: int, item: Any, counter: _NodeCounter) -> dict:
    """Bind one scene node (iterative expansion of repeat)."""
    assert counter is not None, "counter required"
    if not isinstance(node, dict):
        raise NIError("bind_bad_node", "scene node must be a dict")
    if depth > _MAX_SCENE_DEPTH:
        raise NIError("bind_depth", f"exceeds {_MAX_SCENE_DEPTH}")
    counter.count += 1
    if counter.count > _MAX_SCENE_NODES:
        raise NIError("bind_scene_too_large", f"{counter.count} nodes (max {_MAX_SCENE_NODES})")
    ntype = node.get("type")
    if ntype == "repeat":
        return _bind_repeat(node, data, depth=depth, counter=counter)
    out: dict = {"type": ntype}
    for key, value in node.items():
        if key == "type":
            continue
        if key == "children":
            out[key] = [_bind_node(c, data, depth=depth + 1, item=item, counter=counter)
                        for c in (value or [])]  # bounded by scene node cap
        else:
            out[key] = _bind_value(value, data, item=item)
    return out


def _bind_repeat(node: dict, data: dict, *, depth: int, counter: _NodeCounter) -> dict:
    """Expand a repeat into a stack of template clones bound to each list element."""
    items_bind = node.get("items") or {}
    steps = parse_path(items_bind["$bind"])
    items = _resolve_path(data, steps)
    if not isinstance(items, list):
        raise NIError("bind_type", "repeat.items must resolve to a list")
    max_n = int(node.get("max", _MAX_REPEAT_MAX))
    template = node["template"]
    children: list[dict] = []
    for entry in items[:max_n]:  # bounded by max_n <= _MAX_REPEAT_MAX
        children.append(_bind_node(template, data, depth=depth + 1, item=entry, counter=counter))
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
    a fetch host differs, so a stolen or misrouted credential cannot cross hosts. Returns
    the store key name.
    """
    assert secrets_store is not None, "secrets store required"
    assert item_id and name and value, "item_id + name + value required"
    assert isinstance(host, str) and host, "host required (empty = no binding)"
    key = f"ni:{item_id}:{name}"
    secrets_store.put(key, json.dumps({"value": value, "host": host}))
    return key


def _load_credential(secrets_store, key: str, expected_host: str) -> str:
    """Return the stored value iff its bound host matches ``expected_host``; else NIError."""
    assert secrets_store is not None, "secrets store required"
    assert key and expected_host, "key + host required"
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
        bound = bind_scene(validated["scene"], preview_payload)  # proves the preview renders
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
            "consecutive_failures, position, spec_rev, nonce, ciphertext, created_at, "
            "updated_at FROM ni_items WHERE id = ?;",
            [item_id],
        ).fetchone()
        return None if row is None else self._row(row)

    def list_items(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, enabled, state, interval_minutes, last_checked, last_status, "
            "consecutive_failures, position, spec_rev, nonce, ciphertext, created_at, "
            "updated_at FROM ni_items ORDER BY position ASC, created_at ASC LIMIT ?;",
            [_MAX_ITEMS],
        ).fetchall()
        assert isinstance(rows, list), "fetchall must return a list"
        return [self._row(r) for r in rows]  # bounded by _MAX_ITEMS

    def update_spec(self, item_id: str, new_spec: dict, *, origin: str = "user") -> int:
        """Validate + reseal + bump spec_rev; append a revision row and prune to 10."""
        assert item_id, "item id required"
        assert isinstance(new_spec, dict), "spec must be a dict"
        current = self.get_item(item_id)
        if current is None:
            raise ValueError("item not found")
        if origin not in _REVISION_ORIGINS:
            raise ValueError(f"origin must be one of {sorted(_REVISION_ORIGINS)}")
        validated = validate_spec(new_spec)
        new_rev = int(current["spec_rev"]) + 1
        interval = self._clamp_interval(validated)
        nonce, ciphertext = self._seal_item(item_id, validated)
        self._conn.execute(
            "UPDATE ni_items SET nonce = ?, ciphertext = ?, spec_rev = ?, "
            "interval_minutes = ?, updated_at = now() WHERE id = ?;",
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
            "consecutive_failures, position, spec_rev, nonce, ciphertext, created_at, "
            "updated_at FROM ni_items "
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

        Combined with ``mark_checked``, this is the run bookkeeping the engine calls after
        a failure (whether the failure was fetch, transform, contract, or bind — every
        failing outcome bumps the counter equally per §6).
        """
        assert item_id and isinstance(status, str), "item id + status required"
        self._conn.execute(
            "UPDATE ni_items SET consecutive_failures = consecutive_failures + 1, "
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
            "UPDATE ni_items SET consecutive_failures = 0, last_checked = now(), "
            "last_status = ? WHERE id = ?;",
            [status[:_MAX_STATUS], item_id],
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
        """
        assert item_id, "item id required"
        assert isinstance(ok, bool), "ok must be bool"
        assert isinstance(note, str), "note must be a string"
        current = self.get_item(item_id)
        if current is None:
            raise ValueError("item not found")
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
        spec = json.loads(self._aes.decrypt(bytes(row[9]), bytes(row[10]), aad).decode("utf-8"))
        return {
            "id": item_id, "enabled": bool(row[1]), "state": str(row[2]),
            "interval_minutes": int(row[3]),
            "last_checked": None if row[4] is None else str(row[4]),
            "last_status": str(row[5] or ""),
            "consecutive_failures": int(row[6]), "position": int(row[7]),
            "spec_rev": int(row[8]), "spec": spec,
            "created_at": str(row[11]), "updated_at": str(row[12]),
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

def tick(app, pass_budget_seconds: float = 20.0) -> int:
    """Run due NI items — modeled line-for-line on feeds.tick.

    Bails when the vault is locked (sealed specs can't decrypt); owns a per-thread
    cursor (DuckDB cursors are not thread-safe); one bounded try/except per item; a
    wall-clock budget between items so a slow host can't eat the tick.
    """
    assert app is not None, "app required"
    assert pass_budget_seconds > 0, "pass budget must be positive"
    key = getattr(app.state, "master_key", None)
    if key is None:
        return 0  # locked — sealed specs can't even decrypt
    from . import (
        gateway as gateway_mod,  # lazy: keep gateway off ni's import graph edges
    )
    from .scheduler import ScheduleStore
    from .secrets import SecretStore

    cursor = app.state.db.cursor()
    checked = 0
    try:
        store = NIStore(cursor, key)
        secrets_store = SecretStore(cursor, key)
        schedules_store = ScheduleStore(cursor, key)
        started = time.monotonic()
        for item in store.due_items():  # bounded by _MAX_ITEMS_PER_PASS
            if time.monotonic() - started > pass_budget_seconds:
                return checked  # the rest stay due; next tick continues
            try:
                run_item(store, item["id"], gateway_mod=gateway_mod,
                         secrets_store=secrets_store, schedules_store=schedules_store)
            except NIError as exc:
                store.mark_checked(item["id"], exc.kind[:_MAX_STATUS])
            except Exception:  # last-resort net: one bad item must not stop the pass
                log.warning("ni item run failed with unexpected error: item=%s", item["id"])
                store.mark_checked(item["id"], "internal")
            checked += 1
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    return checked


def run_item(store: NIStore, item_id: str, *, gateway_mod, secrets_store,
             schedules_store=None) -> dict:
    """Execute one item end-to-end and apply the state-machine transition.

    Substitute params -> fetch -> pipeline -> optional contract check -> bind -> write
    snapshots (latest always; last_good on success) -> record_run -> transition. Raises
    NIError on a failure (caller records mark_checked + last_status).
    """
    assert store is not None and item_id, "store + id required"
    assert gateway_mod is not None and secrets_store is not None, "gateway + secrets required"
    item = store.get_item(item_id)
    if item is None:
        raise NIError("item_missing")
    started = time.monotonic()
    try:
        spec = substitute_params(item["spec"])
        payload = _fetch_source(spec, item_id, gateway_mod, secrets_store, schedules_store)
        outputs = run_pipeline(spec.get("pipeline") or [], payload)
    except NIError as exc:
        _handle_failure(store, item, exc, started)
        raise
    except Exception as exc:
        wrapped = NIError("internal", exc.__class__.__name__)
        _handle_failure(store, item, wrapped, started)
        raise wrapped from None
    return _finalize_run(store, item, spec, outputs, started)


def _finalize_run(store: NIStore, item: dict, spec: dict, outputs: dict,
                  started: float) -> dict:
    """Contract-check (if applicable), bind, write snapshots, record run + transition."""
    contract = spec.get("contract")
    state = item["state"]
    contract_ok: bool | None = None
    if contract is not None and state in ("live", "degraded", "failing"):
        ok, violation = check_contract(contract, outputs)
        contract_ok = ok
        if not ok:
            exc = NIError("contract_violation", violation)
            _handle_failure(store, item, exc, started, contract_ok=False)
            raise exc
    try:
        bound = bind_scene(spec["scene"], outputs)
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
    return {"status": "ok", "duration_ms": duration_ms}


def _handle_failure(store: NIStore, item: dict, exc: NIError, started: float, *,
                    contract_ok: bool | None = None) -> None:
    """Bookkeeping for any failing outcome: run row + failure counter + state transition."""
    assert store is not None and item is not None and exc is not None, "args required"
    duration_ms = int((time.monotonic() - started) * 1000)
    store.record_run(item["id"], "error", duration_ms=duration_ms, error=exc.kind,
                     contract_ok=contract_ok)
    new_count = store.bump_failure(item["id"], exc.kind)
    _transition_on_failure(store, item, exc, new_count)


def _transition_on_success(store: NIStore, item: dict, spec: dict, outputs: dict) -> None:
    """§6 success transitions: commissioning->live (C3 after C2 ok, contract satisfied);
    contract capture on C1; degraded/failing/live -> live."""
    assert store is not None and item and spec, "args required"
    state = item["state"]
    if state == "commissioning":
        if spec.get("_c2_ok") is True:
            # C3: the first post-C2 run must satisfy the (already captured) contract.
            # Move to live and keep the contract as-is.
            store.set_state(item["id"], "live")
            return
        # C1: capture the contract on first successful commissioning run.
        contract = capture_contract(outputs)
        updated = dict(item["spec"])
        updated["contract"] = contract
        # Re-seal WITHOUT bumping spec_rev — the contract is system-written; a rev bump
        # would incorrectly claim the user/agent changed the spec.
        nonce, ciphertext = store._seal_item(item["id"], updated)
        store.conn.execute(
            "UPDATE ni_items SET nonce = ?, ciphertext = ?, updated_at = now() WHERE id = ?;",
            [nonce, ciphertext, item["id"]],
        )
        return
    if state in ("live", "degraded", "failing"):
        store.set_state(item["id"], "live")


def _transition_on_failure(store: NIStore, item: dict, exc: NIError, count: int) -> None:
    """§6 failure transitions: degraded -> failing after threshold; broken on the
    escalation rule (8 failures across >=7 days) or a permanent refusal."""
    assert store is not None and item is not None and exc is not None, "args required"
    if exc.kind == "secret_host_mismatch":
        # A credential host mismatch is a permanent refusal — no schedule of retries
        # will ever resolve it (per §6). Escalate straight to broken.
        store.set_state(item["id"], "broken")
        return
    if count >= _BROKEN_FAILURE_COUNT:
        created = store.get_created_at(item["id"])
        now = datetime.now(UTC)
        if created is not None and (now - created) >= timedelta(days=_BROKEN_MIN_DAYS):
            store.set_state(item["id"], "broken")
            return
    if item["state"] == "commissioning":
        return  # C1 failure stays commissioning per §6 (agent redrafts)
    if count >= _FAILING_THRESHOLD:
        store.set_state(item["id"], "failing")
    else:
        store.set_state(item["id"], "degraded")


def _fetch_source(spec: dict, item_id: str, gateway_mod, secrets_store,
                  schedules_store) -> dict:
    """Dispatch by source type — each returns the payload the pipeline consumes."""
    assert isinstance(spec, dict) and item_id, "spec + id required"
    source = spec.get("source") or {}
    stype = source.get("type")
    if stype == "http_json":
        return _fetch_http_json(source, item_id, secrets_store)
    if stype == "model":
        return _fetch_model(spec, source, gateway_mod)
    if stype == "internal.schedule":
        return _fetch_internal_schedule(source, schedules_store)
    raise NIError("source_bad_type", str(stype))


def _fetch_http_json(source: dict, item_id: str, secrets_store) -> dict:
    """Guarded JSON fetch; headers with ``$secret`` are host-bound at storage time."""
    from urllib.parse import urlparse

    from . import netguard  # lazy: keep netguard off ni's import graph edges

    url = source["url"]
    host = urlparse(url).hostname or ""
    if not host:
        raise NIError("source_bad_url", "no host")
    resolved_headers: dict[str, str] = {}
    for name, value in (source.get("headers") or {}).items():  # bounded by _MAX_HEADERS
        if isinstance(value, dict) and "$secret" in value:
            resolved_headers[name] = _load_credential(secrets_store, value["$secret"], host)
        elif isinstance(value, str):
            resolved_headers[name] = value
    try:
        return netguard.safe_fetch_json(url, headers=resolved_headers or None)
    except netguard.FetchError as exc:
        raise NIError("fetch_failed", exc.__class__.__name__) from None


def _fetch_model(spec: dict, source: dict, gateway_mod) -> dict:
    """Run one chat completion on the ``ni`` route (or item override); return {'text': ...}."""
    assert isinstance(spec, dict) and isinstance(source, dict), "spec + source required"
    routes = gateway_mod.load_routes(None) if hasattr(gateway_mod, "load_routes") else {}
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


