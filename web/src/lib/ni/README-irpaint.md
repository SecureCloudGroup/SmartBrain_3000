# IrPaint — the client painter for form cards

`IrPaint.svelte` paints a frozen CLIR, the layout the server's `ni_forms` engine
(Python) produced for one card at one span: form, variant, bound record, prim
geometry, pre-broken text runs, ladder choices, lint. By the time a CLIR reaches
the client it is a measured, hash-stable artifact. The client turns it into pixels
and keeps the live bindings ticking.

## What the client does

- `validateClir(payload)` (`clir.ts`) refuses what `types.py::check_clir` would
  refuse: closed prim kinds, px below the type floor, unknown tokens, bad anchors,
  roles outside `TEXT_ROLES`, formats outside `TIME_FMTS`, icons outside `ICONS`,
  oversized prim or point counts. A failure throws a typed `ClirError`; the
  component catches it and shows the app's muted "unrenderable" placeholder.
- `renderModel(clir, {width, now, viewerTz})` resolves every live binding at
  `now` (`applyLive`, a port of `paint/live.py::apply_live`) and emits positioned
  paint ops: text runs with role, px, weight, colour token, alignment and
  direction, plus the shape prims. Summary, reading order and hitmap pass through.
- `IrPaint.svelte` paints the shapes as one inline SVG layer in CLIR order and the
  text and time runs as absolutely positioned DOM text above it. Colours are
  `var(--ni-<token>)`; the painter never emits a colour literal. A 15-second tick
  re-applies the live bindings (variant switches, past-dim boundaries, countdowns).
