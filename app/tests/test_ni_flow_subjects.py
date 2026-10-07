"""§32 verify, the review round of 2026-10-03: the source must be about the subject the user named.

D1 — a source about a DIFFERENT named subject of the same kind shipped: Slack's status for "is zoom having problems",
the CTA for "WMATA red line delays", BART for "subway delays", the Dow for the S&P, Android for "ollama", MLB for "win 4",
the Marlins for "Inter Miami", the Diamondbacks for "the space station over Phoenix".
D7 — the category rule refused right cards because the keyword classify misfiles a subject ("TV schedule" reads as
sports, "oil stocks" as markets).
D8 — the stray-topic check split multi-word keywords: "red" (red flag warning) refused the Red Sox' scores.

The Library here is a REAL LibraryIndex over a slice of the pinned pack (fixtures/ni_flow_verify/pack_subjects.json:
the records, every taxonomy row, their terms and example asks). Each case runs the flow's own steps after a tap —
the declared answers it would show (``select_answers``), the source as the pick sealed it (``seal_library_pick`` →
``_picked_source``), then ``_verify_frame`` — with the intent stage 1 would hand on. No network, no model.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.secrets import gen_master_key

_PACK = Path(__file__).parent / "fixtures" / "ni_flow_verify" / "pack_subjects.json"
_NOW = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)


class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


def build_pack_index(tmp: Path) -> library_index.LibraryIndex:
    """The fixture slice as an installed Library (the schema ``sourcetool build`` writes)."""
    pack = json.loads(_PACK.read_text())
    src = tmp / "src.duckdb"
    con = duckdb.connect(str(src))
    for table, cols in pack["schema"].items():
        con.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in cols)})")
        for row in pack["rows"][table]:
            con.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in cols)})", row)
    con.close()
    payload = gzip.compress(src.read_bytes())
    idx = library_index.LibraryIndex(tmp / "data", netguard_mod=_Net(payload),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(payload).hexdigest()})
    idx.install()
    return idx


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> library_index.LibraryIndex:
    return build_pack_index(tmp_path_factory.mktemp("subjects"))


@pytest.fixture(autouse=True)
def _wired(lib, monkeypatch):
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


def _intent(ask: str, subject: str, wants: list[str], place: str | None = None) -> dict:
    return {"kind": "external_data", "subject": subject, "cadence_minutes": 15, "wants": wants, "threshold": None,
            "place": place, "display_hint": "value", "frame_kind": library_index.frame_kind_from_text(ask),
            "window": ni_flow._window_from_text(ask)}


def verdict(ask: str, subject: str, wants: list[str], sid: str, *, place: str | None = None,
            params: dict | None = None, label: str = "", others: tuple = ()) -> list[str]:
    """The reasons ``_verify_frame`` refuses ``sid`` for the ask ([] = it ships), after a tap on its row (with
    ``others``: the pick's other rows, as (label, params))."""
    intent, params = _intent(ask, subject, wants, place), dict(params or {})
    answers = ni_flow._library_answers(sid)
    assert answers, sid
    scoped = [a for a in answers if ni_flow._names_a_param(a, params)]
    chosen = ni_flow.select_answers(scoped, ask, wants, intent["window"], intent["frame_kind"]) if scoped else []
    if not chosen or not any(ni_flow._answer_score(a, ni_flow._answer_tokens(ask)) > 0 for a in chosen):
        chosen = ni_flow.select_answers(answers, ask, wants, intent["window"], intent["frame_kind"])
    assert chosen, f"{sid} has nothing for {ask!r}"
    if intent["frame_kind"] in ("next_event", "schedule") and chosen[0]["kind"] == "value" \
            and not any(ni_flow._event_time(a) for a in chosen):  # as the build adds the event's time
        when = next((a for a in answers if a["kind"] == "value" and ni_flow._event_time(a)), None)
        chosen = [*chosen[:3], when] if when is not None else chosen
    rows = [{"source_id": sid, "url": "https://example.org/x", "label": label, "params": params, "scope": "global"},
            *({"source_id": "other", "url": f"https://example.org/{i}", "label": lab, "params": dict(p),
               "scope": "global"} for i, (lab, p) in enumerate(others))]
    store = _store()
    item_id = ni_flow.create_shell_item(store, ask)
    rec = ni_flow._make_record(ask, "source", intent=intent)
    rec["_ranked_library"] = rows
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, rows[0]["url"], rows[0])
    live = {**ni_flow._flow_read(store, item_id), "_library_source": sid}
    built = {"chosen": chosen, "answers": answers, "preview_payload": {},
             "unanswered": ni_flow._unanswered_wants(answers, ask, wants, list(params.values()))}
    reasons, _notes = ni_flow._verify_frame(ni_flow._frame_of(ask, intent), ni_flow._picked_source(live), params,
                                            built, ask, intent, _NOW)
    return reasons


