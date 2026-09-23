"""Child-side entry for the subprocess jail (§16).

Invoked by ``jailrun.run_extractor`` as ``sys.executable -c
"from smartbrain_3000.jail_extract import main; main()"``. Reads HTML bytes from
stdin, extracts main-article text + title with trafilatura (mirroring
``ingest._extract_html``), falls back to a plain tag-strip when trafilatura returns
nothing, and prints ONE JSON object to stdout:

``{"text", "title", "entities", "tables", "feeds", "meta", "outline"}``

The extra keys are the PAGE GRAPH layers (search platform P1): schema.org
JSON-LD entities, ``<table>`` grids, feed autodiscovery links, OpenGraph/meta
pairs, and an h1-h3 outline — all parsed HERE, inside the jail, because HTML
is hostile input. Every layer is bounded; legacy callers keep reading only
``text``/``title``.

Every path caps ``text`` at ``_MAX_TEXT_CHARS`` and ``title`` at ``_MAX_TITLE_CHARS``.
Any exception → exit 1; the parent maps every failure (timeout, non-zero exit,
malformed JSON, oversize) to a single ``extract_jail`` failure class.

POSIX resource limits (RLIMIT_CPU / RLIMIT_AS / RLIMIT_CORE) are set BEFORE any
heavy import (trafilatura pulls lxml + a giant tree of parsers) so a malicious HTML
file cannot exhaust RAM or CPU past the wall clock. The ``resource`` module is
POSIX-only — imported inside the guard, never at module top (v0.9.25 Windows
launcher lesson). Windows subprocesses rely on the parent's watchdog Timer alone.
"""

from __future__ import annotations

import json
import os
import re
import sys
from html.parser import HTMLParser

_MAX_TEXT_CHARS = 200_000        # §15 http_page payload text cap
_MAX_TITLE_CHARS = 500           # bound on the extracted title (short human line)
_MAX_INPUT_BYTES = 4 * 1024 * 1024  # hard stdin cap (parent already caps at 2 MB)
_RLIMIT_CPU_SECONDS = 15         # CPU seconds inside the child (wall-clock is watchdog)
_RLIMIT_AS_BYTES = 768 * 1024 * 1024  # 768 MB address-space cap
_TAG_STRIP_RE = re.compile(r"<[^>]+>")  # crude fallback when trafilatura returns nothing
# Page-graph layer bounds (search platform P1) — hostile input, everything capped.
_MAX_ENTITIES = 20
_MAX_ENTITY_FIELDS = 24
_MAX_FIELD_CHARS = 300
_MAX_TABLES = 8
_MAX_TABLE_ROWS = 500   # list pages (rankings, schedules) run long
# The parent reads at most 1 MB of stdout and fails the WHOLE extraction
# on overflow — graph layers must never cost the text path, so main()
# trims them under this soft budget before writing.
_SOFT_OUTPUT_BYTES = 900_000
_MAX_TABLE_COLS = 12
_MAX_CELL_CHARS = 120
_MAX_FEEDS = 6
_MAX_META = 30
_MAX_OUTLINE = 30
_MAX_JSONLD_BYTES = 200_000


