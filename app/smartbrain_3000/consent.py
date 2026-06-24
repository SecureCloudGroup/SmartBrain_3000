"""Remembered tool consent — "ask once, then stop asking" for write actions.

When the user approves a REVIEWED (write) tool they may choose to remember it;
future calls to that tool then run without re-prompting. IRREVERSIBLE tools
(send email, delete) are NEVER remembered — they always re-ask, because that
per-action review is the main safeguard (and the anti-prompt-injection guard).
The set is plaintext config in the meta table (a list of tool names).

A change via remember()/forget() takes effect on the next turn or resume, not
mid-step: a running turn snapshots the set once at entry.
"""

from __future__ import annotations

import json

from . import db, tools

_CONSENT_META_KEY = "remembered_tools"


def _is_reviewed(name: str) -> bool:
    """True only for a currently-registered REVIEWED tool (the one remembered tier)."""
    tool = tools.get_tool(name)
    return tool is not None and tool.tier is tools.Tier.REVIEWED


def remembered(conn) -> set[str]:
    """Return the REVIEWED tool names the user has chosen to auto-approve.

    Tier-filters on read so a corrupt/poisoned row, a future writer that skipped
    the check, or a tool whose tier later changed can never surface as auto-approved
    — the consent set is self-defending, not reliant on caller discipline.
    """
    assert conn is not None, "conn required to read consent"
    raw = db.meta_get(conn, _CONSENT_META_KEY)
    if not raw:
        return set()
    try:
        names = json.loads(raw)
    except (ValueError, TypeError):
        return set()  # corrupt config -> safest is "remember nothing" (re-ask)
    if not isinstance(names, list):
        return set()
    return {n for n in names if isinstance(n, str) and _is_reviewed(n)}


def remember(conn, name: str) -> bool:
    """Remember consent for a REVIEWED tool. No-op (False) for non-REVIEWED tools."""
    assert conn is not None, "conn required to write consent"
    assert name, "tool name required"
    if not _is_reviewed(name):
        return False  # only writes are remembered; irreversible/unknown always re-ask
    names = remembered(conn)
    names.add(name)
    db.meta_set(conn, _CONSENT_META_KEY, json.dumps(sorted(names)))
    return True


def forget(conn, name: str) -> None:
    """Drop a remembered consent so the tool re-prompts again."""
    assert conn is not None, "conn required to write consent"
    assert name, "tool name required"
    names = remembered(conn)
    names.discard(name)
    db.meta_set(conn, _CONSENT_META_KEY, json.dumps(sorted(names)))
