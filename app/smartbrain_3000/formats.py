"""Textual source parsers for the NI card flow (§3 http_json ``format``).

The engine's pipeline grammar consumes JSON-shaped Python values (dicts, lists,
primitives). This module turns CSV / RSS+Atom / XML / plain text into that same
shape so the existing walker + extract/transform grammar stays the ONE code
path — CSV columns become dict keys under ``rows``, feeds become ``items``,
XML becomes nested dicts, text becomes ``{"text": ...}``.

Every parser is stdlib-only, pure, and BOUNDED — no unbounded string / list /
dict growth reaches the pipeline. Untrusted XML documents that name a DOCTYPE
or ENTITY declaration are refused BEFORE parse (entity-expansion attacks); the
stdlib ElementTree is otherwise entity-safe.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET

from . import feeds

# --- bounds (every one refuses with a clean FormatError when exceeded) ---------

MAX_TEXT_CHARS = 200_000          # plain-text bodies (>= _MAX_SUMMARY per item)
MAX_CSV_ROWS = 500                # N data rows kept (a date series keeps the newest)
MAX_CSV_COLUMNS = 60              # a wide spreadsheet is still a spreadsheet
MAX_CSV_CELL = 4_000              # per-cell character cap
MAX_XML_DEPTH = 12                # element nesting cap
MAX_XML_NODES = 5_000             # total elements walked before refusal
MAX_XML_ATTRS = 20                # attributes carried per element
MAX_XML_TEXT = 20_000             # per-element text cap

# The delimiters the sniffer chooses between — pipe / semicolon / tab / comma
# cover the shapes the Library's csv sources ship (US Census, healthdata.gov,
# state open-data portals). ``newline=""`` lets csv.reader own line handling.
_CSV_DELIMITERS = ",;\t|"

# Bare DOCTYPE / ENTITY declarations are refused for feed AND xml before parse:
# stdlib ElementTree resolves entities inline (a self-referential entity is the
# classic billion-laughs shape). Comment/whitespace between ``<?xml`` and the
# root element is fine — this pattern is deliberately narrow.
_XML_UNSAFE_DECL_RE = re.compile(rb"<!(DOCTYPE|ENTITY)\b", re.IGNORECASE)

# XML tag / attribute local-names get slugified to the pipeline's key grammar
# (``[A-Za-z_][A-Za-z0-9_-]``). Names that don't fit at all are dropped rather
# than smuggled through — the walker's dead-key report would refuse them anyway.
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


class FormatError(Exception):
    """A textual response did not parse to a bounded, walkable shape."""


# --- CSV --------------------------------------------------------------------------

def parse_csv(text: str) -> dict:
    """Parse ``text`` as CSV → ``{"columns": [...], "rows": [{col: value}, ...]}``.

    Header row is required (a headerless CSV is refused — the pipeline needs
    named fields). Delimiter is sniffed among ``,;\\t|``. Numeric-looking cells
    stay strings — the extract/transform grammar owns coercion, and the CSV
    parser has no schema. A time series whose first column is a date running
    oldest→newest is returned NEWEST FIRST, so ``rows[0]`` is the latest value
    (field 2026-09-28: FRED unemployment showed 1948's 3.4). Row cap =
    MAX_CSV_ROWS (the newest rows are kept); cell length cap = MAX_CSV_CELL.
    """
    assert isinstance(text, str), "text required"
    assert len(text) >= 0, "text length invariant"
    if not text.strip():
        raise FormatError("empty CSV")
    delimiter = _sniff_csv_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise FormatError(f"CSV has no header row ({exc.__class__.__name__})") from None
    columns = _slug_columns(header)
    if not columns:
        raise FormatError("CSV header row has no usable columns")
    rows = [_csv_row(columns, raw) for raw in reader]  # bounded by netguard's body cap
    first = columns[0]
    dates = [str(r.get(first) or "") for r in rows if r.get(first)]
    if len(dates) > 1 and all(_DATE_RE.match(d) for d in dates[:50]) and dates[0] < dates[-1]:
        rows.reverse()  # a series stored oldest first: the latest value leads
    return {"columns": columns, "rows": rows[:MAX_CSV_ROWS]}


_DATE_RE = re.compile(r"\d{4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2}(:\d{2})?)?")


def _sniff_csv_delimiter(text: str) -> str:
    """Return the delimiter that yields the most cells in the first non-empty line."""
    assert isinstance(text, str), "text required"
    assert text, "text non-empty"
    sample = text[:4096]
    # csv.Sniffer works but throws on ambiguous inputs; a deterministic
    # count-columns tie-break keeps refusals rare and behaviour predictable.
    first = next((ln for ln in sample.splitlines() if ln.strip()), "")
    best = ","
    best_count = -1
    for cand in _CSV_DELIMITERS:  # bounded by len(_CSV_DELIMITERS)
        count = first.count(cand)
        if count > best_count:
            best = cand
            best_count = count
    return best


def _slug_columns(header: list[str]) -> list[str]:
    """Slugify header names → walker-safe keys, deduping collisions with a suffix."""
    assert isinstance(header, list), "header must be a list"
    assert MAX_CSV_COLUMNS > 0, "column cap positive"
    out: list[str] = []
    seen: set[str] = set()
    for raw in header[:MAX_CSV_COLUMNS]:
        base = _slug_key(str(raw or ""))
        if not base:
            base = f"col_{len(out) + 1}"
        name = base
        n = 2
        while name in seen:  # bounded by MAX_CSV_COLUMNS
            name = f"{base}_{n}"
            n += 1
        seen.add(name)
        out.append(name)
    return out


def _csv_row(columns: list[str], raw: list[str]) -> dict:
    """One CSV data row → {col: cell} with cell length capped."""
    assert isinstance(columns, list) and isinstance(raw, list), "args required"
    out: dict = {}
    for i, col in enumerate(columns):  # bounded by MAX_CSV_COLUMNS
        cell = raw[i] if i < len(raw) else ""
        out[col] = str(cell)[:MAX_CSV_CELL]
    return out


def _slug_key(raw: str) -> str:
    """Slug ``raw`` to ``[A-Za-z_][A-Za-z0-9_-]*``; return "" when nothing survives."""
    assert isinstance(raw, str), "raw required"
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", raw.strip()).strip("_")
    if not cleaned:
        return ""
    if not cleaned[0].isalpha() and cleaned[0] != "_":
        cleaned = "_" + cleaned
    match = _KEY_RE.match(cleaned)
    return match.group(0)[:60] if match else ""


# --- feed (RSS 2.0 + Atom) --------------------------------------------------------

def parse_feed(text: str) -> dict:
    """Delegate to ``feeds.parse_feed`` after refusing DOCTYPE/ENTITY.

    ``feeds.parse_feed`` uses stdlib ElementTree, which resolves entities inline —
    the entity-declaration pre-check here is the defence against billion-laughs
    style expansion. Returns ``{"title", "items": [{title, link, summary, ...}]}``.
    """
    assert isinstance(text, str), "text required"
    assert len(text) >= 0, "text length invariant"
    _refuse_doctype(text)
    try:
        return feeds.parse_feed(text)
    except feeds.FeedError as exc:
        raise FormatError(str(exc)) from None


# --- XML --------------------------------------------------------------------------

def parse_xml(text: str) -> dict:
    """Parse ``text`` as XML → xmltodict-shaped nested dict.

    ``{root_tag: {"@attr": ..., "child": [...]}}``: each element becomes a dict
    with optional ``@<attr>`` keys, an optional ``_text`` key, and one entry per
    child element (a list when the child tag repeats, a dict when unique). Local
    tag names (namespaces stripped) are slugified to the walker's key grammar —
    names that don't fit are dropped so the pipeline never sees them. Walked
    iteratively (no recursion) with explicit depth + node bounds.
    """
    assert isinstance(text, str), "text required"
    assert MAX_XML_DEPTH > 0, "depth cap positive"
    _refuse_doctype(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FormatError(f"XML did not parse: {exc}") from None
    root_key = _slug_key(_local_tag(root.tag)) or "root"
    root_dict = _xml_shell(root)
    # (element, depth, parent_dict_it_lives_in) so children can be attached
    # after their own shell is complete. Iterative walk, fixed upper bound.
    stack: list[tuple[ET.Element, int, dict]] = [(root, 1, root_dict)]
    node_count = 0
    for _ in range(MAX_XML_NODES + 1):  # fixed upper bound (P10 #2)
        if not stack:
            return {root_key: root_dict}
        el, depth, own = stack.pop()
        node_count += 1
        if node_count > MAX_XML_NODES:
            raise FormatError(f"XML has more than {MAX_XML_NODES} elements")
        if depth > MAX_XML_DEPTH:
            raise FormatError(f"XML deeper than {MAX_XML_DEPTH} levels")
        for child in list(el):  # bounded by parser input; XML_NODES caps the total
            key = _slug_key(_local_tag(child.tag))
            if not key:
                continue
            sub = _xml_shell(child)
            _attach_child(own, key, sub)
            stack.append((child, depth + 1, sub))
    raise FormatError(f"XML has more than {MAX_XML_NODES} elements")


def _xml_shell(el: ET.Element) -> dict:
    """One element's own attributes + text, without descending into its children."""
    assert isinstance(el, ET.Element), "element required"
    out: dict = {}
    for i, (name, value) in enumerate(el.attrib.items()):  # bounded per element
        if i >= MAX_XML_ATTRS:
            break
        key = _slug_key(_local_tag(name))
        if key:
            out[f"@{key}"] = str(value)[:MAX_XML_TEXT]
    text = (el.text or "").strip()
    if text:
        out["_text"] = text[:MAX_XML_TEXT]
    return out


def _attach_child(parent: dict, key: str, child: dict) -> None:
    """Add ``child`` under ``key`` — promote to a list on the second occurrence."""
    assert isinstance(parent, dict) and isinstance(key, str), "args required"
    if key not in parent:
        parent[key] = child
        return
    prior = parent[key]
    if isinstance(prior, list):
        prior.append(child)
    else:
        parent[key] = [prior, child]


def _local_tag(tag: str) -> str:
    """Strip an ``{ns}local`` prefix; return the local name (or the raw tag)."""
    assert isinstance(tag, str), "tag required"
    return tag.split("}", 1)[1] if tag.startswith("{") and "}" in tag else tag


# --- plain text -------------------------------------------------------------------

def parse_text(text: str) -> dict:
    """Return ``{"text": "..."}`` bounded to MAX_TEXT_CHARS. A whitespace table (a header line —
    ``#`` allowed, as NOAA's buoy and station files use — then rows with the same number of
    columns; a second ``#`` line of units is skipped) also comes back as ``columns`` + ``rows``,
    so its values can be picked like any CSV's; the text is then kept short."""
    assert isinstance(text, str), "text required"
    assert MAX_TEXT_CHARS > 0, "text cap positive"
    table = _whitespace_table(text)
    if table is None:
        return {"text": text[:MAX_TEXT_CHARS]}
    return {"text": text[:2000], **table}


def _whitespace_table(text: str) -> dict | None:
    lines = [ln for ln in text.splitlines()[:MAX_CSV_ROWS + 5] if ln.strip()]
    if len(lines) < 4:
        return None
    header = lines[0].lstrip("#").split()
    body = [ln for ln in lines[1:] if not ln.startswith("#")]
    if len(header) < 3 or len(body) < 3 or any(len(ln.split()) != len(header) for ln in body[:20]):
        return None
    columns = _slug_columns(header)
    rows = [dict(zip(columns, ln.split(), strict=False)) for ln in body if len(ln.split()) == len(header)]
    return {"columns": columns, "rows": rows[:MAX_CSV_ROWS]}


# --- shared defence ---------------------------------------------------------------

def _refuse_doctype(text: str) -> None:
    """Refuse XML/feed bytes that name a DOCTYPE or ENTITY (entity-expansion defence)."""
    assert isinstance(text, str), "text required"
    assert _XML_UNSAFE_DECL_RE is not None, "regex loaded"
    # the whole body (already size-capped by netguard): a head-only scan is bypassed by
    # padding the declaration past it with whitespace or comments
    if _XML_UNSAFE_DECL_RE.search(text.encode("utf-8", "replace")):
        raise FormatError("XML DOCTYPE / ENTITY declarations are refused")


# --- sniffing (paste-URL detection) -----------------------------------------------

def sniff_format(content_type: str, sample: str) -> str:
    """Guess the format of a fetched body: ``json`` / ``feed`` / ``xml`` / ``csv`` / ``text``.

    Content-type is a hint, not a trust signal — the first bytes decide when the
    header is ambiguous or wrong. Used by the flow's paste-URL entry point so a
    user who pastes an RSS or CSV link gets the right parser (Library candidates
    supply their format directly and skip this).
    """
    assert isinstance(content_type, str) and isinstance(sample, str), "args required"
    ct = content_type.split(";", 1)[0].strip().lower()
    stripped = sample.lstrip()
    head = stripped[:512]
    if stripped.startswith(("{", "[")) or "json" in ct:
        return "json"
    lower_head = head.lower()
    if lower_head.startswith("<?xml"):
        return "feed" if "<rss" in lower_head or "<feed" in lower_head else "xml"
    if lower_head.startswith(("<rss", "<feed")):
        return "feed"
    if "rss" in ct or "atom" in ct:
        return "feed"
    if ct.startswith(("text/xml", "application/xml")) or lower_head.startswith("<"):
        return "xml"
    if "csv" in ct or _looks_like_csv(head):
        return "csv"
    return "text"


def _looks_like_csv(head: str) -> bool:
    """Return True when ``head`` is a plausible CSV (>=1 delimiter in the first line)."""
    assert isinstance(head, str), "head required"
    first = next((ln for ln in head.splitlines() if ln.strip()), "")
    return any(first.count(d) >= 1 for d in _CSV_DELIMITERS)
