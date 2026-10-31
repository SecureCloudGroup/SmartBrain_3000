#!/usr/bin/env python3
"""Capture real provider responses as test fixtures for the Neural Interface build path.

Three releases (0.27.1, 0.27.2, 0.27.3) were announced fixed on tests whose Alpha Vantage, Finnhub and Open-Meteo
bodies were typed by hand, and each failed on T's Mac against the real bodies: Alpha Vantage spells its fields
``"Global Quote"`` / ``"05. price"`` and answers a spent quota with a 200 OK sentence, neither of which a hand-typed
fixture had. This tool fetches the catalogue below through the app's own guarded fetch (``netguard``), the way a
card does, and writes each body under ``app/tests/fixtures/providers/<host>/<name>.json`` with the request's
status and content type, the key's placement (never its value: the URL is written with the key redacted) and
``captured: true``. ``test_ni_proof_asks.py`` replays the bodies through the production fetch path and refuses to
pass while any fixture is still provisional (``captured: false``: the documented shape, written by
``--provisional`` so the tests have a body to run against before a capture).

Keys: ``--key alphavantage=<key> --key finnhub=<key>`` or the environment (``SB_CAPTURE_KEY_ALPHAVANTAGE``,
``SB_CAPTURE_KEY_FINNHUB``); a key is sent only to its own host, and is never printed or written. Alpha Vantage's
``demo`` key answers for IBM (its documented example symbol) and is used for the shape captures when no key is given;
Finnhub has no demo key, so its quote body needs the user's key (its 401 is captured without one).

Usage (the operator's machine; the sandboxes this project builds in cannot reach these hosts):
    PYTHONPATH=app python3 tools/ni-capture-fixtures.py --out app/tests/fixtures/providers [--only NAME ...]
    PYTHONPATH=app python3 tools/ni-capture-fixtures.py --out app/tests/fixtures/providers --provisional
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from smartbrain_3000 import logredact, netguard  # noqa: E402

REDACTED = "[redacted]"

# ---- the catalogue -------------------------------------------------------------------------------------------
# name, url (with {key} where the key goes), the key's provider ("" for keyless), how it is placed, a note on what
# the body is for, and the documented shape used until the real body is captured.
CATALOGUE: list[dict] = [
    {"name": "alphavantage-global-quote", "host": "www.alphavantage.co",
     "url": "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=IBM&apikey={key}",
     "provider": "alphavantage", "key": {"in": "query", "name": "apikey"}, "demo": "demo",
     "note": "the quote body the pack's alphavantage-quote row serves: numbered, spaced field names, numbers as text",
     "provisional": {"Global Quote": {"01. symbol": "IBM", "02. open": "289.5000", "03. high": "292.3800",
                                      "04. low": "287.1100", "05. price": "291.2700", "06. volume": "4213456",
                                      "07. latest trading day": "2026-10-30", "08. previous close": "288.9000",
                                      "09. change": "2.3700", "10. change percent": "0.8204%"}}},
    {"name": "alphavantage-time-series-daily", "host": "www.alphavantage.co",
     "url": "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM&apikey={key}",
     "provider": "alphavantage", "key": {"in": "query", "name": "apikey"}, "demo": "demo",
     "note": "the daily series the pack's alphavantage-daily row serves: values filed under each day's date",
     "provisional": {"Meta Data": {"1. Information": "Daily Prices (open, high, low, close) and Volumes",
                                   "2. Symbol": "IBM", "3. Last Refreshed": "2026-10-30", "4. Output Size": "Compact",
                                   "5. Time Zone": "US/Eastern"},
                     "Time Series (Daily)": {"2026-10-30": {"1. open": "289.5000", "2. high": "292.3800",
                                                            "3. low": "287.1100", "4. close": "291.2700",
                                                            "5. volume": "4213456"},
                                             "2026-10-29": {"1. open": "286.0000", "2. high": "290.1000",
                                                            "3. low": "285.2000", "4. close": "288.9000",
                                                            "5. volume": "3987001"}}}},
    {"name": "alphavantage-demo-key-refused", "host": "www.alphavantage.co",
     "url": "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=NVDA&apikey={key}",
     "provider": "alphavantage", "key": {"in": "query", "name": "apikey"}, "demo": "demo", "force_demo": True,
     "note": "a 200 OK sentence instead of data: the demo key on a symbol it does not cover (the same shape as the "
             "spent-quota 'Information' body)",
     "provisional": {"Information": "The **demo** API key is for demo purposes only. Please claim your free API key "
                                    "at (https://www.alphavantage.co/support/#api-key) to explore our full API "
                                    "offerings. It takes fewer than 20 seconds."}},
    {"name": "alphavantage-bad-key", "host": "www.alphavantage.co",
     "url": "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=IBM&apikey={key}",
     "provider": "alphavantage", "key": {"in": "query", "name": "apikey"}, "demo": "not-a-real-key-0000",
     "force_demo": True,
     "note": "a 200 OK sentence under a spaced key: an invalid key",
     "provisional": {"Error Message": "the parameter apikey is invalid or missing. Please claim your free API key on "
                                      "(https://www.alphavantage.co/support/#api-key). It should take less than 20 "
                                      "seconds."}},
    {"name": "finnhub-quote", "host": "finnhub.io",
     "url": "https://finnhub.io/api/v1/quote?symbol=NVDA&token={key}",
     "provider": "finnhub", "key": {"in": "query", "name": "token"}, "demo": None,
     "note": "the flat quote body the pack's finnhub-quote row serves (needs the user's key)",
     "provisional": {"c": 181.25, "d": 2.1, "dp": 1.1721, "h": 183.0, "l": 179.4, "o": 180.0, "pc": 179.15,
                     "t": 1761782400}},
    {"name": "finnhub-quote-no-key", "host": "finnhub.io",
     "url": "https://finnhub.io/api/v1/quote?symbol=NVDA",
     "provider": "", "key": None, "demo": None,
     "note": "the 401 a keyed endpoint answers without a key: the key wall a card asks on",
     "provisional_status": 401, "provisional_content_type": "application/json",
     "provisional": {"error": "You don't have access to this resource."}},
    {"name": "open-meteo-forecast", "host": "api.open-meteo.com",
     "url": "https://api.open-meteo.com/v1/forecast?latitude=40.71&longitude=-74.01&current=temperature_2m,"
            "relative_humidity_2m,weather_code,wind_speed_10m&daily=weather_code,temperature_2m_max,"
            "temperature_2m_min,precipitation_probability_max&temperature_unit=fahrenheit&wind_speed_unit=mph"
            "&timezone=America%2FNew_York",
     "provider": "", "key": None, "demo": None,
     "note": "the keyless forecast body the Global Library's weather sources read (current + daily blocks)",
     "provisional": {"latitude": 40.71, "longitude": -74.01, "generationtime_ms": 0.2, "utc_offset_seconds": -14400,
                     "timezone": "America/New_York", "timezone_abbreviation": "EDT", "elevation": 10.0,
                     "current_units": {"time": "iso8601", "interval": "seconds", "temperature_2m": "°F",
                                       "relative_humidity_2m": "%", "weather_code": "wmo code",
                                       "wind_speed_10m": "mp/h"},
                     "current": {"time": "2026-10-30T14:00", "interval": 900, "temperature_2m": 61.3,
                                 "relative_humidity_2m": 52, "weather_code": 2, "wind_speed_10m": 8.1},
                     "daily_units": {"time": "iso8601", "weather_code": "wmo code", "temperature_2m_max": "°F",
                                     "temperature_2m_min": "°F", "precipitation_probability_max": "%"},
                     "daily": {"time": ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03",
                                        "2026-11-04", "2026-11-05"],
                               "weather_code": [2, 61, 3, 0, 95, 71, 45],
                               "temperature_2m_max": [64.1, 58.0, 55.2, 56.9, 52.5, 49.0, 50.1],
                               "temperature_2m_min": [50.0, 48.2, 45.1, 44.0, 41.7, 39.3, 40.8],
                               "precipitation_probability_max": [10, 80, 20, 0, 90, 60, 5]}}},
]


def redact_url(url: str, key: dict | None) -> str:
    """``url`` with the key's query value replaced by ``[redacted]`` (and any other credential spelling the log
    redaction knows), so a fixture never carries a key."""
    assert isinstance(url, str) and url, "url required"
    parts = urlsplit(url)
    pairs = [(k, REDACTED if key and key.get("in") == "query" and k == key.get("name") else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, safe="[]"), parts.fragment))
    out = logredact.redact(clean)
    assert key is None or key.get("in") != "query" or REDACTED in out, "the key's value is redacted"
    return out


def key_for(entry: dict, keys: dict[str, str]) -> str | None:
    """The key value to send for ``entry``: the user's for its provider, else its demo key; None when none."""
    if not entry.get("key"):
        return None
    if entry.get("force_demo"):
        return entry.get("demo")
    return keys.get(entry["provider"]) or entry.get("demo")


