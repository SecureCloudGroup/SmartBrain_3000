<script lang="ts">
  // One tab strip for one concept — replaces the two divergent idioms (Settings'
  // bordered pills vs Schedules' underline subtabs). Pill is the default voice;
  // `variant="underline"` stays available where a quieter strip fits.
  let {
    tabs,
    active,
    onselect,
    variant = "pill",
  }: {
    tabs: { id: string; label: string }[];
    active: string;
    onselect: (id: string) => void;
    variant?: "pill" | "underline";
  } = $props();
</script>

<div class="tabs {variant}" role="tablist">
  {#each tabs as t (t.id)}
    <button
      role="tab"
      aria-selected={active === t.id}
      class:active={active === t.id}
      onclick={() => onselect(t.id)}
    >
      {t.label}
    </button>
  {/each}
</div>

<style>
  .tabs { display: flex; gap: var(--s-1); flex-wrap: wrap; margin: 0 0 var(--s-4); }
  .tabs button {
    background: transparent;
    color: var(--muted);
    border: 1px solid transparent;
    font-weight: 500;
    min-height: 36px;
    padding: 7px 14px;
  }
  .tabs button:hover { color: var(--text); filter: none; background: var(--elevated); }
  .pill button.active {
    background: var(--accent-tint);
    border-color: transparent;
    color: var(--accent);
    font-weight: 600;
  }
  .underline { gap: var(--s-3); border-bottom: 1px solid var(--border); }
  .underline button { border-radius: 0; padding: 7px 2px; margin-bottom: -1px; }
  .underline button.active {
    color: var(--accent);
    font-weight: 600;
    border-bottom: 2px solid var(--accent);
    background: transparent;
  }
</style>