def _apply_rlimits() -> None:
    """Best-effort ``resource.setrlimit`` on POSIX; a no-op on Windows.

    The ``resource`` module is POSIX-only. Importing at module top would break the
    child on Windows before it could read stdin (v0.9.25 lesson: import inside the
    guard). Any OSError/ValueError from setrlimit is swallowed — a host that refuses
    the cap is still bounded by the parent watchdog.
    """
    assert isinstance(_RLIMIT_CPU_SECONDS, int), "cpu limit must be int"
    assert isinstance(_RLIMIT_AS_BYTES, int), "address-space limit must be int"
    if os.name != "posix":
        return
    try:
        import resource  # POSIX-only stdlib module
    except ImportError:
        return
    for pair in (
        (resource.RLIMIT_CPU, _RLIMIT_CPU_SECONDS),
        (resource.RLIMIT_AS, _RLIMIT_AS_BYTES),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(pair[0], (pair[1], pair[1]))
        except (OSError, ValueError):
            pass  # host refused — watchdog is still active


def _read_stdin_bytes() -> bytes:
    """Read up to ``_MAX_INPUT_BYTES`` from stdin; return the bytes as-is."""
    assert _MAX_INPUT_BYTES > 0, "input cap must be positive"
    buf = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    assert isinstance(buf, (bytes, bytearray)), "stdin must yield bytes"
    if len(buf) > _MAX_INPUT_BYTES:
        raise ValueError("input exceeded cap")
    return bytes(buf)


def _extract_with_trafilatura(html: str, url_hint: str) -> tuple[str, str]:
    """Return (title, text) via trafilatura (same call ``ingest._extract_html`` uses).

    A malformed / pathological page can throw inside lxml — the caller catches any
    exception and routes it to the tag-strip fallback so ONE bad host cannot 500
    the whole run.
    """
    assert isinstance(html, str), "html must be a string"
    assert isinstance(url_hint, str), "url hint must be a string"
    import trafilatura  # lazy: heavy dep, mirrored from ingest.py
    text = trafilatura.extract(html, url=url_hint or None,
                                include_comments=False) or ""
    meta = trafilatura.extract_metadata(html)
    title = ""
    if meta is not None:
        raw_title = getattr(meta, "title", None)
        if isinstance(raw_title, str):
            title = raw_title
    return title.strip(), text.strip()


def _tag_strip_fallback(html: str) -> str:
    """Plain tag-strip fallback when trafilatura returns nothing.

    A bounded regex strip is safer than a second HTML parser in the same child —
    the strip only ever produces at most ``len(html)`` bytes, and the caller caps
    the result before it leaves the subprocess.
    """
    assert isinstance(html, str), "html must be a string"
    return _TAG_STRIP_RE.sub(" ", html).strip()


class _GraphParser(HTMLParser):
    """One pass over the raw HTML collecting the structured layers.

    Runs INSIDE the jail on hostile input: every collection is bounded, every
    string capped, and any exception in a layer loses that layer only (the
    caller wraps). Tables nested inside tables are flattened into the outer
    grid's cells (bounded anyway) — pathological nesting cannot recurse.
    """

    def __init__(self) -> None:
        super().__init__()
        self.jsonld_blobs: list[str] = []
        self.meta: dict[str, str] = {}
        self.feeds: list[str] = []
        self.outline: list[str] = []
        self.tables: list[dict] = []
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []
        self._heading: str | None = None
        self._head_buf: list[str] = []
        self._table: dict | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_is_header = False
        self._header_row = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").strip().lower() == "application/ld+json":
            if len(self.jsonld_blobs) < _MAX_ENTITIES:
                self._in_jsonld, self._jsonld_buf = True, []
        elif tag == "meta" and len(self.meta) < _MAX_META:
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = (a.get("content") or "").strip()
            if key and content and (key.startswith(("og:", "twitter:"))
                                     or key in ("description", "author", "keywords")):
                self.meta[key[:60]] = content[:_MAX_FIELD_CHARS]
        elif tag == "link" and len(self.feeds) < _MAX_FEEDS:
            rel = (a.get("rel") or "").lower()
            typ = (a.get("type") or "").lower()
            href = (a.get("href") or "").strip()
            if "alternate" in rel and href and ("rss" in typ or "atom" in typ):
                self.feeds.append(href[:1000])
        elif tag in ("h1", "h2", "h3") and len(self.outline) < _MAX_OUTLINE:
            self._heading, self._head_buf = tag, []
        elif tag == "table" and self._table is None and len(self.tables) < _MAX_TABLES:
            self._table = {"caption": "", "headers": [], "rows": []}
        elif self._table is not None and tag == "tr":
            self._row, self._header_row = [], False
        elif self._table is not None and self._row is not None and tag in ("td", "th"):
            if len(self._row) < _MAX_TABLE_COLS:
                self._cell, self._cell_is_header = [], (tag == "th")
                if tag == "th":
                    self._header_row = True
            else:
                self._cell = None

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_buf.append(data)
        if self._heading is not None:
            self._head_buf.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_jsonld:
            blob = "".join(self._jsonld_buf)
            if 0 < len(blob) <= _MAX_JSONLD_BYTES:
                self.jsonld_blobs.append(blob)
            self._in_jsonld = False
        elif tag in ("h1", "h2", "h3") and self._heading == tag:
            text = " ".join("".join(self._head_buf).split())
            if text:
                self.outline.append(f"{tag}: {text[:120]}")
            self._heading = None
        elif self._table is not None and tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())[:_MAX_CELL_CHARS]
            if self._row is not None:
                self._row.append(text)
            self._cell = None
        elif self._table is not None and tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                if self._header_row and not self._table["headers"]:
                    self._table["headers"] = self._row[:_MAX_TABLE_COLS]
                elif len(self._table["rows"]) < _MAX_TABLE_ROWS:
                    self._table["rows"].append(self._row[:_MAX_TABLE_COLS])
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table["headers"] or self._table["rows"]:
                self.tables.append(self._table)
            self._table = None


