<script lang="ts">
  // CardShell — the one card frame the NI board renders every item through (ni-format
  // §34, Phase 1a-4). Owns the head/body/footer regions and the few appearance rules
  // that are decided by `state` alone (CardShell never reads `item`; the page computes
  // `state`/`footerText` via $lib/ni/shell and passes everything else as snippets, so
  // every existing flow/commission branch keeps its own markup — see README-irpaint.md
  // "Board" for what the shell owns vs what a form's CLIR already carries).
  import type { Snippet } from "svelte";
  import { formBoxChrome, type CardState } from "$lib/ni/shell";

  let {
    title,
    state,
    hasForm,
    cols = 1,
    rows = 1,
    preview = false,
    footerText = "",
    reason = "",
    chips,
    children,
    below,
    actions,
  }: {
    title: string;
    state: CardState;
    // True when the item's payload is a bound `form` node (§34) — its CLIR paints its
    // own title and its own as-of/host/cadence footer, so CardShell adds neither.
    hasForm: boolean;
    // Grid footprint from $lib/ni/shell cardSpan (the painted CLIR's own span key, else
    // display.size): 1-2 columns, 1-3 rows. The row span and the min-height apply only
    // under the form chrome (see the style note below).
    cols?: 1 | 2;
    rows?: 1 | 2 | 3;
    preview?: boolean;
    footerText?: string;
    // The failing-state explanation line, shown above the dimmed body at full opacity.
    reason?: string;
    chips?: Snippet;
    children?: Snippet;
    below?: Snippet;
    actions?: Snippet;
  } = $props();

  // formBoxChrome ($lib/ni/shell) is the one gate for the tight "exactly the CLIR's
  // box" chrome (padding 16px, no head/foot rows, chips + actions as small overlays,
  // the plane filling the box) vs the legacy 24px chrome with a normal head/foot flow.
  // The page calls the SAME function to shape what it passes as `actions`, so the two
  // never disagree about which chrome a given card gets.
  const box = $derived(formBoxChrome(state, hasForm));
</script>

<div
  class="card ni-card"
  class:cols-2={cols >= 2}
  class:rows-2={rows === 2}
  class:rows-3={rows >= 3}
  class:form={box}
  class:preview
  data-state={state}
