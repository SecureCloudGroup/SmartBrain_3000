<script lang="ts">
  // Small labeled pill — citations, identity/status badges, quiet metadata. The one
  // shape behind what pages previously hand-rolled as .cite / .badge / .fp.
  import type { Snippet } from "svelte";
  import Icon from "./Icon.svelte";
  import type { IconName } from "$lib/icons";

  let {
    icon = "" as IconName | "",
    kind = "" as "" | "accent" | "ok" | "warn" | "danger",
    mono = false,
    onclick = undefined as (() => void) | undefined,
    title = "",
    children,
  }: {
    icon?: IconName | "";
    kind?: "" | "accent" | "ok" | "warn" | "danger";
    mono?: boolean; // fingerprints and other verbatim identifiers
    onclick?: (() => void) | undefined;
    title?: string;
    children?: Snippet;
  } = $props();
</script>

{#if onclick}
  <button class="chip {kind}" class:mono class:clicky={true} {title} {onclick}>
    {#if icon}<Icon name={icon} size={13} />{/if}
    {@render children?.()}
  </button>
{:else}
  <span class="chip {kind}" class:mono {title}>
    {#if icon}<Icon name={icon} size={13} />{/if}
    {@render children?.()}
  </span>
{/if}

<style>
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: var(--f-meta);
    font-weight: 500;
    line-height: 1.6;
    padding: 2px 10px;
    border-radius: var(--r-full);
    border: 1px solid var(--border);
    color: var(--muted);
    background: var(--panel);
    min-height: 0;
    /* A chip never widens its container: an over-long label (a full source URL in a
       search-hit citation) clips inside the pill instead of pushing the page wide.
       min-width:0 also lets chips compress inside flex rows (document tag rows). */
    max-width: 100%;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
  }
  button.chip.clicky { cursor: pointer; transition: border-color var(--t-fast), color var(--t-fast); }
  button.chip.clicky:hover { border-color: var(--accent); color: var(--text); background: var(--panel); filter: none; }
  .mono { font-family: var(--font-mono, ui-monospace, monospace); letter-spacing: 0.02em; }
  .accent { color: var(--accent); border-color: transparent; background: var(--accent-tint); }
  .ok { color: var(--ok); border-color: transparent; background: color-mix(in srgb, var(--ok) 10%, transparent); }
  .warn { color: var(--warn); border-color: transparent; background: color-mix(in srgb, var(--warn) 10%, transparent); }
  .danger { color: var(--danger); border-color: transparent; background: color-mix(in srgb, var(--danger) 10%, transparent); }
</style>
