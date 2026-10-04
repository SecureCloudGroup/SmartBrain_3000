"""fix6-rows regressions (live blind5, 2026-10-04): a named place is applied to a list's rows; a
single-league source refuses a foreign-league conference; "tracker" is a status frame kind so an
"active X" source leads a written outlook; a WMO-code conditions cell answers "hail risk"; a
resolver param that doesn't appear in the url_template rides in ``params`` only (FAA Orlando).

Deterministic replay: no network, no model. Builds the declared-answers path with inline samples
and asserts the sealed pipeline carries the row filter (defect A) or the ask refuses (defect B);
asserts ``library_index.frame_kind_from_text("hurricane tracker") == "status"`` (defect D);
asserts ``select_answers`` picks the conditions cell (which decodes WMO 96 / 99 as hail) on an
"hail risk" ask against the Open-Meteo forecast's declared answers (defect C); and asserts
``library_resolve._expand`` keeps a single URL when a resolver param only lives in ``params``
(defect E — the FAA airport-events source now ships for "delays at Orlando airport").
"""

from __future__ import annotations

import pytest

from smartbrain_3000 import library_index, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.library_index import frame_kind_from_text

# --- defect A: place-row filter ----------------------------------------------------------------

_CBP_ANSWER = {
    "name": "ports", "label": "Border wait times", "words": ["border wait"],
    "primary": True, "kind": "list", "path": "items",
    "cells": [
        {"path": "port_name", "label": "Port", "type": "text"},
        {"path": "crossing_name", "label": "Crossing", "type": "text"},
        {"path": "delay_minutes", "label": "Car wait", "type": "number", "unit": "min"},
    ],
}
_CBP_SAMPLE = {"items": [
    {"port_name": "Alexandria Bay", "crossing_name": "Thousand Islands Bridge", "delay_minutes": 15},
    {"port_name": "Blaine", "crossing_name": "Peace Arch", "delay_minutes": 5},
    {"port_name": "San Ysidro", "crossing_name": "Pedestrian East", "delay_minutes": 30},
    {"port_name": "San Ysidro", "crossing_name": "Pedestrian West", "delay_minutes": 20},
    {"port_name": "San Ysidro", "crossing_name": "Vehicles", "delay_minutes": 72},
    {"port_name": "Nogales", "crossing_name": "DeConcini", "delay_minutes": 25},
]}


def test_a_named_place_scopes_the_list_to_rows_that_name_it() -> None:
    """CBP border wait times carries a "Port" cell; "San Ysidro border wait" picks only those
    rows. The sealed pipeline carries a ``where`` op on the place cell."""
    answer = ni_flow._scope_rows_to_place(_CBP_ANSWER, _CBP_SAMPLE, "San Ysidro")
    assert answer["filter"] == {"path": "port_name", "equals": "San Ysidro"}
    built = ni_flow.build_from_answers([answer], _CBP_SAMPLE, "San Ysidro border wait")
    where = next(op for st in built["pipeline"] if st.get("op") == "transform"
                 for op in st["apply"] if op["fn"] == "where")
    assert where == {"fn": "where", "field": "rows", "key": "port_name", "op": "eq",
                     "value": "San Ysidro"}
    assert len(built["preview_payload"]["rows"]) == 3


def test_a_named_place_the_source_doesnt_hold_is_honest_nothing() -> None:
    """A port not in the response → ``_scope_rows_to_place`` raises the Library's "nothing for
    <place>" signal; ``_try_answers_build`` turns it into a nothing-handoff."""
    with pytest.raises(ValueError, match="has nothing for Pembina"):
        ni_flow._scope_rows_to_place(_CBP_ANSWER, _CBP_SAMPLE, "Pembina")


def test_a_text_only_place_cell_matches_case_insensitively() -> None:
    """The ask typed "san ysidro" (phone lowercase); the filter still binds the canonical row
    value exactly, so the engine's ``where eq`` keeps matching."""
    answer = ni_flow._scope_rows_to_place(_CBP_ANSWER, _CBP_SAMPLE, "san ysidro")
    assert answer["filter"]["equals"] == "San Ysidro"


def test_scope_rows_to_place_leaves_the_answer_alone_when_rows_name_no_place() -> None:
    """A list whose row has no "Port"/"Station"/"City" cell is left as declared — the engine
    reads every row."""
    plain = {"name": "p", "label": "P", "words": [], "primary": True, "kind": "list",
             "path": "items", "cells": [{"path": "x", "label": "X", "type": "number"}]}
    assert ni_flow._scope_rows_to_place(plain, {"items": []}, "San Ysidro") is plain


def test_scope_rows_to_place_respects_an_existing_filter() -> None:
    """A declared ``filter`` scopes the rows already; the place-row helper leaves it alone."""
    pre = {**_CBP_ANSWER, "filter": {"path": "port_status", "equals": "Open"}}
    assert ni_flow._scope_rows_to_place(pre, _CBP_SAMPLE, "San Ysidro") is pre


