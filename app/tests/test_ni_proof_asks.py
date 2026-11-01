"""The proof asks: the Library the user has, the bodies the providers really send, the production fetch path, T's
route table.

Three releases were announced fixed on fixtures typed by hand (0.27.1, 0.27.2, 0.27.3) and each failed on T's Mac
within the hour. These tests replay, through ``ni_flow.run_flow`` with its production fetcher, the rows the v1.6.0
pack really ranks for "NVDA stock price every 30 minutes" (``fixtures/library/rows-nvda.json``, and for the hourly
ask ``rows-nvda-hourly.json``, recorded with ``tools/ni-library-rows.py``) against the bodies the providers serve
(``fixtures/providers/<host>/<name>.json``,
captured with ``tools/ni-capture-fixtures.py``). The only seams are the model (scripted, but it reads the real menu
and copies a path from it), ``netguard._guarded_get_once`` (the one function that touches the network: the bytes
the provider sent, with its status and content type) and the politeness clock, so the key store, the host-bound
credential loader, the request builder, the politeness layer (host budget, pauses, limit bodies), the guard's JSON
parse and every build check run as they do on an install. ``test_every_provider_fixture_is_captured`` keeps the
suite honest: a provisional fixture (the documented shape, ``captured: false``) fails it until the real body is
captured, and every fixture must be the kind of body its catalogue entry is for.

What T's Mac showed on 2026-10-30 and each test here pins:
* Alpha Vantage's ``"Global Quote"`` / ``"05. price"`` keys yielded zero paths, so the row dropped with no model
  call: the walker now writes them as quoted keys and the quote builds.
* a 200 OK sentence (``{"Information": ...}``) was mapped by the model for five calls: it moves on at once.
* the Alpha Vantage key on the card made the key check skip the pause for the Finnhub row, whose fetch the loader
  then refused: the key check compares hosts, and the card asks for Finnhub's key.
* the daily series files its values under each day's date: it is refused with that reason, never mapped.
* research never ran: the card ended ``no_ready_source`` and research waited for a Keep looking tap nobody knew to
  make, while the consent question had promised research "with the same service" since 0.27.0; research now runs
  at once under that consent when every row is gone, and the card lists what it tried and why.

What the first capture showed on 2026-10-31:
* a second request inside one second gets a 200 OK "1 request per second" sentence, which a build read as "No ready
  source answers this yet": the host's "slow down" is a wait, never a verdict. A build waits out a one-second limit
  once and builds; a limit that holds ends it with a retry; a refresh stays due.
* Alpha Vantage answered a made-up key with a real quote: a quote proves nothing about the key, and the invalid-key
  sentence the first fixtures assumed was never seen.

What T chose on 2026-10-31 ("Prefer a source that fits", pack v1.6.0): every 30 minutes is 48 requests a day and
Alpha Vantage's free key allows 25, so a card built on it would stop updating from about midday. The pack carries
each provider's free limit, and the auto-pick tries the rows whose limit covers the cadence first: Finnhub (60 a
minute), then the Alpha Vantage rows. A card on Alpha Vantage says so under Keep, and says when it tries again
while the host holds it off. An hourly card is 24 a day, which Alpha Vantage covers, so for it the Library's own
order holds and Alpha Vantage goes first. The tests of what Alpha Vantage does once the build reaches it use its
two rows alone (``AV_ROWS``: the build after Finnhub's row was skipped) or the hourly ask.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import re
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

import duckdb
import pytest
from test_ni_answers import WEATHER_URL, lib  # noqa: F401  (lib is a fixture: the real Library shape with answers)
from test_ni_auto_pick import _row

from smartbrain_3000 import (
    auth,
    gateway,
    library_index,
    netguard,
    ni,
    ni_flow,
    ni_limits,
    ni_master,
    ni_message,
    ni_polite,
    ni_refresh,
    ni_research_flow,
    ni_routes,
    ni_standing,
)
from smartbrain_3000 import db as dbmod
from smartbrain_3000.secrets import SecretStore, gen_master_key

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PROVIDERS = FIXTURES / "providers"
CAPTURE_TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-capture-fixtures.py"
ROWS_NVDA = json.loads((FIXTURES / "library" / "rows-nvda.json").read_text(encoding="utf-8"))
ROWS_HOURLY = json.loads((FIXTURES / "library" / "rows-nvda-hourly.json").read_text(encoding="utf-8"))
ASK = ROWS_NVDA["ask"]
HOURLY = ROWS_HOURLY["ask"]   # "NVDA stock price every hour": 24 requests a day, which Alpha Vantage's 25 covers
AV_QUOTE, FINNHUB, AV_DAILY = (r["url"] for r in ROWS_NVDA["rows"])   # the Library's order
AV_ROWS = [ROWS_NVDA["rows"][0], ROWS_NVDA["rows"][2]]   # the two Alpha Vantage rows: the build after Finnhub's skip
AV_WINDOWS = [{"requests": 1, "seconds": 1}, {"requests": 25, "seconds": 86400}]
AV_LIMITS = {"provider": "Alpha Vantage", "keyed": True, "windows": AV_WINDOWS}
AV_LINE = ("Alpha Vantage's free key allows 25 requests a day. Refreshing every 30 minutes needs 48, so this card "
           "will stop updating partway through each day.")
T_ROUTES = {"ni": "claudecode/opus", "chat": "claudecode/sonnet", "agent": "claudecode/sonnet"}
T_ZONE = "America/New_York"   # T's Mac (2026-10-31: a test that only failed off UTC)
KEY_PARAMS = frozenset({"apikey", "token", "key", "api_key", "access_token"})
AV_KEY, FH_KEY = "av-test-key-0A1B2C3D", "fh-test-key-9Z8Y7X6W"


def fixture(name: str) -> dict:
    """One captured provider body by name."""
    hits = list(PROVIDERS.glob(f"*/{name}.json"))
    assert len(hits) == 1, f"one fixture named {name}: {hits}"
    return json.loads(hits[0].read_text(encoding="utf-8"))


def _store() -> ni.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return ni.NIStore(conn, gen_master_key())


class _Library:
    """The pack's rows for the ask, as ``ni_flow._library_candidates`` would offer them (no declared answers:
    every quote row of v1.6.0 is keyed and undeclared)."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(r) for r in rows]

    def candidates(self, request: str, limit: int = 3, hint: dict | None = None):
        return copy.deepcopy(self.rows[:limit]), []

    def answers(self, source_id: str) -> list[dict]:
        return []

    def get(self, source_id: str) -> dict | None:
        row = next((r for r in self.rows if r["source_id"] == source_id), None)
        return None if row is None else {"access": {"docs_url": (row.get("needs_key") or {}).get("docs_url", "")},
                                         "provider": {"url": f"https://{row['host']}"}}


def _base(url: str) -> str:
    """A request address with its key parameter left out, for matching a fixture to the address a row fetches."""
    parts = urlsplit(url)
    pairs = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in KEY_PARAMS)
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(pairs)}"


class _Replay:
    """``netguard._guarded_get_once`` as the providers answered: the fixture body, as the bytes and content type
    they sent, for the address a row fetches (``serves``: a fixture name, or names served in turn, the last
    repeating); the key checked the way the provider would (present, under the name the row declares, the value the
    user gave for THIS host); a 4xx fixture raised as the guard raises it. Every request is recorded, keys
    included, so a test can assert exactly what left this computer."""

    def __init__(self, serves: dict[str, str | list[str]], keys: dict[str, str] | None = None) -> None:
        self.serves = {_base(url): [fixture(n) for n in ([names] if isinstance(names, str) else names)]
                       for url, names in serves.items()}
        self.keys = dict(keys or {})
        self.fetched: list[str] = []
        self._sent: dict[str, int] = {}

    def serve(self, url: str, names: str | list[str]) -> None:
        """From now on ``url`` answers ``names`` (a provider whose answer changed)."""
        self.serves[_base(url)] = [fixture(n) for n in ([names] if isinstance(names, str) else names)]
        self._sent[_base(url)] = 0

    def __call__(self, url: str, allowed_ct: tuple, max_bytes: int, deadline_seconds=None, accept_zip_magic=False,
                 method: str = "GET", content=None, extra_headers: dict | None = None, allow_redirects: bool = True,
                 binary_kind=None) -> dict:
        assert method == "GET" and content is None, "a build and a refresh only read"
        self.fetched.append(url)
        parts = urlsplit(url)
        queue = self.serves.get(_base(url))
        assert queue, f"fetched an address no fixture answers: {_base(url)}"
        sent = self._sent.get(_base(url), 0)
        self._sent[_base(url)] = sent + 1
        fx = queue[min(sent, len(queue) - 1)]
        assert parts.hostname == fx["host"], "the fixture's own host"
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if fx.get("key"):
            name = fx["key"]["name"]
            assert fx["key"]["in"] == "query" and query.get(name) == self.keys.get(parts.hostname), \
                f"the key for {parts.hostname} rides as {name}=..., the one the user gave for that host"
            assert allow_redirects is False, "a keyed request never follows a redirect"
        else:
            assert not (set(query) & KEY_PARAMS), "a keyless source is fetched with no key"
        ctype = str(fx.get("content_type") or "application/json")
        if int(fx["status"]) >= 400:
            raise netguard.FetchError(f"upstream returned HTTP {fx['status']}", status=int(fx["status"]),
                                      content_type=ctype)
        assert ctype.startswith(allowed_ct) or "json" in ctype, "a content type the guard lets through"
        raw = json.dumps(fx["body"]).encode("utf-8")
        assert len(raw) <= max_bytes, "within the guard's byte cap"
        return {"final_url": url, "status": int(fx["status"]), "content_type": ctype, "content": raw}


_MENU_LINE = re.compile(r"^- (?P<path>.+?)  \((?P<type>[a-z]+)", re.MULTILINE)
_SHAPE_FIELD = re.compile(r'"(?P<name>[a-z0-9_]+)": "<(?P<type>number|string) path>"')
_ALIASES = {"price": ("c", "last", "close", "price")}   # how the quote providers name a current price


