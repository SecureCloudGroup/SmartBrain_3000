<script lang="ts">
  // PageBrowserStatus — the "Page browser" card on Settings → Status (Round 20, B3b; ni-format
  // §35), the Voice card's sibling: one row per shipped engine (`/api/status/overview
  // .browsers[]`) with one chip for its phase (and a second only while it is paused after
  // repeated failures, `health: "unhealthy"`), a progress bar while it installs or checks
  // itself, its version and age, the sandbox it runs in and when it last passed its check, and
  // why it can or cannot read pages here — every sentence from the closed tables in
  // $lib/browserStatus — then the card flow's counts (`browsers_flow`, unlocked only). There is
  // no Retry / Turn off control: the server exposes none (an engine prepares itself the first
  // time a card needs it; SMARTBRAIN_NO_BROWSER=1 switches the component off).
  import Chip from "$lib/components/Chip.svelte";
  import type { BrowserEngineStatus, BrowsersFlow } from "$lib/api";
  import {
    engineChip,
    engineInMotion,
    engineMeta,
    engineReason,
    healthChip,
    sandboxLine,
    workerChip,
  } from "$lib/browserStatus";

  let {
    engines,
    flow = undefined,
  }: {
    engines: BrowserEngineStatus[];
    // Absent while locked and from a server older than Round 20 (B3a).
    flow?: BrowsersFlow;
  } = $props();
</script>

<div class="card">
  <h2 class="row"><span>Page browser</span></h2>
  <div class="pb-rows">
    {#each engines as engine (engine.name)}
      {@const chip = engineChip(engine)}
      {@const health = healthChip(engine)}
      <div class="pb-engine">
        <div class="pb-row">
          <span>{engine.name} <span class="muted">{engineMeta(engine)}</span></span>
          <span class="pb-chips">
            <Chip kind={chip.kind}>{chip.label}</Chip>
            {#if health}
              <Chip kind={health.kind} title={health.title}>{health.label}</Chip>
            {/if}
          </span>
        </div>
        {#if engineInMotion(engine)}
          <progress class="pb-bar" max="100" value={engine.pct} aria-label={`${engine.name} ${chip.label}`}></progress>
        {/if}
        <p class="muted pb-note">{sandboxLine(engine)}</p>
        <p class="muted pb-note">{engineReason(engine)}</p>
      </div>
    {/each}
    {#if flow}
      {@const worker = workerChip(flow)}
      <div class="pb-row"><span>Cards that read their page in it</span><strong>{flow.rendered_cards}</strong></div>
      <div class="pb-row"><span>Waiting for your Activate</span><strong>{flow.drafts}</strong></div>
      <div class="pb-row"><span>Right now</span><Chip kind={worker.kind}>{worker.label}</Chip></div>
    {/if}
  </div>
</div>

<style>
  /* The Status page's row rhythm (.rows / .srow there are scoped to that page). */
  .pb-rows {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
    margin-top: var(--s-1);
  }
  .pb-engine {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .pb-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-4);
    font-size: var(--f-label);
  }
  .pb-chips {
    display: inline-flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--s-1);
  }
  .pb-note {
    margin: 0;
    font-size: var(--f-meta);
  }
  /* The Voice card's download bar, as a native <progress> (no inline width): the accent
     tint track, the accent-strong fill, fully rounded. */
  .pb-bar {
    appearance: none;
    width: 100%;
    max-width: 14rem;
    height: 8px;
    border: 0;
    border-radius: var(--r-full);
    background: var(--accent-tint);
    overflow: hidden;
  }
  .pb-bar::-webkit-progress-bar {
    background: var(--accent-tint);
    border-radius: var(--r-full);
  }
  .pb-bar::-webkit-progress-value {
    background: var(--accent-strong);
    border-radius: var(--r-full);
  }
  .pb-bar::-moz-progress-bar {
    background: var(--accent-strong);
    border-radius: var(--r-full);
  }
</style>
