"""A provider's sentence in place of its data (2026-10-31).

Some sources answer a refused or limited request with 200 OK and a sentence, so no status says anything is wrong:
Alpha Vantage's demo key on a real symbol, its "1 request per second" burst limit and its "25 requests per day" quota
(all captured on T's Mac, ``app/tests/fixtures/providers/www.alphavantage.co``). This module is the one rule for
telling such a body from data (``provider_message``) and for reading, from the sentence, that the host asked
SmartBrain to wait (``limit_wait``). The build reads the first (``ni_flow._sample_and_map``: a sentence moves on at
once, with the provider's words as the reason, before any model call). The politeness layer reads the second
(``ni_polite``): a host's "slow down" in its body is the same signal as a 429, so the host is paused, a refresh waits
instead of failing and a build ends with a retry, never a decline. Until 2026-10-31 a burst-limited body read as "No
ready source answers this yet". Pure code, no network.
"""
from __future__ import annotations

import re

# a body that is a sentence to the caller, not data: the keys such bodies use, normalized (lower, [a-z0-9] runs)
MESSAGE_KEYS = frozenset({"information", "info", "error", "errors", "errormessage", "message", "msg", "note",
                          "notice", "warning", "detail", "details", "status", "reason", "fault", "description"})
MESSAGE_CODE_KEYS = frozenset({"code", "cod", "errorcode", "status", "statuscode", "success", "ok"})
# what a provider's message carries beside its sentence that names no data: a request id, a link to its docs
MESSAGE_META_KEYS = frozenset({"requestid", "traceid", "documentation", "documentationurl", "moreinfo", "help"})
MESSAGE_ANY_KEYS = MESSAGE_KEYS | MESSAGE_CODE_KEYS | MESSAGE_META_KEYS
MESSAGE_MAX_LEAVES = 4
MESSAGE_MIN_WORDS = 3
MESSAGE_GIST_CHARS = 140
SENTENCE_CHARS = 600  # the whole sentence a limit is read from (the gist can cut the window off)
NUMERIC_TEXT_RE = re.compile(r"-?\d+(?:\.\d+)?")
# A key, token or id a provider echoed back: one unbroken run of 16 or more letters, digits, "_" or "-" holding both a
# digit and a letter (an Alpha Vantage key is 16, a Finnhub token 20). A provider's sentence is shown on the card,
# kept on its record and read by research's model; a key it echoed must reach none of them.
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{16,}"
                       r"(?![A-Za-z0-9_-])")
TOKEN_REDACTED = "[redacted]"

# The words a provider uses when its limit, not the request, is the problem: "Our standard API rate limit is 25
# requests per day", "spreading out your free API requests more sparingly (1 request per second)", "Too Many
# Requests", "quota exceeded", "You have run out of API credits for the current minute". Read from a message body's
# sentence only, never from data.
_LIMIT_RE = re.compile(r"rate[ -]?limit|too many requests|sparingly|\bquota\b|\bthrottl|"
                       r"\b(requests?|calls?|credits?) (per|a|an|each|every) (second|minute|hour|day|month)\b|"
                       r"\b(run|ran) out of (\w+ )?(requests|calls|credits)\b|"
                       r"\blimit (reached|exceeded|hit)\b|\bexceeded (the |your |its |our )?(\w+ )?limit\b",
                       re.IGNORECASE)
_SECOND_RE = re.compile(r"\b(per|a|each|every) second\b|sparingly", re.IGNORECASE)
_MINUTE_RE = re.compile(r"\b(per|a|each|every|the current) minute\b", re.IGNORECASE)
_LONG_RE = re.compile(r"\b(per|an?|each|every) (hour|day|month)\b|\b(hourly|daily|monthly)\b", re.IGNORECASE)
WAIT_SECOND_S = 2.0      # a per-second burst limit: the next request a moment later is fine
WAIT_MINUTE_S = 60.0
WAIT_LONG_S = 3600.0     # an hourly, daily or monthly quota: the politeness cap (``ni_polite.RETRY_AFTER_CAP_S``)
WAIT_UNKNOWN_S = 300.0   # a limit with no window named: as for a 429 with no Retry-After


def numeric_text(example: object) -> bool:
    """A sampled string that is really a number ("6.904", "-3", "42") — many APIs and every
    CSV send numbers as text. Leading zeros ("02134", an id or ZIP) stay text."""
    raw = example
    if isinstance(raw, str) and raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]  # the walker's examples are JSON-encoded
    if not isinstance(raw, str) or not NUMERIC_TEXT_RE.fullmatch(raw.strip()):
        return False
    digits = raw.strip().lstrip("-")
    return not (len(digits) > 1 and digits[0] == "0" and digits[1] != ".")


