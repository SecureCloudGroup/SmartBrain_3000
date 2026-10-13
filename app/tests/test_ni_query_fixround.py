"""Round 20, Q3c — the set F read's defect classes, each fixed by a rule (docs/internal/ni-format.md §36), with
the five wrong set F cards as regression cases on samples recorded from their tapped URLs
(``fixtures/ni_query/samples_f``, fetched 2026-10-12) and the declared answers of Library #12
(``fixtures/ni_query/library_f.json``); the model replies are written by hand to reproduce the live IRs.

A. Grounded filter values: a ``contains`` / ``names`` value stands only on evidence (a proper-noun phrase, a
   holiday, a declared label, a taxonomy keyword of the asked subcategories, the sample's rows) — F22's
   "anywhere in the world" and F26's "active" emptied real rows.
B. Carried entities: on a league-wide list (an address no resolver filled) every proper-noun phrase of the ask is
   carried by a ``names`` filter — F19 built the league scoreboard with no Canucks filter.
C. Ranking defaults: "N <ranking word>" in either order sets the limit, and a ranking the model left unordered
   is ordered by its ranked number cell — F24 showed 20 coins in market-cap order.
D. The rows' name role: a list whose titles repeat (reposts) still names its rows by the titles — the F21 card
   named every Hacker News story by its objectID."""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import json
import pathlib
from zoneinfo import ZoneInfo

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.ni_query import plan_query, recognize
from smartbrain_3000.ni_query.clauses import limit_for, rank_order
from smartbrain_3000.ni_query.decl import answer_named
from smartbrain_3000.ni_query.ground import (
    Grounds,
    carry_entities,
    grounded_by,
    proper_phrases,
)
from smartbrain_3000.ni_query.plan import consumed_words
from smartbrain_3000.ni_query.recognize import words
from smartbrain_3000.secrets import gen_master_key

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ni_query"
_LIBF = json.loads((_FIX / "library_f.json").read_text())
_LIB = json.loads((_FIX / "library.json").read_text())
_SAMPLES = json.loads((_FIX / "samples_f" / "index.json").read_text())
_ASKED = json.loads((_FIX / "asked_subcategories.json").read_text())
_ZONE = "America/New_York"
NY = ZoneInfo(_ZONE)
NOW = dt.datetime(2026, 10, 12, 17, 30, tzinfo=NY)       # right after the samples_f fetch (21:17 UTC)
QUERY_MARK = "You turn a person's ask into one query object"


def _answer(sid: str, name: str) -> dict:
    """One declared answer of a Library #12 source, as the flow cleans it."""
    raw = next(a for a in _LIBF[sid]["answers"] if a["name"] == name)
    cleaned = ni_flow._clean_answer(raw)
    assert cleaned is not None, f"{sid}.{name} breaks the answers contract"
    return cleaned


def _sample(rid: str) -> object:
    """The payload recorded for a set F row (the body as the flow decodes JSON)."""
    rec = json.loads((_FIX / "samples_f" / _SAMPLES[rid]["file"]).read_text())
    return json.loads(rec["body"])


def _spans(ask: str) -> dict:
    return recognize(ask, now=NOW, zone=_ZONE)


# --- A: grounded filter values -------------------------------------------------------------------------------------

_ROWS = {"name": "rows", "kind": "list", "path": "items", "words": ["active fires", "anywhere"],
         "row": [{"path": "name", "type": "text", "label": "Fire"}, {"path": "phase", "type": "text", "label": "Phase"},
                 {"path": "code", "type": "number", "label": "Sky", "codes": "wmo_weather"},
                 {"path": "acres", "type": "number", "label": "Size"}]}
_ROW_SAMPLE = {"items": [{"name": "ROWE CREEK COMPLEX", "phase": "First Quarter", "code": 73, "acres": 5},
                         {"name": "Second Flat", "phase": "Full Moon", "code": 0, "acres": 9}]}


