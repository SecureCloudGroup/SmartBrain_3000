<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "$lib/api";
  import { describeError } from "$lib/errors";
  import Chip from "$lib/components/Chip.svelte";
  import Spinner from "$lib/components/Spinner.svelte";

  // Key names mirror the backend (search.SECRET_*): stored encrypted like provider keys.
  const KEYED = [
    { secret: "websearch:brave:api_key", name: "brave", label: "Brave Search", hint: "api.search.brave.com — free tier available" },
    { secret: "websearch:tavily:api_key", name: "tavily", label: "Tavily", hint: "tavily.com — search API built for assistants" },
  ];
  const ENGINE_LABELS: Record<string, string> = {
    auto: "Automatic (first configured, DuckDuckGo last)",
    searxng: "SearXNG (self-hosted)",
    brave: "Brave Search",
    tavily: "Tavily",
    ddg: "DuckDuckGo (no key)",
  };

  let loaded = $state(false);
  let engine = $state("auto");
  let engines = $state<string[]>([]);
  let configured = $state<string[]>([]);
  let searxngUrl = $state("");
  let setKeys = $state<string[]>([]);
  let inputs = $state<Record<string, string>>({});
  let busy = $state("");
  let error = $state("");
  let notice = $state("");

  async function load() {
    try {
      const cfg = await api.getWebSearch();
      engine = cfg.engine;
      engines = cfg.engines;
      configured = cfg.configured;
      searxngUrl = cfg.searxng_url;
      setKeys = (await api.listSecrets()).keys;
    } catch (err) {
      error = describeError(err);
    } finally {
      loaded = true;
    }
  }
  onMount(load);

  const isSet = (secret: string) => setKeys.includes(secret);

  async function saveConfig() {
    busy = "config";
    error = "";
    notice = "";
    try {
      await api.putWebSearch({ engine, searxng_url: searxngUrl.trim() });
      notice = "Web search settings saved.";
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function saveKey(secret: string, label: string) {
    const value = inputs[secret]?.trim();
    if (!value) {
      error = "Enter an API key first.";
      return;
    }
    busy = secret;
    error = "";
    notice = "";
    try {
      await api.putSecret(secret, value);
      inputs[secret] = "";
      notice = `${label} key saved.`;
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function removeKey(secret: string, label: string) {
    busy = secret;
    error = "";
    notice = "";
    try {
      await api.deleteSecret(secret);
      notice = `${label} key removed.`;
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }
</script>

<h1>Web search</h1>
<p class="muted">
  Where "search the web" looks things up. DuckDuckGo works with no setup; add a
  <strong>SearXNG</strong> instance or a <strong>Brave</strong>/<strong>Tavily</strong> key for
  sturdier results — the chain falls back automatically if a provider is down. Keys are stored
  encrypted on your device, like cloud-provider keys.
</p>

{#if !loaded}
  <Spinner block />
{:else}
  <div class="card">
    <h2 class="row">
      <span>Provider</span>
      {#each configured as name (name)}
        <Chip kind={name === "ddg" ? "" : "ok"}>{name}</Chip>
      {/each}
    </h2>
    <label for="ws-engine">Search with</label>
    <select id="ws-engine" bind:value={engine}>
      {#each engines as e (e)}
        <option value={e}>{ENGINE_LABELS[e] ?? e}</option>
      {/each}
    </select>
    <label for="ws-searxng">SearXNG URL (optional)</label>
    <input
      id="ws-searxng"
      type="url"
      bind:value={searxngUrl}
      placeholder="https://searx.example.com"
      autocomplete="off"
    />
    <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">
      A SearXNG instance you run or trust; its JSON API must be enabled. Leave blank if unused.
    </p>
    <p style="margin-top:0.75rem">
      <button disabled={busy === "config"} onclick={saveConfig}>
        {busy === "config" ? "Saving…" : "Save"}
      </button>
    </p>
  </div>

  {#each KEYED as k (k.secret)}
    <div class="card">
      <h2>
        {k.label}
        <span class="muted" style="font-weight:400">· {isSet(k.secret) ? "configured" : "not set"}</span>
      </h2>
      <label for={`ws-${k.name}`}>API key</label>
      <input
        id={`ws-${k.name}`}
        type="password"
        autocomplete="off"
        placeholder={isSet(k.secret) ? "•••••••• (enter to replace)" : "Enter key"}
        bind:value={inputs[k.secret]}
      />
      <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">{k.hint}</p>
      <p style="margin-top:0.75rem; display:flex; gap:0.5rem">
        <button disabled={busy === k.secret || !inputs[k.secret]} onclick={() => saveKey(k.secret, k.label)}>
          {busy === k.secret ? "Saving…" : "Save"}
        </button>
        {#if isSet(k.secret)}
          <button class="secondary" disabled={busy === k.secret} onclick={() => removeKey(k.secret, k.label)}>
            Remove
          </button>
        {/if}
      </p>
    </div>
  {/each}
{/if}

{#if notice}<p class="notice">{notice}</p>{/if}
{#if error}<p class="error" role="alert">{error}</p>{/if}
