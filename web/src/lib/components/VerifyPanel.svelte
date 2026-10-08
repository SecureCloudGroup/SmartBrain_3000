<script lang="ts">
  // VerifyPanel — the C2 "is this right?" surface for a commissioning item whose
  // payload is a bound `form` node (ni-format §34). Replaces the plain Looks-right /
  // Something's-wrong buttons for a FORM payload only; the caller keeps using those
  // directly for every other card (see +page.svelte). Precondition: the caller only
  // mounts this when `item.payload` is already known to be a bound form (its own
  // CLIR pick always exists then), so `pick` below is never null in practice.
  import { onDestroy, onMount } from "svelte";
  import IrPaint from "$lib/components/IrPaint.svelte";
  import { optionMaxWidth, verifyOptions, type VerifyOption } from "$lib/ni/verify";

  let {
    payload,
    viewerTz,
    busy = false,
    onUse,
    onWrong,
  }: {
    payload: unknown;
    viewerTz: string;
    busy?: boolean;
    onUse: (id: "pick" | "second") => void;
    onWrong: () => void;
  } = $props();

  const options = $derived(verifyOptions(payload));
  const pick = $derived(options.find((o) => o.id === "pick") ?? null);
  const second = $derived(options.find((o) => o.id === "second") ?? null);

  // --- Phone/desktop face toggle — local to this preview, never the automatic
  // (max-width: 560px) breakpoint IrPaint's caller otherwise uses. -------------------
  // Starts on the face the viewer's own device paints (the same 560px breakpoint as
  // the board), so a phone user reviews the phone card first; the toggle still shows
  // the other face.
  let face = $state<"desktop" | "phone">("desktop");
  onMount(() => {
    face = window.matchMedia?.("(max-width: 560px)")?.matches ? "phone" : "desktop";
  });
  function toggleFace(): void {
    console.assert(face === "desktop" || face === "phone", "toggleFace: known face before flip");
    const next = face === "desktop" ? "phone" : "desktop";
    console.assert(next !== face, "toggleFace: flips to the other face");
    face = next;
  }
  function faceOf(opt: VerifyOption): unknown {
    console.assert(opt.id === "pick" || opt.id === "second", "faceOf: known option id");
    console.assert(face === "desktop" || face === "phone", "faceOf: known face");
    return face === "phone" ? opt.clir.phone : opt.clir.desktop;
  }

  // --- Dark/light toggle ------------------------------------------------------------
  // The ideal (ni-format §34 plan) scopes `data-theme` to this panel's own wrapper, so
  // only the preview re-themes. app.css's light palette is declared on the selector
  // `:root[data-theme="light"]`, and web/scripts/gen-ni-tokens.mjs only ever fills the
  // three fixed marker regions inside that EXACT selector (:root / :root[data-theme=
  // light] / the prefers-color-scheme block) — it has no notion of an extra selector
  // to add a wrapper attribute to. Extending the generator for one preview toggle is
  // out of scope here (see the Phase 1a-4 report), so this falls back to the page-level
  // mechanism $lib/theme.svelte.ts already uses — document.documentElement's own
  // data-theme — applied only while the panel is mounted and restored on unmount, and
  // deliberately NOT written through the persisted theme store/localStorage, so a tap
  // here never changes the user's actual site-wide preference.
  function isLightNow(): boolean {
    console.assert(typeof document !== "undefined", "isLightNow: DOM available");
    const attr = document.documentElement.dataset.theme;
    if (attr === "light") return true;
    if (attr === "dark") return false;
    const mq = typeof window === "undefined" ? null : window.matchMedia?.("(prefers-color-scheme: light)");
    return mq?.matches === true;
  }
  let light = $state(false);
  let priorTheme: string | undefined;
  let themeTouched = false;
  onMount(() => { light = isLightNow(); });
  function toggleTheme(): void {
    console.assert(typeof light === "boolean", "toggleTheme: light is boolean");
    console.assert(typeof document !== "undefined", "toggleTheme: DOM available");
    if (!themeTouched) {
      priorTheme = document.documentElement.dataset.theme;
      themeTouched = true;
    }
    light = !light;
    document.documentElement.dataset.theme = light ? "light" : "dark";
  }
  onDestroy(() => {
    if (!themeTouched || typeof document === "undefined") return;
    if (priorTheme === undefined) document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = priorTheme;
  });
</script>

<div class="verify-panel">
  <p class="ni-note-head">This is live data — is it right?</p>
  <div class="verify-toolbar">
    <button type="button" class="ghost" disabled={busy} onclick={toggleFace}>
      {face === "desktop" ? "Desktop" : "Phone"}
    </button>
    <button type="button" class="ghost" disabled={busy} onclick={toggleTheme}>
      {light ? "Light" : "Dark"}
    </button>
  </div>

  <!-- Each option box is capped at the width the card will actually have on the board
       (optionMaxWidth from the face's own span key), so a 1x1 design previews as a
       1x1 card, not stretched across the panel. -->
  {#if pick}
    {@const pickFace = faceOf(pick)}
    <div class="verify-option" style:max-width={optionMaxWidth(pickFace)}>
      <IrPaint clir={pickFace} {viewerTz} />
    </div>
  {/if}
  {#if second}
    {@const secondFace = faceOf(second)}
    <div class="verify-option verify-second" style:max-width={optionMaxWidth(secondFace)}>
      <p class="muted verify-why">{second.why}</p>
      <IrPaint clir={secondFace} {viewerTz} />
    </div>
  {/if}

  {#if pick}
    <div class="ni-actions">
      <button class="secondary" disabled={busy} onclick={() => onUse("pick")}>
        {busy ? "Checking…" : "Use this"}
      </button>
      {#if second}
        <button class="ghost" disabled={busy} onclick={() => onUse("second")}>Use the other design</button>
      {/if}
      <button class="ghost" disabled={busy} onclick={onWrong}>The data is wrong</button>
    </div>
  {/if}
</div>

<style>
  .verify-panel {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
    padding: var(--s-3);
    background: var(--accent-tint);
    border-radius: var(--r-1);
  }
  .verify-toolbar {
    display: inline-flex;
    gap: var(--s-1);
  }
  .verify-option {
    border: 1px solid var(--border);
    border-radius: var(--r-2);
    background: var(--ni-panel);
    padding: var(--s-2);
  }
  .verify-why {
    margin: 0 0 var(--s-2);
    font-size: var(--f-label);
  }
  /* Same shape as +page.svelte's `.ni-actions` (that rule is scoped to the page and
     does not reach this component's own markup); duplicated here rather than shared,
     since it is 3 declarations. */
  .ni-actions {
    display: inline-flex;
    gap: var(--s-1);
    flex-wrap: wrap;
  }
</style>