# --- defect B: a single-league source refuses a foreign-league conference ----------------------

class _TaxLib:
    """A LibraryIndex look-alike that only exposes what ``_stray_topics`` reads: taxonomy
    (classify + topics) and entity_vocabulary (league team / sports_league aliases)."""

    def __init__(self, vocab: dict[str, set[str]]) -> None:
        self._vocab = {k.lower(): set(v) for k, v in vocab.items()}

    def taxonomy(self) -> list[dict]:
        # one subcategory with "standings" + "nfc"/"afc" words so the proper-noun check has a
        # topic context (not strictly required for F7 fallback, but matches the real pack).
        return [{"id": "sports", "subcategories": [
            {"id": "standings", "label": "Standings",
             "keywords": ["standings", "table", "al east", "nl east", "nfc east", "afc east"]}]}]

    def classify(self, text: str, limit: int = 3) -> list[str]:
        return ["sports/standings"] if "standings" in (text or "").lower() else []

    def subcategory(self, _s: str) -> dict:
        return {}

    def entity_vocabulary(self, entity: str) -> set[str]:
        return set(self._vocab.get(entity.strip().lower(), set()))


_MLB_SOURCE = {"categories": ["sports/standings"], "name": "MLB standings",
               "description": "Standings by division", "examples": ["mlb standings", "al east standings"],
               "coverage": {"entity": "MLB", "geo": "US"}, "entity_params": {}, "readings": [],
               "label": "", "scope": "global", "provider": "MLB"}
_MLB_ANSWERS = [{"name": "al_east", "label": "AL East standings", "primary": True, "kind": "list",
                 "path": "r[0].teams", "cells": [{"path": "name", "label": "Team", "type": "text"}],
                 "words": ["al east", "american league east", "standings", "division", "mlb standings",
                           "table", "nl east", "national league east", "nl central"]}]


def _stray(request: str, source: dict, params: dict, answers: list[dict], intent: dict,
            vocab: dict | None = None) -> set[str]:
    frame = {"kind": None, "window": None, "place": None, "categories": ["sports/standings"],
             "ask_categories": ["sports/standings"], "about_categories": ["sports/standings"],
             "lib": _TaxLib(vocab or {"mlb": {"yankees", "mets", "red", "sox", "dodgers", "mlb"}})}
    return ni_flow._stray_topics(frame, source, params, answers, request, intent)


def test_mlb_standings_refuses_nfc_east() -> None:
    """A league source's entity vocabulary doesn't take NFC; it is a stray proper token."""
    stray = _stray("NFC East standings", _MLB_SOURCE, {}, _MLB_ANSWERS,
                   {"subject": "NFC East standings", "wants": ["standings"], "names": ["NFC East"]})
    assert "nfc" in stray


def test_mlb_standings_still_ships_yankees_standings() -> None:
    """``entity_vocabulary('MLB')`` lists ``yankees``; the proper-noun check subtracts it."""
    stray = _stray("Yankees standings", _MLB_SOURCE, {}, _MLB_ANSWERS,
                   {"subject": "New York Yankees", "wants": ["standings"], "names": ["Yankees"]})
    assert "yankees" not in stray


def test_mlb_standings_still_ships_al_east_standings() -> None:
    """A division word the source's answers name is in own; the check leaves it."""
    stray = _stray("AL East standings", _MLB_SOURCE, {}, _MLB_ANSWERS,
                   {"subject": "AL East standings", "wants": ["standings"], "names": ["AL East"]})
    assert "al" not in stray and "east" not in stray


def test_a_single_entity_without_a_league_vocab_still_uses_the_exemption() -> None:
    """A non-sports single entity (Slack) has no league vocab; the exemption keeps the proper
    check off — ``_other_subject``'s D1 branch still catches a foreign subject."""
    slack = {"categories": ["tech/status_pages"], "name": "Slack status", "description": "",
             "examples": [], "coverage": {"entity": "Slack"}, "entity_params": {},
             "readings": [], "label": "", "scope": "global", "provider": "Slack"}
    frame = {"kind": None, "window": None, "place": None, "categories": ["tech/status_pages"],
             "ask_categories": [], "about_categories": [], "lib": _TaxLib({})}
    stray = ni_flow._stray_topics(frame, slack, {}, [], "is zoom down",
                                   {"subject": "Zoom", "wants": ["status"], "names": ["Zoom"]})
    assert "zoom" not in stray  # exemption still protects non-league entities


# --- defect C: WMO weather codes decode hail-thunderstorm, so "hail risk" ships ---------------

