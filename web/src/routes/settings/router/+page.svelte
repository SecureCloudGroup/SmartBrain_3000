<script lang="ts">
  import { onMount } from "svelte";
  import { api, type DiscoveredModel } from "$lib/api";
  import { describeError } from "$lib/errors";

  let models = $state<DiscoveredModel[]>([]);
  let routes = $state<Record<string, string>>({});
  let labels = $state<Record<string, string>>({});
  let busy = $state(false);
  let error = $state("");
  let notice = $state("");

  // Per-model context length (tokens). `ctxDefault` is the fallback when a model has no override;
  // `ctxInput` holds the editable string per model (blank = use the default).
  let ctxDefault = $state(0);
  let ctxInput = $state<Record<string, string>>({});
  let ctxBusy = $state(false);
  let ctxNotice = $state("");

  async function load() {
    error = "";
    try {
      const [m, r, c] = await Promise.all([api.listModels(), api.getRoutes(), api.getContextLengths()]);
      models = m.models;
      // "agent" has no server-side default — seed "" so its selector shows "Same as Chat"
      // (an empty value persists nothing, so the scheduler falls back to the Chat model).
      if (r.routes.agent === undefined) r.routes.agent = "";
      routes = r.routes;
      labels = r.labels;
      ctxDefault = c.default;
      ctxInput = Object.fromEntries(Object.entries(c.lengths).map(([id, n]) => [id, String(n)]));
    } catch (err) {
      error = describeError(err);
    }
  }
  onMount(load);

  // Each capability picks from the right model kind: embedding -> embed models,
  // everything else -> chat models. Grouped by provider for a two-level list.
  const capabilities = $derived(Object.keys(labels));
  const modelsFor = (cap: string) => models.filter((m) => (cap === "embedding" ? m.embed : m.chat));
  const providersFor = (cap: string) => [...new Set(modelsFor(cap).map((m) => m.provider))].sort();
  const known = (id: string) => models.some((m) => m.id === id);

  // The models whose context length affects the tool-result cap: every distinct model routed to a
  // text (non-embedding) capability. Sizing their context lets Chat read/summarize far more per step.
  const ctxModels = $derived([
    ...new Set(capabilities.filter((c) => c !== "embedding").map((c) => routes[c]).filter(Boolean)),
  ]);

  async function saveContextLengths() {
    ctxBusy = true;
    error = "";
    ctxNotice = "";
    try {
      // Send every shown model: a positive integer sets an override, blank/0 resets it to the default.
      const payload: Record<string, number> = {};
      for (const id of ctxModels) {
        const raw = (ctxInput[id] ?? "").trim();
        payload[id] = raw === "" ? 0 : Math.trunc(Number(raw)) || 0;
      }
      const res = await api.putContextLengths(payload);
      ctxInput = Object.fromEntries(Object.entries(res.lengths).map(([id, n]) => [id, String(n)]));
      ctxNotice = "Context lengths saved.";
    } catch (err) {
      error = describeError(err);
    } finally {
      ctxBusy = false;
    }
  }

  async function save() {
    busy = true;
    error = "";
    notice = "";
    try {
      const res = await api.putRoutes(routes);
      routes = res.routes;
      notice = "Routing saved.";
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }
</script>

<h1>Model routing</h1>
<p class="muted">
  Choose which model serves each task. Chat, the agent, and scheduled runs use these defaults
  unless you pick a specific model in a conversation. Models are discovered live from your
  configured providers — add keys under <a href="/settings/providers">Cloud providers</a> or local
  servers under <a href="/settings/models">Local models</a>. <a href="/help#models">Learn more</a>.
</p>

{#if models.length === 0}
  <p class="muted">No models available yet. Configure a provider key or a local model first.</p>
{:else}
  <div class="card">
    {#each capabilities as cap (cap)}
      <label for={`route-${cap}`}>{labels[cap]}</label>
      <select id={`route-${cap}`} bind:value={routes[cap]}>
        {#if cap === "agent"}<option value="">Same as Chat</option>{/if}
        {#each providersFor(cap) as p (p)}
          <optgroup label={p}>
            {#each modelsFor(cap).filter((m) => m.provider === p) as m (m.id)}
              <option value={m.id}>{m.name}{m.pricing ? "" : " · free"}</option>
            {/each}
          </optgroup>
        {/each}
        {#if routes[cap] && !known(routes[cap])}
          <option value={routes[cap]}>{routes[cap]} (unavailable)</option>
        {/if}
      </select>
      {#if cap === "agent"}
        <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">
          Runs your schedules and background tasks. Leave as &ldquo;Same as Chat&rdquo; unless you want a
          different model there. Agent tasks call tools — pick a model that reliably tool-calls (most
          instruct/chat models do; very small or embedding/coder-only models often don&rsquo;t).
        </p>
      {/if}
      {#if cap === "embedding"}
        <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">
          Used for semantic (Meaning) search. Changing it only affects new items — run
          <a href="/knowledge">Knowledge → Reindex</a> so existing documents stay searchable.
        </p>
      {/if}
    {/each}
    <p style="margin-top:1rem">
      <button disabled={busy} onclick={save}>{busy ? "Saving…" : "Save routing"}</button>
      <!-- Inline so the confirmation lands where the user is looking (was page-bottom, off-screen). -->
      {#if notice}<span class="muted" style="margin-left:0.75rem">{notice}</span>{/if}
    </p>
  </div>

  <h2 style="margin-top:2rem">Model context length</h2>
  <p class="muted">
    How many tokens each model can hold. This sizes how much a document read or tool result the model
    gets in one step — a bigger context lets Chat read and summarize far longer documents. Local (MLX)
    models are detected automatically; set a value here for any model that isn&rsquo;t, or to override a
    detected one. Leave blank to use the default ({ctxDefault.toLocaleString()} tokens).
  </p>
  <div class="card">
    {#if ctxModels.length === 0}
      <p class="muted">Pick a model above first — context length applies to the models you route to.</p>
    {:else}
      {#each ctxModels as id (id)}
        <label for={`ctx-${id}`}>{models.find((m) => m.id === id)?.name ?? id}</label>
        <input
          id={`ctx-${id}`}
          type="number"
          min="0"
          step="1024"
          placeholder={`${ctxDefault} (default)`}
          bind:value={ctxInput[id]}
        />
      {/each}
      <p style="margin-top:1rem">
        <button disabled={ctxBusy} onclick={saveContextLengths}>{ctxBusy ? "Saving…" : "Save context lengths"}</button>
        {#if ctxNotice}<span class="muted" style="margin-left:0.75rem">{ctxNotice}</span>{/if}
      </p>
    {/if}
  </div>
{/if}

{#if error}<p class="error">{error}</p>{/if}
