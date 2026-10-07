"""Shared asset + geometry helpers for painters (ported from pipeline/paint/raster.py).

The vector painter needs five raster-free helpers (text_style, _tri_pts, land, icons,
blob_path). The proto held them in raster.py beside HarfBuzz/Pillow draws; moving them
here means ni_forms.paint.vector imports neither uharfbuzz nor Pillow at module load (it opens Pillow lazily
for image prims only, and degrades to the placeholder when it is absent). A future
raster painter (not ported in Phase 1a-1) will import these too.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .. import tokens as TK

_ASSETS = Path(__file__).resolve().parents[1] / "assets"
_BLOBS = _ASSETS.parent / "out" / "blobs"


@lru_cache(maxsize=1)
def icons() -> dict:
    """Load the first-party icon outlines (ICONS enum -> numeric paths)."""
    p = _ASSETS / "icons.json"
    assert p.exists(), f"icons.json missing at {p}"
    return json.loads(p.read_text())


@lru_cache(maxsize=1)
def land() -> dict:
    """Load the simplified public-domain land outline for map_lite's basemap prim."""
    p = _ASSETS / "world110m.json"
    assert p.exists(), f"world110m.json missing at {p}"
    return json.loads(p.read_text())


def blob_path(sha: str) -> Path | None:
    """Return the on-disk path for an image blob by sha; None when absent.

    The product supplies blobs through its own storage; this helper mirrors the
    proto's `out/blobs/<sha>.<ext>` convention so a dev gallery can resolve
    hero images that happen to live in the package tree.
    """
    assert isinstance(sha, str), "sha must be a str"
    assert len(sha) == 64 or sha == "", "sha must be a 64-char hex (or empty)"
    if not _BLOBS.exists():
        return None
    for p in sorted(_BLOBS.glob(f"{sha}.*")):
        return p
    return None


def text_style(p: dict) -> tuple[float, int, bool, float]:
    """(px, weight, tnum, line height) for a text/time prim; ladder px scales lh.

    This mirrors raster.text_style exactly so vector + raster output stay aligned.
    """
    assert isinstance(p, dict), "prim must be a dict"
    assert p.get("role") in TK.TYPE, f"unknown text role {p.get('role')!r}"
    r = TK.TYPE[p["role"]]
    px = p.get("px") or r["px"]
    return px, r["wt"], bool(r.get("tnum")), r["lh"] * px / r["px"]


def _tri_pts(x, y, size, direction):
    """Equilateral triangle vertices for the delta/tri prim (same math as raster)."""
    assert direction in ("up", "down", "left", "right"), f"bad tri dir {direction!r}"
    assert size > 0, "size must be > 0"
    h = size * 0.866
    if direction == "up":
        return [(x - size / 2, y + h / 2), (x + size / 2, y + h / 2), (x, y - h / 2)]
    if direction == "down":
        return [(x - size / 2, y - h / 2), (x + size / 2, y - h / 2), (x, y + h / 2)]
    if direction == "left":
        return [(x + h / 2, y - size / 2), (x + h / 2, y + size / 2), (x - h / 2, y)]
    return [(x - h / 2, y - size / 2), (x - h / 2, y + size / 2), (x + h / 2, y)]
