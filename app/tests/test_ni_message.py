"""``ni_message``: the one rule for a provider's sentence in place of its data, and for reading that the host asked
SmartBrain to wait. The real captured bodies run through it in ``test_ni_proof_asks``; these pin the wordings and
the edges. Wordings marked public are Alpha Vantage's published messages, not bodies captured here."""
from __future__ import annotations

import pytest

from smartbrain_3000 import ni_message

# Alpha Vantage's public wordings (its free tier's daily quota, its older per-minute limit, its burst limit)
DAILY = ("Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day. Please subscribe to "
         "any of the premium plans at https://www.alphavantage.co/premium/ to instantly remove all daily rate limits.")
PER_MINUTE = ("Thank you for using Alpha Vantage! Our standard API call frequency is 5 calls per minute and 500 calls "
              "per day. Please visit https://www.alphavantage.co/premium/ if you would like to target a higher API "
              "call frequency.")
BURST = ("Thank you for using Alpha Vantage! Please consider spreading out your free API requests more sparingly (1 "
         "request per second). You may subscribe to any of the premium plans at https://www.alphavantage.co/premium/ "
         "to lift the free key rate limit (25 requests per day), raise the per-minute rate limit, and completely "
         "remove the daily rate limit.")
DEMO = ("The **demo** API key is for demo purposes only. Please claim your free API key at "
        "(https://www.alphavantage.co/support/#api-key) to explore our full API offerings. It takes fewer than 20 "
        "seconds.")


@pytest.mark.parametrize(("sentence", "wait"), [
    (BURST, ni_message.WAIT_SECOND_S),          # the shortest window it names wins over the upsell's daily one
    (PER_MINUTE, ni_message.WAIT_MINUTE_S),
    (DAILY, ni_message.WAIT_LONG_S),
    ("You have run out of API credits for the current minute.", ni_message.WAIT_MINUTE_S),
    ("Too Many Requests", ni_message.WAIT_UNKNOWN_S),
    ("Your quota has been exceeded, try again later.", ni_message.WAIT_UNKNOWN_S),
    ("API rate limit exceeded for this key.", ni_message.WAIT_UNKNOWN_S),
    ("Monthly request limit reached for your plan.", ni_message.WAIT_LONG_S),
])
def test_a_limit_sentence_names_how_long_to_wait(sentence: str, wait: float) -> None:
    assert ni_message.limit_wait(sentence) == wait


@pytest.mark.parametrize("sentence", [
    DEMO,
    "Invalid API call. Please retry or visit the documentation for TIME_SERIES_DAILY.",
    "This API key is not authorized for this endpoint.",
    "No data for this symbol today.",
    "",
])
def test_a_sentence_about_anything_else_is_no_wait(sentence: str) -> None:
    assert ni_message.limit_wait(sentence) is None
    assert ni_message.host_limit({"Information": sentence}) is None


def test_host_limit_reads_only_a_message_body() -> None:
    assert ni_message.host_limit({"Information": BURST}) == (ni_message.WAIT_SECOND_S, ni_message.gist(BURST))
    assert ni_message.host_limit({"Note": PER_MINUTE})[0] == ni_message.WAIT_MINUTE_S
    assert ni_message.host_limit({"code": 429, "status": "error",
                                  "message": "You have run out of API credits for the current minute."})
    # data that happens to talk about limits is data: a key that names data, a number outside a code key, a list
    assert ni_message.host_limit({"plan": "free", "note": "rate limit is 25 requests per day"}) is None
    assert ni_message.host_limit({"message": "rate limit is 25 requests per day", "remaining": 3}) is None
    assert ni_message.host_limit({"messages": ["rate limit is 25 requests per day"]}) is None
    assert ni_message.host_limit([{"message": DAILY}]) is None and ni_message.host_limit(DAILY) is None


def test_the_sentence_is_whole_and_the_gist_is_cut() -> None:
    long = "word " * 200
    assert ni_message.message_sentence({"Information": DEMO}) == " ".join(DEMO.replace("*", "").split())
    assert len(ni_message.message_sentence({"Information": long})) == ni_message.SENTENCE_CHARS
    gist = ni_message.gist(BURST)
    assert len(gist) <= ni_message.MESSAGE_GIST_CHARS + 1 and gist.endswith("…")
    assert ni_message.gist("short and whole") == "short and whole"
    assert ni_message.message_sentence({"message": "Unauthorized"}) is None, "one word is not a sentence"


def test_a_key_the_provider_echoed_never_leaves_the_sentence() -> None:
    """The sentence is shown on the card, kept on its record, and read by research's model: a key or token in it is
    redacted wherever it sits, and ordinary words, dates and links are left alone."""
    for key in ("ABCD1234EFGH5678", "c1abc2def3ghi4jkl5mn", "sk_live_0123456789abcdef", "a1b2-c3d4-e5f6-a7b8-c9d0"):
        said = ni_message.message_sentence({"Error Message": f"The key {key} has exceeded its rate limit today."})
        assert key not in said and ni_message.TOKEN_REDACTED in said, said
        assert ni_message.limit_wait(said) is not None, "still read as the limit it is"
        assert key not in (ni_message.host_limit({"Note": f"Key {key}: too many requests per minute."}) or ("", ""))[1]
    kept = ni_message.message_sentence({"Information": DEMO})
    assert ni_message.TOKEN_REDACTED not in kept and "https://www.alphavantage.co/support/#api-key" in kept
    assert ni_message.TOKEN_REDACTED not in ni_message.message_sentence(
        {"message": "Data for 2026-10-31T12:07:18 is not ready; see https://example.com/api-reference-guide later."})
    # an identifier with a digit in it is redacted too: a lost word in a reason line, never a leaked key
    assert ni_message.message_sentence({"message": "see documentation_version_2 for more"}) == \
        f"see {ni_message.TOKEN_REDACTED} for more"


def test_a_number_sent_as_text_is_a_number() -> None:
    assert ni_message.numeric_text("6.904") and ni_message.numeric_text('"-3"') and ni_message.numeric_text("0.5")
    assert not ni_message.numeric_text("02134") and not ni_message.numeric_text("12 shares")