# ---- D1: another named subject of the same kind --------------------------------------------------------------

_YANKEES = ("New York Yankees (mlb)", {"team": "147"})

WRONG_SUBJECT = [
    # the review's inputs
    ("is zoom having problems", "Zoom status", ["status"], "slack-status-current", {}),
    ("Amazon Web Services status us-west-2", "Amazon Web Services status", ["status"], "slack-status-current", {}),
    ("status of HR 1", "HR 1 bill status", ["status"], "slack-status-current", {}),
    ("WMATA red line delays", "WMATA Red Line", ["delays"], "cta-alerts", {}),
    ("WMATA red line delays", "WMATA Red Line", ["delays"], "mbta-alerts", {}),
    ("subway delays", "subway", ["delays"], "bart-advisories", {}),
    ("how's the S&P doing today", "S&P 500", ["price", "change"], "fred-djia", {}),
    ("new release of ollama", "ollama", ["release"], "eol-android", {}),
    ("stars on cpython", "cpython", ["stars"], "eol-android", {}),
    ("win 4 results", "Win 4 lottery", ["results"], "mlb-schedule", {}),
    ("Inter Miami next match", "Inter Miami", ["next match"], "mlb-team-schedule",
     {"label": "Miami Marlins (mlb)", "params": {"team": "146"}}),
    ("when will the space station fly over Phoenix", "ISS passes", ["next pass time"], "mlb-team-schedule",
     {"label": "Arizona Diamondbacks (mlb)", "params": {"team": "109"}, "place": "Phoenix"}),
    # new asks of the same class
    ("is Discord down right now", "Discord status", ["status"], "slack-status-current", {}),
    ("SEPTA delays this morning", "SEPTA", ["delays"], "cta-alerts", {}),
    ("how is the nasdaq doing", "Nasdaq", ["price"], "fred-djia", {}),
    ("latest iOS version", "iOS", ["latest version"], "eol-android", {}),
    ("Powerball results", "Powerball", ["winning numbers"], "mlb-schedule", {}),
    ("2 year treasury yield", "2-year Treasury yield", ["yield"], "fred-dgs10", {}),
    ("brent oil price", "Brent crude", ["price"], "fred-dcoilwtico", {}),
    ("Seattle Sounders next match", "Seattle Sounders", ["next match"], "mlb-team-schedule",
     {"label": "Seattle Mariners (mlb)", "params": {"team": "136"}}),
    ("what was the Rangers hockey score", "New York Rangers", ["score"], "mlb-team-results",
     {"label": "Texas Rangers (mlb)", "params": {"team": "140"}}),
    ("diesel price average", "diesel", ["price"], "fred-gasregw", {}),
    ("Saturday Night Live schedule", "Saturday Night Live", ["schedule"], "mlb-schedule", {}),
    ("score of Monday Night Football", "Monday Night Football", ["score"], "nhl-score-now", {}),
    # R9 (yen→dollar + Red Sox, 2026-10-04): a league-wide source never ships for an ask that
    # names ONE team unless it filters to that team. A sibling team row sealed as a reading on
    # the pick (team-source re-pick, hand-tapped siblings) does not excuse the league source.
    ("Yankees score", "New York Yankees", ["score"], "mlb-schedule", {"others": (_YANKEES,)}),
    ("Blue Jays score tonight", "Toronto Blue Jays", ["score"], "mlb-schedule",
     {"others": (("Toronto Blue Jays (mlb)", {"team": "141"}),)}),
    ("how did the Red Sox do last night", "Boston Red Sox", ["score"], "mlb-schedule",
     {"others": (("Boston Red Sox (mlb)", {"team": "111"}),)}),
    ("Red Wings score", "Detroit Red Wings", ["score"], "nhl-score-now",
     {"others": (("Detroit Red Wings (nhl)", {"team": "17"}),)}),
]


