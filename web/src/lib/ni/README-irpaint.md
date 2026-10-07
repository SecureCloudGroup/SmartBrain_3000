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
