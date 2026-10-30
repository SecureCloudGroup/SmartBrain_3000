"""The route × source-kind matrix (2026-10-29/30, the NVDA card): T's real configuration, and every other,
driven end to end.

No test drove an install whose ni / chat / agent routes are all Claude Code with no local model (#561, the NVDA
card): research silently had no model there, the card declined with "no source answers this", and the Settings
page said "no model is routed" for every step, build included. And no test drove the source kinds that install
meets: every stock-quote row in the Library pack is keyed and declares no answers, which the 2026-10-05 rule made a
link. These tests run the same ask through the real flow for each route configuration and each source kind and
check: which model each step really runs on (and that the Settings page says the same); that a cloud model is
asked for on the card before anything leaves this computer; that an ask that cannot be researched says WHY; and
that a data row the Library has not described builds (held for Keep), a keyed one asks for the key first and then
builds, a key the user gave another card for the same host is offered for one tap, and a web page stays a link.

The model seams are at the gateway (``gateway_call`` for the build, ``gateway.chat_with_tools_recorded`` /
``chat_recorded`` for the research), never the researcher's installed seam, so model resolution runs for real.
"""
from __future__ import annotations

import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_ni_answers import OPEN_METEO, WEATHER_URL, lib  # noqa: F401  (lib is a fixture)
from test_ni_auto_pick import _row, _Wrap
from test_ni_research_loop import reply, text_reply

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, ni, ni_flow, ni_master, ni_models, ni_research_flow
from smartbrain_3000.secrets import SecretStore, gen_master_key

ASK = "NVDA stock price every 30 minutes"
FINNHUB = "https://finnhub.io/api/v1/quote?symbol=NVDA"
ALPHA = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=NVDA"
STOOQ = "https://stooq.example/q/l/?s=nvda.us&f=sd2t2ohlcv&e=json"
FINNHUB_PAGE = "https://finnhub.io/docs/api"
KEY_FINNHUB = {"in": "query", "name": "token", "prefix": "", "docs_url": FINNHUB_PAGE}
KEY_ALPHA = {"in": "query", "name": "apikey", "prefix": "", "docs_url": "https://www.alphavantage.co/support/"}
QUOTE = {"c": 181.25, "d": 2.1, "dp": 1.17, "h": 183.0, "l": 179.4, "o": 180.0, "pc": 179.15, "t": 1761782400}
T_ROUTES = {"ni": "claudecode/opus", "chat": "claudecode/sonnet", "agent": "claudecode/sonnet"}
ANTHROPIC = {"chat": "anthropic/claude-sonnet-5-5"}
LOCAL_ONLY = {"chat": "mlx/qwen-local"}
OTHER_CLOUD = {"ni": "openai/gpt-4o"}   # a provider neither side of a step takes: the older route order only
LOOKING = {"since": "2026-10-29T00:00:00+00:00", "runs": 1, "next_at": None}   # the user tapped Keep looking


class _StockLibrary:
    """A Library for the ask. ``keyed``: the quote rows pack v1.5.0 really holds (``finnhub-quote`` and
    ``alphavantage-quote``: keyed JSON, no declared answers). ``pages``: the same providers as web pages only
    (nothing buildable, so the ask goes to research). ``keyless``: one keyless undeclared data row."""

    def __init__(self, kind: str = "keyed") -> None:
        assert kind in ("keyed", "pages", "keyless"), kind
        self.rows = {
            "keyed": [_row("finnhub-quote", FINNHUB, needs_key=KEY_FINNHUB, provider="Finnhub", format="json"),
                      _row("alphavantage-quote", ALPHA, needs_key=KEY_ALPHA, provider="Alpha Vantage", format="json")],
            "pages": [_row("finnhub-docs", FINNHUB_PAGE, provider="Finnhub", format="html")],
            "keyless": [_row("stooq-quote", STOOQ, provider="Stooq", format="json")],
        }[kind]

    def candidates(self, request: str, limit: int = 3, hint: dict | None = None):
        return [dict(r) for r in self.rows[:limit]], []

    def answers(self, source_id: str) -> list[dict]:
        return []

    def get(self, source_id: str) -> dict | None:
        host = {"finnhub-quote": "https://finnhub.io", "alphavantage-quote": "https://www.alphavantage.co",
                "stooq-quote": "https://stooq.example", "finnhub-docs": "https://finnhub.io"}[source_id]
        return {"access": {"docs_url": FINNHUB_PAGE if "finnhub" in source_id else host + "/support/"},
                "provider": {"url": host}}


