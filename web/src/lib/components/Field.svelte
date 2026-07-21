<script lang="ts">
  // Label + control + hint/error in one rhythm, so forms stop hand-laying the same
  // flex column with ad-hoc spacing. The control itself comes in as a snippet.
  import type { Snippet } from "svelte";

  let {
    label,
    hint = "",
    error = "",
    children,
  }: { label: string; hint?: string; error?: string; children?: Snippet } = $props();
</script>

<div class="field">
  <!-- The control renders inside the label (implicit association — a snippet can't carry a for/id pair). -->
  <label>
    <span class="ltext">{label}</span>
    {@render children?.()}
  </label>
  {#if error}
    <p class="ferr">{error}</p>
  {:else if hint}
    <p class="fhint">{hint}</p>
  {/if}
</div>

<style>
  .field { margin: 0 0 var(--s-3); }
  .field label { margin: 0; }
  .ltext { display: block; margin: 0 0 var(--s-1); font-size: var(--f-label); color: var(--muted); }
  .fhint { margin: var(--s-1) 0 0; font-size: var(--f-meta); color: var(--faint); }
  .ferr { margin: var(--s-1) 0 0; font-size: var(--f-meta); color: var(--danger); }
</style>
