# G3 engine round — set G weather defects, live refetch (2026-10-13 00:39Z, keyless: open-meteo / api.weather.gov)

Source review: `blind50g_review.md` / `blind50g_listing.txt` (not shipped — scratchpad only). Each
card below was reproduced on the refetched sample through the tree's own `_try_answers_build` +
`_verify_frame` (`g3/cases.py`), then fixed; `results.json` carries the before/after shape this
file narrates. Raw samples sit beside this file.

| id | ask | source | before | after |
|---|---|---|---|---|
| G5 | UV index in Phoenix this afternoon | Open-Meteo UV | stat, hero "now" (the Day date), Max UV 5.2 a secondary | stat, hero **5.2** (Max UV), Day a secondary |
| G8 | chance of snow in Denver on Thursday | Open-Meteo forecast | conditions form, the Thursday cut never shown | conditions form, Day shown as a secondary |
| G9 | hourly rain forecast for Orlando today | NWS hourly | stat on `rain_this_hour` (one hour's reading) | `series_line` on `hours` (the hour-by-hour list; note `step_preferred`) |
| G10 | will it be foggy in Portland Oregon tomorrow morning | Open-Meteo hourly | `series_line`, a temperature curve; conditions never shown | `table`, conditions leads every hour, never a temperature curve |
| G40 | what time does the sun set in Denver tomorrow | Open-Meteo sun | stat, hero "in 23 h 44 m" (countdown only) | stat, hero **6:23 pm**, countdown riding under it |
| G49 | short weather summary for Seattle tomorrow | NWS periods | `ranked_list`, no row carries a period name | `ranked_list`, rows read "Tuesday Night" / "Wednesday" |
| G50 | today's forecast discussion for Kansas City | NWS periods | stat, hero "now" (the period start), 72 °F beside it | REFUSED — "no source reports a forecast discussion yet" |

Regression carry set (unchanged chosen answer; same or improved secondaries, lint red stays 0
throughout both runs): F11, F49, F50, E7, E10, G11. Full per-field before/after sits in
`g3/cases_base.json` / `g3/cases_after.json` (scratchpad, not shipped).