@pytest.mark.parametrize("ask, subject, wants, sid, kw", WRONG_SUBJECT, ids=lambda x: x if isinstance(x, str) else "")
def test_a_source_about_another_named_subject_is_refused(ask, subject, wants, sid, kw) -> None:
    reasons = verdict(ask, subject, wants, sid, **kw)
    assert reasons, f"{sid} shipped for {ask!r}"


RIGHT_SUBJECT = [
    ("is Slack down", "Slack status", ["status"], "slack-status-current", {}),
    ("is Slack degraded", "Slack status", ["status"], "slack-status-current", {}),
    ("CTA disruption", "CTA", ["delays"], "cta-alerts", {}),
    ("inflation index latest", "CPI", ["index value"], "fred-cpiaucsl", {}),
    ("BART delays", "BART", ["delays"], "bart-advisories", {}),
    ("CTA red line delays", "CTA Red Line", ["delays"], "cta-alerts", {}),
    ("is the red line running", "red line", ["status"], "cta-alerts", {}),
    ("Chicago L delays", "Chicago L trains", ["delays"], "cta-alerts", {}),
    ("how is the Dow doing", "Dow Jones", ["price"], "fred-djia", {}),
    ("how's the S&P doing today", "S&P 500", ["price", "change"], "fred-sp500", {}),
    ("latest Android version", "Android", ["latest version"], "eol-android", {}),
    ("baseball games tonight", "baseball", ["games"], "mlb-schedule", {}),
    # R9 (2026-10-04): "Yankees score" with a team-source sibling now REFUSES on mlb-schedule
    # (league-wide, no team filter). See WRONG_SUBJECT.
    ("MLB scores for the Red Sox and Yankees", "Red Sox Yankees MLB scores", ["scores"], "mlb-schedule", {}),
    ("what was the Rangers hockey score", "New York Rangers", ["score"], "nhl-score-now", {}),
    ("Marlins next game", "Miami Marlins", ["next game"], "mlb-team-schedule",
     {"label": "Miami Marlins (mlb)", "params": {"team": "146"}}),
    ("Rangers score", "Texas Rangers", ["score"], "mlb-team-results",
     {"label": "Texas Rangers (mlb)", "params": {"team": "140"}}),
    ("Lakers next game", "Los Angeles Lakers", ["next game"], "thesportsdb-team-next",
     {"label": "Los Angeles Lakers (nba)", "params": {"team": "134867"}}),
    ("Inter Miami next match", "Inter Miami", ["next match"], "thesportsdb-team-next",
     {"label": "Inter Miami CF (usa.1)", "params": {"team": "137699"}}),
    ("epl standings", "EPL", ["standings"], "thesportsdb-league-table",
     {"label": "English Premier League (epl)", "params": {"league_id": "4328"}}),
    ("American League wild card picture", "AL wild card standings", ["standings"], "mlb-wildcard-standings", {}),
    ("win 4 results", "Win 4 lottery", ["results"], "ny-numbers-win4", {}),
    ("gas prices", "gas prices", ["price"], "fred-gasregw", {}),
    ("price of crude oil", "crude oil", ["price"], "fred-dcoilwtico", {}),
    ("jobless claims", "initial jobless claims", ["claims"], "fred-icsa", {}),
    ("geomagnetic storm forecast", "geomagnetic storm", ["kp forecast"], "swpc-kp-forecast", {}),
    ("aurora forecast for Anchorage tonight", "aurora", ["kp"], "swpc-kp-forecast",
     {"others": (("Aurora (CO)", {"lat": "39.7", "lon": "-104.7"}),)}),
]


@pytest.mark.parametrize("ask, subject, wants, sid, kw", RIGHT_SUBJECT, ids=lambda x: x if isinstance(x, str) else "")
def test_a_source_about_the_named_subject_ships(ask, subject, wants, sid, kw) -> None:
    assert verdict(ask, subject, wants, sid, **kw) == []


