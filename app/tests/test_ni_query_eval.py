"""Round 20 Q3a: tools/ni-query-eval.py's deterministic modes — the recorded Q2 model replies
through the module's normalization reproduce the lead's re-score (50/60 few-shot TEST, 54/82
zero-shot ALL, the same misses), the prompts are byte-identical to the measured ones, and the
gold IRs execute to the harness's own rows on every recorded sample.

The shipped app image omits the repo's tools/ tree: a missing script SKIPS this file (the
pattern of test_ni_flow_eval_plumbing)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_EVAL_PATH = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-query-eval.py"

pytestmark = pytest.mark.skipif(not _EVAL_PATH.exists(), reason="ni-query-eval.py tooling not shipped in the app image")

V1_FEW_MISSES = ["A5", "B5", "B22", "C11", "D10", "D21", "D30", "E13", "E26", "E37"]
V1_ZERO_MISSES = ["A3", "A5", "A6", "A17", "A25", "A29", "A30", "B5", "B14", "B18", "B22", "B24", "B40", "C9", "C11",
                  "C18", "D1", "D3", "D10", "D17", "E3", "E7", "E18", "E22", "E26", "E33", "E37", "E38"]


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("_ni_query_eval", _EVAL_PATH)
    assert spec is not None and spec.loader is not None, "spec load must succeed"
    module = importlib.util.module_from_spec(spec)
    sys.modules["_ni_query_eval"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def data(ev):
    return ev.load()


def _scores(ev, data, key: str, scorer: str) -> dict:
    test = set(data["split"]["test"])
    out = {}
    for rid, rec in data[key].items():
        g = data["gold"][rid]
        if g["note"] or (key == "few" and rid not in test):
            continue
        ctx = ev.row_ctx(data, g)
        if scorer == "v1":
            out[rid] = ev.v1_score(g, rec["parsed"], ctx)
        else:
            out[rid] = ev.full_score(g, ev.finalize_reply(rec["parsed"], ctx)[0], ctx)
    return out


def test_v1_contract_reproduces_the_rescore(ev, data):
    few, zero = _scores(ev, data, "few", "v1"), _scores(ev, data, "zero", "v1")
    assert (sum(map(ev.fully, few.values())), len(few)) == (50, 60)
    assert (sum(map(ev.fully, zero.values())), len(zero)) == (54, 82)
    assert sorted(i for i, s in few.items() if not ev.fully(s)) == sorted(V1_FEW_MISSES)
    assert sorted(i for i, s in zero.items() if not ev.fully(s)) == sorted(V1_ZERO_MISSES)


def test_full_contract_adds_the_number_validator(ev, data):
    """The stated-number validator turns the unstated numeric filters right (E26 few-shot; B24, D10,
    E7, E26 zero-shot) and nothing wrong."""
    few, zero = _scores(ev, data, "few", "full"), _scores(ev, data, "zero", "full")
    assert sum(map(ev.fully, few.values())) == 51 and sum(map(ev.fully, zero.values())) == 58
    assert {i for i, s in few.items() if ev.fully(s)} >= {i for i in few if i not in V1_FEW_MISSES}, "none turns wrong"
    assert {i for i, s in zero.items() if ev.fully(s)} >= {i for i in zero if i not in V1_ZERO_MISSES}


def test_prompts_are_the_measured_ones(ev, data):
    assert ev.prompt_parity(data) == 0


def test_execution_port_check(ev, data):
    assert ev.exec_mode(data) == 0
