"""S2 shadow numbers on the recorded blind-50 (2026-10-05): the new constraint classes object to none of
the 13 cards a human read as right and to each of the 4 read as wrong (tests/ni_eval/shadow_replay.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).parent / "ni_eval" / "shadow_replay.py"
_SPEC = importlib.util.spec_from_file_location("ni_shadow_replay", _PATH)
assert _SPEC is not None and _SPEC.loader is not None, "shadow_replay is importable"
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)
ROWS = _MOD.blind50_rows()


def test_no_false_reject_on_the_right_cards():
    right = [r for r in ROWS if r["verdict"] == "right"]
    assert len(right) == 13, "the blind-50 has 13 right cards"
    assert [r["ask"] for r in right if r["reasons"]] == []


def test_each_wrong_card_gets_a_reason_of_its_own_class():
    wrong = {r["ask"]: set(r["reasons"]) for r in ROWS if r["verdict"] == "wrong"}
    assert wrong == {
        "NHL games tonight": {"window"},
        "how many earthquakes above 4.0 today": {"threshold"},
        "weather in Denver vs Salt Lake City": {"compare"},
        "how many active hurricanes in the Pacific": {"scope"},
    }
