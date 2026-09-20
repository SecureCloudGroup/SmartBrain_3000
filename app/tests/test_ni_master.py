"""NI Master (G1) — the execution-plane skeleton's laws, unit-tested.

The load-bearing test is the no-dead-end PROPERTY: for every terminal state ×
error class × shell combination, ``terminal_surface`` must yield a reason and
a question-or-reopen. The four 2026-09-17 field confusions were all instances
of this law not existing.
"""

from __future__ import annotations

import pytest

from smartbrain_3000 import ni_master

# --- ledger ----------------------------------------------------------------

def test_ledger_appends_bounded_entries_with_part_mapping() -> None:
    record: dict = {}
    ni_master.ledger_append(record, "sampling", "entered")
    ni_master.ledger_append(record, "failed", "failed",
                            error_class="fetch", decision="user retried")
    entries = record["_ledger"]
    assert len(entries) == 2
    assert entries[0]["part"] == "acquire", "stage names map to part anatomy"
    assert entries[1]["part"] == "park" and entries[1]["error_class"] == "fetch"
    assert entries[1]["decision"] == "user retried"
    assert all("t" in e for e in entries)


def test_ledger_is_bounded() -> None:
    record: dict = {}
    for i in range(300):
        ni_master.ledger_append(record, "intent", f"entered-{i}")
    assert len(record["_ledger"]) == ni_master._MAX_LEDGER
    assert record["_ledger"][-1]["outcome"] == "entered-299", "newest kept"


def test_ledger_truncates_oversize_fields() -> None:
    record: dict = {}
    ni_master.ledger_append(record, "mapping", "x" * 500,
                            evidence="e" * 500, decision="d" * 500)
    entry = record["_ledger"][0]
    assert len(entry["outcome"]) == 60
    assert len(entry["evidence"]) == ni_master._MAX_EVIDENCE
    assert len(entry["decision"]) == 60


# --- error classes ----------------------------------------------------------

@pytest.mark.parametrize("error,klass", [
    ("fetch: sample fetch failed: HTTPError", "fetch"),
    ("mapping: no candidate matched", "mapping"),
    ("declined: user declined the source on the card", "declined"),
    ("computed-only requires an explicit YYYY-MM-DD date in the request",
     "computed-only"),
    ("stale: worker died", "stale"),
    (None, "none"),
    ("", "none"),
])
def test_error_class_of(error, klass) -> None:
    assert ni_master.error_class_of(error) == klass


# --- the no-dead-end law (property over the whole input space) --------------

@pytest.mark.parametrize("state", ["failed", "unsupported"])
@pytest.mark.parametrize("shell", [True, False])
@pytest.mark.parametrize("error", [
    None, "", "fetch: down", "mapping: no match", "assembling: bind failed",
    "declined: user declined", "stale: worker died",
    "computed-only requires an explicit YYYY-MM-DD date in the request",
    "totally novel failure text nobody anticipated",
])
def test_terminal_surface_never_dead_ends(state, shell, error) -> None:
    """EVERY terminal surface carries a reason and a question or reopen —
    including error classes that do not exist yet (the law backstop)."""
    record = {"state": state}
    if error is not None:
        record["error"] = error
    out = ni_master.terminal_surface(state, record, shell=shell)
    assert out["reason"], f"no reason for {state}/{error!r}"
    assert out.get("question") or out.get("reopen"), \
        f"dead end for {state}/{error!r}/shell={shell}"
    for r in out.get("reopen", []):
        assert r in ni_master.REOPEN_KINDS


def test_terminal_surface_failed_fetch_shell_offers_both_roads() -> None:
    out = ni_master.terminal_surface(
        "failed", {"state": "failed", "error": "fetch: down"}, shell=True)
    assert set(out["reopen"]) == {"retry", "pick_source"}


def test_terminal_surface_declined_asks_pick_source() -> None:
    out = ni_master.terminal_surface(
        "failed", {"state": "failed", "error": "declined: user declined"},
        shell=True)
    assert out["question"] == {"kind": "pick_source"}


def test_terminal_surface_honors_stamped_question_with_prompt() -> None:
    record = {"state": "unsupported",
              "error": "computed-only requires an explicit YYYY-MM-DD date in the request",
              "_question": {"kind": "supply_date",
                             "prompt": "When is it? Add the date as YYYY-MM-DD."}}
    out = ni_master.terminal_surface("unsupported", record, shell=True)
    assert out["question"]["kind"] == "supply_date"
    assert "YYYY-MM-DD" in out["question"]["prompt"]
    assert "date" in out["reason"].lower()


def test_terminal_surface_rejects_unknown_stamped_kind() -> None:
    """A stamped question outside the closed kinds never reaches the card."""
    record = {"state": "unsupported", "error": "x",
              "_question": {"kind": "run_arbitrary_code"}}
    out = ni_master.terminal_surface("unsupported", record, shell=True)
    assert out.get("question") is None
    assert out["reopen"], "law backstop still applies"


# --- questions for paused states ---------------------------------------------

@pytest.mark.parametrize("state,kind", [
    ("source", "pick_source"),
    ("confirm_source", "approve_source"),
    ("awaiting_credential", "add_key"),
    ("awaiting_params", "fill_params"),
])
def test_question_for_paused_states(state, kind) -> None:
    assert ni_master.question_for(state, {})["kind"] == kind


def test_choose_move_is_total_over_moves() -> None:
    """Every decision the table returns names a registered move."""
    for state in ("failed", "unsupported", "sampling"):
        for klass in ("fetch", "declined", "computed-only", "novel", "none"):
            for shell in (True, False):
                move = ni_master.choose_move(state=state, error_class=klass,
                                             shell=shell)
                assert move["move"] in ni_master.MOVES
