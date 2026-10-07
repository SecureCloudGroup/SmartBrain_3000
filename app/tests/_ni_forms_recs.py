"""Synthetic + sample-derived DataRecords for the forms tests (test data, not pipeline).

Each builder returns (DataRecord, Profile, CardInput). Values for the 7 live cards
come from the staged board / corpus samples; stress records cover 0/1/n rows,
extremes, long and non-Latin text, negatives, all-null.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from smartbrain_3000.ni_forms.canon import canonical, fingerprint, sha256
from smartbrain_3000.ni_forms.types import (
    CardInput,
    Context,
    DataRecord,
    Field,
    FieldProfile,
    ImageRef,
    Profile,
    TimeProfile,
    check_record,
)

# PROTO points at the staged pagegraph file this suite still reads directly;
# the 11 live-card JSONs are vendored under the fixtures tree so no out-of-tree reads.
PROTO = Path(__file__).resolve().parent / "fixtures" / "ni_forms"
CORPUS = PROTO / "corpus"
NOW = datetime(2026, 9, 24, 18, 52, tzinfo=UTC)
NY = "America/New_York"


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def F(name, type="number", role="unknown", label=None, unit=None, precision=None, **kw):
    return Field(name=name, label=label or name.replace("_", " ").capitalize(), path=kw.pop("path", name),
                 type=type, role=role, unit=unit, precision=precision, **kw)


def mk(kind, fields, rows, *, host="example.org", as_of=None, tz=NY, flags=None, parts=None, image=None,
       row_meta=None, inferred=None, error=None, fetched=NOW):
    ctx = Context(fetched_at=iso(fetched), as_of=as_of, source_host=host, card_tz=tz, card_tz_src="data")
    r = DataRecord(v=1, kind=kind, fields=fields, rows=rows, context=ctx, producer="json_profile",
                   flags=flags or {}, parts=parts or {}, image=image, row_meta=row_meta, inferred=inferred or [],
                   error=error)
    r.fingerprint = "" if error else fingerprint(kind, fields, len(rows))
    r.data_hash = sha256(canonical([[f.name for f in fields], rows]))
    check_record(r)
    return r


def prof(rec, sigs, wants=(), now=NOW):
    tf = next((f for f in rec.fields if f.role in ("time", "date")), None)
    fut = past = days = 0
    grain = None
    span_h = None
    cov = False
    if tf is not None and rec.rows:
        i = rec.fields.index(tf)
        ts = []
        for r in rec.rows:
            v = r[i]
            if isinstance(v, str) and len(v) > 10:
                ts.append(datetime.fromisoformat(v))
            elif isinstance(v, str):
                ts.append(datetime.fromisoformat(v + "T12:00:00+00:00"))
        fut = sum(1 for t in ts if t > now)
        past = len(ts) - fut
        z = ZoneInfo(rec.context.card_tz)
        ds = {t.astimezone(z).date() for t in ts}
        days = len(ds)
        cov = now.astimezone(z).date() in ds
        if len(ts) > 1:
            span_h = (max(ts) - min(ts)).total_seconds() / 3600
            grain = "hour"
    fps = []
    for j, f in enumerate(rec.fields):
        vals = [r[j] for r in rec.rows]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        fps.append(FieldProfile(f.name, f.type, f.unit, f.role, len({str(v) for v in vals}),
                                sum(v is None for v in vals), min(nums) if nums else None,
                                max(nums) if nums else None, None, max([len(str(v)) for v in vals] or [0]), 0))
    return Profile(sig=sha256("|".join(sigs)), n_rows=len(rec.rows), fields=fps, signatures=list(sigs),
                   time=TimeProfile(tf.name if tf else None, grain, span_h, cov, fut, past, days),
                   wants=list(wants), wants_coverage={}, history_points=0)


def inp(title, ask=None, url="https://example.org/data", cadence=1800, card_id="t"):
    return CardInput(card_id=card_id, ask=ask or title, title=title, source_url=url, source_kind="http_json",
                     source_format="json", raw_path=None, http_status=200, content_type="application/json",
                     fetched_at=iso(NOW), cadence_s=cadence)


def L(p):
    return json.loads((CORPUS / p).read_text())


# ============================================================================ the 7 live cards
def nvda():
    ts = L("stock_nvda.json")["chart"]["result"][0]
    part_rows = [[iso(datetime.fromtimestamp(t, UTC)), round(c, 2)]
                 for t, c in zip(ts["timestamp"], ts["indicators"]["quote"][0]["close"]) if c is not None]
    part = mk("series", [F("t", "datetime", "time"), F("close", "currency", "measure", "Close", precision=2,
                                                        currency="USD")], part_rows, host="finnhub.io")
    fs = [F("c", "currency", "measure", "Price", precision=2, currency="USD"),
          F("d", "currency", "delta", "Change", precision=2, currency="USD"),
          F("dp", "percent", "delta_pct", "Change %", precision=4, scale="0..100"),
          F("h", "currency", "range_hi", "High", precision=2, currency="USD"),
          F("l", "currency", "range_lo", "Low", precision=2, currency="USD"),
          F("o", "currency", "open", "Open", precision=2, currency="USD"),
          F("pc", "currency", "reference", "Prev close", precision=2, currency="USD")]
    rows = [[223.86, -1.65, -0.7317, 224.63, 221.09, 223.22, 225.51]]
    r = mk("measure", fs, rows, host="finnhub.io", as_of="2026-09-24T18:21:00Z", parts={"intraday": part})
    return r, prof(r, ["measure_with_reference", "measure_with_range"]), inp("NVDA stock price", url="https://finnhub.io/api/v1/quote?symbol=NVDA", cadence=1800)


def nvda_quote_only():
    r, p, i = nvda()
    r2 = mk("measure", r.fields, r.rows, host="finnhub.io", as_of="2026-09-24T18:21:00Z")
    return r2, prof(r2, ["measure_with_reference"]), i


def btc():
    r = mk("measure", [F("usd", "currency", "measure", "Bitcoin · USD", path="bitcoin.usd", precision=0,
                         currency="USD")], [[84292]], host="api.coingecko.com", as_of="2026-09-24T18:18:00Z")
    return r, prof(r, ["single_measure"]), inp("Bitcoin price", url="https://api.coingecko.com/api/v3/simple/price", cadence=1980)


def wx():
    fs = [F("temperature_2m", "quantity", "measure", "Temperature", unit="degF", precision=1, path="current.temperature_2m"),
          F("wind_speed_10m", "quantity", "secondary", "Wind", unit="mph", precision=1, path="current.wind_speed_10m")]
    r = mk("measure", fs, [[69.7, 13.7]], host="api.open-meteo.com", as_of="2026-09-24T18:15:00Z")
    return r, prof(r, ["multi_measure"]), inp("Weather in Charleston, SC", url="https://api.open-meteo.com/v1/forecast", cadence=900)


def wx_rich():
    fs = [F("temperature_2m", "quantity", "measure", "Temperature", unit="degF", precision=1),
          F("apparent", "quantity", "secondary", "Feels like", unit="degF", precision=1),
          F("humidity", "percent", "secondary", "Humidity", precision=0, scale="0..100"),
          F("wind", "quantity", "secondary", "Wind", unit="mph", precision=1),
          F("gusts", "quantity", "secondary", "Gusts", unit="mph", precision=1),
          F("pressure", "quantity", "secondary", "Pressure", unit="hPa", precision=1),
          F("precip", "quantity", "secondary", "Rain", unit="in", precision=2),
          F("uv", "number", "secondary", "UV index", precision=1),
          F("weather_code", "category", "kind", "Conditions", unit="wmo")]
    hourly = mk("series", [F("t", "datetime", "time"), F("temp", "quantity", "measure", "Temperature", unit="degF", precision=1)],
                [[iso(NOW + timedelta(hours=h)), round(70 + 6 * math.sin((h + 3) / 4), 1)] for h in range(-6, 18)])
    r = mk("measure", fs, [[69.7, 72.4, 81, 13.7, 22.1, 1013.2, 0.0, 5.2, 2]], parts={"hourly": hourly},
           as_of="2026-09-24T18:15:00Z", host="api.open-meteo.com")
    return r, prof(r, ["multi_measure"]), inp("Weather in Charleston, SC", url="https://api.open-meteo.com/v1/forecast", cadence=900)


STORM_FIELDS = [F("name", "text", "name", "Name"), F("classification", "category", "status", "Class",
                                                        ordinal=["TD", "TS", "HU", "MH"]),
                F("intensity", "quantity", "value", "Intensity", unit="kt", precision=0),
                F("pressure", "quantity", "secondary", "Pressure", unit="mb", precision=0),
                F("movement_dir", "number", "secondary", "Moving", unit="deg", precision=0),
                F("movement_speed", "quantity", "secondary", "Speed", unit="mph", precision=0),
                F("bin", "identifier", "group", "Basin"),
                F("updated", "datetime", "as_of", "Updated")]
STORM_ROWS = [["Fay", "TS", 40, 1004, 210, 3, "AT1", "2026-09-21T21:00:00Z"],
              ["Odalys", "TS", 45, 1000, 300, 9, "EP1", "2026-09-21T21:00:00Z"],
              ["Polo", "HU", 75, 985, 290, 12, "EP2", "2026-09-21T21:00:00Z"],
              ["Nolo", "TD", 30, 1008, 270, 7, "CP1", "2026-09-21T21:00:00Z"]]


def storms(n=1):
    r = mk("records", STORM_FIELDS, STORM_ROWS[:n], host="www.nhc.noaa.gov", as_of="2026-09-21T21:00:00Z",
           flags={"sample": True})
    return r, prof(r, ["status_records"]), inp("Atlantic tropical storms and hurricanes",
                                               url="https://www.nhc.noaa.gov/CurrentStorms.json", cadence=900)


def hn(n=30):
    hits = L("hn_front.json")["hits"][:n]
    fs = [F("title", "text", "name", "Title"), F("url", "url", "link", "Link"),
          F("points", "number", "count", "Points", precision=0), F("num_comments", "number", "secondary", "Comments", precision=0),
          F("author", "text", "meta", "Author"), F("created_at", "datetime", "time", "Posted")]
    rows = [[h["title"][:120], h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}", h.get("points"),
             h.get("num_comments"), h.get("author"), h["created_at"][:19] + "Z"] for h in hits]
    r = mk("records", fs, rows, host="hn.algolia.com", as_of="2026-09-24T18:22:00Z")
    return r, prof(r, ["records_with_links", "ranked_records"]), inp("Hacker News top stories",
                                                                      url="https://hn.algolia.com/api/v1/search?tags=front_page", cadence=900)


def _loc(day, hm, tz=NY):
    h, m = hm
    return iso(datetime(2026, 9, day, h, m, tzinfo=ZoneInfo(tz)))


def tides_chs():
    ev = [(23, (18, 29), 5.9), (24, (0, 39), 1.0), (24, (6, 40), 5.5), (24, (12, 48), 0.8), (24, (19, 10), 6.0),
          (25, (1, 19), 0.8), (25, (7, 23), 5.8), (25, (13, 35), 0.7), (25, (19, 48), 6.1),
          (26, (1, 59), 0.5), (26, (8, 3), 6.1), (26, (14, 21), 0.5), (26, (20, 26), 6.1)]
    fs = [F("t", "datetime", "time", "Time", wallclock=True), F("height", "quantity", "value", "Height", unit="ft", precision=1),
          F("kind", "category", "kind", "Tide")]
    rows = []
    for i, (d, hm, v) in enumerate(ev):
        rows.append([_loc(d, hm), v, "High" if v > 3 else "Low"])
    r = mk("events", fs, rows, host="www.usharbors.com", as_of="2026-09-24T18:52:00Z")
    return r, prof(r, ["alternating_extrema", "events_with_kind", "dated_rows"], ["each_day", "times"]), \
        inp("Charleston Harbor SC tides", ask="show me tides for Charleston Harbor SC each day",
            url="https://www.usharbors.com/harbor/south-carolina/charleston-sc/tides/", cadence=86400)


def tides_wallace():
    txt = json.loads((CORPUS / "willyweather_wallace_pagegraph.json").read_text())["text"]
    import re
    blocks = re.split(r"\n\s*\n", txt.split("\n", 1)[1])
    rows = []
    datetime(2026, 9, 24, tzinfo=ZoneInfo(NY))
    vals = []
    for d, b in enumerate(blocks):
        for m in re.finditer(r"-\s*(\d{1,2}):(\d{2})\s*([ap]m)\s*([\d.]+)ft", b):
            h = int(m[1]) % 12 + (12 if m[3] == "pm" else 0)
            t = datetime(2026, 9, 24 + d, h, int(m[2]), tzinfo=ZoneInfo(NY))
            vals.append((iso(t), float(m[4])))
    for i, (t, v) in enumerate(vals):
        nb = [vals[j][1] for j in (i - 1, i + 1) if 0 <= j < len(vals)]
        rows.append([t, v, "High" if all(v > n for n in nb) else "Low"])
    fs = [F("t", "datetime", "time", "Time", wallclock=True), F("height", "quantity", "value", "Height", unit="ft"),
          F("kind", "category", "kind", "Tide", derived="extremum_kind_by_neighbors")]
    r = mk("events", fs, rows, host="tides.willyweather.com", as_of=None,
           inferred=["day_groups_positional", "extremum_kind_by_neighbors"], flags={"date_confidence": "inferred"})
    return r, prof(r, ["alternating_extrema", "events_with_kind"], ["extreme_high", "extreme_low", "height_value", "times"]), \
        inp("Wallace Creek - Rantowles Tide Times and Heights",
            ask="Wallace Creek - Rantowles Tide Times and Heights (show high, low, height and times)",
            url="https://tides.willyweather.com/sc/charleston-county/wallace-creek--rantowles.html", cadence=900)


LIVE = {"nvda": nvda, "btc": btc, "wx": wx, "storms": storms, "hn": hn, "tides_chs": tides_chs,
        "tides_wallace": tides_wallace}


# ============================================================================ per-form + stress
def quakes(n=12):
    feats = L("quakes_day.json")["features"][:n]
    fs = [F("place", "text", "name", "Place"), F("mag", "number", "value", "Magnitude", precision=1),
          F("time", "datetime", "time", "Time"), F("lat", "lat", "lat", "Lat"), F("lon", "lon", "lon", "Lon"),
          F("url", "url", "link", "Link")]
    rows = [[f["properties"]["place"] or "Unknown", f["properties"]["mag"],
             iso(datetime.fromtimestamp(f["properties"]["time"] / 1000, UTC)),
             f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0], f["properties"]["url"]] for f in feats]
    r = mk("records", fs, rows, host="earthquake.usgs.gov", as_of=iso(NOW))
    return r, prof(r, ["geo_points", "ranked_records"], ["where"]), inp("Earthquakes today", url="https://earthquake.usgs.gov/x")


def languages():
    d = L("gh_languages.json")
    tot = sum(d.values())
    fs = [F("lang", "category", "name", "Language"), F("bytes", "number", "value", "Bytes", precision=0),
          F("share", "percent", "share", "Share", precision=1, scale="0..100", derived="share_of_total")]
    rows = [[k, v, round(100 * v / tot, 1)] for k, v in d.items()]
    r = mk("records", fs, rows, host="api.github.com")
    return r, prof(r, ["percent_of_whole", "ranked_records"]), inp("Repo languages", url="https://api.github.com/x")


def crypto_top(n=10):
    d = L("crypto_top10.json")[:n]
    fs = [F("rank", "number", "rank", "#", precision=0), F("name", "text", "name", "Coin"),
          F("price", "currency", "value", "Price", currency="USD"),
          F("chg", "percent", "delta_pct", "24h", precision=2, scale="0..100"),
          F("mcap", "currency", "secondary", "Market cap", currency="USD", precision=0),
          F("vol", "currency", "secondary", "Volume", currency="USD", precision=0)]
    rows = [[c["market_cap_rank"], c["name"], c["current_price"], round(c["price_change_percentage_24h"] or 0, 2),
             c["market_cap"], c["total_volume"]] for c in d]
    r = mk("records", fs, rows, host="api.coingecko.com")
    return r, prof(r, ["ranked_records"], ["rank"]), inp("Top 10 crypto", url="https://api.coingecko.com/x")


def fx():
    d = L("fx_usd.json")
    fs = [F("cur", "category", "name", "Currency"), F("rate", "number", "value", "Rate")]
    rows = [[k, v] for k, v in d["rates"].items()]
    r = mk("records", fs, rows, host="api.frankfurter.app", as_of="2026-09-24T00:00:00Z")
    return r, prof(r, ["ranked_records"], ["compare"]), inp("USD exchange rates", url="https://api.frankfurter.app/x")


def compare3():
    fs = [F("sym", "text", "name", "Symbol"), F("price", "currency", "value", "Price", precision=2, currency="USD"),
          F("chg", "percent", "delta_pct", "Change", precision=3, scale="0..100")]
    rows = [["INTC", 127.39, 3.907], ["NVDA", 224.58, -0.412], ["AMD", 161.2, 1.25], ["QCOM", 170.05, -2.3]]
    r = mk("records", fs, rows, host="query1.finance.yahoo.com")
    return r, prof(r, ["ranked_records"], ["compare"]), inp("Chip stocks", ask="compare INTC vs NVDA vs AMD vs QCOM", url="https://query1.finance.yahoo.com/x")


def series_single(n=90, neg=False):
    t0 = NOW - timedelta(days=n)
    rows = [[iso(t0 + timedelta(days=i)), round(100 + 20 * math.sin(i / 9) + i * 0.3 - (130 if neg else 0), 2)] for i in range(n)]
    fs = [F("t", "datetime", "time", "Date"), F("v", "currency", "measure", "Close", precision=2, currency="USD")]
    r = mk("series", fs, rows, host="query1.finance.yahoo.com")
    return r, prof(r, ["time_series_regular"], ["trend"]), inp("NVDA 3 months", ask="NVDA price over the last 3 months", url="https://query1.finance.yahoo.com/x")


def series_multi():
    rows = []
    for s, (base, amp) in {"London": (14, 4), "Tokyo": (22, 5), "New York": (19, 6)}.items():
        for h in range(48):
            rows.append([iso(NOW - timedelta(hours=24) + timedelta(hours=h)), s, round(base + amp * math.sin(h / 4), 1)])
    fs = [F("t", "datetime", "time", "Time"), F("city", "category", "series_id", "City"),
          F("temp", "quantity", "value", "Temperature", unit="degC", precision=1)]
    r = mk("series", fs, rows, host="api.open-meteo.com")
    return r, prof(r, ["multi_series", "time_series_regular"], ["compare", "trend"]), inp("Temps in 3 cities", url="https://api.open-meteo.com/x")


def series_bars():
    rows = [[iso(NOW - timedelta(days=13 - i)), [0.2, 0, 0, 1.4, 0.3, 0, 0, 0, 2.2, 0.1, 0, 0, 0.6, 0][i]] for i in range(14)]
    fs = [F("t", "datetime", "time", "Day"), F("rain", "quantity", "value", "Rain", unit="in", precision=1, agg="per_interval")]
    r = mk("series", fs, rows, host="api.open-meteo.com")
    return r, prof(r, ["time_series_regular"], ["trend"]), inp("Rain, last 2 weeks", url="https://api.open-meteo.com/x")


def matrix():
    t0 = datetime(2026, 9, 17, tzinfo=ZoneInfo("Europe/Berlin"))
    rows = [[iso(t0 + timedelta(hours=h)), round(80 + 60 * math.sin((h % 24 - 6) / 24 * 2 * math.pi) + (h // 24) * 3 - (499.99 if h == 110 else 0), 2)] for h in range(24 * 7)]
    fs = [F("t", "datetime", "time", "Hour"), F("price", "currency", "value", "Price", currency="EUR", precision=2)]
    r = mk("series", fs, rows, host="www.smard.de", tz="Europe/Berlin")
    return r, prof(r, ["matrix", "time_series_regular"], ["extreme_low"]), inp("Power price this week", ask="cheapest hour to charge this week", url="https://www.smard.de/x")


def agenda_rec():
    d = L("calendar_today.synthetic.json")["events"]
    fs = [F("title", "text", "name", "Event"), F("start", "datetime", "time", "Start"),
          F("end", "datetime", "time_end", "End"), F("location", "text", "meta", "Where")]
    rows = []
    for e in d:
        if e.get("all_day"):
            continue
        rows.append([e["title"][:120], iso(datetime.fromisoformat(e["start"])), iso(datetime.fromisoformat(e["end"])), e.get("location")])
    r = mk("events", fs, rows, host="calendar.local")
    return r, prof(r, ["events_with_kind"], ["now", "next"]), inp("Today's calendar", url="https://calendar.example.org/x", cadence=300)


def next_rec():
    fs = [F("name", "text", "name", "Game"), F("t", "datetime", "time", "Start"), F("venue", "text", "meta", "Venue")]
    rows = [["Braves at Mets", "2026-09-24T23:10:00Z", "Citi Field"], ["Braves at Mets", "2026-09-25T23:10:00Z", "Citi Field"],
            ["Phillies at Braves", "2026-09-26T23:20:00Z", "Truist Park"]]
    r = mk("events", fs, rows, host="statsapi.mlb.com")
    return r, prof(r, ["events_with_kind"], ["next"]), inp("Next Braves game", url="https://statsapi.mlb.com/x", cadence=3600)


def dated_rows():
    fs = [F("date", "date", "date", "Day"), F("hi", "quantity", "range_hi", "High", unit="degF", precision=0),
          F("lo", "quantity", "range_lo", "Low", unit="degF", precision=0), F("desc", "text", "name", "Forecast"),
          F("pop", "percent", "secondary", "Rain", precision=0, scale="0..100")]
    words = ["Sunny", "Mostly sunny", "Chance showers", "Thunderstorms likely", "Partly cloudy", "Sunny", "Windy"]
    rows = [[(NOW.date() + timedelta(days=i)).isoformat(), 78 - i, 52 + (i % 3), words[i], [0, 10, 40, 70, 20, 0, 10][i]] for i in range(7)]
    r = mk("records", fs, rows, host="api.weather.gov", tz="America/Denver")
    return r, prof(r, ["dated_rows"], ["each_day"]), inp("Denver 7-day", url="https://api.weather.gov/x", cadence=3600)


def status_many():
    d = L("tfl_status.json")
    fs = [F("name", "text", "name", "Line"), F("status", "category", "status", "Status", ordinal=["Good Service", "Minor Delays", "Severe Delays", "Part Suspended", "Suspended"])]
    rows = [[x["name"], x["lineStatuses"][0]["statusSeverityDescription"]] for x in d]
    rows[3][1] = "Minor Delays"
    rows[7][1] = "Part Suspended"
    r = mk("records", fs, rows, host="api.tfl.gov.uk")
    return r, prof(r, ["status_records"]), inp("Tube status", url="https://api.tfl.gov.uk/x", cadence=300)


def table_rec():
    d = L("epl_table.json")["children"][0]["standings"]["entries"]
    fs = [F("rank", "number", "rank", "#", precision=0), F("team", "text", "name", "Team"),
          F("p", "number", "secondary", "P", precision=0), F("w", "number", "secondary", "W", precision=0),
          F("d", "number", "secondary", "D", precision=0), F("l", "number", "secondary", "L", precision=0),
          F("gd", "number", "delta", "GD", precision=0), F("pts", "number", "value", "Pts", precision=0)]
    rows = []
    for i, e in enumerate(d):
        st = {s["name"]: s.get("value") for s in e["stats"]}
        rows.append([i + 1, e["team"]["shortDisplayName"], st.get("gamesPlayed"), st.get("wins"), st.get("ties"),
                     st.get("losses"), st.get("pointDifferential"), st.get("points")])
    r = mk("records", fs, rows, host="site.api.espn.com")
    return r, prof(r, ["ranked_records"], ["rank"]), inp("Premier League table", url="https://site.api.espn.com/x", cadence=3600)


def text_rec():
    q = L("quote_today.json")[0]
    fs = [F("q", "text", "text_body", "Quote"), F("a", "text", "name", "Author")]
    r = mk("text", fs, [[q["q"], q["a"]]], host="zenquotes.io")
    return r, prof(r, ["text_passage"], ["read"]), inp("Quote of the day", url="https://zenquotes.io/x", cadence=86400)


def rtl_rec():
    d = L("ar_wiki_cairo.json")
    ext = d["extract"]
    fs = [F("title", "text", "name", "Title"), F("extract", "text", "text_body", "Summary"), F("url", "url", "link", "Link")]
    r = mk("text", fs, [[d["title"], ext[:1500], "https://ar.wikipedia.org/wiki/%D8%A7%D9%84%D9%82%D8%A7%D9%87%D8%B1%D8%A9"]],
           host="ar.wikipedia.org")
    return r, prof(r, ["text_passage"], ["read"]), inp("القاهرة", ask="ملخص ويكيبيديا عن القاهرة", url="https://ar.wikipedia.org/x", cadence=86400)


def jp_rec():
    fs = [F("name", "text", "name", "祝日"), F("date", "date", "date", "日付")]
    rows = [["秋分の日", "2026-09-23"], ["スポーツの日", "2026-10-12"], ["文化の日", "2026-11-03"], ["勤労感謝の日", "2026-11-23"]]
    r = mk("records", fs, rows, host="holidays-jp.github.io", tz="Asia/Tokyo")
    return r, prof(r, ["dated_rows", "events_with_kind"], ["next"]), inp("日本の祝日", ask="次の祝日はいつ", url="https://holidays-jp.github.io/x", cadence=86400)


def image_rec(w=1024, h=1024, frames=1):
    fs = [F("title", "text", "name", "Title")]
    r = mk("image", fs, [["Latest image"]], host="sdo.gsfc.nasa.gov",
           image=ImageRef(sha256="ab" * 32, mime="image/jpeg", w=w, h=h, frames=frames), as_of="2026-09-24T18:40:00Z")
    return r, prof(r, ["image"]), inp("Sun now", url="https://sdo.gsfc.nasa.gov/x", cadence=900)


def progress_rec():
    fs = [F("name", "text", "name", "Milestone"), F("closed", "number", "value", "Closed", precision=0),
          F("total", "number", "goal", "Total", precision=0)]
    rows = [["September", 41, 60], ["October", 12, 45], ["Backlog", 380, 350]]
    r = mk("records", fs, rows, host="api.github.com")
    return r, prof(r, ["progress_to_goal"], ["count"]), inp("Milestones", url="https://api.github.com/x", cadence=3600)


def kv_rec():
    fs = [F(k, "number", "value", k, precision=4) for k in ("EUR", "GBP", "JPY", "MXN", "CHF", "CAD", "AUD")]
    r = mk("measure", fs, [[0.8797, 0.7565, 158.85, 17.5841, 0.8012, 1.3571, 1.5132]], host="api.frankfurter.app")
    return r, prof(r, ["multi_measure"]), inp("USD rates", url="https://api.frankfurter.app/x", cadence=86400)


# stress
def empty_status():
    r = mk("records", STORM_FIELDS, [], host="www.nhc.noaa.gov")
    return r, prof(r, ["empty", "status_records"]), inp("Active storms", url="https://www.nhc.noaa.gov/x")


def long_list(n=40):
    fs = [F("title", "text", "name", "Title"), F("url", "url", "link", "Link"), F("score", "number", "count", "Score", precision=0)]
    rows = [[("Ünïcødé Łódź Beşiktaş Hülkenberg — a very long headline that keeps going well past any sane width " * 2)[:120] if i % 3 == 0
             else f"Item {i + 1}: short", f"https://example.org/{i}", 1000 - i * 7] for i in range(n)]
    r = mk("records", fs, rows, host="example.org")
    return r, prof(r, ["records_with_links", "ranked_records"]), inp("Forty links")


def negatives():
    fs = [F("agency", "text", "name", "Agency"), F("outlays", "currency", "value", "Outlays", currency="USD", precision=0)]
    rows = [["Health and Human Services", 1_700_000_000_000], ["Social Security", 1_500_000_000_000],
            ["Defense", 870_000_000_000], ["Treasury", 1_100_000_000_000], ["Undistributed offsetting receipts", -150_000_000_000],
            ["Education", 250_000_000_000]]
    r = mk("records", fs, rows, host="api.fiscaldata.treasury.gov")
    return r, prof(r, ["ranked_records"], ["rank"]), inp("Federal outlays by agency")


def all_null():
    fs = [F("wave", "quantity", "value", "Wave", unit="ft"), F("t", "datetime", "time", "Time")]
    r = mk("series", fs, [], host="marine-api.open-meteo.com", error={"code": "all_null", "detail": "all values null"})
    return r, prof(r, ["empty"]), inp("Tahoe waves")


def mapped_quakes():
    return quakes(12)


FORM_FIXTURES = {
    "stat": ["nvda", "nvda_quote_only", "btc"],
    "conditions": ["wx", "wx_rich"],
    "kv_grid": ["kv_rec", "fx"],
    "compare": ["compare3", "fx", "negatives"],
    "bars": ["languages", "negatives", "crypto_top"],
    "series_line": ["series_single", "series_multi", "series_bars"],
    "heatmap": ["matrix"],
    "event_curve": ["tides_chs", "tides_wallace"],
    "next_event": ["next_rec", "tides_chs", "jp_rec"],
    "agenda": ["agenda_rec", "next_rec"],
    "day_table": ["dated_rows", "tides_chs", "tides_wallace", "jp_rec"],
    "entity_list": ["storms", "status_many", "empty_status"],
    "ranked_list": ["hn", "long_list", "quakes"],
    "table": ["table_rec", "crypto_top", "quakes"],
    "text_brief": ["text_rec", "rtl_rec"],
    "image": ["image_rec"],
    "progress": ["progress_rec"],
    "map_lite": ["quakes"],
}


def get(name):
    fn = globals()[name]
    return fn()


# ---------------------------------------------------------------------------- extremes
def huge_value():
    fs = [F("v", "currency", "measure", "Market cap", precision=2, currency="USD"),
          F("ref", "currency", "reference", "Yesterday", precision=2, currency="USD")]
    r = mk("measure", fs, [[1234567890123.45, 1234000000000.12]], host="api.example-markets.org")
    return r, prof(r, ["measure_with_reference"]), inp("A very long card title that keeps going and going past two lines of header text")


def one_row_list():
    fs = [F("title", "text", "name", "Title"), F("url", "url", "link", "Link"), F("score", "number", "count", "Score", precision=0)]
    r = mk("records", fs, [["The only item — 東京の天気 · ملخص ويكيبيديا", "https://example.org/1", 7]], host="example.org")
    return r, prof(r, ["records_with_links"]), inp("One link")


def emoji_list():
    fs = [F("title", "text", "name", "Title"), F("url", "url", "link", "Link")]
    rows = [["🚀 Launch day recap ✨", "https://example.org/a"], ["http only link", "http://example.org/b"],
            ["userinfo link", "https://user:pw@example.org/c"], ["Łódź · Beşiktaş · Hülkenberg", "https://example.org:8443/d"]]
    r = mk("records", fs, rows, host="example.org")
    return r, prof(r, ["records_with_links"]), inp("Links with odd text")


FORM_FIXTURES["stat"] += ["huge_value"]
FORM_FIXTURES["ranked_list"] += ["one_row_list", "emoji_list"]
STRESS = ["empty_status", "long_list", "series_multi", "image_rec", "rtl_rec", "negatives", "all_null", "one_row_list",
          "huge_value", "emoji_list", "jp_rec"]
