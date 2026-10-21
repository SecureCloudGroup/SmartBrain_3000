<script lang="ts">
  // BuildProgress — the v2 build line on a card that is walking its creation flow: a step bar
  // from `flow.progress` ({step, of, label}), the auto-pick's "Trying source 2 of 3 (host)" while
  // it works through Library sources, and a Cancel button when the backend declares the `cancel`
  // action. Cancel is cooperative on the server (the flow ends failed(cancelled), the card keeps
  // its spec and Retry is offered); the board's per-item poll shows the result. The busy and
  // error state of Cancel is this component's own, never the page's.
  import type { NiFlowProgress } from "$lib/api";

  let {
    itemId,
    progress,
    canCancel = false,
    cancel,
    onCancelled = () => {},
  }: {
    itemId: string;
    progress: NiFlowProgress;
    // The row's backend actions include "cancel".
    canCancel?: boolean;
    // The page passes api.niFlowCancel; tests pass a stub (this file imports no api).
    cancel: (id: string) => Promise<unknown>;
    // Called after a successful cancel so the page can refresh the board.
    onCancelled?: () => void;
  } = $props();

  let busy = $state(false);
  let failure = $state("");

  const of = $derived(Math.max(1, progress.of));
  const step = $derived(Math.min(of, Math.max(0, progress.step)));
  const attempt = $derived(
    progress.try && progress.try.of > 1
      ? `Trying source ${progress.try.n} of ${progress.try.of}${progress.try.host ? ` (${progress.try.host})` : ""}`
      : "",
  );

  async function stop(): Promise<void> {
    console.assert(typeof itemId === "string" && itemId !== "", "stop: itemId required");
    console.assert(!busy, "stop: not already stopping");
    busy = true;
    failure = "";
    try {
      await cancel(itemId);
      onCancelled();
    } catch (err) {
      failure = (err instanceof Error && err.message) || "Couldn’t stop this build.";
    } finally {
      busy = false;
    }
  }
</script>

<div class="bp">
  <div
    class="bp-bar"
    role="progressbar"
    aria-label="Build progress"
    aria-valuemin="0"
    aria-valuemax={of}
    aria-valuenow={step}
    aria-valuetext={`Step ${step} of ${of}: ${progress.label}`}
  >
    {#each Array.from({ length: of }, (_, i) => i) as i (i)}
      <span class="bp-seg" class:done={i < step} class:now={i === step - 1}></span>
    {/each}
  </div>
  <p class="bp-label muted">{progress.label}</p>
  {#if attempt}
    <p class="bp-try muted">{attempt}</p>
  {/if}
  {#if canCancel}
    <div class="bp-actions">
      <button class="ghost" disabled={busy} onclick={stop}>{busy ? "Stopping…" : "Cancel"}</button>
    </div>
  {/if}
  {#if failure}
    <p class="error bp-error">{failure}</p>
  {/if}
</div>

<style>
  .bp {
    display: flex;
    flex-direction: column;
    gap: var(--s-1);
  }
  .bp-bar {
    display: flex;
    gap: 3px;
  }
  .bp-seg {
    flex: 1;
    height: 4px;
    border-radius: 2px;
    background: var(--border);
  }
  .bp-seg.done {
    background: var(--accent);
  }
  .bp-seg.now {
    animation: bp-pulse 1.4s ease-in-out infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    .bp-seg.now {
      animation: none;
    }
  }
  @keyframes bp-pulse {
    50% {
      opacity: 0.45;
    }
  }
  .bp-label,
  .bp-try,
  .bp-error {
    margin: 0;
    font-size: var(--fs-sm, 0.875rem);
  }
  .bp-actions {
    margin-top: var(--s-1);
  }
</style>
