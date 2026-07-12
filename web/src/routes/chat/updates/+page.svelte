<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type RecentScheduleRun } from "$lib/api";
  import { describeError } from "$lib/errors";
  import { scheduleUpdates } from "$lib/scheduleUpdates.svelte";

  // Read-only feed of output from the user's scheduled items — the "Scheduled updates" chat.
  // Data is synthesized from schedule_runs (already encrypted at rest); opening the feed marks
  // every run seen, which clears the nav badge.
  let runs = $state<RecentScheduleRun[]>([]);
  let loading = $state(true);
  let error = $state("");

  function localTs(s: string | null): string {
    if (!s) return "";
    const d = new Date(s.slice(0, 19).replace(" ", "T") + "Z");
    return Number.isNaN(d.getTime()) ? s : d.toLocaleString();
  }
  function runStatusLabel(status: string): string {
    if (status === "awaiting_approval") return "Needs approval";
    if (status === "error") return "Failed";
    if (status === "complete") return "Done";
    return status;
  }
  // The body under each "### Scheduled Item …" header — mirrors the Schedules Output tab.
  function bodyOf(run: RecentScheduleRun): string {
    if (run.error) return run.error;
    if (run.status === "awaiting_approval") return "Awaiting your approval — open Activity to review.";
    return run.message || "(no output)";
  }

  async function load() {
    loading = true;
    error = "";
    try {
      runs = (await api.recentScheduleRuns()).runs;
      // Opening the feed clears the unseen badge (server-side + local store). Set the store
      // directly (authoritative), matching how the Activity page clears its pending badge.
      await api.markScheduleUpdatesSeen();
      scheduleUpdates.count = 0;
    } catch (err) {
      error = describeError(err);
    } finally {
      loading = false;
    }
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await load();
  });
</script>

{#if account.status?.unlocked}
  <div class="updates-head">
    <h1>Scheduled updates</h1>
    <span class="grow"></span>
    <a class="link" href="/chat">&larr; Back to chat</a>
    <button class="secondary" disabled={loading} onclick={load}>Refresh</button>
  </div>
  <p class="muted">
    Output from your scheduled items, newest first. The same output is on the
    <a href="/schedules">Schedules</a> page — and you can ask about it in <a href="/chat">Chat</a> anytime.
  </p>

  {#if error}<p class="error">{error}</p>{/if}
  {#if loading && runs.length === 0}
    <p class="muted">Loading&hellip;</p>
  {:else if runs.length === 0}
    <p class="muted">No scheduled output yet. When one of your schedules fires, its result shows up here.</p>
  {/if}

  <div class="chat-log">
    {#each runs as run (run.id)}
      <div class="bubble {run.status === 'error' ? 'err' : 'assistant'}">{`### Scheduled Item ${run.schedule_title} ###\n\n${bodyOf(run)}`}</div>
      <p class="feed-meta muted">
        {localTs(run.ran_at)} &middot; {runStatusLabel(run.status)}{#if !run.seen} &middot; <span class="new">New</span>{/if}
      </p>
    {/each}
  </div>
{:else}
  <p class="muted">Loading&hellip;</p>
{/if}

<style>
  .updates-head {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    flex-wrap: wrap;
  }
  .updates-head h1 {
    margin: 0;
  }
  /* Per-run timestamp/status line under each bubble; the "New" marker on not-yet-seen runs. */
  .feed-meta {
    margin: 0.15rem 0 0.85rem 0.2rem;
    font-size: 0.8em;
  }
  .new {
    color: var(--accent);
    font-weight: 600;
  }
</style>
