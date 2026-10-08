"""R19 Phase 1b: `ni_forms.clock.next_boundary` (pure unit tests) and the clock-sensitive
forms it serves (`day_table`, `agenda`) at the layout level — the card-tz midnight edge,
the verified-vs-inferred weekday branch, and the viewer-zone invariant for place-bound text.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from smartbrain_3000.ni_forms.catalog import day_table
from smartbrain_3000.ni_forms.clock import next_boundary
from smartbrain_3000.ni_forms.enumerate import enumerate as enumerate_cands
from smartbrain_3000.ni_forms.layout import layout_span
from smartbrain_3000.ni_forms.paint import live as live_mod
from smartbrain_3000.ni_forms.spans import Span
from smartbrain_3000.ni_forms.types import Context, DataRecord, Field
from tests import _ni_forms_recs as recs
from tests._ni_forms_cand import cand_for

LA = "America/Los_Angeles"


# --------------------------------------------------------------------- next_boundary
def _rec(fields: list, rows: list | None = None, *, card_tz: str = "UTC",
        as_of: str | None = None, fetched_at: str = "2026-10-07T12:00:00Z") -> DataRecord:
    ctx = Context(fetched_at=fetched_at, as_of=as_of, source_host="example.org",
                  card_tz=card_tz, card_tz_src="data")
    return DataRecord(v=1, kind="measure", fields=fields, rows=rows or [[None] * len(fields)],
                      context=ctx, producer="json_profile", fingerprint="x")


def test_next_boundary_midnight_is_in_the_card_zone_not_viewer_or_utc() -> None:
    now = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    rec = _rec([Field(name="t", label="T", path="t", type="datetime", role="time")])
    got = next_boundary(rec, [], now=now, card_tz=LA, cadence_s=0)
    assert got == live_mod.next_midnight(now, LA)
    assert got != live_mod.next_midnight(now, "UTC")
    assert got != live_mod.next_midnight(now, "America/New_York")


def test_next_boundary_dst_transition_within_48h_wins_over_midnight() -> None:
    # Nov 1 2026 01:00 EDT: the fall-back transition (~06:00Z) is ~1h away; the next
    # card-tz midnight is ~24h away, so the DST change must be the earlier boundary.
    now = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    rec = _rec([Field(name="t", label="T", path="t", type="datetime", role="time")])
    transitions = live_mod.dst_transitions(now, "America/New_York", 48)
    assert transitions, "fixture must straddle the Nov 1 2026 US DST change"
    got = next_boundary(rec, [], now=now, card_tz="America/New_York", cadence_s=0)
    assert got == transitions[0]
    assert got < live_mod.next_midnight(now, "America/New_York")


def test_next_boundary_next_event_instant_beats_midnight() -> None:
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    tfield = Field(name="t", label="T", path="t", type="datetime", role="time")
    rec = _rec([tfield], rows=[["2026-10-07T12:30:00Z"]], card_tz="UTC")
    got = next_boundary(rec, [], now=now, card_tz="UTC", cadence_s=0)
    assert got == datetime(2026, 10, 7, 12, 30, tzinfo=UTC)
    assert got < live_mod.next_midnight(now, "UTC")


def test_next_boundary_stale_threshold_when_it_is_soonest() -> None:
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    field = Field(name="n", label="N", path="n", type="number", role="measure")
    rec = _rec([field], rows=[[1]], fetched_at="2026-10-07T11:50:00Z")
    got = next_boundary(rec, [], now=now, card_tz="UTC", cadence_s=600)  # 10-minute cadence
    assert got == datetime(2026, 10, 7, 12, 10, tzinfo=UTC)  # as_of (11:50) + 2x600s


def test_next_boundary_none_for_no_time_and_no_cadence() -> None:
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    field = Field(name="n", label="N", path="n", type="number", role="measure")
    rec = _rec([field], rows=[[1]])
    assert next_boundary(rec, [], now=now, card_tz="UTC", cadence_s=0) is None


def test_next_boundary_reads_live_bindings_from_the_clirs() -> None:
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    field = Field(name="n", label="N", path="n", type="number", role="measure")
    rec = _rec([field], rows=[[1]])
    clir = {"prims": [], "live": [{"k": "past_dim", "args": {"prims": [], "t": "2026-10-07T12:05:00Z"}}]}
    got = next_boundary(rec, [clir], now=now, card_tz="UTC", cadence_s=0)
    assert got == datetime(2026, 10, 7, 12, 5, tzinfo=UTC)


# --------------------------------------------------------------------- day_label_rc
def test_day_label_rc_weekday_only_when_verified() -> None:
    """`inferred_dates` (shell.py) decides the branch: a verified record reads a
    weekday for a far day; an inferred one keeps the +n days / Today / Tomorrow
    recipe even far from today (never a confident weekday it cannot back)."""
    d0 = date(2026, 10, 7)
    far = date(2026, 10, 12)   # n=5: outside the -1..1 Today/Tomorrow/Yesterday band
    near = date(2026, 10, 8)   # n=1: Tomorrow regardless of verified/inferred

    verified = SimpleNamespace(flags={}, inferred=[])
    lrc, tiso = day_table.day_label_rc(SimpleNamespace(rec=verified), far, d0)
    assert lrc is None and tiso == far.isoformat()   # the caller renders this as a weekday (EEE d)

    inferred = SimpleNamespace(flags={"date_confidence": "inferred"}, inferred=[])
    lrc2, tiso2 = day_table.day_label_rc(SimpleNamespace(rec=inferred), far, d0)
    assert tiso2 is None and lrc2 == ["d", "relday", [far.isoformat(), d0.isoformat()]]

    lrc3, tiso3 = day_table.day_label_rc(SimpleNamespace(rec=verified), near, d0)
    assert tiso3 is None and lrc3 == ["d", "relday", [near.isoformat(), d0.isoformat()]]


# --------------------------------------------------------------------- today at local midnight
def _day_table_dates_rec(base: date):
    days = [base + timedelta(days=i) for i in (-1, 0, 1, 2, 3)]
    fs = [recs.F("d", "date", "date", "Date"), recs.F("name", "text", "name", "Event")]
    rows = [[d.isoformat(), f"Event {i}"] for i, d in enumerate(days)]
    r = recs.mk("records", fs, rows, tz=LA)
    return r, recs.prof(r, ["dated_rows"], [])


def test_day_table_today_label_correct_at_local_midnight_edges() -> None:
    base = date(2026, 10, 8)
    rec, prof = _day_table_dates_rec(base)
    inp = recs.inp("Events", url="https://example.org/events")
    cand = cand_for("day_table", rec, prof)
    assert cand is not None and cand.variant == "rows"

    # the tallest desktop span (most rows worth of vertical room), so more than one
    # day's row actually renders -- cand_for builds a bare stub with no `.plans`.
    span = Span.parse("d4x3")

    # just after LA-local midnight: the UTC instant is already minutes into the NEW day,
    # but the record's own rows are keyed by LA-local date -- `base` must read Today.
    just_after = datetime(base.year, base.month, base.day, 0, 10, tzinfo=ZoneInfo(LA)).astimezone(UTC)
    clir = layout_span(cand, rec, prof, inp, span, just_after).clir
    texts = [ln for p in clir["prims"] if p["k"] == "text" for ln in p["lines"]]
    assert "Today" in texts and "Yesterday" not in texts

    # just before LA-local midnight (23:55): `base` is still Today for 5 more minutes,
    # and the next day must not yet read Today.
    just_before = datetime(base.year, base.month, base.day, 23, 55, tzinfo=ZoneInfo(LA)).astimezone(UTC)
    clir2 = layout_span(cand, rec, prof, inp, span, just_before).clir
    texts2 = [ln for p in clir2["prims"] if p["k"] == "text" for ln in p["lines"]]
    assert "Today" in texts2
    assert "Tomorrow" in texts2


def _agenda_dates_rec(base: date):
    fs = [recs.F("t", "datetime", "time", "Time"), recs.F("name", "text", "name", "Event")]
    rows = []
    for i, d in enumerate((base + timedelta(days=-1), base, base + timedelta(days=1))):
        at = datetime(d.year, d.month, d.day, 9, 0, tzinfo=ZoneInfo(LA))
        rows.append([recs.iso(at), f"Event {i}"])
    r = recs.mk("events", fs, rows, tz=LA)
    return r, recs.prof(r, [], [])


def test_agenda_today_label_correct_at_local_midnight_edge() -> None:
    """At 00:10 LA-local, yesterday's event (base - 1) must read `Yesterday`, never
    `Today` -- the discriminating word pair (`Yesterday` AND `Tomorrow` both present)
    pins `d0` to exactly `base`, the card-tz calendar day, not a UTC-derived one."""
    base = date(2026, 10, 8)
    rec, prof = _agenda_dates_rec(base)
    inp = recs.inp("Schedule", url="https://example.org/sched")
    cand = cand_for("agenda", rec, prof)
    assert cand is not None

    just_after = datetime(base.year, base.month, base.day, 0, 10, tzinfo=ZoneInfo(LA)).astimezone(UTC)
    clir = layout_span(cand, rec, prof, inp, Span.parse("d2x2"), just_after).clir
    texts = [ln for p in clir["prims"] if p["k"] == "text" for ln in p["lines"]]
    assert "Yesterday" in texts and "Tomorrow" in texts


# --------------------------------------------------------------------- viewer-zone flap
def test_viewer_zone_flap_day_table_and_agenda() -> None:
    """The same record laid out for two different viewer timezones must produce an
    IDENTICAL CLIR for day_table and agenda: every card-zone day label / event time is
    card-tz-bound; the client (not this bind) renders any viewer-tz prim later."""
    rec, prof, inp = recs.get("tides_chs")
    dt_cand = next(x for x in enumerate_cands(rec, prof, inp, recs.NOW) if x.form == "day_table")
    inp2 = recs.inp(inp.title, inp.ask, inp.source_url, inp.cadence_s)
    inp2.viewer_tz = LA
    for key in list(dt_cand.plans)[:4]:
        a = layout_span(dt_cand, rec, prof, inp, Span.parse(key), recs.NOW)
        b = layout_span(dt_cand, rec, prof, inp2, Span.parse(key), recs.NOW)
        assert a.hash == b.hash, key

    arec, aprof = _agenda_dates_rec(date(2026, 9, 25))
    ainp = recs.inp("Schedule", url="https://example.org/sched")
    acand = cand_for("agenda", arec, aprof)
    ainp2 = recs.inp(ainp.title, ainp.ask, ainp.source_url, ainp.cadence_s)
    ainp2.viewer_tz = "America/New_York"
    for key in ("d2x1", "d2x2"):
        a = layout_span(acand, arec, aprof, ainp, Span.parse(key), recs.NOW)
        b = layout_span(acand, arec, aprof, ainp2, Span.parse(key), recs.NOW)
        assert a.hash == b.hash, key
