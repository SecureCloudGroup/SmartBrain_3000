"""Which record fields the ask names — the ASKED quantity (fix round 1a-5, class A).

WHY: the first live measurement (2026-10-07) showed plans that dropped the very field the
words were about — the desktop day table of "rain chances this week" kept the high and
dropped the rain column; the table for "top 10 by market cap" showed the price. Nothing in
the engine knew which field the ask was about. This module decides it by code: a field is
asked when a content word of the ask or of the frame's wants matches one of the field's own
words (label, name, path tail) — the same token after a plural fold, or two tokens that
share a synonym group of the wants lexicon (``field_words``: rain / precipitation / showers,
temp / temperature ...). No model, no per-card vocabulary: one lexicon file.

Consumers: ``record`` (the asked number leads the row), ``enumerate`` (a span that drops an
asked field ranks below one that keeps it; the default span keeps every asked field),
``present`` (L-ASK hard gate on the model's pick).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_ASSETS = Path(__file__).with_name("assets")
_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_MAX_WORDS = 64                 # bound on the tokens read from one ask / one field
_STOP = frozenset({
    "the", "and", "for", "with", "from", "into", "this", "that", "these", "those", "what", "whats", "how",
    "where", "who", "which", "show", "get", "give", "tell", "will", "does", "there", "please", "near",
    "right", "now", "today", "tonight", "tomorrow", "week", "weekend", "year", "month", "latest", "recent",
    "current", "any", "many", "much", "next", "upcoming", "top", "best", "new", "last", "night", "all",
    "about", "over", "under", "above", "below", "between", "after", "before", "vs", "versus", "compare",
    "live", "map", "list", "chart", "history", "trend",
})


@lru_cache(maxsize=1)
def _groups() -> dict[str, tuple]:
    """token -> synonym group ids (a word may sit in several: "when" is a time and a date), from
    the wants lexicon's ``field_words`` block."""
    p = _ASSETS / "lexicon" / "wants.json"
    assert p.exists(), f"wants lexicon missing at {p}"
    groups = json.loads(p.read_text(encoding="utf-8")).get("field_words") or {}
    out: dict[str, tuple] = {}
    for gid, words in groups.items():
        for w in words[:32]:            # bounded by the lexicon
            key = str(w).lower()
            out[key] = (*out.get(key, ()), gid)
    return out


def _fold(word: str) -> str:
    """A simple plural folded to its singular (as the flow's answer matcher folds it)."""
    assert isinstance(word, str), "word must be a str"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    """Lower-case words (camelCase split, plurals folded, >= 3 chars) plus their synonym group ids."""
    assert isinstance(text, str), "text must be a str"
    groups = _groups()
    out: set[str] = set()
    for raw in _WORD.findall(_CAMEL.sub(" ", text).lower())[:_MAX_WORDS]:
        if len(raw) < 3:
            continue
        w = _fold(raw)
        out.add(w)
        for key in (raw, w):           # bounded: 2 spellings
            for gid in groups.get(key, ()):
                out.add("group:" + gid)
    return out


def ask_words(ask: str, wants: list | None) -> set[str]:
    """The content words of the ask and of the frame's wants, stop words removed, groups added."""
    assert isinstance(ask, str), "ask must be a str"
    assert wants is None or isinstance(wants, list), "wants must be a list or None"
    text = " ".join([ask, *[str(w) for w in (wants or [])[:8]]])
    return {t for t in _tokens(text) if t not in _STOP}


def field_words(name: str, label: str, path: str) -> set[str]:
    """A field's own words: its label, its name and the tail of its source path."""
    assert isinstance(name, str) and isinstance(label, str), "name + label must be str"
    assert isinstance(path, str), "path must be a str"
    tail = path.replace("[]", "").rsplit(".", 1)[-1]
    return _tokens(" ".join([label, name.replace("_", " "), tail.replace("_", " ")]))


# a closed vocabulary (a kind / status word: tide H / L, a weather word) is never the asked QUANTITY —
# the ask's "tides" names the list, and a kind word shows through the form's own words (High / Low)
_NOT_ASKED_TYPES = frozenset({"category", "bool", "url", "identifier"})


def asked_fields(fields: list, ask: str, wants: list | None = None) -> list[str]:
    """Names of the fields the ask (or the frame's wants) names — numbers, times, dates and text
    names, never a closed-vocabulary column — in record order; [] when none."""
    assert isinstance(fields, list), "fields must be a list"
    assert isinstance(ask, str), "ask must be a str"
    words = ask_words(ask, wants)
    if not words:
        return []
    out: list[str] = []
    for f in fields[:24]:              # bounded: a record holds <= 24 fields
        if f.type in _NOT_ASKED_TYPES:
            continue
        if field_words(f.name, f.label or "", f.path or "") & words:
            out.append(f.name)
    return out
