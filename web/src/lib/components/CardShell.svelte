<script lang="ts">
  // CardShell — the one card frame the NI board renders every item through (ni-format
  // §34, Phase 1a-4). Owns the head/body/footer regions and the few appearance rules
  // that are decided by `state` alone (CardShell never reads `item`; the page computes
  // `state`/`footerText` via $lib/ni/shell and passes everything else as snippets, so
  // every existing flow/commission branch keeps its own markup — see README-irpaint.md
  // "Board" for what the shell owns vs what a form's CLIR already carries).
  import type { Snippet } from "svelte";
  import Chip from "$lib/components/Chip.svelte";
  import type { FitText } from "$lib/ni/fit";
  import { formBoxChrome, type CardState } from "$lib/ni/shell";

  let {
    title,
    state,
    hasForm,
    cols = 1,
    rows = 1,
    preview = false,
    footerText = "",
    interpretation = "",
    reason = "",
    rendered = false,
    notice = "",
    fit = null,
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
    // The card's interpretation line (ni-format §36, code-authored — $lib/ni/shell
    // interpretationOf): when set it leads the non-form footer; "" keeps today's footer.
    interpretation?: string;
    // The failing-state explanation line, shown above the dimmed body at full opacity.
    reason?: string;
    // Round 20 (B3b, ni-format §35): the card reads its page in SmartBrain's page browser — a
    // small muted "page browser" chip follows the page's own chips.
    rendered?: boolean;
    // A code-authored state line that is not a failure ($lib/ni/browser renderNotice: the page
    // browser is unavailable on this machine). When set it takes the reason line's place, in any
    // state, and keeps the legacy chrome (formBoxChrome).
    notice?: string;
    // The fit advisory (ruling 2026-10-12; $lib/ni/fit fitText; fix round 2026-10-13 after a
    // rendered check found it covering painted content on tight-chrome cards): a warn chip, its
    // title carrying the detail ("<chip>. Missing: a, b"). Legacy chrome: in the head's chip row,
    // the detail also as the footer's second line on a phone. Form (tight) chrome: in the foot
    // strip beside the ⋯ menu instead — never in the overlaid head row — the strip's one line
    // folding in the detail on a phone, in place of a second line. null = none.
    fit?: FitText | null;
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
  const box = $derived(formBoxChrome(state, hasForm, notice));
  // The fit chip's title is the whole advisory, so a chip cut short by a narrow card still reads in full.
  const fitTitle = $derived(fit ? (fit.detail ? `${fit.chip}. ${fit.detail}` : fit.chip) : "");
  // The foot strip's one phone-width line (form chrome only): the chip words, the detail folded
  // in when there is one — there is no separate second line to carry it there (see fitTitle).
  const fitStripPhone = $derived(fit ? (fit.detail ? `${fit.chip} · ${fit.detail}` : fit.chip) : "");
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
  <div class="ni-head" class:has-fit={!!fit && !box}>
    {#if hasForm}
      <!-- One title per card: the CLIR paints its own (keep the one-title rule). -->
      <span class="ni-title" aria-hidden="true"></span>
    {:else}
      <strong class="ni-title">{title}</strong>
    {/if}
    <span class="ni-chips">
      {#if fit && !box}
        <!-- Legacy chrome only — a tight-chrome card wears it in the foot strip instead (below):
             rendered checked (2026-10-13) and found the overlaid chip covering a 1x1 card's hero
             or first row; the foot's own 32px reserved strip never has painted content to cover. -->
        <span class="ni-fit">
          <Chip kind="warn" title={fitTitle}><span class="ni-fit-text">{fit.chip}</span></Chip>
        </span>
      {/if}
      {@render chips?.()}
      {#if rendered}
        <Chip title="This card reads its page in SmartBrain's own local page browser">page browser</Chip>
      {/if}
    </span>
  </div>

  <div class="ni-body" class:ni-body-dim={state === "failing"}>
    {@render children?.()}
  </div>
  {#if notice}
    <p class="ni-state-note">{notice}</p>
  {:else if state === "failing" && reason}
    <p class="ni-fail-reason">{reason}</p>
  {/if}

  {@render below?.()}

  <div class="ni-foot" class:has-fit-line={!!fit?.detail && !box} class:has-fit-strip={!!fit && box}>
    {#if !hasForm && footerText}
      {#if interpretation}
        <!-- Plain text, never {@html}: one line cut with an ellipsis, the whole line in
             the title, then the usual as-of · host · cadence segments. -->
        <span class="muted ni-fresh ni-foot-text">
          <span class="ni-interp" title={interpretation}>{interpretation}</span>
          <span>· {footerText}</span>
        </span>
      {:else}
        <span class="muted ni-fresh">{footerText}</span>
      {/if}
    {/if}
    {#if fit && box}
      <!-- Tight chrome: the CLIR's own reserved blank strip, left of the ⋯ — never over the
           CLIR plane. The full sentence is the chip's title; a phone has no hover, so there the
           one line folds the detail in instead (fitStripPhone) rather than a second line. -->
      <span class="ni-fit-strip">
        <Chip kind="warn" title={fitTitle}>
          <span class="ni-fit-strip-text ni-fit-strip-short">{fit.chip}</span>
          <span class="ni-fit-strip-text ni-fit-strip-phone">{fitStripPhone}</span>
        </Chip>
      </span>
    {/if}
    {@render actions?.()}
    {#if fit?.detail && !box}
      <!-- Legacy chrome, a phone has no hover: there the detail is the footer's second line. -->
      <span class="ni-fit-line">{fit.detail}</span>
    {/if}
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
    /* A grid item's min-width defaults to auto (its content's min-content size) — paired with
       .ni-grid2's minmax(0, 1fr) tracks (+page.svelte "Grid rhythm"), this is what actually lets
       a card shrink to its track's share instead of forcing the whole row (every card in it)
       wider than the viewport, caught in the same rendered phone check. */
    min-width: 0;
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
  /* Carrying the fit strip: both left AND right set (not width:auto) — the strip + ⋯ row can
     never grow past the card's own content box, at any width (the rendered check caught the
     overlaid head row doing exactly that; the foot must not repeat it). */
  .ni-card.form .ni-foot.has-fit-strip {
    left: var(--s-2);
    flex-wrap: nowrap;
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
  .ni-fail-reason,
  .ni-state-note {
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
  /* The interpretation segment keeps to one line (ellipsis); when it and the as-of ·
     host · cadence segments do not fit side by side, those wrap onto their own line
     with their full text — the same rule on a phone-width card. */
  .ni-foot-text {
    display: flex;
    flex-wrap: wrap;
    column-gap: var(--s-1);
    min-width: 0;
    max-width: 100%;
  }
  .ni-interp {
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* The fit chip (ruling 2026-10-12): Chip's warn kind on an opaque --panel backing — over a
     form card's CLIR the warn tint alone is translucent and the CLIR's title would show through.
     Its words ellipsize, never wrap (the title carries the whole advisory), and it gives up its
     width before any other chip does: its shrink weight dwarfs theirs. */
  .ni-fit {
    display: inline-flex;
    min-width: 0;
    flex-shrink: 100;
    border-radius: var(--r-full);
    background: var(--panel);
  }
  .ni-fit-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ni-head.has-fit .ni-chips { min-width: 0; }
  /* The fit strip (form/tight chrome only — fix round 2026-10-13: a rendered check found the
     overlaid head-row chip covering a 1x1 card's painted hero/first row and a 3x1 card's plot).
     Sits beside the ⋯ in the foot's own reserved 32px strip, which the CLIR never paints into —
     never over the CLIR plane. An opaque --panel backing for the same reason as .ni-fit (Chip's
     warn tint alone is translucent); its words ellipsize, never wrap. */
  .ni-fit-strip {
    display: inline-flex;
    min-width: 0;
    border-radius: var(--r-full);
    background: var(--panel);
  }
  .ni-fit-strip-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Only one of the strip's two lines is ever in the flex row (the other is display:none), so
     there is never a second, independent ellipsis fighting the first for the same space. */
  .ni-fit-strip-phone { display: none; }
  /* The fit detail line (legacy chrome only now) shows on a phone only (no hover there); above
     the 560px phone-face breakpoint the chip's title carries it. */
  .ni-fit-line { display: none; }
  @media (max-width: 560px) {
    .ni-fit-line {
      display: block;
      flex-basis: 100%;
      font-size: var(--f-meta);
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    /* No hover on a phone: the strip's one line folds the detail in instead of naming the
       chip alone (fitStripPhone) — there is no second line to carry it in the 32px strip. */
    .ni-fit-strip-short { display: none; }
    .ni-fit-strip-phone { display: block; }
  }

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
