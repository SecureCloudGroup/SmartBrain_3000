<script lang="ts">
  // Settings → Self-improvement: the two framework switches plus full visibility into
  // everything it has done (the change ledger) and learned (optimizer strategies).
  // An autonomous-within-bounds system owes the user an inspectable record — this is it.
  import { onMount } from "svelte";
  import { api, type Improvement, type OptimizerStrategy } from "$lib/api";
  import { describeError } from "$lib/errors";
  import Spinner from "$lib/components/Spinner.svelte";

  let loaded = $state(false);
  let reviewerOn = $state(false);
  let lastRun = $state<string | null>(null);
  let optimizerOn = $state(false);
  let strategies = $state<OptimizerStrategy[]>([]);
  let improvements = $state<Improvement[]>([]);
  let busy = $state("");
  let error = $state("");

  async function load() {
    try {
      const [si, opt, ledger] = await Promise.all([
        api.getSelfImprove(), api.getOptimizer(), api.listImprovements(),
      ]);
      reviewerOn = si.enabled;
      lastRun = si.last_run;
      optimizerOn = opt.enabled;
      strategies = opt.strategies;
      improvements = ledger.improvements;
    } catch (err) {
      error = describeError(err);
    } finally {
      loaded = true;
    }
  }
  onMount(load);

  async function toggleReviewer() {
    busy = "reviewer";
    error = "";
    try {
      reviewerOn = (await api.putSelfImprove(!reviewerOn)).enabled;
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function toggleOptimizer() {
    busy = "optimizer";
    error = "";
    try {
      optimizerOn = (await api.putOptimizer(!optimizerOn)).enabled;
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  // Human labels for ledger rows — what happened, in plain words.
  const STATUS_LABEL: Record<string, string> = {
    proposed: "proposed",
    active: "applied",
    reverted: "reverted (made things worse)",
    rejected: "rejected by you",
    superseded: "superseded",
  };
  const CATEGORY_LABEL: Record<string, string> = {
    preference: "learned preference",
    workflow: "suggested routine",
    knowledge: "knowledge gap",
    prompt: "prompt strategy",
  };
  const fmtWhen = (ts: string | null) => (ts ? ts.slice(0, 16).replace("T", " ") : "");
</script>

<h1>Self-improvement</h1>
<p class="muted">
  SmartBrain can review its own recent performance every 8 hours and carefully improve —
  always local, always reversible, and never silent: every change it applies or reverts is
  announced in the chat feed, and everything lives in the record below. Both switches are
  off until you turn them on.
</p>

{#if !loaded}
  <Spinner />
{:else}
  {#if error}<p class="error">{error}</p>{/if}

  <section class="card">
    <div class="row">
      <div>
        <h2>Self-review</h2>
        <p class="muted">
          Scores Chat, Knowledge, and Tools from private on-device telemetry; when something
          needs attention, a local model may learn <em>one</em> preference at a time — applied
          as a visible "(learned)&nbsp;…" memory fact, measured, and auto-reverted if it
          doesn't help. Also spots routines worth a schedule (parked in Activity for your
          approval) and searches your knowledge couldn't answer.
          {#if lastRun}<br />Last review: {fmtWhen(lastRun)}.{/if}
        </p>
      </div>
      <button class="toggle" disabled={busy === "reviewer"} onclick={toggleReviewer}>
        {reviewerOn ? "On" : "Off"}
      </button>
    </div>
  </section>

  <section class="card">
    <div class="row">
      <div>
        <h2>Prompt optimizer</h2>
        <p class="muted">
          Learns how different kinds of requests (factual, multi-step, code, retrieval) go —
          and may steer them with a short guidance note. A strategy starts in
          <em>shadow</em> (it only watches), goes live only after a measured trial proves the
          problem is real, and is turned off automatically if it doesn't help. Guided answers
          show a small "guided&nbsp;·&nbsp;…" chip.
        </p>
      </div>
      <button class="toggle" disabled={busy === "optimizer"} onclick={toggleOptimizer}>
        {optimizerOn ? "On" : "Off"}
      </button>
    </div>
    {#if strategies.length}
      <ul class="plain">
        {#each strategies as s (s.id)}
          <li>
            <span class="badge {s.status}">{s.status}</span>
            <strong>{s.request_type.replace("_", " ")}</strong> — “{s.directive}”
            <span class="muted">· would have applied {s.fired}×</span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="card">
    <h2>What it has done</h2>
    {#if improvements.length === 0}
      <p class="muted">Nothing yet. Improvements appear here as they are proposed, applied,
        kept, or reverted — with the digest announcing anything that changes behavior.</p>
    {:else}
      <ul class="plain">
        {#each improvements as imp (imp.id)}
          <li>
            <span class="badge {imp.status}">{STATUS_LABEL[imp.status] ?? imp.status}</span>
            <strong>{CATEGORY_LABEL[imp.category] ?? imp.category}</strong> — {imp.description}
            <span class="muted">· {fmtWhen(imp.created_at)}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}

<style>
  .row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }
  .toggle {
    min-width: 4rem;
    flex: none;
  }
  ul.plain {
    list-style: none;
    padding: 0;
    margin: 0.9rem 0 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  ul.plain li {
    font-size: 0.9rem;
    line-height: 1.45;
  }
  .badge {
    display: inline-block;
    font-size: 0.7rem;
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    color: var(--muted);
    margin-right: 0.4rem;
    text-transform: none;
  }
  .badge.active { color: var(--ok, #4a9); border-color: currentColor; }
  .badge.reverted, .badge.disabled { color: var(--warn, #c66); border-color: currentColor; }
</style>
