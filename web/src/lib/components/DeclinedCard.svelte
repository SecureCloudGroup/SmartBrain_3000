<script lang="ts">
  // DeclinedCard — the honest end of an ask the system cannot answer reliably (plan G6: "a decline is
  // acceptable, a wrong card is the failure"). It says so plainly, gives the backend's one-line reason and up to
  // three links (read-only, opened in a new tab; nothing here builds or fetches), and offers the way back: run it
  // again, paste a link to the data (a shell), keep looking for a source (a standing order) or stop, bring the
  // user's own key to a source that asked for one (Use my key), or on a finished card Fix / Refine. The page owns the requests; this component owns what it shows and nothing else
  // needs to be kept for it.
  import Icon from "$lib/components/Icon.svelte";
  import type { FlowDecline } from "$lib/ni/types.gen";
  import { DECLINED_HEADLINE, declinedLinks } from "$lib/ni/flow";
  import { relTime } from "$lib/ni/time";

  let {
    decline,
    busy = false,
    error = "",
    canFix = false,
    canRefine = false,
    canRetry = false,
    canPick = false,
    canKeepLooking = false,
    canStopLooking = false,
    canUseKey = false,
    onFix,
    onRefine,
    onRetry,
    onPick,
    onKeepLooking = () => {},
    onStopLooking = () => {},
    onUseKey = () => {},
  }: {
    decline: FlowDecline;
    busy?: boolean;
    // The server's sentence for the last reopen that failed, or "".
    error?: string;
    canFix?: boolean;
    canRefine?: boolean;
    canRetry?: boolean;
    canPick?: boolean;
    canKeepLooking?: boolean;
    canStopLooking?: boolean;
    canUseKey?: boolean;
    onFix: () => void;
    onRefine: () => void;
    onRetry: () => void;
    onPick: () => void;
    onKeepLooking?: () => void;
    onStopLooking?: () => void;
    onUseKey?: () => void;
  } = $props();

  const links = $derived(declinedLinks(decline));
  const headline = $derived(decline.headline.trim() || DECLINED_HEADLINE);
</script>

<div class="dc" data-reason={decline.reason}>
  <p class="dc-head">{headline}</p>
  <p class="muted dc-line">{decline.line}</p>
  {#if decline.looking}
    <p class="dc-looking">Still looking (started {relTime(decline.looking.since)}). When I find a source, the card
      shows here for your Keep.</p>
  {/if}
  {#if links.length > 0}
    <p class="muted dc-note">Pages that may help — SmartBrain can't keep a live card from these:</p>
    <ul class="dc-links">
      {#each links as link (link.url)}
        <li>
          <a href={link.url} target="_blank" rel="noopener noreferrer"
          ><Icon name="link" size={14} /> {link.title || link.host}</a>
          <span class="muted dc-host">{link.host}</span>
        </li>
      {/each}
    </ul>
  {/if}
  {#if decline.key_wall && canUseKey}
    <p class="muted dc-note">{decline.key_wall.host} asks for a key. If you have one, SmartBrain can use it there,
      and only there.</p>
  {/if}
  {#if canFix || canRefine || canRetry || canPick || canKeepLooking || canStopLooking || canUseKey}
    <div class="dc-actions">
      {#if canUseKey && decline.key_wall}
        <button class="secondary" disabled={busy} onclick={onUseKey}>Use my key for {decline.key_wall.host}</button>
      {/if}
      {#if canKeepLooking}
        <button class="secondary" disabled={busy} onclick={onKeepLooking}>Keep looking</button>
      {/if}
      {#if canFix}
        <button class="secondary" disabled={busy} onclick={onFix}>Fix</button>
      {/if}
      {#if canRefine}
        <button class="ghost" disabled={busy} onclick={onRefine}>Refine…</button>
      {/if}
      {#if canRetry}
        <button class="secondary" disabled={busy} onclick={onRetry}>{busy ? "Retrying…" : "Try again"}</button>
      {/if}
      {#if canPick}
        <button class="ghost" disabled={busy} onclick={onPick}>Paste a link</button>
      {/if}
      {#if canStopLooking}
        <button class="ghost" disabled={busy} onclick={onStopLooking}>Stop looking</button>
      {/if}
    </div>
  {/if}
  {#if error}
    <p class="error dc-note">{error}</p>
  {/if}
</div>

<style>
  .dc {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .dc-head {
    margin: 0;
    font-weight: 600;
  }
  .dc-line,
  .dc-note,
  .dc-looking {
    margin: 0;
  }
  .dc-note,
  .dc-host {
    font-size: var(--f-label);
  }
  .dc-links {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .dc-links li {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .dc-host {
    display: block;
  }
  .dc-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s-2);
  }
</style>
