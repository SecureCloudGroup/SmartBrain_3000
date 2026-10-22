"""Every regex the NI runs over fetched bytes must stay fast on hostile shapes.

Two reviews (#531, #532) each found one pattern that backtracked for seconds over a crafted body. This
sweeps every compiled pattern in the modules that read fetched bodies, so a new one is checked the day it
lands. Patterns that only see bounded spec fields (ni, ni_flow) are out of scope.
"""
from __future__ import annotations

import copy
import importlib
import pkgutil
import re
import time
from datetime import UTC, datetime

import pytest

from smartbrain_3000 import (
    formats,
    netguard,
    ni_fetch,
    ni_polite,
    ni_refresh,
    ni_research,
    ni_verify,
    pagegraph,
)
from smartbrain_3000.ni_research import digest
from smartbrain_3000.ni_verify import constraints

# every ni_research module reads fetched text or model replies, so the package is walked, not listed
MODULES = (formats, netguard, ni_fetch, ni_polite, pagegraph,
           *(importlib.import_module(f"{ni_research.__name__}.{m.name}") for m in pkgutil.iter_modules(ni_research.__path__)),
           # ni_verify reads the request and the fetched outputs for the refresh re-check, so it is walked too
           *(importlib.import_module(f"{ni_verify.__name__}.{m.name}") for m in pkgutil.iter_modules(ni_verify.__path__)))
N = 40000
SHAPES = ("<a hidden ", "<script>", "<!--", "<html ", "<", " ", "a", "a=", "=", "\t\n", "<?xml ", "<!doctype ",
          "key: ", "bearer ", "sk-", "eyJ", "A" * 8 + ".", "</", "<div style='display:none' ", "&#", "\\\"", "0x")
BUDGET_S = 0.5


def _patterns() -> list[tuple[str, re.Pattern]]:
    out = []
    for mod in MODULES:  # bounded: the module tuple
        for name, value in vars(mod).items():
            if isinstance(value, re.Pattern):
                out.append((f"{mod.__name__}.{name}", value))
            elif isinstance(value, tuple) and value and all(isinstance(v, re.Pattern) for v in value):
                out.extend((f"{mod.__name__}.{name}[{i}]", v) for i, v in enumerate(value))
    assert out, "the sweep found patterns"
    return out


@pytest.mark.parametrize(("name", "pattern"), _patterns(), ids=[n for n, _ in _patterns()])
def test_pattern_is_fast_on_hostile_shapes(name: str, pattern: re.Pattern) -> None:
    for shape in SHAPES:  # bounded: the shape table
        text = shape * (N // len(shape) + 1) + "!"
        subject = text.encode() if isinstance(pattern.pattern, bytes) else text
        start = time.perf_counter()
        for _ in pattern.finditer(subject):  # bounded by the subject's length
            pass
        assert time.perf_counter() - start < BUDGET_S, f"{name} backtracks on {shape!r} x {N}"


def _drift(text: str) -> object:
    """The refresh re-check over fetched text: a recipe card whose outputs (and, for the list split, request) are
    ``text``. Reads outputs only through ni_verify, as a refresh does."""
    spec = {"goal": text[:5000], "source": {"url": "https://x.example.org/q.json"},
            "recipe": {"question": {"kind": "lookup", "subject": "x", "places": ["Paris"], "entities": [], "window": None,
                                    "threshold": None, "order": [], "limit": None, "compare_count": 0, "units": None,
                                    "cadence": "daily"},
                       "verification": {"checks": [{"check": c, "ok": True, "reason": ""} for c in sorted(ni_refresh.RECHECKED)]},
                       "sources": [{"scope": "place"}]}}
    return ni_refresh.drift_reasons(copy.deepcopy(spec), {"a": text, "b": [text], "c": {"d": text}}, datetime.now(UTC))


ENTRY_POINTS = {
    "ni_refresh.drift_reasons": _drift,
    "ni_verify.constraints.parse_entities": lambda text: constraints.parse_entities("compare " + text),
    "formats.looks_like_html": formats.looks_like_html,
    "formats.parse_csv": formats.parse_csv,
    "formats.parse_feed": formats.parse_feed,
    "formats.parse_xml": formats.parse_xml,
    "formats.parse_text": formats.parse_text,
    "formats.sniff_format": lambda text: formats.sniff_format("text/plain", text),
    "digest.clean_text": digest.clean_text,
    "digest.digest": lambda text: digest.digest("https://x.example.org/a", "json", {"field": text, "list": [text]}),
    "digest.clean_search_rows": lambda text: digest.clean_search_rows(
        [{"url": "https://x.example.org/" + text[:200], "title": text, "snippet": text}] * 8),
    "digest.search_digest": lambda text: digest.search_digest(text, [{"title": text, "url": "https://x.example.org/",
                                                                      "snippet": text}]),
}
BODY_CHARS = 100_000
ENTRY_BUDGET_S = 2.0


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_text_entry_point_is_fast_on_hostile_bodies(name: str) -> None:
    """The functions fetched bodies actually go through, on a body the size netguard admits for text."""
    fn = ENTRY_POINTS[name]
    for shape in SHAPES:  # bounded: the shape table
        text = shape * (BODY_CHARS // len(shape) + 1) + "!"
        start = time.perf_counter()
        try:
            fn(text)
        except (ValueError, formats.FormatError):
            pass  # refusing the body is fine; taking long over it is not
        assert time.perf_counter() - start < ENTRY_BUDGET_S, f"{name} is slow on {shape!r} x {BODY_CHARS}"


def test_the_list_split_is_linear_on_a_long_whitespace_run() -> None:
    """L1: 40,000 whitespace characters were 34 s through ``_LIST_SPLIT_RE``."""
    start = time.perf_counter()
    for text in (" " * N + "!", "a" + " " * N + "and b", "a" + " " * N + "& b", "a" + " " * N + "with", "\t" * N):
        constraints._LIST_SPLIT_RE.split(text)
    assert time.perf_counter() - start < BUDGET_S
    assert constraints._LIST_SPLIT_RE.split("a, b and c & d with e to f") == ["a", "b", "c", "d", "e", "f"]
