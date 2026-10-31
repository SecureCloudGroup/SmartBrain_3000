"""Keys stay out of the log (``logredact``).

Found on T's Mac on 2026-10-30: app.log held every keyed Neural Interface fetch as a full URL with the user's key in
its query string, written by the HTTP client's INFO request line. These tests pin both layers: the client's request
loggers are quiet, and any logger's message is redacted before a handler sees it.
"""
from __future__ import annotations

import logging
import time

import pytest

from smartbrain_3000 import logredact

KEY = "ZK9QX2A7LMNOP3R4S5T6"


@pytest.mark.parametrize("line", [
    f"HTTP Request: GET https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=NVDA&apikey={KEY} \"HTTP/1.1 200 OK\"",
    f"fetching https://finnhub.io/api/v1/quote?symbol=NVDA&token={KEY}",
    f"GET https://api.example.org/v1?key={KEY}&q=x",
    f"https://api.openweathermap.org/data/2.5/weather?q=Paris&appid={KEY}",
    f"headers={{'Authorization': 'Bearer {KEY}'}}",
    f"X-Api-Key: {KEY}",
    f"x-ebirdapitoken={KEY}",
    f"request failed: url=https://h.example/?access_token={KEY}&x=1 status=401",
])
def test_a_credential_in_a_log_line_is_redacted(line: str) -> None:
    out = logredact.redact(line)
    assert KEY not in out and logredact.REDACTED in out, out


def test_the_rest_of_the_line_is_kept() -> None:
    out = logredact.redact(f"GET https://finnhub.io/api/v1/quote?symbol=NVDA&token={KEY} -> 200")
    assert out == "GET https://finnhub.io/api/v1/quote?symbol=NVDA&token=[redacted] -> 200"
    assert logredact.redact("ni_flow: library candidates failed: ValueError") == "ni_flow: library candidates failed: ValueError"
    assert logredact.redact("GET https://h.example/?symbol=NVDA&apikey=") == "GET https://h.example/?symbol=NVDA&apikey="


def test_install_redacts_every_logger_and_quiets_the_http_client(caplog) -> None:
    logredact.install()
    logredact.install()  # idempotent
    assert logredact.installed()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
    log = logging.getLogger("smartbrain.test.redact")
    with caplog.at_level(logging.INFO, logger="smartbrain.test.redact"):
        log.info("fetched %s for %s", f"https://finnhub.io/api/v1/quote?symbol=NVDA&token={KEY}", "card-1")
        log.warning("sent Authorization: Bearer %s", KEY)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert KEY not in text and "card-1" in text and text.count(logredact.REDACTED) == 2
    assert all(r.args == () for r in caplog.records), "the record carries its redacted text, no raw args"


def test_the_http_clients_own_request_line_never_reaches_a_handler(caplog) -> None:
    logredact.install()
    with caplog.at_level(logging.DEBUG, logger="smartbrain.test.other"):
        logging.getLogger("httpx").info('HTTP Request: GET https://finnhub.io/api/v1/quote?token=%s "HTTP/1.1 200 OK"', KEY)
    assert caplog.records == [], "an INFO request line is below the client logger's level"


def test_redaction_is_fast_on_hostile_lines() -> None:
    """The factory runs on every record: a long line of near-matches must not backtrack."""
    for shape in ("&apikey=", "apikey=", "Authorization: ", "a=", "&", "'", " ", "=", "token"):
        text = shape * (40000 // len(shape) + 1) + "!"
        start = time.perf_counter()
        logredact.redact(text)
        assert time.perf_counter() - start < 0.5, f"slow on {shape!r}"


def test_a_traceback_that_names_a_keyed_url_is_redacted(caplog) -> None:
    """The review of #569 reproduced it: ``log.exception`` over an HTTP client's status error prints the URL
    the request went to, key and all, from the traceback, which the message factory alone never saw."""
    import httpx

    logredact.install()
    url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=NVDA&apikey={KEY}"
    request = httpx.Request("GET", url)
    log = logging.getLogger("smartbrain.test.redact.trace")
    with caplog.at_level(logging.DEBUG, logger="smartbrain.test.redact.trace"):
        try:
            httpx.Response(401, request=request).raise_for_status()
        except httpx.HTTPStatusError:
            log.exception("fetch failed for %s", "card-1")
        try:
            raise ValueError("no key in this one")
        except ValueError:
            log.exception("plain failure")
    assert KEY in url and "card-1" in caplog.text and "HTTPStatusError" in caplog.text, caplog.text
    assert KEY not in caplog.text and "apikey=[redacted]" in caplog.text, caplog.text
    failed, plain = caplog.records
    assert failed.exc_info is None and KEY not in failed.exc_text, "the raw exception is not kept for a handler"
    assert plain.exc_info is not None and "no key in this one" in caplog.text, "a clean traceback is left as it is"


def test_a_stack_that_names_a_keyed_url_is_redacted(caplog) -> None:
    """``stack_info`` prints the caller's source lines; one that holds a keyed URL is redacted like a message."""
    logredact.install()
    log = logging.getLogger("smartbrain.test.redact.stack")
    with caplog.at_level(logging.DEBUG, logger="smartbrain.test.redact.stack"):
        record = logging.getLogRecordFactory()(
            "smartbrain.test.redact.stack", logging.WARNING, __file__, 1, "asked from here", (), None,
            sinfo=f"Stack (most recent call last):\n  fetch('https://h.example/?symbol=NVDA&token={KEY}')")
        log.handle(record)
    assert KEY not in caplog.text and "token=[redacted]" in caplog.text and "symbol=NVDA" in caplog.text
