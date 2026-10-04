"""Page-card verify gate (C9): a deterministic check that a page READING is
data the page states, about the thing asked, in the shape and time asked.

The labeled set is REAL: 50 pages recorded 2026-09-29 with the app's own
guarded fetch (tests/fixtures/pages/*.html.gz, manifest.json), each reading
labeled accept/reject by hand (readings.json). ``seed`` rows are the field
failures and must-accepts the root-cause synthesis named; ``holdout`` rows were
written alongside them and never used to shape the rules. The gate must never
accept a labeled reject (false-accept 0) and may refuse at most 5% of the
accepts. Pages are parsed in-process by the jail's own ``extract`` (the same
function the subprocess runs) so the set stays fast; test_pagegraph covers the
real subprocess.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

import pytest

from smartbrain_3000 import jail_extract, page_verify, pagegraph

_PAGES = Path(__file__).parent / "fixtures" / "pages"
_MANIFEST = json.loads((_PAGES / "manifest.json").read_text())
_READINGS = json.loads((_PAGES / "readings.json").read_text(encoding="utf-8"))
_GRAPHS: dict[str, dict] = {}


def _recorded(name: str) -> dict:
    """The PageGraph of one recorded page (cached; parsed once per run)."""
    if name not in _GRAPHS:
        raw = gzip.decompress((_PAGES / f"{name}.html.gz").read_bytes())
        url = _MANIFEST[name]["url"]
        _GRAPHS[name] = pagegraph.graph_from_extract(
            url, jail_extract.extract(raw, url))
    return _GRAPHS[name]


def _verdict(case: dict) -> list[str]:
    return page_verify.verify_page_reading(
        _recorded(case["page"]), case["preview"],
        frame_kind=case["frame_kind"], wants=case["wants"],
        subject=case["subject"], now=datetime.fromisoformat(case["now"]),
        many=case["many"])


# ---- the labeled set ---------------------------------------------------------


@pytest.mark.parametrize("case", [c for c in _READINGS if c["set"] == "seed"],
                         ids=lambda c: c["id"])
def test_seed_readings(case: dict) -> None:
    """Every field failure the synthesis named is refused; every named
    must-accept passes."""
    reasons = _verdict(case)
    if case["expect"] == "reject":
        assert reasons, f"{case['id']}: shipped {case['preview']} ({case['note']})"
    else:
        assert reasons == [], f"{case['id']}: refused a right reading: {reasons}"


def test_labeled_set_rates() -> None:
    """Whole set, seeds + holdout: false-accept 0, false-reject <= 5%."""
    false_accept, false_reject, accepts = [], [], 0
    for case in _READINGS:
        reasons = _verdict(case)
        if case["expect"] == "reject" and not reasons:
            false_accept.append(case["id"])
        if case["expect"] == "accept":
            accepts += 1
            if reasons:
                false_reject.append((case["id"], reasons))
    assert len(_READINGS) >= 50 and accepts >= 20
    assert false_accept == [], f"shipped wrong readings: {false_accept}"
    assert len(false_reject) <= 0.05 * accepts, f"refused right readings: {false_reject}"


def test_every_rejection_names_a_reason() -> None:
    """Reasons are short plain sentences the flow can show or journal."""
    for case in _READINGS:
        for reason in _verdict(case):
            assert isinstance(reason, str) and 0 < len(reason) <= 200


# ---- the rules, one at a time (mini graphs) --------------------------------

_NOW = datetime.fromisoformat("2026-09-29T10:00:00-05:00")


def _graph(text: str = "", **layers) -> dict:
    base = {"url": "https://status.example.org/", "text": text, "title": "",
            "entities": [], "tables": [], "feeds": [], "meta": {}, "outline": [],
            "readability": {"readable": True, "kind": "ok"}}  # rules under test
    base.update(layers)
    return base


def _check(graph: dict, preview: dict, *, frame: str | None = "status",
           wants: list[str] | None = None, subject: str = "Example",
           many: bool = False, now: datetime = _NOW) -> list[str]:
    return page_verify.verify_page_reading(
        graph, preview, frame_kind=frame, wants=wants or ["status"],
        subject=subject, now=now, many=many)


_STATUS_TEXT = ("Example service status\nAll systems are running normally today. "
                "Messaging: No issues. Files: No issues. Calls: Degraded performance "
                "in the west region since 9:12 AM.")


def test_chrome_title_site_name_and_want_label_refused() -> None:
    g = _graph(_STATUS_TEXT, title="Example Status Page",
               meta={"og:site_name": "Example Inc"})
    assert _check(g, {"status": "Example Status Page"})      # the title
    assert _check(g, {"status": "Example Inc"})              # the site name
    assert _check(g, {"status": "Status Page"})              # a fragment of the title
    assert _check(g, {"status": "status"})                   # the want label itself
    assert _check(g, {"status": "No issues"}) == []


def test_chrome_self_entity_name_refused_but_event_name_is_data() -> None:
    g = _graph("Tonight: Riverside Regatta starts 7 PM. Example Radio is on air.",
               entities=[{"type": "Organization", "name": "Example Radio"},
                         {"type": "Event", "name": "Riverside Regatta"}])
    assert _check(g, {"event": "Example Radio"}, frame="current_value", wants=["event"])
    assert _check(g, {"event": "Riverside Regatta"}, frame="current_value",
                  wants=["event"], subject="regatta") == []


def test_heading_equal_value_is_data_unless_it_is_the_wants_label() -> None:
    g = _graph("Example status\nAll Systems Operational\nJackpot\nNext Estimated Jackpot\n$20 Million",
               outline=["h2: All Systems Operational", "h3: Next Estimated Jackpot"])
    assert _check(g, {"status": "All Systems Operational"}) == []
    assert _check(g, {"jackpot": "Next Estimated Jackpot"}, frame="current_value",
                  wants=["jackpot"])
    assert _check(g, {"jackpot": "$20 Million"}, frame="current_value",
                  wants=["jackpot"]) == []


def test_ungrounded_words_and_numbers_refused() -> None:
    g = _graph(_STATUS_TEXT)
    assert _check(g, {"status": "Operational"})               # word not on the page
    assert _check(g, {"status": "Degraded performance since 9:15"})  # 15 is invented
    assert _check(g, {"status": "Degraded performance in the west region"}) == []


def test_many_want_needs_three_rows() -> None:
    g = _graph("Example forum top posts\nFirst post title here\nSecond post title here\n"
               "Third post title here\nFourth post", title="Forum")
    assert _check(g, {"posts": "First post title here"}, frame="latest_items",
                  wants=["posts"], many=True)
    assert _check(g, {"posts": ["First post title here", "Second post title here"]},
                  frame="latest_items", wants=["posts"], many=True)
    rows = ["First post title here", "Second post title here", "Third post title here"]
    assert _check(g, {"posts": rows}, frame="latest_items", wants=["posts"],
                  many=True) == []
    assert _check(g, {"rows": [{"t": r} for r in rows]}, frame="latest_items",
                  wants=["posts"], many=True) == []


def test_next_event_time_must_be_ahead_in_the_pages_zone() -> None:
    text = ("Old Faithful\nStart Time (US/Mountain)\nPredicted Next Eruption\n"
            "Today at 0952 ± 13 minutes\nLast eruption 29 Sep 2026 @ 0807\n"
            "Note: 21 Jun 2026 @ 2054 calling it")
    g = _graph(text, title="Old Faithful Geyser")
    mdt_915 = datetime.fromisoformat("2026-09-29T09:15:00-06:00")
    cdt_1015 = datetime.fromisoformat("2026-09-29T10:15:00-05:00")  # same instant
    kw = {"frame": "next_event", "wants": ["next eruption"], "subject": "Old Faithful"}
    assert _check(g, {"next_eruption": "Today at 0952 ± 13 minutes"}, now=mdt_915, **kw) == []
    # the user's clock is CDT; the page's times are Mountain — still ahead
    assert _check(g, {"next_eruption": "Today at 0952 ± 13 minutes"}, now=cdt_1015, **kw) == []
    assert _check(g, {"next_eruption": "29 Sep 2026 @ 0807"}, now=mdt_915, **kw)
    assert _check(g, {"next_eruption": "21 Jun 2026 @ 2054"}, now=mdt_915, **kw)
    later = datetime.fromisoformat("2026-09-29T10:30:00-06:00")
    assert _check(g, {"next_eruption": "Today at 0952 ± 13 minutes"}, now=later, **kw)


def test_next_event_grace_and_horizon() -> None:
    g = _graph("Launch window opens 2026-09-29T14:50:00Z. Backup 2028-01-01T00:00:00Z.")
    kw = {"frame": "next_event", "wants": ["launch time"], "subject": "launch"}
    now = datetime.fromisoformat("2026-09-29T15:00:00+00:00")
    assert _check(g, {"launch_time": "2026-09-29T14:50:00Z"}, now=now, **kw) == []  # grace
    assert _check(g, {"launch_time": "2028-01-01T00:00:00Z"}, now=now, **kw)        # > 400 d
    assert _check(g, {"launch_time": "soon"}, now=now, **kw)                        # no time


def test_result_needs_teams_points_and_date() -> None:
    g = _graph("Final: Alabama 49, South Carolina 18 on Sat Sep 26, 2026 at "
               "Bryant-Denny Stadium.", title="Scores")
    kw = {"frame": "result", "wants": ["score"], "subject": "Alabama football"}
    assert _check(g, {"score": "49-18"}, **kw)
    assert _check(g, {"score": "Alabama 49, South Carolina 18"}, **kw)  # no date
    assert _check(g, {"score": "Alabama 49, South Carolina 18 on Sat Sep 26, 2026"},
                  **kw) == []


def test_subject_must_be_on_the_page() -> None:
    g = _graph("Service health for all regions. Everything is operating normally.")
    assert _check(g, {"status": "operating normally"}, subject="AWS us-east-1")
    g2 = _graph("eu-west-1: operating normally. AWS health dashboard.")
    assert _check(g2, {"status": "operating normally"}, subject="AWS us-east-1")
    g3 = _graph("AWS health: us-east-1 operating normally.")
    assert _check(g3, {"status": "operating normally"}, subject="AWS us-east-1") == []


# D9 (review 2026-10-03): model subjects are often plural; the page says the
# singular (and the other way round). Both directions fold, -ies/-y too.
@pytest.mark.parametrize(("subject", "text"), [
    ("tropical storms", "Tropical Storm Milton is moving north at 12 mph."),
    ("Old Faithful eruption predictions", "Old Faithful eruption prediction: 10:42 AM ±10 min."),
    ("Powerball jackpots", "Powerball jackpot: $409 Million."),
    ("battery recalls", "Battery recall issued for model X."),
    ("battery", "Recalled batteries: model X and Y."),
    ("movies", "Movie showtimes for tonight."),
    ("geysers", "Geyser activity in the Upper Basin."),
    ("watches", "Watch for the tsunami warning area."),
    ("tides", "Tide chart for Charleston Harbor."),
    ("ferries", "Ferry departures from Seattle."),
    ("storm", "Storms are expected tonight."),
])
def test_subject_plural_folds_both_ways(subject: str, text: str) -> None:
    assert page_verify._subject_reasons(subject, text) == []


@pytest.mark.parametrize(("subject", "text"), [
    ("glass", "Glas Restaurant."),                # -ss never folds
    ("gas", "Atlanta, GA weather."),              # too short to fold
    ("bus", "BU campus map."),                    # -us never folds
    ("analysis", "One analysi."),                 # -is never folds
    ("storms", "Stormy seas ahead."),             # a different word
    ("batteries", "Battering ram."),
])
def test_subject_fold_does_not_overreach(subject: str, text: str) -> None:
    assert page_verify._subject_reasons(subject, text) != []


def test_news_want_does_not_fold_to_new() -> None:
    assert page_verify.has_evidence(_graph("What is new this week."), ["news"], "") is False


def test_plural_subject_full_gate_and_has_evidence() -> None:
    g = _graph("Tropical Storm Milton: maximum sustained winds 65 mph, moving north.")
    kw = {"frame": "current_value", "wants": ["winds"], "subject": "tropical storms"}
    assert _check(g, {"winds": "65 mph"}, **kw) == []
    assert page_verify.has_evidence(g, ["track"], "tropical storms") is True


def test_definition_section_is_not_current_data() -> None:
    text = ("Metro Alerts\nRed Line: single tracking between A and B.\n"
            "How to read Metro alerts\nDelay - trains are running behind their "
            "normal spacing because of an earlier incident.")
    g = _graph(text, outline=["h1: Metro Alerts", "h2: How to read Metro alerts"])
    kw = {"frame": "alerts", "wants": ["delays"], "subject": "red line"}
    assert _check(g, {"delays": "trains are running behind their normal spacing"}, **kw)
    assert _check(g, {"delays": "single tracking between A and B"}, **kw) == []


def test_past_record_lists_and_normals_are_not_current() -> None:
    g = _graph("Houston outages\n- Sep 28: 4,333 customers out, restored in 12 hours\n"
               "- Sep 19: 7,129 customers out, restored in 8.7 days\n"
               "Right now: 530 customers out. The resort gets over 200 inches of "
               "annual snowfall; 3 inches of new snow expected Friday.")
    kw = {"frame": "status", "wants": ["outages"], "subject": "Houston"}
    assert _check(g, {"outages": "Sep 28: 4,333 customers out; Sep 19: 7,129 customers out"}, **kw)
    assert _check(g, {"outages": "530 customers out"}, **kw) == []
    kw = {"frame": "forecast", "wants": ["snowfall"], "subject": "resort"}
    assert _check(g, {"snowfall": "over 200 inches of annual snowfall"}, **kw)
    assert _check(g, {"snowfall": "3 inches of new snow expected Friday"}, **kw) == []


def test_news_article_is_not_a_current_reading_but_serves_news_asks() -> None:
    g = _graph("Powerball jackpot jumps to $389M for Monday. The next drawing is "
               "Monday at 10:59 PM.", title="Powerball jackpot jumps to $389M",
               entities=[{"type": "NewsArticle", "headline": "Powerball jackpot jumps"}])
    assert _check(g, {"jackpot": "$389M"}, frame="current_value", wants=["jackpot"],
                  subject="Powerball")
    assert _check(g, {"story": "Powerball jackpot jumps to $389M for Monday"},
                  frame="latest_items", wants=["story"], subject="Powerball") == []


def test_unreadable_page_refused_before_anything_else() -> None:
    shell = _graph("", title="Reddit")
    shell.pop("readability")  # computed: an empty page is a shell
    assert _check(shell, {"posts": ["a", "b", "c"]}, frame="latest_items",
                  wants=["posts"], many=True)


def test_preview_title_key_is_not_a_reading() -> None:
    """The flow adds the card title to the preview; it is never verified as a value."""
    g = _graph(_STATUS_TEXT, title="Example Status Page")
    assert _check(g, {"status": "No issues", "title": "Example Status Page"}) == []


# F2b (review 2026-10-04): the interpreted tier (default) grounds only against
# what the model was shown — body text + tables; a value found only in meta or
# JSON-LD entities isn't grounded. The compiled tier still reads those (it lifts
# values verbatim from entities / tables / meta).
def test_interpreted_tier_does_not_ground_against_meta_or_entities() -> None:
    g = _graph("Powerball jackpot information updated for every drawing.",
                entities=[{"type": "Event", "name": "Powerball drawing",
                           "offers.price": "777"}],
                meta={"og:description": "Jackpot 999 Million"})
    kw = {"frame": "current_value", "wants": ["jackpot"], "subject": "Powerball"}
    assert _check(g, {"jackpot": "777"}, **kw)              # only in entities
    assert _check(g, {"jackpot": "999 Million"}, **kw)      # only in meta
    compiled = page_verify.verify_page_reading(
        g, {"jackpot": "777"}, frame_kind="current_value", wants=["jackpot"],
        subject="Powerball", now=_NOW, many=False, tier="compiled")
    assert compiled == []


# ---- has_evidence: the pre-model check -------------------------------------


@pytest.mark.parametrize(("page", "wants", "subject", "expect"), [
    ("flightaware_aal100", ["status"], "AA 100", False),     # a feedback modal
    ("health_aws_status", ["status"], "AWS us-east-1", False),  # empty shell
    ("reddit_worldnews_new", ["top posts"], "r/worldnews", False),
    ("docs_aws_health_dashboard", ["status"], "AWS us-east-1", True),  # says 'status'
    ("powerball_home", ["jackpot"], "Powerball", True),
    ("slack_status", ["status"], "Slack", True),
    ("wmata_red_status", ["delays"], "DC metro red line", True),
    ("art_python_tutorial", ["jackpot"], "Powerball", False),
])
def test_has_evidence_on_recorded_pages(page, wants, subject, expect) -> None:
    assert page_verify.has_evidence(_recorded(page), wants, subject) is expect


def test_grounding_is_the_engines_rule() -> None:
    """Words and numbers are grounded by ni.ground_values — one rule for the
    build and every refresh."""
    g = _recorded("lagcheck_slack")
    assert _check(g, {"status": "Operational"}, subject="Slack")
    assert _check(g, {"status": "Slack is working normally"}, subject="Slack") == []
    g = _recorded("powerball_home")
    kw = {"frame": "current_value", "wants": ["jackpot"], "subject": "Powerball"}
    assert _check(g, {"jackpot": "$409 Million"}, **kw) == []
    assert _check(g, {"jackpot": "$410 Million"}, **kw)
