"""S7: compare cards. An ask that compares two to four named things lands on a card that shows every one of
them with its own number, or ends honestly ("can't compare these yet"); never a one-thing card.

Synthetic sources only: one address per thing on a made-up host, the fetch layer faked at ``ni_fetch.fetch_parsed``
(the card run) or ``netguard`` (the researcher), and a fake Library that answers per thing.
"""
from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from test_ni import _FakeGateway, _store
from test_ni_answers import _INTENT, _QUERY_MARK, _ir_reply, _value, lib  # noqa: F401  (lib is a fixture)
from test_ni_auto_pick import _model, _Search  # noqa: F401
from test_ni_auto_pick import _store as _flow_store
from test_ni_research_tools import Net, _ctx

from smartbrain_3000 import (
    ni,
    ni_compare,
    ni_compare_flow,
    ni_fetch,
    ni_flow,
    ni_refresh,
    ni_research_flow,
)
from smartbrain_3000.ni_research import tools
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import SecretStore

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
HOST = "https://api.prices.example.org"
PRICES = {"Nvidia": 131.5, "AMD": 150.25, "Intel": 24.8, "Qualcomm": 160.0}
SLUG = {"Nvidia": "nvda", "AMD": "amd", "Intel": "intc", "Qualcomm": "qcom"}


def _url(name: str) -> str:
    return f"{HOST}/quote/{SLUG[name]}"


def _body(name: str, price: float | None = None) -> dict:
    return {"quote": {"price": PRICES[name] if price is None else price, "currency": "USD"}}


def _things(names: list[str]) -> list[dict]:
    return [{"name": n, "url": _url(n), "path": "quote.price"} for n in names]


def _ask(names: list[str]) -> str:
    return "compare " + ", ".join(names[:-1]) + " and " + names[-1] + " stock prices"


def _built(names: list[str]) -> dict:
    built = ni_compare.build(_things(names), [PRICES[n] for n in names], label="Price", unit="USD",
                             title=ni_compare.title_of(names, "Price"), ask=_ask(names), now=NOW, cadence_s=3600,
                             viewer_tz="UTC")
    assert built is not None
    return built


def _spec(names: list[str]) -> dict:
    built = _built(names)
    source = {"type": "http_json", "url": _url(names[0]), "compare": {"entities": _things(names)}}
    spec = ni_flow.build_final_spec(_ask(names), {"subject": _ask(names), "cadence_minutes": 60}, source, 60,
                                    built["pipeline"], built["scene"])
    spec["title"] = ni_compare.title_of(names, "Price")
    return ni.validate_spec(spec)


GROUPS = [["Nvidia", "AMD"], ["Nvidia", "AMD", "Intel"], ["Nvidia", "AMD", "Intel", "Qualcomm"]]


# ---- reading the ask --------------------------------------------------------------------------------------

@pytest.mark.parametrize("names", GROUPS, ids=["two", "three", "four"])
def test_asked_reads_each_thing_in_the_users_words(names) -> None:
    assert list(ni_compare.asked(_ask(names))) == names


@pytest.mark.parametrize("text", ["mortgage rates now versus a year ago", "weather today", "price of Nvidia stock"])
def test_a_time_comparison_or_one_thing_is_not_a_compare(text) -> None:
    assert ni_compare.asked(text) == ()


def test_entity_ask_names_just_one_thing() -> None:
    ask = ni_compare.entity_ask(_ask(["Nvidia", "AMD", "Intel"]), ("Nvidia", "AMD", "Intel"), 1)
    assert "AMD" in ask and "Nvidia" not in ask and "Intel" not in ask and "compare" not in ask.lower()


# ---- the build: every thing, its own number -----------------------------------------------------------------

