"""The route matrix (2026-10-29, the NVDA card): T's real configuration, and every other, driven end to end.

No test drove an install whose ni / chat / agent routes are all Claude Code with no local model (#561, the NVDA
card): research silently had no model there, the card declined with "no source answers this", and the Settings
page said "no model is routed" for every step, build included. These tests run the same ask through the real
flow for each route configuration and check three things: which model each step really runs on (and that the
Settings page says the same), that a cloud model is asked for on the card before anything leaves this computer,
and that an ask that cannot be researched says WHY in its own line.

The model seams are at the gateway (``gateway_call`` for the build, ``gateway.chat_with_tools_recorded`` /
``chat_recorded`` for the research), never the researcher's installed seam, so model resolution runs for real.
"""
from __future__ import annotations

import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_ni_auto_pick import _row
from test_ni_research_loop import reply, text_reply

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, ni, ni_flow, ni_master, ni_models, ni_research_flow
from smartbrain_3000.secrets import gen_master_key

ASK = "NVDA stock price every 30 minutes"
FINNHUB = "https://finnhub.io/api/v1/quote?symbol=NVDA"
ALPHA = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=NVDA"
STOOQ = "https://stooq.example/q/l/?s=nvda.us&f=sd2t2ohlcv&e=json"
KEY_FINNHUB = {"in": "query", "name": "token", "prefix": "", "docs_url": "https://finnhub.io/docs/api"}
KEY_ALPHA = {"in": "query", "name": "apikey", "prefix": "", "docs_url": "https://www.alphavantage.co/support/"}
T_ROUTES = {"ni": "claudecode/opus", "chat": "claudecode/sonnet", "agent": "claudecode/sonnet"}
ANTHROPIC = {"chat": "anthropic/claude-sonnet-5-5"}
LOCAL_ONLY = {"chat": "mlx/qwen-local"}
OTHER_CLOUD = {"ni": "openai/gpt-4o"}   # a provider neither side of a step takes: the older route order only
LOOKING = {"since": "2026-10-29T00:00:00+00:00", "runs": 1, "next_at": None}   # the user tapped Keep looking


class _StockLibrary:
    """A Library whose only rows for the ask are keyed quote sources that declare no answers (pack v1.5.0:
    ``finnhub-quote`` and ``alphavantage-quote`` are exactly that)."""

    def __init__(self, keyless: bool = False) -> None:
        self.rows = [_row("finnhub-quote", FINNHUB, needs_key=KEY_FINNHUB, provider="Finnhub"),
                     _row("alphavantage-quote", ALPHA, needs_key=KEY_ALPHA, provider="Alpha Vantage")]
        if keyless:   # an undeclared row the closed-world local tier may read (it never reads a keyed one)
            self.rows.append(_row("stooq-quote", STOOQ, needs_key=None, format="json", provider="Stooq"))

    def candidates(self, request: str, limit: int = 3, hint: dict | None = None):
        return [dict(r) for r in self.rows[:limit]], []

    def answers(self, source_id: str) -> list[dict]:
        return []

    def get(self, source_id: str) -> dict | None:
        host = {"finnhub-quote": "https://finnhub.io", "alphavantage-quote": "https://www.alphavantage.co",
                "stooq-quote": "https://stooq.example"}
        return {"access": {"docs_url": host[source_id] + "/docs"}, "provider": {"url": host[source_id]}}


def _store() -> ni.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return ni.NIStore(conn, gen_master_key())


def _intent(model: str, prompt: str) -> str:
    return json.dumps({"kind": "external_data", "subject": "NVDA", "cadence_minutes": 30, "wants": ["price"],
                       "threshold": None, "display_hint": "value"})


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


@pytest.fixture(autouse=True)
def _wired(monkeypatch) -> Iterator[None]:
    for name in ("SMARTBRAIN_NI_RESEARCH", "SMARTBRAIN_NI_RESEARCH_MODEL", "SMARTBRAIN_NI_RESEARCH_WEB"):
        monkeypatch.delenv(name, raising=False)
    ni_research_flow.set_research_chat(None)   # the gateway path: model resolution runs for real
    library = _StockLibrary()
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: library)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    yield


