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
  import { api, ApiError, type AppStatus } from "$lib/api";
  import { Recorder } from "$lib/audio/recorder";
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

  // ---- Mic & Speaker check (field request): record 3 s with a live level bar, play
  // it back (you HEAR what the recorder heard — the speaker check and the garble
  // detector in one), transcribe it, and keep the bytes on disk for diagnosis. ----
  const testRec = new Recorder();
  let micTest = $state<"idle" | "recording" | "processing" | "done">("idle");
  let micTestLevel = $state(0);
  let micTestPeakSeen = $state(0);
  let micTestSeconds = $state(0);
  let micTestUrl = $state<string | null>(null); // playback of the exact recording
  let micTestText = $state<string | null>(null);
  let micTestError = $state("");
  let micCountdown = $state(3);
  testRec.onLevel = (rms) => {
    micTestLevel = Math.min(1, rms * 6);
    if (rms > micTestPeakSeen) micTestPeakSeen = rms;
  };

  async function runMicTest() {
    if (micTest === "recording" || micTest === "processing") return;
    micTestError = "";
    micTestText = null;
    micTestPeakSeen = 0;
    if (micTestUrl) URL.revokeObjectURL(micTestUrl);
    micTestUrl = null;
    try {
      await testRec.start();
    } catch (err) {
      micTest = "idle";
      micTestError =
        err instanceof DOMException && (err.name === "NotAllowedError" || err.name === "SecurityError")
          ? "Microphone access was denied — allow it for this site in the browser, and check System Settings → Privacy & Security → Microphone."
          : `Could not start the microphone: ${err instanceof Error ? err.message : String(err)}`;
      return;
    }
    micTest = "recording";
    for (micCountdown = 3; micCountdown > 0; micCountdown--) {
      await new Promise((r) => setTimeout(r, 1000));
    }
    micTest = "processing";
    try {
      const rec = await testRec.stop();
      micTestLevel = 0;
      micTestSeconds = rec.seconds;
      micTestUrl = URL.createObjectURL(rec.blob);
      if (rec.peak < 0.003) {
        micTestError = "The microphone recorded silence — check the input device and its level (System Settings → Sound → Input).";
        micTest = "done";
        return;
      }
      const r = await api.voiceTranscribe(rec.blob, { keep: true });
      micTestText = r.text || "";
    } catch (err) {
      micTestError = err instanceof ApiError && err.message ? err.message : describeError(err);
    }
    micTest = "done";
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

        <!-- The whole voice path, tested in one tap: record → hear yourself → read the
             transcription. Each leg failing points at exactly one culprit. -->
        <div class="mic-test">
          <div class="srow">
            <span><strong>Mic &amp; speaker check</strong></span>
            <button
              class="secondary"
              disabled={micTest === "recording" || micTest === "processing"}
              onclick={runMicTest}
            >
              {micTest === "recording" ? `Recording… ${micCountdown}` : micTest === "processing" ? "Working…" : "Test now"}
            </button>
          </div>
          {#if micTest === "recording"}
            <p class="muted" style="margin:0.35rem 0 0.25rem; font-size:0.85rem">Say a full sentence out loud — the bar should move with your voice:</p>
            <div class="bar"><div class="fill" style={`width:${Math.round(micTestLevel * 100)}%`}></div></div>
          {/if}
          {#if micTest === "done"}
            {#if micTestUrl}
              <p class="muted" style="margin:0.5rem 0 0.25rem; font-size:0.85rem">
                <strong>1. Speaker check:</strong> press play — you should hear yourself, clearly.
              </p>
              <audio controls src={micTestUrl} style="width:100%; max-width:24rem"></audio>
            {/if}
            {#if micTestText !== null}
              <p class="muted" style="margin:0.5rem 0 0.25rem; font-size:0.85rem"><strong>2. What dictation heard</strong> ({micTestSeconds.toFixed(1)}s, peak {micTestPeakSeen.toFixed(3)}):</p>
              {#if micTestText}
                <p style="margin:0; font-size:0.95rem">“{micTestText}”</p>
              {:else}
                <p class="error" style="margin:0; font-size:0.9rem">No words recognized. If the playback above sounds clear, the engine is the problem — if it sounds silent or garbled, the microphone is.</p>
              {/if}
            {/if}
          {/if}
          {#if micTestError}<p class="error" style="margin:0.4rem 0 0; font-size:0.9rem">{micTestError}</p>{/if}
        </div>
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
  .mic-test {
    margin-top: 0.6rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--border);
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