_OPEN_METEO = [
    {"name": "temperature", "label": "Temperature", "words": ["temperature", "temp"],
     "primary": True, "kind": "value", "path": "current.temperature_2m", "type": "number",
     "measure": "temperature", "window": "now"},
    {"name": "conditions", "label": "Conditions", "words": ["conditions", "sky", "storm",
     "thunderstorm", "lightning", "hail", "fog"], "primary": True, "kind": "value",
     "path": "current.weather_code", "type": "number", "codes": "wmo_weather", "measure": "conditions",
     "window": "now"},
    {"name": "high_today", "label": "High today", "words": ["high", "max"], "primary": True,
     "kind": "value", "path": "daily.temperature_2m_max[0]", "type": "number",
     "measure": "temperature", "window": "today"},
]


def test_hail_risk_lands_on_the_conditions_answer() -> None:
    """The user's "hail" is a word of the Conditions answer (which decodes WMO 96/99 as hail);
    ``_unanswered_wants`` doesn't flag it, and the Open-Meteo card ships honestly."""
    answers = [ni_flow._clean_answer(a) for a in _OPEN_METEO]
    assert all(answers)
    unanswered = ni_flow._unanswered_wants(answers, "hail risk Oklahoma City today",
                                            ["hail risk"], ["35.467", "-97.513"])
    assert unanswered == []
    assert "hail" in {w for a in answers for w in a["words"]}


def test_wmo_weather_table_includes_the_hail_codes() -> None:
    """The two WMO codes a hail-risk card actually reports."""
    table = nimod.LABEL_TABLES["wmo_weather"]
    assert "hail" in table[96].lower() and "hail" in table[99].lower()


# --- defect D: "tracker" is a status frame kind ------------------------------------------------


def test_hurricane_tracker_is_a_status_frame() -> None:
    """So an active-storms source with the ``alerts`` kind leads a single-basin ``text_brief``
    outlook: ``_SERVES['status']`` includes ``alerts`` and ``current_value``, the written outlook
    kind ``text_brief`` is not in it."""
    assert frame_kind_from_text("hurricane tracker") == "status"
    assert "alerts" in library_index._SERVES["status"]
    assert "text_brief" not in library_index._SERVES["status"]


def test_other_tracker_asks_are_status_too() -> None:
    """"tracker" is general — no hard-coding of "hurricane"."""
    assert frame_kind_from_text("flight tracker") == "status"
    assert frame_kind_from_text("fire trackers") == "status"


# --- defect E: a resolver param that isn't in the url_template still rides as a filter --------


def test_expand_keeps_one_url_when_the_url_doesnt_take_the_choice_param() -> None:
    """FAA airport-events exposes a global ``airport-events`` endpoint and filters by ``airport``
    inside each answer; the url_template doesn't include ``{airport}``. ``_expand`` must emit a
    single URL with the first reading in ``params`` instead of two identical URLs the caller
    would dedup-reject with "can't tell the readings apart"."""
    from smartbrain_3000 import library_resolve
    values = {"airport": [("KMCO", "Orlando International Airport (FL)"),
                           ("KSFB", "Orlando Sanford International Airport (FL)")]}
    out = library_resolve._expand("https://nasstatus.faa.gov/api/airport-events",
                                   values, groups={"airport": "airport"})
    assert len(out) == 1
    assert out[0]["params"] == {"airport": "KMCO"}
    assert out[0]["label"] == "Orlando International Airport (FL)"


def test_expand_still_branches_when_the_url_takes_the_choice_param() -> None:
    """A per-airport URL (``?ids={airport}``) still produces one URL per reading, so the pick
    offers both for a disambiguation."""
    from smartbrain_3000 import library_resolve
    values = {"airport": [("KMCO", "Orlando International Airport (FL)"),
                           ("KSFB", "Orlando Sanford International Airport (FL)")]}
    out = library_resolve._expand("https://aviationweather.gov/api/data/metar?ids={airport}&format=json",
                                   values, groups={"airport": "airport"})
    assert len(out) == 2
    assert {u["params"]["airport"] for u in out} == {"KMCO", "KSFB"}


def test_rows_are_not_rescoped_when_the_address_already_took_the_place() -> None:
    """A source the Library offered FOR the named place (scope "place": its address took the place —
    USGS earthquakes in a state) is already scoped; its rows' place cells name each row ("62 km WNW of
    Elfin Cove, Alaska"), so re-filtering them by the place would empty a right card (live holdout
    2026-10-04). A global list (CBP's ports) and a geo-filled address keep their current behavior."""
    assert ni_flow._rows_need_place_scope({"_library_scope": "place"}, {"state": "AK"}) is False
    assert ni_flow._rows_need_place_scope({"_library_scope": "global"}, {}) is True
    assert ni_flow._rows_need_place_scope({"_library_scope": "global"}, {"lat": "1", "lon": "2"}) is False
