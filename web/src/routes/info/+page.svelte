<script lang="ts">
  import Tabs from "$lib/components/Tabs.svelte";
  import EmptyState from "$lib/components/EmptyState.svelte";
  import Spinner from "$lib/components/Spinner.svelte";
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type RecentScheduleRun, type Schedule, type ScheduleRun } from "$lib/api";
  import { describeError } from "$lib/errors";
  import { localTs, runStatusLabel } from "$lib/runs";

  // This page only READS run output. Marking runs seen stays with Chat's
  // Scheduled-updates feed (markScheduleUpdatesSeen) — calling it here would stop
  // new-run notices from ever appearing in Chat.

  let tab = $state("all"); // "all" or a schedule id
  let schedules = $state<Schedule[]>([]);
  let recentRuns = $state<RecentScheduleRun[]>([]); // All tab: aggregate, newest first
  let scheduleRuns = $state<ScheduleRun[]>([]); // per-schedule tab
  let error = $state("");

  async function loadSchedules() {
    schedules = (await api.listSchedules()).schedules;
  }

  async function loadAll() {
    try {
      recentRuns = (await api.recentScheduleRuns()).runs;
    } catch (err) {
      error = describeError(err);
    }
  }

  async function loadSchedule(sid: string) {
    try {
      scheduleRuns = (await api.listScheduleRuns(sid)).runs;
    } catch (err) {
      error = describeError(err);
      if ((err as { status?: number })?.status === 404) {
        // The schedule was deleted since the tabs rendered — fall back to All.
        tab = "all";
        await Promise.all([loadSchedules(), loadAll()]);
      }
    }
  }

  async function selectTab(id: string) {
    tab = id;
    error = "";
    scheduleRuns = [];
    if (id === "all") await loadAll();
    else await loadSchedule(id);
  }

  async function refresh() {
    error = "";
    if (tab === "all") await loadAll();
    else await loadSchedule(tab);
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    try {
      await Promise.all([loadSchedules(), loadAll()]);
    } catch (err) {
      error = describeError(err);
    }
  });
</script>

{#if account.status?.unlocked}
  <h1>Info</h1>
  <p class="muted">
    Output from your scheduled runs, newest first — everything, or one schedule at a time.
    Manage the schedules themselves on the <a href="/schedules">Schedules</a> page.
  </p>

  <Tabs
    tabs={[{ id: "all", label: "All" }, ...schedules.map((s) => ({ id: s.id, label: s.title }))]}
    active={tab}
    onselect={(id) => selectTab(id)}
  />

  <div class="output-head">
    <span class="spacer"></span>
    <button class="secondary" onclick={refresh}>Refresh</button>
  </div>

  {#if tab === "all"}
    {#if recentRuns.length === 0}
      <EmptyState icon="info" title="Nothing has run yet" body="Runs land here after a schedule fires — or use “Run now” on the Schedules page to see one immediately." />
    {/if}
    {#each recentRuns as run (run.id)}
      <div class="card run-card">
        <p class="run-head">Scheduled Item: {run.schedule_title}</p>
        <p class="muted" style="margin:0 0 0.35rem; font-size:0.8em">{localTs(run.ran_at)} · {runStatusLabel(run.status)}</p>
        {#if run.error}
          <p class="error run-body">{run.error}</p>
        {:else if run.status === "awaiting_approval"}
          <p class="notice" style="margin:0">Awaiting your approval — open Activity to review.</p>
        {:else}
          <p class="run-body">{run.message || "(no output)"}</p>
        {/if}
      </div>
    {/each}
  {:else}
    {#if scheduleRuns.length === 0 && !error}
      <EmptyState icon="info" title="No output yet" body="This schedule hasn’t produced output yet — “Run now” on the Schedules page fires it immediately." />
    {/if}
    {#each scheduleRuns as run (run.id)}
      <div class="card run-card">
        <p class="muted" style="margin:0 0 0.35rem; font-size:0.8em">{localTs(run.ran_at)} · {runStatusLabel(run.status)}</p>
        {#if run.error}
          <p class="error run-body">{run.error}</p>
        {:else if run.status === "awaiting_approval"}
          <p class="notice" style="margin:0">Awaiting your approval — open Activity to review.</p>
        {:else}
          <p class="run-body">{run.message || "(no output)"}</p>
        {/if}
      </div>
    {/each}
  {/if}

  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <Spinner block />
{/if}

<style>
  .output-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
  }
  .run-card .run-head {
    margin: 0 0 0.2rem;
    font-weight: 600;
  }
  /* Run output is model text: preserve its line breaks, but break ANY long token —
     an unwrappable URL used to widen the whole page on phones and clip every line. */
  .run-body {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
