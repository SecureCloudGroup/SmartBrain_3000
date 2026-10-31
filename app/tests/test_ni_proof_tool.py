"""``tools/ni-proof.py``: the observation that decides "fixed" (postmortem 2026-10-31, rule A1, M13).

The tool drives a running SmartBrain over its local API. These run it against the real app (``create_app`` with
its routes, flow worker, key store and engine) on T's route table, over the v1.5.0 Library rows for the NVDA ask
and the provider bodies captured on T's Mac, with the same two seams as the proof asks: the model (scripted, it
reads the real menu) and ``netguard._guarded_get_once`` (the bytes the provider sent). So what the tool reports is
what the card did on the production path, and the report is checked for the one thing it must never hold: a key.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_ni_proof_asks import (
    ASK,
    AV_KEY,
    AV_QUOTE,
    FH_KEY,
    ROWS_NVDA,
    T_ROUTES,
    _Library,
    _Model,
    _Replay,
    fixture,
)

from smartbrain_3000 import (
    gateway,
    netguard,
    ni,
    ni_flow,
    ni_message,
    ni_polite,
    ni_routes,
)

TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-proof.py"
pytestmark = pytest.mark.skipif(not TOOL.exists(), reason="the proof tool ships in the repository only")
AV_DAILY = ROWS_NVDA["rows"][2]["url"]


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("ni_proof_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # its dataclasses resolve their annotations through the module table
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A fresh install on T's routes (Claude Code for ni/chat/agent, no local model), unlocked."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "proof.duckdb"))
    monkeypatch.setattr(gateway, "load_routes", lambda conn: dict(T_ROUTES))
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    from smartbrain_3000.main import create_app
    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        yield client


def _wire(monkeypatch, rows: list[dict], replay: _Replay) -> _Model:
    """The pack's rows for the ask, the providers' captured answers, and the scripted model at the worker."""
    library = _Library(rows)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: library)
    monkeypatch.setattr(netguard, "_guarded_get_once", replay)
    model = _Model()
    real = ni_flow.run_flow

    def run_flow(store, item_id, *, gateway_call=None, **kwargs):
        return real(store, item_id, gateway_call=gateway_call or model, **kwargs)

    monkeypatch.setattr(ni_flow, "run_flow", run_flow)   # the worker reads the module's name at call time
    return model


def _api(client: TestClient):
    def call(method: str, path: str, body: dict | None = None):
        resp = client.request(method, path, json=body) if body is not None else client.request(method, path)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"text": resp.text[:400]}
    return call


def _run(tool, client, out: pathlib.Path, *args: str, environ: dict | None = None,
         api=None) -> tuple[int, dict, str]:
    code = tool.main(["--ask", ASK, "--timeout", "60", "--poll", "0.05", "--out", str(out), *args],
                     api=api or _api(client), environ=environ or {})
    reports = sorted(out.glob("*.json"))
    assert len(reports) <= 1
    report = json.loads(reports[0].read_text(encoding="utf-8")) if reports else {}
    markdown = reports[0].with_suffix(".md").read_text(encoding="utf-8") if reports else ""
    return code, report, markdown


def test_the_nvda_ask_is_observed_from_the_words_to_a_refreshed_card(tool, app, monkeypatch, tmp_path,
                                                                      capsys) -> None:
    """The run the done gate names: the ask typed, T's cloud model allowed on the card, Alpha Vantage's key given
    from an environment variable when the card asks for it, the reading kept, one refresh. The report shows each
    of those steps, the price the card holds, the audit rows, and no key anywhere."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": AV_KEY})
    _wire(monkeypatch, ROWS_NVDA["rows"], replay)
    code, report, markdown = _run(tool, app, tmp_path / "out", "--consent", "allow", "--key",
                                  "www.alphavantage.co=SB_PROOF_AV", "--yes", "--refresh", "--expect", "built",
                                  environ={"SB_PROOF_AV": AV_KEY})
    assert code == 0 and report["end"] == "built" and report["met"] is True, (report.get("end"), report["events"])
    assert report["why_not"] == [] and "Not met" not in markdown
    assert [t["tap"] for t in report["taps"]] == ["consent", "key:www.alphavantage.co", "yes"]
    assert all(t["status"] == 200 for t in report["taps"])
    asked = [e.get("asks") for e in report["events"]]
    assert "model_consent" in asked, "the card asked for the cloud model before any model call"
    paused = [e for e in report["events"] if e.get("access")]
    assert paused and paused[0]["access"]["host"] == "www.alphavantage.co" and paused[0]["access"]["key"]
    assert any(e.get("awaiting_yes", {}).get("host") == "www.alphavantage.co" for e in report["events"])
    price = float(fixture("alphavantage-global-quote")["body"]["Global Quote"]["05. price"])
    assert {"at": "price", "value": price} in report["card"]["values"], report["card"]
    assert report["card"]["built_from"]["host"] == "www.alphavantage.co"
    assert report["refresh"]["status"] == 200 and report["refresh"]["answer"]["status"] == "ok"
    assert report["card"]["state"] == "live" and report["card"]["runs"][0]["status"] == "ok"
    assert [s["model"] for s in report["app"]["steps"] if s["step"] == "build"] == ["claudecode/opus"]
    tools_seen = {a["tool"] for a in report["audit"]}
    assert {"ni_intake", "ni_flow_answer", "ni_flow_access"} <= tools_seen, tools_seen
    assert all(u == f"{AV_QUOTE}&apikey={AV_KEY}" for u in replay.fetched), "the key went to its own host only"
    printed = capsys.readouterr()
    for text in (json.dumps(report), markdown, printed.out, printed.err):
        assert AV_KEY not in text, "no key in the report or on the terminal"
    assert "SB_PROOF_AV" in markdown and "End: **built**" in markdown


