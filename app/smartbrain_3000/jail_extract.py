"""Child-side entry for the subprocess jail (§16).

Invoked by ``jailrun.run_extractor`` as ``sys.executable -c
"from smartbrain_3000.jail_extract import main; main()"``. Reads HTML bytes from
stdin, extracts main-article text + title with trafilatura (mirroring
``ingest._extract_html``), falls back to a plain tag-strip when trafilatura returns
nothing, and prints ONE JSON object ``{"text": str, "title": str}`` to stdout.

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

_MAX_TEXT_CHARS = 200_000        # §15 http_page payload text cap
_MAX_TITLE_CHARS = 500           # bound on the extracted title (short human line)
_MAX_INPUT_BYTES = 4 * 1024 * 1024  # hard stdin cap (parent already caps at 2 MB)
_RLIMIT_CPU_SECONDS = 15         # CPU seconds inside the child (wall-clock is watchdog)
_RLIMIT_AS_BYTES = 768 * 1024 * 1024  # 768 MB address-space cap
_TAG_STRIP_RE = re.compile(r"<[^>]+>")  # crude fallback when trafilatura returns nothing


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


def _cap(value: str, cap: int) -> str:
    """Cap ``value`` at ``cap`` chars — bind-time defense (§15's post-extract cap)."""
    assert isinstance(value, str), "value must be a string"
    assert cap > 0, "cap must be positive"
    if len(value) <= cap:
        return value
    return value[:cap]


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
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
