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

  async function load() {
    error = "";
    try {
      const [m, r] = await Promise.all([api.listModels(), api.getRoutes()]);
      models = m.models;
      routes = r.routes;
      labels = r.labels;
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
      {#if cap === "embedding"}
        <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">
          Used for semantic (Meaning) search. Changing it only affects new items — run
          <a href="/knowledge">Knowledge → Reindex</a> so existing documents stay searchable.
        </p>
      {/if}
    {/each}
    <p style="margin-top:1rem">
      <button disabled={busy} onclick={save}>{busy ? "Saving…" : "Save routing"}</button>
    </p>
  </div>
{/if}

{#if notice}<p class="muted">{notice}</p>{/if}
{#if error}<p class="error">{error}</p>{/if}