def _drive(monkeypatch, routes: dict, research: _Research, *, allow: bool = True) -> tuple[ni.NIStore, str, dict, list]:
    """Intake the ask as a standing order (so research runs at once), answer the consent question when one is
    asked, and return the end record plus the models the build called."""
    monkeypatch.setattr(gateway, "load_routes", lambda conn: dict(routes))
    monkeypatch.setattr(gateway, "chat_with_tools_recorded", research.tools)
    monkeypatch.setattr(gateway, "chat_recorded", research.plain)
    store = _store()
    item_id = ni_flow.create_shell_item(store, ASK)
    ni_flow._flow_write(store, item_id, {**ni_flow._make_record(ASK, "intent"), "_looking": LOOKING})
    built: list[str] = []
    call = lambda m, p: built.append(m) or _intent(m, p)
    fetch = lambda url: (_ for _ in ()).throw(AssertionError(f"nothing is fetched before a key: {url}"))
    out = ni_flow.run_flow(store, item_id, gateway_call=call, fetcher=fetch)
    if out["state"] == "unsupported" and allow:
        question = ni_master.question_for("unsupported", out)
        assert question["kind"] == "model_consent" and built == [], "asked before any model call"
        assert out["_question"]["web"] is True, "the question shows the research and web wording the consent seals"
        ni_flow._transition(store, item_id, "intent", _question=None, error=None,
                            _model_consent=question["model"], _model_consent_web=True)
        out = ni_flow.run_flow(store, item_id, gateway_call=call, fetcher=fetch)
    return store, item_id, out, built


@pytest.mark.parametrize(("routes", "cloud"), [(T_ROUTES, "claudecode/opus"), (ANTHROPIC, "anthropic/claude-sonnet-5-5")])
def test_a_cloud_route_builds_and_researches_on_the_consented_model(monkeypatch, routes, cloud) -> None:
    """T's install (Claude Code routes, no local model) and an Anthropic install: one consent question names the
    model; the build and the research both run on it; nothing is fetched (the quote rows need a key)."""
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
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _StockLibrary(keyless=True))
    research = _Research([text_reply('{"pick": null}')])
    _store_, _id, out, built = _drive(monkeypatch, LOCAL_ONLY, research)
    assert set(built) == {"mlx/qwen-local"} and research.models == ["mlx/qwen-local"]
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "no_answering_source"
    assert "model" not in out["_decline_card"]["line"], "research ran: the line is the run's own verdict"


def test_no_route_at_all_fails_honestly_before_any_call(monkeypatch) -> None:
    research = _Research([])
    _store_, _id, out, built = _drive(monkeypatch, {}, research)
    assert out["state"] == "failed" and built == [] and research.models == []
    assert ni_master.error_class_of(out["error"]) == "model"
    assert ni_master.reason_for("failed", out["error"]).startswith("No model is routed for building cards")


def test_a_provider_no_side_takes_builds_through_the_older_order_and_the_decline_names_the_missing_model(monkeypatch) -> None:
    """A build still runs on an explicit ni route of any provider (the older order), but research takes only an
    Anthropic, Claude Code or local model: the ask ends declined and its line says so, never "no source answers
    this"; the paste box stays one tap away."""
    research = _Research([])
    store, item_id, out, built = _drive(monkeypatch, OTHER_CLOUD, research)
    assert set(built) == {"openai/gpt-4o"} and research.models == []
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "no_answering_source"
    line = out["_decline_card"]["line"]
    assert line.startswith("I can't look for a source: no cloud or local model is routed for research")
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
    out = ni_flow.run_flow(store, item_id, gateway_call=_intent, fetcher=lambda url: {})
    assert out["state"] == "declined" and research.models == []
    assert out["_decline_card"]["line"].startswith("I can't look for a source: research is turned off")


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