def test_a_filled_subject_counts_only_for_a_kind_the_ask_is_about(lib) -> None:
    """"Phoenix" fills a baseball team; "the space station over Phoenix" is not baseball, so the fill doesn't
    exempt it from the category rule (it did: the Diamondbacks' schedule shipped)."""
    reasons = verdict("when will the space station fly over Phoenix", "ISS passes", ["next pass time"],
                      "mlb-team-schedule", place="Phoenix", label="Arizona Diamondbacks (mlb)", params={"team": "109"})
    assert any(r.startswith("it is sports data") for r in reasons), reasons


def test_the_pick_keeps_the_librarys_readings_across_re_picks() -> None:
    store = _store()
    item_id = ni_flow.create_shell_item(store, "Yankees score")
    rows = [{"source_id": "mlb-team-results", "url": "https://example.org/a", "label": "New York Yankees (mlb)",
             "params": {"team": "147"}, "scope": "global"},
            {"source_id": "nws-forecast", "url": "https://example.org/b", "label": "Aurora (CO)",
             "params": {"lat": "1", "lon": "2"}, "scope": "global"},
            {"source_id": "mlb-schedule", "url": "https://example.org/c", "label": "", "params": {}, "scope": "global"}]
    rec = ni_flow._make_record("Yankees score", "source", intent=_intent("Yankees score", "Yankees", ["score"]))
    rec["_ranked_library"] = rows
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, rows[0]["url"], rows[0])
    rec = ni_flow._flow_read(store, item_id)
    rec["_ranked_library"] = rows[2:]  # the first row was refused: the pick re-lands without it
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, rows[2]["url"], rows[2])
    live = ni_flow._flow_read(store, item_id)
    assert live["_library_label"] == "" and live["_library_readings"] == ["New York Yankees (mlb)"]


# ---- D7: the keyword classify misfiles a subject ---------------------------------------------------------------

MISFILED = [
    # the review's inputs (each the source's own example ask)
    ("what's on network TV tonight", "TV schedule", ["shows"], "tvmaze-schedule", {}),
    ("oil stocks report", "crude oil stocks", ["stocks"], "eia-petroleum-stocks", {}),
    ("price of eggs", "eggs", ["price"], "fred-apu0000708111", {}),
    ("storm surge warning Galveston", "storm surge warning", ["warnings"], "nws-tropical-alerts-point",
     {"place": "Galveston", "params": {"lat": "29.3", "lon": "-94.8"}}),
    ("when is the next Severance episode", "Severance", ["next episode"], "tvmaze-show-next",
     {"params": {"show": "Severance"}, "label": "Severance"}),
    ("the bbc on world events", "BBC world news", ["headlines"], "bbc-world", {}),
    ("wall street journal world", "Wall Street Journal world news", ["headlines"], "wsj-world", {}),
    # new asks of the same class
    ("gas inventories this week", "natural gas inventories", ["storage"], "eia-natgas-storage", {}),
    ("TV listings for tonight", "TV listings", ["shows"], "tvmaze-schedule", {}),
    ("crude oil inventories", "crude oil inventories", ["stocks"], "eia-petroleum-stocks", {}),
    ("price of milk", "milk", ["price"], "fred-apu0000709112", {}),
    ("coffee prices", "coffee", ["price"], "fred-apu0000717311", {}),
    ("nyt world news", "NYT world news", ["headlines"], "nyt-world", {}),
]


@pytest.mark.parametrize("ask, subject, wants, sid, kw", MISFILED, ids=lambda x: x if isinstance(x, str) else "")
def test_a_misfiled_subject_does_not_refuse_its_own_source(ask, subject, wants, sid, kw) -> None:
    reasons = verdict(ask, subject, wants, sid, **kw)
    assert not any(" data, not " in r for r in reasons), reasons


OTHER_KIND = [
    ("will it storm tonight", "weather", ["storms"], "swpc-kp-forecast"),
    ("river level in Boise", "Boise River", ["river level"], "fred-cpiaucsl"),
    ("is it gonna storm in Tulsa tonight", "weather", ["thunderstorms"], "swpc-kp-forecast"),
    ("pollen count in Atlanta", "pollen", ["pollen count"], "eia-natgas-storage"),
    ("on this day in history", "on this day in history", ["events"], "fred-cpiaucsl"),
]