@pytest.mark.parametrize(("ask", "value", "col", "keywords", "rule"), [
    ("is the CTA Blue Line running normally", "Blue Line", "*", (), "proper"),
    ("when is thanksgiving this year", "Thanksgiving", "name", (), "holiday"),
    ("which fires are listed", "Size", "name", (), "declared"),                    # a cell label
    ("fires with snow", "Snow", "name", (), "declared"),                           # a label of the declared codes
    ("red flag warnings in California", "Red Flag Warning", "name", ("red flag warning",), "keyword"),
    ("heat advisories in Phoenix", "Heat Advisories", "name", ("heat advisory",), "keyword"),   # plurals folded
    ("flash flood watches around Louisville", "Flash Flood Watch", "name", ("flood watch",), "keyword"),  # a kind
    ("when does the first quarter moon fall", "first quarter", "phase", (), "observed"),
    ("fires burning in the rowe creek area", "Rowe Creek", "*", (), "observed"),
    ("latest earthquakes above magnitude 5 anywhere in the world", "anywhere in the world", "name", (), None),
    ("largest active wildfires in Oregon by size", "active", "name", ("wildfire", "fires"), None),
    ("largest active wildfires in Oregon by size", "active wildfires", "name", ("wildfire",), None),  # a topic word
    ("latest tech news from Ars Technica", "tech", "name", ("tech news",), None),
    ("show active fires", "active fires", "name", (), None),                      # the words lexicon is no evidence
    ("when does the full moon fall", "Full Moon", "name", (), None),               # another column holds it
])
def test_a_filter_value_stands_on_evidence(ask, value, col, keywords, rule) -> None:
    """Rule A (ni_query/ground.py): proper phrase, holiday, declared label, taxonomy keyword (or a kind of a
    multi-word one), the sample's rows in the cells the filter reads — never the answer's ``words``."""
    spans = _spans(ask)
    phrases = proper_phrases(ask, spans, frozenset(), frozenset({"cta", "alerts"}))
    grounds = Grounds(keywords=frozenset(keywords), sample=_ROW_SAMPLE)
    assert grounded_by(value, col, _ROWS, phrases=phrases, spans=spans, grounds=grounds) == rule


# every gold where value (sets A-F, clean and noted) and the rule that grounds it, with the evidence the flow
# reads: the subcategories the Library files the ask under, the recorded sample, the address's consumed words
GOLD_GROUNDING = {
    "A30": "keyword", "A31": "keyword", "A34": "keyword", "A38": "holiday", "B32": "keyword", "C34": "keyword",
    "D18": "proper", "D33": "keyword", "E14": "observed", "E31": "proper", "E33": "keyword", "E34": "keyword",
    "E36": "keyword", "E39": "holiday", "F19": "proper", "F25": "proper", "F33": "keyword", "F35": "keyword",
    "F37": "keyword"}


def _gold(rid: str) -> dict:
    name = "gold_f.jsonl" if rid.startswith("F") else "gold.jsonl"
    return next(json.loads(x) for x in (_FIX / name).read_text().splitlines() if f'"id": "{rid}"' in x)


def _gold_sample(rid: str) -> object:
    folder = "samples_f" if rid.startswith("F") else "samples"
    entry = json.loads((_FIX / folder / "index.json").read_text()).get(rid) or {}
    body = json.loads((_FIX / folder / entry["file"]).read_text())["body"] if entry.get("file") else ""
    return json.loads(body) if body.lstrip()[:1] == "{" else None


@pytest.mark.parametrize(("rid", "rule"), sorted(GOLD_GROUNDING.items()))
def test_every_gold_filter_value_stands_on_evidence(rid, rule) -> None:
    """The lead's check: every gold ``contains`` / ``names`` value stays grounded (E14 by the moon-phase rows
    alone; E33 "Flash Flood Watch" as a kind of the keyword "flood watch")."""
    g = _gold(rid)
    src = (_LIBF if rid.startswith("F") else _LIB)[g["source_id"]]
    spans = recognize(g["ask"], now=NOW, zone=_ZONE)
    own = frozenset(w for w, _, _ in words(src["name"].casefold()))
    keywords = frozenset(k.casefold() for c in _ASKED["asked"][rid] for k in _ASKED["keywords"][c])
    grounds = Grounds(keywords=keywords, sample=_gold_sample(rid))
    answer = answer_named(src["answers"], g["ir"]["answer"])
    texts = [w for w in g["ir"]["where"] if w["op"] in ("contains", "names")]
    assert texts, rid
    for w in texts:
        got = grounded_by(w["value"], w["col"], answer, phrases=proper_phrases(g["ask"], spans, frozenset(), own),
                          spans=spans, grounds=grounds)
        assert got == rule, (rid, w, got)


