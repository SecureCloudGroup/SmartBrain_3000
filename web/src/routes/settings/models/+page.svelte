<script lang="ts">
  import { onMount } from "svelte";
  import { api, type LocalModels } from "$lib/api";
  import { describeError } from "$lib/errors";
  import Chip from "$lib/components/Chip.svelte";

  // Local model servers (Ollama/MLX) run on this machine. The user just gives a port; we
  // compose a loopback URL and the BACKEND translates it to whatever its runtime needs
  // (host.docker.internal in a container, 127.0.0.1 natively — gateway.localize_local_url).
  // Values saved by older builds used the docker host; both spell "this machine".
  // Advanced users can override with a full URL (non-standard host).
  const HOST = "127.0.0.1";
  const THIS_MACHINE_HOSTS = ["127.0.0.1", "localhost", "host.docker.internal"];
  const DEFAULT_PORT = { ollama: 11434, mlx: 8888, mlxe: 8899 } as const;
  // Genuinely-good local defaults we suggest per backend (docs/02-models.md): Qwen2.5-7B for
  // chat, plus the embedding model semantic search needs. Surfaced in the pull/serve hints below.
  const RECOMMENDED = {
    ollama: "qwen2.5:7b-instruct",
    mlx: "mlx-community/Qwen2.5-7B-Instruct-4bit",
    embed: "nomic-embed-text:v1.5",
  } as const;

  let models = $state<LocalModels | null>(null);
  let ollamaPort = $state(String(DEFAULT_PORT.ollama));
  let mlxPort = $state(String(DEFAULT_PORT.mlx));
  let ollamaAdv = $state(""); // full-URL override (advanced)
  let mlxAdv = $state("");
  let mlxePort = $state(String(DEFAULT_PORT.mlxe));
  let mlxeAdv = $state("");
  let showOllamaAdv = $state(false);
  let showMlxAdv = $state(false);
  let showMlxeAdv = $state(false);
  let mlxKey = $state("");
  let mlxeKey = $state("");
  let busy = $state("");
  let error = $state("");
  let notice = $state("");
  const NOT_LIVE = "Saved — but not live in the gateway yet (it'll sync once the model server is reachable).";

  function validPort(p: string): boolean {
    const n = Number(p);
    return Number.isInteger(n) && n >= 1 && n <= 65535;
  }
  // Build the URL we save: the override if present, else this-machine:port.
  function urlFor(port: string, adv: string, useAdv: boolean): string {
    return useAdv && adv.trim() ? adv.trim() : `http://${HOST}:${port}`;
  }
  // Map a saved URL back to the UI: any this-machine host -> port field; anything else -> Advanced.
  function hydrate(url: string, fallback: number): { port: string; adv: string; useAdv: boolean } {
    if (!url) return { port: String(fallback), adv: "", useAdv: false };
    try {
      const u = new URL(url);
      if (THIS_MACHINE_HOSTS.includes(u.hostname))
        return { port: u.port || String(fallback), adv: "", useAdv: false };
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
      const me = hydrate(models.mlxe.url, DEFAULT_PORT.mlxe);
      mlxePort = me.port;
      mlxeAdv = me.adv;
      showMlxeAdv = me.useAdv;
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

  async function saveMlxe() {
    busy = "mlxe";
    error = "";
    notice = "";
    try {
      const r = await api.putMlxe(urlFor(mlxePort, mlxeAdv, showMlxeAdv), mlxeKey);
      mlxeKey = "";
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
  const mlxeInvalid = $derived(!showMlxeAdv && !validPort(mlxePort));
</script>

<h1>Local models <span class="muted" style="font-weight:400; font-size:0.9rem">· optional</span></h1>
<p class="muted">
  Local models keep your prompts fully on your machine. Run <strong>Ollama</strong> (any OS) or
  <strong>MLX</strong> (Apple Silicon), then tell SmartBrain which port it&rsquo;s listening on. Most
  people skip this and just add a cloud key under <a href="/settings/providers">Cloud providers</a>.
</p>

<!-- Tiered onboarding suggestion: meet the user where they are. Only shown before any local
     backend is connected — priority Ollama → MLX → none. If both are running we recommend
     Ollama; the non-primary backend can still be connected from its card below. -->
{#if models && !models.ollama.configured && !models.mlx.configured}
  {#if models.ollama.detected}
    <p style="margin:0 0 1rem; padding:0.6rem 0.85rem; border:1px solid var(--accent); border-radius:var(--r-1)">
      <strong>Recommended: Ollama</strong> is running on this machine — the simplest way to stay fully local.
      Connect it below, then pull a good model:
      <code>ollama pull {RECOMMENDED.ollama}</code> and (for semantic search) <code>ollama pull {RECOMMENDED.embed}</code>.
    </p>
  {:else if models.mlx.detected}
    <p style="margin:0 0 1rem; padding:0.6rem 0.85rem; border:1px solid var(--accent); border-radius:var(--r-1)">
      <strong>Recommended: MLX</strong> is running on this machine. Connect it below, then serve a good model:
      <code>mlx_lm.server --model {RECOMMENDED.mlx}</code>.
    </p>
  {:else}
    <p class="muted" style="margin:0 0 1rem; padding:0.6rem 0.85rem; border:1px solid var(--border); border-radius:var(--r-1)">
      <strong>No local model server found.</strong> To run models on your machine, set up
      <strong>Ollama</strong> (any OS) or <strong>MLX</strong> (Apple Silicon), then connect it below.
      New to local models? <a href="/help#models">Learn more</a>.
    </p>
  {/if}
{/if}

<div class="card">
  <h2 class="row">
    <span>Ollama</span>
    {#if models}
      {@const ok = models.ollama.configured && models.ollama.reachable}
      <Chip kind={!models.ollama.configured ? "" : ok ? "ok" : "danger"}>
        {!models.ollama.configured ? "off" : ok ? "connected" : "unreachable"}
      </Chip>
    {/if}
  </h2>
  {#if models && !models.ollama.configured && models.ollama.detected}
    <p style="margin:0 0 0.6rem; padding:0.5rem 0.75rem; border:1px solid var(--ok); border-radius:var(--r-1); color:var(--ok)">
      ✓ Found Ollama running on this machine.
      <button class="link" disabled={busy === "ollama"} onclick={() => run("ollama", () => api.putOllama(models!.ollama.default_url))}>Connect it</button>
    </p>
  {/if}
  <label for="ollama-port">Port</label>
  <input id="ollama-port" type="number" min="1" max="65535" bind:value={ollamaPort} disabled={showOllamaAdv} autocomplete="off" />
  <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">
    For Ollama running <strong>on this machine</strong> — default port 11434; leave it unless you
    changed it. Ollama on <strong>another computer</strong>? Use &ldquo;Server on another
    machine&rdquo; below.
  </p>
  {#if ollamaInvalid}<p class="error" style="font-size:0.85rem; margin:0.25rem 0 0">Enter a port between 1 and 65535.</p>{/if}
  {#if models?.ollama.models.length}
    <p class="muted" style="margin-top:0.5rem">Models: {models.ollama.models.join(", ")}</p>
  {/if}
  {#if models?.ollama.configured && !models.ollama.reachable}
    <p class="muted" style="margin-top:0.5rem">
      Can&rsquo;t reach Ollama. Install + start it (<a href="https://ollama.com/download" target="_blank" rel="noreferrer">ollama.com/download</a>),
      then pull a model: <code>ollama pull {RECOMMENDED.ollama}</code> and (for semantic search) <code>ollama pull {RECOMMENDED.embed}</code>.
    </p>
  {/if}
  {#if models && !models.ollama.configured && !models.ollama.detected}
    <p class="muted" style="margin-top:0.5rem">
      New to local models? Install + start Ollama (<a href="https://ollama.com/download" target="_blank" rel="noreferrer">ollama.com/download</a>),
      then pull a model: <code>ollama pull {RECOMMENDED.ollama}</code> and (for semantic search) <code>ollama pull {RECOMMENDED.embed}</code>.
    </p>
  {/if}
  <details bind:open={showOllamaAdv} style="margin-top:0.5rem">
    <summary class="muted" style="font-size:0.85rem; cursor:pointer">Server on another machine (or a custom URL)</summary>
    <input
      type="url"
      bind:value={ollamaAdv}
      placeholder={`http://192.168.1.50:${DEFAULT_PORT.ollama}`}
      autocomplete="off"
      style="margin-top:0.4rem"
    />
    <p class="muted" style="font-size:0.8rem; margin:0.25rem 0 0">
      Enter the other computer&rsquo;s address, e.g. <code>http://192.168.1.50:11434</code>. On that
      machine, Ollama must listen beyond localhost: start it with <code>OLLAMA_HOST=0.0.0.0</code>
      (and allow it through its firewall). See the
      <a href="/help#connect-a-model" target="_blank">models guide</a>.
    </p>
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
      <Chip kind={!models.mlx.configured ? "" : ok ? "ok" : "danger"}>
        {!models.mlx.configured ? "off" : ok ? "connected" : "unreachable"}
      </Chip>
    {/if}
  </h2>
  {#if models && !models.mlx.configured && models.mlx.detected}
    <p style="margin:0 0 0.6rem; padding:0.5rem 0.75rem; border:1px solid var(--ok); border-radius:var(--r-1); color:var(--ok)">
      ✓ Found an MLX server running on this machine.
      <button class="link" disabled={busy === "mlx"} onclick={() => run("mlx", () => api.putMlx(models!.mlx.default_url, ""))}>Connect it</button>
    </p>
  {/if}
  <label for="mlx-port">Port</label>
  <input id="mlx-port" type="number" min="1" max="65535" bind:value={mlxPort} disabled={showMlxAdv} autocomplete="off" />
  <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">
    For an MLX server <strong>on this machine</strong> — default port 8888. Server on
    <strong>another computer</strong> (e.g. a Mac running oMLX)? Use &ldquo;Server on another
    machine&rdquo; below.
  </p>
  {#if mlxInvalid}<p class="error" style="font-size:0.85rem; margin:0.25rem 0 0">Enter a port between 1 and 65535.</p>{/if}
  <label for="mlx-key" style="margin-top:0.5rem">API key <span class="muted" style="font-weight:400">(optional)</span></label>
  <input id="mlx-key" type="password" bind:value={mlxKey} autocomplete="off" placeholder="Leave blank if your server has none" />
  {#if models?.mlx.models.length}
    <p class="muted" style="margin-top:0.5rem">Models: {models.mlx.models.join(", ")}</p>
  {/if}
  {#if models?.mlx.configured && !models.mlx.reachable}
    <p class="muted" style="margin-top:0.5rem">
      Can&rsquo;t reach MLX. On this machine: start the server bound to <code>0.0.0.0</code>, e.g.
      <code>mlx_lm.server --host 0.0.0.0 --port {mlxPort}</code>. On another machine: enable its
      network access / LAN setting (oMLX has a toggle), allow it through the firewall, and if the
      server requires an API key, enter it above — a missing key looks exactly like unreachable.
    </p>
  {/if}
  <details bind:open={showMlxAdv} style="margin-top:0.5rem">
    <summary class="muted" style="font-size:0.85rem; cursor:pointer">Server on another machine (or a custom URL)</summary>
    <input
      type="url"
      bind:value={mlxAdv}
      placeholder={`http://192.168.1.50:${DEFAULT_PORT.mlx}`}
      autocomplete="off"
      style="margin-top:0.4rem"
    />
    <p class="muted" style="font-size:0.8rem; margin:0.25rem 0 0">
      Enter the other computer&rsquo;s address, e.g. <code>http://192.168.1.50:8888</code>. On that
      machine, enable the server&rsquo;s network/LAN access (oMLX: allow network access, which also
      issues the API key to paste above) and allow it through the firewall. See the
      <a href="/help#connect-a-model" target="_blank">models guide</a>.
    </p>
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

<div class="card">
  <h2 class="row">
    <span>MLX embeddings</span>
    {#if models}
      {@const ok = models.mlxe.configured && models.mlxe.reachable}
      <Chip kind={!models.mlxe.configured ? "" : ok ? "ok" : "danger"}>
        {!models.mlxe.configured ? "off" : ok ? "connected" : "unreachable"}
      </Chip>
    {/if}
  </h2>
  <p class="muted" style="margin:0 0 0.5rem; font-size:0.9rem">
    <strong>Most setups don&rsquo;t need this.</strong> For semantic search, the simplest path is to
    load an <em>encoder</em> embedding model (e.g. <code>modernbert-embed</code>) on your regular
    MLX chat server and route Model routing → Embedding to it — one server runs everything. This
    card is only for <em>decoder</em>-class embedders (Qwen3-Embedding), which chat servers refuse:
    a tiny dedicated server serves them instead. Its installer ships in the source repo
    (<code>tools/mlx_embed_server/install.sh</code> — requires a <code>git clone</code> of
    SmartBrain_3000 on the server machine; it is not part of the desktop install). Details in the
    <a href="/help#connect-a-model" target="_blank">models guide</a>.
  </p>
  {#if models && !models.mlxe.configured && models.mlxe.detected}
    <p style="margin:0 0 0.6rem; padding:0.5rem 0.75rem; border:1px solid var(--ok); border-radius:var(--r-1); color:var(--ok)">
      ✓ Found the embeddings server running on this machine.
      <button class="link" disabled={busy === "mlxe"} onclick={() => run("mlxe", () => api.putMlxe(models!.mlxe.default_url, ""))}>Connect it</button>
    </p>
  {/if}
  <label for="mlxe-port">Port</label>
  <input id="mlxe-port" type="number" min="1" max="65535" bind:value={mlxePort} disabled={showMlxeAdv} autocomplete="off" />
  <p class="muted" style="font-size:0.85rem; margin:0.25rem 0 0">
    For the embeddings server <strong>on this machine</strong> — the install script&rsquo;s default
    is 8899. On <strong>another computer</strong>? Use &ldquo;Server on another machine&rdquo; below.
  </p>
  {#if mlxeInvalid}<p class="error" style="font-size:0.85rem; margin:0.25rem 0 0">Enter a port between 1 and 65535.</p>{/if}
  <label for="mlxe-key" style="margin-top:0.5rem">API key <span class="muted" style="font-weight:400">(optional)</span></label>
  <input id="mlxe-key" type="password" bind:value={mlxeKey} autocomplete="off" placeholder="Leave blank if your server has none" />
  {#if models?.mlxe.models.length}
    <p class="muted" style="margin-top:0.5rem">Models: {models.mlxe.models.join(", ")}</p>
  {/if}
  <details bind:open={showMlxeAdv} style="margin-top:0.5rem">
    <summary class="muted" style="font-size:0.85rem; cursor:pointer">Server on another machine (or a custom URL)</summary>
    <input
      type="url"
      bind:value={mlxeAdv}
      placeholder={`http://192.168.1.50:${DEFAULT_PORT.mlxe}`}
      autocomplete="off"
      style="margin-top:0.4rem"
    />
    <p class="muted" style="font-size:0.8rem; margin:0.25rem 0 0">
      Enter the other computer&rsquo;s address, e.g. <code>http://192.168.1.50:8899</code> — the
      embeddings server must be reachable from this machine (bound beyond localhost, firewall open).
    </p>
  </details>
  <p style="margin-top:0.75rem; display:flex; gap:0.5rem">
    <button disabled={busy === "mlxe" || mlxeInvalid || (showMlxeAdv && !mlxeAdv.trim())} onclick={saveMlxe}>
      {busy === "mlxe" ? "Saving…" : "Save & connect"}
    </button>
    {#if models?.mlxe.configured}
      <button class="secondary" disabled={busy === "mlxe"} onclick={() => run("mlxe", () => api.deleteLocalModel("mlxe"))}>Remove</button>
    {/if}
  </p>
</div>

{#if notice}<p class="muted">{notice}</p>{/if}
{#if error}<p class="error">{error}</p>{/if}
