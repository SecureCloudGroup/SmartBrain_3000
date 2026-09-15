# NI case taxonomy — what users ask for, and what each ask must do

The living map behind the NI test matrix. Every row here is a REAL thing a
user could type in chat; every shipped row has a registry entry in
`app/tests/fixtures/ni_flow/cases.json` (single source of truth for the eval
gates AND the pytest suite — see "Adding a case" at the bottom). Status:

- **SHIPPED** — in the registry, exercised by the recorded + live engine gates.
- **WAVE** — landing in the current expansion wave.
- **ROADMAP(reason)** — a real ask we do not serve yet; the reason names the
  missing capability so a future wave picks it up deliberately, not by
  accident.
- **BY-DESIGN** — the honest outcome IS the feature (a refusal, a pause, a
  degrade note). These rows keep the safety edges regression-tested.

## A. Creation — external JSON through the flow

| # | Ask (in the user's words) | Dimension exercised | Status |
|---|---|---|---|
| A1 | "show me AAPL every 5 minutes" + a URL | explicit-URL value card, cadence parse | SHIPPED (aapl-5min) |
| A2 | "what's bitcoin worth right now, keep it updated" | vague subject → keyless recipe, default cadence | SHIPPED (btc-vague) |
| A3 | "track the weather in Kansas City" | recipe confirm + consented geocode two-step | SHIPPED (kc-weather) |
| A4 | "latest earthquakes above magnitude 5" | list card, `where` threshold, payload-grounded type reconcile | SHIPPED (quakes-m5) |
| A5 | "how many people are in space right now" | scalar count | SHIPPED (people-space) |
| A6 | "EUR to USD exchange rate, update hourly" | fx, cadence word | SHIPPED (eur-usd-hourly) |
| A7 | "top stories on Hacker News" | list-of-objects, repeat scene | SHIPPED (hn-frontpage) |
| A8 | "ISS location on a map, every minute" | display-hint degrade (map → value, honest note) | SHIPPED (iss-map) |
| A9 | "track bitcoin and ethereum in USD" | multi-subject → multi-field value card | SHIPPED (crypto-pair; list-hint degrades to value when no path is list-shaped) |
| A10 | "how many stars does <repo> have" | needle-in-haystack selection (~100-key response) | SHIPPED (github-stars) |
| A11 | "sunrise and sunset times for today" | string-typed value fields (ISO times) | SHIPPED (sunrise-times) |
| A12 | "temperature in Fahrenheit" | unit conversion (scale + offset transforms, authored from the request) | SHIPPED (offset op + deterministic °F authoring) |
| A13 | "alert me when bitcoin drops below 50000" | alert-carrying card (threshold + direction → §12 alert, edge-triggered) | SHIPPED (threshold+direction → §12 alert, value class only) |
| A14 | "today's temperature forecast as a chart" | series → spark scene | ROADMAP(assembler cannot author spark/series scenes; stress-POC proved selection works) |
| A15 | "the 5 biggest earthquakes today" | sort_by + top_n authored from intent | ROADMAP(assembler does not author sort/top-N pipelines) |
| A16 | "how much did bitcoin move since yesterday" | delta_prev authored from intent | ROADMAP(assembler does not author delta pipelines) |
| A17 | "stock quote with my Finnhub key" end-to-end | keyed recipe → awaiting_credential → key PUT → activate, LIVE | SHIPPED (L8 model-free; live keyed run stays operator-manual — needs a real key) |
| A18 | "every morning" / "twice a day" / "weekly" | cadence phrasing → minutes mapping | SHIPPED (cadence_free_phrasings rows — kind/validity gated, cadence free) |
| A19 | "every 10 seconds" | cadence floor clamp, honest copy | SHIPPED (cadence_free_phrasings row) |

## B. Refusals, pauses, degradations — the safety edges (BY-DESIGN outcomes)