# --- B: carried entities --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("ask", "consumed", "own", "phrases"), [
    ("final score of the latest Canucks game", (), ("nhl", "scores", "today"), ["Canucks"]),
    ("Canucks score tonight", (), (), []),                               # the first word is no evidence
    ("is the CTA Blue Line running normally", (), ("cta", "train", "alerts"), ["Blue Line"]),
    ("is the CTA Blue Line running normally", (), (), ["CTA Blue Line"]),
    ("can I see the Blue Jays score", (), (), ["Blue Jays"]),             # "I" never
    ("did the Blue Jays win last night", ("Toronto Blue Jays",), (), []),  # the address took them
    ("Final Score Of The Latest Canucks Game", (), (), []),              # title case tells nothing
    ("WHAT IS THE CANUCKS SCORE", (), (), []),                            # nor all caps
    ("delays at Chicago O'Hare right now", (), (), ["Chicago O'Hare"]),
    ("weather in St. Louis this weekend", (), (), ["St. Louis"]),
    ("is there a game on Saturday for the Sharks", (), (), ["Sharks"]),  # a time word is no entity
    ("when is Thanksgiving this year", (), (), []),                      # nor a holiday
    ("Bruins score? Who won", (), (), []),                               # a sentence starts after "?"
    ("upcoming SpaceX launches from Vandenberg this month", ("Vandenberg", "SpaceX"), (), []),
    ("US unemployment rate over the past three years", (), ("unemployment", "rate", "fred"), []),
    ("what is the US unemployment rate", (), ("unemployment", "rate", "fred"), ["US"]),   # carried by no series
])
def test_proper_phrases(ask, consumed, own, phrases) -> None:
    taken = consumed_words(ask, list(consumed))
    assert proper_phrases(ask, _spans(ask), taken, frozenset(own)) == phrases


def test_an_entity_is_carried_only_by_rows_that_name_things() -> None:
    games, recent = _answer("nhl-score-now", "games"), answer_named(_LIBF["fred-unrate"]["answers"], "recent")
    names = {"col": "*", "op": "names", "value": "Canucks"}
    assert carry_entities([], games, ["Canucks"]) == ([names], ["Canucks"])
    held = [{"col": "*", "op": "names", "value": "Vancouver Canucks"}]
    assert carry_entities(held, games, ["Canucks"]) == (held, [])          # already carried
    assert carry_entities([], recent, ["US"]) == ([], [])                  # a date + number series names nothing
    temperature = answer_named(_LIBF["open-meteo-forecast"]["answers"], "temperature")
    assert carry_entities([], temperature, ["Fargo"]) == ([], [])          # a value answer never filters
    full = [{"col": "*", "op": "names", "value": f"x{i}"} for i in range(6)]
    assert carry_entities(full, games, ["Canucks"]) == (full, [])          # the IR's six-filter bound
    division = {"name": "al_east_standings", "label": "AL East standings", "kind": "list", "path": "records",
                "row": [{"path": "team.name", "type": "text", "label": "Team"}]}
    assert carry_entities([], division, ["East"]) == ([], [])              # the chosen answer names it


@pytest.mark.parametrize(("sid", "ask", "added"), [
    ("usgs-quakes-25-day", "how many earthquakes hit Japan this week", ["Japan"]),   # a worldwide list
    ("usgs-quakes-near", "how many earthquakes near SF this week", []),    # a resolver took the place ("SF")
    ("nhl-club-schedule", "when do the Habs play next", []),               # a resolver took the team ("Habs")
])
def test_only_an_address_no_resolver_filled_carries_the_asks_entities(sid, ask, added) -> None:
    """B on the rules floor: a league-wide / worldwide list carries the capitalized entity; an address a resolver
    filled already holds it, in a spelling the consumed words need not show (the intent's place for "SF" is
    "San Francisco", the pick's label "Boston Bruins")."""
    src = _LIB[sid]
    plan = plan_query(ask, kind="count" if "many" in ask else "next_event", wants=[],
                      source={"id": sid, "name": src["name"]}, answers=src["answers"], params=src["params"],
                      consumed=["San Francisco"] if "SF" in ask else [], now=NOW, zone=_ZONE, call_model=None,
                      pool=[])
    assert [w["value"] for w in plan.ir["where"] if w["op"] == "names"] == added, plan.ir
    assert [n for n in plan.notes if n.startswith("entity_added:")] == [f"entity_added:{a}" for a in added]


