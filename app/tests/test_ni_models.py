"""The per-step model map (ni_models): defaults, overrides, the local-only summary, research's older switch,
the cost view's per-step rows and the build's cached system prompt."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db, gateway, ni_flow, ni_models, usage
from smartbrain_3000.ni_research import policy

_BOTH = {"ni": "anthropic/claude-sonnet", "chat": "mlx/qwen-local"}


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _conn(tmp_path):
    conn = db.open_db(tmp_path / "m.duckdb")
    db.run_migrations(conn)
    return conn


@pytest.fixture(autouse=True)
def _no_env(monkeypatch) -> None:
    for name in ("SMARTBRAIN_NI_RESEARCH", "SMARTBRAIN_NI_RESEARCH_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_auto_runs_one_time_steps_on_cloud_and_repeated_steps_locally(tmp_path) -> None:
    conn = _conn(tmp_path)
    picks = {s: ni_models.choose(conn, s, _BOTH) for s in ni_models.STEPS}
    assert {s: (c.where, c.model) for s, c in picks.items()} == {
        "build": ("cloud", "anthropic/claude-sonnet"), "research": ("cloud", "anthropic/claude-sonnet"),
        "refresh": ("local", "mlx/qwen-local"), "summary": ("local", "mlx/qwen-local")}


def test_auto_falls_back_to_the_other_side(tmp_path) -> None:
    conn = _conn(tmp_path)
    assert ni_models.choose(conn, "build", {"chat": "mlx/qwen-local"}).model == "mlx/qwen-local"
    assert ni_models.choose(conn, "refresh", {"chat": "anthropic/claude-sonnet"}).model == "anthropic/claude-sonnet"


def test_summary_never_resolves_to_cloud(tmp_path) -> None:
    conn = _conn(tmp_path)
    c = ni_models.choose(conn, "summary", {"chat": "anthropic/claude-sonnet"})
    assert c.model is None and c.cloud is None
    with pytest.raises(ValueError):
        ni_models.save(conn, {"summary": "cloud"})
    db.meta_set(conn, "ni_step_models", '{"summary": "cloud"}')  # a hand-edited store still reads local
    assert ni_models.setting(conn, "summary") == "local"
    assert ni_models.choose(conn, "summary", _BOTH).model == "mlx/qwen-local"


def test_override_per_step(tmp_path) -> None:
    conn = _conn(tmp_path)
    assert ni_models.save(conn, {"build": "local", "refresh": "cloud"})["build"] == "local"
    assert ni_models.choose(conn, "build", _BOTH).model == "mlx/qwen-local"
    assert ni_models.choose(conn, "refresh", _BOTH).model == "anthropic/claude-sonnet"
    # a forced side with nothing routed there is None, never the other side
    assert ni_models.choose(conn, "build", {"ni": "anthropic/claude-sonnet"}).model is None
    with pytest.raises(ValueError):
        ni_models.save(conn, {"verify": "local"})
    with pytest.raises(ValueError):
        ni_models.save(conn, {"build": "fast"})


def test_cloud_model_is_anthropic_only(tmp_path) -> None:
    conn = _conn(tmp_path)
    assert ni_models.cloud_model(conn, {"ni": "openai/gpt-4o", "chat": "claudecode/sonnet"}) is None
    db.meta_set(conn, "ni_research_model", "anthropic/claude-opus")
    assert ni_models.cloud_model(conn, _BOTH) == "anthropic/claude-opus"


def test_research_reads_its_step_and_keeps_the_older_switch(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    db.meta_set(conn, "ni_research", "off")
    assert policy.setting(conn) == "off"           # the older switch, step unset
    ni_models.save(conn, {"research": "local"})
    assert policy.setting(conn) == "local"         # the step wins over the older meta
    monkeypatch.setenv("SMARTBRAIN_NI_RESEARCH", "cloud")
    assert policy.setting(conn) == "cloud"         # the env still wins over everything


def test_build_model_follows_the_step(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    store = type("S", (), {"conn": conn})()
    monkeypatch.setattr(gateway, "load_routes", lambda c: dict(_BOTH))
    assert ni_flow._resolve_flow_model(store) == "anthropic/claude-sonnet"
    ni_models.save(conn, {"build": "local"})
    assert ni_flow._resolve_flow_model(store) == "mlx/qwen-local"


def test_build_forced_side_is_honoured_exactly(tmp_path, monkeypatch) -> None:
    """build=cloud with no Anthropic route stops (None), never the older order's openai or local pick; auto keeps it."""
    conn = _conn(tmp_path)
    store = type("S", (), {"conn": conn})()
    monkeypatch.setattr(gateway, "load_routes", lambda c: {"ni": "openai/gpt-4o", "chat": "mlx/qwen-local"})
    assert ni_flow._resolve_flow_model(store) == "openai/gpt-4o"   # auto, no Anthropic route: P0 order
    ni_models.save(conn, {"build": "cloud"})
    assert ni_flow._resolve_flow_model(store) is None
    monkeypatch.setattr(gateway, "load_routes", lambda c: {"chat": "anthropic/claude-sonnet"})
    ni_models.save(conn, {"build": "local"})
    assert ni_flow._resolve_flow_model(store) is None


