"""Keys stay out of the log.

The app sets no logging policy of its own, and a library it imports configures the root logger at INFO with a
handler on stderr; the launcher captures that stream into app.log. ``httpx`` logs every request it sends at INFO
with the full URL — for a keyed Neural Interface source that URL carries the user's key in its query string
(``...&apikey=<the key>``). Found on T's Mac on 2026-10-30: an owner-only, unrotated app.log of 4.2 GB holding
every keyed fetch since the card was made.

Two layers, both installed once from ``main`` at import:

* the request loggers of the HTTP client (``httpx``, ``httpcore``) are raised to WARNING, so no request line
  is written at all — a URL is never log material here (``netguard`` reports fetch failures by class name for
  the same reason);
* a log-record factory redacts a credential wherever one still appears in any logger's message: a key-bearing
  query parameter (``apikey=``, ``token=``, ``key=``, ...), an authorization or API-key header value. The
  factory runs for every record before any handler sees it, so a line a future module writes with a URL in
  it is cleaned too.

``redact`` is pure and tested on its own; ``install`` is idempotent.
"""
from __future__ import annotations

import logging
import re

REDACTED = "[redacted]"

# query parameters that carry a credential, as the Library pack and the common providers spell them
_QUERY_PARAM_RE = re.compile(
    r"([?&;](?:api[_-]?key|apikey|key|token|access[_-]?token|auth[_-]?token|api[_-]?token|client[_-]?secret|"
    r"secret|password|passwd|pwd|appid|app[_-]?id|x[_-]?api[_-]?key|signature|sig)=)([^&;\s\"'<>]+)",
    re.IGNORECASE)
# header-style spellings: ``Authorization: Bearer <value>``, ``X-Api-Key=<value>``, ``'token': '<value>'``
_HEADER_RE = re.compile(
    r"((?:authorization|proxy-authorization|x-api-key|x-auth-token|x-ebirdapitoken|api-key|apikey|api_key|"
    r"x-rapidapi-key|ocp-apim-subscription-key)['\"]?\s*[:=]\s*['\"]?)"
    r"((?:bearer|basic|token|apikey)\s+)?([^\s'\",;}\]]+)",
    re.IGNORECASE)
# the client's request line form: ``HTTP Request: GET https://host/path?...`` (left out entirely by the WARNING
# level below; the pattern is here for a line another logger writes with a bare URL carrying a key)
_QUIET_LOGGERS = ("httpx", "httpcore")

_installed = False


def _query_value(match: re.Match) -> str:
    value = match.group(2)
    return match.group(0) if value.startswith(REDACTED) else match.group(1) + REDACTED


def _header_value(match: re.Match) -> str:
    value = match.group(3)
    if value.startswith(REDACTED[:-1]):  # already clean (the pattern stops at the closing bracket)
        return match.group(0)
    return match.group(1) + (match.group(2) or "") + REDACTED


def redact(text: str) -> str:
    """``text`` with every credential value replaced by ``[redacted]``; unchanged text comes back as is, and text
    already redacted is left alone (redacting twice changes nothing)."""
    assert isinstance(text, str), "text required"
    out = _HEADER_RE.sub(_header_value, _QUERY_PARAM_RE.sub(_query_value, text))
    assert isinstance(out, str), "a string"
    return out


def _redacting_factory(inner):
    """A ``logging`` record factory that formats the record's message once and stores it redacted."""
    assert callable(inner), "the factory to wrap"

    def factory(*args, **kwargs):
        record = inner(*args, **kwargs)
        try:
            text = record.getMessage()
        except Exception:  # a record whose args don't format: leave it to the handler to report
            return record
        clean = redact(text)
        if clean != text:
            record.msg, record.args = clean, ()
        return record

    return factory


def install() -> None:
    """Install the redacting record factory and quiet the HTTP client's request lines. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    logging.setLogRecordFactory(_redacting_factory(logging.getLogRecordFactory()))
    for name in _QUIET_LOGGERS:  # bounded: the two client loggers
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


def installed() -> bool:
    """True once ``install`` ran in this process."""
    return _installed