@pytest.mark.parametrize("ask, subject, wants, sid", OTHER_KIND)
def test_another_kind_of_data_is_still_refused(ask, subject, wants, sid) -> None:
    assert verdict(ask, subject, wants, sid), f"{sid} shipped for {ask!r}"


# ---- D8: a multi-word keyword counts only whole ----------------------------------------------------------------

def test_a_word_of_a_phrase_is_no_topic_unless_the_phrase_was_said(lib) -> None:
    frame = ni_flow._frame_of("MLB scores for the Red Sox and Yankees",
                              _intent("MLB scores for the Red Sox and Yankees", "Red Sox Yankees", ["scores"]))
    source = ni_flow._picked_source({"_library_source": "mlb-schedule"})
    answers = ni_flow._library_answers("mlb-schedule")
    ask = "MLB scores for the Red Sox and Yankees"
    assert ni_flow._stray_topics(frame, source, {}, answers, ask,
                                  _intent(ask, "Red Sox Yankees", ["scores"])) == set()
    # said whole, the phrase is a topic ("red flag warning" is a weather alert, not a forecast)
    ask = "red flag warning near Boise"
    stray = ni_flow._stray_topics(ni_flow._frame_of(ask, _intent(ask, "red flag warning", ["warning"])),
                                  ni_flow._picked_source({"_library_source": "fred-gasregw"}),
                                  {}, ni_flow._library_answers("fred-gasregw"), ask,
                                  _intent(ask, "red flag warning", ["warning"]))
    assert {"red", "flag"} <= stray


PHRASE_WORDS = [
    # the review's inputs
    ("MLB scores for the Red Sox and Yankees", "Red Sox Yankees MLB scores", ["scores"], "mlb-schedule", {}),
    ("wildfires in California", "California wildfires", ["fires"], "nifc-current-fires",
     {"params": {"state": "CA"}, "label": "California (CA)"}),
    # R9 (2026-10-04): "Blue Jays tonight" / "Red Sox last night" / "Red Wings score" on a
    # league-wide source with a team-source SIBLING sealed as a reading now refuses — a
    # league source without the team filter never ships for the named team (see WRONG_SUBJECT).
    ("southern California wildfires", "southern California wildfires", ["fires"], "nifc-current-fires",
     {"params": {"state": "CA"}, "label": "California (CA)"}),
    ("Blue Origin launch schedule", "Blue Origin launches", ["next launch"], "jolpica-f1-next", {}),
]


@pytest.mark.parametrize("ask, subject, wants, sid, kw", PHRASE_WORDS[:-1], ids=lambda x: x if isinstance(x, str) else "")
def test_a_word_of_an_unsaid_phrase_never_refuses(ask, subject, wants, sid, kw) -> None:
    reasons = verdict(ask, subject, wants, sid, **kw)
    assert not any(r.startswith("it isn't about") for r in reasons), reasons
    assert reasons == [], reasons


def test_a_phrase_said_whole_is_a_topic() -> None:
    """"Blue Origin" said whole is a launch topic: the F1 schedule isn't about it."""
    ask, subject, wants, sid, kw = PHRASE_WORDS[-1]
    reasons = verdict(ask, subject, wants, sid, **kw)
    assert "it isn't about blue, origin" in reasons, reasons


# ---- D5: forward-only windows erased right cards ---------------------------------------------------------------

@pytest.mark.parametrize("ask, window", [
    # the review's inputs: a name with a day word is no window
    ("Saturday Night Live schedule", None), ("score of Monday Night Football", None),
    ("Black Friday game deals", None),
    # more names of the class
    ("Super Tuesday results", None), ("Cyber Monday laptop deals", None), ("Good Friday mass times", None),
    ("Fat Tuesday parade route", None), ("Thursday Night Football score", None),
    # a real window beside such a name still counts; a plain day word still does
    ("Sunday Night Football tonight", "tonight"), ("Black Friday deals this weekend", "weekend"),
    ("weather Saturday night", "dow:sat"), ("is it going to rain on Friday", "dow:fri"),
])
def test_a_name_with_a_day_word_is_no_window(ask, window) -> None:
    assert ni_flow._window_from_text(ask) == window