def test_build_with_nothing_routed_fails_as_model_not_intent(tmp_path, monkeypatch) -> None:
    """Live 2026-10-26: the build step forced to Local on an install whose ni / chat / agent routes are all
    Claude Code. The flow stops before any model call and the card must say so (class ``model``), not call the
    request hard to understand (class ``intent``)."""
    from smartbrain_3000 import ni, ni_master

    monkeypatch.setattr(gateway, "load_routes",
                        lambda c: {"ni": "claudecode/opus", "chat": "claudecode/sonnet", "agent": "claudecode/sonnet"})
    conn = _conn(tmp_path)
    ni_models.save(conn, {"build": "local"})
    store = ni.NIStore(conn, b"\x00" * 32)
    item_id = ni_flow.create_shell_item(store, "show me the price of NVDA every 15 minutes")
    calls: list[str] = []
    result = ni_flow.run_flow(store, item_id, gateway_call=lambda m, p: calls.append(m) or "{}",
                              fetcher=lambda url: {})
    assert result["state"] == "failed" and calls == []
    assert ni_master.error_class_of(result["error"]) == "model"
    assert ni_master.reason_for("failed", result["error"]).startswith("No model is routed for building cards")


def test_saving_auto_leaves_research_off_switch_alone(tmp_path, monkeypatch) -> None:
    """The page sends every step on Save: auto entries are unset, so an older ``ni_research=off`` stays off."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(gateway, "load_routes", lambda c: dict(_BOTH))
    db.meta_set(conn, "ni_research", "off")
    ni_models.save(conn, {s: "auto" for s in ni_models.STEPS} | {"build": "local"})
    assert policy.setting(conn) == "off"
    assert "research" not in ni_models.stored(conn)
    research = next(r for r in ni_models.describe(conn) if r["step"] == "research")
    assert research["off"] is True and research["model"] is None
    ni_models.save(conn, {"research": "cloud"})   # an explicit choice turns it back on
    assert policy.setting(conn) == "cloud"


def test_cache_system_marks_only_the_system_prompt() -> None:
    msgs = [{"role": "system", "content": "RULES"}, {"role": "user", "content": "ask"}]
    out = ni_flow._cache_system(msgs)
    assert out[0]["content"] == [{"type": "text", "text": "RULES", "cache": True}] and out[1] is msgs[1]
    wire = gateway._wire_messages(out, "anthropic/claude-sonnet")
    assert wire[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert gateway._wire_messages(out, "mlx/qwen-local")[0]["content"] == "RULES"  # byte-identical off Anthropic


def test_usage_rows_carry_the_step(tmp_path) -> None:
    conn = _conn(tmp_path)
    reply = {"usage": {"prompt_tokens": 100, "completion_tokens": 10}}
    ni_models.record(conn, "build", "anthropic/claude-sonnet", reply)
    ni_models.record(conn, "refresh", "mlx/qwen-local", reply)
    ni_models.record(conn, "refresh", "mlx/qwen-local", reply)
    usage.record_response(conn, "mlx/qwen-local", reply, purpose="chat")
    rows = {(r["purpose"], r["model"]): r["calls"] for r in usage.by_purpose(conn, "ni.")}
    assert rows == {("ni.build", "anthropic/claude-sonnet"): 1, ("ni.refresh", "mlx/qwen-local"): 2}


def test_step_models_endpoint_and_cost_view(client: TestClient, monkeypatch) -> None:
    assert client.get("/api/ni/step-models").status_code == 423
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "load_routes", lambda c: dict(_BOTH))
    steps = {s["step"]: s for s in client.get("/api/ni/step-models").json()["steps"]}
    assert steps["build"]["where"] == "cloud" and steps["summary"]["options"] == ["auto", "local"]
    assert client.put("/api/ni/step-models", json={"steps": {"summary": "cloud"}}).status_code == 400
    r = client.put("/api/ni/step-models", json={"steps": {"build": "local"}}).json()
    assert {s["step"]: s["model"] for s in r["steps"]}["build"] == "mlx/qwen-local"
    conn = client.app.state.dbx
    ni_models.record(conn, "build", "anthropic/claude-sonnet",
                     {"usage": {"prompt_tokens": 1000, "completion_tokens": 100}})
    monkeypatch.setattr(gateway, "list_models", lambda: [
        {"id": "anthropic/claude-sonnet", "pricing": {"prompt": 3e-6, "completion": 1.5e-5}}])
    u = client.get("/api/usage").json()
    (row,) = u["ni_steps"]
    assert row["step"] == "build" and row["model"] == "anthropic/claude-sonnet" and row["local"] is False
    assert abs(row["cost"] - (1000 * 3e-6 + 100 * 1.5e-5)) < 1e-12