def _message_leaves(sample: dict) -> list[tuple[str, object]] | None:
    """The (normalized key, value) leaves of a body that can only be a provider talking about the request, or None
    when it is data. Data is: a key that names data (any key outside the message, code and meta keys — ``title``,
    ``price``, ``indicator``: a status page's ``{"indicator": "none", "description": "All Systems Operational"}``
    is data), a list, an object nested deeper than one wrapper (``{"status": {"error_code": .., "error_message":
    ..}}`` is one), more than ``MESSAGE_MAX_LEAVES`` leaves, or a number (or a number sent as text) outside a code
    key. (2026-10-31, caught by a live-regression test: the first rule read a sentence under any message-like key,
    so a page object with a title and a note was taken for a message.)"""
    leaves: list[tuple[str, object]] = []
    stack: list[tuple[object, int]] = [(sample, 0)]
    for _ in range(8):  # bounded: the top object and its wrappers; a body with more objects than that is data
        if not stack:
            return leaves
        node, depth = stack.pop()
        if not isinstance(node, dict) or depth > 1:
            return None
        for key, value in node.items():  # bounded by the leaf cap below
            norm = re.sub(r"[^a-z0-9]+", "", str(key).lower())
            if norm not in MESSAGE_ANY_KEYS or isinstance(value, list):
                return None
            if isinstance(value, dict):
                stack.append((value, depth + 1))
                continue
            numeric = (isinstance(value, (int, float)) and not isinstance(value, bool)) \
                or (isinstance(value, str) and numeric_text(value))
            if numeric and norm not in MESSAGE_CODE_KEYS:
                return None
            leaves.append((norm, value))
            if len(leaves) > MESSAGE_MAX_LEAVES:
                return None
    return None


def message_sentence(sample: object) -> str | None:
    """The whole sentence a source answered with instead of its data (whitespace folded, Markdown stars dropped,
    anything that looks like a key or token replaced by ``[redacted]``, at most ``SENTENCE_CHARS``), or None when
    the body is data. A body is a message when every key in it is a message, code or request-id key, at the top or
    under one wrapper (``_message_leaves``: no key names data), and a leaf under a message key reads as a sentence
    of three words or more."""
    if not isinstance(sample, dict) or not sample:
        return None
    leaves = _message_leaves(sample)
    if not leaves:
        return None
    for norm, value in leaves:  # bounded: ≤ MESSAGE_MAX_LEAVES
        if norm in MESSAGE_KEYS and isinstance(value, str) and len(value.split()) >= MESSAGE_MIN_WORDS:
            text = " ".join(value[:4 * SENTENCE_CHARS].replace("*", "").split())
            return _TOKEN_RE.sub(TOKEN_REDACTED, text)[:SENTENCE_CHARS]
    return None


def gist(sentence: str) -> str:
    """The reason-line cut of a provider's sentence: at most ``MESSAGE_GIST_CHARS``, an ellipsis when cut."""
    assert isinstance(sentence, str), "a sentence"
    text = " ".join(sentence.split())
    return text[:MESSAGE_GIST_CHARS].rstrip() + ("…" if len(text) > MESSAGE_GIST_CHARS else "")


def provider_message(sample: object) -> str | None:
    """The gist of the sentence a source answered with instead of its data, or None.

    Alpha Vantage answers its demo key on a real symbol, a burst over 1 request a second and a spent daily quota
    with 200 OK bodies (``{"Information": "The demo API key is for demo purposes only..."}``), so no status says
    anything is wrong, the walker offered the one sentence as a string candidate, and the mapping spent five model
    calls trying to read a price out of it (T's Mac, 2026-10-30). It answers a made-up key with a real quote, so a
    quote that answers proves nothing about the key (captured 2026-10-31)."""
    sentence = message_sentence(sample)
    return None if sentence is None else gist(sentence)


def limit_wait(sentence: object) -> float | None:
    """How long the host asked SmartBrain to wait, read from its sentence; None when the sentence is not about a
    limit. The shortest window it names wins, since Alpha Vantage's burst message also names its daily limit in its
    upsell: a per-second limit ``WAIT_SECOND_S``, per minute ``WAIT_MINUTE_S``, per hour, day or month
    ``WAIT_LONG_S``, a limit with no window ``WAIT_UNKNOWN_S``."""
    if not isinstance(sentence, str) or not _LIMIT_RE.search(sentence):
        return None
    if _SECOND_RE.search(sentence):
        return WAIT_SECOND_S
    if _MINUTE_RE.search(sentence):
        return WAIT_MINUTE_S
    if _LONG_RE.search(sentence):
        return WAIT_LONG_S
    return WAIT_UNKNOWN_S


def host_limit(sample: object) -> tuple[float, str] | None:
    """``(seconds, gist)`` when a body is the host's message that its limit was hit, else None (data, or a sentence
    about anything else: a demo key, a bad symbol)."""
    sentence = message_sentence(sample)
    if sentence is None:
        return None
    wait = limit_wait(sentence)
    return None if wait is None else (wait, gist(sentence))