class _Model:
    """The build's model, scripted by stage; the mapping stage READS the menu the flow printed and copies one
    offered path per field (by name, then by the provider's own alias for it, then the first of the type), so a
    spelling the flow cannot print or verify fails here as it would with a real model. Counts calls by stage.
    ``cadence``: the minutes the intent reads from the ask (30 for the proof ask, 60 for the hourly one)."""

    def __init__(self, picks: dict[str, str] | None = None, cadence: int = 30) -> None:
        self.calls: dict[str, int] = {"intent": 0, "mapping": 0, "judge": 0, "other": 0}
        self.picks = picks  # a forced reply for the mapping stage (a test of invalid picks)
        self.cadence = cadence

    def __call__(self, model: str, prompt: str) -> str:
        assert model == "claudecode/opus", f"T's build model, consented; got {model}"
        if "Classify a dashboard-card request" in prompt:
            self.calls["intent"] += 1
            return json.dumps({"kind": "external_data", "subject": "NVDA", "cadence_minutes": self.cadence,
                               "wants": ["price"], "threshold": None, "display_hint": "value"})
        if "Choose the best candidate path" in prompt:
            self.calls["mapping"] += 1
            if self.picks is not None:
                return json.dumps(self.picks)
            return json.dumps(self._pick(prompt))
        if '"serves"' in prompt or "serves" in prompt and "wrong" in prompt:
            self.calls["judge"] += 1
            return json.dumps({"serves": True, "gaps": [], "wrong": []})
        self.calls["other"] += 1
        return "{}"

    @staticmethod
    def _pick(prompt: str) -> dict:
        menu = [(m.group("path"), m.group("type")) for m in _MENU_LINE.finditer(prompt)]
        assert menu, "the menu lists the offered paths"
        out = {}
        for m in _SHAPE_FIELD.finditer(prompt):  # bounded: the fields (≤4)
            name, wanted = m.group("name"), m.group("type")
            typed = [p for p, t in menu if t == wanted]
            named = [p for p in typed if name in ni_flow._path_tail_slug(p)]
            aliased = [p for p in typed if ni_flow._path_tail_slug(p) in _ALIASES.get(name, ())]
            out[name] = (named or aliased or typed or [menu[0][0]])[0]
        return out


class _Research:
    """The researcher's model seam on the consented route, scripted: every reply is prose with no tool call, so the
    run ends in its own verdict (``research_gave_up``), never a fault. Records each call, so a test can assert that
    research ran at all (T's install never researched: the tap was the gate), and on which route."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, model: str, messages: list, tools: list | None) -> dict:
        assert model == "claudecode/opus", f"research runs on the route the card's consent names; got {model}"
        self.seen.append(model)
        return {"choices": [{"message": {"role": "assistant", "content": "still looking"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20}}


def _researcher() -> _Research:
    """Install the research seam for claudecode/opus; the card's own consent decides whether it may run."""
    seam = _Research()
    ni_research_flow.set_research_chat(seam, cloud="claudecode/opus")
    return seam


def _tried(store: ni.NIStore, item_id: str) -> list[tuple[str, str]]:
    """What the card shows it tried: (host, why), in order."""
    field = ni_flow.board_flow_field(store, item_id, read_only=True) or {}
    return [(t["host"], t["why"]) for t in field.get("tried") or []]


@pytest.fixture(autouse=True)
def _t_install(monkeypatch):
    """T's install: every route Claude Code and no local model, research on its default (Automatic: the suite's
    conftest turns it off for every other test), the card's consent for claudecode/opus sealed, the key store wired
    as ``main`` wires it, no search provider, and T's Mac's zone (America/New_York; the databases keep production's
    UTC pin, conftest). The politeness clock is a test clock: a wait moves it (``slept``)."""
    for name in ("SMARTBRAIN_NI_RESEARCH", "SMARTBRAIN_NI_RESEARCH_MODEL", "SMARTBRAIN_NI_RESEARCH_WEB"):
        monkeypatch.delenv(name, raising=False)
    if hasattr(time, "tzset"):
        monkeypatch.setenv("TZ", T_ZONE)
        time.tzset()
    monkeypatch.setattr(gateway, "load_routes", lambda conn: dict(T_ROUTES))
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    clock = SimpleNamespace(now=1000.0, slept=[])

    def sleep(seconds: float) -> None:
        clock.slept.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(ni_polite, "_clock", lambda: clock.now)
    monkeypatch.setattr(ni_polite, "_sleep", sleep)
    yield clock
    ni_flow.set_secrets_provider(None)
    ni_flow.set_library_provider(None)
    ni_research_flow.set_research_chat(None)
    monkeypatch.undo()
    if hasattr(time, "tzset"):
        time.tzset()


def _card(monkeypatch, rows: list[dict], replay: _Replay, ask: str = ASK) -> tuple[ni.NIStore, str, SecretStore]:
    """A shell card for ``ask`` with claudecode/opus consented (the consent question is tested in
    test_ni_route_matrix), over ``rows``, fetching through ``replay``."""
    library = _Library(rows)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: library)
    monkeypatch.setattr(netguard, "_guarded_get_once", replay)
    store = _store()
    secrets = SecretStore(store.conn, gen_master_key())
    ni_flow.set_secrets_provider(lambda: secrets)
    item_id = ni_flow.create_shell_item(store, ask)
    ni_flow._flow_write(store, item_id, {**ni_flow._make_record(ask, "intent"), "_model_consent": "claudecode/opus",
                                         "_model_consent_web": True})
    return store, item_id, secrets


def _held(store: ni.NIStore, item_id: str, out: dict, url: str, path: str = "mapping") -> dict:
    """The card SmartBrain's own pick built, held for the user's Keep."""
    assert out["state"] == "ready", (out.get("state"), out.get("error"), out.get("notes"))
    item = store.get_item(item_id)
    assert item["spec"]["_built_from"]["path"] == path and ni.awaits_yes(item), "held for Keep"
    assert item["spec"]["source"]["url"] == url and item["state"] == "commissioning"
    assert item_id not in [i["id"] for i in store.due_items()], "nothing recurs before Keep"
    return item


def _notes(store: ni.NIStore, item_id: str) -> str:
    return json.dumps((ni_flow._flow_read(store, item_id) or {}).get("notes"))


def _board(store: ni.NIStore, item_id: str) -> dict:
    """The card's row as ``GET /api/ni/board`` serves it (its ``limit`` line and ``waiting`` block included)."""
    return ni_routes._board_row(store, store.get_item(item_id))


# ---- the fixtures themselves ----------------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(time, "tzset"), reason="the zone is set with time.tzset")
def test_these_tests_run_in_ts_zone_on_productions_utc_database() -> None:
    """T's Mac runs America/New_York; a database the app opens is pinned to UTC (``db.open_db``) and so is every one
    the suite opens (conftest). Both hold here, so a time the build writes reads back as it does on T's install."""
    assert datetime.now().astimezone().tzname() in ("EDT", "EST")
    conn = duckdb.connect(":memory:")
    assert conn.execute("SELECT current_setting('TimeZone');").fetchone()[0] == "UTC"
    assert conn.cursor().execute("SELECT current_setting('TimeZone');").fetchone()[0] == "UTC"


def test_the_recorded_rows_are_the_packs_three_keyed_undeclared_quote_rows() -> None:
    """What the v1.6.0 pack offers for the NVDA ask, in the Library's order (T's Mac ranked the same three on
    v1.5.0), each with the free request limits its provider documents, so the matrix below runs over the Library the
    user has, not rows a test invented. The pack ranks the same rows for the hourly ask."""
    assert ROWS_NVDA["pack_version"] == ROWS_HOURLY["pack_version"] == "v1.6.0"
    rows = ROWS_NVDA["rows"]
    assert [r["source_id"] for r in rows] == ["alphavantage-quote", "finnhub-quote", "alphavantage-daily"]
    assert all(r["needs_key"] and r["needs_key"]["in"] == "query" and r["answers"] is None for r in rows)
    assert [r["needs_key"]["name"] for r in rows] == ["apikey", "token", "apikey"]
    assert [r["host"] for r in rows] == ["www.alphavantage.co", "finnhub.io", "www.alphavantage.co"]
    assert [r["limits"] for r in rows] == [AV_WINDOWS, [{"requests": 60, "seconds": 60}], AV_WINDOWS]
    assert ROWS_HOURLY["ask"] == "NVDA stock price every hour" and ROWS_HOURLY["rows"] == rows


def test_every_recorded_row_fixture_comes_from_the_pack_this_build_pins() -> None:
    """The rows a test replays are the pinned pack's (``library_index.PACK``): a pack bump re-records them with
    ``tools/ni-library-rows.py``, or this fails, so the matrix never runs over a Library no install has."""
    recorded = sorted((FIXTURES / "library").glob("rows-*.json"))
    assert len(recorded) >= 3, "rows-nvda, rows-nvda-hourly and rows-msft"
    for path in recorded:  # bounded: the fixtures directory
        assert json.loads(path.read_text(encoding="utf-8"))["pack_version"] == library_index.PACK["tag"], path.name


def _pick_order(rows: list[dict], cadence: object) -> list[str]:
    """The order the auto-pick tries ``rows`` in for an intent of ``cadence`` minutes."""
    return [r["source_id"] for r in ni_flow._by_fit(ni_flow._library_rows(copy.deepcopy(rows)),
                                                    {"cadence_minutes": cadence})]


def test_the_pick_tries_first_the_source_whose_free_limit_covers_the_cadence() -> None:
    """T, 2026-10-31: "Prefer a source that fits". Every 30 minutes is 48 requests a day: Finnhub's free key (60 a
    minute) covers it and Alpha Vantage's (25 a day) does not, so Finnhub goes first and the Alpha Vantage rows after
    it, in the Library's order. From every 58 minutes (25 a day) Alpha Vantage covers it and the Library's order
    holds. A cadence under the third-party floor counts as the floor (a card is never refreshed faster), and an ask
    with no cadence is the default card's."""
    rows = ROWS_NVDA["rows"]
    finnhub_first = ["finnhub-quote", "alphavantage-quote", "alphavantage-daily"]
    library_order = ["alphavantage-quote", "finnhub-quote", "alphavantage-daily"]
    assert _pick_order(rows, 30) == _pick_order(rows, 57) == finnhub_first
    assert _pick_order(rows, 58) == _pick_order(rows, 60) == _pick_order(rows, 24 * 60) == library_order
    assert ni_limits.need(86400, 57) == 26 and ni_limits.need(86400, 58) == 25, "the boundary: 25 a day"
    assert _pick_order(rows, 1) == finnhub_first, "every minute counts as every 15 (96 a day)"
    assert _pick_order(rows, None) == _pick_order(rows, "30") == _pick_order(rows, True) == finnhub_first, \
        "no cadence or a malformed one is the default card's (every 15 minutes)"
    assert ni_flow._DEFAULT_CADENCE == ni_polite.CADENCE_FLOOR_MINUTES == 15


