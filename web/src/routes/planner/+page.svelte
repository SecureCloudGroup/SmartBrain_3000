<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type Task, type TaskInput, type TaskPriority, type TaskRecur } from "$lib/api";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";

  let tasks = $state<Task[]>([]);
  let title = $state("");
  let notes = $state("");
  let due = $state("");
  let dueTime = $state("");
  let priority = $state<TaskPriority>("medium");
  let recur = $state<TaskRecur>("none");
  let tagsStr = $state("");
  let busy = $state(false);
  let error = $state("");
  // Inline edit of an existing task.
  let editId = $state<string | null>(null);
  let eTitle = $state("");
  let eNotes = $state("");
  let eDue = $state("");
  let eDueTime = $state("");
  let ePriority = $state<TaskPriority>("medium");
  let eRecur = $state<TaskRecur>("none");
  let eTagsStr = $state("");

  const PRIORITY_ORDER: Record<TaskPriority, number> = { high: 0, medium: 1, low: 2 };
  const PRIORITY_LABEL: Record<TaskPriority, string> = { high: "High", medium: "Med", low: "Low" };
  const PRIORITY_COLOR: Record<TaskPriority, string> = {
    high: "var(--danger, #d33)",
    medium: "var(--muted)",
    low: "var(--muted)",
  };
  const RECUR_LABEL: Record<TaskRecur, string> = { none: "", daily: "Repeats daily", weekly: "Repeats weekly" };

  function strToTags(s: string): string[] {
    return s.split(",").map((t) => t.trim()).filter(Boolean);
  }

  function localDate(d: Date): string {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }
  // Human-readable due label, e.g. "Fri, Jun 26, 2026 · 1:00 PM" (was the raw "2026-06-2613:00").
  function dueLabel(t: Task): string {
    if (!t.due_date) return "";
    const d = new Date(`${t.due_date}T${t.due_time || "00:00"}`);
    if (Number.isNaN(d.getTime())) return t.due_time ? `${t.due_date} ${t.due_time}` : t.due_date;
    const date = d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
    if (!t.due_time) return date;
    return `${date} · ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
  }
  const today = localDate(new Date());
  const weekEnd = localDate(new Date(Date.now() + 7 * 86400000));

  const isOverdue = (t: Task) => t.status === "open" && !!t.due_date && t.due_date < today;
  // Group open tasks by due date; within a group, sort by date, then time, then priority.
  const bySchedule = (a: Task, b: Task) => {
    const ad = a.due_date ?? "", bd = b.due_date ?? "";
    if (ad !== bd) return ad < bd ? -1 : 1;
    const at = a.due_time ?? "", bt = b.due_time ?? "";
    if (at !== bt) return at < bt ? -1 : 1;
    return PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority];
  };
  const groups = $derived.by(() => {
    const open = tasks.filter((t) => t.status === "open");
    return [
      { label: "Today & overdue", items: open.filter((t) => t.due_date && t.due_date <= today) },
      { label: "This week", items: open.filter((t) => t.due_date && t.due_date > today && t.due_date <= weekEnd) },
      { label: "Later", items: open.filter((t) => t.due_date && t.due_date > weekEnd) },
      { label: "No date", items: open.filter((t) => !t.due_date) },
      { label: "Done", items: tasks.filter((t) => t.status === "done") },
    ]
      .map((g) => ({ ...g, items: [...g.items].sort(bySchedule) }))
      .filter((g) => g.items.length > 0);
  });

  async function load() {
    try {
      tasks = (await api.listTasks()).tasks;
    } catch (err) {
      error = describeError(err);
    }
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await load();
  });

  async function add(event: Event) {
    event.preventDefault();
    if (!title.trim()) return;
    busy = true;
    error = "";
    try {
      const body: TaskInput = {
        title: title.trim(),
        notes: notes.trim(),
        due_date: due || null,
        due_time: dueTime || null,
        priority,
        recur,
        tags: strToTags(tagsStr),
      };
      await api.addTask(body);
      title = notes = due = dueTime = tagsStr = "";
      priority = "medium";
      recur = "none";
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  function startEdit(t: Task) {
    editId = t.id;
    eTitle = t.title;
    eNotes = t.notes;
    eDue = t.due_date ?? "";
    eDueTime = t.due_time ?? "";
    ePriority = t.priority;
    eRecur = t.recur;
    eTagsStr = t.tags.join(", ");
    error = "";
  }
  function cancelEdit() {
    editId = null;
  }
  async function saveEdit(t: Task) {
    if (!eTitle.trim()) return;
    busy = true;
    error = "";
    try {
      const body: TaskInput = {
        title: eTitle.trim(),
        notes: eNotes.trim(),
        due_date: eDue || null,
        due_time: eDueTime || null,
        priority: ePriority,
        recur: eRecur,
        tags: strToTags(eTagsStr),
      };
      await api.updateTask(t.id, body);
      editId = null;
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function toggle(t: Task) {
    error = "";
    try {
      await api.setTaskStatus(t.id, t.status === "open" ? "done" : "open");
      await load();
    } catch (err) {
      error = describeError(err);
    }
  }

  async function remove(id: string) {
    if (!(await confirmDialog({ title: "Delete task", body: "Delete this task?", confirmLabel: "Delete", danger: true }))) return;
    error = "";
    try {
      await api.deleteTask(id);
      tasks = tasks.filter((t) => t.id !== id);
    } catch (err) {
      error = describeError(err);
    }
  }
</script>

{#if account.status?.unlocked}
  <h1>Planner</h1>

  <div class="card">
    <form onsubmit={add} style="display:flex; flex-direction:column; gap:0.5rem">
      <input bind:value={title} placeholder="New task…" aria-label="New task" />
      <div style="display:flex; gap:0.5rem; flex-wrap:wrap; align-items:center">
        <label>Due <input type="date" bind:value={due} style="width:auto" /></label>
        <label>Time <input type="time" bind:value={dueTime} style="width:auto" /></label>
        <label>Priority
          <select bind:value={priority}>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
        </label>
        <label>Repeat
          <select bind:value={recur}>
            <option value="none">No</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
        </label>
      </div>
      <input bind:value={tagsStr} placeholder="Tags (comma-separated, optional)" />
      <textarea bind:value={notes} rows="2" placeholder="Notes (optional)"></textarea>
      <p style="margin:0"><button disabled={busy || !title.trim()} type="submit">Add</button></p>
    </form>
  </div>

  {#if tasks.length === 0}
    <div class="card"><p class="muted" style="margin:0">No tasks yet — add one above.</p></div>
  {/if}

  {#each groups as g (g.label)}
    <div class="card">
      <h2>{g.label} <span class="muted" style="font-weight:400">· {g.items.length}</span></h2>
      {#each g.items as t (t.id)}
        {#if editId === t.id}
          <div style="display:flex; flex-direction:column; gap:0.4rem; margin-top:0.5rem">
            <input bind:value={eTitle} placeholder="Title" />
            <div style="display:flex; gap:0.5rem; flex-wrap:wrap; align-items:center">
              <label>Due <input type="date" bind:value={eDue} style="width:auto" /></label>
              <label>Time <input type="time" bind:value={eDueTime} style="width:auto" /></label>
              <label>Priority
                <select bind:value={ePriority}>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
              </label>
              <label>Repeat
                <select bind:value={eRecur}>
                  <option value="none">No</option>
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                </select>
              </label>
            </div>
            <input bind:value={eTagsStr} placeholder="Tags (comma-separated)" />
            <textarea bind:value={eNotes} rows="2" placeholder="Notes (optional)"></textarea>
            <div style="display:flex; gap:0.5rem; align-items:center">
              <span style="flex:1"></span>
              <button disabled={busy || !eTitle.trim()} onclick={() => saveEdit(t)}>Save</button>
              <button class="secondary" disabled={busy} onclick={cancelEdit}>Cancel</button>
            </div>
          </div>
        {:else}
          <div style="display:flex; gap:0.5rem; align-items:flex-start; margin-top:0.4rem">
            <input
              type="checkbox"
              style="width:auto; margin-top:0.3rem"
              checked={t.status === "done"}
              onchange={() => toggle(t)}
            />
            <span style="flex:1; {t.status === 'done' ? 'opacity:0.55; text-decoration:line-through' : ''}">
              <span>{t.title}</span>
              {#if t.priority !== "medium"}<span style="font-size:0.75em; font-weight:600; color:{PRIORITY_COLOR[t.priority]}"> · {PRIORITY_LABEL[t.priority]}</span>{/if}
              {#if t.recur !== "none"}<span class="muted" style="font-size:0.8em"> · {RECUR_LABEL[t.recur]}</span>{/if}
              {#if t.notes}<div class="muted" style="font-size:0.9em; white-space:pre-wrap">{t.notes}</div>{/if}
              {#if t.tags.length}<div style="font-size:0.8em; margin-top:0.15rem">{#each t.tags as tag (tag)}<span class="tag">{tag}</span>{/each}</div>{/if}
            </span>
            {#if t.due_date}<span style="white-space:nowrap; {isOverdue(t) ? 'color:var(--danger); font-weight:600' : 'color:var(--muted)'}">{dueLabel(t)}{#if isOverdue(t)} · overdue{/if}</span>{/if}
            <button class="secondary" title="Edit" onclick={() => startEdit(t)}>Edit</button>
            <button class="del" title="Delete" aria-label="Delete task" onclick={() => remove(t.id)}>✕</button>
          </div>
        {/if}
      {/each}
    </div>
  {/each}

  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <p class="muted">Loading&hellip;</p>
{/if}

<style>
  .tag {
    display: inline-block;
    background: var(--field, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 999px;
    padding: 0.05rem 0.5rem;
    margin: 0 0.25rem 0.25rem 0;
    color: var(--muted);
  }
</style>
