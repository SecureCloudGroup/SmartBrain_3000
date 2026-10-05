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
           many: bool = False, now: datetime = _NOW,
           window: str | None = None, tier: str = "interpreted") -> list[str]:
    return page_verify.verify_page_reading(
        graph, preview, frame_kind=frame, wants=wants or ["status"],
        subject=subject, now=now, many=many, tier=tier, window=window)


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


# F6-A (blind-5): a compiled page card shipped "h3: Traffic & Road Conditions" for
# "I-70 road conditions Colorado". The outline label carried its jail "h3:" prefix,
# and the chrome check didn't normalize the prefix off the value when comparing to
# the (prefix-stripped) heading set — so a heading restating the ask slipped past.
def test_heading_label_with_h_level_prefix_is_refused_as_a_label() -> None:
    g = _graph("Interstate 70 - Colorado.\nTraffic and Road Conditions.\n"
               "Open with chains advised.",
               outline=["h1: Interstate 70", "h3: Traffic & Road Conditions"])
    assert _check(g, {"road_conditions": "h3: Traffic & Road Conditions"},
                  frame="status", wants=["road conditions"],
                  subject="Interstate 70")
    # the actual data line still ships
    assert _check(g, {"road_conditions": "Open with chains advised"},
                  frame="status", wants=["road conditions"],
                  subject="Interstate 70") == []


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


# fix7-page (blind-6): Yahoo Finance shipped "S&P 500 INDEX (^SPX)" as the value
# for "how's the S&P doing today" — a whole-word substring of the page's own title
# that embeds a digit (500). The chrome check must still refuse it; a lone number
# ("65") still ships (3+ alpha-char requirement).
def test_chrome_substring_with_embedded_digits_refused_but_pure_number_ships() -> None:
    g = _graph("Chicago Options - Delayed Quote USD. S&P 500 INDEX (^SPX) 7,722.72 +56.27.",
               title="S&P 500 INDEX (^SPX) Charts, Data & News - Yahoo Finance")
    assert _check(g, {"value": "S&P 500 INDEX (^SPX)"},
                  frame="current_value", wants=["value"], subject="S&P 500")
    # a bare number isn't a chrome fragment, even if it appears inside the title
    assert _check(g, {"value": "7,722.72"},
                  frame="current_value", wants=["value"], subject="S&P 500") == []


# fix7-page (blind-6): findarepo compiled card picked an ItemList entity's own
# ``name`` field ("Trending Python repositories") — a LABEL for the list, not an
# item of it. List/collection-type entities must count as chrome.
def test_list_entity_name_is_chrome_label_not_a_reading() -> None:
    g = _graph("Trending Python Repos — Daily Star Rankings.\nTheAlgorithms/Python.",
               title="Trending Python GitHub Repos — Daily Star Rankings | findarepo",
               entities=[{"type": "ItemList", "name": "Trending Python repositories"}])
    assert _check(g, {"trending_python_repos": "Trending Python repositories"},
                  frame="latest_items", wants=["trending python repos"],
                  subject="Python repositories", tier="compiled")


# fix7-page (blind-6): flight-status.com shipped "San Francisco (SFO) 2026-06-30T07:00"
# for "flight status DL 405" — a 96-day-stale flight time. A current/status reading
# that is a bare clock-timestamped event parsed well past now must refuse; a status
# word in the reading ("prohibited" on a burn-ban "As of 8/11/26, ...") keeps the
# standing-order reading from refusing.
def test_stale_clock_timestamp_in_current_reading_refuses() -> None:
    text = ("DL 405 takes off from San Francisco (SFO) 2026-06-30T07:00 to JFK. "
            "As of 8/11/26, outdoor burning is prohibited in Travis County. "
            "In effect since 8/11/26.")
    g = _graph(text, title="Delta DL405 Flight Status : Live Tracking & Updates",
               subject="DL 405")
    now = datetime.fromisoformat("2026-10-04T10:00:00+00:00")
    kw = {"frame": "status", "wants": ["status"], "subject": "DL 405", "now": now}
    assert _check(g, {"status": "San Francisco (SFO) 2026-06-30T07:00"}, **kw)
    # a status word in the reading rides through: a burn ban still ships
    assert _check(g, {"status": "As of 8/11/26, outdoor burning is prohibited in Travis County."},
                  **kw) == []
    # date-only readings (no clock) ride through too — not a bare timestamp
    assert _check(g, {"status": "In effect since 8/11/26"}, **kw) == []


