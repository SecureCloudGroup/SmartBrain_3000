<script lang="ts">
  // Settings → Self-improvement: the two framework switches plus full visibility into
  // everything it has done (the change ledger) and learned (optimizer strategies).
  // An autonomous-within-bounds system owes the user an inspectable record — this is it.
  import { onMount } from "svelte";
  import { api, type Improvement, type OptimizerStrategy, type SelfImproveInterval } from "$lib/api";
  import { describeError } from "$lib/errors";
  import Spinner from "$lib/components/Spinner.svelte";

  // The segmented control's options — one Off plus the four allowed cadences. Order and
  // set must match SelfImproveInterval and the server's ALLOWED_INTERVAL_HOURS.
  const CADENCE_OPTIONS: { value: "off" | SelfImproveInterval; label: string }[] = [
    { value: "off", label: "Off" },
    { value: 2, label: "2h" },
    { value: 4, label: "4h" },
    { value: 8, label: "8h" },
    { value: 24, label: "24h" },
  ];

  let loaded = $state(false);
  let reviewerOn = $state(false);
  let reviewerInterval = $state<SelfImproveInterval>(8);
  let lastRun = $state<string | null>(null);
  let optimizerOn = $state(false);
  let strategies = $state<OptimizerStrategy[]>([]);
  let improvements = $state<Improvement[]>([]);
  let busy = $state("");
  let error = $state("");

  // The highlighted segment: Off when the reviewer is disabled (regardless of the stored
  // interval), otherwise the stored interval. Derived so a server echo re-renders it.
  const cadenceChoice = $derived<"off" | SelfImproveInterval>(
    reviewerOn ? reviewerInterval : "off",
  );

  async function load() {
    try {
      const [si, opt, ledger] = await Promise.all([
        api.getSelfImprove(), api.getOptimizer(), api.listImprovements(),
      ]);
      reviewerOn = si.enabled;
      reviewerInterval = si.interval_hours;
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

  async function setCadence(choice: "off" | SelfImproveInterval) {
    console.assert(busy !== "reviewer", "setCadence: reentered while a PUT was in flight");
    console.assert(
      choice === "off" || CADENCE_OPTIONS.some((o) => o.value === choice),
      "setCadence: unknown choice",
    );
    // One PUT per click — since the API takes both fields, an hour pick sends
    // { enabled: true, interval_hours } together; Off leaves interval untouched.
    const patch = choice === "off"
      ? { enabled: false }
      : { enabled: true, interval_hours: choice };
    busy = "reviewer";
    error = "";
    try {
      const next = await api.putSelfImprove(patch);
      reviewerOn = next.enabled;
      reviewerInterval = next.interval_hours;
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
  SmartBrain can review its own recent performance on the cadence you pick below — every 2,
  4, 8, or 24 hours — and carefully improve — always local, always reversible, and never
  silent: every change it applies or reverts is announced in the chat feed, and everything
  lives in the record below. Both switches are off until you turn them on.
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
      <div class="segmented" role="radiogroup" aria-label="Self-review cadence">
        {#each CADENCE_OPTIONS as opt (opt.value)}
          <button
            type="button"
            role="radio"
            aria-checked={cadenceChoice === opt.value}
            class:active={cadenceChoice === opt.value}
            disabled={busy === "reviewer"}
            onclick={() => setCadence(opt.value)}
          >
            {opt.label}
          </button>
        {/each}
      </div>
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
  /* Segmented radio group for the review cadence — matches the pill-tab voice
     (transparent buttons, accent-tint highlight for the active option) rather than
     inventing a new control. Wraps on narrow rows instead of overflowing. */
  .segmented {
    display: inline-flex;
    flex-wrap: wrap;
    gap: var(--s-1);
    flex: none;
  }
  .segmented button {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: var(--r-1);
    font-size: var(--f-label);
    font-weight: 500;
    min-height: 36px;
    padding: 7px 12px;
    min-width: 3rem;
  }
  .segmented button:hover:not(:disabled) {
    color: var(--text);
    background: var(--elevated);
    filter: none;
  }
  .segmented button.active {
    background: var(--accent-tint);
    border-color: transparent;
    color: var(--accent);
    font-weight: 600;
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
