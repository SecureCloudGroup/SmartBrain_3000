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
from urllib.parse import urlsplit

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
    "awaiting_access": "authorize",
    "sampling": "acquire",
    "mapping": "shape",
    "assembling": "present",
    "awaiting_credential": "authorize",
    "awaiting_params": "authorize",
    "ready": "prove",
    "failed": "park",
    "unsupported": "park",
    "declined": "park",
}

# Terminal flow states: the flow is over and nothing is building. ``declined`` (v2) is the honest "I couldn't
# build a reliable card for this"; ``unsupported`` keeps the answerable stops (a question the user can answer)
# and every v1 ending; ``failed`` is a fault the user can retry.
TERMINAL_STATES: frozenset[str] = frozenset({"failed", "unsupported", "declined"})

# Closed question kinds a paused/terminal card may ask. Each maps to one card
# affordance the frontend already has or gains in G1.
QUESTION_KINDS: frozenset[str] = frozenset({
    "pick_source",     # source pause: Library/web suggestions + paste-a-URL
    "give_access",     # awaiting_access: the picked source's key and/or contact email
    "add_key",         # awaiting_credential: credential PUT
    "fill_params",     # awaiting_params: Fill modal
    "supply_date",     # computed ask without a literal date (G1 new)
    "validate",        # commissioning C2: Looks right / Something's wrong
    "model_consent",   # ruling 2: build with a non-local model? (or the local one)
})

# Closed reopen affordances for terminal records. "retry" re-runs the sealed
# request; "pick_source" re-enters the source pick; "delete" always exists on
# the card chrome and is listed only when it is the honest primary way out.
REOPEN_KINDS: frozenset[str] = frozenset({"retry", "pick_source", "delete"})

# Closed vocabulary of card actions the backend declares on a v2 board row / flow
# (a public contract with the web client: unknown kinds must be ignored there).
ACTION_KINDS: frozenset[str] = frozenset({
    "keep", "dismiss", "cancel", "retry", "fix", "refine", "run", "pause", "resume",
    "edit", "delete", "activate", "activate_sites", "give_access", "answer_date",
    "allow_model", "use_local", "fill_param", "add_credential", "review_fix", "pick_source",
})

# ---- the declined card state (plan G6, P6, 6.1, 6.2: "a decline is acceptable, a wrong card is the failure") ----
#
# A decline is an ask the system cannot answer reliably. It ends as ONE terminal state, ``declined``, carrying a
# closed ``reason`` code, one plain line, and up to ``DECLINE_LINKS`` links. Every v2 ending that stops without a
# card is a row of ``ENDINGS`` below, the only place the rule lives: which flow state it lands in and, for a
# decline, which code. ``ni_flow._end`` reads this table; nothing else decides.
DECLINE_REASONS: frozenset[str] = frozenset({
    "no_answering_source",   # no source we can read declares an answer for this ask (and research found none)
    "research_gave_up",      # the researcher ran and ended without a card: the budget or the steps ran out, or the model stopped, with no card
    "verify_refused",        # a card was built and the verifier refused it twice
    "compare_unbuildable",   # a compare of named things that no source reads side by side
    "source_cant_answer",    # the one source the user named (or Fix re-reads) does not answer this ask
    "ask_unsupported",       # this kind of ask is not built yet
})
DECLINE_LINKS = 3
DECLINE_HEADLINE = "I couldn't build a reliable card for this"
DECLINE_LINES: dict[str, str] = {
    "no_answering_source": "No source I can read answers this.",
    "research_gave_up": "I looked for a source and ran out of steps before finding one I could trust.",
    "verify_refused": "I found a source, but the card built from it failed its checks twice, so I won't show it.",
    "compare_unbuildable": "I can't compare these yet: no source reads them side by side.",
    "source_cant_answer": "That source can't answer this ask.",
    "ask_unsupported": "This kind of ask isn't supported yet.",
}
# The user's ways back from a decline: run the same ask again, or re-enter the source pick (paste a link).
DECLINE_REOPEN: tuple[str, ...] = ("retry", "pick_source")

# One row per ending of the v2 flow that is not a card: (flow state it lands in, decline code or None).
# A decline is for an ask nothing the user can do on this card will make answerable; an ending where the user
# can still act (pick a link, add a key, answer a question, retry a fault) keeps its own state.
ENDINGS: dict[str, tuple[str, str | None]] = {
    # -- declined: nothing the card can ask the user would change the answer --
    "research_declined_checks": ("declined", "verify_refused"),
    "research_declined_budget": ("declined", "research_gave_up"),
    "research_declined_no_source": ("declined", "no_answering_source"),
    "no_source_research_off": ("declined", "no_answering_source"),
    "no_source_rows_exhausted": ("declined", "no_answering_source"),
    "compare_unbuildable": ("declined", "compare_unbuildable"),
    "compare_refused_at_build": ("declined", "compare_unbuildable"),
    "source_cant_answer": ("declined", "source_cant_answer"),
    "ask_unsupported": ("declined", "ask_unsupported"),
    # -- stays: the user can act --
    "paste_a_link": ("source", None),                    # the stop card's paste box (a decline the user reopened)
    "needs_key_or_email": ("awaiting_access", None),     # the picked source's key or contact email
    # (a built card that waits for a key or a value is not an ending: it is a draft card with its own gate)
    "model_consent_question": ("unsupported", None),     # build with a model outside this computer?
    "date_question": ("unsupported", None),              # "when is it?"
    "fault": ("failed", None),                           # fetch / mapping / assembly: retry runs it again
}

