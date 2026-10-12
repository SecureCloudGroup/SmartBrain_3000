"""Thin model shim (adapted from the proto's pipeline/common/llm.py).

The product injects the gateway transport as `call(messages) -> str`; this module
owns only the schema contract, one validator retry, and the thread-local
`no_model()` guard. No cache, lock, budget, HTTP or jsonschema import: the proto
depended on `jsonschema` and `fcntl`; the port keeps stdlib only.

Supported schema subset (exactly what present.py and future rolebind writers emit):
  type: object | array | string | number | integer | boolean | null
  additionalProperties: false
  required: [...]
  properties: {...}
  items: schema
  minItems / maxItems
  enum: [...]
  anyOf: [schema, ...]       (one branch must validate)
"""
from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass

PURPOSES = frozenset({"rolebind", "present", "critic", "probe", "fit", "query"})

_forbid = threading.local()


class ModelUnavailable(RuntimeError):
    """Gateway down, invalid JSON twice, or the caller said off; callers fall to the rules floor."""


class SchemaInvalid(ModelUnavailable):
    """The reply never matched the schema (after the one retry): the caller takes the rules floor and
    records gate `schema_invalid` — a transport failure is the plain ModelUnavailable."""


class ModelForbidden(RuntimeError):
    """A model call was attempted inside no_model() - a refresh-path contract violation."""


@dataclass
class LLMMeta:
    purpose: str
    ok: bool
    retries: int
    error: str | None = None


@contextlib.contextmanager
def no_model():
    """Block model calls on the refresh path: a call inside raises ModelForbidden."""
    prev = getattr(_forbid, "on", False)
    _forbid.on = True
    try:
        yield
    finally:
        _forbid.on = prev


def _type_ok(v, t: str) -> bool:
    """One JSON Schema primitive type against a parsed value."""
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "string":
        return isinstance(v, str)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    return False


def _validate(value, schema: dict, path: str) -> list[str]:
    """Return a list of error messages; empty = valid. Covers only the schema subset
    described in the module docstring; anything else is treated as 'no constraint'."""
    assert isinstance(schema, dict), "schema must be a dict"
    assert isinstance(path, str), "path must be a str"
    errs: list[str] = []
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            if not _validate(value, branch, path):
                return []
        errs.append(f"{path}: no anyOf branch matched")
        return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: not in enum {schema['enum']!r}")
        return errs
    t = schema.get("type")
    if t is not None and not _type_ok(value, t):
        errs.append(f"{path}: expected type {t}, got {type(value).__name__}")
        return errs
    if t == "object" or (t is None and isinstance(value, dict)):
        if isinstance(value, dict):
            props = schema.get("properties", {})
            for req in schema.get("required", []):
                if req not in value:
                    errs.append(f"{path}: missing required key {req!r}")
            if schema.get("additionalProperties") is False:
                for k in value:
                    if k not in props:
                        errs.append(f"{path}: extra key {k!r}")
            for k, sub in props.items():
                if k in value:
                    errs.extend(_validate(value[k], sub, f"{path}.{k}"))
    if t == "array" or (t is None and isinstance(value, list)):
        if isinstance(value, list):
            lo, hi = schema.get("minItems"), schema.get("maxItems")
            if lo is not None and len(value) < lo:
                errs.append(f"{path}: len {len(value)} < minItems {lo}")
            if hi is not None and len(value) > hi:
                errs.append(f"{path}: len {len(value)} > maxItems {hi}")
            if "items" in schema:
                for i, item in enumerate(value):
                    errs.extend(_validate(item, schema["items"], f"{path}[{i}]"))
    return errs


def _parse(content: str) -> tuple[object, str | None]:
    """Return (object, None) or (None, "reason"). The gateway may wrap JSON in a
    code fence; peel the fence conservatively before json.loads."""
    assert isinstance(content, str), "content must be a str"
    body = content.strip()
    if body.startswith("```"):
        nl = body.find("\n")
        if nl != -1:
            body = body[nl + 1:]
        body = body.removesuffix("```")
    try:
        return json.loads(body), None
    except json.JSONDecodeError as ex:
        return None, f"not JSON: {ex}"


def chat_json(purpose: str, messages: list, schema: dict, *, call, max_tokens: int = 500,
              skeleton: str | None = None, check=None) -> tuple[object, LLMMeta]:
    """Validate the gateway reply against `schema`; one retry on an invalid response.

    `call(messages) -> str` is the injected transport (the product passes the SB
    gateway); `max_tokens` is accepted for symmetry with the proto's signature but
    not forwarded, since the transport contract is `messages -> str` only. `skeleton`
    (fix round 1a-6, class M) is the literal JSON shape the caller's prompt already
    showed; the retry repeats it instead of describing the schema error alone.
    ``check(obj) -> [str]`` (Round 20, the query layer) states rules the schema subset cannot
    — an id from this call's menu — after the schema passed; its messages reach the retry the
    same way, so they state the violated rule and never list the menu.
    """
    assert purpose in PURPOSES, f"purpose must be one of {sorted(PURPOSES)}"
    assert callable(call), "call must be a callable (messages) -> str"
    if getattr(_forbid, "on", False):
        raise ModelForbidden(f"model call ({purpose}) inside no_model()")
    _ = max_tokens
    msgs = list(messages)
    last_err = ""
    for retries in range(2):     # one retry on invalid; bounded loop (NASA power-of-10 rule 2)
        try:
            content = call(msgs)
        except Exception as ex:
            raise ModelUnavailable(f"{type(ex).__name__}: {ex}") from ex
        if not isinstance(content, str):
            raise ModelUnavailable(f"transport returned {type(content).__name__}, not str")
        obj, parse_err = _parse(content)
        if parse_err is None:
            errs = _validate(obj, schema, "$")
            if not errs and check is not None:
                errs = list(check(obj))[:4]
            if not errs:
                return obj, LLMMeta(purpose=purpose, ok=True, retries=retries)
            last_err = "; ".join(errs[:4])
        else:
            last_err = parse_err
        if retries >= 1:
            break
        retry_ask = f"Reply with ONLY this JSON shape:\n{skeleton}" if skeleton else \
            "Reply with JSON matching the schema only."
        msgs = msgs + [{"role": "assistant", "content": content[:2000]},
                       {"role": "user", "content": f"Invalid: {last_err[:300]}. {retry_ask}"}]
    raise SchemaInvalid(f"invalid: {last_err}")