def test_the_pick_counts_the_cadence_a_card_really_refreshes_at() -> None:
    """A card on a third-party host is never refreshed faster than every 15 minutes (96 requests a day), so a source
    allowing 100 a day covers a five-minute ask there and goes first; on an own host there is no floor, so five
    minutes is 288 a day and the same limit does not cover it. Mutation check: a pick that ignored the floor sent
    the five-minute ask past a source that serves it."""
    free = {**ROWS_NVDA["rows"][1], "source_id": "free-100", "url": "https://quotes.example.org/v1/nvda.json",
            "host": "quotes.example.org", "needs_key": None, "limits": [{"requests": 100, "seconds": 86400}]}
    own = {**free, "source_id": "own-100", "url": "http://quotes.local/nvda.json", "host": "quotes.local"}
    rows = [*ROWS_NVDA["rows"], free]
    assert _pick_order(rows, 5) == ["free-100", "finnhub-quote", "alphavantage-quote", "alphavantage-daily"]
    assert _pick_order([*ROWS_NVDA["rows"], own], 5) == ["finnhub-quote", "own-100", "alphavantage-quote",
                                                        "alphavantage-daily"]
    assert _pick_order([*ROWS_NVDA["rows"], own], 15) == ["own-100", "finnhub-quote", "alphavantage-quote",
                                                         "alphavantage-daily"]


def test_a_limit_nobody_documented_counts_as_covering_and_keyless_rows_go_first_within_a_group() -> None:
    """Most providers document no number: an unknown limit covers, so a pack without limits (v1.5.0) and a malformed
    field (data from a pack is never trusted to be well formed) keep the order the auto-pick had before. Within the
    rows that cover, and within the rows that don't, a row that needs no key goes before one that asks for it; a
    keyless row that runs out still goes after a keyed one that covers."""
    rows = ROWS_NVDA["rows"]
    library_order = ["alphavantage-quote", "finnhub-quote", "alphavantage-daily"]
    assert _pick_order([{**r, "limits": None} for r in rows], 30) == library_order, "a pack without limits"
    malformed = [{"requests": 25, "seconds": 86400}, {"requests": 0, "seconds": 1}]
    assert _pick_order([{**r, "limits": malformed} for r in rows], 30) == library_order, "all or nothing: unknown"
    keyless = {**rows[1], "source_id": "free-quote", "url": "https://quotes.example.org/v1/nvda.json",
               "host": "quotes.example.org", "needs_key": None, "limits": None}
    assert _pick_order([*rows, keyless], 30) == ["free-quote", "finnhub-quote", "alphavantage-quote",
                                                 "alphavantage-daily"]
    runs_out = {**keyless, "source_id": "free-but-short", "limits": [{"requests": 10, "seconds": 86400}]}
    assert _pick_order([runs_out, *rows], 30) == ["finnhub-quote", "free-but-short", "alphavantage-quote",
                                                  "alphavantage-daily"]


def test_every_provider_fixture_is_captured() -> None:
    """A provisional fixture is the documented shape, typed by hand: the exact thing three releases were wrongly
    cleared on. This fails until ``tools/ni-capture-fixtures.py`` has replaced each one with the body the provider
    sent, on a machine that reaches the provider."""
    provisional = sorted(p.stem for p in PROVIDERS.glob("*/*.json")
                         if not json.loads(p.read_text(encoding="utf-8")).get("captured"))
    assert provisional == [], f"still the documented shape, not a captured body: {provisional}"


def _capture_tool():
    spec = importlib.util.spec_from_file_location("ni_capture_fixtures", CAPTURE_TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


@pytest.mark.skipif(not CAPTURE_TOOL.exists(), reason="the capture tool ships in the repository only")
def test_every_fixture_is_the_kind_of_body_its_catalogue_entry_is_for() -> None:
    """2026-10-31: the first capture saved Alpha Vantage's "1 request per second" sentence as the daily series,
    ``captured: true``, and an entry expected an invalid-key sentence nobody had seen (a made-up key got a real
    quote). Every fixture is checked by the capture tool's own rule (``check``: the status, the kind of body by the
    app's ``ni_message`` reading, the keys data must carry), and every entry has exactly its one fixture."""
    tool = _capture_tool()
    files = {p.stem: p for p in PROVIDERS.glob("*/*.json")}
    assert sorted(files) == sorted(e["name"] for e in tool.CATALOGUE), "one fixture per catalogue entry, no other"
    for entry in tool.CATALOGUE:  # bounded: the catalogue
        fx = json.loads(files[entry["name"]].read_text(encoding="utf-8"))
        assert fx["host"] == entry["host"] and fx["key"] == entry.get("key"), entry["name"]
        assert tool.check(entry, fx) == [], f"{entry['name']}: {tool.check(entry, fx)}"
    kinds = {e["name"]: e["expect"]["kind"] for e in tool.CATALOGUE}
    assert kinds["alphavantage-burst-limited"] == "limit" and kinds["alphavantage-made-up-key"] == "data"


def test_no_fixture_carries_a_key() -> None:
    files = sorted(PROVIDERS.glob("*/*.json"))
    assert len(files) >= 7, "the catalogue"
    for path in files:  # bounded: the fixture files
        fx = json.loads(path.read_text(encoding="utf-8"))
        query = dict(parse_qsl(urlsplit(fx["url"]).query, keep_blank_values=True))
        if fx.get("key"):
            assert query.get(fx["key"]["name"]) == "[redacted]", f"{path.name}: the key's value is redacted"
        assert not any(v and k in KEY_PARAMS and v != "[redacted]" for k, v in query.items()), path.name
        assert fx["host"] == urlsplit(fx["url"]).hostname and isinstance(fx["status"], int)


# ---- the quote row: the real body through the production fetch path -----------------------------------------------

def test_the_quote_row_builds_from_the_real_body_with_the_users_key_on_the_production_fetch_path(monkeypatch) -> None:
    """The NVDA ask on T's install once the build reaches Alpha Vantage's rows (Finnhub's skipped), end to end: the
    quote row asks for its key before anything is fetched; the key goes under this card bound to
    www.alphavantage.co; the build samples through ``ni_flow.default_fetcher`` → ``_fetch_with_access`` →
    ``ni.http_request_parts`` → ``ni_fetch`` → ``netguard`` with ``apikey=`` on the request and refuses redirects;
    the walker offers Alpha Vantage's spaced, numbered keys as quoted paths; the model copies one from the menu; the
    price (sent as text) is converted; the card is held for Keep with the quoted path in its sealed pipeline, the
    key only as a reference, and the source's free limits sealed with it."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": AV_KEY})
    model = _Model()
    store, item_id, secrets = _card(monkeypatch, AV_ROWS, replay)
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["source_url"] == AV_QUOTE and replay.fetched == []
    assert out["_access"]["host"] == "www.alphavantage.co" and out["_access"]["key"]["name"] == "apikey"
    assert model.calls == {"intent": 1, "mapping": 0, "judge": 0, "other": 0}, "nothing but the intent before the key"
    assert ni_flow.give_access(store, item_id, secrets, key=AV_KEY, email=None) == []
    assert json.loads(secrets.get(f"ni:{item_id}:api_key")) == {"value": AV_KEY, "host": "www.alphavantage.co"}
    out = ni_flow.run_flow(store, item_id, gateway_call=model, source_url=AV_QUOTE)
    item = _held(store, item_id, out, AV_QUOTE)
    assert replay.fetched == [f"{AV_QUOTE}&apikey={AV_KEY}"], "one sample, the production request"
    assert model.calls["mapping"] >= 1 and model.calls["intent"] == 1, "the sealed intent is reused after the pause"
    body = fixture("alphavantage-global-quote")["body"]["Global Quote"]
    extract = item["spec"]["pipeline"][0]
    assert extract["op"] == "extract" and extract["paths"]["price"] == '["Global Quote"]["05. price"]', extract
    assert item["spec"]["pipeline"][1] == {"op": "transform", "apply": [{"fn": "number", "field": "price"}]}
    assert ni.run_pipeline(item["spec"]["pipeline"], fixture("alphavantage-global-quote")["body"]) == \
        {"price": float(body["05. price"])}, "the sealed pipeline reads the quote's price as a number"
    assert body["05. price"].rstrip("0") in json.dumps(store.read_snapshot(item_id, "preview")), "shown on the card"
    assert item["spec"]["source"]["secret_query"] == {"apikey": {"$secret": f"ni:{item_id}:api_key"}}
    sealed = json.dumps(item["spec"]) + json.dumps(ni_flow._flow_read(store, item_id))
    assert AV_KEY not in sealed, "the key is stored once, host-bound, and referenced everywhere else"
    assert item["spec"]["_built_from"] == {"path": "mapping", "host": "www.alphavantage.co",
                                           "title": ROWS_NVDA["rows"][0]["title"], "limits": AV_LIMITS}
    ni.validate_spec(item["spec"])  # the sealed card is a valid card: its quoted path is grammar, not a hack


def test_the_sealed_card_refreshes_through_the_same_request(monkeypatch) -> None:
    """The engine's refresh (``ni.run_item``) reads the card's pipeline over a fresh fetch with the same key
    reference: the quoted path resolves there too, with the price converted."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": AV_KEY})
    store, item_id, secrets = _card(monkeypatch, AV_ROWS, replay)
    ni_flow.run_flow(store, item_id, gateway_call=_Model())
    ni_flow.give_access(store, item_id, secrets, key=AV_KEY, email=None)
    out = ni_flow.run_flow(store, item_id, gateway_call=_Model(), source_url=AV_QUOTE)
    _held(store, item_id, out, AV_QUOTE)
    monkeypatch.setattr(ni, "_clock", lambda: datetime.now().astimezone() + timedelta(minutes=31))
    ni.run_item(store, item_id, gateway_mod=gateway, secrets_store=secrets)
    assert len(replay.fetched) == 2 and all(u == f"{AV_QUOTE}&apikey={AV_KEY}" for u in replay.fetched)
    latest = store.read_snapshot(item_id, "latest")
    price = fixture("alphavantage-global-quote")["body"]["Global Quote"]["05. price"]
    assert latest["ok"] and price.rstrip("0") in json.dumps(latest["payload"]), latest
    runs = store.conn.execute("SELECT status FROM ni_runs WHERE item_id = ?;", [item_id]).fetchall()
    assert runs and runs[-1][0] == "ok", runs


