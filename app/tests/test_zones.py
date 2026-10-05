"""Legacy zone names read the same everywhere (PR #483 CI, 2026-10-05): the shipped image (Debian slim)
has no 'US/Mountain'-style names, so a page that printed its times in US/Mountain was read on the
user's clock there and a future eruption looked past. The legacy names map to their canonical zones."""
from smartbrain_3000.zones import zone_named


def test_legacy_us_names_map_to_their_canonical_zone() -> None:
    assert zone_named("US/Mountain").key == "America/Denver"
    assert zone_named("US/Eastern").key == "America/New_York"
    assert zone_named("US/Pacific").key == "America/Los_Angeles"
    assert zone_named("America/Chicago").key == "America/Chicago"


def test_an_unknown_or_malformed_name_is_none() -> None:
    assert zone_named("Mars/Olympus") is None
    assert zone_named("") is None
    assert zone_named("../etc/passwd") is None