def test_a_daily_ask_that_cannot_build_is_reported_with_what_was_tried(tool, app, monkeypatch, tmp_path) -> None:
    """The daily series files its values under each day's date: the card moves on and declines (research is off
    here), and the report carries the card's own tried line and decline, the reason the user would read."""
    replay = _Replay({AV_DAILY: "alphavantage-time-series-daily"}, {"www.alphavantage.co": AV_KEY})
    _wire(monkeypatch, [ROWS_NVDA["rows"][2]], replay)
    code, report, markdown = _run(tool, app, tmp_path / "out", "--key", "www.alphavantage.co=SB_PROOF_AV",
                                  "--expect", "declined", environ={"SB_PROOF_AV": AV_KEY})
    assert code == 0 and report["end"] == "declined", (report.get("end"), report["events"])
    last = report["events"][-1]
    assert last["decline"]["reason"] and last["decline"]["line"]
    assert any(t.startswith("www.alphavantage.co: ") for t in last["tried"]), last
    assert "## Sources tried" in markdown and "www.alphavantage.co: " in markdown
    assert "## Decline" in markdown and last["decline"]["line"] in markdown
    assert AV_KEY not in json.dumps(report) + markdown


def test_a_card_waiting_on_a_key_the_run_was_not_given_times_out_and_says_so(tool, app, monkeypatch,
                                                                             tmp_path) -> None:
    """No key for the first row and no skip: the card waits for a person, the run reports that and ends at its
    timeout (exit 3), sending nothing to the provider."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"})
    _wire(monkeypatch, ROWS_NVDA["rows"], replay)
    code, report, _ = _run(tool, app, tmp_path / "out", "--timeout", "1.5")
    assert code == 3 and report["end"] == "timeout"
    assert report["events"][-1]["flow"] == "awaiting_access" and replay.fetched == []
    assert "skip_source" in report["events"][-1]["actions"], "the card offers the skip this run did not take"


def test_skip_keyless_goes_past_a_row_with_no_key_to_the_one_it_has(tool, app, monkeypatch, tmp_path) -> None:
    """Given only Finnhub's key, the run skips the Alpha Vantage quote at its pause and the build goes on to
    Finnhub, whose key it then gives: the card builds from finnhub.io and the tried list names the skip."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote", ROWS_NVDA["rows"][1]["url"]: "finnhub-quote"},
                     {"finnhub.io": FH_KEY})
    _wire(monkeypatch, ROWS_NVDA["rows"][:2], replay)
    code, report, _ = _run(tool, app, tmp_path / "out", "--key", "finnhub.io=SB_PROOF_FH", "--skip-keyless",
                           "--expect", "built", environ={"SB_PROOF_FH": FH_KEY})
    assert code == 0 and report["card"]["built_from"]["host"] == "finnhub.io", report["events"]
    assert [t["tap"] for t in report["taps"]] == ["consent", "skip:www.alphavantage.co", "key:finnhub.io"]
    assert any("www.alphavantage.co: " in t for e in report["events"] for t in e.get("tried") or [])
    assert all("finnhub.io" in u for u in replay.fetched), "nothing was sent to the source it skipped"


