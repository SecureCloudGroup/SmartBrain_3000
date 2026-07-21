<script lang="ts">
  // First-run/empty surfaces as onboarding: icon, value statement, verb-first action —
  // never an apologetic bare "No X yet" line.
  import type { Snippet } from "svelte";
  import Icon from "./Icon.svelte";
  import type { IconName } from "$lib/icons";

  let {
    icon,
    title,
    body = "",
    children,
  }: { icon: IconName; title: string; body?: string; children?: Snippet } = $props();
</script>

<div class="empty">
  <div class="eic"><Icon name={icon} size={24} /></div>
  <!-- h2, not h3: these render directly under the page h1 (axe heading-order) -->
  <h2>{title}</h2>
  {#if body}<p>{body}</p>{/if}
  {#if children}<div class="cta">{@render children()}</div>{/if}
</div>

<style>
  .empty {
    text-align: center;
    padding: var(--s-7) var(--s-5);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s-3);
  }
  .eic {
    width: 52px;
    height: 52px;
    border-radius: var(--r-3);
    background: var(--accent-tint);
    color: var(--accent);
    display: grid;
    place-items: center;
  }
  h2 { margin: 0; font-size: var(--f-section); font-weight: 600; }
  p { margin: 0; color: var(--muted); max-width: 26rem; }
  .cta { display: flex; gap: var(--s-2); flex-wrap: wrap; justify-content: center; margin-top: var(--s-1); }
</style>