# fix7-page (blind-6): cityvibe.me's static guide answered "line at Franklin Barbecue
# right now" with "50 to 100 people" — a page with NO freshness signal. A "right now"
# ask against a current/status frame requires an updated / as-of / minutes-ago phrase
# or an entity date within 48 h or today's date on the page.
def test_right_now_ask_against_page_with_no_freshness_signal_refuses() -> None:
    text = ("Franklin Barbecue is famous for its long lines, especially during "
            "peak hours. 50 to 100 people typically wait in the morning.")
    g = _graph(text, title="How long is the line at Franklin Barbecue?")
    now = datetime.fromisoformat("2026-10-04T10:00:00+00:00")
    kw = {"frame": "current_value", "wants": ["current line length"],
          "subject": "Franklin Barbecue", "now": now, "window": "now"}
    assert _check(g, {"current_line_length": "50 to 100 people"}, **kw)
    # a page that carries an updated phrase ships (an 'as of' line is a freshness signal)
    g_fresh = _graph(text + "\nLast updated 15 minutes ago.",
                     title="How long is the line at Franklin Barbecue?")
    assert _check(g_fresh, {"current_line_length": "50 to 100 people"}, **kw) == []
    # a page that carries an entity dateModified within 48 h ships
    g_mod = _graph(text, entities=[{"type": "WebPage",
                                     "dateModified": "2026-10-04T08:00:00+00:00"}])
    assert _check(g_mod, {"current_line_length": "50 to 100 people"}, **kw) == []
    # an ask that doesn't name a 'right now' window never fires the check
    kw_no_window = {**kw, "window": None}
    assert _check(g, {"current_line_length": "50 to 100 people"},
                  **kw_no_window) == []


# F6-ISS (blind-5): "ISS passes over Tucson" shipped "Saturday, Oct 10" (a date, no
# clock) for a next_event pass want. C9: a next event needs a time still to come —
# a date-only reading on a time-typed key must refuse under next_event / schedule.
def test_next_event_date_only_reading_on_time_typed_key_refuses() -> None:
    g = _graph("ISS passes over Tucson. Next pass Saturday, Oct 10 at 6:12 PM.",
               title="ISS over Tucson")
    kw = {"frame": "next_event", "wants": ["pass"], "subject": "ISS over Tucson"}
    now = datetime.fromisoformat("2026-10-04T10:00:00-07:00")
    assert _check(g, {"pass": "Saturday, Oct 10"}, now=now, **kw)
    # a time of day on the same ask ships
    assert _check(g, {"pass": "Saturday, Oct 10 at 6:12 PM"}, now=now, **kw) == []


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


# fix8 (blind-7, 2026-10-04): mlb.com shipped "2026 Schedule (PDF)" as the Durham
# Bulls schedule value — a download-link label, not a value. The file-ext suffix
# refuses any reading ending in "(PDF)" / "(ICS)" / "(XLSX)" / …; call-to-action
# labels ("Download", "View schedule", "Click here", "Learn more") refuse too.
def test_file_ext_suffix_reading_is_refused_as_a_download_label() -> None:
    text = ("Durham Bulls 2026 Schedule. The season begins in April. "
            "Download the full schedule or view game-by-game dates. 2026 Schedule (PDF)")
    g = _graph(text, title="Durham Bulls Schedule")
    kw = {"frame": "schedule", "wants": ["schedule"], "subject": "Durham Bulls"}
    assert _check(g, {"schedule": "2026 Schedule (PDF)"}, **kw)
    assert _check(g, {"schedule": "2026 Schedule (ICS)"}, **kw)
    # a real data line still ships (grounded, not a label)
    assert _check(g, {"schedule": "The season begins in April"}, **kw) == []


