<script lang="ts">
  // TriedList — "What I tried": the sources a build dropped and why, in the backend's words, verbatim
  // (2026-10-31: three dropped rows read as "no source answers this" while the reasons lived only in the sealed
  // record). Shown on the key pause and on the declined card, so the user sees what happened before deciding.
  // Renders nothing when nothing was tried; never more than six rows (the backend's own cap).
  import type { FlowTried } from "$lib/ni/types.gen";

  let { tried = [] }: { tried?: FlowTried[] } = $props();
  const rows = $derived(
    (tried ?? []).filter((t) => !!t && typeof t.host === "string" && t.host.length > 0).slice(0, 6),
  );
</script>

{#if rows.length > 0}
  <div class="tried">
    <p class="muted tried-head">What I tried</p>
    <ul class="tried-list">
      {#each rows as row, i (`${i}-${row.host}`)}
        <li><span class="tried-source">{row.provider || row.host}</span> <span class="muted">{row.why}</span></li>
      {/each}
    </ul>
  </div>
{/if}

<style>
  .tried {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .tried-head,
  .tried-list {
    margin: 0;
    font-size: var(--f-label);
  }
  .tried-list {
    list-style: none;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .tried-list li {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .tried-source {
    font-weight: 600;
  }
</style>