# ---- the source that fits the cadence (pack v1.6.0) ------------------------------------------------------------

def test_the_nvda_ask_tries_finnhub_first_and_alpha_vantage_is_sent_nothing(monkeypatch) -> None:
    """The proof ask with both keys at hand (T has both; the Alpha Vantage one already on another card): the build
    pauses first for Finnhub's key, since its free key covers every 30 minutes and Alpha Vantage's 25 a day do not
    (T's Mac built on Alpha Vantage, which would have stopped updating from about midday). Finnhub's key builds the
    card from its flat body, the card refreshes through the same request, and nothing is ever sent to Alpha
    Vantage. Finnhub's limits are sealed with the card, and they leave the card nothing to say."""
    replay = _Replay({FINNHUB: "finnhub-quote"}, {"finnhub.io": FH_KEY})
    model = _Model()
    store, item_id, secrets = _card(monkeypatch, ROWS_NVDA["rows"], replay)
    ni.put_credential(secrets, "another-card", ni_flow.KEY_PARAM, AV_KEY, "www.alphavantage.co")
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["source_url"] == FINNHUB and out["_access"]["host"] == "finnhub.io"
    assert replay.fetched == [] and not out["_access"].get("reuse"), "a key for another host is never offered"
    assert ni_flow.give_access(store, item_id, secrets, key=FH_KEY, email=None) == []
    item = _held(store, item_id, ni_flow.run_flow(store, item_id, gateway_call=model, source_url=FINNHUB), FINNHUB)
    assert item["spec"]["pipeline"][0]["paths"]["price"] == "c"
    assert item["spec"]["_built_from"]["limits"] == {"provider": "Finnhub", "keyed": True,
                                                     "windows": [{"requests": 60, "seconds": 60}]}
    ni.validate_spec(item["spec"])
    assert "limit" not in _board(store, item_id) and "waiting" not in _board(store, item_id)
    monkeypatch.setattr(ni, "_clock", lambda: datetime.now().astimezone() + timedelta(minutes=31))
    ni.run_item(store, item_id, gateway_mod=gateway, secrets_store=secrets)
    assert store.read_snapshot(item_id, "latest")["ok"]
    assert replay.fetched == [f"{FINNHUB}&token={FH_KEY}"] * 2, "the build's sample and the refresh; no other host"
    assert AV_KEY not in _kept(store, item_id) and FH_KEY not in _kept(store, item_id)


def test_with_only_an_alpha_vantage_key_the_card_says_at_keep_that_it_will_run_out(monkeypatch) -> None:
    """No Finnhub key: Skip at Finnhub's pause, and the build goes on to Alpha Vantage's quote row, which asks for
    its key. The card builds from it and, held for Keep, says that Alpha Vantage's free key allows 25 requests a
    day while refreshing every 30 minutes needs 48 (T, 2026-10-31: "says so when none does"). Finnhub was sent
    nothing."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": AV_KEY})
    model = _Model()
    store, item_id, secrets = _card(monkeypatch, ROWS_NVDA["rows"], replay)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["_access"]["host"] == "finnhub.io"
    assert ni_flow.skip_source(store, item_id) is True
    out = ni_flow.run_flow(store, item_id, gateway_call=model)   # the worker the skip started
    assert out["state"] == "awaiting_access" and out["source_url"] == AV_QUOTE
    assert ni_flow.give_access(store, item_id, secrets, key=AV_KEY, email=None) == []
    item = _held(store, item_id, ni_flow.run_flow(store, item_id, gateway_call=model, source_url=AV_QUOTE), AV_QUOTE)
    assert item["spec"]["_built_from"]["limits"] == AV_LIMITS and item["interval_minutes"] == 30
    row = _board(store, item_id)
    assert row["limit"] == AV_LINE and row["awaiting_yes"] and "waiting" not in row
    assert replay.fetched == [f"{AV_QUOTE}&apikey={AV_KEY}"], "Finnhub was sent nothing"


# ---- a sentence instead of data --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["alphavantage-demo-key-refused"])
def test_a_sentence_body_moves_on_at_once_with_no_model_call_and_the_reason_on_the_record(monkeypatch, name) -> None:
    """Alpha Vantage refuses its demo key on a real symbol with a 200 OK sentence. The row moves on with the
    sentence's gist as its reason and not one mapping or judge call; the pick goes on to Finnhub, which asks for
    its own key. (An hourly card: Alpha Vantage's free key covers it, so its quote row goes first. A limit sentence
    is a wait, not a refusal: the tests below.)"""
    replay = _Replay({AV_QUOTE: name}, {"www.alphavantage.co": AV_KEY})
    model = _Model(cadence=60)
    store, item_id, secrets = _card(monkeypatch, ROWS_HOURLY["rows"], replay, ask=HOURLY)
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["source_url"] == AV_QUOTE, "the Library's order holds"
    ni_flow.give_access(store, item_id, secrets, key=AV_KEY, email=None)
    out = ni_flow.run_flow(store, item_id, gateway_call=model, source_url=AV_QUOTE)
    assert replay.fetched == [f"{AV_QUOTE}&apikey={AV_KEY}"]
    assert model.calls["mapping"] == 0 and model.calls["judge"] == 0, "no model reads a sentence"
    assert out["state"] == "awaiting_access" and out["source_url"] == FINNHUB, "on to the next row, which asks"
    notes = _notes(store, item_id)
    assert "answered with a message instead of data" in notes, notes
    gist = fixture(name)["body"][next(iter(fixture(name)["body"]))]
    assert " ".join(gist.replace("*", "").split())[:60] in notes, "the provider's own words are the reason"
    assert "www.alphavantage.co" in notes or "Alpha Vantage" in notes


def test_provider_message_tells_a_sentence_from_data() -> None:
    for name in ("alphavantage-global-quote", "alphavantage-time-series-daily", "alphavantage-made-up-key",
                 "finnhub-quote", "open-meteo-forecast"):
        assert ni_flow.provider_message(fixture(name)["body"]) is None, f"{name} is data"
        assert ni_message.host_limit(fixture(name)["body"]) is None, f"{name} is no limit"
    for name in ("alphavantage-demo-key-refused", "alphavantage-burst-limited", "finnhub-quote-no-key"):
        assert ni_flow.provider_message(fixture(name)["body"]), f"{name} is a sentence"
    assert ni_message.host_limit(fixture("alphavantage-demo-key-refused")["body"]) is None, "a refusal, not a wait"
    assert ni_message.host_limit(fixture("alphavantage-burst-limited")["body"])[0] == ni_message.WAIT_SECOND_S
    assert ni_flow.provider_message({"status": "ok", "note": "Data under a free license for personal use",
                                     "price": "123.4"}) is None, "a number sent as text makes it data"
    assert ni_flow.provider_message({"status": {"error_code": 429, "error_message": "You've exceeded the Rate Limit."}})
    assert ni_flow.provider_message({"code": 429, "status": "error",
                                     "message": "You have run out of API credits for the current minute."})
    assert ni_flow.provider_message({"cod": "429", "message": "Your account is temporarily blocked"}), "a code as text"
    assert ni_flow.provider_message({"message": "Unauthorized"}) is None, "one word is not a sentence to quote"
    assert ni_flow.provider_message({"rows": [], "message": "no rows matched your query today"}) is None, "a list is data"
    # a key that names data makes the body data, whatever text sits beside it
    assert ni_flow.provider_message({"page": {"title": "text only", "note": "no number anywhere"}}) is None
    assert ni_flow.provider_message({"status": {"indicator": "none", "description": "All Systems Operational"}}) is None
    assert ni_flow.provider_message({"Global Quote": {}}) is None, "an empty quote is no sentence"
    assert ni_flow.provider_message({"error": {"error": {"message": "nested three deep is not a message body"}}}) is None


# ---- the host asks SmartBrain to wait ----------------------------------------------------------------------------

def _keyed_quote_card(monkeypatch, rows: list[dict], replay: _Replay, model: _Model) -> tuple[ni.NIStore, str, dict]:
    """The NVDA card past its key pause: the user's Alpha Vantage key given, the quote row's build run (``rows``
    the Alpha Vantage rows, which the build reaches after a Finnhub skip)."""
    store, item_id, secrets = _card(monkeypatch, rows, replay)
    ni_flow.run_flow(store, item_id, gateway_call=model)
    ni_flow.give_access(store, item_id, secrets, key=AV_KEY, email=None)
    return store, item_id, ni_flow.run_flow(store, item_id, gateway_call=model, source_url=AV_QUOTE)


def test_a_one_second_limit_is_waited_out_once_and_the_quote_builds(monkeypatch, _t_install) -> None:
    """T's Mac, 2026-10-31: a second Alpha Vantage request inside one second got the "1 request per second"
    sentence, and a build that read it ended "No ready source answers this yet". The politeness layer reads the
    sentence as the host asking to wait: a build someone is waiting on waits the second it named, once, asks again
    and builds from the quote, with no model call over the sentence."""
    replay = _Replay({AV_QUOTE: ["alphavantage-burst-limited", "alphavantage-global-quote"]},
                     {"www.alphavantage.co": AV_KEY})
    model = _Model()
    store, item_id, out = _keyed_quote_card(monkeypatch, AV_ROWS, replay, model)
    item = _held(store, item_id, out, AV_QUOTE)
    assert replay.fetched == [f"{AV_QUOTE}&apikey={AV_KEY}"] * 2, "the request, and once more after the wait"
    assert _t_install.slept == [ni_message.WAIT_SECOND_S], "the wait the host named, once"
    assert item["spec"]["pipeline"][0]["paths"]["price"] == '["Global Quote"]["05. price"]'
    assert model.calls["mapping"] >= 1 and model.calls["judge"] <= 1


def test_a_limit_that_holds_ends_the_build_with_a_retry_and_the_hosts_words(monkeypatch, _t_install) -> None:
    """The host asks again after the wait: the build is not a verdict on the source. Every row of that host goes
    for this build (one wall for every tenant), no model reads the sentence, no research runs past a source that is
    only busy, and the card ends failed with a retry and the host's own words, never declined. While the host's
    pause holds a retry sends nothing and says the same; once it ends, the retry builds."""
    replay = _Replay({AV_QUOTE: "alphavantage-burst-limited", AV_DAILY: "alphavantage-time-series-daily"},
                     {"www.alphavantage.co": AV_KEY})
    model = _Model()
    researched: list[str] = []
    monkeypatch.setattr(ni_research_flow, "fallback", lambda *a, **k: researched.append("ran") or {})
    store, item_id, out = _keyed_quote_card(monkeypatch, AV_ROWS, replay, model)
    assert out["state"] == "failed" and ni_master.error_class_of(out["error"]) == "waited", out
    assert replay.fetched == [f"{AV_QUOTE}&apikey={AV_KEY}"] * 2, "asked twice, the daily row never fetched"
    assert model.calls["mapping"] == 0 and model.calls["judge"] == 0 and researched == []
    said = fixture("alphavantage-burst-limited")["body"]["Information"]
    assert "www.alphavantage.co asked SmartBrain to wait" in out["error"] and said[:60] in out["error"], out["error"]
    assert ni_master.reason_for("failed", out["error"]).startswith("The source asked SmartBrain to wait")
    assert ni_master.choose_move(state="failed", error_class="waited", shell=True)["reopen"] == ["retry", "pick_source"]
    assert not (ni_flow._flow_read(store, item_id) or {}).get("_decline_card"), "never a decline"
    # Retry while the host's pause holds: nothing is sent, the same words
    record = ni_flow._flow_read(store, item_id)
    ni_flow._flow_write(store, item_id, ni_flow._fresh_record(record, ASK, "intent", "retry", keep_named=True),
                        revive=True)
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "failed" and ni_master.error_class_of(out["error"]) == "waited", out
    assert len(replay.fetched) == 2 and said[:60] in out["error"], "no request while the host's pause holds"
    # Retry after it: the host answers, the card builds
    _t_install.now += ni_message.WAIT_SECOND_S + 1
    replay.serve(AV_QUOTE, "alphavantage-global-quote")
    record = ni_flow._flow_read(store, item_id)
    ni_flow._flow_write(store, item_id, ni_flow._fresh_record(record, ASK, "intent", "retry", keep_named=True),
                        revive=True)
    _held(store, item_id, ni_flow.run_flow(store, item_id, gateway_call=model), AV_QUOTE)


def test_a_refresh_the_host_asks_to_wait_stays_due_and_is_never_a_failure(monkeypatch, _t_install) -> None:
    """A kept card whose refresh gets the host's limit sentence is not failed (no run row, no streak, no repair, the
    last value stays): the host is paused for the window it named and the card stays due; the next refresh after
    the pause reads the quote. Before 2026-10-31 the sentence was read as the card's data and failed it, and nothing
    on the card said why it stopped updating: the board row now carries the host's wait (when the card tries again,
    in the host's words) beside the line about the free limit its cadence overruns, until the pause ends."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": AV_KEY})
    store, item_id, secrets = _card(monkeypatch, AV_ROWS, replay)
    ni_flow.run_flow(store, item_id, gateway_call=_Model())
    ni_flow.give_access(store, item_id, secrets, key=AV_KEY, email=None)
    _held(store, item_id, ni_flow.run_flow(store, item_id, gateway_call=_Model(), source_url=AV_QUOTE), AV_QUOTE)
    assert _board(store, item_id)["limit"] == AV_LINE and "waiting" not in _board(store, item_id)
    monkeypatch.setattr(ni, "_clock", lambda: datetime.now().astimezone() + timedelta(minutes=31))
    replay.serve(AV_QUOTE, "alphavantage-burst-limited")
    with pytest.raises(ni.NIError) as busy:
        ni.run_item(store, item_id, gateway_mod=gateway, secrets_store=secrets)
    assert busy.value.kind == "host_busy" and busy.value.kind in ni.CAPACITY_KINDS
    assert _t_install.slept == [], "a refresh never waits: the card stays due"
    item = store.get_item(item_id)
    assert item["consecutive_failures"] == 0 and store.conn.execute(
        "SELECT count(*) FROM ni_runs WHERE item_id = ? AND status <> 'ok';", [item_id]).fetchone()[0] == 0
    before = datetime.now(UTC)
    row = _board(store, item_id)
    waiting = row["waiting"]
    assert set(waiting) == {"host", "until", "said"} and waiting["host"] == "www.alphavantage.co"
    assert "1 request per second" in waiting["said"] and waiting["said"] == ni_polite.SHARED._said(waiting["host"])
    until = datetime.fromisoformat(waiting["until"])
    assert until.utcoffset() == timedelta(0) and timedelta(0) <= until - before.replace(microsecond=0) \
        <= timedelta(seconds=ni_message.WAIT_SECOND_S + 1), "when the card tries again, in UTC"
    assert row["limit"] == AV_LINE, "the line about the limit rides beside the wait"
    _t_install.now += ni_message.WAIT_SECOND_S + 1
    assert "waiting" not in _board(store, item_id), "the pause is over: no wait to show"
    replay.serve(AV_QUOTE, "alphavantage-global-quote")
    ni.run_item(store, item_id, gateway_mod=gateway, secrets_store=secrets)
    assert store.read_snapshot(item_id, "latest")["ok"]
    assert len(replay.fetched) == 3, "the build's sample, the limited refresh, the refresh after the pause"