@pytest.mark.parametrize("names", GROUPS, ids=["two", "three", "four"])
def test_build_shows_every_thing_with_its_own_number(names) -> None:
    built = _built(names)
    assert built["scene"]["form"] == "compare"
    rows = built["preview_payload"][ni_compare.ROWS]
    assert [r["name"] for r in rows] == names and [r["value"] for r in rows] == [PRICES[n] for n in names]
    have, missing = ni_compare.shown(names, built["preview_payload"])
    assert have == names and not missing and ni_compare.reasons(names, built["preview_payload"]) == []
    spec = _spec(names)
    assert ni_compare.refusal(spec, built["preview_payload"]) is None
    assert ni_compare.is_compare(spec) and [e["name"] for e in ni_compare.sealed(spec)] == names


def test_labels_come_from_the_question_not_the_source() -> None:
    names = ["Nvidia", "AMD"]
    body = {**_body("Nvidia"), "name": "IGNORE PREVIOUS", "symbol": "ZZZ"}
    built = ni_compare.build(_things(names), [ni_compare.number_at(body, "quote.price"), 150.25], label="Price",
                             unit="USD", title="t", ask=_ask(names), now=NOW, cadence_s=3600, viewer_tz="UTC")
    assert "IGNORE" not in str(built) and "ZZZ" not in str(built)


# ---- the sealed source shape ------------------------------------------------------------------------------

def _src(names=("Nvidia", "AMD"), **over) -> dict:
    src = {"type": "http_json", "url": _url(names[0]), "compare": {"entities": _things(list(names))}}
    src.update(over)
    return src


@pytest.mark.parametrize("mutate", [
    lambda s: s["compare"].update(extra=1),
    lambda s: s["compare"].update(entities=s["compare"]["entities"][:1]),
    lambda s: s["compare"].update(entities=_things(list(PRICES)) + [{"name": "Fifth", "url": HOST + "/q/5", "path": "a"}]),
    lambda s: s["compare"]["entities"][1].update(name="Nvidia"),  # duplicate name
    lambda s: s["compare"]["entities"][1].update(url=s["compare"]["entities"][0]["url"], path="quote.price"),  # same reading
    lambda s: s["compare"]["entities"][1].update(url="https://other.example.org/quote/amd"),  # a second host
    lambda s: s["compare"]["entities"][1].update(url=HOST + "/quote/{{ticker}}"),  # a template, not an address
    lambda s: s["compare"]["entities"][1].update(path="$$bad path"),
    lambda s: s.update(url=_url("AMD")),  # the source address is the first thing's
    lambda s: s.update(headers={"X": "y"}),
    lambda s: s.update(secret_query={"name": "k", "secret": "S"}),
], ids=["extra-key", "one", "five", "dup-name", "dup-url", "second-host", "template", "bad-path", "url-mismatch",
        "headers", "secret"])
def test_validate_source_refuses(mutate) -> None:
    src = _src()
    mutate(src)
    spec = _spec(["Nvidia", "AMD"])
    spec["source"] = src
    with pytest.raises((ValueError, TypeError, AssertionError)):
        ni.validate_spec(spec)


def test_one_address_may_serve_several_things_at_different_paths() -> None:
    src = _src()
    src["compare"]["entities"][1].update(url=src["compare"]["entities"][0]["url"], path="quote.other")
    ni_compare.validate_source(src)


# ---- the run: fan-out fetch, an honest partial --------------------------------------------------------------

def _run(monkeypatch, names, bodies: dict, *, fail: set | None = None):
    store, conn, key = _store()
    spec = _spec(names)
    iid = store.add_item(spec, _built(names)["preview_payload"])
    store.set_state(iid, "live")
    fetched: list[str] = []

    def fetch(url, fmt="json", **kw):
        fetched.append(url)
        if url in (fail or set()):
            raise ni_fetch.netguard.FetchError("down", kind="http_error")
        return copy.deepcopy(bodies[url])

    monkeypatch.setattr(ni_fetch, "fetch_parsed", fetch)
    try:
        result = ni.run_item(store, iid, gateway_mod=_FakeGateway(text="", model="x"),
                             secrets_store=SecretStore(conn, key), schedules_store=ScheduleStore(conn, key))
    except ni.NIError as exc:  # a failed run records its status, then raises to the scheduler
        result = {"error": str(exc), "kind": exc.kind}
    return store, iid, result, fetched


