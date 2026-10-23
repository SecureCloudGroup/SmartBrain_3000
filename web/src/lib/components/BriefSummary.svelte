<script lang="ts">
  // BriefSummary — the opt-in summary of a headline brief card (plan S8, decision D4 "Headlines
  // + local"). The headlines above it are the source's own words, extracted by code; this block
  // is the only place a model's words can appear, and only when the owner turns it on. Turning it
  // on asks the server to pin the card to a LOCAL model (it refuses, with a plain reason, when
  // there is none: no cloud fallback exists), then refreshes the card once so a summary is
  // written. The text always sits under the server's own label ("Summary written by your local
  // model"). Form state (busy, the refusal sentence) lives here, in the card's own component.
  import { api, type NiBoardItem } from "$lib/api";
  import { describeError } from "$lib/errors";

  let { item, onchanged }: { item: NiBoardItem; onchanged: () => Promise<void> | void } = $props();

  let busy = $state(false);
  let refusal = $state("");

  const brief = $derived(item.brief);
  // Kept cards only: a held card (waiting for the owner's Keep) and a draft have no summary yet.
  const kept = $derived(["live", "degraded", "failing", "paused"].includes(item.state) && !item.awaiting_yes);
  const on = $derived(brief?.summary === true);
  const unavailable = $derived(!on && brief?.local === false);

  async function flip() {
    if (!brief || busy) return;
    const next = !on;
    busy = true;
    refusal = "";
    try {
      await api.niBriefSummary(item.id, next);
      // One refresh writes the first summary; a failed refresh leaves the card as it was and says so below.
      if (next) await api.niRun(item.id).catch(() => undefined);
      await onchanged();
    } catch (err) {
      refusal = describeError(err);
    } finally {
      busy = false;
    }
  }
</script>

{#if brief && (kept || on)}
  <div class="brief" data-state={brief.state}>
    {#if on && brief.state === "ready"}
      <p class="brief-label">{brief.label}</p>
      <p class="brief-text">{brief.text}</p>
    {:else if on && brief.state === "unavailable"}
      <p class="muted brief-note">No summary this time: your local model could not write one. The headlines above are current.</p>
    {:else if on}
      <p class="muted brief-note">Your local model will write the summary on the next refresh.</p>
    {/if}
    <label class="brief-toggle" class:disabled={unavailable || busy}>
      <input
        type="checkbox"
        role="switch"
        checked={on}
        disabled={busy || !kept || unavailable}
        onchange={flip}
        aria-describedby="brief-help-{item.id}"
      />
      <span>Summary from your local model</span>
    </label>
    <p class="muted brief-help" id="brief-help-{item.id}">
      {#if unavailable}
        No local model is set up on this computer, so this stays off. A summary is never written by a cloud model.
      {:else if on}
        Written on this computer after each refresh. The headlines above are always the source's own words.
      {:else}
        Off: you see the source's headlines as they are. Turn it on and your local model writes a short summary
        of them after each refresh. Nothing is sent to a cloud model.
      {/if}
    </p>
    {#if refusal}
      <p class="error brief-refusal" role="alert">{refusal}</p>
    {/if}
  </div>
{/if}

<style>
  .brief {
    display: flex;
    flex-direction: column;
    gap: var(--s-1);
    margin-top: var(--s-2);
    padding-top: var(--s-2);
    border-top: 1px solid var(--border);
    min-width: 0;
  }
  .brief-label {
    margin: 0;
    font-size: var(--f-label);
    color: var(--muted, inherit);
  }
  .brief-text {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .brief-note,
  .brief-help,
  .brief-refusal {
    margin: 0;
    font-size: var(--f-label);
  }
  .brief-toggle {
    display: flex;
    align-items: center;
    gap: var(--s-2);
    cursor: pointer;
  }
  .brief-toggle.disabled {
    cursor: default;
    opacity: 0.7;
  }
</style>