# ---- the key stays the user's ------------------------------------------------------------------------------------

def _quota_spent(key: str) -> dict:
    """Alpha Vantage's answer once a key's 25 requests for the day are spent, as publicly reported (users quoting it,
    found 2026-10-31): it names the key it was sent. NOT captured, so it is no fixture of the catalogue
    (``test_every_provider_fixture_is_captured``): capturing it spends a key's whole day. These tests rely only on
    the key being in it and on its window being a day; the rest is the captured burst answer's (status, content
    type, host)."""
    return {**fixture("alphavantage-burst-limited"), "captured": False,
            "body": {"Information": f"We have detected your API key as {key} and our standard API rate limit is 25 "
                                    "requests per day. Please subscribe to any of the premium plans at "
                                    "https://www.alphavantage.co/premium/ to instantly remove all daily rate limits."}}


def _serve_body(replay: _Replay, url: str, fx: dict) -> None:
    replay.serves[_base(url)] = [fx]
    replay._sent[_base(url)] = 0


class _Recording(_Model):
    """The scripted model, keeping every prompt it was sent."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def __call__(self, model: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return super().__call__(model, prompt)


def _kept(store: ni.NIStore, item_id: str) -> str:
    """Everything the card keeps that a person, a phone or a model can read: its record (notes, error, what it
    waited on), its History, what the card shows, its spec, its snapshots, its runs and its board row (the host's
    wait in its own words included)."""
    item = store.get_item(item_id) or {}
    runs = store.conn.execute("SELECT * FROM ni_runs WHERE item_id = ?;", [item_id]).fetchall()
    return json.dumps([ni_flow._flow_read(store, item_id), store.read_journal(item_id),
                       ni_flow.board_flow_field(store, item_id, read_only=True), item.get("spec"),
                       store.read_snapshot(item_id, "preview"), store.read_snapshot(item_id, "latest"),
                       [[str(v) for v in row] for row in runs],
                       ni_routes._board_row(store, item) if item else None], default=str)


# The sentence reader already hides a word shaped like a token (16 or more letters and digits, ``ni_message``); a key of
# another shape went through to every place a build keeps a source's words: all letters (one Alpha Vantage key in
# about 200 has no digit) or shorter than 16. The body is now scrubbed of the request's own key at the fetch.
LETTERS_KEY = "QWERTYUIOPASDFGH"


@pytest.mark.parametrize("key", [AV_KEY, LETTERS_KEY], ids=["token-shaped", "letters-only"])
def test_a_spent_quota_that_names_the_key_ends_the_build_with_its_window_and_keeps_no_key(monkeypatch, _t_install,
                                                                                        key) -> None:
    """The card every 30 minutes asks Alpha Vantage 48 times a day; its free key allows 25, then answers with a
    sentence that names the key. A build keeps a source's words (its error, History, what it waited on, what the
    politeness layer repeats to every request while the host's pause holds). The body is scrubbed at the fetch, so
    the key is nowhere the card keeps nor in any prompt, whatever its shape; the build ends with a retry and a
    line that says a retry waits for the limit to reset (not "Retry later"); the day's limit is never waited out."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": key})
    _serve_body(replay, AV_QUOTE, _quota_spent(key))
    model = _Recording()
    store, item_id, secrets = _card(monkeypatch, AV_ROWS, replay)
    ni_flow.run_flow(store, item_id, gateway_call=model)
    ni_flow.give_access(store, item_id, secrets, key=key, email=None)
    out = ni_flow.run_flow(store, item_id, gateway_call=model, source_url=AV_QUOTE)
    assert out["state"] == "failed" and ni_master.error_class_of(out["error"]) == "waited", out
    assert replay.fetched == [f"{AV_QUOTE}&apikey={key}"], "asked once: a day's limit is never waited out"
    assert _t_install.slept == [] and model.calls["mapping"] == 0
    assert f"asked SmartBrain to wait: We have detected your API key as {netguard.SCRUBBED} and" in out["error"]
    assert ni_master.reason_for("failed", out["error"]) == ni_master._WAITED_BY_WINDOW[ni_message.WAIT_LONG_S]
    assert "A retry before the limit resets ends the same way" in ni_master.reason_for("failed", out["error"])
    assert key not in _kept(store, item_id), "the key is nowhere the card keeps"
    assert netguard.SCRUBBED in ni_polite.SHARED._said("www.alphavantage.co")
    assert key not in ni_polite.SHARED._said("www.alphavantage.co"), "nor in what the pause repeats"
    assert model.prompts and not any(key in p for p in model.prompts), "nor in any prompt"


