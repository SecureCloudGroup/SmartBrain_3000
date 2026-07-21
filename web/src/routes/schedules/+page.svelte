<script lang="ts">
  import Icon from "$lib/components/Icon.svelte";
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type Schedule, type RecentScheduleRun } from "$lib/api";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";

  type Tab = "output" | "create" | "items";
  let tab = $state<Tab>("output");

  let schedules = $state<Schedule[]>([]);
  let recentRuns = $state<RecentScheduleRun[]>([]); // aggregate output across all schedules

  // Create form
  let title = $state("");
  let prompt = $state("");
  let repeat = $state(1440); // minutes; 0 = once
  let startIn = $state(0); // minutes until the first run
  let busy = $state(false);

  // Items: per-card action + inline edit of an existing schedule
  let busyId = $state<string | null>(null);
  let editId = $state<string | null>(null);
  let editTitle = $state("");
  let editPrompt = $state("");
  let editRepeat = $state(1440);

  let error = $state("");
  let notice = $state(""); // transient action feedback (e.g. "ran — see Output")

  const repeats = [
    { value: 0, label: "Once" },
    { value: 60, label: "Hourly" },
    { value: 1440, label: "Daily" },
    { value: 10080, label: "Weekly" },
  ];

  // One-click starting points — fill the Create form so the user can review then Add.
  const presets = [
    { label: "Check the news", title: "News check", prompt: "Search the web for today's top news headlines and give me a short summary.", repeat: 1440 },
    { label: "Morning briefing", title: "Morning briefing", prompt: "Brief me: my open tasks due today, plus anything notable I should know.", repeat: 1440 },
    { label: "Weekly knowledge review", title: "Weekly review", prompt: "Summarize what I added to my knowledge base over the past week.", repeat: 10080 },
  ];
  function usePreset(p: (typeof presets)[number]) {
    title = p.title;
    prompt = p.prompt;
    repeat = p.repeat;
  }
  const starts = [
    { value: 0, label: "Now" },
    { value: 60, label: "In 1 hour" },
    { value: 1440, label: "Tomorrow" },
  ];

  function repeatLabel(m: number): string {
    return repeats.find((r) => r.value === m)?.label ?? `Every ${m} min`;
  }

  // Server timestamps are UTC strings (e.g. "2026-06-21 14:30:00") — render in the user's locale.
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

  async function load() {
    try {
      schedules = (await api.listSchedules()).schedules;
    } catch (err) {
      error = describeError(err);
    }
  }

  async function loadOutput() {
    try {
      recentRuns = (await api.recentScheduleRuns()).runs;
    } catch (err) {
      error = describeError(err);
    }
  }

  async function selectTab(t: Tab) {
    tab = t;
    notice = "";
    if (t === "output") await loadOutput();
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await Promise.all([load(), loadOutput()]);
  });

  async function add(event: Event) {
    event.preventDefault();
    if (!title.trim() || !prompt.trim()) return;
    busy = true;
    error = "";
    try {
      await api.addSchedule({ title: title.trim(), prompt: prompt.trim(), interval_minutes: repeat, start_in_minutes: startIn, model: null });
      title = "";
      prompt = "";
      await load();
      notice = "Schedule created.";
      tab = "items"; // show it in the list
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function toggle(s: Schedule) {
    error = "";
    try {
      await api.setScheduleEnabled(s.id, !s.enabled);
      await load();
    } catch (err) {
      error = describeError(err);
    }
  }

  function startEdit(s: Schedule) {
    editId = s.id;
    editTitle = s.title;
    editPrompt = s.prompt;
    editRepeat = s.interval_minutes;
    error = "";
  }
  function cancelEdit() {
    editId = null;
  }
  async function saveEdit(s: Schedule) {
    if (!editTitle.trim() || !editPrompt.trim()) return;
    busyId = s.id;
    error = "";
    try {
      // start_in_minutes is irrelevant for an existing schedule (first run already set); the backend ignores it.
      await api.updateSchedule(s.id, { title: editTitle.trim(), prompt: editPrompt.trim(), interval_minutes: editRepeat, start_in_minutes: 0, model: s.model });
      editId = null;
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busyId = null;
    }
  }

  async function remove(id: string) {
    if (!(await confirmDialog({ title: "Delete schedule", body: "Delete this schedule?", confirmLabel: "Delete", danger: true }))) return;
    error = "";
    try {
      await api.deleteSchedule(id);
      schedules = schedules.filter((s) => s.id !== id);
    } catch (err) {
      error = describeError(err);
    }
  }

  async function runNow(s: Schedule) {
    error = "";
    notice = "";
    busyId = s.id;
    try {
      const r = await api.runSchedule(s.id);
      notice =
        r.status === "awaiting_approval" ? `“${s.title}” needs approval — see Activity.`
        : r.status === "error" ? `“${s.title}” failed — details below.`
        : `“${s.title}” ran — output below.`;
      await Promise.all([load(), loadOutput()]);
      tab = "output"; // the result now shows at the top of Output
    } catch (err) {
      error = describeError(err);
    } finally {
      busyId = null;
    }
  }
</script>

{#if account.status?.unlocked}
  <h1>Schedules</h1>
  <p class="muted">
    A schedule runs a prompt on a timer while the app is unlocked. Read-only steps run on their
    own; anything that changes data or sends waits for your approval in Activity.
  </p>

  <div class="subtabs" role="tablist">
    <button role="tab" aria-selected={tab === "output"} class:active={tab === "output"} onclick={() => selectTab("output")}>Output</button>
    <button role="tab" aria-selected={tab === "create"} class:active={tab === "create"} onclick={() => selectTab("create")}>Create</button>
    <button role="tab" aria-selected={tab === "items"} class:active={tab === "items"} onclick={() => selectTab("items")}>Items</button>
  </div>

  {#if notice}<p class="notice">{notice}</p>{/if}

  {#if tab === "output"}
    <div class="output-head">
      <p class="muted" style="margin:0; flex:1">Latest output from your schedules, newest first.</p>
      <button class="secondary" onclick={loadOutput}>Refresh</button>
    </div>
    {#if recentRuns.length === 0}
      <p class="muted">No output yet. Runs appear here after a schedule fires — or use “Run now” on the Items tab.</p>
    {/if}
    {#each recentRuns as run (run.id)}
      <div class="card run-card">
        <p class="run-head">Scheduled Item: {run.schedule_title}</p>
        <p class="muted" style="margin:0 0 0.35rem; font-size:0.8em">{localTs(run.ran_at)} · {runStatusLabel(run.status)}</p>
        {#if run.error}
          <p class="error" style="margin:0; white-space:pre-wrap">{run.error}</p>
        {:else if run.status === "awaiting_approval"}
          <p class="notice" style="margin:0">Awaiting your approval — open Activity to review.</p>
        {:else}
          <p style="margin:0; white-space:pre-wrap">{run.message || "(no output)"}</p>
        {/if}
      </div>
    {/each}
  {:else if tab === "create"}
    <div class="card">
      <p class="muted" style="margin:0 0 0.5rem; font-size:0.85rem">
        Quick start:
        {#each presets as p (p.label)}
          <button type="button" class="secondary" style="margin:0 0.25rem 0.25rem 0" onclick={() => usePreset(p)}>{p.label}</button>
        {/each}
      </p>
      <form onsubmit={add} style="display:flex; flex-direction:column; gap:0.5rem">
        <input bind:value={title} placeholder="Name (e.g. Morning task review)" />
        <textarea bind:value={prompt} rows="2" placeholder="What should it do? (e.g. Summarize my open tasks)"></textarea>
        <div style="display:flex; gap:0.5rem; flex-wrap:wrap; align-items:center">
          <label>Repeat
            <select bind:value={repeat}>
              {#each repeats as r (r.value)}<option value={r.value}>{r.label}</option>{/each}
            </select>
          </label>
          <label>First run
            <select bind:value={startIn}>
              {#each starts as s (s.value)}<option value={s.value}>{s.label}</option>{/each}
            </select>
          </label>
          <span class="spacer"></span>
          <button disabled={busy || !title.trim() || !prompt.trim()} type="submit">Add schedule</button>
        </div>
      </form>
    </div>
  {:else}
    {#if schedules.length === 0}
      <p class="muted">No schedules yet. Use the <button type="button" class="linklike" onclick={() => selectTab("create")}>Create</button> tab to add one.</p>
    {/if}
    {#each schedules as s (s.id)}
      <div class="card">
        {#if editId === s.id}
          <div style="display:flex; flex-direction:column; gap:0.5rem">
            <input bind:value={editTitle} placeholder="Name" />
            <textarea bind:value={editPrompt} rows="2" placeholder="What should it do?"></textarea>
            <div style="display:flex; gap:0.5rem; flex-wrap:wrap; align-items:center">
              <label>Repeat
                <select bind:value={editRepeat}>
                  {#each repeats as r (r.value)}<option value={r.value}>{r.label}</option>{/each}
                </select>
              </label>
              <span class="spacer"></span>
              <button disabled={busyId === s.id || !editTitle.trim() || !editPrompt.trim()} onclick={() => saveEdit(s)}>Save</button>
              <button class="secondary" disabled={busyId === s.id} onclick={cancelEdit}>Cancel</button>
            </div>
          </div>
        {:else}
          <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
            <input type="checkbox" style="width:auto" checked={s.enabled} onchange={() => toggle(s)} aria-label="Enabled" />
            <strong style="flex:1; min-width:8rem; {s.enabled ? '' : 'opacity:0.55'}">{s.title}</strong>
            <span class="muted">{repeatLabel(s.interval_minutes)}</span>
            <button class="secondary" disabled={busyId === s.id} onclick={() => startEdit(s)}>Edit</button>
            <button disabled={busyId === s.id} onclick={() => runNow(s)}>{busyId === s.id ? "Running…" : "Run now"}</button>
            <button class="del" title="Delete" aria-label="Delete schedule" onclick={() => remove(s.id)}><Icon name="x" size={14} /></button>
          </div>
          <p class="muted" style="margin:0.4rem 0 0">{s.prompt}</p>
          <p class="muted" style="margin:0.2rem 0 0; font-size:0.85em">
            Next: {localTs(s.next_run)}{#if s.last_run} · Last: {localTs(s.last_run)}{/if}
          </p>
        {/if}
      </div>
    {/each}
  {/if}

  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <p class="muted">Loading&hellip;</p>
{/if}

<style>
  .subtabs {
    display: flex;
    gap: 0.25rem;
    border-bottom: 1px solid var(--border);
    margin: 0.75rem 0 1rem;
    overflow-x: auto; /* keep the three tabs on one row on a narrow phone */
  }
  .subtabs button {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 0.5rem 0.9rem;
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }
  .subtabs button.active {
    color: var(--text);
    border-bottom-color: var(--text);
    font-weight: 600;
  }
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
  .linklike {
    background: none;
    border: none;
    padding: 0;
    color: var(--text);
    text-decoration: underline;
    cursor: pointer;
    font: inherit;
  }
</style>
