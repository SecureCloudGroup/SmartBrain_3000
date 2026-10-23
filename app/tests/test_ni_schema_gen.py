"""S6b: the generated NI wire schema and the type files cut from it stay in step with the Python sources."""
from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ni_recipe import _recipe

from smartbrain_3000 import ni_flow, ni_master

_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "gen-ni-schema.py"
# the app image ships app/ only: the generator (and web/, where the TS file lives) are repo-only
pytestmark = pytest.mark.skipif(not _SCRIPT.exists(), reason="gen-ni-schema tool not shipped in the app image")
gen = None
if _SCRIPT.exists():
    _spec = importlib.util.spec_from_file_location("gen_ni_schema", _SCRIPT)
    gen = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(gen)


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _errors(value: object, name: str) -> list[str]:
    schema = json.loads(gen.OUT_SCHEMA.read_text())
    return gen.validate(value, {"$ref": f"#/$defs/{name}"}, schema)


def test_committed_files_match_the_sources() -> None:
    assert gen.main(["--check"]) == 0, "run tools/gen-ni-schema.py and commit the generated files"


def test_vocabularies_come_from_the_python_constants() -> None:
    defs = gen.build_schema()["$defs"]
    assert set(defs["ActionKind"]["enum"]) == set(ni_master.ACTION_KINDS)
    assert set(defs["FlowState"]["enum"]) == set(ni_flow.FLOW_STATES)
    assert set(defs["QualityTier"]["enum"]) == {"curated", "verified_endpoint", "page_reading"}
    assert "ActionKind" in gen.OUT_TS.read_text() and "ActionKind" in gen.OUT_PY.read_text()


def test_check_fails_on_a_stale_file_and_write_repairs_it(tmp_path, monkeypatch, capsys) -> None:
    for name in ("OUT_SCHEMA", "OUT_PY", "OUT_TS"):
        copy = tmp_path / getattr(gen, name).name
        copy.write_text(getattr(gen, name).read_text())
        monkeypatch.setattr(gen, name, copy)
    assert gen.main(["--check"]) == 0
    gen.OUT_TS.write_text(gen.OUT_TS.read_text().replace('"retry", ', ""))
    assert gen.main(["--check"]) == 1
    assert "types.gen.ts" in capsys.readouterr().out
    assert gen.main([]) == 0 and gen.main(["--check"]) == 0
    gen.OUT_PY.unlink()
    assert gen.main(["--check"]) == 1


def test_real_v2_board_rows_and_the_poll_validate(client) -> None:
    from test_ni_yes_gate import _fetching_preview, _fetching_scene_spec

    store = client.app.state.ni
    building = ni_flow.create_shell_item(store, "river level in Boise")
    ni_flow._transition(store, building, "sampling", _try={"n": 2, "of": 3, "host": "api.example.org"})
    live = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(live, "live")
    rows = client.get("/api/ni/board").json()["items"]
    assert {r["id"] for r in rows} == {building, live}
    for row in rows:
        assert _errors(row, "BoardRow") == [], row["id"]
        assert _errors(client.get(f"/api/ni/items/{row['id']}/flow").json(), "FlowPoll") == []
    assert next(r for r in rows if r["id"] == building)["flow"]["progress"]["try"]["n"] == 2


def test_the_schema_rejects_drift_in_a_row() -> None:
    row = {"id": "x", "state": "live", "actions": ["retry"]}
    assert any("missing" in e for e in _errors(row, "BoardRow"))
    flow = {"state": "sampling", "actions": ["teleport"]}
    assert _errors(flow, "Flow") != [] and _errors({"state": "sampling", "extra": 1}, "Flow") != []
    assert _errors({"state": "sampling", "actions": ["cancel"]}, "Flow") == []


def test_the_recipe_block_validates_and_bad_ones_are_refused() -> None:
    assert _errors(_recipe(), "Recipe") == []
    assert _errors({k: v for k, v in _recipe().items() if k != "tier"}, "Recipe") != []
    for bad in (lambda r: r.update(tier="gold"), lambda r: r.update(sources=[]),
                lambda r: r["question"].update(limit=101), lambda r: r["question"].update(kind="vibes"),
                lambda r: r["provenance"].update(author="model"), lambda r: r["sources"][0].update(why="x" * 201)):
        r = _recipe()
        bad(r)
        assert _errors(r, "Recipe") != []
