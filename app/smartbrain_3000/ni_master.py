"""NI Master — the execution-plane skeleton (design rounds 7-8, wave G1).

Two planes govern Neural Interface items:

- **Execution plane** (this module + ni_flow): for any want, compose a plan
  over general components and execute it deterministically. In G1 the plan is
  still the fixed default sequence (the shipped ni_flow stages, wrapped as
  registered parts); PLAN/LOCATE/JUDGE interiors arrive in later waves. What
  G1 establishes is the *contract skeleton*: the closed move set, the
  append-only ledger, and the single-writer law for card copy.
- **Oversight plane** (ni_watch): standing watchers over ledgers/logs/feedback.

Three laws this module owns:

1. **Ledger** — every part transition and failure appends a bounded entry to
   the flow record's ``_ledger`` (sealed with the record; underscore keys ride
   ``ni_flow._transition`` carry-forward). The ledger is the master's decision
   input, the card's copy source, and the gates' assertion artifact.
2. **No dead ends** — ``choose_move`` maps every failure class to a move from
   the closed set; a terminal state must surface a user-facing ``reason`` and
   at least one reopen affordance, or a question. ``derive_card_state`` is
   where that law is enforced (and ni_watch flags any record that violates it).
3. **Single writer** — the board's user-facing flow copy (reason sentences,
   questions, reopen affordances) is derived HERE, not in scattered frontend
   conditionals. ``ni_flow.board_flow_field`` delegates to this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Closed move set (round 7). ``research`` is registered now, chosen from G3.
MOVES: frozenset[str] = frozenset({
    "advance", "retry", "reroute", "ask_user", "research", "degrade",
    "finish", "park",
})

# The ten general components (round 7). G1 wraps the shipped stages as the
# parts marked with an adapter; the rest are registered contracts whose
# interiors land in G2 (judge/shape), G3 (plan/locate), G4 (sustain.refine).
PARTS: tuple[str, ...] = (
    "understand", "plan", "locate", "authorize", "acquire",
    "shape", "judge", "present", "prove", "sustain",
)

# ni_flow stage name -> part name (ledger vocabulary; the flow's states remain
# the user-visible phases, the parts are the architecture's anatomy).
STAGE_PART: dict[str, str] = {
    "intent": "understand",
    "source": "locate",
    "confirm_source": "authorize",
    "sampling": "acquire",
    "mapping": "shape",
    "assembling": "present",
    "awaiting_credential": "authorize",
    "awaiting_params": "authorize",
    "ready": "prove",
    "failed": "park",
    "unsupported": "park",
}

# Closed question kinds a paused/terminal card may ask. Each maps to one card
# affordance the frontend already has or gains in G1.
QUESTION_KINDS: frozenset[str] = frozenset({
    "pick_source",     # source pause: vetted suggestions + paste-a-URL
    "approve_source",  # confirm pause: Approve / Not this source
    "add_key",         # awaiting_credential: credential PUT
    "fill_params",     # awaiting_params: Fill modal
    "supply_date",     # computed ask without a literal date (G1 new)
    "validate",        # commissioning C2: Looks right / Something's wrong
})

# Closed reopen affordances for terminal records. "retry" re-runs the sealed
# request; "pick_source" re-enters the source pick; "delete" always exists on
# the card chrome and is listed only when it is the honest primary way out.
REOPEN_KINDS: frozenset[str] = frozenset({"retry", "pick_source", "delete"})

_MAX_LEDGER = 120
_MAX_EVIDENCE = 200


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ledger_append(record: dict, part: str, outcome: str, *,
                  error_class: str | None = None,
                  evidence: str | None = None,
                  decision: str | None = None) -> None:
    """Append one bounded entry to the record's ``_ledger`` (in place).

    ``part`` is a PARTS name or a raw stage name (mapped via STAGE_PART).
    Entries are small, host-free, and truncated — the ledger is an audit
    spine, not a payload store.
    """
    assert isinstance(record, dict) and isinstance(part, str), "args required"
    assert isinstance(outcome, str) and outcome, "outcome required"
    entry: dict[str, Any] = {
        "t": _now_iso(),
        "part": STAGE_PART.get(part, part),
        "outcome": outcome[:60],
    }
    if error_class:
        entry["error_class"] = str(error_class)[:40]
    if evidence:
        entry["evidence"] = str(evidence)[:_MAX_EVIDENCE]
    if decision:
        entry["decision"] = str(decision)[:60]
    ledger = record.get("_ledger")
    if not isinstance(ledger, list):
        ledger = []
    ledger.append(entry)
    record["_ledger"] = ledger[-_MAX_LEDGER:]


def error_class_of(error: str | None) -> str:
    """Host-free class token from a flow record's ``error`` field.

    The flow writes ``"<class>: <detail>"`` (``_fail``) or a bare reason
    (``unsupported`` / pause markers). The class is the routing key for
    ``choose_move`` and the clustering key for ni_watch.
    """
    if not error or not isinstance(error, str):
        return "none"
    head = error.split(":", 1)[0].strip().lower()
    # Freeform reasons (unsupported records) carry no colon — the class is
    # their first token ("computed-only requires…" -> "computed-only").
    head = head.split()[0] if head.split() else ""
    return (head or "none")[:40]


def choose_move(*, state: str, error_class: str, shell: bool) -> dict:
    """The closed decision table (v0): failure -> next move.

    Returns ``{"move": <MOVES>, "question": <kind>|None, "reopen": [..]}``.
    G1 rules are deliberately few and total — every input maps somewhere, and
    the default is an honest park with retry, never a silent dead end.
    """
    assert isinstance(state, str) and isinstance(error_class, str), "args required"
    if state == "unsupported" and error_class == "computed-only":
        # The one unsupported class a user answer can unblock today.
        return {"move": "ask_user", "question": "supply_date",
                "reopen": ["retry"]}
    if state == "failed" and error_class == "declined":
        # Round-5 field lesson: a decline is a fork, not a death.
        return {"move": "ask_user", "question": "pick_source", "reopen": []}
    if state == "failed" and shell and error_class in ("fetch", "stale"):
        # The source (or the worker) let the shell down — offer both roads.
        return {"move": "park", "question": None,
                "reopen": ["retry", "pick_source"]}
    if state in ("failed", "unsupported"):
        return {"move": "park", "question": None, "reopen": ["retry"]}
    return {"move": "advance", "question": None, "reopen": []}


# User-facing reason sentences per error class — single-writer law: the card
# renders THESE, not raw error strings (History keeps the raw detail).
_REASONS: dict[str, str] = {
    "fetch": "The source couldn't be fetched (it may be down, or not a JSON API).",
    "mapping": "The data came back, but the fields for this card couldn't be matched.",
    "assembling": "The card couldn't be assembled from the mapped fields.",
    "confirm": "The source approval didn't complete.",
    "stale": "Creation stalled and was stopped — the build never finished.",
    "declined": "You declined the suggested source.",
    "computed-only": "This needs a specific date, written as YYYY-MM-DD.",
    "intent": "The request couldn't be understood well enough to build from.",
}


def reason_for(state: str, error: str | None) -> str:
    """One honest sentence for a terminal record; raw detail stays in History."""
    klass = error_class_of(error)
    if klass in _REASONS:
        return _REASONS[klass]
    if state == "unsupported":
        detail = (error or "").strip()
        return detail[:160] if detail else "This request can't be served yet."
    return "Creation didn't finish."


def question_for(state: str, record: dict) -> dict | None:
    """The question a paused (or answerable-terminal) record asks the user."""
    assert isinstance(record, dict), "record required"
    if state == "source":
        return {"kind": "pick_source"}
    if state == "confirm_source":
        return {"kind": "approve_source"}
    if state == "awaiting_credential":
        return {"kind": "add_key"}
    if state == "awaiting_params":
        return {"kind": "fill_params"}
    stamped = record.get("_question")
    if isinstance(stamped, dict) and stamped.get("kind") in QUESTION_KINDS:
        return {"kind": str(stamped["kind"]),
                **({"prompt": str(stamped.get("prompt"))[:200]}
                   if stamped.get("prompt") else {})}
    return None


def terminal_surface(state: str, record: dict, *, shell: bool) -> dict:
    """Reason + reopen + optional question for a terminal record.

    The no-dead-end law lives here: the returned dict ALWAYS carries a
    non-empty ``reason`` and (a question or at least one reopen affordance).
    ni_watch asserts this invariant over live records.
    """
    assert state in ("failed", "unsupported"), "terminal states only"
    error = record.get("error") if isinstance(record.get("error"), str) else None
    move = choose_move(state=state, error_class=error_class_of(error),
                       shell=shell)
    out: dict[str, Any] = {"reason": reason_for(state, error)}
    # A question STAMPED on the record (e.g. supply_date at termination) is
    # authoritative; the move table covers records that predate stamping.
    question = question_for(state, record)
    if question is None and move["move"] == "ask_user" and move.get("question"):
        question = {"kind": move["question"]}
    if question is not None:
        out["question"] = question
    reopen = [r for r in move.get("reopen", []) if r in REOPEN_KINDS]
    if not question and not reopen:
        reopen = ["retry"]  # law backstop: never a bare terminal
    if reopen:
        out["reopen"] = reopen
    return out