def test_a_refresh_that_meets_the_spent_quota_stays_due_for_its_window_and_keeps_no_key(monkeypatch,
                                                                                       _t_install) -> None:
    """The kept card's refresh meets the spent quota: the card stays due (a capacity status, no failure), the host
    is paused for the window the sentence names (the hour cap, not a second), nothing is sent while it holds, and
    the key is in neither the refusal, nor what the pause repeats to every request, nor anything the card keeps.
    The card says why it isn't updating and when it tries again (T, 2026-10-31: "when the limit is spent, with when
    it resumes"): the line about the free limit its cadence overruns, and the host's wait with its time."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": LETTERS_KEY})
    store, item_id, secrets = _card(monkeypatch, AV_ROWS, replay)
    ni_flow.run_flow(store, item_id, gateway_call=_Model())
    ni_flow.give_access(store, item_id, secrets, key=LETTERS_KEY, email=None)
    _held(store, item_id, ni_flow.run_flow(store, item_id, gateway_call=_Model(), source_url=AV_QUOTE), AV_QUOTE)
    monkeypatch.setattr(ni, "_clock", lambda: datetime.now().astimezone() + timedelta(minutes=31))
    _serve_body(replay, AV_QUOTE, _quota_spent(LETTERS_KEY))
    with pytest.raises(ni.NIError) as busy:
        ni.run_item(store, item_id, gateway_mod=gateway, secrets_store=secrets)
    assert busy.value.kind == "host_busy" and LETTERS_KEY not in str(busy.value)
    assert LETTERS_KEY not in ni_polite.SHARED._said("www.alphavantage.co"), "the pause repeats no key"
    before = datetime.now(UTC).replace(microsecond=0)
    row = _board(store, item_id)
    assert row["limit"] == AV_LINE and row["waiting"]["host"] == "www.alphavantage.co"
    assert row["waiting"]["said"].startswith(f"We have detected your API key as {netguard.SCRUBBED} and")
    resumes = datetime.fromisoformat(row["waiting"]["until"]) - before
    assert timedelta(seconds=ni_message.WAIT_LONG_S - 2) <= resumes <= timedelta(seconds=ni_message.WAIT_LONG_S + 2), \
        "the hour cap, the most one answer can hold a host off"
    _t_install.now += ni_message.WAIT_MINUTE_S
    with pytest.raises(ni.NIError) as still:
        ni.run_item(store, item_id, gateway_mod=gateway, secrets_store=secrets)
    assert still.value.kind == "host_busy" and len(replay.fetched) == 2, "nothing sent while the day's pause holds"
    assert store.get_item(item_id)["consecutive_failures"] == 0
    assert LETTERS_KEY not in _kept(store, item_id)


# The keyed builds the proof ask can make: Finnhub's (the source that covers every 30 minutes, ``token=``) and
# Alpha Vantage's once Finnhub's row is skipped (``apikey=``).
_KEYED_BUILDS = {
    "finnhub": (ROWS_NVDA["rows"], FINNHUB, "finnhub.io", FH_KEY, "token", "finnhub-quote"),
    "alphavantage": (AV_ROWS, AV_QUOTE, "www.alphavantage.co", AV_KEY, "apikey", "alphavantage-global-quote"),
}


@pytest.mark.parametrize("build", sorted(_KEYED_BUILDS))
def test_no_log_record_holds_the_key_over_the_keyed_build_and_refresh_on_the_real_http_client(monkeypatch, caplog,
                                                                                            _t_install,
                                                                                            build) -> None:
    """Postmortem M6, end to end. The keyed NVDA build and a refresh run over the real HTTP client: ``httpx`` with a
    stub transport in place of the network, so everything above it is production (netguard's pinned request, the
    politeness layer, the key store, the request builder). Every logger is at DEBUG, the HTTP client's own included,
    whose INFO request line names the URL and its key: each such line reaches the handler redacted, and no record
    holds the key (message, arguments or traceback)."""
    import logging

    import httpx

    from smartbrain_3000 import logredact

    rows, url, host, key, param, served = _KEYED_BUILDS[build]
    real_once = netguard._guarded_get_once
    replay = _Replay({url: served}, {host: key})
    store, item_id, secrets = _card(monkeypatch, rows, replay)
    monkeypatch.setattr(netguard, "_guarded_get_once", real_once)   # the real guarded request, not the replay
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        url = f"https://{request.headers['host']}{request.url.raw_path.decode('ascii')}"
        sent.append(url)
        fx = replay.serves[_base(url)][0]
        return httpx.Response(int(fx["status"]), headers={"content-type": fx["content_type"]},
                              content=json.dumps(fx["body"]).encode("utf-8"))

    class _Stubbed(httpx.Client):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(netguard.httpx, "Client", _Stubbed)
    monkeypatch.setattr(netguard, "_validated_ip", lambda host: "93.184.216.34")
    logredact.install()
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="httpx"), \
            caplog.at_level(logging.DEBUG, logger="httpcore"):
        out = ni_flow.run_flow(store, item_id, gateway_call=_Model())
        assert out["state"] == "awaiting_access" and out["_access"]["host"] == host, "the build's first pause"
        ni_flow.give_access(store, item_id, secrets, key=key, email=None)
        _held(store, item_id, ni_flow.run_flow(store, item_id, gateway_call=_Model(), source_url=url), url)
        monkeypatch.setattr(ni, "_clock", lambda: datetime.now().astimezone() + timedelta(minutes=31))
        ni.run_item(store, item_id, gateway_mod=gateway, secrets_store=secrets)
    assert sent == [f"{url}&{param}={key}"] * 2, "the build's sample and the refresh, over the client"
    assert store.read_snapshot(item_id, "latest")["ok"]
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert len(lines) == 2 and all("HTTP Request: GET" in m and f"{param}=[redacted]" in m for m in lines), lines
    assert key not in caplog.text
    assert not any(key in r.getMessage() or key in repr(r.args) or key in (r.exc_text or "")
                   for r in caplog.records)


# ---- the card's key is for one host ------------------------------------------------------------------------------

def test_the_cards_key_for_another_host_does_not_count_for_this_row(monkeypatch) -> None:
    """T's Mac: once the Alpha Vantage key sat on the card, the key check saw "a key exists" and skipped the pause
    for the Finnhub row; the host-bound loader then refused the key and the row dropped in 20 ms with no fetch. The
    check compares the stored key's host with the row's: the card asks for Finnhub's key, nothing is fetched, and
    the key the user then gives builds the card from Finnhub's flat body (``c``, the current price). (T's Mac
    tried Alpha Vantage first; since pack v1.6.0 that is the order for a card whose cadence its free key covers, as
    an hourly one.)"""
    replay = _Replay({AV_QUOTE: "alphavantage-demo-key-refused", FINNHUB: "finnhub-quote"},
                     {"www.alphavantage.co": AV_KEY, "finnhub.io": FH_KEY})
    model = _Model(cadence=60)
    store, item_id, secrets = _card(monkeypatch, ROWS_HOURLY["rows"], replay, ask=HOURLY)
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["_access"]["host"] == "www.alphavantage.co"
    ni_flow.give_access(store, item_id, secrets, key=AV_KEY, email=None)
    out = ni_flow.run_flow(store, item_id, gateway_call=model, source_url=AV_QUOTE)   # Alpha Vantage answers a sentence
    assert out["state"] == "awaiting_access" and out["source_url"] == FINNHUB and out["_access"]["host"] == "finnhub.io"
    assert replay.fetched == [f"{AV_QUOTE}&apikey={AV_KEY}"], "nothing fetched from Finnhub before its key"
    assert ni_flow.missing_access(store, item_id, secrets) == ["key"], "the Alpha Vantage key is not Finnhub's"
    assert ni_flow.board_flow_field(store, item_id, read_only=True)["access"]["key"]["reuse"] is False
    assert ni_flow.give_access(store, item_id, secrets, key=FH_KEY, email=None) == []
    assert json.loads(secrets.get(f"ni:{item_id}:api_key"))["host"] == "finnhub.io"
    out = ni_flow.run_flow(store, item_id, gateway_call=model, source_url=FINNHUB)
    item = _held(store, item_id, out, FINNHUB)
    assert replay.fetched[-1] == f"{FINNHUB}&token={FH_KEY}" and len(replay.fetched) == 2
    assert item["spec"]["pipeline"][0]["paths"]["price"] == "c"
    assert ni.run_pipeline(item["spec"]["pipeline"], fixture("finnhub-quote")["body"]) == \
        {"price": fixture("finnhub-quote")["body"]["c"]}
    assert str(fixture("finnhub-quote")["body"]["c"]) in json.dumps(store.read_snapshot(item_id, "preview"))
    assert FH_KEY not in json.dumps(item["spec"]) and AV_KEY not in json.dumps(item["spec"])
    assert item["spec"]["_built_from"]["limits"] == {"provider": "Finnhub", "keyed": True,
                                                     "windows": [{"requests": 60, "seconds": 60}]}
    assert "limit" not in _board(store, item_id), "Finnhub's free key covers every hour: nothing to say"


def test_missing_access_counts_only_a_key_bound_to_the_rows_host() -> None:
    store = _store()
    secrets = SecretStore(store.conn, gen_master_key())
    item_id = ni_flow.create_shell_item(store, ASK)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(ASK, "source"))
    ni_flow.seal_access(store, item_id, FINNHUB, ROWS_NVDA["rows"][1])
    ni.put_credential(secrets, item_id, ni_flow.KEY_PARAM, AV_KEY, "www.alphavantage.co")
    assert ni_flow.missing_access(store, item_id, secrets, copy=False) == ["key"]
    ni.put_credential(secrets, item_id, ni_flow.KEY_PARAM, FH_KEY, "Finnhub.IO")   # stored normalized
    assert ni_flow.missing_access(store, item_id, secrets, copy=False) == []
    assert ni_flow._card_key_host(secrets, item_id) == "finnhub.io" and ni_flow._card_key_host(secrets, "none") is None


def test_a_row_with_no_host_and_a_card_with_no_key_is_no_match() -> None:
    """Review of #569: the host check compared the card's key host with the row's, and with neither (no key stored,
    a sealed access with no host) None equalled None, so the card counted a key it does not have and skipped the
    pause. Nothing is copied or offered for a row with no host either."""
    store = _store()
    secrets = SecretStore(store.conn, gen_master_key())
    item_id = ni_flow.create_shell_item(store, ASK)
    record = ni_flow._make_record(ASK, "source")
    for access in ({"key": {"in": "query", "name": "apikey"}, "host": ""},
                   {"key": {"in": "query", "name": "apikey"}}):
        ni_flow._flow_write(store, item_id, {**record, "_access": access})
        assert ni_flow._card_key_host(secrets, item_id) is None
        assert ni_flow.missing_access(store, item_id, secrets) == ["key"], access
        assert ni_flow.missing_access(store, item_id, secrets, copy=False) == ["key"], access
        assert secrets.get(f"ni:{item_id}:{ni_flow.KEY_PARAM}") is None, "nothing copied"
        assert not (ni_flow._flow_read(store, item_id) or {})["_access"].get("reuse"), "nothing offered"


# ---- a series filed under dates ----------------------------------------------------------------------------------

def test_the_daily_series_is_refused_for_its_dated_paths_and_research_then_runs_under_the_consent(monkeypatch) -> None:
    """``["Time Series (Daily)"]["2026-10-30"]["4. close"]`` is today's close today and the same day's close for
    ever: a card bound to it goes stale silently. With only such paths (and the series' meta names beside them)
    the row moves on with that reason before any model call. With no row left, research runs AT ONCE on the route
    the card's consent names (no Keep looking tap: on T's install that tap was the gate and research never ran),
    and the ask ends as research's own decline, lookable, listing the row it tried and why."""
    rows = [ROWS_NVDA["rows"][2]]
    replay = _Replay({AV_DAILY: "alphavantage-time-series-daily"}, {"www.alphavantage.co": AV_KEY})
    model, research = _Model(), _researcher()
    store, item_id, secrets = _card(monkeypatch, rows, replay)
    ni_flow.run_flow(store, item_id, gateway_call=model)
    ni_flow.give_access(store, item_id, secrets, key=AV_KEY, email=None)
    out = ni_flow.run_flow(store, item_id, gateway_call=model, source_url=AV_DAILY)
    assert replay.fetched == [f"{AV_DAILY}&apikey={AV_KEY}"] and model.calls["mapping"] == 0
    assert research.seen == ["claudecode/opus"] * len(research.seen) and research.seen, "research ran, no tap"
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "research_gave_up", out
    assert out["_decline_card"]["look"] is True
    assert _tried(store, item_id) == [("www.alphavantage.co", ni_flow._DATED_WHY)], "the card says what it tried"
    field = ni_flow.board_flow_field(store, item_id, read_only=True)
    assert field["tried"][0]["provider"] == rows[0]["provider"] and "keep_looking" in field["actions"]
    cands = ni_flow.derive_paths(fixture("alphavantage-time-series-daily")["body"])
    assert any(ni_flow._dated_path(c["path"]) for c in cands), "the walker offers the dated paths; the flow refuses them"