def fetch(entry: dict, keys: dict[str, str]) -> dict:
    """One guarded fetch of ``entry``: ``{status, content_type, body}``; a 4xx/5xx is recorded, not raised."""
    key = key_for(entry, keys)
    if entry.get("key") and not key:
        raise LookupError(f"{entry['name']}: no key for {entry['provider']} (pass --key {entry['provider']}=...)")
    url = entry["url"].format(key=key) if key else entry["url"]
    headers = None
    if entry.get("key") and entry["key"].get("in") == "header":
        headers = {entry["key"]["name"]: key}
        url = entry["url"].replace("{key}", "")
    try:
        # the guard the flow fetches with: address checks, pinned connection, redirects refused, byte cap
        got = netguard._guarded_get(url, ("application/json", "text/"), netguard._MAX_BYTES,
                                    extra_headers=netguard._json_headers(headers), allow_redirects=False)
    except netguard.FetchError as exc:
        if exc.status is None:
            raise
        # the guard refuses a 4xx before reading it; the refusal's body is the fixture here (a 401's JSON), read
        # once more by the same client, so a test can replay the same wall
        return {"status": exc.status, "content_type": exc.content_type or "", "body": _refusal_body(url, headers)}
    text = netguard.decode_body(got["content"], got["content_type"])
    try:
        body = json.loads(text)
    except ValueError:
        body = text
    return {"status": int(got["status"]), "content_type": str(got["content_type"] or "").split(";")[0].strip(),
            "body": body}


