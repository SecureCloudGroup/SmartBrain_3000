<script lang="ts">
  import { onMount } from "svelte";
  import { api, type LocalModels } from "$lib/api";
  import { describeError } from "$lib/errors";

  // Local model servers (Ollama/MLX) run on the host; the in-container gateway reaches
  // them at host.docker.internal. We hide that plumbing: the user just gives a port, and
  // we compose the URL. Advanced users can override with a full URL (non-standard host).
  const HOST = "host.docker.internal";
  const DEFAULT_PORT = { ollama: 11434, mlx: 8888 } as const;

  let models = $state<LocalModels | null>(null);
  let ollamaPort = $state(String(DEFAULT_PORT.ollama));
  let mlxPort = $state(String(DEFAULT_PORT.mlx));
  let ollamaAdv = $state(""); // full-URL override (advanced)
  let mlxAdv = $state("");
  let showOllamaAdv = $state(false);
  let showMlxAdv = $state(false);
  let mlxKey = $state("");
  let busy = $state("");
  let error = $state("");
  let notice = $state("");
  const NOT_LIVE = "Saved — but not live in the gateway yet (it'll sync once the model server is reachable).";

  function validPort(p: string): boolean {
    const n = Number(p);
    return Number.isInteger(n) && n >= 1 && n <= 65535;
  }
  // Build the URL the gateway uses: the override if present, else host.docker.internal:port.
  function urlFor(port: string, adv: string, useAdv: boolean): string {
    return useAdv && adv.trim() ? adv.trim() : `http://${HOST}:${port}`;
  }
  // Map a saved URL back to the UI: standard host -> port field; anything else -> Advanced.
  function hydrate(url: string, fallback: number): { port: string; adv: string; useAdv: boolean } {
    if (!url) return { port: String(fallback), adv: "", useAdv: false };
    try {
      const u = new URL(url);
      if (u.hostname === HOST) return { port: u.port || String(fallback), adv: "", useAdv: false };
      return { port: String(fallback), adv: url, useAdv: true };
    } catch {
      return { port: String(fallback), adv: "", useAdv: false };
    }
  }

  async function load() {
    try {
      models = await api.localModels();
      const o = hydrate(models.ollama.url, DEFAULT_PORT.ollama);
      ollamaPort = o.port;
      ollamaAdv = o.adv;
      showOllamaAdv = o.useAdv;
      const m = hydrate(models.mlx.url, DEFAULT_PORT.mlx);
      mlxPort = m.port;
      mlxAdv = m.adv;
      showMlxAdv = m.useAdv;
    } catch (err) {
      error = describeError(err);
    }
  }
  onMount(load);

  async function run(label: string, fn: () => Promise<{ gateway_synced?: boolean }>) {
    busy = label;
    error = "";
    notice = "";
    try {
      const r = await fn();
      if (r?.gateway_synced === false) notice = NOT_LIVE;
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  // Dedicated MLX save so the API key (a secret) is cleared from client state +
  // the input after a successful save, matching the providers page hygiene.
  async function saveMlx() {
    busy = "mlx";
    error = "";
    notice = "";
    try {
      const r = await api.putMlx(urlFor(mlxPort, mlxAdv, showMlxAdv), mlxKey);
      mlxKey = "";
      if (r.gateway_synced === false) notice = NOT_LIVE;
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  const ollamaInvalid = $derived(!showOllamaAdv && !validPort(ollamaPort));
  const mlxInvalid = $derived(!showMlxAdv && !validPort(mlxPort));
</script>

<h1>Local models <span class="muted" style="font-weight:400; font-size:0.9rem">· optional</span></h1>
<p class="muted">
  Local models keep your prompts fully on your machine. Run <strong>Ollama</strong> (any OS) or
  <strong>MLX</strong> (Apple Silicon), then tell SmartBrain which port it&rsquo;s listening on. Most
  people skip this and just add a cloud key under <a href="/settings/providers">Cloud providers</a>.
</p>

<div class="card">
  <h2 class="row">
    <span>Ollama</span>
    {#if models}
      {@const ok = models.ollama.configured && models.ollama.reachable}
      {@const col = !models.ollama.configured ? "var(--muted)" : ok ? "var(--ok)" : "var(--danger)"}
      <span class="badge" style="border-color:{col}; color:{col}">
        {!models.ollama.configured ? "off" : ok ? "connected" : "unreachable"}
      </span>
    {/if}
  </h2>
  {#if models && !models.ollama.configured && models.ollama.detected}
    <p style="margin:0 0 0.6rem; padding:0.5rem 0.75rem; border:1px solid var(--ok); border-radius:6px; color:var(--ok)">
      ✓ Found Ollama running on this machine.
      <button class="link" disabled={busy === "ollama"} onclick={() => run("ollama", () => api.putOllama(models!.ollama.default_url))}>Connect it</button>
    </p>
  {/if}
  <label for="ollama-port">Port</label>
  <input id="ollama-port" type="number" min="1" max="65535" bind:value={ollamaPort} disabled={showOllamaAdv} autocomplete="off" />
  <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">The port your Ollama server listens on. Default is 11434 — leave it unless you changed it.</p>
  {#if ollamaInvalid}<p class="error" style="font-size:0.85rem; margin:0.25rem 0 0">Enter a port between 1 and 65535.</p>{/if}
  {#if models?.ollama.models.length}
    <p class="muted" style="margin-top:0.5rem">Models: {models.ollama.models.join(", ")}</p>
  {/if}
  {#if models?.ollama.configured && !models.ollama.reachable}
    <p class="muted" style="margin-top:0.5rem">
      Can&rsquo;t reach Ollama. Install + start it (<a href="https://ollama.com/download" target="_blank" rel="noreferrer">ollama.com/download</a>),
      then pull a model: <code>ollama pull llama3.1:8b</code> and (for semantic search) <code>ollama pull nomic-embed-text:v1.5</code>.
    </p>
  {/if}
  {#if models && !models.ollama.configured && !models.ollama.detected}
    <p class="muted" style="margin-top:0.5rem">
      New to local models? Install + start Ollama (<a href="https://ollama.com/download" target="_blank" rel="noreferrer">ollama.com/download</a>),
      then pull a model: <code>ollama pull llama3.1:8b</code> and (for semantic search) <code>ollama pull nomic-embed-text:v1.5</code>.
    </p>
  {/if}
  <details bind:open={showOllamaAdv} style="margin-top:0.5rem">
    <summary class="muted" style="font-size:0.85rem; cursor:pointer">Advanced: use a full URL</summary>
    <input
      type="url"
      bind:value={ollamaAdv}
      placeholder={`http://${HOST}:${DEFAULT_PORT.ollama}`}
      autocomplete="off"
      style="margin-top:0.4rem"
    />
    <p class="muted" style="font-size:0.8rem; margin:0.25rem 0 0">For a server on another host or a non-standard address.</p>
  </details>
  <p style="margin-top:0.75rem; display:flex; gap:0.5rem">
    <button
      disabled={busy === "ollama" || ollamaInvalid || (showOllamaAdv && !ollamaAdv.trim())}
      onclick={() => run("ollama", () => api.putOllama(urlFor(ollamaPort, ollamaAdv, showOllamaAdv)))}
    >
      {busy === "ollama" ? "Saving…" : "Save & connect"}
    </button>
    {#if models?.ollama.configured}
      <button class="secondary" disabled={busy === "ollama"} onclick={() => run("ollama", () => api.deleteLocalModel("ollama"))}>Remove</button>
    {/if}
  </p>
</div>

<div class="card">
  <h2 class="row">
    <span>MLX</span>
    {#if models}
      {@const ok = models.mlx.configured && models.mlx.reachable}
      {@const col = !models.mlx.configured ? "var(--muted)" : ok ? "var(--ok)" : "var(--danger)"}
      <span class="badge" style="border-color:{col}; color:{col}">
        {!models.mlx.configured ? "off" : ok ? "connected" : "unreachable"}
      </span>
    {/if}
  </h2>
  {#if models && !models.mlx.configured && models.mlx.detected}
    <p style="margin:0 0 0.6rem; padding:0.5rem 0.75rem; border:1px solid var(--ok); border-radius:6px; color:var(--ok)">
      ✓ Found an MLX server running on this machine.
      <button class="link" disabled={busy === "mlx"} onclick={() => run("mlx", () => api.putMlx(models!.mlx.default_url, ""))}>Connect it</button>
    </p>
  {/if}
  <label for="mlx-port">Port</label>
  <input id="mlx-port" type="number" min="1" max="65535" bind:value={mlxPort} disabled={showMlxAdv} autocomplete="off" />
  <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">The port your MLX server listens on. Default is 8888.</p>
  {#if mlxInvalid}<p class="error" style="font-size:0.85rem; margin:0.25rem 0 0">Enter a port between 1 and 65535.</p>{/if}
  <label for="mlx-key" style="margin-top:0.5rem">API key <span class="muted" style="font-weight:400">(optional)</span></label>
  <input id="mlx-key" type="password" bind:value={mlxKey} autocomplete="off" placeholder="Leave blank if your server has none" />
  {#if models?.mlx.models.length}
    <p class="muted" style="margin-top:0.5rem">Models: {models.mlx.models.join(", ")}</p>
  {/if}
  {#if models?.mlx.configured && !models.mlx.reachable}
    <p class="muted" style="margin-top:0.5rem">
      Can&rsquo;t reach MLX. Start your MLX server on the host bound to <code>0.0.0.0</code> (so the gateway
      can reach it) on this port — e.g. <code>mlx_lm.server --host 0.0.0.0 --port {mlxPort}</code>.
    </p>
  {/if}
  <details bind:open={showMlxAdv} style="margin-top:0.5rem">
    <summary class="muted" style="font-size:0.85rem; cursor:pointer">Advanced: use a full URL</summary>
    <input
      type="url"
      bind:value={mlxAdv}
      placeholder={`http://${HOST}:${DEFAULT_PORT.mlx}`}
      autocomplete="off"
      style="margin-top:0.4rem"
    />
    <p class="muted" style="font-size:0.8rem; margin:0.25rem 0 0">For a server on another host or a non-standard address.</p>
  </details>
  <p style="margin-top:0.75rem; display:flex; gap:0.5rem">
    <button disabled={busy === "mlx" || mlxInvalid || (showMlxAdv && !mlxAdv.trim())} onclick={saveMlx}>
      {busy === "mlx" ? "Saving…" : "Save & connect"}
    </button>
    {#if models?.mlx.configured}
      <button class="secondary" disabled={busy === "mlx"} onclick={() => run("mlx", () => api.deleteLocalModel("mlx"))}>Remove</button>
    {/if}
  </p>
</div>

{#if notice}<p class="muted">{notice}</p>{/if}
{#if error}<p class="error">{error}</p>{/if}
