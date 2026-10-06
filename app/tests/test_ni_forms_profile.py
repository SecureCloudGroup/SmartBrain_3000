"""Tests for ni_forms.profile: a DataRecord -> Profile with the right shape signatures.

Covers the main shapes (single measure, multi-measure, time series, events, status
records, ranked records, empty); new in the port (the proto had no isolated profile test).
"""
from __future__ import annotations

from datetime import UTC, datetime

from smartbrain_3000.ni_forms import profile as pf
from smartbrain_3000.ni_forms.canon import canonical, fingerprint, sha256
from smartbrain_3000.ni_forms.types import CardInput, Context, DataRecord, Field

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
NY = "America/New_York"


def _ctx(as_of: str | None = None) -> Context:
    return Context(fetched_at="2026-10-06T15:00:00Z", as_of=as_of, source_host="example.org",
                   card_tz=NY, card_tz_src="data")


def _rec(kind: str, fields: list, rows: list, *, as_of: str | None = None) -> DataRecord:
    r = DataRecord(v=1, kind=kind, fields=fields, rows=rows, context=_ctx(as_of),
                   producer="json_profile")
    r.fingerprint = fingerprint(kind, fields, len(rows))
    r.data_hash = sha256(canonical([[f.name for f in fields], rows]))
    return r


def _inp(ask: str) -> CardInput:
    return CardInput(card_id="t", ask=ask, title="t", source_url="https://example.org",
                     source_kind="http_json", source_format="json", raw_path=None, http_status=200,
                     content_type="application/json", fetched_at="2026-10-06T15:00:00Z", cadence_s=0)


def test_single_measure():
    """One numeric measure, one row -> single_measure in the signatures."""
    f = [Field(name="price", label="Price", path="price", type="currency", role="measure",
               currency="USD", precision=2)]
    rec = _rec("measure", f, [[223.86]], as_of="2026-10-06T14:00:00Z")
    p = pf.profile(rec, _inp("price"), NOW)
    assert "single_measure" in p.signatures, f"got {p.signatures!r}"
    assert p.n_rows == 1, "n_rows should match row count"


def test_multi_measure():
    """Multiple numeric measures, one row -> multi_measure."""
    f = [Field(name="t", label="Temp", path="t", type="quantity", role="measure", unit="degF"),
         Field(name="w", label="Wind", path="w", type="quantity", role="secondary", unit="mph")]
    rec = _rec("measure", f, [[70.0, 12.0]])
    p = pf.profile(rec, _inp("weather"), NOW)
    assert "multi_measure" in p.signatures, f"got {p.signatures!r}"
    assert p.n_rows == 1, "n_rows invariant"


def test_time_series():
    """Time + numeric, several rows -> a time_series_* signature appears."""
    f = [Field(name="t", label="Time", path="t", type="datetime", role="time"),
         Field(name="v", label="Value", path="v", type="number", role="measure")]
    rows = [[f"2026-10-0{d}T12:00:00Z", float(d)] for d in range(1, 6)]
    rec = _rec("series", f, rows)
    p = pf.profile(rec, _inp("trend"), NOW)
    assert any(s.startswith("time_series_") for s in p.signatures), f"got {p.signatures!r}"
    assert p.time.field == "t", "time field should be 't'"


def test_events_with_kind():
    """Events with a kind field + a future row -> events_with_kind."""
    f = [Field(name="t", label="When", path="t", type="datetime", role="time"),
         Field(name="k", label="Kind", path="k", type="category", role="kind")]
    rows = [["2026-10-07T12:00:00Z", "launch"], ["2026-10-08T12:00:00Z", "launch"]]
    rec = _rec("events", f, rows)
    p = pf.profile(rec, _inp("next launches"), NOW)
    assert "events_with_kind" in p.signatures, f"got {p.signatures!r}"
    assert p.time.future_events >= 1, "at least one future event"


def test_status_records():
    """Name + status role -> status_records."""
    f = [Field(name="n", label="Name", path="n", type="text", role="name"),
         Field(name="s", label="Status", path="s", type="category", role="status")]
    rows = [["A", "ok"], ["B", "down"], ["C", "ok"]]
    rec = _rec("records", f, rows)
    p = pf.profile(rec, _inp("statuses"), NOW)
    assert "status_records" in p.signatures, f"got {p.signatures!r}"


def test_ranked_records():
    """Name + numeric value, no time -> ranked_records."""
    f = [Field(name="n", label="Name", path="n", type="text", role="name"),
         Field(name="v", label="Value", path="v", type="number", role="value")]
    rows = [["A", 10.0], ["B", 20.0], ["C", 30.0]]
    rec = _rec("records", f, rows)
    p = pf.profile(rec, _inp("top items"), NOW)
    assert "ranked_records" in p.signatures, f"got {p.signatures!r}"


def test_empty():
    """A measure record with zero rows -> empty."""
    f = [Field(name="x", label="X", path="x", type="number", role="measure")]
    rec = _rec("measure", f, [])
    p = pf.profile(rec, _inp("nothing"), NOW)
    assert "empty" in p.signatures, f"got {p.signatures!r}"


def test_sig_is_deterministic():
    """Same inputs -> same profile sig (used by few-shot retrieval)."""
    f = [Field(name="x", label="X", path="x", type="number", role="measure")]
    rec1 = _rec("measure", f, [[1.0]])
    rec2 = _rec("measure", f, [[1.0]])
    p1 = pf.profile(rec1, _inp("x"), NOW)
    p2 = pf.profile(rec2, _inp("x"), NOW)
    assert p1.sig == p2.sig, "sig must be stable for identical input"
    assert p1.n_rows == p2.n_rows, "n_rows invariant"


def test_extract_wants_orders_by_appearance():
    """'high ... next' returns [extreme_high, next] in that order."""
    hits = pf.extract_wants("show me the highest value and the next event")
    assert hits, "non-empty for a sentence with wants"
    assert hits.index("extreme_high") < hits.index("next"), f"order violated: {hits!r}"