>
  <div class="ni-head">
    {#if hasForm}
      <!-- One title per card: the CLIR paints its own (keep the one-title rule). -->
      <span class="ni-title" aria-hidden="true"></span>
    {:else}
      <strong class="ni-title">{title}</strong>
    {/if}
    <span class="ni-chips">{@render chips?.()}</span>
  </div>

  <div class="ni-body" class:ni-body-dim={state === "failing"}>
    {@render children?.()}
  </div>
  {#if state === "failing" && reason}
    <p class="ni-fail-reason">{reason}</p>
  {/if}

  {@render below?.()}

  <div class="ni-foot">
    {#if !hasForm && footerText}
      <span class="muted ni-fresh">{footerText}</span>
    {/if}
    {@render actions?.()}
  </div>
</div>

<style>
  /* Card frame + head/body/foot regions, lifted from routes/ni/+page.svelte unchanged
     (Phase 1a-4). `.ni-card.cols-2`/`.rows-2`/`.rows-3` are the grid-span classes the
     board's fixed rhythm reads (+page.svelte's <style> — "Grid rhythm"), from the
     painted CLIR's own span key (cardSpan — the phone face may need a different
     footprint from the desktop face); the row span is additionally gated on `.form` (= formBoxChrome, NOT raw hasForm — see the
     script): a legacy card's content height is whatever NiScene renders, so forcing
     it into a fixed 2-row box could clip it, and `.form` is false for `verifying`/
     `designing`/`failing`/`broken` even on a bound form for the same reason (their
     body can carry more than a bare CLIR). A legacy (or excluded-state) card keeps
     `grid-row: auto`, so its own content height can run past one 176px row — accepted
     for sealed legacy scenes, not fixed here. `position: relative` anchors `.form`'s
     overlay head/body/foot below. */
  .ni-card {
    display: flex;
    flex-direction: column;
    gap: var(--s-3);
    margin: 0;
    position: relative;
  }
  .ni-card.cols-2 { grid-column: span 2; }
  .ni-card.rows-2.form { grid-row: span 2; }
  .ni-card.rows-3.form { grid-row: span 3; }
  .ni-card.preview {
    border-style: dashed;
    border-color: var(--border-strong);
  }
  /* `.form`: exactly the CLIR's own box (spans.py content_h = rows*176 + (rows-1)*gap
     - 32), 16px padding, no separate head/foot rows. `--ni-row-gap` is declared on
     `.ni-grid2` (12px/16px at the same 560px breakpoint IrPaint's pickClir uses) and
     falls back to 16px if this component is ever used outside that grid. */
  .ni-card.form {
    padding: var(--s-4);
    min-height: 176px;
  }
  .ni-card.form.rows-2 {
    min-height: calc(176px * 2 + var(--ni-row-gap, 16px));
  }
  .ni-card.form.rows-3 {
    min-height: calc(176px * 3 + 2 * var(--ni-row-gap, 16px));
  }
  .ni-card.form .ni-head {
    position: absolute;
    top: var(--s-2);
    right: var(--s-2);
    z-index: 2;
  }
  .ni-card.form .ni-body {
    position: absolute;
    inset: 0;
    overflow: hidden;
  }
  .ni-card.form .ni-foot {
    position: absolute;
    bottom: var(--s-2);
    right: var(--s-2);
    left: auto;
    width: auto;
    margin-top: 0;
    padding-top: 0;
    border-top: none;
    z-index: 2;
  }
  .ni-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-2);
    flex-wrap: wrap;
  }
  .ni-chips {
    display: inline-flex;
    align-items: center;
    gap: var(--s-1);
    flex-wrap: wrap;
  }
  .ni-title {
    font-size: var(--f-label);
    font-weight: 600;
    /* Wraps to at most 2 lines (today's single nowrap line clipped a long title); a
       3rd line would push into the body on a small card's fixed row height. */
    display: -webkit-box;
    line-clamp: 2;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    min-width: 0;
  }
  .ni-body { min-width: 0; }
  /* "failing" (and "degraded", the same bucket — $lib/ni/shell cardState): the last-good
     body dims, the reason line and the existing Fix affordance (rendered via `below`,
     outside this wrapper) stay at full opacity. Never combined with `.form` (see
     formBoxChrome), so this never dims an absolutely-positioned overlay body. */
  .ni-body-dim { opacity: 0.6; }
  .ni-fail-reason {
    margin: var(--s-1) 0 0;
    font-size: var(--f-label);
    color: var(--muted);
  }
  .ni-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-2);
    margin-top: auto; /* keeps footers aligned across cards of different body heights */
    padding-top: var(--s-2);
    border-top: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .ni-fresh { font-size: var(--f-meta); }

  /* Inline-style migration (Phase 1a-4): the card's flow/commission branches repeated
     the same `style="…"` attribute ~23 times across +page.svelte. The markup itself
     stays authored there (passed in through the snippets above — a flow pause or a C2
     screen is page logic, not shell chrome), so these 6 names are :global — the one
     deliberate exception to "this file styles only the markup it authors directly". */
  :global(.ni-note) { margin: 0; font-size: var(--f-label); }
  :global(.ni-note-head) { margin: 0; font-size: var(--f-label); font-weight: 600; }
  :global(.ni-note-sp) { margin: var(--s-1) 0 0; font-size: var(--f-label); }
  :global(.ni-note-tight) { margin: 2px 0 0; font-size: var(--f-label); }
  :global(.ni-note-gap) { margin: 0 0 var(--s-2); font-size: var(--f-label); }
  :global(.ni-actions-sp) { margin-top: var(--s-2); }
</style>
