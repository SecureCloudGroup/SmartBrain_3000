"""Time-zone names from outside (a page, a source's own field) → a zone the app can use.

The legacy "US/…"-style names are not in every tz database (Debian slim, the shipped image, moved them
to tzdata-legacy), so they map to their canonical zones first: the same name reads the same everywhere.
"""
from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}")
_LEGACY = {
    "US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles", "US/Alaska": "America/Anchorage", "US/Hawaii": "Pacific/Honolulu",
    "US/Arizona": "America/Phoenix", "US/East-Indiana": "America/Indiana/Indianapolis",
    "US/Michigan": "America/Detroit", "US/Aleutian": "America/Adak", "US/Samoa": "Pacific/Pago_Pago",
    "Canada/Eastern": "America/Toronto", "Canada/Central": "America/Winnipeg",
    "Canada/Mountain": "America/Edmonton", "Canada/Pacific": "America/Vancouver",
}


def zone_named(name: str) -> ZoneInfo | None:
    """The zone ``name`` names (legacy names mapped to canonical ones), or None when it names none."""
    text = str(name or "").strip()
    if not text or len(text) > 64 or not _NAME_RE.fullmatch(text):
        return None
    try:
        return ZoneInfo(_LEGACY.get(text, text))
    except (ZoneInfoNotFoundError, ValueError):
        return None