def _refusal_body(url: str, headers: dict | None) -> object:
    """The body of a request the guard refused for its status (it already passed the address checks)."""
    import httpx

    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        response = client.get(url, headers={"User-Agent": netguard.USER_AGENT, **(headers or {})})
    try:
        return response.json()
    except ValueError:
        return response.text[:4000]


def write(entry: dict, got: dict, out_dir: pathlib.Path, *, captured: bool, secret: str | None = None) -> pathlib.Path:
    """Write one fixture file; the URL is redacted and the body is as served. ``secret``: the user's key for this
    entry (never a demo key): a body that carries it is refused, not written (ValueError)."""
    path = out_dir / entry["host"] / f"{entry['name']}.json"
    record = {"name": entry["name"], "host": entry["host"], "url": redact_url(entry["url"].replace("{key}", "k"),
                                                                              entry.get("key")),
              "key": entry.get("key"), "status": got["status"], "content_type": got["content_type"],
              "captured": captured, "captured_at": datetime.now(UTC).isoformat(timespec="seconds") if captured else None,
              "note": entry["note"], "body": got["body"]}
    text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    assert REDACTED in text or not entry.get("key"), "a keyed fixture names where the key went, never its value"
    if secret and secret in text:
        raise ValueError(f"{entry['name']}: the provider's body carries the key, so it is not written")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--only", action="append", default=[], help="capture only these names")
    ap.add_argument("--key", action="append", default=[], metavar="PROVIDER=KEY")
    ap.add_argument("--provisional", action="store_true", help="write the documented shapes (captured: false)")
    args = ap.parse_args()
    logredact.install()  # a request line names the full address, key included: never printed
    keys: dict[str, str] = {}
    for spec in args.key:  # bounded: a few providers
        provider, _, value = spec.partition("=")
        if provider and value:
            keys[provider.strip().lower()] = value.strip()
    for entry in CATALOGUE:  # bounded: the catalogue
        env = os.environ.get(f"SB_CAPTURE_KEY_{entry['provider'].upper()}") if entry.get("provider") else None
        if env and entry["provider"] not in keys:
            keys[entry["provider"]] = env.strip()
    failures = 0
    for entry in CATALOGUE:  # bounded: the catalogue
        if args.only and entry["name"] not in args.only:
            continue
        if args.provisional:
            got = {"status": int(entry.get("provisional_status") or 200),
                   "content_type": str(entry.get("provisional_content_type") or "application/json"),
                   "body": entry["provisional"]}
            path = write(entry, got, args.out, captured=False)
            print(f"provisional {path}")
            continue
        secret = None if entry.get("force_demo") or not entry.get("key") else keys.get(entry["provider"])
        try:
            got = fetch(entry, keys)
            path = write(entry, got, args.out, captured=True, secret=secret)
        except Exception as exc:  # one provider's refusal never stops the others
            failures += 1
            print(f"FAILED     {entry['name']}: {type(exc).__name__}: {logredact.redact(str(exc))[:200]}")
            continue
        print(f"captured   {path} (HTTP {got['status']}, {got['content_type']})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
