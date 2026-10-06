"""Token access for both painters and the layout (FROZEN). Owner: architect.

CLIR prims name tokens (e.g. 'viz-line'); only painters resolve them to colours.
The layout reads TYPE / SPACE / VIZ for geometry. Nothing else may hard-code a
colour, a font size or a spacing value.
"""
from __future__ import annotations

import json
from pathlib import Path

_T = json.loads((Path(__file__).with_name("tokens.json")).read_text())

THEMES = ("dark", "light")
TYPE: dict = {k: v for k, v in _T["type"].items() if not k.startswith("_")}
ROLE_TOKENS: dict = {k: v for k, v in _T["role_tokens"].items() if not k.startswith("_")}
SPACE: dict = _T["space"]
VIZ: dict = _T["viz"]
MOTION: dict = _T["motion"]
COLOR_NAMES: frozenset = frozenset(_T["color"]["dark"])
assert set(_T["color"]["dark"]) == set(_T["color"]["light"]), "themes must define the same tokens"


def rgba(theme: str, name: str) -> tuple[int, int, int, float]:
    """'#rrggbb' or '#rrggbb@a' -> (r, g, b, a in 0..1)."""
    v = _T["color"][theme][name]
    hexpart, _, a = v.partition("@")
    h = hexpart.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), float(a) if a else 1.0


def css(theme: str, name: str) -> str:
    r, g, b, a = rgba(theme, name)
    return f"#{r:02x}{g:02x}{b:02x}" if a == 1.0 else f"rgba({r},{g},{b},{a:g})"


def css_vars(theme: str) -> str:
    """':root' custom properties for the vector painter: --ni-<token>."""
    return "\n".join(f"  --ni-{n}: {css(theme, n)};" for n in sorted(COLOR_NAMES))