def test_consent_stop_reports_the_question_and_sends_nothing(tool, app, monkeypatch, tmp_path) -> None:
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"})
    model = _wire(monkeypatch, ROWS_NVDA["rows"], replay)
    code, report, _ = _run(tool, app, tmp_path / "out", "--consent", "stop", "--expect", "not-built")
    assert code == 0 and report["end"] == "stopped" and report["taps"] == []
    assert report["events"][-1]["asks"] == "model_consent" and sum(model.calls.values()) == 0
    assert replay.fetched == []


# ---- what the first run on T's Mac showed (2026-10-31 13:47Z) ---------------------------------------------------
# The NVDA card built there in 16 s from Alpha Vantage on claudecode/opus, price 204.64. The tool's refresh came
# 0.2 s after the YES's own run; Alpha Vantage answered its "1 request per second" sentence; the route said "the
# local page browser is busy" (no page browser was involved), left the card stamped as checked so its first
# scheduled proof moved a whole cadence out, and the tool still reported the run as met.

@pytest.fixture
def polite_clock(monkeypatch):
    """The politeness layer's clock as a test clock: a wait the host asks for moves it and is recorded."""
    clock = SimpleNamespace(now=1000.0, slept=[])

    def sleep(seconds: float) -> None:
        clock.slept.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(ni_polite, "_clock", lambda: clock.now)
    monkeypatch.setattr(ni_polite, "_sleep", sleep)
    return clock


def _limited_from_the_refresh(client: TestClient, replay: _Replay, answers: list[str]):
    """The app's API as the tool calls it, Alpha Vantage answering ``answers`` from the refresh on: the YES's run
    has just fetched, and a second request inside a second gets the limit sentence."""
    call = _api(client)

    def api(method: str, path: str, body: dict | None = None):
        if method == "POST" and path.endswith("/run"):
            replay.serve(AV_QUOTE, answers)
        return call(method, path, body)
    return api


def test_a_refresh_right_after_yes_waits_the_sources_second_out_and_reads_the_quote(tool, app, monkeypatch, tmp_path,
                                                                                    polite_clock) -> None:
    """A run someone tapped waits the second the host named, once, as a build does, asks again and reads the
    quote: the refresh is ok and the card goes live."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": AV_KEY})
    _wire(monkeypatch, ROWS_NVDA["rows"], replay)
    api = _limited_from_the_refresh(app, replay, ["alphavantage-burst-limited", "alphavantage-global-quote"])
    code, report, _ = _run(tool, app, tmp_path / "out", "--key", "www.alphavantage.co=SB_PROOF_AV", "--yes",
                           "--refresh", "--expect", "built", environ={"SB_PROOF_AV": AV_KEY}, api=api)
    assert code == 0 and report["met"] is True and report["why_not"] == [], report.get("why_not")
    assert report["refresh"]["status"] == 200 and report["refresh"]["answer"]["status"] == "ok"
    assert polite_clock.slept == [ni_message.WAIT_SECOND_S], "the second the host named, waited once"
    assert replay.fetched[-2:] == [f"{AV_QUOTE}&apikey={AV_KEY}"] * 2, "the limited request, and once more"
    assert report["card"]["state"] == "live"


def test_a_refresh_the_source_still_refuses_says_so_and_leaves_the_card_as_due_as_it_was(tool, app, monkeypatch,
                                                                                         tmp_path,
                                                                                         polite_clock) -> None:
    """The host asks again after the wait: the refresh is a 409 with the host's code and a sentence about the
    source (never the page browser), the run is not met and the report says why, and the card is exactly as due
    as the YES left it, so the engine's next pass makes its first scheduled proof once the host allows."""
    replay = _Replay({AV_QUOTE: "alphavantage-global-quote"}, {"www.alphavantage.co": AV_KEY})
    _wire(monkeypatch, ROWS_NVDA["rows"], replay)
    api = _limited_from_the_refresh(app, replay, ["alphavantage-burst-limited"])
    code, report, markdown = _run(tool, app, tmp_path / "out", "--key", "www.alphavantage.co=SB_PROOF_AV", "--yes",
                                  "--refresh", "--expect", "built", environ={"SB_PROOF_AV": AV_KEY}, api=api)
    assert code == 1 and report["end"] == "built" and report["met"] is False
    assert report["refresh"] == {"status": 409, "answer": {"detail": ni_routes._HOST_BUSY_DETAIL,
                                                           "code": "host_busy"}}
    assert "page browser" not in ni_routes._HOST_BUSY_DETAIL
    assert report["why_not"][0].startswith("the refresh answered HTTP 409") and "Not met: the refresh" in markdown
    store = app.app.state.ni
    item = store.get_item(report["item_id"])
    assert item["state"] == "commissioning" and item["consecutive_failures"] == 0
    assert item["last_checked"] is None, "as due as the YES left it: its first scheduled proof is the next pass"
    polite_clock.now += ni_message.WAIT_SECOND_S + 1
    replay.serve(AV_QUOTE, "alphavantage-global-quote")
    ni.tick(app.app)
    item = store.get_item(report["item_id"])
    assert item["state"] == "live" and item["last_status"] == "ok", "the card refreshed on its own"


