"""Canonical JSON and hashes (FROZEN). Owner: architect.

canonical(): sorted keys, no whitespace, floats rounded (CLIR geometry to 2 dp),
NaN/inf rejected. Same input -> same bytes -> same sha256, on every run.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass


def _norm(o, nd: int | None):
    if is_dataclass(o):
        o = asdict(o)
    if isinstance(o, float):
        if not math.isfinite(o):
            raise ValueError("non-finite float in canonical JSON")
        if nd is not None:
            o = round(o, nd)
            return int(o) if o == int(o) else o
        return o
    if isinstance(o, dict):
        return {str(k): _norm(v, nd) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_norm(v, nd) for v in o]
    return o


def canonical(obj, float_dp: int | None = None) -> bytes:
    return json.dumps(_norm(obj, float_dp), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256(b: bytes | str) -> str:
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def clir_hash(clir: dict) -> str:
    """Hash of the CLIR with geometry rounded to 0.01 px. The CLIR carries no debug,
    timings or lint, so nothing to strip."""
    return sha256(canonical(clir, float_dp=2))


def fingerprint(kind: str, fields: list, n_rows: int) -> str:
    """sha256(kind, [(name,type,unit,role)], row_count_class). row_count_class:
    0 | 1 | 2-9 | 10-99 | 100+ so that ordinary row-count changes are not drift."""
    cls = "0" if n_rows == 0 else "1" if n_rows == 1 else "2-9" if n_rows < 10 else "10-99" if n_rows < 100 else "100+"
    sig = [kind, [[f["name"], f["type"], f.get("unit"), f.get("role")] if isinstance(f, dict)
                  else [f.name, f.type, f.unit, f.role] for f in fields], cls]
    return sha256(canonical(sig))