def _store() -> ni.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return ni.NIStore(conn, gen_master_key())


def _model(model: str, prompt: str) -> str:
    """The build's model, scripted by stage: the intent, the mapping (the quote's current price), the judge."""
    if "Classify a dashboard-card request" in prompt:
        return json.dumps({"kind": "external_data", "subject": "NVDA", "cadence_minutes": 30, "wants": ["price"],
                           "threshold": None, "display_hint": "value"})
    if "Choose the best candidate path" in prompt:
        return json.dumps({"price": "c"})
    return json.dumps({"serves": True, "gaps": [], "wrong": []})


class _Research:
    """The gateway's two research seams, scripted: records the model each call ran on."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies, self.models = list(replies), []

    def tools(self, conn, purpose, messages, model, tools_, **kw):
        assert purpose == "ni.research" and tools_, "the research purpose, with tools"
        self.models.append(model)
        return self.replies.pop(0)

    def plain(self, conn, purpose, messages, model, **kw):
        assert purpose == "ni.research", "the research purpose"
        self.models.append(model)
        return self.replies.pop(0)


def _stalls() -> list[dict]:
    return [reply(("library_search", {"query": "NVDA quote"})), text_reply("still looking"), text_reply("still looking"),
            text_reply("still looking")]


def _no_fetch(url: str) -> object:
    raise AssertionError(f"nothing is fetched here: {url}")


@pytest.fixture(autouse=True)
def _wired(monkeypatch) -> Iterator[None]:
    for name in ("SMARTBRAIN_NI_RESEARCH", "SMARTBRAIN_NI_RESEARCH_MODEL", "SMARTBRAIN_NI_RESEARCH_WEB"):
        monkeypatch.delenv(name, raising=False)
    ni_research_flow.set_research_chat(None)   # the gateway path: model resolution runs for real
    library = _StockLibrary("pages")
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: library)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    yield
    ni_flow.set_secrets_provider(None)


def _drive(monkeypatch, routes: dict, research: _Research, *, allow: bool = True, fetch=_no_fetch,
           call=_model) -> tuple[ni.NIStore, str, dict, list]:
    """Intake the ask as a standing order (so research runs at once), answer the consent question when one is
    asked, and return the end record plus the models the build called."""
    monkeypatch.setattr(gateway, "load_routes", lambda conn: dict(routes))
    monkeypatch.setattr(gateway, "chat_with_tools_recorded", research.tools)
    monkeypatch.setattr(gateway, "chat_recorded", research.plain)
    store = _store()
    item_id = ni_flow.create_shell_item(store, ASK)
    ni_flow._flow_write(store, item_id, {**ni_flow._make_record(ASK, "intent"), "_looking": LOOKING})
    built: list[str] = []
    seen = lambda m, p: built.append(m) or call(m, p)
    out = ni_flow.run_flow(store, item_id, gateway_call=seen, fetcher=fetch)
    if out["state"] == "unsupported" and allow:
        question = ni_master.question_for("unsupported", out)
        assert question["kind"] == "model_consent" and built == [], "asked before any model call"
        assert out["_question"]["web"] is True, "the question shows the research and web wording the consent seals"
        ni_flow._transition(store, item_id, "intent", _question=None, error=None,
                            _model_consent=question["model"], _model_consent_web=True)
        out = ni_flow.run_flow(store, item_id, gateway_call=seen, fetcher=fetch)
    return store, item_id, out, built


# ---- routes: which model, with whose consent ---------------------------------------------------------------

@pytest.mark.parametrize(("routes", "cloud"), [(T_ROUTES, "claudecode/opus"), (ANTHROPIC, "anthropic/claude-sonnet-5-5")])
def test_a_cloud_route_builds_and_researches_on_the_consented_model(monkeypatch, routes, cloud) -> None:
    """T's install (Claude Code routes, no local model) and an Anthropic install: one consent question names the
    model; the build and the research both run on it; nothing is fetched (the Library holds only pages here)."""
    research = _Research(_stalls())
    store, item_id, out, built = _drive(monkeypatch, routes, research)
    assert set(built) == {cloud}, "the build ran on the consented model"
    assert research.models == [cloud] * 4, "the research ran on the same model, under the same consent"
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "research_gave_up"
    assert out["_decline_card"]["look"] is True, "a run that could research again is lookable"
    assert ni_flow._model_consent_of(store, item_id, out) == cloud


def test_the_consent_question_names_the_claude_code_model_and_nothing_runs_without_it(monkeypatch) -> None:
    research = _Research(_stalls())
    _store_, _id, out, built = _drive(monkeypatch, T_ROUTES, research, allow=False)
    assert out["state"] == "unsupported" and built == [] and research.models == []
    question = ni_master.question_for("unsupported", out)
    assert question["model"] == "claudecode/opus" and question["local"] == ""
    assert "research" in question["prompt"] and "outside this computer" in question["prompt"]


def test_a_local_only_route_builds_and_researches_locally_with_no_consent(monkeypatch) -> None:
    """The keyless data row is tried first (its sample fetch fails here), then the closed-world local tier."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _StockLibrary("keyless"))
    research = _Research([text_reply('{"pick": null}')])
    failing = lambda url: (_ for _ in ()).throw(RuntimeError("no network for the sample"))
    _store_, _id, out, built = _drive(monkeypatch, LOCAL_ONLY, research, fetch=failing)
    assert set(built) == {"mlx/qwen-local"} and research.models == ["mlx/qwen-local"]
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "no_answering_source"
    assert "model" not in out["_decline_card"]["line"], "research ran: the line is the run's own verdict"


