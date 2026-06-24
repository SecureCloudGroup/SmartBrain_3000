"""Tests for encrypted memory + identity and server-side chat injection (H2)."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway
from smartbrain_3000.memory import MemoryStore
from smartbrain_3000.secrets import gen_master_key


def _mem(master_key: bytes | None = None) -> MemoryStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return MemoryStore(conn, master_key or gen_master_key())


# --- storage layer --------------------------------------------------------

def test_add_list_delete_memory() -> None:
    m = _mem()
    mid = m.add_memory("I am vegetarian")
    assert [x["text"] for x in m.list_memories()] == ["I am vegetarian"]
    m.delete_memory(mid)
    assert m.list_memories() == []


def test_profile_defaults_and_roundtrip() -> None:
    m = _mem()
    assert m.get_profile() == {"assistant_name": "", "user_name": "", "instructions": ""}
    m.set_profile("Jarvis", "Alex", "Be concise.")
    assert m.get_profile() == {"assistant_name": "Jarvis", "user_name": "Alex", "instructions": "Be concise."}
    m.set_profile("Friday", "Sam", "")  # replaces the singleton
    assert m.get_profile()["assistant_name"] == "Friday"


def test_system_prompt_none_when_empty() -> None:
    assert _mem().system_prompt() is None


def test_system_prompt_composition() -> None:
    m = _mem()
    m.set_profile("Jarvis", "Alex", "Always be concise.")
    m.add_memory("Timezone is Pacific")
    sp = m.system_prompt()
    assert "You are Jarvis, a personal assistant for Alex." in sp
    assert "Always be concise." in sp
    assert "- Timezone is Pacific" in sp


def test_memory_encrypted_at_rest_and_wrong_key() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    m = MemoryStore(conn, gen_master_key())
    m.add_memory("super-secret-fact")
    m.set_profile("", "", "secret-instruction")
    raw = b"".join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM memories;").fetchall())
    raw += b"".join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM profile;").fetchall())
    assert b"super-secret-fact" not in raw and b"secret-instruction" not in raw
    with pytest.raises(Exception):
        MemoryStore(conn, gen_master_key()).list_memories()


def test_profile_aad_rejects_memory_ciphertext() -> None:
    # A memory ciphertext must not authenticate as the profile (distinct AAD).
    from cryptography.exceptions import InvalidTag

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    m = MemoryStore(conn, gen_master_key())
    m.add_memory("fact")
    row = conn.execute("SELECT nonce, ciphertext FROM memories;").fetchone()
    conn.execute("INSERT INTO profile (id, nonce, ciphertext) VALUES (1, ?, ?);", [bytes(row[0]), bytes(row[1])])
    with pytest.raises(InvalidTag):
        m.get_profile()


# --- HTTP API + chat injection --------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_memory_api_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/memories").status_code == 423
    assert client.get("/api/profile").status_code == 423


def test_memory_and_profile_api(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    mid = client.post("/api/memories", json={"text": "Likes tea"}).json()["id"]
    assert client.get("/api/memories").json()["memories"][0]["text"] == "Likes tea"
    client.put("/api/profile", json={"assistant_name": "Jarvis", "user_name": "Alex", "instructions": "Be brief."})
    assert client.get("/api/profile").json()["assistant_name"] == "Jarvis"
    assert client.delete(f"/api/memories/{mid}").json() == {"ok": True}
    assert client.get("/api/memories").json()["memories"] == []


def test_chat_injects_memory_system_prompt(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/memories", json={"text": "Allergic to peanuts"})
    client.put("/api/profile", json={"assistant_name": "Jarvis", "user_name": "Alex", "instructions": ""})
    seen: dict = {}
    monkeypatch.setattr(gateway, "chat", lambda messages, model: seen.update(messages=messages) or {"choices": []})
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"})
    assert seen["messages"][0]["role"] == "system"
    assert "Allergic to peanuts" in seen["messages"][0]["content"]
    assert "Jarvis" in seen["messages"][0]["content"]
    assert seen["messages"][1] == {"role": "user", "content": "hi"}


def test_chat_injects_base_prompt_without_memory(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    seen: dict = {}
    monkeypatch.setattr(gateway, "chat", lambda messages, model: seen.update(messages=messages) or {"choices": []})
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"})
    # Base grounding (current time + tool guidance) is injected even with no profile/facts.
    content = seen["messages"][0]["content"]
    assert seen["messages"][0]["role"] == "system"
    assert "current date and time" in content
    # Trust-critical: the model must be told to actually call a tool for actions, never
    # to narrate a state change it didn't perform (the "claimed task added" failure mode).
    assert "MUST emit the matching tool call" in content
    assert seen["messages"][1] == {"role": "user", "content": "hi"}


def test_chat_respects_caller_system_message(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/memories", json={"text": "fact"})
    seen: dict = {}
    monkeypatch.setattr(gateway, "chat", lambda messages, model: seen.update(messages=messages) or {"choices": []})
    body = {"messages": [{"role": "system", "content": "caller"}, {"role": "user", "content": "hi"}], "capability": "fast_chat"}
    client.post("/api/chat", json=body)
    assert [m["content"] for m in seen["messages"]] == ["caller", "hi"]  # no double system message