# fix8 (blind-7, 2026-10-04): the ask said "rn" and the freshness gate missed
# it. "rn" / "atm" slang + "right this (minute|second|moment|instant)" / "this
# instant" are now 'now'. Short tokens match only at utterance end so an ATM
# machine ("open atm near me") doesn't false-trigger.
@pytest.mark.parametrize("ask, window", [
    ("how long is the line at Franklin Barbecue rn", "now"),
    ("line at Franklin Barbecue atm", "now"),
    ("line rn.", "now"),
    ("temperature right this minute", "now"),
    ("price right this second", "now"),
    ("weather this instant", "now"),
    # false-positive guards
    ("open atm near me", None),
    ("the atms are empty", None),
    ("rn traffic", None),
    ("live oak weather", None),
    ("live music tonight", "tonight"),
    ("is my package live", None),
])
def test_slang_now_parses_without_overtriggering(ask, window) -> None:
    assert ni_flow._window_from_text(ask) == window


def _pick(ask: str, sid: str, wants: tuple = ()) -> list[str]:
    answers = ni_flow._library_answers(sid)
    window = ni_flow._window_from_text(ask)
    return [a["name"] for a in ni_flow.select_answers(answers, ask, list(wants), window,
                                                       library_index.frame_kind_from_text(ask))]


@pytest.mark.parametrize("ask, sid, shown", [
    # the review's inputs (each "nothing" before)
    ("moon phase tonight", "usno-moon-today", "moon_phase"),
    ("top songs this week", "apple-top-songs", "chart"),
    ("big quakes this week", "usgs-quakes-45-week", "quakes"),
    ("asteroids passing earth this week", "jpl-close-approaches", None),
    ("nba games tonight", "thesportsdb-next-league", "upcoming"),
    ("gas prices this week", "fred-gasregw", None),
    ("mortgage rates this week", "fred-mortgage30us", None),
    ("initial jobless claims this week", "fred-icsa", None),
    # more of the class
    ("how big is the mega millions jackpot tonight", "ny-lottery-megamillions", None),
    ("is it a full moon tonight", "usno-moon-today", None),
    ("FDA drug recalls this week", "openfda-drug-recalls", None),
    ("solar flare activity this week", "swpc-alerts", None),
    ("coffee prices this week", "fred-apu0000717311", None),
])
def test_a_slow_value_or_a_current_list_serves_this_week_and_tonight(ask, sid, shown) -> None:
    picked = _pick(ask, sid)
    assert picked, f"{sid} has nothing for {ask!r}"
    if shown:
        assert shown in picked, picked


def test_a_fast_moving_value_still_doesnt_stand_for_another_window() -> None:
    """Weather moves hour to hour: the temperature now is never tonight's."""
    assert "temperature" not in _pick("temperature tonight", "open-meteo-forecast")
    assert _pick("temperature tonight", "open-meteo-forecast")
    assert "temperature" not in _pick("temperature this week", "open-meteo-forecast")


# ---- D6: a next event whose date is month + day numbers (USNO) ---------------------------------------------------

@pytest.mark.parametrize("ask, subject, wants, sid", [
    # the review's inputs
    ("when is the next full moon", "full moon", ["date"], "usno-moon-phases"),
    ("when is the winter solstice", "winter solstice", ["date"], "usno-seasons"),
    ("when is the spring equinox", "spring equinox", ["date"], "usno-seasons"),
    ("when is the next new moon", "new moon", ["date"], "usno-moon-phases"),
    # more of the class
    ("date of the next full moon", "full moon", ["date"], "usno-moon-phases"),
    ("when is the summer solstice", "summer solstice", ["date"], "usno-seasons"),
    ("when is the first day of fall", "fall equinox", ["date"], "usno-seasons"),
    ("next first quarter moon", "first quarter moon", ["date"], "usno-moon-phases"),
    ("when is the fall equinox this year", "fall equinox", ["date"], "usno-seasons"),
])
def test_a_month_and_day_carry_the_next_events_date(ask, subject, wants, sid) -> None:
    reasons = verdict(ask, subject, wants, sid)
    assert not any("no time for the next event" in r for r in reasons), reasons