@pytest.mark.parametrize("names", GROUPS, ids=["two", "three", "four"])
def test_run_fetches_each_address_once_and_shows_all(monkeypatch, names) -> None:
    bodies = {_url(n): _body(n, PRICES[n] + 1) for n in names}
    store, iid, result, fetched = _run(monkeypatch, names, bodies)
    assert sorted(fetched) == sorted(_url(n) for n in names)
    assert result.get("status") in (None, "ok") and store.get_item(iid)["last_status"] in (None, "ok")
    snap = store.read_snapshot(iid, "latest") or store.read_snapshot(iid, "preview")
    assert snap is not None


@pytest.mark.parametrize("names", GROUPS, ids=["two", "three", "four"])
def test_one_failing_fetch_is_a_partial_status_and_the_last_good_stays(monkeypatch, names) -> None:
    bodies = {_url(n): _body(n) for n in names}
    store, iid, result, _ = _run(monkeypatch, names, bodies, fail={_url(names[-1])})
    item = store.get_item(iid)
    assert item["last_status"] == ni_compare.PARTIAL
    assert names[-1] in (item.get("last_error") or str(result))
    assert store.read_snapshot(iid, "preview") is not None  # the card keeps showing what it last showed


def test_a_shared_address_is_fetched_once_and_each_thing_read_at_its_path(monkeypatch) -> None:
    names = ["Nvidia", "AMD"]
    spec = _spec(names)
    shared = _url("Nvidia")
    for i, n in enumerate(names):
        spec["source"]["compare"]["entities"][i].update(url=shared, path=f"quotes.{SLUG[n]}")
    got: list[str] = []

    def get(url):
        got.append(url)
        return {"quotes": {SLUG[n]: PRICES[n] for n in names}}
    out = ni_compare.fetch(spec["source"], get)
    assert got == [shared] and [r["value"] for r in out["rows"]] == [PRICES[n] for n in names]


def test_a_programming_error_is_not_a_missing_thing() -> None:
    def get(url):
        raise KeyError("a bug, not a dead address")
    with pytest.raises(KeyError):
        ni_compare.fetch(_src(), get)


def test_a_dead_address_is_that_things_absence() -> None:
    def get(url):
        raise ni.NIError("fetch_failed", "down")
    with pytest.raises(ni.NIError) as exc:
        ni_compare.fetch(_src(), get)
    assert exc.value.kind == ni_compare.PARTIAL


def test_one_missing_number_is_a_partial_too(monkeypatch) -> None:
    names = ["Nvidia", "AMD", "Intel"]
    bodies = {_url(n): _body(n) for n in names}
    bodies[_url("AMD")] = {"quote": {"price": None}}
    store, iid, _, _ = _run(monkeypatch, names, bodies)
    assert store.get_item(iid)["last_status"] == ni_compare.PARTIAL


def test_a_busy_host_defers_instead_of_a_partial(monkeypatch) -> None:
    def get(url):
        raise ni.NIError(min(ni.CAPACITY_KINDS), "busy")
    source = _src()
    with pytest.raises(ni.NIError) as err:
        ni_compare.fetch(source, get)
    assert err.value.kind in ni.CAPACITY_KINDS


# ---- a one-thing card for a compare ask is refused on every build path ----------------------------------

ONE_THING = {"rows": [{"name": "Nvidia", "value": 131.5}]}


def test_refusal_names_the_missing_thing() -> None:
    spec = {"goal": _ask(["Nvidia", "AMD"])}
    why = ni_compare.refusal(spec, ONE_THING)
    assert why and why.startswith("can't compare these yet") and "AMD" in why
    assert ni_compare.refusal({"goal": "price of Nvidia stock"}, ONE_THING) is None


