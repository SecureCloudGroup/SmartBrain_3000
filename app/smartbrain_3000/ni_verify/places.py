"""Place and US-state checks of the verify step (moved verbatim from ni_flow)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ._hooks import place_states_via_resolver as _place_states_via_resolver
from .text import (
    _GEO_PARAMS,
    _MAX_VALUE_ANSWERS,
)


def _scoping_names_place(chosen: list[dict], place: str) -> bool:
    """True when a chosen list answer's row filter equals (case-folded, whole-word overlap) a
    word of ``place``: the data IS scoped to the ask's named value, so the subcategory policy's
    "isn't specific to" note is no longer honest. Bounded by ``chosen`` (few) and the folded
    word count of each side (``_MAX_WORDS`` only clipped the input texts).

    fix7-lib (2026-10-04): the CBP "San Ysidro" scope and the NHL "Metropolitan" subdivision
    scope both use the same ``filter.equals`` seal; this check is their shared gate."""
    assert isinstance(place, str), "place must be a string"
    place_words = {w.casefold() for w in re.findall(r"[A-Za-z0-9]+", place) if len(w) >= 3}
    if not place_words:
        return False
    for a in chosen[:_MAX_VALUE_ANSWERS]:  # bounded
        flt = (a.get("filter") or {}).get("equals")
        if not isinstance(flt, str):
            continue
        flt_words = {w.casefold() for w in re.findall(r"[A-Za-z0-9]+", flt) if len(w) >= 3}
        if flt_words & place_words:
            return True
    return False


_NOT_A_PLACE = frozenset({"us", "usa", "u.s.", "united states", "america", "nationwide", "national", "here",
                          "near me", "nearby", "my area", "local", "my location", "current location", "home"})


def _place_taken(place: str, source: dict, params: dict) -> bool:
    """Did the address take the asked place? Offered for it (scope), filled from it (a geo parameter),
    or about it by its own declared coverage."""
    if source.get("scope") == "place" or set(params) & _GEO_PARAMS:
        return True
    own = " ".join([str(source.get("name") or ""), str((source.get("coverage") or {}).get("entity") or "")])
    return all(t in own.casefold() for t in place.casefold().split())


_LABEL_STATE_RE = re.compile(r"\(([A-Z]{2})\)\s*$")


def _label_state_code(label: str) -> str | None:
    """The US state code the Library's ``_label`` suffixed to a place reading ("California (PA)"
    → "PA"), or None. The resolver appends "(STATE_CODE)" for every place/zip fill so a US-state
    ask that landed on a same-named city of a different state refuses honestly."""
    assert isinstance(label, str), "label must be a string"
    from ..library_resolve import US_STATES  # local import: avoid a module cycle
    m = _LABEL_STATE_RE.search(label.strip())
    if m is None or m.group(1) not in US_STATES:
        return None
    return m.group(1)


def _national_us_misses_place(source: dict, place: str, request: str) -> bool:
    """True iff the source is a national-US series (``coverage.geo == "US"``) and the asked place
    names a US sub-national area (a state the ask spelled, or a city/county the pack's place
    resolver lands in a US state). Shipping the national figure as the asked place's reading would
    be a confidently wrong card ("unemployment rate in Ohio" over the FRED national UNRATE); the
    verify step refuses so the next source gets a shot. Deterministic; no model."""
    assert isinstance(source, dict) and isinstance(place, str), "args required"
    assert isinstance(request, str), "request must be a string"
    if ((source.get("coverage") or {}).get("geo") or "") != "US":
        return False
    return bool(_place_states_via_resolver(place, request))


def _resolver_landed_outside_state(source: dict, place: str, request: str) -> bool:
    """True iff the ask names a US state (direct state-name/state-code match) but the source's
    place resolver landed on a reading whose ``_label`` suffix is a DIFFERENT state ("earthquakes
    in california" → California, PA). ``states_in`` only reads words the ask spelled, so an
    ambiguous "California" won't drag in every California-named town's state. Deterministic; no
    model."""
    assert isinstance(source, dict) and isinstance(place, str), "args required"
    assert isinstance(request, str), "request must be a string"
    # local import: avoid a module cycle
    from ..library_resolve import US_STATES, states_in
    label = str(source.get("label") or "")
    label_state = _label_state_code(label)
    if label_state is None:
        return False
    # only a same-named town of a state the ask names ("California (PA)" for "california"): a reading
    # whose place is called something else ("Los Angeles (CA)" for "LA", which is also Louisiana's
    # code) was read from the ask's own place words, not mistaken for a state
    label_name = label.rsplit(" (", 1)[0].strip().lower()
    named = {code for code in states_in(f"{request} {place}") if US_STATES[code].lower() == label_name}
    return bool(named) and label_state not in states_in(f"{request} {place}")


# F6-C (blind-5, 2026-10-04): "flu levels in Texas" shipped a mapping-path card
# whose rows named MS/NJ/VA/AL/KS with no Texas row — the model judge couldn't
# tell. ``_rows_contradict_place`` is the code check: when the asked place names
# a US state and the built rows carry state cells for OTHER states but none for
# the asked one, the pick was wrong, not a useful disclosure. Fires only on
# clearly state-keyed rows, so a per-hour forecast with no state cells is not
# affected. The row-filter work (fix6-rows) owns the drop; this refuses when
# filtering is impossible (nothing for the asked state is on the source today).
_MAX_CHECKED_ROWS = 50


_MAX_CHECKED_CELLS = 30


def _rows_contradict_place(preview: object, place: str) -> bool:
    """True iff ``preview['rows']`` names US states OTHER than ``place`` and
    never the asked one. fix10 (blind-8, 2026-10-04): a ``county`` cell is a
    state signal too — the NY flu dataset's rows name counties (OTSEGO,
    NIAGARA, …) that the place resolver maps to NY, so a Texas ask refuses
    even without a state cell. Bounded, pure-code, no model."""
    assert isinstance(place, str), "place must be a string"
    asked_code = _state_code_of(place)
    if asked_code is None:
        return False
    rows = (preview or {}).get("rows") if isinstance(preview, dict) else None
    if not isinstance(rows, list) or not rows:
        return False
    from ..library_resolve import US_STATES  # local import: avoid a module cycle
    seen: set[str] = set()
    county_cells: list[str] = []
    for row in rows[:_MAX_CHECKED_ROWS]:
        if not isinstance(row, dict):
            continue
        for key, value in list(row.items())[:_MAX_CHECKED_CELLS]:
            if not isinstance(value, str):
                continue
            raw = value.strip()
            if len(raw) == 2 and raw.upper() in US_STATES:
                seen.add(raw.upper())
                continue
            code = _state_code_of(raw)
            if code is not None:
                seen.add(code)
                continue
            if isinstance(key, str) and "county" in key.lower() \
                    and len(county_cells) < _MAX_CHECKED_CELLS and raw:
                county_cells.append(raw)
    if county_cells and asked_code not in seen:
        for cell in county_cells:
            seen |= _place_states_via_resolver(cell, "")
            if asked_code in seen:
                break
    return bool(seen) and asked_code not in seen


def _state_code_of(place: str) -> str | None:
    """The two-letter US state code the string names, or None."""
    assert isinstance(place, str), "place must be a string"
    from ..library_resolve import _STATE_BY_NAME, US_STATES  # local: no cycle
    raw = place.strip()
    if not raw:
        return None
    if len(raw) == 2 and raw.upper() in US_STATES:
        return raw.upper()
    return _STATE_BY_NAME.get(raw.lower())


def _source_host_states(source: dict) -> set[str]:
    """The US states the picked source's publisher host names. Dot-split host
    labels; a 2-char label whose uppercase is a USPS code (``data.ny.gov``,
    ``data.pa.gov``) → the code; a lowercase label that is a full state name
    (``data.texas.gov``, ``data.delaware.gov``) → its code; a ``cityof<city>``
    label (``data.cityofchicago.org``) → the states the city resolves to via
    the pack's place resolver. {} when no host or no state recognisable.
    Bounded by the host's dot labels. (fix10 blind-8, 2026-10-04:
    health.data.ny.gov shipped a 'flu activity in texas' card.)"""
    assert isinstance(source, dict), "source must be a dict"
    from ..library_resolve import _STATE_BY_NAME, US_STATES  # local: avoid a cycle
    host = str((source.get("coverage") or {}).get("entity") or "")
    if "." not in host:
        tmpl = str((source.get("access") or {}).get("url_template") or "")
        try:
            host = urlparse(tmpl).hostname or ""
        except (ValueError, TypeError):
            host = ""
    if not host:
        return set()
    out: set[str] = set()
    for label in host.lower().split(".")[:8]:  # bounded by dot labels
        if len(label) == 2 and label.upper() in US_STATES:
            out.add(label.upper())
            continue
        code = _STATE_BY_NAME.get(label)
        if code:
            out.add(code)
            continue
        m = re.match(r"cityof([a-z]+)$", label)
        if m:
            out |= _place_states_via_resolver(m.group(1), "")
    return out


def _source_contradicts_place(source: dict, place: str, request: str) -> bool:
    """True iff the picked source's own name / coverage / publisher host names
    a specific US state and the ask's place can't land in it. The row-level
    fix6-map check reads the preview; this reads the SOURCE record so a
    single-state dataset with no state-keyed rows is still refused for a
    different-state ask. fix10 (blind-8, 2026-10-04) adds the publisher-host
    detection (health.data.ny.gov → NY, data.pa.gov → PA, data.texas.gov → TX,
    data.cityofchicago.org → IL via the place resolver). Deterministic; no
    model."""
    assert isinstance(source, dict) and isinstance(place, str), "args required"
    assert isinstance(request, str), "request must be a string"
    from ..library_resolve import states_in  # local: avoid a cycle
    source_text = " ".join([str(source.get("name") or ""),
                             str((source.get("coverage") or {}).get("entity") or ""),
                             str((source.get("coverage") or {}).get("geo") or "")])
    source_states = states_in(source_text) | _source_host_states(source)
    if len(source_states) != 1:
        return False  # a nationwide / unknown-coverage source: this check doesn't fire
    if not place.strip():
        return False  # no asked place: nothing to contradict
    ask_states = _place_states_via_resolver(place, request)
    return bool(ask_states) and bool(source_states.isdisjoint(ask_states))
