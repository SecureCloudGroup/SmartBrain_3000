"""Page tables as card data (the ``table`` source format): which table of a page, named by what it says.

A web page's ``<table>`` grids are read by the jail (``jail_extract``; HTML never parses in-process) into
``{caption, heading, headers, rows}``. This module is the pure part that turns such a grid into rows a card
pipeline reads (the shape CSV has: ``{"columns": [...], "rows": [{column: cell}]}``) and that names a table
structurally, so a refresh finds the same table again or says plainly that it cannot:

* ``locator`` seals a table by its header names (plus the caption or heading it sits under, when it has one);
  never by position alone, because positions shift when a page adds a banner table.
* ``locate`` finds exactly one table for a locator: an exact header match, else a table that still holds every
  sealed header in order (a column added later is fine). Two candidates the sealed context cannot tell apart,
  or none, is a ``TableGone``: it never picks a different table.
* Only table cells are ever read. Text outside tables is not extracted as a value here or anywhere downstream.

Everything is bounded and string-only (no regex over a page's text): a page can hand over at most the jail's
8 tables x 500 rows x 12 columns x 120 characters.
"""
from __future__ import annotations

from . import formats

MAX_COLUMNS = 12            # the jail's own column cap
MAX_CELL_CHARS = 200        # the jail keeps 120; the parent never trusts that bound
MAX_HEADER_CHARS = 60
MAX_CONTEXT_CHARS = 80
MAX_ROWS = 500
MAX_TABLES = 8
MIN_COLUMNS = 2             # one named column is a list, not a table of data
PROMOTE_MIN_ROWS = 3        # a first row becomes the header only with at least two rows of data under it
GONE = "table_gone"
AMBIGUOUS = "table_ambiguous"


class TableGone(LookupError):
    """The sealed table cannot be found on the page (``kind`` ``table_gone``) or the page now holds several
    that fit equally (``table_ambiguous``). The engine reports it as a failed fetch; it never reads another table."""

    def __init__(self, kind: str, detail: str) -> None:
        assert kind in (GONE, AMBIGUOUS), "a closed kind"
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _cell(value: object, n: int = MAX_CELL_CHARS) -> str:
    """One cell as clean text of at most ``n`` characters: the input is cut before anything scans it."""
    assert n > 0, "a positive bound"
    return " ".join(value[: 4 * n].split())[:n] if isinstance(value, str) else \
        str(value)[:n] if isinstance(value, (int, float)) and not isinstance(value, bool) else ""


def norm(text: object, n: int = MAX_HEADER_CHARS) -> str:
    """A header or context as compared: lower-cased, whitespace collapsed, at most ``n`` characters."""
    assert n > 0, "a positive bound"
    return _cell(text, n).lower()


def _looks_like_label(cell: str) -> bool:
    """A header-ish cell: has a letter. A bare number or date-less figure is data."""
    return any(ch.isalpha() for ch in cell)


def read(raw: object) -> dict | None:
    """One jail table as ``{caption, heading, headers, rows}`` of clean strings, or None when it is not a data
    table: no usable grid, fewer than two named columns, or no data row. A grid with no header row (the jail
    reads ``<th>`` cells only) promotes its first row to the header when every cell of it is a distinct label and
    at least two rows of data follow. Rows are padded or cut to the header width."""
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        return None
    headers = [_cell(h, MAX_HEADER_CHARS) for h in (raw.get("headers") or [])[:MAX_COLUMNS]] if isinstance(raw.get("headers"), list) else []
    rows = [[_cell(c) for c in r[:MAX_COLUMNS]] for r in raw["rows"][:MAX_ROWS] if isinstance(r, list)]
    if not any(headers):
        first = rows[0] if rows else []
        distinct = {norm(c) for c in first}
        if len(rows) >= PROMOTE_MIN_ROWS and all(first) and all(_looks_like_label(c) for c in first) \
                and len(distinct) == len(first):
            headers, rows = first, rows[1:]
        else:
            return None
    if sum(1 for h in headers if h) < MIN_COLUMNS or not rows:
        return None
    width = len(headers)
    rows = [(r + [""] * width)[:width] for r in rows]
    return {"caption": _cell(raw.get("caption"), MAX_CONTEXT_CHARS), "heading": _cell(raw.get("heading"), MAX_CONTEXT_CHARS),
            "headers": headers, "rows": rows}


def read_all(raws: object) -> list[dict]:
    """The data tables of a jail payload's ``tables`` list, in page order (at most ``MAX_TABLES``)."""
    out: list[dict] = []
    for raw in (raws if isinstance(raws, list) else [])[:MAX_TABLES]:  # bounded by the jail
        table = read(raw)
        if table is not None:
            out.append(table)
    return out


