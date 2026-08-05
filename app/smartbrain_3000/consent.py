"""Remembered tool consent — "ask once, then stop asking" for write actions.

When the user approves a REVIEWED (write) tool they may choose to remember it;
future calls to that tool then run without re-prompting. IRREVERSIBLE tools
(send email, delete) are NEVER remembered — they always re-ask, because that
per-action review is the main safeguard (and the anti-prompt-injection guard).

Tools the model can AIM at a destination — the ones whose ``url`` arg it
composes — take a middle path: PER-SITE consent. The whole tool can never be
remembered (an injected URL pointed at the attacker's host would fetch there
unattended). A remembered SITE names one host only: an injected URL points at
the ATTACKER's host, which won't be on the user's list, so that call still
parks — the user's own news site is on the list, so the scheduled check runs.
web_fetch and kb_ingest_url are the two — see ``_SITE_SCOPED_EGRESS``.

Egress the USER configured (a search provider, their own mailbox) carries no
model-composed address and stays whole-tool remember-able. See
``remember_mode`` / ``_FIXED_DESTINATION_EGRESS``.

Storage: the meta row is a JSON list of entry strings. Each entry is either
``"tool"`` (whole-tool consent) or ``"tool@host"`` (site-scoped consent, host
lowercase ASCII, no port, no scheme, exact-host match — subdomains do NOT
inherit). The reader validates per entry, so a corrupt/poisoned row can never
surface as auto-approved.

A change via ``remember`` / ``remember_site`` / ``forget`` / ``forget_site``
takes effect on the next turn or resume, not mid-step: a running turn
snapshots the entry set once at entry.
"""

from __future__ import annotations

import json
import urllib.parse

from . import db, tools

_CONSENT_META_KEY = "remembered_tools"


# Egress tools whose destination the USER configured, not the model: a search
# provider, their own mailbox. The model supplies a query, never an address, so no
# amount of injected instruction can point one of these at a server the attacker
# reads. They stay whole-tool remember-able.
#
# The distinction that matters is WHO NAMES THE DESTINATION, not whether the tool
# touches the network. web_fetch and kb_ingest_url take a `url` straight from the
# model, so whole-tool remembering them would let it reach anywhere unattended —
# those get PER-SITE consent below instead.
#
# Membership here is deliberate and reviewed: an unlisted egress tool is refused
# both modes, so a newly added egress tool is non-remember-able until someone
# decides which mode it belongs to.
_FIXED_DESTINATION_EGRESS = frozenset({"web_search", "web_research", "email_list", "email_read"})

# REVIEWED egress tools whose destination arrives as ``args["url"]`` from the
# model. These carry a site-scoped consent path (never whole-tool). Deliberately
# tiny and explicit — a new site-scoped tool is added here, on purpose, by hand.
_SITE_SCOPED_EGRESS = frozenset({"web_fetch", "kb_ingest_url"})

_MAX_HOST_LEN = 253  # RFC 1035 total host length


def _is_valid_host(host: str) -> bool:
    """True for a normalized ASCII host (dot-separated labels, no port/scheme/@)."""
    assert isinstance(host, str), "host must be a string"
    assert host is not None, "host must not be None"
    if not host or len(host) > _MAX_HOST_LEN:
        return False
    if any(c in host for c in "@:/ \t\n\r"):
        return False
    parts = host.split(".")
    if not parts:  # host was all dots — split yields empty strings but the list is non-empty
        return False
    for part in parts:  # bounded by host length
        if not part or not all(c.isalnum() or c == "-" for c in part):
            return False
    return True


def host_from_url(url: str) -> str | None:
    """Lowercase ASCII host of `url`, or None for hostless/invalid input (never raises)."""
    assert isinstance(url, str), "url must be a string"
    assert url is not None, "url must not be None"
    try:
        parsed = urllib.parse.urlsplit(url)
    except (ValueError, TypeError):
        return None
    host = parsed.hostname  # urllib: lowercases ASCII hosts, strips port
    if not host or not _is_valid_host(host):
        return None
    return host


def _parse_entry(entry: str) -> tuple[str, str | None] | None:
    """Parse a stored entry into ``(tool, host_or_None)``; None if malformed."""
    assert isinstance(entry, str), "entry must be a string"
    assert entry is not None, "entry must not be None"
    if not entry:
        return None
    if "@" not in entry:
        return (entry, None)
    tool, _, host = entry.partition("@")
    if not tool or not host or "@" in host:
        return None
    return (tool, host)


