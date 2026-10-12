<script lang="ts">
  // RenderSitesPanel — the Activate panel of a draft whose page also loads other sites (Round
  // 20, B3b; ni-format §35, ruling R6). The page browser's one creation visit found the sites
  // the page depends on; the card waits as a draft that names them, and Activate is the user's
  // OK for every update to load exactly these sites. Presentational like VerifyPanel: the page
  // owns POST /activate and hands back `busy` and the server's refusal sentence (`error`). The
  // sites are registrable domains sealed by code, shown as plain text, one per line.
  import { alsoLoadsLine } from "$lib/ni/browser";

  let {
    sites,
    busy = false,
    error = "",
    onActivate,
  }: {
    // The other sites, never the page's own ($lib/ni/browser renderDraftSites: unique, ≤ 12).
    sites: string[];
    busy?: boolean;
    // The server's closed refusal sentence (a 409 / 403 detail, verbatim), or "".
    error?: string;
    onActivate: () => void;
  } = $props();
</script>

<div class="sites-panel">
  <p class="ni-note-head">{alsoLoadsLine(sites.length)}:</p>
  <ul class="sites-list">
    {#each sites as site (site)}
      <li>{site}</li>
    {/each}
  </ul>
  <p class="muted ni-note">Activate is your OK for every update of this card to load exactly these sites.</p>
  <div class="sites-actions">
    <button disabled={busy} onclick={onActivate}>{busy ? "Activating…" : "Activate"}</button>
  </div>
  {#if error}
    <p class="error ni-note">{error}</p>
  {/if}
</div>

<style>
  /* The same "your decision" surface as the page's source pick and C2 panels (+page.svelte
     .ni-commission): the accent tint under --text and --muted, both gated in
     theme-contrast.test.ts. */
  .sites-panel {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
    padding: var(--s-3);
    background: var(--accent-tint);
    border-radius: var(--r-1);
  }
  .sites-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--f-label);
    overflow-wrap: anywhere; /* a long domain wraps inside the card, never past it */
  }
  .sites-actions {
    display: inline-flex;
    gap: var(--s-1);
    flex-wrap: wrap;
  }
</style>