def keys(headers: list[str]) -> list[str | None]:
    """The pipeline key of each column: its header slugged to the path grammar, a collision suffixed in column
    order, ``None`` for an unnamed column (it has no key, so a column inserted before it can never shift a name)."""
    assert isinstance(headers, list), "headers required"
    out: list[str | None] = []
    seen: set[str] = set()
    for raw in headers[:MAX_COLUMNS]:
        base = formats._slug_key(norm(raw)) if norm(raw) else ""
        if not base:
            out.append(None)
            continue
        name, n = base, 2
        while name in seen:  # bounded by MAX_COLUMNS
            name, n = f"{base}_{n}", n + 1
        seen.add(name)
        out.append(name)
    return out


def payload(table: dict) -> dict:
    """The rows a pipeline reads, shaped like a CSV: ``{"columns": [key...], "rows": [{key: cell}...]}`` in the
    page's own order (a table is not re-sorted: the first row is the page's first row)."""
    assert isinstance(table, dict) and table.get("headers"), "a read table"
    cols = keys(table["headers"])
    named = [(i, k) for i, k in enumerate(cols) if k]
    rows = [{k: (r[i] if i < len(r) else "") for i, k in named} for r in table["rows"][:MAX_ROWS]]
    return {"columns": [k for _, k in named], "rows": rows}


def context(table: dict) -> str:
    """What the table sits under: its caption, else the heading before it (lower-cased), else ``""``."""
    assert isinstance(table, dict), "a table"
    return norm(table.get("caption") or table.get("heading") or "", MAX_CONTEXT_CHARS)


def locator(table: dict) -> dict:
    """``{"headers": [...], "context": "..."}`` (context only when the table has one): the structural name a card
    seals. Header names are the table's named columns, normalised."""
    assert isinstance(table, dict) and table.get("headers"), "a read table"
    out: dict = {"headers": [norm(h) for h in table["headers"] if norm(h)][:MAX_COLUMNS]}
    ctx = context(table)
    if ctx:
        out["context"] = ctx
    return out


def validate_locator(loc: object, where: str = "spec.source.table") -> dict:
    """Shape-check a sealed locator (closed keys, bounded plain strings); returns it. Raises ``ValueError``."""
    if not isinstance(loc, dict) or set(loc) - {"headers", "context"} or "headers" not in loc:
        raise ValueError(f"{where} must be {{headers, context?}}")
    heads = loc["headers"]
    if not isinstance(heads, list) or not MIN_COLUMNS <= len(heads) <= MAX_COLUMNS:
        raise ValueError(f"{where}.headers must hold {MIN_COLUMNS}..{MAX_COLUMNS} header names")
    for h in heads:  # bounded by MAX_COLUMNS
        if not isinstance(h, str) or not 0 < len(h) <= MAX_HEADER_CHARS or not h.isprintable() or norm(h) != h:
            raise ValueError(f"{where}.headers must be normalised names of 1..{MAX_HEADER_CHARS} characters")
    ctx = loc.get("context")
    if ctx is not None and (not isinstance(ctx, str) or not 0 < len(ctx) <= MAX_CONTEXT_CHARS or not ctx.isprintable()
                            or norm(ctx, MAX_CONTEXT_CHARS) != ctx):
        raise ValueError(f"{where}.context must be a normalised line of 1..{MAX_CONTEXT_CHARS} characters")
    return loc


def _in_order(sealed: list[str], have: list[str]) -> bool:
    """True when every sealed header appears in ``have`` in the sealed order (other columns may sit between)."""
    it = iter(have)
    return all(any(h == x for x in it) for h in sealed)


def locate(tables: list[dict], loc: dict) -> tuple[int, dict]:
    """``(index, table)`` of the one table this locator names among the page's read ``tables``, else ``TableGone``.

    Exact header match first; with none, a table that still holds every sealed header in order. Among several
    candidates the sealed context (caption or heading) must single one out; otherwise the page is ambiguous."""
    validate_locator(loc)
    sealed = list(loc["headers"])
    pages = [(i, t, [norm(h) for h in t["headers"] if norm(h)]) for i, t in enumerate(tables[:MAX_TABLES])]
    for tier in (lambda have: have == sealed, lambda have: _in_order(sealed, have)):  # exact, then still-holds
        found = [(i, t) for i, t, have in pages if tier(have)]
        fit = len(found)
        if fit > 1 and loc.get("context"):
            found = [(i, t) for i, t in found if context(t) == loc["context"]]
        if len(found) == 1:
            return found[0]
        if fit > 1:  # several fit and the sealed context does not single one out: ambiguous, not gone
            raise TableGone(AMBIGUOUS, f"{fit} tables on the page fit the sealed columns equally")
    raise TableGone(GONE, "no table on the page has the sealed columns")
