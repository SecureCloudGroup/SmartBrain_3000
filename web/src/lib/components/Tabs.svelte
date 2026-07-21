<script lang="ts">
  // One tab strip for one concept — replaces the two divergent idioms (Settings'
  // bordered pills vs Schedules' underline subtabs). Pill is the default voice;
  // `variant="underline"` stays available where a quieter strip fits.
  let {
    tabs,
    active,
    onselect = () => {},
    variant = "pill",
  }: {
    tabs: { id: string; label: string; href?: string }[]; // href: route tabs (Settings) render as links
    active: string;
    onselect?: (id: string) => void;
    variant?: "pill" | "underline";
  } = $props();
</script>

<div class="tabs {variant}" role="tablist">
  {#each tabs as t (t.id)}
    {#if t.href}
      <a role="tab" href={t.href} aria-selected={active === t.id} class:active={active === t.id}>{t.label}</a>
    {:else}
      <button
        role="tab"
        aria-selected={active === t.id}
        class:active={active === t.id}
        onclick={() => onselect(t.id)}
      >
        {t.label}
      </button>
    {/if}
  {/each}
</div>

<style>
  .tabs { display: flex; gap: var(--s-1); flex-wrap: wrap; margin: 0 0 var(--s-4); }
  .tabs button,
  .tabs a {
    display: inline-flex;
    align-items: center;
    background: transparent;
    color: var(--muted);
    border: 1px solid transparent;
    border-radius: var(--r-1);
    font-size: var(--f-label);
    font-weight: 500;
    min-height: 36px;
    padding: 7px 14px;
    text-decoration: none;
    cursor: pointer;
    transition: background var(--t-fast), color var(--t-fast);
  }
  .tabs button:hover,
  .tabs a:hover { color: var(--text); filter: none; background: var(--elevated); text-decoration: none; }
  .pill button.active,
  .pill a.active {
    background: var(--accent-tint);
    border-color: transparent;
    color: var(--accent);
    font-weight: 600;
  }
  .underline { gap: var(--s-3); border-bottom: 1px solid var(--border); }
  .underline button,
  .underline a { border-radius: 0; padding: 7px 2px; margin-bottom: -1px; }
  .underline button.active,
  .underline a.active {
    color: var(--accent);
    font-weight: 600;
    border-bottom: 2px solid var(--accent);
    background: transparent;
  }
  @media (max-width: 640px) {
    /* Long strips (Settings has seven tabs): one horizontally-scrollable row. */
    .tabs {
      flex-wrap: nowrap;
      overflow-x: auto;
      max-width: 100%;
      -webkit-overflow-scrolling: touch;
    }
    .tabs button,
    .tabs a {
      white-space: nowrap;
    }
  }
</style>