def remember_mode(name: str) -> str | None:
    """Return the consent mode of a tool: ``"tool"``, ``"site"``, or None.

    - ``"tool"``: whole-tool consent (safe destinations — non-egress, or egress
      the user configured).
    - ``"site"``: per-host consent only (model-aimed egress carved out
      explicitly in ``_SITE_SCOPED_EGRESS``).
    - None: never remembered (IRREVERSIBLE, unknown, or an unlisted egress tool).
    """
    assert isinstance(name, str), "name must be a string"
    assert name, "tool name required"
    tool = tools.get_tool(name)
    if tool is None or tool.tier is not tools.Tier.REVIEWED:
        return None
    if not tool.egress or name in _FIXED_DESTINATION_EGRESS:
        return "tool"
    if name in _SITE_SCOPED_EGRESS:
        return "site"
    return None  # model-aimed egress with no explicit carve-out


def is_rememberable(name: str) -> bool:
    """Backward-compat: True only for whole-tool consent (``remember_mode == "tool"``)."""
    assert isinstance(name, str), "name must be a string"
    assert name, "tool name required"
    return remember_mode(name) == "tool"


def remembered(conn) -> set[str]:
    """Return the raw consent entry set the user has chosen.

    Self-defending on READ: a plain name that isn't currently whole-tool remember-able
    is dropped; a ``tool@host`` entry is dropped unless the tool is currently site-mode
    and the host is a syntactically valid ASCII host. That keeps a corrupt/poisoned row,
    an older writer that skipped the check, or a tool whose mode later changed from ever
    surfacing as auto-approved.
    """
    assert conn is not None, "conn required to read consent"
    raw = db.meta_get(conn, _CONSENT_META_KEY)
    if not raw:
        return set()
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return set()  # corrupt config -> safest is "remember nothing" (re-ask)
    if not isinstance(entries, list):
        return set()
    out: set[str] = set()
    for entry in entries:  # bounded by config size
        if not isinstance(entry, str):
            continue
        parsed = _parse_entry(entry)
        if parsed is None:
            continue
        name, host = parsed
        mode = remember_mode(name)
        if host is None and mode == "tool":
            out.add(name)
        elif host is not None and mode == "site" and _is_valid_host(host):
            out.add(entry)
    return out


def remember(conn, name: str) -> bool:
    """Remember consent for a whole-tool rememberable tool. False for anything else."""
    assert conn is not None, "conn required to write consent"
    assert name, "tool name required"
    if remember_mode(name) != "tool":
        return False  # site-mode / irreversible / unknown always re-ask (or use remember_site)
    entries = remembered(conn)
    entries.add(name)
    db.meta_set(conn, _CONSENT_META_KEY, json.dumps(sorted(entries)))
    return True


def remember_site(conn, name: str, url: str) -> bool:
    """Remember consent for one HOST of a site-scoped tool. False for anything else.

    Refuses tools that aren't site-mode (whole-tool tools use ``remember``; IRREVERSIBLE
    and unlisted egress never remember). Refuses a URL with no valid host so the meta
    row cannot grow entries the reader would just drop on the next read.
    """
    assert conn is not None, "conn required to write consent"
    assert name, "tool name required"
    if remember_mode(name) != "site":
        return False
    if not isinstance(url, str):
        return False
    host = host_from_url(url)
    if host is None:
        return False
    entries = remembered(conn)
    entries.add(f"{name}@{host}")
    db.meta_set(conn, _CONSENT_META_KEY, json.dumps(sorted(entries)))
    return True


def forget(conn, name: str) -> None:
    """Drop a whole-tool remembered consent so the tool re-prompts again."""
    assert conn is not None, "conn required to write consent"
    assert name, "tool name required"
    entries = remembered(conn)
    entries.discard(name)
    db.meta_set(conn, _CONSENT_META_KEY, json.dumps(sorted(entries)))


def forget_site(conn, name: str, host: str) -> None:
    """Drop a site-scoped consent for one (tool, host) pair."""
    assert conn is not None, "conn required to write consent"
    assert name, "tool name required"
    if not isinstance(host, str) or not host:
        return
    entries = remembered(conn)
    entries.discard(f"{name}@{host}")
    db.meta_set(conn, _CONSENT_META_KEY, json.dumps(sorted(entries)))


def allowed_in(entries, name: str, args: dict) -> bool:
    """Total: True iff `name` with `args` is auto-approved by the given entry snapshot.

    A missing/malformed url on a site-mode tool is False, never an exception — the
    turn's classifier calls this on every proposed dangerous call.
    """
    assert isinstance(name, str), "name must be a string"
    assert entries is not None, "entries snapshot required (empty set is fine)"
    if not isinstance(args, dict):
        return False
    if name in entries:  # whole-tool consent (fast path)
        return True
    if remember_mode(name) != "site":
        return False
    url = args.get("url")
    if not isinstance(url, str):
        return False
    host = host_from_url(url)
    if host is None:
        return False
    return f"{name}@{host}" in entries


def allowed(conn, name: str, args: dict) -> bool:
    """Total membership check: True if `name` with these `args` is pre-approved."""
    assert conn is not None, "conn required to read consent"
    assert isinstance(name, str), "name must be a string"
    return allowed_in(remembered(conn), name, args)