def test_a_number_in_the_title_is_not_a_second_thing() -> None:
    assert ni_compare.reasons(["Nvidia", "AMD"], {"rows": [{"name": "Nvidia vs AMD", "value": None}]})


def test_finalize_refuses_a_one_entity_card_for_a_compare_ask(monkeypatch) -> None:
    store = _flow_store()
    item_id = ni_flow.create_shell_item(store, _ask(["Nvidia", "AMD"]))
    spec = _spec(["Nvidia", "AMD"])
    out = ni_flow._finalize(store, item_id, spec, ONE_THING, note="n", born="flow", fetched_at=NOW)
    assert out["state"] == "unsupported" and "can't compare these yet" in str(out)


def test_the_researcher_cannot_land_a_one_entity_card(monkeypatch) -> None:
    names = ["Nvidia", "AMD"]
    ctx, _ = _compare_ctx(monkeypatch, names)
    sealed = tools.propose_compare(ctx, {"metric": "Price", "unit": "USD", "things": [
        {"name": n, "url": _url(n), "path": "quote.price"} for n in names]})["proposal"]
    assert ni_research_flow._can_land(ctx, "item-1", _ask(names), sealed) is None   # the honest compare lands
    one = copy.deepcopy(sealed)   # the same sealed recipe, but the card reads ONE of the two things
    one["spec"].update(source={"type": "http_json", "url": _url("Nvidia")},
                       pipeline=[{"op": "extract", "paths": {"price": "quote.price"}}],
                       scene={"type": "stack", "dir": "v", "gap": "sm", "children": [
                           {"type": "text", "value": "{{price}}", "role": "value", "tone": "default", "size": "md"}]})
    proposal = one
    why = ni_research_flow._can_land(ctx, "item-1", _ask(names), proposal)
    assert why and "can't compare these yet" in why, why
    # the gate line is what refuses it: with the refusal off the same proposal lands
    monkeypatch.setattr(ni_compare, "refusal", lambda *a, **k: None)
    assert ni_research_flow._can_land(ctx, "item-1", _ask(names), proposal) is None


# ---- the refresh re-check ---------------------------------------------------------------------------------

def test_refresh_recheck_notices_a_thing_that_stopped_having_a_number() -> None:
    spec = _spec(["Nvidia", "AMD", "Intel"])
    good = _built(["Nvidia", "AMD", "Intel"])["preview_payload"]
    assert ni_compare.drift(spec, good) == []
    drifted = {"rows": [r for r in good["rows"] if r["name"] != "Intel"]}
    found = ni_compare.drift(spec, drifted)
    assert found and found[0].startswith("compare:") and "Intel" in found[0]
    blank = {"rows": [{**r, "value": None} if r["name"] == "AMD" else r for r in good["rows"]]}
    assert any("AMD" in r for r in ni_compare.drift(spec, blank))


def test_refresh_checks_are_wired_through_the_card_run(monkeypatch) -> None:
    names = ["Nvidia", "AMD"]
    spec = _spec(names)
    found = ni._constraint_drift(spec, {"rows": []}, NOW)
    assert "compare:" in found and "AMD" in found
    assert ni._constraint_drift(spec, _built(names)["preview_payload"], NOW) == ""
    assert ni_refresh.reason_of({"last_status": ni_compare.PARTIAL}) == "source_dead"


# ---- provenance: a composed per-entity address is refused -----------------------------------------------

def _qlib(rows_by_thing: dict):
    class Lib:
        def candidates(self, ask, limit=3, hint=None):
            for name, rows in rows_by_thing.items():
                if name.lower() in ask.lower():
                    return rows[:limit], []
            return [], []
    return Lib()


