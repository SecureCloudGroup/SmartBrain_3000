"""fix12-lib (live blind9, 2026-10-05): never a confidently wrong Library card.

Three class fixes, each deterministic (no network, no model):

* Defect 1 — a NAMED US state (or city whose place resolver lands in one) on a national-US
  series ("Unemployment rate (FRED)", coverage.geo="US") is a place the address never took;
  the national cut shown as the asked place's reading would be a confidently wrong card.
  ``_verify_source`` refuses so the next source gets a shot, instead of shipping the national
  figure with a note. A truly place-free subject (aurora Kp over a region) still rides through
  as a note because its source's coverage isn't US-national.
* Defect 2 — the ask-level cue bypass in ``_allowed`` must not admit an airport entity whose
  only said-name is a single cardinal-direction word ("northeast" → Northeast Philadelphia
  Airport, matched by alias). "amtrak northeast regional delays" shipped FAA NAS because
  "delays" is an airport cue the ask said and "northeast" is a 9-char word that passed the
  short-code guard. The direction-only name is a weak mention; the airport source rides
  through only when the ask names an airport by more than a direction alone.
* Defect 3 — for a US-state ask whose ``place`` resolver landed on a same-named city of a
  DIFFERENT state ("earthquakes in california" → California, PA), ``_verify_source`` refuses
  so the state-bounded sibling source ("USGS earthquakes in a state", us_state resolver) gets
  a shot. "earthquakes near Pinnacles CA" keeps the near-a-place source because its label
  still says "(CA)".
"""

from __future__ import annotations

from smartbrain_3000 import library_index, ni_flow

# --- defect 1 ---------------------------------------------------------------------------------

def test_a_us_state_on_a_national_us_series_is_a_place_the_address_never_took() -> None:
    """"unemployment rate in Ohio" over FRED's national UNRATE (coverage.geo="US") refuses —
    the national cut labeled "Ohio" would be a confidently wrong card."""
    source = {"name": "Unemployment rate (FRED)",
              "coverage": {"entity": "UNRATE", "geo": "US"}, "provider": "FRED",
              "scope": "global"}
    assert ni_flow._national_us_misses_place(source, "Ohio",
                                               "unemployment rate in ohio") is True


def test_a_truly_place_free_global_subject_still_rides_through_as_a_note() -> None:
    """"aurora forecast tonight upper peninsula" over SWPC K-index (coverage.geo="global")
    doesn't refuse — the policy's place-free note still ships (the planet's Kp isn't the UP's)."""
    source = {"name": "Planetary K-index forecast",
              "coverage": {"entity": "", "geo": "global"}, "provider": "NOAA SWPC",
              "scope": "global"}
    assert ni_flow._national_us_misses_place(source, "upper peninsula",
                                               "aurora forecast tonight upper peninsula") is False


def test_no_asked_place_does_not_fire_the_national_us_check() -> None:
    """"unemployment rate" without a place names no sub-national area — the national series
    rides through unchanged."""
    source = {"name": "Unemployment rate (FRED)",
              "coverage": {"entity": "UNRATE", "geo": "US"}, "provider": "FRED",
              "scope": "global"}
    assert ni_flow._national_us_misses_place(source, "", "unemployment rate") is False


# --- defect 2 ---------------------------------------------------------------------------------

class _Con:
    """Minimal con stand-in: a fake duckdb cursor whose execute returns a result object with
    fetchone()/fetchall() backed by lists this test wires. Only used for queries ``_allowed`` /
    ``_takes`` / ``_subcategories`` / ``_keywords`` / ``_serves`` reach via ``con.execute``."""

    def __init__(self) -> None:
        pass


class _TransitIdx(library_index.LibraryIndex):
    """A LibraryIndex stand-in whose ``_allowed`` dependencies are deterministic: a rail-cued
    ask lands on ``travel/rail`` + ``travel/transit_alerts`` (classify by "delays" keywords —
    neither takes the ``airport`` resolver), with ``travel/airport_delays`` as a sibling that
    DOES. The typed() path decides whether the airport entity rides through from the sibling."""

    def __init__(self) -> None:
        self._takes_map = {"travel/rail": set(), "travel/transit_alerts": {"place"},
                           "travel/airport_delays": {"airport"}}
        self._subs = {"travel/rail": (["current_value"], {}),
                      "travel/transit_alerts": (["alerts", "status"], {}),
                      "travel/airport_delays": (["current_value", "status"], {})}
        self._kw = {"travel/rail": ["amtrak", "train status"],
                    "travel/transit_alerts": ["delays", "delay", "service alert"],
                    "travel/airport_delays": ["airport delay", "ground stop", "airport",
                                              "flight delays"]}

    def _takes(self, _con, sub: str) -> set[str]:  # type: ignore[override]
        return set(self._takes_map.get(sub, set()))

    def _subcategories(self, _con):  # type: ignore[override]
        return dict(self._subs)

    def _keywords(self, sub: str) -> list[str]:  # type: ignore[override]
        return list(self._kw.get(sub, []))