# --- C: ranking defaults --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("ask", "limit"), [
    ("ten biggest cryptocurrencies by trading volume today", 10),
    ("biggest ten coins ranked by trading volume", 10),
    ("top 5 cryptocurrencies by price right now", 5),
    ("the 3 largest wildfires in California", 3),
    ("5 most recent earthquakes in Alaska", 5),
    ("the 2 lowest gas prices", 2),
    ("30 year mortgage rate trend this year", None),
    ("latest earthquakes above magnitude 5 anywhere in the world", None),
    ("largest active wildfires in Oregon by size", None),
])
def test_the_limit_is_read_from_a_number_beside_a_ranking_word(ask, limit) -> None:
    quakes = _answer("usgs-quakes-25-day", "quakes")
    assert limit_for(ask, "ranking", quakes, _spans(ask))[0] == limit


@pytest.mark.parametrize(("sid", "name", "ask", "order"), [
    ("coingecko-markets", "volume", "ten biggest cryptocurrencies by trading volume today",
     [{"col": "total_volume", "dir": "desc"}]),                           # the ask's quantity word names the cell
    ("coingecko-markets", "top_coins", "ten biggest cryptocurrencies",
     [{"col": "market_cap", "dir": "desc"}]),                             # the answer's own label names it
    ("nifc-current-fires", "fires", "largest active wildfires in Oregon by size",
     [{"col": "attributes.IncidentSize", "dir": "desc"}]),
    ("nhl-standings-now", "standings", "NHL standings in the Atlantic division",
     [{"col": "points", "dir": "desc"}]),                                 # the first number cell
    ("usgs-quakes-25-day", "quakes", "the 5 smallest earthquakes today", [{"col": "properties.mag", "dir": "asc"}]),
])
def test_a_ranking_the_model_left_unordered_is_ordered_by_its_ranked_cell(sid, name, ask, order) -> None:
    answers = _LIBF[sid]["answers"]
    answer = answer_named(answers, name)
    assert rank_order(ask, "ranking", answer, answers, _spans(ask), frozenset()) == order
    assert rank_order(ask, "latest_items", answer, answers, _spans(ask), frozenset()) == []   # rankings only


# --- the five wrong set F cards, through the declared build ----------------------------------------------------------

def _ir(answer: str, **kw) -> str:
    return json.dumps({"answer": answer, "params": {}, "select": [], "where": [], "time": None, "order": [],
                       "limit": None, "agg": "none", **kw})


def _model(reply: str):
    """The query turn reads ``reply``; every other turn (PRESENT, FIT) has no model (the rules)."""
    def model(prompt: str) -> str:
        if QUERY_MARK in prompt:
            return reply
        raise RuntimeError("no model for this turn")
    return model


class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


F_SOURCES = ("nhl-score-now", "hn-search", "usgs-quakes-25-day", "coingecko-markets", "nifc-current-fires")


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    """A real LibraryIndex over the five sources' Library #12 records (no taxonomy: no keyword evidence)."""
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE library_sources(id VARCHAR PRIMARY KEY, record JSON)")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    for sid in F_SOURCES:
        rec = {"id": sid, "name": _LIBF[sid]["name"], "description": sid, "tier": "curated", "categories": [],
               "kinds": [], "examples": [], "provider": {"id": "p", "name": _LIBF[sid]["name"], "authority": "official"},
               "access": {"kind": "http_json", "url_template": "https://example.org/" + sid,
                          "params": _LIBF[sid]["params"], "auth": "none", "headers": {}},
               "answers": _LIBF[sid]["answers"]}
        con.execute("INSERT INTO library_sources VALUES (?, ?)", (sid, json.dumps(rec)))
    con.execute("INSERT INTO library_meta VALUES ('records', ?)", (str(len(F_SOURCES)),))
    con.close()
    raw = gzip.compress(src.read_bytes())
    idx = library_index.LibraryIndex(tmp_path / "data", netguard_mod=_Net(raw),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(raw).hexdigest()})
    idx.install()
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: idx)
    monkeypatch.setattr(nimod, "_clock", lambda: NOW)
    monkeypatch.setattr(nimod, "_user_timezone", NY)
    return idx


