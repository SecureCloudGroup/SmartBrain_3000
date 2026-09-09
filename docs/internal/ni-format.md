# Neural Interface item format (v1)

Internal contract for the Neural Interface (NI) subsystem: the item spec, the scene
grammar, the pipeline, the data contract, and the lifecycle state machine. The Python
validators (`app/smartbrain_3000/ni.py`) and the TypeScript renderer
(`web/src/lib/ni/`) both implement THIS document; when they disagree, this document
wins and both are bugs.

Design laws (decided 2026-09-08, full rationale in the operator's design record):

- **Deterministic core, AI at the edges.** Models run at design time, commissioning,
  and repair time only. The runtime path — fetch → extract → transform → contract
  check → bind → render — is pure deterministic code. The same payload always
  produces the same scene.
- **Authorization is not trust.** The user's choice of a source authorizes the
  connection; the fetched content stays untrusted forever. Every model output that
  was derived from fetched bytes must be a closed-schema, egress-inert artifact
  (scene trees, extract paths, transform ops — never URLs, headers, schedules, or
  free-form markup).
- **Template-native.** Every item is an instantiated template. `params` declare the
  slots; local items simply have all slots filled. Export-as-template is a strip of
  filled values back to slots, never a schema change.
- **Nothing renders that did not validate.** The scene validator and the payload
  binder run server-side on every snapshot write; the client renderer additionally
  refuses unknown node types. Malformed data can never reach the page.

## 1. Storage (migration 40)

Follows the house sealed-body + plaintext-operational-columns convention
(schedules/feeds):

- `ni_items` — plaintext: `id TEXT PRIMARY KEY`, `enabled BOOLEAN`, `state TEXT`
  (§6), `interval_minutes INTEGER`, `last_checked TIMESTAMP`, `last_status TEXT`
  (host-free), `consecutive_failures INTEGER`, `first_failure_at TIMESTAMP`
  (streak marker; §6), `position INTEGER`, `spec_rev INTEGER`,
  `created_at TIMESTAMP`, `updated_at TIMESTAMP`; sealed body (AAD
  `ni_item:<id>`): the spec (§2).
- `ni_snapshots` — `item_id TEXT`, `slot TEXT` (`latest` | `last_good` | `preview`;
  v2 adds `history` (§11) and `alert_state` (§12) — the slot set is app-level, no
  schema change),
  `nonce BLOB`, `ciphertext BLOB` (AAD `ni_snapshot:<item_id>:<slot>`), `ok BOOLEAN`,
  `created_at TIMESTAMP`, `PRIMARY KEY (item_id, slot)`. Sealed body: the bound
  payload (§4.3) — the data, not the scene.
- `ni_revisions` — `item_id TEXT`, `rev INTEGER`, `nonce BLOB`, `ciphertext BLOB`
  (AAD `ni_revision:<item_id>:<rev>`), `origin TEXT` (`user` | `agent` |
  `repair_l1` | `repair_l2` | `template`), `created_at TIMESTAMP`, `PRIMARY KEY
  (item_id, rev)`. Keep the last 10 per item; pruning in code.
- `ni_runs` — `item_id TEXT`, `ts TIMESTAMP`, `status TEXT`, `duration_ms INTEGER`,
  `error TEXT` (host-free class only), `contract_ok BOOLEAN`. Keep the last 50 per
  item; pruning in code. Plaintext throughout — operational telemetry, no content.

No foreign keys; `NIStore.delete` cascades in code (feeds precedent).

## 2. Item spec (sealed body of `ni_items`)

```json
{
  "version": 1,
  "title": "Spending this week",
  "goal": "<the user's words, verbatim — never rewritten>",
  "params": {
    "symbol": {"label": "Ticker symbol", "kind": "string", "value": "ACME"}
  },
  "source": { ... §3 ... },
  "pipeline": [ ... §4 ... ],
  "scene": { ... §5 ... },
  "display": {"size": "small"},
  "contract": null,
  "repair_policy": {"l1": true, "l2_frontier": false},
  "model": null
}
```

- `params.*.kind` ∈ `string | number | secret`. A `secret` param's `value` is always
  a SecretStore key name in the `ni:<item_id>:` namespace, never the secret itself.
  `{{param:NAME}}` placeholders may appear only where a field's schema says so.
- `display.size` ∈ `small | wide` (wide spans two grid columns).
- `contract` is system-written at commissioning (§7); the agent may never set it.
- `model` optionally overrides the `ni` route for `model` sources (schedules.model
  precedent).
- Interval floor: `interval_minutes >= 1`, clamped (never an error). One-shot items
  do not exist in v1; `enabled=false` is the pause.
- v2 optional keys: `"history"` (§11) and `"alerts"` (§12). A v1 app's validator
  rejects specs carrying them (closed-key law) — that is the intended forward
  refusal.

## 3. Sources (v1: three types, closed set)

`http_json`:
```json
{"type": "http_json", "url": "https://api.example.com/quote?sym={{param:symbol}}",
 "headers": {"X-Api-Key": {"$secret": "ni:<item_id>:api_key"}}}
```
- Fetched via `netguard.safe_fetch_json` only. `validate_public_url` runs on the
  param-substituted URL at every create / update / commission (skipped when a
  referenced string param is still empty; commission re-checks). The URL's
  **scheme + authority is frozen literal**: `{{param:...}}` may appear only inside
  the path or query, never in the scheme/userinfo/host/port — validators refuse
  a placeholder there. Param values that DO land in the URL are percent-encoded
  at substitution time (`urllib.parse.quote(safe="")`) so a value can never
  rewrite URL structure.
- Header values are literal strings or `{"$secret": "ni:..."}` refs — nothing
  else. `{{param:...}}` is forbidden inside plain header values (a param cannot
  smuggle auth-shaped bytes into a header). Auth-shaped literal header names
  (`authorization`, `proxy-authorization`, `cookie`, any name containing
  `token`/`secret`/`key`) are refused unless the value is a `$secret` ref.
  `$secret` refs must start with `ni:` at spec validation; the loader further
  enforces the full `ni:{item_id}:` scoping (a spec cannot borrow another
  item's credentials).
- `$secret` values resolve at fetch time by the engine; the resolved secret is
  host-bound: it attaches only when the request host equals the host recorded
  when the credential was entered (`put_credential` IDNA-lowercases the host so
  the compare matches `urlparse().hostname`). Any other host refuses the fetch
  outright (`secret_host_mismatch` → permanent broken). Secrets never ride an
  http request (`secret_requires_https` refusal).
- **Redirect discipline** (credential exfiltration guard): whenever the fetch
  carries ANY header (secret or literal), the engine passes
  `allow_redirects=False` through `safe_fetch_json` / `_guarded_get`, so a 3xx
  raises `FetchError("redirect refused")` before the header re-sends to a
  rewritten host. Header-free requests keep the default redirect behavior.
- Response caps: 2 MB, `application/json`/`text/` content types, 8s per-read
  timeout (netguard defaults).

`model`:
```json
{"type": "model", "instruction": "Write a one-paragraph morning briefing …"}
```
- Runs through `gateway` on the `ni` route (new capability, default local), or the
  item's `model` override. Output is plain text handed to the pipeline as
  `{"text": "<output>"}`. The instruction is user-visible spec content.

`internal.schedule`:
```json
{"type": "internal.schedule", "schedule_id": "<id>"}
```
- Reads the newest `schedule_runs` row for that schedule (message only). Zero
  egress. Payload to the pipeline: `{"message": str, "status": str, "ts": str}`.

## 4. Pipeline

An ordered list of stages; each consumes and produces a JSON value. The implicit
final stage is always bind + render-validate (§4.3).

### 4.1 `extract`

```json
{"op": "extract", "paths": {"price": "quote.latest", "rows": "items[0:5]"}}
```
Path grammar (shared with scene bindings, one implementation):

```
path     = segment ("." segment)*
segment  = key | key "[" index "]" | key "[" slice "]"
key      = [A-Za-z_][A-Za-z0-9_-]*
index    = "-"? digits
slice    = digits? ":" digits?
```
A path that fails to resolve is a stage failure (→ run fails, §6). Output is the
object of named extracts; downstream stages and the scene see only these names.

### 4.2 `transform`

```json
{"op": "transform", "apply": [
  {"fn": "round", "field": "price", "digits": 2},
  {"fn": "sort_by", "field": "rows", "key": "amount", "dir": "desc"},
  {"fn": "top_n", "field": "rows", "n": 5}
]}
```
Closed function set v1: `round(field, digits)`, `scale(field, factor)`,
`rename(field, to)`, `pick(field, keys)` (for lists of objects: keep only these
keys), `sort_by(field, key?, dir)`, `top_n(field, n<=50)`. All pure; type
mismatches are stage failures, never coercion guesses.

Added in v2 (same closed-set discipline):
- Aggregates over a list-of-objects field: `sum(field, key, as)`, `avg(field,
  key, as)`, `min(field, key, as)`, `max(field, key, as)`, `count(field, as)` —
  each writes a NEW numeric output named `as` (the list is untouched; `as` must
  not collide with an existing output; numeric key values required, mismatch =
  stage failure; empty list ⇒ count 0, others = stage failure `empty_aggregate`).
- `delta_prev(field, series, as)` — current numeric `field` minus the LAST point
  of history series `series` (§11); writes `{value, direction: "up"|"down"|
  "flat"}` to `as`. When the series is empty (first run) it writes
  `{value: 0, direction: "flat"}` — never a failure, so commissioning passes.

### 4.3 Bind + render-validate (implicit, always last)

Binding walks the scene, resolves every `{"$bind": path}` and `{{path}}`
interpolation against the pipeline output, enforces §5 caps, and produces the
**bound payload**: the scene with data inlined — this is what `ni_snapshots`
stores and what the client renders. Any unresolved binding, type mismatch, or cap
violation fails the run; `last_good` keeps rendering.

## 5. Scene grammar (v1)

A scene is a tree of nodes. Closed node set; unknown `type` = validation failure
server-side and refusal client-side.

Layout:
- `{"type": "stack", "dir": "v" | "h", "gap": "sm"|"md", "children": [...]}`
- `{"type": "grid", "cols": 2..4, "children": [...]}`
- `{"type": "divider"}`

Content (each `value`-like prop accepts a JSON literal or `{"$bind": "<path>"}`):
- `{"type": "text", "value": "…{{path}}…", "role": "title"|"label"|"value"|"caption",
   "tone": "default"|"muted"|"accent"|"ok"|"warn"|"danger", "size": "sm"|"md"|"lg"}`
- `{"type": "number", "value": …, "format": "plain"|"compact"|"percent"|"currency",
   "unit": "…", "tone": …, "size": …}`
- `{"type": "chip", "value": …, "kind": ""|"accent"|"ok"|"warn"|"danger"}`
- `{"type": "bar", "value": …, "max": …, "tone": …}`  (rendered 0..1 of max)
- `{"type": "icon", "name": "<lucide subset name>", "tone": …}`
- `{"type": "repeat", "items": {"$bind": "<path to list>"}, "max": 20,
   "template": <node>}` — inside `template`, paths beginning `item.` resolve
   against the current list element.

Added in v2:
- `{"type": "spark", "points": {"$bind": "history.<name>"}, "kind": "line"|"bars",
   "tone": …}` — a sparkline over a history series (§11) or any bound list of
   numbers / `{t, v}` points; ≤ 500 points (excess = bind failure); rendered as
   hand-rolled inline SVG (polyline / rects), stroke/fill from the tone token,
   fixed viewBox, width 100%, no axes. Non-numeric point = bind failure.
- `{"type": "gauge", "value": …, "min": 0, "max": …, "tone": …, "label": "…"}` —
  an arc gauge; `max > min` required, value clamped visually to [min, max]
  (clamping is presentation; the raw value still binds for conditions).

Conditions (v2, **bind-time — the client never sees them**): any CONTENT node may
carry `"when": [rule…]` (≤ 5 rules):

```json
{"when": [{"left": {"$bind": "delta.value"}, "op": "lt", "right": 0,
           "set": {"tone": "danger"}}]}
```
- `left`/`right`: `{"$bind": path}` or a JSON scalar. `op` ∈ `lt|le|gt|ge|eq|ne`
  (ordering ops require numbers on both sides — mismatch = bind failure `when_type`;
  eq/ne compare scalars strictly, no coercion).
- `set`: `{"tone": <tone enum>}` and/or `{"hidden": true}`. Rules evaluate in
  order at BIND time; later tone wins; `hidden` drops the node from the bound
  payload entirely. The stored/bound payload carries only the RESULT — `when`
  never survives binding, and the client validator continues to REJECT it in a
  bound payload. Determinism is preserved: conditions are pure functions of the
  run's data.

Rules:
- **Text is text.** Bound strings render as plain text — no markdown, no HTML, no
  links. (Injection point P3: a lying string can render, but it cannot become UI.)
- Tones map to design tokens only; no raw colors anywhere in the grammar.
- Caps: ≤ 100 nodes after repeat expansion, depth ≤ 8, text ≤ 2000 chars, repeat
  `max` ≤ 50. Enforced at bind time and again by the renderer.
- Reserved for later phases (validators must REJECT, so old apps refuse new
  scenes rather than mis-render them): `image`, `on_tap` (behaviors).

## 6. Lifecycle state machine (fully enumerated)

States (plaintext `ni_items.state`):
`draft → commissioning → live ⇄ degraded ⇄ failing → broken`, plus `paused`
(user, from any post-draft state; resume returns to `commissioning` if the item
was never live, else `live`).

Approval IS consent. NI write tools are REVIEWED + egress + non-rememberable, so
the create/update handler only ever runs after explicit human approval. The
default landing state for a fresh `create_ni_item` is therefore
**`commissioning`** (not `draft`) — the C1 tick happens on the next scheduler
pass. Two exceptions land it in `draft` instead: (a) any `secret`-kind param
whose value is empty (the credential must be entered via
`PUT /credential` before the engine tries), and (b) an explicit `draft: true`
in the create args (the agent's "show me first" affordance).

An `update_ni_item` whose source effectively changed sends the item straight to
`commissioning` too — the approved update card IS the re-consent. Every
`update_spec` also strips `_c2_ok` and `contract` from the sealed body and
resets `consecutive_failures` + `first_failure_at` (both attestations describe a
specific spec and are meaningless once the spec moves).

- `draft` — spec + `preview` snapshot (dummy data, agent-invented, validated
  against the same grammar). Never fetches. Never expires in v1. Left by the
  Activate button (`POST /api/ni/items/{id}/commission` — refuses 409 unless
  state is `draft` and every secret param has a value).
- `commissioning` — entered on create (default), on activate, or on the
  re-consent path after an update whose source effectively changed.
  - **C1 (immediate)**: one real run of the full pipeline. Failure → stays
    `commissioning`, error surfaced for redrafting.
  - **C2 (user)**: the real result renders on /ni with "Looks right" /
    "Something's wrong". Wrong → back to draft with the user's note.
    `POST /api/ni/items/{id}/validate` refuses with 409 unless the item is
    currently `commissioning` (integrity: a verdict outside commissioning has
    no C1 output to endorse).
  - **C3 (automatic)**: the next engine-cadence run must pass and satisfy the
    contract captured at C1. **If `_c2_ok` is set but no contract was captured
    yet, the first clean run captures the contract and STAYS commissioning**
    (one more clean run required to promote to `live`). If a contract IS
    present when `_c2_ok` fires, the pre-bind check runs on every subsequent
    run; a violation is a run failure that keeps the item at `commissioning`.
- `live` — engine runs at cadence; every run contract-checked.
- `degraded` — latest run failed or violated contract; `last_good` renders dimmed.
  `latest` becomes an `ok=false` marker with an empty payload so the board's
  latest-if-ok-else-last_good fallback picks up last_good on its own.
- `failing` — ≥ 3 consecutive failures; effective interval doubles per failure
  beyond 3 (cap 24h).
- `broken` — 8 consecutive failures over ≥ 7 days **measured from
  `first_failure_at`** (the streak start), NOT `created_at`. A long-lived
  healthy item that only starts failing today therefore can't be classed
  broken from its birthday. `first_failure_at` is set on the FIRST failure of
  a streak, cleared on success, on `update_spec`, and on `commission()`. A
  permanent refusal (e.g. credential host mismatch) escalates straight to
  `broken` without waiting on the counter. Engine stops scheduling broken
  items; carrier-row alert fires; only user/agent action (edit → re-commission)
  leaves it.

Every failure records an `ni_runs` row with a host-free error class. `mark_checked`
on every attempt is the backoff (feeds law).

## 7. Data contract

Captured by the system at commissioning (C1 result, confirmed by C2):

```json
{"shape": {"price": "number", "rows": "list", "rows[].amount": "number"},
 "bounds": {"price": {"min": 0}}}
```
- `shape` — type fingerprint of every pipeline output the scene binds.
- `bounds` — optional plausibility ranges (system-suggested, user-editable).
Checked on every run after binding; a violation is a contract failure (run fails,
item degrades) even when everything parsed. Repair (later phases) targets contract
satisfaction, not mere parsing.

## 8. Engine pass

`ni.tick(app, pass_budget_seconds, breaker_open=None)` called from
`scheduler.tick` after the feeds pass, feeds contract verbatim: locked →
return; own cursor; due =
`enabled AND state NOT IN ('draft','paused','broken') AND (last_checked IS NULL OR
age > effective_interval)`, `NULLS FIRST`, oldest first; `_MAX_ITEMS_PER_PASS = 3`;
per-item try/except; wall-clock budget between items; `mark_checked` on every
attempt; host-free `last_status`. `model` sources additionally require
`gateway.local_available()` when the route is local, and respect the scheduler
breaker: `_auto_update_ni` passes the scheduler's `_breaker_open` to `tick`, and
while it returns True, model-source items are SKIPPED (they stay due — no
`mark_checked`, so the next tick with a healthy gateway picks them up).
`http_json` and `internal.schedule` items are unaffected by the breaker (they
don't touch the gateway).

Bound-payload guards on every write: `bind_scene` results are re-checked against
per-prop expectations (text/chip string ≤ 2000 chars; number/bar numeric+finite;
repeat items list) — a mismatch is `bind_type`. The JSON-serialized bound
payload is capped at 256 KiB before `write_snapshot`; exceeding = run failure
`payload_too_large`. Any exception on the whole finalize path (contract check,
bind, snapshot write) is wrapped as `NIError` so mark_checked + `ni_runs` row +
failure bump + `latest` ok=false snapshot fire on every failure path (a raw
`ValueError` used to skip the run row entirely).

## 9. Tools and tiers (v1)

| tool | tier | notes |
|---|---|---|
| `list_ni_items` | OBSERVE | plaintext state + titles |
| `read_ni_item` | OBSERVE | spec + health + latest bound payload (untrusted-data provenance line first, KB-tool precedent) |
| `create_ni_item` | REVIEWED, egress | full spec + preview payload; validates everything; lands in `commissioning` (approval == consent) unless a secret param is unfilled or `draft: true` is passed |
| `update_ni_item` | REVIEWED | partial; a source change (URL/headers/type/instruction OR the value of any param referenced by `source.url` via `{{param:}}`) sends the item to `commissioning` (re-consent). Optional `preview_payload` rewrites the preview snapshot alongside the spec — note the preview is stale until this is passed |
| `set_ni_item_enabled` | REVIEWED | pause/resume |
| `run_ni_item_now` | REVIEWED, egress | clears `last_checked`; refuses draft/paused/broken |
| `delete_ni_item` | IRREVERSIBLE | typed confirm |

`NI_WRITE_TOOLS = {create, update, set_enabled, run_now}` joins
`UNATTENDED_NEVER_AUTO`. None of the NI write tools are ever rememberable
(consent.py returns `None` for unlisted egress tools by default — leave them
unlisted on purpose).

The creation consent law: the ActionCard for `create_ni_item` must show the source
URL host + path unmissably and the user must have picked the source in
conversation before the card is parked. Approving the card is what moves
`draft → commissioning`.

## 10. HTTP surface (v1)

- `GET  /api/ni/board` — all items: plaintext state/freshness + decrypted bound
  payload (`preview` for drafts, else `latest` ok / `last_good`).
- `GET  /api/ni/items/{id}` — spec (secrets as names), health, run history.
- `POST /api/ni/items/{id}/validate` — C2 verdict `{ok: bool, note?: str}`.
  Refuses 409 unless the item is currently `commissioning`.
- `POST /api/ni/items/{id}/commission` — the Activate button on the drafted
  card: `draft` → `commissioning`. Refuses 409 unless state is `draft`, and
  409 with a clear detail when any secret param is still unfilled. Response
  `{"state": "commissioning"}`.
- `POST /api/ni/items/{id}/run` — manual refresh (engine path, same guards).
  Refuses 409 for `draft` and `broken`, and 409 when paused (`enabled=false`).
- `PATCH /api/ni/items/{id}` — position/display/enabled (UI edits).
- `DELETE /api/ni/items/{id}`.
- `PUT  /api/ni/items/{id}/credential` — enter a secret param value
  (desktop-local, body `{name, value, host}`); stores under `ni:<id>:<name>`,
  host-bound (host IDNA-lowercased at store time so it matches
  `urlparse().hostname`). Secrets never travel through chat or tool args.
- All handlers 423 → the standard locked contract; literal paths before `{id}`
  (schedule_routes ordering comment).

## 11. History (v2)

Optional spec field:

```json
{"history": {"track": {"priceLog": "price"}, "max_points": 100}}
```

- `track`: name → pipeline-output path (§4.1 grammar). ≤ 4 series; values must be
  numeric at run time (non-numeric = run failure `history_type`). Series names
  share the output namespace rules (no `item`, no collisions with pipeline
  outputs).
- After every SUCCESSFUL run (commissioning included), the engine appends
  `{"t": "<iso8601 UTC>", "v": <number>}` per tracked series and trims to
  `max_points` (≤ 500, default 100), storing all series in the sealed
  `ni_snapshots` slot `history` (AAD `ni_snapshot:<id>:history`). Failed runs
  append nothing.
- The binder exposes each series as `history.<name>` (a list of `{t, v}`) to
  `$bind`, `spark.points`, and `delta_prev`. `history.*` paths are read-only
  inputs — extract/transform outputs cannot be named `history`. A series' `name`
  also must not collide with any pipeline output (the tracked PATH points at an
  output; the NAME is a separate namespace entry).
- Tracked series with no points yet are **seeded as empty lists** for the binder,
  so a spark over `history.<name>` binds to `[]` on the first run (C1) and
  renders as an empty placeholder — an item charting its own history must be
  able to commission. `delta_prev` likewise never fails on an empty series (§4.2).
- Deleting the item deletes the slot (cascade); a source-change rewind KEEPS
  history (same subject, new plumbing) unless the user deletes the item.

## 12. Alerts (v2)

Optional spec field — the same closed condition grammar as §5 `when`, promoted:

```json
{"alerts": [{"name": "price-drop", "left": {"$bind": "delta.value"}, "op": "lt",
             "right": 0, "message": "{{title}}: price fell to {{price}}",
             "cooldown_minutes": 60}]}
```

- ≤ 5 rules; `name` = short slug (charset `[a-z0-9-]{1,40}`, unique per item);
  `left`/`op`/`right` exactly as §5 conditions; `message` is a template
  interpolated against the run's outputs plus `{{title}}` (plain text, ≤ 500
  chars bound); `cooldown_minutes` clamped to ≥ 5, default 60.
- Evaluated by the engine after every successful bind, on LIVE items only.
  **Edge-triggered**: a rule fires only on the false→true transition of its
  condition, and never again until it has been false at least once — and never
  within its cooldown. Per-rule state (`active: bool`, `last_fired: ts`) lives in
  the sealed `alert_state` snapshot slot; plaintext columns carry nothing about
  alert rules or their names.
- A fired alert (and every `broken` transition) posts to the **NI carrier row**:
  a third reserved schedule id (`_CARRIER_IDS` pattern, id `neural-interface`,
  title "Neural Interface") via `schedules.record_run`-style writes — riding the
  existing unseen badge, the chat `### Scheduled Item ###` injection, and the
  /info archive with zero new notification plumbing. Alert body = the bound
  `message`; broken body = "<title> is broken — open Neural Interface, or ask me
  to fix it." Host-free, as always.
- The evaluation is deterministic (pure function of run outputs + stored state);
  a rule whose binds cannot resolve marks the RUN failed (`alert_bind`), exactly
  like a scene bind failure — alerts are part of the contract surface, not
  best-effort.