# ---- no key: skip ---------------------------------------------------------------------------------------------------

def test_skipping_each_keyed_host_goes_on_to_the_next_and_then_to_research(monkeypatch) -> None:
    """The user has no key for a source the build paused on: Skip drops that row with that reason, and with it the
    host's other rows that need the same key (review of #570: the daily row asked for Alpha Vantage's key again), and
    the build goes on from the rows it had left (never a second ranking, which would offer the rows again). Finnhub
    pauses first (its free key covers every 30 minutes); skipped, Alpha Vantage's quote row pauses; skipped, its
    daily row goes with it, nothing is left and research runs at once under the card's consent. Every pause and the
    decline list what was tried. Nothing is ever fetched and no mapping call is made."""
    replay, model, research = _Replay({}, {}), _Model(), _researcher()
    store, item_id, _secrets = _card(monkeypatch, ROWS_NVDA["rows"], replay)
    started: list[dict] = []
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda store_, item_id_, **kw: started.append(kw) or True)
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["_access"]["host"] == "finnhub.io"
    field = ni_flow.board_flow_field(store, item_id, read_only=True)
    assert "skip_source" in field["actions"] and "give_access" in field["actions"] and "tried" not in field
    assert ni_flow.skip_source(store, item_id) is True and started == [{}], "the skip resumes the build itself"
    left = [r["url"] for r in ni_flow._flow_read(store, item_id).get("_ranked_library") or []]
    assert FINNHUB not in left and AV_QUOTE in left and AV_DAILY in left, left
    out = ni_flow.run_flow(store, item_id, gateway_call=model)   # the worker the skip started
    assert out["state"] == "awaiting_access" and out["_access"]["host"] == "www.alphavantage.co"
    assert out["source_url"] == AV_QUOTE
    assert _tried(store, item_id) == [("finnhub.io", ni_flow._SKIP_WHY)]
    assert ni_flow.skip_source(store, item_id) is True
    left = [r["url"] for r in ni_flow._flow_read(store, item_id).get("_ranked_library") or []]
    assert AV_DAILY not in left and AV_QUOTE not in left, "the host's keyed rows go with the skip"
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert research.seen, "nothing left: research ran at once under the consent"
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "research_gave_up", out
    assert [h for h, _w in _tried(store, item_id)] == ["finnhub.io", "www.alphavantage.co"]
    field = ni_flow.board_flow_field(store, item_id, read_only=True)
    assert "skip_source" not in field["actions"] and "keep_looking" in field["actions"]
    assert replay.fetched == [] and model.calls["mapping"] == 0 and model.calls["intent"] == 1
    with pytest.raises(ValueError, match="skip"):
        ni_flow.skip_source(store, item_id)   # a declined card has nothing to skip


