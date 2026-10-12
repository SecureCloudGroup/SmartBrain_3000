<script lang="ts">
  // FeedOffer — "This page gets its data from a feed" on a page card's C2 panel (Round 20, B3b;
  // ni-format §35, ruling R12). The page browser's creation visit saw the page fetch its own
  // data; each candidate shows its EXACT address — plain text on one line, cut with an ellipsis,
  // the whole address in its title — with one "Use this feed instead" control: the tap rebuilds
  // the card from that address. Presentational like VerifyPanel: the page owns POST
  // /switch-feed and hands back `busy` and the server's refusal sentence (`error`).
  import type { NiFeedCandidate } from "$lib/api";

  let {
    candidates,
    busy = false,
    error = "",
    onSwitch,
  }: {
    // $lib/ni/browser feedCandidates: https only, unique, ≤ 5.
    candidates: NiFeedCandidate[];
    busy?: boolean;
    // The server's closed refusal sentence (a 409 / 403 detail, verbatim), or "".
    error?: string;
    onSwitch: (url: string) => void;
  } = $props();
</script>

<div class="feed-offer">
  <p class="ni-note-head">This page gets its data from a feed</p>
  <ul class="feed-list">
    {#each candidates as feed (feed.url)}
      <li class="feed-row">
        <span class="feed-url" title={feed.url}>{feed.url}</span>
        <button class="ghost" disabled={busy} onclick={() => onSwitch(feed.url)}>Use this feed instead</button>
      </li>
    {/each}
  </ul>
  {#if error}
    <p class="error ni-note">{error}</p>
  {/if}
</div>

<style>
  /* Sits inside the C2 panel (the accent tint): a hairline sets it apart from Yes / No. */
  .feed-offer {
    display: flex;
    flex-direction: column;
    gap: var(--s-1);
    margin-top: var(--s-3);
    padding-top: var(--s-2);
    border-top: 1px solid var(--border);
  }
  .feed-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  /* The address takes its own line (a card can be one phone column wide), the control
     sits under it. */
  .feed-row {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    min-width: 0;
  }
  .feed-url {
    max-width: 100%;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--f-label);
  }
</style>
