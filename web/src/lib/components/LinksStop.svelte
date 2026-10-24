<script lang="ts">
  // LinksStop — the v2 honest stop: the auto-pick found no Library source that could build this
  // card, so the card says so, lists the pages that may help as READ-ONLY links (they open in a
  // new tab and never build), and keeps the paste-a-link field. There is no source to pick here.
  // The paste text is this component's own state; the page owns the POST and says whether it
  // worked (the text clears only then).
  import Icon from "$lib/components/Icon.svelte";
  import type { NiSuggestion } from "$lib/api";
  import { linkWhat, pickRows } from "$lib/ni/flow";

  let {
    suggestions = undefined,
    busy = false,
    error = "",
    clause = "",
    onPaste,
  }: {
    suggestions?: NiSuggestion[];
    busy?: boolean;
    // The server's refusal sentence for the last paste, or "".
    error?: string;
    // What the paste's consent covers when the page needs the page browser, or "".
    clause?: string;
    // Resolves true when the link was accepted. `needsKey` (release P3): the user said it takes their own key.
    onPaste: (url: string, needsKey: boolean) => Promise<boolean>;
  } = $props();

  let pasted = $state("");
  let needsKey = $state(false);
  const links = $derived(pickRows(suggestions).links);

  async function submit(): Promise<void> {
    console.assert(typeof pasted === "string", "submit: text is a string");
    const url = pasted.trim();
    console.assert(url.length === 0 || url.length >= 1, "submit: url trimmed");
    if (url.length < 8) return;
    if (await onPaste(url, needsKey && url.startsWith("https://"))) {
      pasted = "";
      needsKey = false;
    }
  }
</script>

<div class="ls">
  <p class="ls-head">No SmartBrain Library source answers this yet.</p>
  {#if links.length > 0}
    <p class="muted ls-note">Pages that may help — SmartBrain can't keep a live card from these yet:</p>
    <ul class="ls-links">
      {#each links as link (link.url)}
        <li>
          <a href={link.url} target="_blank" rel="noopener noreferrer"
          ><Icon name="link" size={14} /> {link.title || link.host}</a>
          <span class="muted ls-what">{linkWhat(link)}</span>
        </li>
      {/each}
    </ul>
  {/if}
  <form class="ls-form" onsubmit={(e) => { e.preventDefault(); void submit(); }}>
    <input
      type="url"
      bind:value={pasted}
      placeholder={links.length > 0
        ? "Found the data? Paste its link (https://…)"
        : "Or paste any web page or API URL (https://…)"}
      maxlength="2000"
      disabled={busy}
      aria-label="Paste a source URL"
    />
    <label class="muted ls-key">
      <input type="checkbox" bind:checked={needsKey} disabled={busy} />
      It needs my key
    </label>
    {#if clause}
      <p class="muted ls-note">{clause}</p>
    {/if}
    <button type="submit" class="secondary" disabled={busy || pasted.trim().length < 8}>Use this URL</button>
  </form>
  {#if error}
    <p class="error ls-note">{error}</p>
  {/if}
</div>

<style>
  .ls-key {
    display: flex;
    align-items: center;
    gap: var(--s-1);
  }
  .ls {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .ls-head,
  .ls-note {
    margin: 0;
  }
  .ls-note,
  .ls-what {
    font-size: var(--f-label);
  }
  .ls-links {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .ls-links li {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .ls-what {
    display: block;
  }
  .ls-form {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .ls-form input {
    width: 100%;
    min-width: 0;
  }
  .ls-form button {
    align-self: flex-start;
  }
</style>