def _tool_reply(name: str, args: dict, prompt: int) -> dict:
    """A research model reply on the Claude Code route: one tool call, and the usage a new CLI process reports for a
    prompt it was sent whole (no cache share)."""
    call = {"id": "c0", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": 200,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


def test_research_after_both_skips_offers_neither_site_again_and_the_card_says_what_it_searched_and_read(
        monkeypatch, caplog) -> None:
    """Run B on T's Mac (2026-10-31, #570 at f80b265): no keys, both keyed sites skipped, research at once on
    claudecode/opus; it declined after 11 calls, and nothing said what it had searched or read, its line said "ran
    out of steps" for the token cap, and its three links were the two sites just skipped. Here, on the real gateway
    path of T's route: research is one CLI session, closed at the end; the Library offers research neither skipped
    site's keyed rows, so neither is read, offered as Use my key or linked; the card lists the skips, then the run's
    searches and reads in its own words; the line names the model budget; the app log has one line for the run with
    its cache share and no address."""
    replay, model = _Replay({}, {}), _Model()
    store, item_id, _secrets = _card(monkeypatch, ROWS_NVDA["rows"], replay)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    monkeypatch.setattr(ni_research_flow, "_CHAT", None)   # the gateway path T's install runs, not a seam
    opened: list = []

    class _Session:
        closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(ni_research_flow.claudecli, "open_turn_session", lambda: opened.append(_Session()) or opened[-1])
    page = "https://quotes.example.com/nvda"
    searched: list[str] = []
    monkeypatch.setattr(ni_research_flow, "_SEARCH", lambda query, limit: searched.append(query) or {
        "results": [{"title": "NVDA quote", "url": page, "snippet": "NVIDIA stock price today"}]})
    read: list[str] = []
    monkeypatch.setattr(ni_research_flow.tools, "_fetch_and_digest", lambda ctx, url, fmt, robots=True: read.append(url)
                        or {"ok": False, "error": "not_data", "detail": "a web page with no data table (page text is not read)"})
    # uncached prompts, as a new CLI process per call reports them: the fourth call takes the run past its token cap
    # (60,000), so its own tool call is refused and the run stops there
    script = [_tool_reply("library_search", {"query": "NVDA stock price"}, 20_000),
              _tool_reply("web_search", {"query": "NVDA stock price api"}, 20_000),
              _tool_reply("fetch_sample", {"url": page}, 19_000),
              _tool_reply("library_search", {"query": "NVIDIA quote"}, 1_000)]
    calls: list[dict] = []

    def chat(conn, purpose, messages, model_, tools_, **kw):
        calls.append({"model": model_, "session": kw.get("session"), "messages": copy.deepcopy(messages),
                      "progress": ni_flow.build_progress(ni_flow._flow_read(store, item_id) or {})})
        return script.pop(0)

    monkeypatch.setattr(gateway, "chat_with_tools_recorded", chat)
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["_access"]["host"] == "finnhub.io"
    assert ni_flow.skip_source(store, item_id) is True
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["_access"]["host"] == "www.alphavantage.co"
    assert ni_flow.skip_source(store, item_id) is True
    with caplog.at_level("INFO", logger="smartbrain_3000.ni_research_flow"):
        out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "declined", out
    assert len(calls) == 4 and {c["model"] for c in calls} == {"claudecode/opus"}
    assert len(opened) == 1 and all(c["session"] is opened[0] for c in calls) and opened[0].closed, \
        "one CLI session for the run, on every call, wiped when it ended"
    assert {json.dumps(c["progress"]) for c in calls} == {json.dumps({"step": 2, "of": 4, "label": "Finding a source"})}, \
        "while research runs the card names no Library row (it named finnhub.io, just skipped, on T's Mac)"
    shown = json.dumps([m for c in calls for m in c["messages"] if m.get("role") == "tool"])
    assert "alphavantage" not in shown and "finnhub" not in shown, "research was offered neither skipped site"
    assert searched == ["NVDA stock price api"] and read == [page] and replay.fetched == []
    card = out["_decline_card"]
    assert card["reason"] == "research_gave_up" and card["line"] == ni_master.GAVE_UP_LINES["tokens"], card
    assert [link["url"] for link in card["links"]] == [page] and card.get("key_wall") is None
    assert _tried(store, item_id) == [
        ("finnhub.io", ni_flow._SKIP_WHY), ("www.alphavantage.co", ni_flow._SKIP_WHY),
        ("web search", 'searched for "NVDA stock price api"'),
        ("research", "read quotes.example.com (no data file or table)")]
    view = ni_master.terminal_surface("declined", ni_flow._flow_read(store, item_id), shell=True)
    assert view["reason"] == ni_master.GAVE_UP_LINES["tokens"] and "use_key" not in view["reopen"]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ni_research: cloud run ")]
    assert len(lines) == 1 and "ended declined (tokens)" in lines[0] and "cache read 0, write 0" in lines[0], lines
    assert "reads: quotes.example.com no data file or table" in lines[0]
    assert "; 3 tool calls (library_search 1, web_search 1, fetch_sample 1), 1 searches, 1 fetches" in lines[0], \
        "every tool call is named; the one its budget refused is not one"
    assert "https://" not in lines[0] and "NVDA stock price api" not in lines[0], "no address and no search words"


def _skip_both(monkeypatch) -> tuple[ni.NIStore, str, SecretStore, _Model, _Research, _Replay]:
    """The NVDA card with no keys: both keyed sites skipped, research declined, the card lookable."""
    replay, model, research = _Replay({}, {}), _Model(), _researcher()
    store, item_id, secrets = _card(monkeypatch, ROWS_NVDA["rows"], replay)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    for host in ("finnhub.io", "www.alphavantage.co"):   # the order of fit: Finnhub covers every 30 minutes
        out = ni_flow.run_flow(store, item_id, gateway_call=model)
        assert out["state"] == "awaiting_access" and out["_access"]["host"] == host, out
        assert ni_flow.skip_source(store, item_id) is True
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "declined" and out["_decline_card"]["look"] is True, out
    return store, item_id, secrets, model, research, replay


def test_keep_looking_after_both_skips_looks_again_without_asking_for_either_key(monkeypatch) -> None:
    """Keep looking on the card research declined after both keyed sites were skipped. Each look runs the ask again,
    and before, it paused for the first site's key again: the order stopped there, asking for a key the user had said
    they don't have. The skips ride the order: a look drops both sites' keyed rows with the skip's reason and goes
    on to research, fetching nothing. A key for a skipped site given since on another card is offered on the card,
    as any same-host key is (the user's tap, nothing copied)."""
    store, item_id, secrets, model, research, replay = _skip_both(monkeypatch)
    runs = len(research.seen)
    assert ni_standing.seal(store, item_id, ni_flow._flow_read(store, item_id)) is not None   # the Keep looking tap
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "declined" and isinstance(out.get("_looking"), dict), out
    assert len(research.seen) > runs, "the look went on to research"
    assert _tried(store, item_id) == [("finnhub.io", ni_flow._SKIP_WHY), ("www.alphavantage.co", ni_flow._SKIP_WHY)]
    assert ni_flow.skipped_sites(ni_flow._flow_read(store, item_id)) == {"www.alphavantage.co", "finnhub.io"}
    assert replay.fetched == [] and model.calls["mapping"] == 0
    # a key for Alpha Vantage given on another card since: the next look (a free re-match) offers it on this card
    ni.put_credential(secrets, "another-card", ni_flow.KEY_PARAM, AV_KEY, "www.alphavantage.co")
    assert ni_standing.rematch(store) == 1 and ni_standing.run_due(store) == item_id
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["_access"]["host"] == "www.alphavantage.co", out
    assert out["_access"].get("reuse") is True, "offered for one tap"
    assert secrets.get(f"ni:{item_id}:{ni_flow.KEY_PARAM}") is None and replay.fetched == [], "nothing copied or sent"


def test_a_retry_after_a_skip_asks_for_the_key_again(monkeypatch) -> None:
    """A Retry starts a fresh record without the skips (the user may have a key by then; it is the one way to bring
    one later), so the build pauses for the first site's key again: Finnhub's, the source that covers the cadence."""
    store, item_id, _secrets, model, _research, replay = _skip_both(monkeypatch)
    assert ni_flow.reseed_for_retry(store, item_id, ni_flow._flow_read(store, item_id), ASK) is True
    assert ni_flow.skipped_sites(ni_flow._flow_read(store, item_id)) == frozenset()
    out = ni_flow.run_flow(store, item_id, gateway_call=model)
    assert out["state"] == "awaiting_access" and out["_access"]["host"] == "finnhub.io", out
    assert replay.fetched == []


def test_a_skip_keeps_the_hosts_rows_that_need_no_key() -> None:
    """A key is the host's, so a skip takes the host's keyed rows; a row of the same host that needs no key is still
    worth trying (a multi-tenant host can serve both), and another host's rows are untouched."""
    keyless = {**ROWS_NVDA["rows"][2], "source_id": "av-keyless", "url": AV_DAILY + "&datatype=json",
               "needs_key": None}
    host = "www.alphavantage.co"
    assert ni_flow._goes_with(ROWS_NVDA["rows"][2], host, host_wide=False), "the same host's keyed row goes"
    assert not ni_flow._goes_with(keyless, host, host_wide=False), "its keyless row stays"
    assert not ni_flow._goes_with(ROWS_NVDA["rows"][1], host, host_wide=False), "another host's row stays"
    assert ni_flow._goes_with(keyless, host, host_wide=True), "a host-wide refusal takes every row of the host"


def test_a_cloud_build_a_phone_started_draws_the_allowance_before_its_first_model_call(monkeypatch) -> None:
    """A Retry, Fix, Refine or Skip from a paired phone runs a cloud build: it draws one unit of the daily allowance
    before its first model call, once a run. With today's spent, the card fails with the allowance's own line
    (review of #570: it read "No model is routed") and nothing reaches the model or a source. The Desktop's own run
    is never counted."""
    monkeypatch.setenv(ni_refresh.ENV_DAILY, "1")
    replay, model = _Replay({}, {}), _Model()
    store, first, _secrets = _card(monkeypatch, ROWS_NVDA["rows"], replay)

    def card(ask: str) -> str:   # another card for the same ask (a card's name is unique)
        item_id = ni_flow.create_shell_item(store, ask)
        ni_flow._flow_write(store, item_id, {**ni_flow._make_record(ask, "intent"),
                                             "_model_consent": "claudecode/opus", "_model_consent_web": True})
        return item_id

    with auth.serving_via(auth.PHONE_VIA), ni_refresh.paid_scope(False):
        out = ni_flow.run_flow(store, first, gateway_call=model)
    assert out["state"] == "awaiting_access" and model.calls["intent"] == 1, "the phone's run built on its unit"
    with auth.serving_via(auth.PHONE_VIA), ni_refresh.paid_scope(False):
        refused = ni_flow.run_flow(store, card(ASK + ", from the phone again"), gateway_call=model)
    assert refused["state"] == "failed" and refused["error"] == f"phone_allowance: {ni_refresh.PHONE_SPEND_LINE}"
    assert model.calls["intent"] == 1 and replay.fetched == [], "nothing reached the model or a source"
    line = ni_master.terminal_surface("failed", refused, shell=True)["reason"]
    assert line.startswith("Today's allowance for cloud model use started from a phone is spent"), line
    assert "No model is routed" not in line
    out = ni_flow.run_flow(store, card(ASK + ", at the desk"), gateway_call=model)   # the Desktop's own run
    assert out["state"] == "awaiting_access" and model.calls["intent"] == 2, "the owner at the desk is not counted"


def test_an_address_the_user_named_cannot_be_skipped(monkeypatch) -> None:
    """Skip is for a row the build picked; the one address the user pasted is theirs, so the card offers no skip
    and the route's helper refuses."""
    replay, model = _Replay({}, {}), _Model()
    store, item_id, _secrets = _card(monkeypatch, ROWS_NVDA["rows"], replay)
    ni_flow.run_flow(store, item_id, gateway_call=model)
    ni_flow.ask_for_key(store, item_id, FINNHUB)   # the user's own address asks for a key (P3)
    field = ni_flow.board_flow_field(store, item_id, read_only=True)
    assert field["state"] == "awaiting_access" and "skip_source" not in field["actions"]
    with pytest.raises(ValueError, match="skip"):
        ni_flow.skip_source(store, item_id)
    assert replay.fetched == []


# ---- the budget --------------------------------------------------------------------------------------------------

def test_a_build_stops_honestly_when_its_model_budget_is_spent(monkeypatch) -> None:
    """A model that never copies an offered path (the failure class the mapping retries on) would otherwise spend
    its calls on every row of the pick. The row that hits the cap moves on naming the budget, the build then fails
    with its own class and line, and a retry runs with a fresh budget."""
    monkeypatch.setattr(ni_flow, "_BUILD_MODEL_CALLS", 3)
    replay = _Replay({FINNHUB: "finnhub-quote"}, {"finnhub.io": FH_KEY})
    model = _Model(picks={"price": "not.an.offered.path"})
    store, item_id, secrets = _card(monkeypatch, ROWS_NVDA["rows"], replay)
    ni_flow.run_flow(store, item_id, gateway_call=model)
    ni_flow.give_access(store, item_id, secrets, key=FH_KEY, email=None)
    out = ni_flow.run_flow(store, item_id, gateway_call=model, source_url=FINNHUB)
    assert out["state"] == "failed" and ni_master.error_class_of(out["error"]) == "model_budget", out["error"]
    assert ni_master.reason_for("failed", out["error"]).startswith("The build spent its model-call budget")
    assert model.calls["mapping"] == 3, "exactly the budget: the fourth call was never made"
    assert ni_flow._BUDGET_WHY in out["error"] and "3 calls" in out["error"]
    assert len(replay.fetched) == 1, "one row's sample; no second row is fetched once the budget is gone"


# ---- a keyless declared row: Open-Meteo's real body through the declared-answers path ------------------------------

def _weather_model(model: str, prompt: str) -> str:
    assert model == "claudecode/opus"
    if "Classify a dashboard-card request" in prompt:
        return json.dumps({"kind": "external_data", "subject": "NYC temperature", "cadence_minutes": 30,
                           "wants": ["temperature"], "threshold": None, "display_hint": "value"})
    return json.dumps({"serves": True, "gaps": [], "wrong": []})


def test_open_meteo_builds_from_its_captured_body_through_the_declared_answers_path(lib, monkeypatch) -> None:  # noqa: F811
    """The Global Library's weather source (keyless, answers declared): the captured forecast body, re-dated to
    today only in its daily ``time`` axis (the Library's window checks read it; nothing else is touched), through
    the same production fetch path. The card is built from the declared answers with no mapping call."""
    body = fixture("open-meteo-forecast")["body"]
    today = datetime.now().date()
    body["daily"]["time"] = [(today + timedelta(days=i)).isoformat() for i in range(len(body["daily"]["time"]))]
    body["current"]["time"] = datetime.now().strftime("%Y-%m-%dT%H:00")

    class _Wrap:
        def __init__(self, rows):
            self.rows = rows

        def candidates(self, request, limit=3, hint=None):
            return copy.deepcopy(self.rows[:limit]), []

        def answers(self, source_id):
            return lib.answers(source_id)

        def get(self, source_id):
            return lib.get(source_id)

    class _Weather(_Replay):
        def __init__(self):
            super().__init__({}, {})
            self.serves = {_base(WEATHER_URL): [{**fixture("open-meteo-forecast"), "body": body}]}

    replay = _Weather()
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Wrap([_row("open-meteo-forecast", WEATHER_URL)]))
    monkeypatch.setattr(netguard, "_guarded_get_once", replay)
    store = _store()
    ask = "NYC temperature every 30 minutes"
    item_id = ni_flow.create_shell_item(store, ask)
    ni_flow._flow_write(store, item_id, {**ni_flow._make_record(ask, "intent"), "_model_consent": "claudecode/opus",
                                         "_model_consent_web": True})
    out = ni_flow.run_flow(store, item_id, gateway_call=_weather_model)
    _held(store, item_id, out, WEATHER_URL, path="auto")
    assert replay.fetched == [WEATHER_URL]
    preview = json.dumps(store.read_snapshot(item_id, "preview")["payload"])
    assert str(body["current"]["temperature_2m"]) in preview, preview
    assert "declared answers" in _notes(store, item_id)