| # | Ask | Honest outcome | Status |
|---|---|---|---|
| B1 | "days until Christmas" (no literal date) | `unsupported` — computed needs YYYY-MM-DD; chat asks for the date | SHIPPED (xmas-countdown) |
| B2 | "days until 2026-12-25" | computed source, zero egress, real day count | SHIPPED (pytest) |
| B3 | "track my custom sensor feed" (no URL, no recipe) | pause at `source` (AWAITING_SOURCE_PICK); chat presents candidates | SHIPPED (no-source-pause) |
| B4 | "my router status at http://192.168.1.1/..." | `failed(fetch)` — netguard refuses private ranges; never fetched | SHIPPED (lan-refused) |
| B5 | "BBC headlines from their RSS feed" (XML endpoint) | `failed(fetch)` — non-JSON body, honest class, no thrash | SHIPPED (xml-not-json) |
| B6 | a URL that 404s / returns an error body | `failed(fetch)`, one attempt, honest note | SHIPPED (dead-endpoint) |
| B7 | API needing a key, sampled without one (401) | `failed(fetch)` at sampling — the sample IS the probe | SHIPPED (dead-endpoint class covers it) |
| B8 | payload > 32KB sample cap | deterministic downsample, re-trim to 1 exemplar | SHIPPED (quakes + unit tests) |
| B9 | API keys outside the §4.1 path grammar (spaced keys) | unaddressable report → pick a different source | SHIPPED (derive tests; the Alpha-Vantage lesson) |
| B10 | "weather" recipe slot code can't derive, lookup fails | draft + `awaiting_params` → card Fill affordance, never a guess | SHIPPED (#429) |
| B11 | "show me <thing with no conceivable public API>" | model-level: honest "no source" conversation, flow never starts | BY-DESIGN (chat behavior; not machine-testable, guide-covered) |
| B12 | http_page scrape ask ("price from this webpage") | flow declines http_page; create_ni_item + §27 discipline is the door | BY-DESIGN (guide-covered; §27 suite owns it) |
| B13 | image ask ("US weather radar") | recorded gate covers format; flow has NO image path — authored via create_ni_item | ROADMAP(decide: image class joins the flow, or stays documented exclusion) |

## C. Lifecycle — the card exists; the user talks about it

All model-free (scripted through the REAL tool registry) in
`app/tests/test_ni_lifecycle.py` — the matrix that catches the field pain the
creation gates can't.

| # | Ask | What must happen | Status |
|---|---|---|---|
| L1 | "show me AAPL" when an AAPL card exists | duplicate-title guard names the card + points at update | SHIPPED (test_ni_lifecycle.py) |
| L2 | "make my AAPL card hourly" | update_ni_item cadence-only; state untouched | SHIPPED (test_ni_lifecycle.py) |
| L3 | "rename it to Apple Stock" | title-only update; state untouched | SHIPPED (test_ni_lifecycle.py) |
| L4 | "pause / resume the bitcoin card" | set_ni_item_enabled both ways | SHIPPED (test_ni_lifecycle.py) |
| L5 | "refresh it now" | run_ni_item_now; UUID-shape gate on invented ids | SHIPPED (test_ni_lifecycle.py) |
| L6 | "my weather card is broken — fix it" | remap re-enters at sampling on the item's OWN frozen source | SHIPPED (test_ni_lifecycle.py) |
| L7 | "change the source of my card" (flow-born) | refused with the remap pointer (the §29 door) | SHIPPED (test_ni_lifecycle.py) |
| L8 | keyed card: add key → activate | credential PUT → commission succeeds; unfilled → 409 | SHIPPED (test_ni_lifecycle.py) |
| L9 | "that number looks wrong" (C2) | validate wrong → back to draft, journal entry | SHIPPED (test_ni_lifecycle.py) |
| L10 | "delete the bitcoin card" | IRREVERSIBLE tier; cascades snapshots/journal/secrets | SHIPPED (test_ni_lifecycle.py) |
| L11 | "what's on my dashboard?" / "is it live yet?" | list/read state literacy; never claims live prematurely | SHIPPED (test_ni_lifecycle.py) |
| L12 | fill a needs_params slot from chat | model directs to the card's Fill (never invents the value) | SHIPPED (test_ni_lifecycle.py) |

## D. Internal sources — no egress (own suites; sampled here for literacy)

- internal.schedule / internal.kb / internal.ni composite / mcp_tool cards:
  each shipped with its own phase suite (§18/§22/§25). The lifecycle matrix
  includes one composite-literacy row; deeper matrices stay in their suites.
- ROADMAP: a flow path for internal sources ("make a card from my News
  check") — today these author via create_ni_item, which remains their door.

## E. Alerts & notification reach

- A13 (SHIPPED) authors the alert; §12 edge-trigger + cooldown machinery is
  already gate-tested in its own suite.
- "email me when…" — the blessed §16 carve-out is designed, NOT built.
  ROADMAP(email channel unbuilt).

## Adding a case (the 3-step recipe)

1. Add a row to `app/tests/fixtures/ni_flow/cases.json` — id, request,
   dimension, url/fixture, fields, klass, `expected` (engine_state +
   grounded intent), phrasings.
2. Record its fixture: `python tools/ni-flow-eval.py --record --only <id>`
   (writes the trimmed live response into the fixtures dir).
3. Run the gates: pytest picks the row up automatically (parametrized over
   the registry); `--recorded` / `--chaos` / `--engine` include it by id.

The registry is the single source of truth — the eval and the pytest suite
must never carry a private case list again.