def test_the_watch_waits_for_the_next_scheduled_run_and_stops_at_it(tool) -> None:
    """``--watch``: the ask's own cadence is what is being proved. The watch only reads the card, and stops at
    the first new run that read the source; a window with none is not met, and the report says so."""
    clock = SimpleNamespace(now=0.0)
    runs = [{"ts": "2026-10-31 13:47:16", "status": "ok", "duration_ms": 140, "error": None}]

    def api(method: str, path: str, body: dict | None = None):
        assert method == "GET" and body is None, "the watch only reads"
        if path == "/api/ni/board":
            return 200, {"items": [{"id": "c1", "state": "live", "summary": "price: 204.64",
                                    "payload": {"price": 204.64}}]}
        return 200, {"runs": list(runs)}

    def sleep(seconds: float) -> None:
        clock.now += seconds
        if clock.now >= 1800 and len(runs) == 1:   # the engine's pass, 30 minutes after the refresh
            runs.insert(0, {"ts": "2026-10-31 14:17:30", "status": "ok", "duration_ms": 150, "error": None})

    plan = tool.Plan(ask=ASK, refresh=True, watch_s=33 * 60.0)
    seen = tool._watch_runs(api, "c1", {"state": "live", "runs": list(runs)}, plan, lambda: clock.now, sleep,
                            lambda line: None)
    assert seen["ok_runs"] == 1 and [r["ts"] for r in seen["runs"]] == ["2026-10-31 14:17:30"]
    assert 1800 <= seen["waited_s"] <= 1800 + tool.WATCH_POLL_MAX_S, "stopped at the run, not at the window"
    report = {"end": "built", "refresh": {"status": 200, "answer": {"status": "ok"}},
              "watch": {"minutes": 1.0, "runs": [], "ok_runs": 0}}
    refreshed, watched, why = tool.asked_for(report, tool.Plan(ask=ASK, refresh=True, watch_s=60.0))
    assert refreshed is True and watched is False and why == ["no scheduled run read the source within 1.0 min"]
    assert tool.met("built", {"values": [1]}, "built", refreshed=True, watched=False) is False
    assert tool.met("built", {"values": [1]}, "built", refreshed=False) is False
    assert tool.met("built", {"values": [1]}, "built") is True, "nothing asked beyond the build"


# ---- the pieces -------------------------------------------------------------------------------------------------

def _view(flow: dict | None, **over) -> dict:
    return {"id": "card-1", "state": "draft", "flow": flow, "awaiting_yes": None, "actions": ["cancel", "delete"],
            **over}


def test_each_answer_is_the_plans_and_is_made_once(tool) -> None:
    plan = tool.Plan(ask=ASK, keys={"www.alphavantage.co": "AV"}, reuse=True, skip_keyless=True, yes=True)
    consent = _view({"state": "unsupported", "question": {"kind": "model_consent", "model": "claudecode/opus"}})
    tap = tool.tap_for(consent, plan, set(), {})
    assert tap.body == {"kind": "model_consent", "value": "allow"} and "claudecode/opus" in tap.note
    assert tool.tap_for(consent, plan, {"consent"}, {}) is None, "a question asked again is a person's"
    assert tool.tap_for(consent, tool.Plan(ask=ASK, consent="stop"), set(), {}) is None
    keyed = _view({"state": "awaiting_access", "access": {"host": "www.alphavantage.co", "key": {"reuse": True}}},
                  actions=["skip_source", "cancel", "delete"])
    tap = tool.tap_for(keyed, plan, set(), {"AV": "the-key-value"})
    assert tap.body == {"key": "the-key-value"} and "the-key-value" not in tap.note and "$AV" in tap.note
    assert tool.tap_for(keyed, plan, {"key:www.alphavantage.co"}, {"AV": "x"}).name == "reuse:www.alphavantage.co"
    other = _view({"state": "awaiting_access", "access": {"host": "finnhub.io", "key": {}}},
                  actions=["skip_source", "delete"])
    assert tool.tap_for(other, plan, set(), {}).path.endswith("/flow/skip-source")
    assert tool.tap_for(other, tool.Plan(ask=ASK), set(), {}) is None, "no skip unless the run was told to"
    held = _view(None, state="commissioning", awaiting_yes={"from": "library", "host": "www.alphavantage.co"})
    assert tool.tap_for(held, plan, set(), {}).body == {"ok": True}
    assert tool.end_of(held) == "built" and tool.end_of(consent) == "unsupported" and tool.end_of(keyed) is None