def test_a_next_event_still_needs_a_time_the_source_has() -> None:
    """The ISS's position now is no answer to when it passes (an observation's "as of" is no event time)."""
    reasons = verdict("when will the ISS pass over Denver", "ISS passes", ["next pass time"], "wheretheiss-now",
                      place="Denver")
    assert "it gives no time for the next event" in reasons, reasons


# ---- D13: a real TypeError inside locate is a failure, never a silent retry without the frame ------------------

def test_a_typeerror_inside_candidates_is_not_retried_without_the_hint(monkeypatch) -> None:
    calls: list = []

    class Broken:
        def candidates(self, ask, limit=3, hint=None):
            calls.append(hint)
            raise TypeError("a bug inside locate")

    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: Broken())
    intent = _intent("Bills score", "Buffalo Bills", ["score"])
    assert ni_flow._library_candidates("Bills score", intent) == []
    assert calls == [ni_flow._library_hint(intent)]


# ---- D3: the lookup chain survives locate → pick → seal → sample (integration, a real LibraryIndex) -------------

_SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "ni_flow_verify" / "samples.json").read_text())
_POINTS = {"properties": {"gridId": "CHS", "gridX": 87, "gridY": 77}}


def test_the_nws_lookup_chain_runs_from_locate_to_the_card(lib, monkeypatch) -> None:
    """NWS's forecast address is a template its points helper finishes ({office}/{grid_x},{grid_y}). Locate
    must hand the chain on with the candidate; else the tap samples the literal template, gets a 404 and the
    card fails dead (review D3). Runs the real path: candidates() → _library_rows → seal_library_pick →
    _sample_and_map."""
    fetched_at = datetime.fromisoformat(_SAMPLES["nws_forecast_chs"]["fetched"])
    monkeypatch.setattr(nimod, "_clock", lambda: fetched_at)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    ask = "Charleston SC forecast tonight"
    intent = _intent(ask, "weather", ["forecast"], "Charleston SC")
    cands, skipped = lib.candidates(ask, hint=ni_flow._library_hint(intent))
    nws = [c for c in cands if c["source_id"] == "nws-forecast"]
    assert nws, (cands, skipped)
    rows = ni_flow._library_rows(nws)
    assert rows and rows[0]["lookup"], "locate dropped the NWS lookup chain"
    store = _store()
    item_id = ni_flow.create_shell_item(store, ask)
    rec = ni_flow._make_record(ask, "source", intent=intent)
    rec["_ranked_library"] = rows
    ni_flow._flow_write(store, item_id, rec)
    ni_flow.seal_library_pick(store, item_id, rows[0]["url"], rows[0])
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        if url.startswith("https://api.weather.gov/points/"):
            return json.loads(json.dumps(_POINTS))
        assert url == "https://api.weather.gov/gridpoints/CHS/87,77/forecast", url
        return json.loads(json.dumps(_SAMPLES["nws_forecast_chs"]["sample"]))

    prompts: list[str] = []

    def model(prompt: str) -> str:
        # a declared build makes no mapping / judge call; the §34 PRESENT menu (an enum-only
        # pick among finished designs) is the one model turn, and an off reply takes the floor
        prompts.append(prompt)
        assert "You choose how a personal dashboard card presents" in prompt, "no model call"
        return "{}"

    out = ni_flow._sample_and_map(store, item_id, ask, intent, rows[0]["url"], model, fetch)
    assert fetched[-1] == "https://api.weather.gov/gridpoints/CHS/87,77/forecast", fetched
    assert out["state"] == "ready", out["notes"]
    assert store.get_item(item_id)["spec"]["source"]["url"] == "https://api.weather.gov/gridpoints/CHS/87,77/forecast"


def test_category_facts_come_from_the_librarys_public_subcategory(lib) -> None:
    """D11: the flow reads ``subcategory()``; an older Library without it (or a broken one) gives no facts —
    never a reach into the index's private reader."""
    assert ni_flow._subcategory(lib, "sports/schedules")["policy"]["match"] == "name"

    class Older:
        def _conn(self):
            raise AssertionError("a private reader was used")

    class Broken:
        def subcategory(self, sub):
            raise RuntimeError("broken")

    assert ni_flow._subcategory(Older(), "sports/schedules") == {}
    assert ni_flow._subcategory(Broken(), "sports/schedules") == {}