def _build(rid: str, sid: str, ask: str, kind: str, reply: str, *, label: str = "", params: dict | None = None,
           place: str | None = None) -> dict:
    """The declared build of one set F ask on its recorded sample (the live run's pick: label + params)."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    url = json.loads((_FIX / "samples_f" / _SAMPLES[rid]["file"]).read_text())["url"]
    item_id = ni_flow.create_shell_item(store, ask)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(ask, "source"))
    ni_flow.seal_library_pick(store, item_id, url, {"source_id": sid, "url": url, "format": "json",
                                                    "params": params or {}, "label": label,
                                                    "scope": "place" if place else "global"})
    intent = {"kind": "external_data", "subject": ask, "cadence_minutes": 15, "wants": [], "threshold": None,
              "place": place, "display_hint": "value", "names": [], "frame_kind": kind}
    built = ni_flow._try_answers_build(store, item_id, ask, intent, url, copy.deepcopy(_sample(rid)), _model(reply),
                                       NOW, do_fetch=lambda _u: copy.deepcopy(_sample(rid)))
    assert built is not None and not built.get("nothing"), built
    return built


def test_f19_the_asked_team_is_carried_and_no_row_is_the_honest_empty(lib) -> None:
    """F19 "final score of the latest Canucks game" — the live IR carried no Canucks filter and the card showed
    Panthers at Sabres. Code carries the capitalized team the address did not take; the recorded scoreboard has
    no Canucks game, so the card is empty under a line that names the team."""
    built = _build("F19", "nhl-score-now", "final score of the latest Canucks game", "result",
                   _ir("games", order=[{"col": "startTimeUTC", "dir": "desc"}], limit=1))
    q = built["query"]["query"]
    assert q["ir"]["where"] == [{"col": "*", "op": "names", "value": "Canucks"}]
    assert "entity_added:Canucks" in q["notes"] and q["coverage"]["where"] == "recognizer"
    assert built["query"]["interpretation"] == "Games · mentions Canucks · the latest one"
    assert built["preview_payload"]["rows"] == [] and built["preview_payload"]["rows_count"] == 0
    nimod._validate_query(q)


def test_f21_the_story_titles_name_the_rows(lib) -> None:
    """F21 "newest posts on Hacker News about Rust" through the build, on the run's own rows: the titles lead."""
    built = _build("F21-run", "hn-search", "newest posts on Hacker News about Rust", "latest_items",
                   _ir("stories", order=[{"col": "created_at", "dir": "desc"}]), params={"topic": "posts rust"})
    assert built["query"]["query"]["ir"]["where"] == []      # "Rust" was the address's topic, never a filter
    assert built["query"]["interpretation"] == "Newest matching stories"
    roles = {f["path"]: f["role"] for f in built["scene"]["record"]["fields"]}
    assert roles["title"] == "name" and roles["objectID"] != "name", roles


def test_f21_reposted_titles_still_name_the_rows() -> None:
    """D, test first (failed before the fix): the run's 20 rows hold one title three times (reposts), so the
    titles are not all distinct — the "Item id" column named every row ("50062217; 50061674; …"). The titles
    name the rows; an opaque id never does."""
    sample = _sample("F21-run")
    titles = [h["title"] for h in sample["hits"]]
    assert len(set(titles)) < len(titles), "the recorded rows hold a repost"
    built = ni_flow.build_from_answers([_answer("hn-search", "stories")], sample,
                                       "newest posts on Hacker News about Rust")
    roles = {f["path"]: f["role"] for f in built["scene"]["record"]["fields"]}
    assert roles["title"] == "name", roles
    assert roles["objectID"] != "name", roles


