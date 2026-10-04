"""Live e2e regressions of 2026-10-04 (dev r7 / holdout r7 against r8 / r6), replayed deterministically.

* "gas prices" shipped Colorado's natural-gas dataset and "latest Python release" / "NASA picture of the
  day" lost their Library rows: the intent's ``frame_kind`` fell back to the MODEL's guess when the words
  state no kind ("latest_items" for a bare "gas prices"), and that guess gated locate (a current value is
  "not latest items"), the verify gate and the page shape check ("one value for a list ask").
* "NASA picture of the day" named no taxonomy keyword, so the keyword path failed closed although the ask
  names a reviewed source by its own name ("NASA Astronomy Picture of the Day").
* the harvested dataset's dates showed "Jan 15, 12:00 AM": the mapping path read every timestamp as a time.
* "pollen count in Atlanta" ended FAILED on the first web row's mapping error with two rows left.

The pack is a slice of the REAL candidate pack (pin3, v1.3.0-rc3): every source of the asked
subcategories, the NASA sources, the two Colorado datasets, the whole taxonomy, and the resolver
readings of the asks' words (``fixtures/library_live_1004/pack_slice.json``)."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.secrets import gen_master_key

_SLICE = Path(__file__).parent / "fixtures" / "library_live_1004" / "pack_slice.json"

_TABLES = {
    "sources": ("library_sources", """id VARCHAR PRIMARY KEY, name VARCHAR, description VARCHAR,
        provider_id VARCHAR, provider_name VARCHAR, authority VARCHAR, tier VARCHAR, geo VARCHAR, entity VARCHAR,
        access_kind VARCHAR, url_template VARCHAR, docs_url VARCHAR, auth VARCHAR, terms_status VARCHAR,
        cadence VARCHAR, validation_status VARCHAR, robots VARCHAR, votes_yes INTEGER, votes_no INTEGER,
        prior DOUBLE, record JSON, kinds VARCHAR[], role VARCHAR, audience VARCHAR"""),
    "categories": ("library_source_categories", "source_id VARCHAR, category VARCHAR, subcategory VARCHAR"),
    "terms": ("library_terms", "term VARCHAR, source_id VARCHAR, weight DOUBLE"),
    "source_resolvers": ("library_source_resolvers", "source_id VARCHAR, resolver VARCHAR"),
    "taxonomy": ("library_taxonomy", "category VARCHAR, subcategory VARCHAR, label VARCHAR, kinds VARCHAR[], "
                                     "params VARCHAR[], keywords VARCHAR[], policy JSON"),
    "meta": ("library_meta", "key VARCHAR, value VARCHAR"),
    "resolver_entries": ("library_resolver_entries", "id VARCHAR PRIMARY KEY, resolver VARCHAR, kind VARCHAR, "
                                                     "key VARCHAR, name VARCHAR, lat DOUBLE, lon DOUBLE, "
                                                     "state VARCHAR, attrs JSON, rank DOUBLE"),
    "resolver_aliases": ("library_resolver_aliases", "alias VARCHAR, entry_id VARCHAR, partial BOOLEAN"),
}
_JSON_COLS = {"record", "policy", "attrs"}


def _build(path: Path) -> None:
    data = json.loads(_SLICE.read_text())
    con = duckdb.connect(str(path))
    for key, (table, cols) in _TABLES.items():
        con.execute(f"CREATE TABLE {table}({cols})")
        names = [c.strip().split()[0] for c in cols.replace("\n", " ").split(",")]
        for row in data[key]:  # bounded by the slice
            con.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(names))})",
                        [json.dumps(row[n]) if n in _JSON_COLS and row[n] is not None else row[n]
                         for n in names])
    con.close()


class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> library_index.LibraryIndex:
    tmp = tmp_path_factory.mktemp("live1004")
    _build(tmp / "src.duckdb")
    payload = gzip.compress((tmp / "src.duckdb").read_bytes())
    idx = library_index.LibraryIndex(tmp / "data", netguard_mod=_Net(payload),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(payload).hexdigest()})
    idx.install()
    return idx


def _model(**reply):
    base = {"kind": "external_data", "subject": "", "cadence_minutes": 15, "wants": ["value"],
            "threshold": None, "place": None, "display_hint": "value"}
    return lambda _p: json.dumps({**base, **reply})


# the model replies the live run's shape: a list kind the words never state
_GUESSED = {
    "gas prices": {"subject": "gas prices", "wants": ["current_price"], "frame_kind": "latest_items"},
    "latest Python release": {"subject": "Python release", "wants": ["latest_release"],
                              "frame_kind": "latest_items"},
    "NASA picture of the day": {"subject": "NASA picture of the day", "wants": ["picture"],
                                "frame_kind": "latest_items", "display_hint": "image"},
}


@pytest.mark.parametrize("ask", sorted(_GUESSED))
def test_a_kind_the_words_dont_state_is_open(ask) -> None:
    intent = ni_flow.stage_intent(ask, _model(**_GUESSED[ask]))
    assert intent["frame_kind"] is None
    assert not ni_flow._many_ask(intent)  # a page reading is not held to a list's rows
    assert "frame_kind" not in ni_flow._INTENT_PROMPT


def test_a_kind_the_words_state_still_wins() -> None:
    assert ni_flow.stage_intent("Seahawks score", _model(frame_kind="current_value"))["frame_kind"] == "result"
    assert ni_flow.stage_intent("top news headlines", _model())["frame_kind"] == "latest_items"


@pytest.mark.parametrize(("ask", "first", "needs_key"), [
    ("gas prices", "fred-gasregw", False),
    ("latest Python release", "eol-python", False),
    ("NASA picture of the day", "nasa-apod", True),
])
def test_the_live_asks_find_their_library_rows(lib, monkeypatch, ask, first, needs_key) -> None:
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    intent = ni_flow.stage_intent(ask, _model(**_GUESSED[ask]))
    rows = ni_flow._library_candidates(ask, intent)
    assert rows and rows[0]["source_id"] == first, [r["source_id"] for r in rows]
    assert bool(rows[0]["needs_key"]) is needs_key
    assert not any(r["source_id"].startswith("socrata-") for r in rows)  # never the harvested gas dataset


def test_a_named_source_frames_only_what_it_names(lib) -> None:
    # the ask's every word is in the source's own name: that source's categories frame the lookup
    assert [c["source_id"] for c in lib.candidates("NASA picture of the day")[0]][:1] == ["nasa-apod"]
    # words no reviewed source's name holds together still fail closed
    assert lib.candidates("NASA budget")[0] == []
    assert lib.candidates("picture of my dog")[0] == []


def test_midnight_timestamps_read_as_dates_on_the_mapping_path() -> None:
    rows = [{"date": f"2001-0{m}-15T00:00:00.000", "price": "2.8"} for m in range(1, 4)]
    built = ni_flow.assemble_from_mapping({"date": "items[0].date", "price": "items[0].price"},
                                          {"date": "string", "price": "string"}, ni_flow._DISPLAY_LIST, rows)
    ops = built["pipeline"][-1]["apply"]
    assert {"fn": "date", "field": "rows", "key": "date"} in ops
    assert built["preview_payload"]["rows"][0]["date"] == "Jan 15, 2001"
    # a real clock time stays a time
    rows = [{"date": f"2001-0{m}-15T13:30:00", "price": "2.8"} for m in range(1, 4)]
    built = ni_flow.assemble_from_mapping({"date": "items[0].date", "price": "items[0].price"},
                                          {"date": "string", "price": "string"}, ni_flow._DISPLAY_LIST, rows)
    assert {"fn": "time", "field": "rows", "key": "date"} in built["pipeline"][-1]["apply"]


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


_WEB = [{"title": "Pollen Count | Atlanta Allergy", "host": "www.atlantaallergy.com",
         "url": "https://www.atlantaallergy.com/pollen_counts", "evidence": []},
        {"title": "Atlanta Pollen Count", "host": "pollentracker.app",
         "url": "https://pollentracker.app/atlanta", "evidence": []}]


@pytest.mark.parametrize("sample", [
    {"page": {"title": "text only", "note": "no number anywhere"}},  # mapping: no candidate of the wanted type
    {},  # derive: no candidate paths at all
])
def test_a_mapping_misfit_hands_over_to_the_next_row(monkeypatch, sample) -> None:
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    store = _store()
    item_id = ni_flow.create_shell_item(store, "pollen count in Atlanta")
    ni_flow._transition(store, item_id, "source", error=ni_flow.AWAITING_SOURCE_PICK, _ranked_search=list(_WEB))
    intent = {"kind": "external_data", "subject": "pollen count", "cadence_minutes": 60,
              "wants": ["pollen_count"], "threshold": None, "place": "Atlanta", "display_hint": "value",
              "frame_kind": None, "window": None}
    out = ni_flow._sample_and_map(store, item_id, "pollen count in Atlanta", intent, _WEB[0]["url"],
                                  lambda _p: "{}", lambda _u: sample)
    assert out["state"] == "source" and out["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert [r["url"] for r in out["_ranked_search"]] == [_WEB[1]["url"]]
    assert "pick another source" in out["notes"][-1]
    # the last row: an honest pause, never a failed card
    out = ni_flow._sample_and_map(store, item_id, "pollen count in Atlanta", intent, _WEB[1]["url"],
                                  lambda _p: "{}", lambda _u: sample)
    assert out["state"] == "source" and "paste a link" in out["notes"][-1]


def test_a_mapping_misfit_on_a_pasted_link_still_fails_honestly(monkeypatch) -> None:
    store = _store()
    item_id = ni_flow.create_shell_item(store, "pollen count in Atlanta")
    intent = {"kind": "external_data", "subject": "pollen count", "cadence_minutes": 60,
              "wants": ["pollen_count"], "threshold": None, "place": None, "display_hint": "value",
              "frame_kind": None, "window": None}
    out = ni_flow._sample_and_map(store, item_id, "pollen count in Atlanta", intent, "https://example.org/p",
                                  lambda _p: "{}", lambda _u: {"page": {"title": "text only"}})
    assert out["state"] == "failed"