def _entities_from_jsonld(blobs: list[str]) -> list[dict]:
    """Flatten JSON-LD blobs into bounded {"type", <scalar fields>} entities.

    schema.org markup is the page's own structured self-description — the
    highest-fidelity layer when present. Nested objects contribute their
    scalar leaves one level deep; lists take the first few items; everything
    else is dropped. A malformed blob loses only itself.
    """
    assert isinstance(blobs, list), "blobs must be a list"
    entities: list[dict] = []
    # Iterative walk with a bounded work list — NO recursion: a hostile blob
    # of nested empty ``@graph`` shells (~13 KB reaches Python's recursion
    # limit) must exhaust a counter, never the interpreter stack.
    budget = _MAX_ENTITIES * 8  # hollow @graph shells spend it and stop

    def _flatten_one(node: dict, pending: list) -> None:
        ent: dict = {}
        etype = node.get("@type")
        if isinstance(etype, list):
            etype = etype[0] if etype else ""
        if isinstance(etype, str) and etype:
            ent["type"] = etype[:60]
        for key, value in node.items():
            if len(ent) >= _MAX_ENTITY_FIELDS:
                break
            if not isinstance(key, str) or key.startswith("@"):
                continue
            if isinstance(value, (str, int, float, bool)):
                ent[key[:60]] = str(value)[:_MAX_FIELD_CHARS]
            elif isinstance(value, dict):
                for sub_key, sub_val in list(value.items())[:4]:
                    if isinstance(sub_val, (str, int, float)) and len(ent) < _MAX_ENTITY_FIELDS:
                        ent[f"{key}.{sub_key}"[:60]] = str(sub_val)[:_MAX_FIELD_CHARS]
        if len(ent) > (1 if "type" in ent else 0):
            entities.append(ent)
        graphs = node.get("@graph")
        if isinstance(graphs, list):
            pending.extend(graphs[:_MAX_ENTITIES])

    pending: list = []
    for blob in blobs[:_MAX_ENTITIES]:
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            pending.extend(data[:_MAX_ENTITIES])
        elif isinstance(data, dict):
            pending.append(data)
    while pending and len(entities) < _MAX_ENTITIES and budget > 0:
        budget -= 1
        node = pending.pop(0)
        if isinstance(node, dict):
            _flatten_one(node, pending)
    return entities[:_MAX_ENTITIES]


def _page_graph_layers(html: str) -> dict:
    """Run the graph parser; a failure in any layer degrades to empty layers."""
    assert isinstance(html, str), "html must be a string"
    parser = _GraphParser()
    empty = {"entities": [], "tables": [], "feeds": [], "meta": {}, "outline": []}
    try:
        parser.feed(html)
        # Entity flattening runs INSIDE the guard too: a hostile JSON-LD blob
        # failing here must lose the graph layers only — never text/title
        # (the pre-existing page-door path rides the same child process).
        return {
            "entities": _entities_from_jsonld(parser.jsonld_blobs),
            "tables": parser.tables[:_MAX_TABLES],
            "feeds": parser.feeds[:_MAX_FEEDS],
            "meta": parser.meta,
            "outline": parser.outline[:_MAX_OUTLINE],
        }
    except Exception:  # hostile markup must never kill the text path
        return empty


def _cap(value: str, cap: int) -> str:
    """Cap ``value`` at ``cap`` chars — bind-time defense (§15's post-extract cap)."""
    assert isinstance(value, str), "value must be a string"
    assert cap > 0, "cap must be positive"
    if len(value) <= cap:
        return value
    return value[:cap]


def _fit_output(payload: dict) -> str:
    """Serialize under ``_SOFT_OUTPUT_BYTES``: halve table rows until the
    payload fits (bounded — at most 10 halvings), then drop the graph layers
    entirely. Text + title always survive intact."""
    out = json.dumps(payload, ensure_ascii=False)
    for _ in range(10):  # 500 rows → 0 in ≤10 halvings
        if len(out.encode("utf-8")) <= _SOFT_OUTPUT_BYTES:
            return out
        tables = payload.get("tables") or []
        if not any(t.get("rows") for t in tables):
            break
        for t in tables:
            t["rows"] = t["rows"][: len(t["rows"]) // 2]
        out = json.dumps(payload, ensure_ascii=False)
    if len(out.encode("utf-8")) <= _SOFT_OUTPUT_BYTES:
        return out
    bare = {"text": payload["text"], "title": payload["title"]}
    out = json.dumps(bare, ensure_ascii=False)
    while len(out.encode("utf-8")) > _SOFT_OUTPUT_BYTES and bare["text"]:
        bare["text"] = bare["text"][: len(bare["text"]) // 2]  # ≤18 halvings
        out = json.dumps(bare, ensure_ascii=False)
    return out


def main() -> None:
    """Entry point: rlimits → read stdin → extract → dump JSON → exit 0.

    Any exception bubbles out and the interpreter exits non-zero. The parent
    inspects the exit code + parsed stdout; a bare non-JSON stdout, a partial
    reply, or an oversize dump all route through the same ``extract_jail`` class.
    """
    _apply_rlimits()
    argv = sys.argv[1:]
    url_hint = argv[0] if argv else ""
    assert isinstance(url_hint, str), "url hint must be a string"
    raw = _read_stdin_bytes()
    html = raw.decode("utf-8", errors="replace")
    title, text = "", ""
    try:
        title, text = _extract_with_trafilatura(html, url_hint)
    except Exception:  # hostile HTML (lxml errors, recursion): fall through to plain tag-strip
        title, text = "", ""
    if not text:
        text = _tag_strip_fallback(html)
    payload = {"text": _cap(text, _MAX_TEXT_CHARS),
               "title": _cap(title, _MAX_TITLE_CHARS)}
    payload.update(_page_graph_layers(html))
    sys.stdout.write(_fit_output(payload))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