def test_f22_a_question_phrase_never_filters_the_quakes(lib) -> None:
    """F22 "latest earthquakes above magnitude 5 anywhere in the world" — the live IR's `contains "anywhere in
    the world"` emptied the three M5+ quakes of the feed. No evidence grounds the phrase: it is dropped, the
    magnitude stays, and the line no longer claims it."""
    reply = _ir("quakes", where=[{"col": "properties.mag", "op": ">", "value": 5},
                                 {"col": "properties.place", "op": "contains", "value": "anywhere in the world"}])
    built = _build("F22", "usgs-quakes-25-day", "latest earthquakes above magnitude 5 anywhere in the world",
                   "latest_items", reply)
    q = built["query"]["query"]
    assert q["ir"]["where"] == [{"col": "properties.mag", "op": ">", "value": 5}]
    assert "filter_ungrounded:anywhere in the world" in q["notes"] and q["coverage"]["where"] == "approximated"
    assert built["query"]["interpretation"] == "Earthquakes · Magnitude > 5"
    assert sorted(r["properties"]["mag"] for r in built["preview_payload"]["rows"]) == [5.2, 5.4, 5.5]


def test_f24_ten_biggest_by_volume_is_a_sorted_top_ten(lib) -> None:
    """F24 "ten biggest cryptocurrencies by trading volume today" — the live IR had no order and no limit: 20
    coins in market-cap order. "ten biggest" is the limit; the ranking's cell is the 24h volume the ask names."""
    built = _build("F24", "coingecko-markets", "ten biggest cryptocurrencies by trading volume today", "ranking",
                   _ir("volume", time={"from": "today", "to": "today"}))
    q = built["query"]["query"]
    assert q["ir"]["order"] == [{"col": "total_volume", "dir": "desc"}] and q["ir"]["limit"] == 10
    assert "order_default" in q["notes"] and q["coverage"]["order"] == "rules"
    assert built["query"]["interpretation"] == "24h trading volume · today · top 10 by 24h volume"
    rows = built["preview_payload"]["rows"]
    assert [r["name"] for r in rows[:3]] == ["Tether", "Bitcoin", "USDC"] and len(rows) == 10
    assert [r["total_volume"] for r in rows] == sorted((r["total_volume"] for r in rows), reverse=True)


def test_f26_the_word_active_never_empties_the_fires(lib) -> None:
    """F26 "largest active wildfires in Oregon by size" — the live IR's `contains "active"` on the fire names
    emptied 25 real fires. The word grounds nothing and is dropped; the ranking is ordered by size."""
    reply = _ir("fires", where=[{"col": "attributes.IncidentName", "op": "contains", "value": "active"}])
    built = _build("F26", "nifc-current-fires", "largest active wildfires in Oregon by size", "ranking", reply,
                   label="Oregon (OR)", params={"state": "OR"}, place="Oregon")
    q = built["query"]["query"]
    assert q["ir"]["where"] == [] and q["ir"]["order"] == [{"col": "attributes.IncidentSize", "dir": "desc"}]
    assert "filter_ungrounded:active" in q["notes"] and q["coverage"]["where"] == "approximated"
    assert q["coverage"]["order"] == "rules"
    assert built["query"]["interpretation"] == "Current wildfires · highest Size first"
    rows = built["preview_payload"]["rows"]
    assert len(rows) == 25 and rows[0]["attributes"]["IncidentName"] == "ROWE CREEK COMPLEX"


def test_the_flow_hands_the_asked_subcategories_keywords_to_the_plan(monkeypatch) -> None:
    """The flow's evidence for rule 4: the taxonomy keywords of the subcategories the Library files the ask (and
    the intent's subject) under — nothing without a Library."""
    class _Lib:
        def classify(self, text: str) -> list[str]:
            return ["weather/alerts"] if "warning" in text else []

        def taxonomy(self) -> list[dict]:
            return [{"id": "weather", "subcategories": [{"id": "alerts", "keywords": ["Red Flag Warning", "alerts"]},
                                                        {"id": "forecast", "keywords": ["snow"]}]}]
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Lib())
    intent = {"subject": "red flag warnings", "wants": []}
    assert ni_flow._asked_keywords("red flag warnings in California", intent) == {"red flag warning", "alerts"}
    assert ni_flow._asked_keywords("snow in Tahoe", {"subject": "snow", "wants": []}) == frozenset()
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: None)
    assert ni_flow._asked_keywords("red flag warnings in California", intent) == frozenset()