_GATE_ACTIONS: dict[str, tuple[str, ...]] = {
    "awaiting_access": ("give_access",),
    "awaiting_credential": ("add_credential",),
    "awaiting_params": ("fill_param",),
}
_QUESTION_ACTIONS: dict[str, tuple[str, ...]] = {
    "supply_date": ("answer_date",),
    "model_consent": ("allow_model", "use_local"),
    "give_access": ("give_access",),
    "pick_source": ("pick_source",),
}
# The action behind each way out a terminal card names: a reopen the card offers always has its action on the
# board row ("delete" is on every row already). ``pick_source`` is a shell's: a finished card reopens via Fix.
_REOPEN_ACTIONS: dict[str, tuple[str, ...]] = {"retry": ("retry",), "pick_source": ("pick_source",), "delete": ()}
assert set(_REOPEN_ACTIONS) == REOPEN_KINDS, "every reopen kind has its action"
assert all(a in ACTION_KINDS for ways in (*_REOPEN_ACTIONS.values(), *_QUESTION_ACTIONS.values()) for a in ways), \
    "closed vocabulary"
_ITEM_ACTIONS: dict[str, tuple[str, ...]] = {
    "draft": ("edit", "refine", "delete"),
    "commissioning": ("run", "pause", "edit", "refine", "delete"),
    "live": ("run", "pause", "edit", "refine", "delete"),
    "degraded": ("run", "pause", "fix", "edit", "refine", "delete"),
    "failing": ("run", "pause", "fix", "edit", "refine", "delete"),
    "broken": ("fix", "edit", "delete"),
    "paused": ("resume", "edit", "delete"),
}


# The flow marker for "stopped, nothing building" (ni_flow.AWAITING_SOURCE_PICK is the same text).
PARKED_MARKER = "awaiting_pick"


def actions_for(state: str, record: dict, *, shell: bool, awaiting_yes: bool,
                item_state: str, has_payload: bool) -> list[str]:
    """The card actions the backend declares (pure, no store). ``state`` is the flow state
    ('' when there is no live flow). Law: a RUNNING flow always offers ``cancel`` (a flow parked on
    the pick marker runs nothing, so it has none); a terminal one always
    offers at least one way out; ``delete`` is on every list; every kind is in ``ACTION_KINDS``."""
    assert isinstance(record, dict), "record required"
    assert isinstance(state, str) and isinstance(item_state, str), "states are text"
    out: list[str] = []
    if state and state != "ready":
        if state in TERMINAL_STATES:
            surface = terminal_surface(state, record, shell=shell)
            out += _QUESTION_ACTIONS.get(str((surface.get("question") or {}).get("kind")), ())
            for kind in surface.get("reopen", []):  # bounded by REOPEN_KINDS
                out += _REOPEN_ACTIONS[kind]
            out += ("delete",) if shell else ("fix", "refine", "delete")
        else:
            out += _GATE_ACTIONS.get(state, ())
            if not (state == "source" and record.get("error") == PARKED_MARKER):
                out.append("cancel")
    elif awaiting_yes:
        out += ("keep", "dismiss")
    else:
        out += _ITEM_ACTIONS.get(item_state, ())
        if item_state == "draft" and has_payload:
            out.insert(0, "activate")
    out.append("delete")  # the card chrome always has Delete, whatever the card is doing
    result = list(dict.fromkeys(out))
    assert all(kind in ACTION_KINDS for kind in result), "closed vocabulary"
    assert "delete" in result, "delete always exists"
    return result


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
    if state == "declined":
        # A decline is final for this build; the user reopens it (run again, or paste a link to the data).
        return {"move": "park", "question": None, "reopen": list(DECLINE_REOPEN)}
    if state == "failed" and error_class == "declined":
        # Round-5 field lesson: a decline is a fork, not a death.
        return {"move": "ask_user", "question": "pick_source", "reopen": []}
    if state == "failed" and shell and error_class in ("fetch", "stale"):
        # The source (or the worker) let the shell down — offer both roads.
        return {"move": "park", "question": None,
                "reopen": ["retry", "pick_source"]}
    if state == "failed" and error_class == "cancelled":
        # v2 Cancel: the user stopped it; the one way on is to run the same request again
        return {"move": "park", "question": None, "reopen": ["retry"]}
    if state in TERMINAL_STATES:
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
    "research": "The research couldn't finish (the model was unavailable) — retry runs it again.",
    # Claims audit 2026-09-21: classes the flow actually writes that the map
    # missed — the card showed the blind sentence for each of these.
    "assembly": "The card couldn't be assembled from the mapped fields.",
    "derive": "The data came back in a shape no card can read yet.",
    "busy": "Another card was building — this one couldn't start.",
    "worker": "The build crashed — retry runs it again from your request.",
    "remap": "The rebuild couldn't start from this card's source.",
    "cancelled": "You stopped this build.",
}