- Width: the wrapper measures the card's content box. The plane is painted at
  that width (the CLIR's anchors `l/r/c/f` resolve against it, as in the Python
  painter's `card_html(width=…)`); a cell narrower than the bucket floor paints
  at the floor and scales down, so prims never overlap.
- Text boxes sit where the Python painter puts them: top = baseline − half-leading
  − ascent, with Inter's hhea metrics (`INTER_ASCENT`, `INTER_DESCENT`).
- Overflow sentinel (`overflow.ts`): after `document.fonts.ready`, any text whose
  `scrollWidth` exceeds its laid-out `max_w` gets `data-ellipsis` and bumps a
  module counter. The CSS ellipsis already keeps the layout intact; the counter is
  the measurement the plan asks for (it must stay at zero on the gallery).
- `role="img"` + `aria-label` = the code-written summary; the text is still real,
  selectable DOM.

## What the client never does

- Never lints, never lays out, never re-wraps: lines are pre-broken, prims are
  pre-positioned, `max_w` was decided at the bucket floor.
- Never computes or verifies the layout hash.
- Never emits `{@html}`; every run is a plain `{line}` interpolation.
- Never loads image prims yet: no shipped form emits one, so the box paints as a
  placeholder until the image form lands with its loader. Basemap prims paint the
  land box until the coastline rings arrive: `basemap.ts` fetches
  `/ni/world110m.json` once per page (a failed fetch keeps the box and the next
  basemap prim retries), and `mapPaths` projects the rings into the prim's box
  exactly as the basemap branch of `paint/vector.py` does (same mapping, skip rule
  and number format; `basemap.test.ts` pins it against Python-computed points).

## Live-binding parity rule

`clir.ts` is a direct port of `app/smartbrain_3000/ni_forms/paint/live.py`:
`formatTime` ↔ `format_time`, `timePrimText` ↔ `time_prim_text`, `countdownText`,
`countUpText`, `ageText`, `numberText`, `activeVariant`, `applyLive`,
`lineSegments` ↔ `line_segments` (the client approximates each label box from
`max_w` and the role's line height, since it has no glyph widths; the split is the
same). Any change to `paint/live.py` must land here in the same PR and vice versa.
`clir.test.ts` pins the ported cases against the three fixtures under
`app/tests/fixtures/ni_forms/clir/`.

## Tokens and icons

`tokens.ts`, `icons.generated.ts`, the three `ni-tokens:*` regions in
`src/app.css` (`:root`, `:root[data-theme="light"]`, the system-preference
block) and `static/ni/world110m.json` (the land outline, served same-origin for
`basemap.ts`) are written by one generator from the Python-owned
`app/smartbrain_3000/ni_forms/tokens.json`, `assets/icons.json` and
`assets/world110m.json`:

```
node web/scripts/gen-ni-tokens.mjs
```

`theme-vars.test.ts` checks that every generated token is defined in `app.css`
for both themes and that the regions match the generator's output.

## What the engine emits

A bound form node on the board is

```jsonc
{
  "type": "form",
  "form": "stat",
  "clir": { "desktop": { /* CLIR v1 */ }, "phone": { /* CLIR v1 */ } },
  "summary": "NVDA 223.86, down 1.65",
  "hash": "…",
  "lint": { "red": 0, "amber": 0 }
}
```

`+page.svelte` paints `clir.phone` when `(max-width: 560px)` matches, otherwise
`clir.desktop`, the same breakpoint that collapses span-2 cards. Any other payload
goes to `NiScene` unchanged, so sealed v1 scenes keep rendering.

## Board (Phase 1a-4)

`CardShell.svelte` is the one card frame `routes/ni/+page.svelte` renders every board
item through; `VerifyPanel.svelte` is the C2 surface for a commissioning item whose
payload is a bound form. Both stay thin — the appearance decisions they need live in
`$lib/ni/shell.ts` (`cardState`, `footerText`, `cardSpan`, `bodyShowsPanel`,
`formBoxChrome`) and `$lib/ni/verify.ts` (`verifyOptions`), unit-tested without
mounting Svelte.

**What CardShell owns:** the head (title at 14/600, wrapping to 2 lines — hidden for a
bound form, since the CLIR paints its own, per the one-title rule — chips stay right
of it), the body region (dimmed to 60% opacity plus a reason line when `cardState`
reads `"failing"`/`"degraded"`), and, **for a non-form card only**, the mandatory
footer line `footerText` builds: `"as of <local time> [· <host>] · every <N>m"`; the
host segment joins the line only when the board row actually exposes one
(`awaiting_yes.host` today — a `NiBoardItem` otherwise carries no source URL, per
`ni_routes.py _board_row`), so most non-form footers read without one rather than
inventing it. The `.cols-2`/`.rows-2`/`.rows-3` span classes (from the painted CLIR's own span key, else `display.size`, via
`cardSpan`) and the dashed `.preview` border also live here. Everything else — the
flow stage lines, the commissioning affordances, the action-row buttons — is still the
page's own markup, passed in as snippets (`chips`, `below`, `actions`) plus the default
body `children`, so no existing branch was rewritten, only relocated.

**What a form's CLIR already owns:** its own title text, and its own
as-of/host/cadence footer line (`ni_forms/shell.py`), laid out server-side at the
card's measured width. CardShell adds neither for a `hasForm` card — a second title or
a second footer would break the one-title / one-footer rule the shell exists to keep.
For the same reason the card BODY never paints the CLIR a second time once VerifyPanel
is already showing it: `bodyShowsPanel(item, hasForm)` is true exactly when the page is
about to mount VerifyPanel for this item (commissioning, a non-preview form payload,
not yet confirmed, not the separate held-source screen below), and the body's
`{:else if item.payload}` branch renders nothing in that case — VerifyPanel, in
`below`, is the only painted copy of the pick on that one screen.

**`formBoxChrome(state, hasForm)`** is the second, narrower gate: true only for
`fresh`/`stale`/`paused` on a bound form — the three states whose body is *always* a
bare CLIR with an empty `below` (no reason line, no Fix, no needs-credentials row).
Only then does CardShell shrink to exactly the CLIR's own box: `spans.py` sizes a
design to `rows * 176 + (rows-1) * gap - 32` px of *content*, i.e. the whole card is
the row span with 16px padding and nothing else — so CardShell drops its normal 24px
padding and head/foot rows for these cards, absolutely positions the (usually empty)
chip row top-right and the History/Run now/More actions bottom-right (each over the
CLIR, which is why a healthy card showing no chips is the common, cleanest case), and
lets the CLIR's own plane fill the box (`overflow: hidden`). `verifying` (VerifyPanel —
a review surface taller than any design) and `designing` (the Preview tag, Activate,
flow/consent copy) are excluded by `formBoxChrome` itself; `failing`/`broken`/a
commissioning run with failures are excluded too, because `below` can hold the reason
line and the Fix affordance there, which the tight chrome has no room for — those keep
the legacy 24px chrome even on a bound form, at the cost of not being exactly 176px/
368px tall. The page calls the same `formBoxChrome` to decide whether `actions` renders
History/Run now inline (legacy chrome) or folds them into the `⋯` menu (tight chrome,
where a second inline button pair would not fit a 176px-wide small card).