def test_an_airport_entity_named_by_a_direction_alone_is_not_kept_on_a_transit_cue() -> None:
    """"amtrak northeast regional delays": classify lands on rail + transit_alerts (neither takes
    the airport resolver), the airport entity (PNE, matched by alias "northeast") must NOT ride
    through the sibling airport_delays via the ask-level "delays" cue — "northeast" is a
    direction, not an airport name."""
    idx = _TransitIdx()
    found = {"airport": {"name": "Northeast Philadelphia Airport"}}
    said_by = {"airport": ["northeast"]}
    kept = idx._allowed(_Con(), ["travel/rail", "travel/transit_alerts"], found, said_by, set(),
                         ask_words={"amtrak", "northeast", "regional", "delays"})
    assert kept == {}, kept


def test_an_airport_entity_named_by_its_full_name_still_rides_through() -> None:
    """"delays at O'Hare": the airport entity (said_by names two tokens of the airport's own
    name) still rides through from the sibling subcategory — fix7-page's intended path is
    unchanged."""
    idx = _TransitIdx()
    found = {"airport": {"name": "Chicago O'Hare International Airport"}}
    said_by = {"airport": ["o hare"]}
    kept = idx._allowed(_Con(), ["travel/transit_alerts"], found, said_by, set(),
                         ask_words={"delays", "at", "o", "hare"})
    assert "airport" in kept, kept


def test_an_airport_entity_the_ask_spells_by_code_rides_through_any_cue() -> None:
    """"delays at ORD": the airport entity is in ``spelled`` (the ask said its IATA code) and
    ``typed`` returns True regardless of name_words. The direction-only guard doesn't fire."""
    idx = _TransitIdx()
    found = {"airport": {"name": "Chicago O'Hare International Airport"}}
    said_by = {"airport": []}
    kept = idx._allowed(_Con(), ["travel/transit_alerts"], found, said_by, {"airport"},
                         ask_words={"delays", "at", "ord"})
    assert "airport" in kept, kept


# --- defect 3 ---------------------------------------------------------------------------------

def test_a_us_state_ask_whose_place_resolver_landed_on_a_different_state_refuses() -> None:
    """"earthquakes in california" shipped a USGS near-a-place source whose resolver landed on
    "California, PA" (label "California (PA)"); the state-code mismatch refuses the pick."""
    source = {"name": "USGS earthquakes near a place",
              "coverage": {"entity": "", "geo": "global"}, "provider": "US Geological Survey",
              "scope": "place", "label": "California (PA)"}
    assert ni_flow._resolver_landed_outside_state(source, "california",
                                                     "earthquakes in california last 24 hrs") is True


def test_a_city_level_place_in_the_asked_state_still_ships() -> None:
    """"earthquakes near Pinnacles CA" keeps the near-a-place source — the label "Pinnacles (CA)"
    names the asked state, so no mismatch."""
    source = {"name": "USGS earthquakes near a place",
              "coverage": {"entity": "", "geo": "global"}, "provider": "US Geological Survey",
              "scope": "place", "label": "Pinnacles (CA)"}
    assert ni_flow._resolver_landed_outside_state(source, "Pinnacles CA",
                                                     "earthquakes near Pinnacles CA") is False


def test_a_state_ask_on_a_state_bounded_source_still_ships() -> None:
    """"earthquakes in Alaska" rides through "USGS earthquakes in a state" (us_state resolver,
    label "Alaska (AK)") — no mismatch."""
    source = {"name": "USGS earthquakes in a state",
              "coverage": {"entity": "", "geo": "US"}, "provider": "US Geological Survey",
              "scope": "place", "label": "Alaska (AK)"}
    assert ni_flow._resolver_landed_outside_state(source, "Alaska",
                                                    "earthquakes in Alaska") is False


def test_a_label_without_a_parenthetical_state_code_rides_through() -> None:
    """A source whose label has no "(STATE_CODE)" suffix is not a place-resolved pick; the check
    doesn't fire."""
    source = {"name": "USGS global earthquakes feed",
              "coverage": {"entity": "", "geo": "global"}, "provider": "US Geological Survey",
              "scope": "global", "label": ""}
    assert ni_flow._resolver_landed_outside_state(source, "California",
                                                    "earthquakes in California") is False


def test_label_state_code_extracts_the_trailing_state_in_parens() -> None:
    """``_label_state_code`` reads the resolver's "(STATE_CODE)" suffix; a non-US code is None."""
    assert ni_flow._label_state_code("California (PA)") == "PA"
    assert ni_flow._label_state_code("Pinnacles (CA)") == "CA"
    assert ni_flow._label_state_code("Alaska (AK)") == "AK"
    assert ni_flow._label_state_code("California") is None
    assert ni_flow._label_state_code("Pinnacles (XX)") is None  # XX is not a US state code


def test_a_city_nickname_that_is_a_state_code_is_not_a_state() -> None:
    """'air quality in LA' read Los Angeles (CA) and was refused 'it isn't for LA' because LA is also
    Louisiana's code (live dev 2026-10-05). The landed-in-another-state rule is for a same-named town
    of the state the ask names ('California (PA)' for 'california'), nothing wider."""
    landed = ni_flow._resolver_landed_outside_state
    assert landed({"label": "Los Angeles (CA)"}, "LA", "air quality in LA") is False
    assert landed({"label": "California (PA)"}, "california", "earthquakes in california") is True
    assert landed({"label": "California (PA)"}, "California PA", "weather in California PA") is False
    assert landed({"label": "Pinnacles (CA)"}, "Pinnacles CA", "earthquakes near Pinnacles CA") is False
    assert landed({"label": "Portland (OR)"}, "Portland", "Portland weather") is False
