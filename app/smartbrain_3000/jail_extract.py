"""Child-side entry for the subprocess jail (§16).

Invoked by ``jailrun.run_extractor`` as ``sys.executable -c
"from smartbrain_3000.jail_extract import main; main()"``. Reads HTML bytes from
stdin, decodes them (byte-order mark, then the page's declared ``<meta>`` charset,
then UTF-8), extracts main-article text + title with trafilatura (mirroring
``ingest._extract_html``), and prints ONE JSON object to stdout:

``{"text", "title", "entities", "tables", "feeds", "meta", "outline"}``

The extra keys are the PAGE GRAPH layers (search platform P1): schema.org
JSON-LD entities, ``<table>`` grids, feed autodiscovery links, OpenGraph/meta
pairs, and an h1-h3 outline — all parsed HERE, inside the jail, because HTML
is hostile input. Every layer is bounded; legacy callers keep reading only
``text``/``title``.

``text`` is the article text, then — after ``PAGE_EXTRA_MARK`` — the page's other
visible text (script/style/SVG never count) that the article extractor dropped,
bounded. An article extractor throws away small data widgets, and on status,
lottery, scoreboard and dashboard pages those widgets ARE the answer (field
2026-09-29: powerball.com's "Estimated Jackpot $409 Million" never reached the
reader). The same text feeds the build and every refresh, so a card built on a
widget value can re-read it. When trafilatura finds no article, the visible
text alone is the text — never the raw tag-strip of scripts and CSS (field:
a JS proof-of-work challenge was read as the page).

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
# The rest of the page's visible text, appended after the article text.
PAGE_EXTRA_MARK = "\n\n[more text on the page]\n"  # public: pagegraph splits on it
_MAX_EXTRA_CHARS = 8_000         # bound on the appended visible-text remainder
_MAX_BODY_CHARS = 120_000        # bound on visible text collected from the body
# Elements whose content a reader never sees as page text. ``select``,
# ``option`` and ``datalist`` carry filter choices, not the page's reading
# (field 2026-10-04: an aggregator's ``<option>Major outage</option>`` was
# grounding readings).
_INVISIBLE_TAGS = frozenset({"script", "style", "noscript", "template", "svg",
                             "title", "iframe", "object", "canvas", "math",
                             "select", "option", "datalist"})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "h1", "h2",
    "h3", "h4", "h5", "h6", "section", "article", "header", "footer", "nav",
    "main", "aside", "dt", "dd", "dl", "blockquote", "pre", "form", "fieldset",
    "figure", "figcaption", "hr", "address", "details", "summary",
    "label", "button", "caption"})
# HTML spec's "close a p element" start-tag list — the ONLY starts that
# implicitly close an open <p>. _BLOCK_TAGS is wider (it also drives body
# line breaks) and must not be used here: <br>, <label>, <button>, <td>,
# <th>, <tr>, <caption> never close a paragraph (field 2026-10-04:
# ``<p hidden>S1<br>S2</p>`` leaked S2 as visible because <br> popped the
# hidden <p>).
_P_CLOSERS = frozenset({
    "address", "article", "aside", "blockquote", "details", "dialog", "div",
    "dl", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav",
    "ol", "p", "pre", "search", "section", "table", "ul"})
_HIDDEN_STYLE_RE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                              re.IGNORECASE)
# Guard against malformed HTML stacking an unbounded number of open elements.
_MAX_OPEN_STACK = 256
# HTML void elements — no content and no end tag. A ``hidden``/``display:none``
# on them hides nothing (they carry no body text); pushing them onto the
# open stack leaks the state across every following sibling (field 2026-10-04:
# one ``<img style='display:none'>`` on githubstatus and the USWDS banner
# ``<img aria-hidden>`` on every .gov page zeroed the rest of the body).
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img",
                        "input", "link", "meta", "source", "track", "wbr"})
# React Server Components streaming writes real body content into a template
# ``<div hidden id="S:N">`` / ``id="B:N">`` that is swapped in on hydration —
# the content IS the page (field 2026-10-04: wmata lost ~21 kB of real
# status alerts to the stock [hidden] rule). Any other hidden div stays hidden.
_REACT_STREAM_ID_RE = re.compile(r"^[SB]:\d+$")
# HTML's implied end tags: a new start tag closes any open peer of the same
# category sitting on top of the stack (field 2026-10-04: ``<li hidden>a<li>b``,
# ``<p hidden>x<p>y``, ``<tr hidden>…<tr>``, ``<td hidden>…<td>``,
# ``<dt hidden>…<dd>`` all left the first hidden element open and swallowed
# the rest of the body). We also close any open ``<p>`` when a block-level
# element starts, matching the spec's generate-implied-end-tags step.
_PEER_IMPLICIT_CLOSE: dict[str, frozenset[str]] = {
    "p": frozenset({"p"}),
    "li": frozenset({"li"}),
    "option": frozenset({"option"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "tr": frozenset({"tr", "td", "th"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
}
_INLINE_BREAKS = frozenset({"a", "span", "img", "input"})
_META_CHARSET_RE = re.compile(rb"<meta[^>]{0,200}?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]{2,40})",
                              re.IGNORECASE)
# A charset token the parent may forward from the HTTP response Content-Type.
# Strict and bounded so a hostile parent (there isn't one) can't smuggle data.
_CHARSET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{2,40}$")
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


def _decode_html(raw: bytes, declared_charset: str = "") -> str:
    """Bytes → text: byte-order mark, then the page's own ``<meta>`` charset,
    then the HTTP header's declared charset (forwarded from the parent — field
    2026-10-04: Apache's "Content-Type: text/html; charset=ISO-8859-1" with no
    ``<meta>`` on real latin-1 bytes read as mojibake), then UTF-8 with
    replacement (``netguard.decode_body``, the one decode rule every fetch
    path shares)."""
    assert isinstance(raw, (bytes, bytearray)), "raw must be bytes"
    assert isinstance(declared_charset, str), "charset must be a string"
    match = _META_CHARSET_RE.search(bytes(raw[:4096]))
    if match:
        content_type = f"text/html; charset={match.group(1).decode('ascii')}"
    elif declared_charset and _CHARSET_TOKEN_RE.match(declared_charset):
        content_type = f"text/html; charset={declared_charset}"
    else:
        content_type = "text/html"
    from .netguard import decode_body  # lazy: the shared decode rule, only in the child
    return decode_body(bytes(raw), content_type)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _extra_text(article: str, body: str) -> str:
    """The visible body lines the article text does not already carry, in page
    order, deduplicated, bounded by ``_MAX_EXTRA_CHARS``."""
    assert isinstance(article, str) and isinstance(body, str), "text required"
    art = _norm(article)
    art_lines = {_norm(line) for line in article.splitlines()}
    out: list[str] = []
    seen: set[str] = set()
    size = 0
    for raw_line in body.splitlines():  # bounded by _MAX_BODY_CHARS
        line = _norm(raw_line)
        if len(line) < 2 or line in seen or line in art_lines or line in art:
            continue
        seen.add(line)
        if size + len(line) + 1 > _MAX_EXTRA_CHARS:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def _is_invisible_attrs(a: dict) -> bool:
    """A start tag whose attributes hide it from a sighted reader: ``hidden``
    (any value, per the HTML spec) or an inline style with ``display:none`` /
    ``visibility:hidden``. ``aria-hidden`` is for assistive tech, not sighted
    readers (field 2026-10-04: MLB standings rows + USWDS banner icons were
    dropped). React Server Components streaming writes real content into a
    ``<div hidden id="S:N">`` / ``id="B:N">`` template that is swapped in on
    hydration — treat that id convention as visible (field: wmata lost ~21 kB).
    Nesting is handled by the stack in ``_GraphParser.handle_starttag``."""
    if "hidden" in a:
        rid = (a.get("id") or "").strip()
        return not (rid and _REACT_STREAM_ID_RE.match(rid))
    style = a.get("style") or ""
    return bool(_HIDDEN_STYLE_RE.search(style))


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
        self.body: list[str] = []   # visible text, block tags as line breaks
        self._body_size = 0
        self._hidden = 0            # depth inside hidden regions
        # Parallel open-element stack: (tag, is_hidden). We track EVERY open
        # element so implicit end tags and ancestor end tags pop the right
        # hidden entries — a hidden stack keyed on name alone leaked state
        # across every well-formed sibling that happened to share the
        # hidden element's name (field 2026-10-04: ``<div><span hidden>x</div>``
        # left ``span hidden`` live for the rest of the body).
        self._open: list[tuple[str, bool]] = []
        # Per-tag count of hidden-maker starts that could not be pushed
        # because the open stack hit ``_MAX_OPEN_STACK``. Fail closed:
        # script / style / [hidden] / display:none past the cap must still
        # hide their content, and their matching end tag releases a slot
        # (field 2026-10-04: 300 unclosed <span> uncovered <script>SECRET,
        # <style>CSS, and <div hidden>text, all read as visible body).
        self._overflow_hidden: dict[str, int] = {}

    def _body_break(self, sep: str = "\n") -> None:
        if not self.body:
            return
        if not self.body[-1].isspace():
            self.body.append(sep)
        elif sep == "\n":
            self.body[-1] = "\n"  # a block edge outranks a word gap

    def _pop_one(self) -> bool:
        """Pop the topmost open element; return its hidden flag."""
        assert self._open, "open stack must be non-empty when popping"
        assert self._hidden >= 0, "hidden depth cannot go negative"
        _, was_hidden = self._open.pop()
        if was_hidden:
            self._hidden -= 1
        return was_hidden

    def _implicit_close_before(self, tag: str) -> None:
        """Apply HTML's generate-implied-end-tags before pushing ``tag``.

        A start tag in the HTML spec's "close a p element" list closes an
        open ``<p>`` on top; a same-category peer (``<li>``, ``<p>``,
        ``<dt>``/``<dd>``, ``<tr>``, ``<td>``/``<th>``, ``<option>``)
        closes any run of open peers on top. Bounded by ``_MAX_OPEN_STACK``.
        """
        assert isinstance(tag, str), "tag must be a string"
        assert len(self._open) <= _MAX_OPEN_STACK, "open stack bounded"
        if (tag in _P_CLOSERS and tag != "p"
                and self._open and self._open[-1][0] == "p"):
            self._pop_one()
        peers = _PEER_IMPLICIT_CLOSE.get(tag)
        if not peers:
            return
        for _ in range(_MAX_OPEN_STACK):
            if not self._open or self._open[-1][0] not in peers:
                return
            self._pop_one()

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in _INVISIBLE_TAGS:
            return  # a self-closed <svg/> hides nothing
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        self._implicit_close_before(tag)
        is_hidden = tag in _INVISIBLE_TAGS or _is_invisible_attrs(a)
        if tag not in _VOID_TAGS:
            if len(self._open) < _MAX_OPEN_STACK:
                self._open.append((tag, is_hidden))
                if is_hidden:
                    self._hidden += 1
            elif is_hidden:
                self._overflow_hidden[tag] = self._overflow_hidden.get(tag, 0) + 1
                self._hidden += 1
        if not is_hidden:
            if tag in _BLOCK_TAGS:
                self._body_break()
            elif tag in _INLINE_BREAKS:
                self._body_break(" ")  # adjacent links/spans are separate words
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
        if not self._hidden and self._body_size < _MAX_BODY_CHARS:
            if data.strip():
                chunk = data[:_MAX_BODY_CHARS - self._body_size]
                self.body.append(chunk)
                self._body_size += len(chunk)
            else:
                self._body_break(" ")
        if self._in_jsonld:
            self._jsonld_buf.append(data)
        if self._heading is not None:
            self._head_buf.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        # Pop down to the matching ancestor on the full open-element stack —
        # malformed markup (an unclosed <span> that never closed before </div>,
        # a <td hidden> left open at </tr>) must never leak the hidden state
        # across the rest of the body. Bounded by _MAX_OPEN_STACK.
        found_at = -1
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                found_at = i
                break
        hidden_self_popped = False
        if found_at >= 0:
            for _ in range(len(self._open) - found_at):
                name = self._open[-1][0]
                if self._pop_one() and name == tag:
                    hidden_self_popped = True
        elif self._overflow_hidden.get(tag, 0) > 0:
            # Matching end tag for an un-pushed hidden-maker: release one
            # slot of its overflow depth so its hidden region closes.
            self._overflow_hidden[tag] -= 1
            self._hidden -= 1
            hidden_self_popped = True
        if tag in _BLOCK_TAGS and not hidden_self_popped:
            self._body_break()
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


def _page_graph_layers(html: str) -> tuple[dict, str]:
    """Run the graph parser; return (layers, visible body text). A failure in
    any layer degrades to empty layers and whatever body text was read."""
    assert isinstance(html, str), "html must be a string"
    parser = _GraphParser()
    empty = {"entities": [], "tables": [], "feeds": [], "meta": {}, "outline": []}
    try:
        parser.feed(html)
        parser.close()
        # Entity flattening runs INSIDE the guard too: a hostile JSON-LD blob
        # failing here must lose the graph layers only — never text/title
        # (the pre-existing page-door path rides the same child process).
        return {
            "entities": _entities_from_jsonld(parser.jsonld_blobs),
            "tables": parser.tables[:_MAX_TABLES],
            "feeds": parser.feeds[:_MAX_FEEDS],
            "meta": parser.meta,
            "outline": parser.outline[:_MAX_OUTLINE],
        }, "".join(parser.body)
    except Exception:  # hostile markup must never kill the text path
        return empty, "".join(parser.body)


def _page_text(article: str, body: str) -> str:
    """The article text plus the visible text it dropped (after
    ``PAGE_EXTRA_MARK``); the visible text alone when there is no article."""
    assert isinstance(article, str) and isinstance(body, str), "text required"
    if not article:
        return "\n".join(line for line in (_norm(x) for x in body.splitlines()) if line)
    extra = _extra_text(article, body)
    return article + PAGE_EXTRA_MARK + extra if extra else article


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


def extract(raw: bytes, url_hint: str, *, declared_charset: str = "") -> dict:
    """Bytes → the jail payload ``{"text", "title", <graph layers>}``. Pure and
    in-process-callable (tests parse recorded pages with it); ``main`` runs it
    inside the subprocess jail for every real fetch. ``declared_charset`` is
    the HTTP Content-Type charset (bounded token, falls through on BOM / meta)."""
    assert isinstance(raw, (bytes, bytearray)), "raw must be bytes"
    assert isinstance(url_hint, str), "url hint must be a string"
    assert isinstance(declared_charset, str), "charset must be a string"
    html = _decode_html(bytes(raw), declared_charset)
    title, text = "", ""
    try:
        title, text = _extract_with_trafilatura(html, url_hint)
    except Exception:  # hostile HTML (lxml errors, recursion): the visible text still reads
        title, text = "", ""
    layers, body = _page_graph_layers(html)
    payload = {"text": _cap(_page_text(text, body), _MAX_TEXT_CHARS),
               "title": _cap(title, _MAX_TITLE_CHARS)}
    payload.update(layers)
    return payload


def main() -> None:
    """Entry point: rlimits → read stdin → extract → dump JSON → exit 0.

    Any exception bubbles out and the interpreter exits non-zero. The parent
    inspects the exit code + parsed stdout; a bare non-JSON stdout, a partial
    reply, or an oversize dump all route through the same ``extract_jail`` class.
    """
    _apply_rlimits()
    argv = sys.argv[1:]
    url_hint = argv[0] if argv else ""
    declared_charset = argv[1] if len(argv) > 1 else ""
    assert isinstance(url_hint, str), "url hint must be a string"
    assert isinstance(declared_charset, str), "charset must be a string"
    payload = extract(_read_stdin_bytes(), url_hint, declared_charset=declared_charset)
    sys.stdout.write(_fit_output(payload))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
