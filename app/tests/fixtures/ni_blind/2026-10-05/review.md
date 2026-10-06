# Phase 0 baseline — fresh blind 50 (2026-10-05, v0.25.0 main b7069790, pack v1.3.0, local mlx/Qwen3.5-9B)

Harness: tools/ni-live-e2e.py --asks-file blind50.json (50 asks, 16 question kinds, 0 overlap with any eval set),
run in the dev image (container clock UTC). Outcomes: live 23 · links 22 · awaiting pick 4 · needs-key 1.

Live cards read by eye (the harness state is never the verdict):

| # | kind | ask | source | verdict |
|---|---|---|---|---|
| 1 | current_value | how warm is it in Boise right now | Open-Meteo | right |
| 2 | current_value | water temperature at Virginia Beach | NOAA CO-OPS | right |
| 3 | forecast | rain chances in Nashville this week | Open-Meteo | right (data); flat label/value presentation |
| 4 | forecast | snow forecast for Tahoe this weekend | NWS → Open-Meteo | right |
| 5 | forecast | surf forecast Huntington Beach | Open-Meteo marine | right (hourly series as raw columns) |
| 6 | forecast | hourly temps in Minneapolis today | NWS hourly | partial: times shown in the container's zone (UTC "3:00 AM" for 10 pm CDT) |
| 7 | next_event | next rocket launch from Vandenberg | Launch Library 2 | partial: list not filtered to Vandenberg (known gap: pad filter) |
| 8 | next_event | what day is the next new moon | USNO | right |
| 9 | next_event | next Bears game | TheSportsDB | right |
| 10 | schedule | NHL games tonight | TheSportsDB next events | WRONG: shows tomorrow's games under "tonight" (no window cut) |
| 11 | result | last night's Mariners result | MLB statsapi | honest empty (no game) |
| 12 | latest_items | latest NPR headlines | NPR feed | right |
| 13 | ranking | top 10 cryptocurrencies by market cap | CoinGecko | right |
| 14 | trend | US gas price trend this year | FRED | partial: latest value only, no series |
| 15 | trend | 30 year mortgage rate history | FRED | partial: latest value only, no series |
| 16 | status | Discord status | Statuspage | right |
| 17 | alerts | any flood warnings in Houston | NWS point alerts | right (honest empty) |
| 18 | alerts | tornado watches in Oklahoma right now | NWS state alerts | right (honest empty) |
| 19 | count | how many earthquakes above 4.0 today | USGS M2.5+ day | WRONG: counts M2.5+, the 4.0 threshold is ignored |
| 20 | count | how many active hurricanes in the Pacific | NWS tropical alerts for a point | WRONG: wrong source class; "0" from a point feed |
| 21 | lookup | when is Thanksgiving this year | Nager holidays | partial: upcoming-holidays list, the asked one not selected |
| 22 | map | where is the ISS right now on a map | wheretheiss | partial: lat/lon values, no map |
| 23 | compare | weather in Denver vs Salt Lake City | Open-Meteo | WRONG: one unnamed city's weather for a two-city compare |

Tally: right 13 · partial 6 · wrong 4 (of 23 live). Live rate 46%; wrong-card rate 8% of asks, 17% of live cards.
Links by kind: text_brief 3, result 2, status 2, lookup 2, map 2, image 2, the rest 1 each.
Awaiting pick: Cubs schedule this week, recent FDA recalls, NFC West standings right now, compare Ethereum and Solana prices.
Needs key: today's picture from the Mars rover (NASA APOD).

Classes behind the 4 wrong cards (feed the labeled sets; no per-ask patches):
- window cut on schedule + tonight (10); threshold on a count (19); compare kind unsupported but a single-place source accepted (23);
  category match too coarse for a count over a basin (20). All four are what the build-time FitVerdict (Phase 3) and the
  question-kind × answer-kind table (B2: compare → kv_grid/table or links; count with a threshold → honest gap) exist for.