def _compare_ctx(monkeypatch, names):
    bodies = {_url(n): _body(n) for n in names}
    net = Net(monkeypatch, bodies)
    rows = {n: [{"url": _url(n), "format": "json", "title": f"{n} quote", "provider": "P", "scope": "global",
                 "tier": "reviewed", "needs_key": None, "needs_contact": False}] for n in names}
    ctx = _ctx(_qlib(rows))
    ctx.question.update(kind="compare", entities=list(names), compare_count=len(names), places=[])
    ctx.request = _ask(names)
    for n in names:
        assert tools.library_search(ctx, f"{n} stock price")["ok"]
        assert tools.fetch_sample(ctx, _url(n))["ok"]
    return ctx, net


@pytest.mark.parametrize("names", GROUPS, ids=["two", "three", "four"])
def test_propose_compare_seals_a_card_that_shows_all(monkeypatch, names) -> None:
    ctx, _ = _compare_ctx(monkeypatch, names)
    out = tools.propose_compare(ctx, {"metric": "Price", "unit": "USD", "things": [
        {"name": n.upper(), "url": _url(n), "path": "quote.price"} for n in names]})
    assert out.get("ok"), out
    spec = out["proposal"]["spec"]
    assert [e["name"] for e in ni_compare.sealed(spec)] == names  # the question's spelling, not the model's
    assert "Price" not in spec["title"] and "Price" not in str(spec["scene"])  # nor does the model's metric label it
    assert spec["title"] == ni_compare.title_of(names, "Value")


def test_propose_compare_refuses_a_composed_address(monkeypatch) -> None:
    names = ["Nvidia", "AMD"]
    ctx, _ = _compare_ctx(monkeypatch, names)
    composed = HOST + "/quote/amd?x=1"  # never handed over by the Library or a search
    out = tools.propose_compare(ctx, {"metric": "Price", "unit": "USD", "things": [
        {"name": "Nvidia", "url": _url("Nvidia"), "path": "quote.price"},
        {"name": "AMD", "url": composed, "path": "quote.price"}]})
    assert not out.get("ok") and out["error"] == "provenance"


def test_propose_compare_refuses_the_wrong_things_address(monkeypatch) -> None:
    names = ["Nvidia", "AMD"]
    ctx, _ = _compare_ctx(monkeypatch, names)
    out = tools.propose_compare(ctx, {"metric": "Price", "unit": "USD", "things": [
        {"name": "Nvidia", "url": _url("Nvidia"), "path": "quote.price"},
        {"name": "AMD", "url": _url("Nvidia"), "path": "quote.price"}]})
    assert not out.get("ok")  # one address cannot stand for two things


def test_propose_compare_needs_every_asked_thing(monkeypatch) -> None:
    names = ["Nvidia", "AMD", "Intel"]
    ctx, _ = _compare_ctx(monkeypatch, names)
    out = tools.propose_compare(ctx, {"metric": "Price", "unit": "USD", "things": [
        {"name": "Nvidia", "url": _url("Nvidia"), "path": "quote.price"},
        {"name": "AMD", "url": _url("AMD"), "path": "quote.price"}]})
    assert not out.get("ok") and out["error"] == "invalid_args"


def test_propose_card_cannot_land_a_one_entity_card_for_a_compare_ask(monkeypatch) -> None:
    names = ["Nvidia", "AMD"]
    ctx, _ = _compare_ctx(monkeypatch, names)
    draft = {"version": 1, "title": "Nvidia", "goal": _ask(names), "params": {},
             "source": {"type": "http_json", "url": _url("Nvidia")},
             "pipeline": [{"op": "extract", "paths": {"price": "quote.price"}}],
             "scene": {"type": "stack", "dir": "v", "gap": "sm", "children": [
                 {"type": "text", "value": "{{price}}", "role": "value", "tone": "default", "size": "md"}]},
             "display": {"size": "small"}, "model": None}
    out = tools.propose_card(ctx, draft)
    assert not out.get("ok")


# ---- v2 auto-pick over a Library whose address is filled per thing ---------------------------------------

_RATE_URL = "https://api.rates.example.org/latest?base=USD&quote={q}"