**VerifyPanel** shows the sealed pick's CLIR (and, when the payload carries one
`alternatives` entry — a parallel engine change, absent today — the runner-up, with a
code-written one-line why built from its own `summary`) through this same `IrPaint`,
behind a local phone/desktop face toggle and a dark/light toggle. The toggle is a
page-level `data-theme` override (restored on unmount) rather than a preview-scoped
one: the light palette lives on the fixed selector `:root[data-theme="light"]` in
`app.css`, and `gen-ni-tokens.mjs` only ever fills that selector's own marker region —
it has no notion of adding a second selector (e.g. a `.verify-preview` wrapper) to it.

**Grid rhythm:** `.ni-grid2` is a fixed rhythm — 2 columns on a phone, 3 from 780px, 4
from 1040px, `grid-auto-rows: minmax(176px, auto)`, `grid-auto-flow: row` (never
`dense`, so DOM, focus and visual order always match the user's order). `minmax`, not a
bare `176px`: a bare length forces every implicit row to exactly that height, so a
sibling taller than its span (a legacy card, a `verifying` card holding VerifyPanel)
would overflow into the row below instead of growing its own; `minmax(176px, auto)`
keeps a `formBoxChrome` card's span exact (nothing in it ever asks for more than its
box) while letting a taller neighbour grow the row it actually occupies. `.cols-2`/
`.rows-2`/`.rows-3` span columns/rows (the rows only when `formBoxChrome` is also true, see above) —
a legacy card, and a `verifying` card even on a bound form, keep `grid-row: auto`
content-sized height; accepted for sealed legacy scenes and for the review screen, not
a bug this phase fixes. `--ni-row-gap` (12px/16px) mirrors `spans.py`'s `GAP` at the
same 560px breakpoint `IrPaint`'s `pickClir` already uses, not the grid's own 780/1040
column breakpoints — a card's gap tracks which CLIR face is painting, not how many
columns the board currently shows.

**Column widths, verified against the real page chrome** (the `/ni` route renders
inside `<main class="wrap-wide">`, capped at `min(64rem, 100%)` with 24px side padding,
beside a 232px sidebar from 768px up — `app.css` `.wrap-wide`, `.shell.with-side`):
a single grid-column cell's width, computed from `(min(1024, viewport − 232) − 48 −
(cols−1) × 16) / cols` for every `cols` tier, works out to **156px at 780px** growing to
**242px at 1039px** (3 columns, just before the 4-column tier), then **178px at 1040px**
growing to a **flat 232px from 1256px on** (4 columns, once `wrap-wide`'s own 1024px
cap binds). All four numbers sit close to, but at the low end of each tier briefly
below, the painter's desktop-S bucket (188–224px content / 220–256px cell per
`spans.py`). This phase does not move the 780/1040 breakpoints to chase that: `IrPaint`
already paints any cell narrower than its bucket floor at the floor and scales down
(`README-irpaint.md` above — "never overlap"), and any cell wider than the ceiling
simply gives the design more room, so neither direction clips or overlaps — a sub-floor
cell is reduced fidelity, not breakage. The 2-column (phone) tier, by contrast, lands
inside `spans.py`'s own phone buckets: **130px at 320px** and **250px at 560px** (half,
gap 12px, bucket 128–160 — slightly over the ceiling at the high end), **249px at
561px** growing to **352px at 767px** (gap steps to 16px here, since `IrPaint`'s face
switches to desktop above 560px while the grid is still 2 columns below 780px — a
"desktop-face, 2-column-grid" gap this phase inherits, not introduces).
