<script lang="ts">
  // The approval surface — a proposed action rendered as a deliberate pause, not an alarm.
  // Neutral panel + accent edge for reviewed actions; the danger edge and badge are reserved
  // for irreversible scopes (which also default-deny: Approve never steals Enter here).
  import type { Snippet } from "svelte";
  import Icon from "./Icon.svelte";
  import type { IconName } from "$lib/icons";

  let {
    icon = "pencil" as IconName,
    title,
    tier = "reviewed",
    scope = "",
    actions,
  }: {
    icon?: IconName;
    title: string;
    tier?: "reviewed" | "irreversible";
    scope?: string; // pre-formatted "key: value" lines (Activity's fmtArgs output)
    actions?: Snippet;
  } = $props();
</script>

<div class="actioncard" class:irreversible={tier === "irreversible"}>
  <div class="head">
    <Icon name={tier === "irreversible" ? "warn" : icon} size={16} />
    {title}
  </div>
  {#if scope}<pre class="scope">{scope}</pre>{/if}
  <div class="foot">
    {#if tier === "irreversible"}
      <span class="chip warn"><Icon name="warn" size={12} /> Irreversible</span>
    {:else}
      <span class="chip"><Icon name="refresh" size={12} /> Reversible</span>
    {/if}
    <span class="gap"></span>
    {@render actions?.()}
  </div>
</div>

<style>
  .actioncard {
    border: 1px solid var(--border-strong);
    border-left: 3px solid var(--accent);
    border-radius: var(--r-2);
    background: var(--panel);
    padding: var(--s-4);
    display: flex;
    flex-direction: column;
    gap: var(--s-3);
    margin: var(--s-3) 0;
  }
  .actioncard.irreversible { border-left-color: var(--danger); }
  .head {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 600;
    font-size: var(--f-label);
  }
  .head :global(svg) { color: var(--accent); }
  .irreversible .head :global(svg) { color: var(--danger); }
  .scope {
    margin: 0;
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--f-meta);
    color: var(--muted);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .foot { display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap; }
  .gap { flex: 1; }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: var(--f-meta);
    font-weight: 500;
    padding: 2px 10px;
    border-radius: var(--r-full);
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .chip.warn {
    color: var(--warn);
    border-color: transparent;
    background: color-mix(in srgb, var(--warn) 10%, transparent);
  }
</style>
