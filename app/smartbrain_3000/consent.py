"""Remembered tool consent — "ask once, then stop asking" for write actions.

When the user approves a REVIEWED (write) tool they may choose to remember it;
future calls to that tool then run without re-prompting. IRREVERSIBLE tools
(send email, delete) are NEVER remembered — they always re-ask, because that
per-action review is the main safeguard (and the anti-prompt-injection guard).
Tools that can carry data OUT to a destination the model picked are never
remembered either, for the same reason: unattended egress with a model-composed
URL is the shape a prompt-injection exfiltration takes. Egress that only pulls
the user's own data in from a fixed endpoint stays remember-able. See
_is_rememberable and _INBOUND_EGRESS.
The set is plaintext config in the meta table (a list of tool names).

A change via remember()/forget() takes effect on the next turn or resume, not
mid-step: a running turn snapshots the set once at entry.
"""

from __future__ import annotations

import json

from . import db, tools

_CONSENT_META_KEY = "remembered_tools"


# Egress tools that only pull the user's OWN data in from a fixed, already-authorized
# endpoint. They carry no model-chosen destination, so they cannot be the outbound leg
# of an exfiltration and stay remember-able. Anything absent from this set is refused
# by default — a new egress tool is non-remember-able until someone judges it inbound.
_INBOUND_EGRESS = frozenset({"email_list", "email_read"})


def _is_rememberable(name: str) -> bool:
    """True only for a registered REVIEWED tool that cannot carry data outbound.

    REVIEWED is the one remembered tier; outbound egress is excluded on top of it.
    Remembering such a tool turns "the model may fetch this URL, now that you have
    approved it" into "the model may reach ANY destination it composes, unattended"
    — and it composes those from content it has just read (a document, an imported
    vault, an email), which is precisely the channel a prompt injection arrives on.
    The call is still audited, but an audit records an exfiltration; it does not
    prevent one, and the per-call review was what stood between the two.
    """
    tool = tools.get_tool(name)
    if tool is None or tool.tier is not tools.Tier.REVIEWED:
        return False
    return not tool.egress or name in _INBOUND_EGRESS


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
    return {n for n in names if isinstance(n, str) and _is_rememberable(n)}


def remember(conn, name: str) -> bool:
    """Remember consent for a rememberable tool. No-op (False) for any other."""
    assert conn is not None, "conn required to write consent"
    assert name, "tool name required"
    if not _is_rememberable(name):
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
