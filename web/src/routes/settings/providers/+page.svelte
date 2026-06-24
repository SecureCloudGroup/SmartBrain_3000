<script lang="ts">
  import { onMount } from "svelte";
  import { api, type DiscoveredModel } from "$lib/api";
  import { describeError } from "$lib/errors";

  const PROVIDERS = [
    { logical: "openai", label: "OpenAI" },
    { logical: "anthropic", label: "Anthropic" },
    { logical: "google", label: "Google (Gemini)" },
  ];
  const keyName = (logical: string) => `provider:${logical}:api_key`;
  // The gateway names a provider differently from our logical key (Google → Gemini).
  const GATEWAY_NAME: Record<string, string> = { openai: "openai", anthropic: "anthropic", google: "gemini" };

  let setKeys = $state<string[]>([]);
  let models = $state<DiscoveredModel[]>([]);
  let inputs = $state<Record<string, string>>({});
  let busy = $state("");
  let error = $state("");
  let notice = $state("");

  async function load() {
    try {
      setKeys = (await api.listSecrets()).keys;
      try {
        models = (await api.listModels()).models;
      } catch {
        models = []; // gateway not ready — show keys without the catalog
      }
    } catch (err) {
      error = describeError(err);
    }
  }
  onMount(load);

  const isSet = (logical: string) => setKeys.includes(keyName(logical));
  const label = (logical: string) => PROVIDERS.find((p) => p.logical === logical)?.label ?? logical;
  // Chat-capable models the gateway currently exposes for this provider.
  const chatModelsFor = (logical: string) =>
    models.filter((m) => m.provider === GATEWAY_NAME[logical] && m.chat);

  function focusKeyInput(logical: string) {
    const el = document.getElementById(`k-${logical}`);
    if (el instanceof HTMLInputElement) el.focus();
  }

  async function save(logical: string) {
    const value = inputs[logical]?.trim();
    if (!value) {
      error = "Enter an API key first.";
      focusKeyInput(logical);
      return;
    }
    busy = logical;
    error = "";
    notice = "";
    try {
      const r = await api.putSecret(keyName(logical), value);
      inputs[logical] = "";
      if (r.gateway_synced === false) {
        notice = `${label(logical)} key saved — but not live in the gateway yet (it'll sync when the gateway is reachable).`;
        await load();
        return;
      }
      notice = `${label(logical)} key saved — discovering models…`;
      await load();
      const found = chatModelsFor(logical).length;
      notice = found
        ? `${label(logical)} connected — ${found} chat models available.`
        : `${label(logical)} key saved. Models may take a moment to appear — reload if needed.`;
    } catch (err) {
      error = describeError(err);
      focusKeyInput(logical);
    } finally {
      busy = "";
    }
  }

  async function remove(logical: string) {
    busy = logical;
    error = "";
    notice = "";
    try {
      await api.deleteSecret(keyName(logical));
      notice = `${label(logical)} key removed.`;
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }
</script>

<h1>Cloud providers</h1>
<p class="muted">
  Stored encrypted on your device and sent to a provider only when you use one of its cloud
  models. Saved values are never shown back — you can only replace or remove them.
</p>

{#each PROVIDERS as p (p.logical)}
  <div class="card">
    <h2>
      {p.label}
      <span class="muted" style="font-weight:400">· {isSet(p.logical) ? "configured" : "not set"}</span>
    </h2>
    <label for={`k-${p.logical}`}>API key</label>
    <input
      id={`k-${p.logical}`}
      type="password"
      autocomplete="off"
      placeholder={isSet(p.logical) ? "•••••••• (enter to replace)" : "Enter key"}
      bind:value={inputs[p.logical]}
    />
    <p style="margin-top:0.75rem; display:flex; gap:0.5rem">
      <button disabled={busy === p.logical || !inputs[p.logical]} onclick={() => save(p.logical)}>
        {busy === p.logical ? "Saving…" : "Save"}
      </button>
      {#if isSet(p.logical)}
        <button class="secondary" disabled={busy === p.logical} onclick={() => remove(p.logical)}>
          Remove
        </button>
      {/if}
    </p>
    {#if isSet(p.logical) && chatModelsFor(p.logical).length}
      <p class="muted" style="margin-top:0.5rem">
        {chatModelsFor(p.logical).length} chat models available (e.g.
        {chatModelsFor(p.logical).slice(0, 4).map((m) => m.id.split("/")[1]).join(", ")}…). Choose which
        serves each task under <a href="/settings/router">Model routing</a>, or pick one per chat.
      </p>
    {/if}
  </div>
{/each}

{#if notice}<p class="muted">{notice}</p>{/if}
{#if error}<p class="error" role="alert">{error}</p>{/if}
