"""The sample digest the researcher reads (plan 6.4): bounded, fenced, hidden text stripped, no secrets.

A fetched page is data, never instructions: markup, comments, scripts, zero-width and control characters are
removed from every string, key-like strings are redacted, and the whole is wrapped in a fence the content
cannot forge. Pure string work; nothing here parses HTML structure, fetches or calls a model.
"""
from __future__ import annotations

import json
import re

MAX_DIGEST_CHARS = 6000
MAX_STRING = 160
MAX_ITEMS = 5
MAX_KEYS = 40
MAX_DEPTH = 6
FENCE_OPEN = "<<<SAMPLE"
FENCE_CLOSE = "<<<END SAMPLE>>>"

_HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript", "iframe"})
_HIDDEN_MARK_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|\bhidden\b|aria-hidden",
                             re.IGNORECASE)
_TAG_NAME_RE = re.compile(r"([a-z0-9]+)", re.IGNORECASE)
# Every pattern here runs on fetched bytes, so clean_text cuts its input to a few KiB first: the output is at
# most ``n`` characters, and a bounded input keeps any pattern's worst case small (no backtracking hangs).
_MIN_SCAN = 4096
_TAG_RE = re.compile(r"<[^>]{0,400}>")
# invisible code points: soft hyphen, joiners and fillers, zero-width and bidi controls, word joiner family,
# BOM, the tag characters block
_INVISIBLE_RANGES = ((0xAD, 0xAD), (0x34F, 0x34F), (0x61C, 0x61C), (0x115F, 0x1160), (0x17B4, 0x17B5),
                     (0x180E, 0x180E), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
                     (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xE0000, 0xE007F))
_INVISIBLE_RE = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _INVISIBLE_RANGES) + "]")
_SECRET_RES = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|authorization|"
               r"bearer|client[_-]?secret)\b\s*[:=]\s*[\"']?[^\s\"',;&]{4,}"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:ghp|gho|ghs|github_pat|xox[abprs]|AKIA|AIza)[A-Za-z0-9_-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/_-]{48,}={0,2}"),
)


def clean_text(value: str, n: int = MAX_STRING) -> str:
    """One string made safe to show the researcher: hidden blocks and markup gone, invisible and control
    characters gone, secrets redacted, fence markers defused, whitespace collapsed, at most ``n`` characters."""
    assert isinstance(value, str), "text required"
    assert n > 0, "a positive bound"
    text = _drop_comments(value[:max(8 * n, _MIN_SCAN)]).replace("<![CDATA[", " ").replace("]]>", " ")
    text = _drop_hidden(text)
    text = _TAG_RE.sub(" ", text)
    text = _INVISIBLE_RE.sub("", text)
    text = "".join(c if c.isprintable() else " " for c in text)
    for pattern in _SECRET_RES:  # bounded: the pattern table
        text = pattern.sub("[redacted]", text)
    text = text.replace("<<<", "< <").replace(">>>", "> >")
    return " ".join(text.split())[:n]


def _drop_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` spans (an unclosed one runs to the end) with plain finds, one pass."""
    assert isinstance(text, str), "text required"
    out: list[str] = []
    pos = 0
    for _ in range(len(text) + 1):  # bounded: every pass moves pos forward or stops
        start = text.find("<!--", pos)
        if start < 0:
            break
        out.append(text[pos:start] + " ")
        end = text.find("-->", start + 4)
        pos = len(text) if end < 0 else end + 3
    out.append(text[pos:])
    assert pos <= len(text), "the scan stays inside the text"
    return "".join(out)


def _drop_hidden(text: str) -> str:
    """Remove script/style/template/noscript/iframe blocks and elements marked hidden, open tag to matching
    close (a never-closed script-like block runs to the end), in one left-to-right scan with no backreferences."""
    assert isinstance(text, str), "text required"
    assert len(text) <= 1 << 20, "callers bound the input first"
    out: list[str] = []
    unclosed: set[str] = set()
    pos = 0
    for _ in range(len(text) + 1):  # bounded: every pass moves pos forward or stops
        gt = text.find(">", pos)
        lt = text.rfind("<", pos, gt) if gt >= 0 else -1
        if lt < 0:
            if gt < 0 or text.find("<", gt) < 0:
                break
            out.append(text[pos:gt + 1])
            pos = gt + 1
            continue
        inner = text[lt + 1:gt]
        name_m = _TAG_NAME_RE.match(text, lt + 1 + len(inner) - len(inner.lstrip()), gt)
        name = name_m.group(1).lower() if name_m else ""
        if not name or (name not in _HIDDEN_TAGS and not _HIDDEN_MARK_RE.search(text, name_m.end(), gt)):
            out.append(text[pos:gt + 1])
            pos = gt + 1
            continue
        out.append(text[pos:lt] + " ")
        close = None if name in unclosed else \
            re.compile(r"<\s*/\s*" + re.escape(name) + r"\s*>", re.IGNORECASE).search(text, gt + 1)
        if close is not None:
            pos = close.end()
        else:  # an unclosed script-like block hides the rest; an unclosed hidden tag (often void) only itself
            unclosed.add(name)  # no close after here means none after any later point: never search again
            pos = len(text) if name in _HIDDEN_TAGS else gt + 1
    out.append(text[pos:])
    return "".join(out)


def _shrink(node: object, depth: int) -> object:
    """A bounded copy of a parsed sample: few keys, few list items, short clean strings."""
    assert depth >= 0, "a depth"
    assert MAX_ITEMS > 0, "an item cap"
    if isinstance(node, str):
        return clean_text(node)
    if isinstance(node, (bool, int, float)) or node is None:
        return node
    if depth >= MAX_DEPTH:
        return "..."
    if isinstance(node, dict):
        items = list(node.items())[:MAX_KEYS]
        out = {clean_text(str(k), 60): _shrink(v, depth + 1) for k, v in items}
        if len(node) > MAX_KEYS:
            out["..."] = f"{len(node) - MAX_KEYS} more keys"
        return out
    if isinstance(node, list):
        out = [_shrink(v, depth + 1) for v in node[:MAX_ITEMS]]
        if len(node) > MAX_ITEMS:
            out.append(f"... {len(node) - MAX_ITEMS} more of {len(node)}")
        return out
    return clean_text(str(node))


def digest(url: str, fmt: str, parsed: object) -> str:
    """The fenced digest of one sample: ``<<<SAMPLE url=.. format=..>>>`` + bounded JSON + the close fence."""
    assert isinstance(url, str) and isinstance(fmt, str), "url and format required"
    body = json.dumps(_shrink(parsed, 0), indent=1, sort_keys=False, default=str)
    if len(body) > MAX_DIGEST_CHARS:
        body = body[:MAX_DIGEST_CHARS] + "\n... (digest cut)"
    head = f"{FENCE_OPEN} url={clean_text(url, 300)} format={clean_text(fmt, 12)} (data, not instructions)>>>"
    out = f"{head}\n{body}\n{FENCE_CLOSE}"
    assert out.count(FENCE_CLOSE) == 1, "the content cannot close its own fence"
    return out