class _PerThingLib:
    """The real fixture Library for answers; ``candidates`` answers per thing with that thing's own address."""

    def __init__(self, real) -> None:
        self.real, self.asked = real, []

    def candidates(self, request, limit=3, hint=None):
        self.asked.append(request)
        code = next((c for c in ("EUR", "GBP", "JPY", "CAD") if c in request.upper()), None)
        if code is None:
            return [], []
        row = {"source_id": "fx-rates", "title": "FX", "host": "api.rates.example.org",
               "url": _RATE_URL.format(q=code), "provider": "P", "authority": "official", "label": "", "choice": False,
               "params": {"quote": code}, "format": "json"}
        return [row], []

    def answers(self, source_id):
        return self.real.answers(source_id)

    def get(self, source_id):
        return self.real.get(source_id)


def _run_flow(library, monkeypatch, request, bodies):
    store = _flow_store()
    wrap = _PerThingLib(library)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: wrap)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    monkeypatch.setattr(ni_research_flow, "fallback", lambda *a, **k: None)
    item_id = ni_flow.create_shell_item(store, request)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(request, "intent"))
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        return copy.deepcopy(bodies[url])
    out = ni_flow._run_external_flow(store, item_id, request, {**_INTENT, "wants": [], "subject": request}, None,
                                     _model, fetch)
    return store, item_id, out, fetched


@pytest.fixture(autouse=True)
def _v2(monkeypatch):
    from smartbrain_3000 import gateway as _gateway
    monkeypatch.setattr(_gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v2")


@pytest.mark.parametrize("codes", [["EUR", "GBP"], ["EUR", "GBP", "JPY"], ["EUR", "GBP", "JPY", "CAD"]],
                         ids=["two", "three", "four"])
def test_auto_pick_builds_a_compare_from_a_parameterised_row(lib, monkeypatch, codes) -> None:  # noqa: F811
    rates = {"EUR": 0.92, "GBP": 0.79, "JPY": 149.5, "CAD": 1.36}
    bodies = {_RATE_URL.format(q=c): {"rates": {c: rates[c]}} for c in codes}
    request = "compare " + ", ".join(codes[:-1]) + " and " + codes[-1] + " exchange rates"
    store, item_id, out, fetched = _run_flow(lib, monkeypatch, request, bodies)
    assert out["state"] == "ready", out
    assert sorted(fetched) == sorted(bodies)
    spec = store.get_item(item_id)["spec"]
    assert ni_compare.is_compare(spec) and [e["name"] for e in ni_compare.sealed(spec)] == codes
    assert spec["scene"]["form"] == "compare"


class _OneAddressLib(_PerThingLib):
    """A Library whose answer carries the thing in the PATH ("rates.{quote}"): every thing has the SAME address."""

    def __init__(self, real, params=None) -> None:
        super().__init__(real)
        self.params = params

    def candidates(self, request, limit=3, hint=None):
        rows, why = super().candidates(request, limit, hint)
        return [{**r, "url": _SHARED_URL, **({"params": self.params} if self.params else {})} for r in rows], why


_SHARED_URL = "https://api.rates.example.org/latest?base=USD"
_SHARED_BODY = {"rates": {"EUR": 0.92, "GBP": 0.79, "JPY": 149.5}}


def _run_shared(lib, monkeypatch, request, params=None):  # noqa: F811
    wrap = _OneAddressLib(lib, params)
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: wrap)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    monkeypatch.setattr(ni_research_flow, "fallback", lambda *a, **k: None)
    store = _flow_store()
    item_id = ni_flow.create_shell_item(store, request)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(request, "intent"))
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        return copy.deepcopy(_SHARED_BODY)
    out = ni_flow._run_external_flow(store, item_id, request, {**_INTENT, "wants": [], "subject": request}, None,
                                     _model, fetch)
    return store, item_id, out, fetched