def test_no_route_at_all_fails_honestly_before_any_call(monkeypatch) -> None:
    research = _Research([])
    _store_, _id, out, built = _drive(monkeypatch, {}, research)
    assert out["state"] == "failed" and built == [] and research.models == []
    assert ni_master.error_class_of(out["error"]) == "model"
    assert ni_master.reason_for("failed", out["error"]).startswith("No model is routed for building cards")


def test_a_provider_no_side_takes_builds_and_researches_through_the_older_order(monkeypatch) -> None:
    """Automatic with neither side routed: the older order's model (an explicit ni route of any provider) runs the
    build AND the research, under the one consent, exactly as the Settings page reports (#565 review)."""
    research = _Research(_stalls())
    store, item_id, out, built = _drive(monkeypatch, OTHER_CLOUD, research)
    assert set(built) == {"openai/gpt-4o"} and research.models == ["openai/gpt-4o"] * 4
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "research_gave_up"
    assert ni_flow._model_consent_of(store, item_id, out) == "openai/gpt-4o"


def test_research_with_no_model_on_its_side_declines_naming_the_missing_model(monkeypatch) -> None:
    """The build runs, research cannot (its step was saved as Local while a local model was routed, since
    removed): the ask ends declined and its line says so, never "no source answers this"; the paste box stays
    one tap away."""
    research = _Research([])
    monkeypatch.setattr(gateway, "load_routes", lambda conn: dict(OTHER_CLOUD))
    monkeypatch.setattr(gateway, "chat_with_tools_recorded", research.tools)
    store = _store()
    dbmod.meta_set(store.conn, "ni_step_models", '{"research": "local"}')
    item_id = ni_flow.create_shell_item(store, ASK)
    ni_flow._flow_write(store, item_id, {**ni_flow._make_record(ASK, "intent"), "_looking": LOOKING,
                                         "_model_consent": "openai/gpt-4o", "_model_consent_web": True})
    out = ni_flow.run_flow(store, item_id, gateway_call=_model, fetcher=_no_fetch)
    assert out["state"] == "declined" and research.models == []
    assert out["_decline_card"]["reason"] == "no_answering_source"
    line = out["_decline_card"]["line"]
    assert line.startswith("I can't look for a source: no local model is routed for research")
    assert "paste a link" in line and out["_decline_card"]["look"] is False
    assert "pick_source" in ni_flow.board_flow_field(store, item_id, read_only=True)["reopen"]