def test_the_report_is_refused_when_a_key_or_the_token_is_in_it(tool, tmp_path) -> None:
    report = {"ask": ASK, "started_at": "2026-10-31T13:00:00+00:00", "app": {"version": "x", "steps": []},
              "events": [{"t": 0.1, "card": "draft", "reason": "the provider said: key the-key-value is wrong"}],
              "taps": [], "card": {}, "end": "failed", "seconds": 1.0}
    with pytest.raises(ValueError):
        tool.write_report(report, tmp_path, ["the-key-value"])
    assert not list(tmp_path.iterdir()), "nothing written"
    json_path, md_path = tool.write_report(report, tmp_path, ["another-key-value"])
    assert json_path.name.startswith("nvda-stock-price-every-30-minutes-") and md_path.exists()


def test_the_token_goes_to_this_computer_only_and_is_read_from_the_launchers_file(tool, tmp_path) -> None:
    with pytest.raises(tool.ProofError):
        tool.http_api("http://192.168.1.20:33000", "t" * 40)
    with pytest.raises(tool.ProofError):
        tool.http_api("https://example.com", "t" * 40)
    token = tmp_path / "local-api.token"
    token.write_text("z" * 43 + "\n", encoding="ascii")
    assert tool.read_token(str(token), {}) == "z" * 43
    assert tool.read_token(None, {"SMARTBRAIN_LOCAL_TOKEN": "e" * 40}) == "e" * 40
    (tmp_path / "short").write_text("abc", encoding="ascii")
    with pytest.raises(tool.ProofError):
        tool.read_token(str(tmp_path / "short"), {})


def test_numbers_are_read_off_the_payload_with_where_they_sit(tool) -> None:
    payload = {"price": 207.04, "label": "NVDA", "rows": [{"close": 1.5, "up": True}], "n": 3}
    assert tool.numbers_in(payload) == [{"at": "price", "value": 207.04}, {"at": "rows[0].close", "value": 1.5},
                                        {"at": "n", "value": 3}]
    assert tool.met("built", {"values": []}, "built") is False, "built means a number on the card"
    assert tool.met("declined", {}, "not-built") and not tool.met("timeout", {}, "any")


def test_no_line_the_tool_prints_holds_a_key_or_the_token(tool, tmp_path, capsys) -> None:
    """Review of #569: the report file refuses a key, but the lines printed on the way there (the card's reason, a
    refusal) went out as they came, and those lines are pasted where the report goes. Every printed line has the
    run's keys and the token scrubbed first, whatever put one there."""
    key = "QWERTYUIOPASDFGH"

    def api(method: str, path: str, body: dict | None = None):
        if path == "/api/health":
            return 200, {"version": "test"}
        if path == "/api/ni/step-models":
            return 200, {"steps": []}
        if path == "/api/ni/intake":
            return 200, {"id": "c1"}
        if path == "/api/ni/items/c1/flow":
            return 200, {"id": "c1", "state": "draft", "actions": ["delete"],
                         "flow": {"state": "failed", "reason": f"The source said: We have detected your API key as {key}",
                                  "error": f"waited: www.alphavantage.co asked SmartBrain to wait: key {key}"}}
        return 200, {}

    out = tmp_path / "out"
    code = tool.main(["--ask", ASK, "--key", "www.alphavantage.co=SB_PROOF_AV", "--timeout", "5", "--poll", "0.01",
                      "--out", str(out)], api=api, environ={"SB_PROOF_AV": key})
    printed = capsys.readouterr()
    assert code == 2 and not out.exists(), "the report would hold the key: nothing was written"
    assert key not in printed.out + printed.err, printed
    assert f"API key as {tool.SCRUBBED}" in printed.out and "nothing was written" in printed.err
    assert tool.scrubbed("token=tok-1 key=tok-1-and-more", ["tok-1", "tok-1-and-more", ""]) == \
        f"token={tool.SCRUBBED} key={tool.SCRUBBED}", "the longest first, so no part of a key is left"