def test_shared_address_rows_build_one_fetch_two_paths(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, out, fetched = _run_shared(lib, monkeypatch, "compare EUR and GBP exchange rates")
    assert out["state"] == "ready", out
    assert fetched == [_SHARED_URL]
    spec = store.get_item(item_id)["spec"]
    assert [e["name"] for e in ni_compare.sealed(spec)] == ["EUR", "GBP"]
    assert len({e["path"] for e in ni_compare.sealed(spec)}) == 2


@pytest.mark.parametrize("params", [None, {"quote": "EUR"}], ids=["path-per-thing", "same-reading"])
def test_shared_address_rows_end_ready_or_unsupported_never_failed(lib, monkeypatch, params) -> None:  # noqa: F811
    _store_, _id, out, _ = _run_shared(lib, monkeypatch, "compare EUR and GBP exchange rates", params)
    assert out["state"] in ("ready", "unsupported"), out
    if params:
        assert out["state"] == "unsupported" and "can't compare these yet" in str(out)


def test_a_long_token_in_the_ask_never_fails_the_item(lib, monkeypatch) -> None:  # noqa: F811
    request = "compare EUR and GBP exchange rates " + "x" * 300
    bodies = {_RATE_URL.format(q=c): {"rates": {c: 1.0}} for c in ("EUR", "GBP")}
    _s, _i, out, _f = _run_flow(lib, monkeypatch, request, bodies)
    assert out["state"] in ("ready", "unsupported"), out
    assert ni_compare_flow.metric_for(request, ("EUR", "GBP"), [])[0] is None   # no raise on its own either


def test_a_seal_error_in_one_source_is_that_sources_reason_not_a_crash(lib, monkeypatch) -> None:  # noqa: F811
    def boom(*a, **k):
        raise ValueError("seal refused")
    monkeypatch.setattr(ni_flow, "build_final_spec", boom)
    bodies = {_RATE_URL.format(q=c): {"rates": {c: 1.0}} for c in ("EUR", "GBP")}
    _s, _i, out, _f = _run_flow(lib, monkeypatch, "compare EUR and GBP exchange rates", bodies)
    assert out["state"] == "unsupported", out


def test_auto_pick_declines_honestly_when_no_source_serves_every_thing(lib, monkeypatch) -> None:  # noqa: F811
    request = "compare EUR and Zorblax exchange rates"
    store, item_id, out, fetched = _run_flow(lib, monkeypatch, request, {})
    assert fetched == []
    assert out["state"] == "unsupported" and "can't compare these yet" in str(out)
    assert store.get_item(item_id)["spec"].get("_shell") is True  # no one-thing card was built


def test_auto_pick_declines_when_one_thing_cannot_be_read(lib, monkeypatch) -> None:  # noqa: F811
    bodies = {_RATE_URL.format(q="EUR"): {"rates": {"EUR": 0.92}}, _RATE_URL.format(q="GBP"): {"rates": {}}}
    store, item_id, out, _ = _run_flow(lib, monkeypatch, "compare EUR and GBP exchange rates", bodies)
    assert out["state"] == "unsupported" and "GBP" in str(out)


def test_more_than_four_things_decline(lib, monkeypatch) -> None:  # noqa: F811
    request = "compare Nvidia, AMD, Intel, Qualcomm and Apple stock prices"
    store, item_id, out, _ = _run_flow(lib, monkeypatch, request, {})
    assert out["state"] == "unsupported" and "up to 4" in str(out)


def test_metric_for_skips_a_measure_the_source_does_not_report() -> None:
    answers = [{**_value("rate", "Exchange rate", "rates.{quote}", ["rate"], primary=True), "window": None,
                "codes": None}]
    answers[0].pop("codes")
    got, why = ni_compare_flow.metric_for("compare EUR and GBP rate", ("EUR", "GBP"), answers)
    assert got is not None and not why
    got, why = ni_compare_flow.metric_for("compare EUR and GBP humidity", ("EUR", "GBP"), answers)
    assert got is None and why
    got, why = ni_compare_flow.metric_for("compare EUR and GBP rate tomorrow", ("EUR", "GBP"), answers)
    assert got is None and why