def reason_for(state: str, error: str | None) -> str:
    """One honest sentence for a terminal record; raw detail stays in History."""
    klass = error_class_of(error)
    if state == "declined":  # the code is the error's class; an unknown code reads the blind sentence, never a raise
        return DECLINE_LINES.get(klass, DECLINE_HEADLINE + ".")
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
    if state == "awaiting_access":
        return {"kind": "give_access"}
    if state == "awaiting_credential":
        return {"kind": "add_key"}
    if state == "awaiting_params":
        return {"kind": "fill_params"}
    stamped = record.get("_question")
    if isinstance(stamped, dict) and stamped.get("kind") in QUESTION_KINDS:
        question = {"kind": str(stamped["kind"]),
                    **({"prompt": str(stamped.get("prompt"))[:(1400 if stamped["kind"] == "model_consent" else 200)]}
                       if stamped.get("prompt") else {})}
        if stamped["kind"] == "model_consent":  # the card names both choices exactly
            question["model"] = str(stamped.get("model") or "")[:200]
            question["local"] = str(stamped.get("local") or "")[:200]
        return question
    return None


def terminal_surface(state: str, record: dict, *, shell: bool) -> dict:
    """Reason + reopen + optional question for a terminal record.

    The no-dead-end law lives here: the returned dict ALWAYS carries a
    non-empty ``reason`` and (a question or at least one reopen affordance).
    ni_watch asserts this invariant over live records.
    """
    assert state in TERMINAL_STATES, "terminal states only"
    error = record.get("error") if isinstance(record.get("error"), str) else None
    move = choose_move(state=state, error_class=error_class_of(error),
                       shell=shell)
    out: dict[str, Any] = {"reason": reason_for(state, error)}
    if state == "declined":
        view = decline_view(record)
        out["reason"] = view["line"]
        out["decline"] = view
    # A question STAMPED on the record (e.g. supply_date at termination) is
    # authoritative; the move table covers records that predate stamping.
    question = question_for(state, record)
    if question is None and move["move"] == "ask_user" and move.get("question"):
        question = {"kind": move["question"]}
    if not shell and question is not None and question.get("kind") == "pick_source":
        question = None  # a finished card never re-enters the source pick: Fix re-reads its own source
    if question is not None:
        out["question"] = question
    reopen = [r for r in move.get("reopen", []) if r in REOPEN_KINDS and (shell or r != "pick_source")]
    if not question and not reopen:
        reopen = ["retry"]  # law backstop: never a bare terminal
    if reopen:
        out["reopen"] = reopen
    return out


def clean_links(links: object) -> list[dict]:
    """Up to ``DECLINE_LINKS`` links a declined card may show: ``{title, url, host}`` with an https address, one per
    address, each field bounded. Anything else a caller hands over is dropped, never rendered."""
    assert DECLINE_LINKS > 0, "a link cap"
    out: list[dict] = []
    seen: set[str] = set()
    for row in links if isinstance(links, list) else []:  # bounded by the input, cut at DECLINE_LINKS
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        url = row["url"].strip()
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or len(url) > 500 or url in seen:
            continue
        seen.add(url)
        title = " ".join(str(row.get("title") or "").split())[:120]
        out.append({"title": title or parts.hostname, "url": url, "host": parts.hostname.lower()[:120]})
        if len(out) == DECLINE_LINKS:
            break
    return out


def decline_view(record: dict) -> dict:
    """``{reason, headline, line, links}`` of a declined record, from its sealed ``_decline_card`` (single writer:
    the card renders this verbatim). A record without a valid seal still answers honestly with the blind code
    ``no_answering_source``; the view is total."""
    assert isinstance(record, dict), "record required"
    sealed = record.get("_decline_card") if isinstance(record.get("_decline_card"), dict) else {}
    code = sealed.get("reason") if sealed.get("reason") in DECLINE_REASONS else "no_answering_source"
    line = " ".join(str(sealed.get("line") or "").split())[:200] or DECLINE_LINES[code]
    out = {"reason": code, "headline": DECLINE_HEADLINE, "line": line, "links": clean_links(sealed.get("links"))}
    assert out["reason"] in DECLINE_REASONS and len(out["links"]) <= DECLINE_LINKS, "a closed decline"
    return out


def seal_decline(ending: str, line: str | None, links: object) -> dict:
    """The ``_decline_card`` seal for one ``ENDINGS`` row that declines: ``{reason, line, links}``."""
    assert ending in ENDINGS, "a known ending"
    state, code = ENDINGS[ending]
    assert state == "declined" and code in DECLINE_REASONS, "this ending declines"
    text = " ".join(str(line or "").split())[:200] or DECLINE_LINES[code]
    return {"reason": code, "line": text, "links": clean_links(links)}