@pytest.mark.parametrize("label", [
    "Download", "Click here", "Learn more", "View schedule", "View all",
    "See more", "Open PDF", "View details",
])
def test_bare_call_to_action_labels_are_refused(label: str) -> None:
    text = f"Durham Bulls Schedule page.\n{label}\nThe season begins in April."
    g = _graph(text, title="Durham Bulls Schedule")
    kw = {"frame": "schedule", "wants": ["schedule"], "subject": "Durham Bulls"}
    assert _check(g, {"schedule": label}, **kw)


# fix8 (blind-7, 2026-10-04): "line at Franklin Barbecue rn" shipped "on average,
# 3 to 5 hours long" because the freshness gate keyed on window=now. The window
# now parses "rn" / "atm" / "right this minute"; and a reading phrased as a
# typical / average value never answers a 'now' ask, under any frame_kind.
def test_right_now_ask_against_typical_average_reading_refuses() -> None:
    text = ("Franklin Barbecue is famous for its long lines. The line for lunch "
            "is, on average, 3 to 5 hours long. Last updated 15 minutes ago. "
            "There are about 30 people in line this morning. Typically 50 to 100 "
            "people wait in the morning.")
    g = _graph(text, title="How long is the line at Franklin Barbecue?")
    now = datetime.fromisoformat("2026-10-04T10:00:00+00:00")
    kw = {"frame": "current_value", "wants": ["line length"],
          "subject": "Franklin Barbecue", "now": now, "window": "now"}
    # 'on average' refuses even with a fresh signal on the page
    assert _check(g, {"line_length": "on average, 3 to 5 hours long"}, **kw)
    assert _check(g, {"line_length": "typically 50 to 100 people"}, **kw)
    # a non-'now' ask (no window) doesn't fire the check
    kw_no_window = {**kw, "window": None, "frame": "forecast"}
    assert _check(g, {"line_length": "on average, 3 to 5 hours long"}, **kw_no_window)
    # a straight current reading still ships
    assert _check(g, {"line_length": "about 30 people in line"}, **kw) == []


# fix8 (blind-7, 2026-10-04): "pollen count atlanta tomorrow" shipped a page titled
# "Pollen Count on 2026-10-04" — today's date, not tomorrow's. The title's own date
# is pure code to parse; a day-window ask against a page whose title names a date
# that doesn't match must refuse.
def test_day_window_mismatch_against_page_title_date_refuses() -> None:
    text = ("Atlanta pollen count published daily. Today's count is 0/5.\n"
            "The pollen count for the day is reported each morning.")
    g = _graph(text, title="Pollen Count on 2026-10-04 | Atlanta Allergy & Asthma")
    now = datetime.fromisoformat("2026-10-04T10:00:00-04:00")
    kw = {"frame": "current_value", "wants": ["pollen count"],
          "subject": "Atlanta pollen", "now": now, "window": "tomorrow"}
    reasons = _check(g, {"pollen_count": "0/5"}, **kw)
    assert reasons and any("2026-10-04" in r for r in reasons), reasons
    # a 'today' ask matches today's dated page
    kw_today = {**kw, "window": "today"}
    assert _check(g, {"pollen_count": "0/5"}, **kw_today) == []
    # a page with no date on it doesn't fire the check
    g_no_date = _graph(text, title="Atlanta Pollen Count Today")
    assert _check(g_no_date, {"pollen_count": "0/5"}, **kw) == []
    # a weekend ask against a dated Saturday page ships
    g_sat = _graph(text, title="Pollen Count on 2026-10-10 | Atlanta Allergy & Asthma")
    kw_wkd = {**kw, "window": "weekend"}
    assert _check(g_sat, {"pollen_count": "0/5"}, **kw_wkd) == []