def test_research_turned_off_is_named_as_the_reason(monkeypatch) -> None:
    research = _Research([])
    monkeypatch.setattr(gateway, "load_routes", lambda conn: dict(T_ROUTES))
    store = _store()
    dbmod.meta_set(store.conn, "ni_research", "off")
    monkeypatch.setattr(gateway, "chat_with_tools_recorded", research.tools)
    item_id = ni_flow.create_shell_item(store, ASK)
    ni_flow._flow_write(store, item_id, {**ni_flow._make_record(ASK, "intent"), "_looking": LOOKING,
                                         "_model_consent": "claudecode/opus", "_model_consent_web": True})
    out = ni_flow.run_flow(store, item_id, gateway_call=_model, fetcher=_no_fetch)
    assert out["state"] == "declined" and research.models == []
    assert out["_decline_card"]["line"].startswith("I can't look for a source: research is turned off")


# ---- source kinds on T's routes: what builds, what asks, what stays a link ---------------------------------

def _consented(monkeypatch, library, secrets: SecretStore | None = None, ask: str = ASK) -> tuple[ni.NIStore, str, list]:
    """T's routes, the card's consent for claudecode/opus already sealed (the question is tested above)."""
    monkeypatch.setattr(gateway, "load_routes", lambda conn: dict(T_ROUTES))
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: library)
    store = _store()
    if secrets is None:
        secrets = SecretStore(store.conn, gen_master_key())
    ni_flow.set_secrets_provider(lambda: secrets)
    item_id = ni_flow.create_shell_item(store, ask)
    ni_flow._flow_write(store, item_id, {**ni_flow._make_record(ASK, "intent"), "_model_consent": "claudecode/opus",
                                         "_model_consent_web": True})
    return store, item_id, []


def _fetcher(fetched: list, by_url: dict):
    def fetch(url: str) -> object:
        fetched.append(url)
        assert url in by_url, f"fetched an address that is no Library row: {url}"
        return dict(by_url[url])
    return fetch


def _held_auto_card(store: ni.NIStore, item_id: str, out: dict, url: str, path: str = "mapping") -> dict:
    """A card from SmartBrain's own pick: ``auto`` (declared answers) or ``mapping`` (the model mapped the sample);
    both wait for the user's Keep before anything recurs."""
    assert out["state"] == "ready", out
    item = store.get_item(item_id)
    assert item["spec"]["_built_from"]["path"] == path and ni.awaits_yes(item), "SmartBrain's own pick: held for Keep"
    assert item["spec"]["source"]["url"] == url and item["state"] == "commissioning"
    assert item_id not in [i["id"] for i in store.due_items()], "nothing recurs before Keep"
    return item


def _weather_model(model: str, prompt: str) -> str:
    if "Classify a dashboard-card request" in prompt:
        return json.dumps({"kind": "external_data", "subject": "NYC temperature", "cadence_minutes": 30,
                           "wants": ["temperature"], "threshold": None, "display_hint": "value"})
    return json.dumps({"serves": True, "gaps": [], "wrong": []})


