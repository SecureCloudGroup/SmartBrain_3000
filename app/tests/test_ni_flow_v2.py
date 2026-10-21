"""The ``ni_flow = v1|v2`` flag: env wins, then meta, default v1, garbage reads as v1."""

from __future__ import annotations

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow
from smartbrain_3000.secrets import gen_master_key


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.delenv("SMARTBRAIN_NI_FLOW", raising=False)
    c = duckdb.connect(":memory:")
    dbmod.run_migrations(c)
    return c


def test_default_is_v1(conn):
    assert ni_flow.flow_mode(conn) == "v1"
    assert ni_flow._is_v2(nimod.NIStore(conn, gen_master_key())) is False


def test_meta_selects_v2(conn):
    dbmod.meta_set(conn, "ni_flow", "v2")
    assert ni_flow.flow_mode(conn) == "v2"
    assert ni_flow._is_v2(nimod.NIStore(conn, gen_master_key())) is True


def test_env_wins_over_meta(conn, monkeypatch):
    dbmod.meta_set(conn, "ni_flow", "v2")
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v1")
    assert ni_flow.flow_mode(conn) == "v1"
    dbmod.meta_set(conn, "ni_flow", "v1")
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", " V2 ")
    assert ni_flow.flow_mode(conn) == "v2"


@pytest.mark.parametrize("junk", ["v3", "2", "true", "", "v2; drop"])
def test_garbage_reads_as_v1(conn, monkeypatch, junk):
    dbmod.meta_set(conn, "ni_flow", junk)
    assert ni_flow.flow_mode(conn) == "v1"
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", junk or "x")
    assert ni_flow.flow_mode(conn) == "v1"
