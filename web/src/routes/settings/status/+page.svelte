<script lang="ts">
  // Settings → Status: everything worth knowing the state of, on one calm page.
  // Field-driven (the voice model download was invisible and read as a broken
  // product): every background process the app runs gets a row, a plain-words
  // state, and — where something is in motion — a live progress bar. Polls softly
  // while open; nothing here fires probes that could block the page.
  import { onDestroy, onMount } from "svelte";
  import Chip from "$lib/components/Chip.svelte";
  import Spinner from "$lib/components/Spinner.svelte";
  import { account } from "$lib/account.svelte";
  import { api, type AppStatus } from "$lib/api";
  import { describeError } from "$lib/errors";

  let status = $state<AppStatus | null>(null);
  let error = $state("");
  let timer: ReturnType<typeof setInterval> | null = null;

  async function load() {
    try {
      status = await api.appStatus();
      error = "";
    } catch (err) {
      error = describeError(err);
    }
  }

  onMount(async () => {
    await load();
    timer = setInterval(load, 3000);
  });
  onDestroy(() => {
    if (timer) clearInterval(timer);
  });

  // Voice model row: phase -> chip kind + plain words. "ready" is quiet; motion and
  // trouble are loud.
  const voicePhase = $derived(status?.voice_local?.phase ?? "absent");
  type ChipKind = "" | "ok" | "warn" | "accent" | "danger";
  const voiceChip = $derived<{ kind: ChipKind; label: string }>(
    voicePhase === "ready" ? { kind: "ok", label: "ready" }
    : voicePhase === "error" ? { kind: "danger", label: "download failed" }
    : voicePhase === "loading" ? { kind: "accent", label: "loading engine" }
    : voicePhase === "downloading" ? { kind: "accent", label: `downloading ${status?.voice_local?.pct ?? 0}%` }
    : { kind: "", label: "waiting" },
  );

  function retryVoice() {
    void api.voiceStatus(false, true).then(load).catch(() => undefined);
  }
</script>

{#if account.status?.unlocked}
  <h1>Status</h1>
  <p class="muted">What the app is doing right now — updated live while you watch.</p>

  {#if status}
    <div class="card">
      <h2 class="row"><span>App</span><Chip kind="ok">v{status.version}</Chip></h2>
      <div class="rows">
        <div class="srow"><span>Vault</span><Chip kind={status.unlocked ? "ok" : ""}>{status.unlocked ? "unlocked" : "locked"}</Chip></div>
      </div>
    </div>

    <div class="card">
      <h2 class="row">
        <span>Voice</span>
        <Chip kind={voiceChip.kind}>{voiceChip.label}</Chip>
      </h2>
      <div class="rows">
        <div class="srow">
          <span>Built-in dictation model <span class="muted">(one-time ~236 MB download)</span></span>
          {#if voicePhase === "downloading"}
            <div class="bar" role="progressbar" aria-valuenow={status.voice_local.pct} aria-valuemin={0} aria-valuemax={100}>
              <div class="fill" style={`width:${status.voice_local.pct}%`}></div>
            </div>
          {:else if voicePhase === "error"}
            <button class="secondary" onclick={retryVoice}>Retry download</button>
          {/if}
        </div>
        {#if voicePhase === "error"}
          <p class="error" style="margin:0.25rem 0 0; font-size:0.85rem">{status.voice_local.error}</p>
        {/if}
        {#if status.voice}
          <div class="srow">
            <span>Dictation engine in use</span>
            <Chip kind="ok">{status.voice.engine === "server" ? "your audio server" : "built-in"}</Chip>
          </div>
          {#if status.voice.tts_model}
            <div class="srow"><span>Server voice (spoken replies)</span><Chip kind="ok">{status.voice.tts_model}</Chip></div>
          {/if}
        {/if}
      </div>
    </div>

    {#if status.local_models}
      <div class="card">
        <h2 class="row"><span>Model servers</span></h2>
        <div class="rows">
          <div class="srow"><span>Ollama</span><Chip kind={status.local_models.ollama_configured ? "ok" : ""}>{status.local_models.ollama_configured ? "configured" : "off"}</Chip></div>
          <div class="srow"><span>MLX</span><Chip kind={status.local_models.mlx_configured ? "ok" : ""}>{status.local_models.mlx_configured ? "configured" : "off"}</Chip></div>
          <div class="srow"><span>MLX embeddings</span><Chip kind={status.local_models.mlxe_configured ? "ok" : ""}>{status.local_models.mlxe_configured ? "configured" : "off"}</Chip></div>
        </div>
        <p class="muted" style="font-size:0.8rem; margin:0.5rem 0 0">
          Live reachability checks run on <a href="/settings/models">Local models</a>.
        </p>
      </div>
    {/if}

    <div class="grid2">
      {#if status.knowledge}
        <div class="card">
          <h2 class="row"><span>Knowledge</span></h2>
          <div class="rows">
            <div class="srow"><span>Documents</span><strong>{status.knowledge.documents}</strong></div>
            <div class="srow"><span>Embedded chunks</span><strong>{status.knowledge.embedded_chunks}</strong></div>
          </div>
        </div>
      {/if}
      {#if status.schedules}
        <div class="card">
          <h2 class="row"><span>Schedules</span></h2>
          <div class="rows">
            <div class="srow"><span>Enabled</span><strong>{status.schedules.enabled} of {status.schedules.total}</strong></div>
          </div>
        </div>
      {/if}
      {#if status.feeds}
        <div class="card">
          <h2 class="row"><span>Feeds</span>{#if status.feeds.errors}<Chip kind="danger">{status.feeds.errors} failing</Chip>{/if}</h2>
          <div class="rows">
            <div class="srow"><span>Subscriptions</span><strong>{status.feeds.count}</strong></div>
          </div>
        </div>
      {/if}
      {#if status.devices}
        <div class="card">
          <h2 class="row"><span>Remote access</span></h2>
          <div class="rows">
            <div class="srow"><span>Paired devices</span><strong>{status.devices.paired}</strong></div>
          </div>
        </div>
      {/if}
    </div>
  {:else if error}
    <p class="error">{error}</p>
  {:else}
    <Spinner block />
  {/if}
{:else}
  <Spinner block />
{/if}

<style>
  .rows {
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
    margin-top: 0.25rem;
  }
  .srow {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    font-size: 0.92rem;
  }
  .grid2 {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
    gap: 0.75rem;
  }
  .bar {
    flex: 1;
    max-width: 14rem;
    height: 8px;
    border-radius: var(--r-full);
    background: var(--accent-tint);
    overflow: hidden;
  }
  .fill {
    height: 100%;
    background: var(--accent-strong);
    border-radius: var(--r-full);
    transition: width 0.6s ease;
  }
</style>