def test_a_keyless_declared_row_builds_from_its_declared_answers_with_no_mapping(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, fetched = _consented(monkeypatch, _Wrap(lib, [_row("open-meteo-forecast", WEATHER_URL)]),
                                         ask="NYC temperature every 30 minutes")
    out = ni_flow.run_flow(store, item_id, gateway_call=_weather_model,
                           fetcher=_fetcher(fetched, {WEATHER_URL: OPEN_METEO}))
    _held_auto_card(store, item_id, out, WEATHER_URL, path="auto")
    assert fetched == [WEATHER_URL] and "declared answers" in json.dumps(ni_flow._flow_read(store, item_id).get("notes"))


def test_a_keyless_undeclared_data_row_builds_by_mapping_and_is_held_for_keep(monkeypatch) -> None:
    """2026-10-30: a data row the Library has not described builds the way a pasted link does (the model maps the
    sample, the checks judge it) and waits for the user's Keep; before, it was a link and the ask declined."""
    store, item_id, fetched = _consented(monkeypatch, _StockLibrary("keyless"))
    out = ni_flow.run_flow(store, item_id, gateway_call=_model, fetcher=_fetcher(fetched, {STOOQ: QUOTE}))
    item = _held_auto_card(store, item_id, out, STOOQ)
    assert fetched == [STOOQ]
    assert "181.25" in json.dumps(store.read_snapshot(item_id, "preview")), "the quote's current price is on the card"
    assert item["spec"]["source"].get("headers", {}) == {} and "token" not in json.dumps(item["spec"])


def test_a_keyed_undeclared_row_asks_for_the_key_then_builds(monkeypatch) -> None:
    """The NVDA ask on T's install: the quote rows are keyed and undeclared. Nothing is fetched and no secret is
    read before the card asks; the key the user gives is stored under this card, bound to the host, and the
    build then samples with it and maps the quote."""
    store, item_id, fetched = _consented(monkeypatch, _StockLibrary("keyed"))
    out = ni_flow.run_flow(store, item_id, gateway_call=_model, fetcher=_fetcher(fetched, {FINNHUB: QUOTE}))
    assert out["state"] == "awaiting_access" and fetched == [] and out["source_url"] == FINNHUB
    record = ni_flow._flow_read(store, item_id)
    assert record["_access"]["key"] == KEY_FINNHUB and record["_access"]["host"] == "finnhub.io"
    assert [r["url"] for r in record["_ranked_library"]] == [FINNHUB, ALPHA], "the rows a NO can move on to"
    secrets = ni_flow._resolve_secrets_store()
    assert ni_flow.missing_access(store, item_id, secrets) == ["key"]
    assert ni_flow.give_access(store, item_id, secrets, key="fh-secret-123", email=None) == []
    assert json.loads(secrets.get(f"ni:{item_id}:api_key"))["host"] == "finnhub.io"
    out = ni_flow.run_flow(store, item_id, gateway_call=_model, fetcher=_fetcher(fetched, {FINNHUB: QUOTE}),
                           source_url=FINNHUB)
    item = _held_auto_card(store, item_id, out, FINNHUB)
    assert fetched == [FINNHUB] and "181.25" in json.dumps(store.read_snapshot(item_id, "preview"))
    assert "fh-secret-123" not in json.dumps(item["spec"]) and "fh-secret-123" not in json.dumps(record)


def test_a_key_the_user_gave_another_card_for_the_host_is_offered_for_one_tap(monkeypatch) -> None:
    """T's orphaned Alpha Vantage key: a key given to an earlier Library card for the same host is never read
    before the card asks (no silent copy on an auto pick), but the card offers it, and one tap builds."""
    store = _store()
    secrets = SecretStore(store.conn, gen_master_key())
    ni.put_credential(secrets, "an-older-card", "api_key", "av-older-key", "www.alphavantage.co")
    library = _StockLibrary("keyed")
    library.rows.reverse()   # Alpha Vantage first
    store2, item_id, fetched = _consented(monkeypatch, library, secrets)
    out = ni_flow.run_flow(store2, item_id, gateway_call=_model, fetcher=_fetcher(fetched, {ALPHA: QUOTE}))
    assert out["state"] == "awaiting_access" and out["source_url"] == ALPHA and fetched == []
    assert ni_flow.missing_access(store2, item_id, secrets) == [], "a Library row's same-host key is reused on the ask"
    assert json.loads(secrets.get(f"ni:{item_id}:api_key"))["host"] == "www.alphavantage.co"
    out = ni_flow.run_flow(store2, item_id, gateway_call=_model, fetcher=_fetcher(fetched, {ALPHA: QUOTE}),
                           source_url=ALPHA)
    _held_auto_card(store2, item_id, out, ALPHA)
    assert fetched == [ALPHA]


def test_a_page_row_stays_a_link_and_the_decline_lists_it(monkeypatch) -> None:
    store, item_id, fetched = _consented(monkeypatch, _StockLibrary("pages"))
    dbmod.meta_set(store.conn, "ni_research", "off")
    out = ni_flow.run_flow(store, item_id, gateway_call=_model, fetcher=_fetcher(fetched, {}))
    assert out["state"] == "declined" and fetched == []
    assert [link["url"] for link in out["_decline_card"]["links"]] == ["https://finnhub.io"]   # the provider's page


# ---- the Settings page says what the flow runs -----------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        test_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield test_client


@pytest.mark.parametrize(("routes", "expect"), [
    (T_ROUTES, {"build": ("cloud", "claudecode/opus"), "research": ("cloud", "claudecode/opus"),
                "refresh": ("cloud", "claudecode/opus"), "summary": (None, None)}),
    (ANTHROPIC, {"build": ("cloud", "anthropic/claude-sonnet-5-5"), "research": ("cloud", "anthropic/claude-sonnet-5-5"),
                 "refresh": ("cloud", "anthropic/claude-sonnet-5-5"), "summary": (None, None)}),
    (LOCAL_ONLY, {s: ("local", "mlx/qwen-local") for s in ni_models.STEPS}),
    (OTHER_CLOUD, {"build": ("cloud", "openai/gpt-4o"), "research": ("cloud", "openai/gpt-4o"),
                   "refresh": ("cloud", "openai/gpt-4o"), "summary": (None, None)}),
    ({}, {s: (None, None) for s in ni_models.STEPS}),
])
def test_the_settings_page_reports_the_model_each_step_really_runs_on(client: TestClient, monkeypatch, routes, expect) -> None:
    """The page reads ``ni_models.choose``, the same rule the flow runs: "no model is routed" appears only where
    the step would really stop (2026-10-29: it said so for every step of T's install while builds ran)."""
    monkeypatch.setattr(gateway, "load_routes", lambda c: dict(routes))
    steps = {s["step"]: s for s in client.get("/api/ni/step-models").json()["steps"]}
    assert {s: (steps[s]["where"], steps[s]["model"]) for s in ni_models.STEPS} == expect
    assert steps["build"]["fallback"] is (routes == OTHER_CLOUD)
    conn = client.app.state.dbx
    store = ni.NIStore(conn, gen_master_key())
    assert ni_flow._resolve_flow_model(store) == expect["build"][1], "the flow's build model is the page's"


def test_the_settings_page_refuses_a_side_nothing_is_routed_on(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(gateway, "load_routes", lambda c: dict(T_ROUTES))
    r = client.put("/api/ni/step-models", json={"steps": {"build": "local"}})
    assert r.status_code == 400 and "no local model is routed" in r.json()["detail"]
    r = client.put("/api/ni/step-models", json={"steps": {"research": "cloud"}})
    assert r.status_code == 200 and {s["step"]: s["model"] for s in r.json()["steps"]}["research"] == "claudecode/opus"
    assert ni_models.stored(client.app.state.dbx) == {"research": "cloud"}
