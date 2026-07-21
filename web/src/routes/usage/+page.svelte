<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type UsageRow } from "$lib/api";
  import { describeError } from "$lib/errors";
  import { remote } from "$lib/remote/connection.svelte";
  import Spinner from "$lib/components/Spinner.svelte";

  let rows = $state<UsageRow[]>([]);
  let total = $state(0);
  let error = $state("");
  let loaded = $state(false);
  let range = $state("today"); // today | 5 | 10 | 30 | custom
  let customFrom = $state("");
  let customTo = $state("");
  let needDates = $state(false);

  // created_at is stored UTC; format a local Date as the matching UTC string.
  const toUtc = (d: Date) => d.toISOString().slice(0, 19).replace("T", " ");

  function dayStart(daysAgo: number): Date {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - daysAgo);
    return d;
  }

  // Local midnight of a YYYY-MM-DD date; next=true gives the start of the
  // following day (an exclusive upper bound). Empty input -> null.
  function localMidnight(ymd: string, next = false): Date | null {
    if (!ymd) return null;
    const [y, m, d] = ymd.split("-").map(Number);
    return new Date(y, m - 1, d + (next ? 1 : 0), 0, 0, 0, 0); // JS normalizes month/day rollover
  }

  // Calendar-aligned bounds (local), sent as UTC strings the server filters on.
  // since is inclusive; until is exclusive (start of the day AFTER the range), so
  // rows in the final sub-second of a day are never dropped.
  function bounds(): { since?: string; until?: string } {
    if (range === "custom") {
      const s = localMidnight(customFrom);
      const e = localMidnight(customTo, true);
      return { since: s ? toUtc(s) : undefined, until: e ? toUtc(e) : undefined };
    }
    const daysAgo = range === "today" ? 0 : Number(range) - 1; // N calendar days, incl. today
    return { since: toUtc(dayStart(daysAgo)), until: toUtc(dayStart(-1)) }; // until = tomorrow midnight
  }

  async function load() {
    if (range === "custom" && (!customFrom || !customTo)) {
      needDates = true;
      return; // wait for both dates before querying
    }
    needDates = false;
    error = "";
    try {
      const u = await api.getUsage(bounds());
      rows = u.usage;
      total = u.total_cost;
    } catch (err) {
      error = describeError(err);
    } finally {
      loaded = true;
    }
  }

  onMount(async () => {
    if (remote.status !== "idle") return goto("/chat"); // usage analytics is a Desktop (review-at-the-desk) page
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await load();
  });

  // Sub-cent costs need more precision; whole dollars don't.
  const money = (n: number) => "$" + n.toFixed(n > 0 && n < 0.01 ? 4 : 2);
  const fmt = (n: number) => n.toLocaleString();
</script>

{#if account.status?.unlocked}
  <h1>Usage &amp; cost</h1>
  <p class="muted">
    Estimated spend per model, computed from your providers' live pricing. Local models (Ollama,
    MLX) run on your hardware and cost nothing.
  </p>

  <div class="range">
    <label for="range">Range</label>
    <select id="range" bind:value={range} onchange={load}>
      <option value="today">Today</option>
      <option value="5">Last 5 days</option>
      <option value="10">Last 10 days</option>
      <option value="30">Last 30 days</option>
      <option value="custom">Custom…</option>
    </select>
    {#if range === "custom"}
      <input type="date" bind:value={customFrom} onchange={load} aria-label="From date" />
      <span class="muted">to</span>
      <input type="date" bind:value={customTo} onchange={load} aria-label="To date" />
    {/if}
  </div>

  {#if needDates}
    <p class="muted">Pick a start and end date.</p>
  {:else if rows.length === 0}
    {#if loaded}
      <p class="muted">No usage in this range. Usage appears here after you <a href="/chat">chat with a model</a>.</p>
    {:else}
      <Spinner block />
    {/if}
  {:else}
    <div class="card">
      <p class="total">Total: <strong>{money(total)}</strong></p>
      <div class="table-scroll">
      <table class="usage">
        <thead>
          <tr><th>Model</th><th>Calls</th><th>Prompt</th><th>Completion</th><th>Cost</th></tr>
        </thead>
        <tbody>
          {#each rows as r (r.model)}
            <tr>
              <td>{r.model}</td>
              <td>{fmt(r.calls)}</td>
              <td>{fmt(r.prompt_tokens)}</td>
              <td>{fmt(r.completion_tokens)}</td>
              <td>{r.local ? "free" : money(r.cost)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      </div>
    </div>
  {/if}
  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <Spinner block />
{/if}

<style>
  .range {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin: 0.5rem 0 1rem;
  }
  .range label {
    margin: 0;
  }
  .range select,
  .range input {
    width: auto;
  }
  .table-scroll {
    overflow-x: auto; /* scroll instead of shredding columns on small screens */
  }
  table.usage {
    width: 100%;
    min-width: 30rem;
    border-collapse: collapse;
    margin-top: 0.5rem;
  }
  table.usage th,
  table.usage td {
    text-align: right;
    padding: 0.4rem 0.5rem;
    border-bottom: 1px solid var(--border);
  }
  table.usage th:first-child,
  table.usage td:first-child {
    text-align: left;
    word-break: break-word;
  }
  table.usage th {
    color: var(--muted);
    font-weight: 600;
    font-size: 0.85rem;
  }
  .total {
    margin: 0 0 0.5rem;
  }
</style>
