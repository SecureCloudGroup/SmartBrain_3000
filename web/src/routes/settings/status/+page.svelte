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
  import { loadWakeWord, matchWake, saveWakeWord } from "$lib/audio/wakeword";
  import { SPEECH_RATE_KEY, speechRate } from "$lib/audio/speaker";
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
    ({ phrase: wakePhrase, aliases: wakeAliases } = loadWakeWord());
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

  function human(bytes: number): string {
    if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(2)} GB`;
    if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${bytes} B`;
  }

  // This browser's own footprint (PWA caches etc.) — client-side, works wherever the
  // page renders; null when the browser won't say.
  let browserUsage = $state<number | null>(null);
  void navigator.storage?.estimate?.().then((e) => (browserUsage = e.usage ?? null)).catch(() => undefined);

  // Playback speed for spoken replies (field request): stored per device, applied to
  // the next sentence spoken — no reload, no server.
  let ttsRate = $state(speechRate());
  function setTtsRate(e: Event) {
    ttsRate = parseFloat((e.target as HTMLSelectElement).value);
    try {
      localStorage.setItem(SPEECH_RATE_KEY, String(ttsRate));
    } catch {
      /* storage unavailable — applies for this visit only */
    }
    if (typeof speechSynthesis !== "undefined") {
      // Audible confirmation at the new speed — the setting explains itself.
      const u = new SpeechSynthesisUtterance("This is how fast I'll speak.");
      u.rate = ttsRate;
      speechSynthesis.cancel();
      speechSynthesis.speak(u);
    }
  }

  function retryVoice() {
    void api.voiceStatus(false, true).then(load).catch(() => undefined);
  }

  // ---- Mic & Speaker check (field request): record 3 s with a live level bar, play
  // it back (you HEAR what the recorder heard — the speaker check and the garble
  // detector in one), transcribe it, and keep the bytes on disk for diagnosis. ----
  const testRec = new Recorder();
  let playCtx: AudioContext | null = null;
  let playSrc: AudioBufferSourceNode | null = null;
  let playing = $state(false);
  let micTestBlob = $state<Blob | null>(null); // gates the Play button's render

  // Safari renders an <audio> with a WAV blob URL as "Error" — decoding through the
  // audio engine works everywhere, so the play button drives a BufferSource instead.
  async function togglePlayback() {
    if (playing) {
      playSrc?.stop();
      return;
    }
    if (!micTestBlob) return;
    try {
      playCtx = playCtx ?? new AudioContext();
      await playCtx.resume();
      const buf = await playCtx.decodeAudioData(await micTestBlob.arrayBuffer());
      const src = playCtx.createBufferSource();
      src.buffer = buf;
      src.connect(playCtx.destination);
      src.onended = () => {
        playing = false;
        playSrc = null;
      };
      playSrc = src;
      playing = true;
      src.start();
    } catch (err) {
      playing = false;
      micTestError = `Playback failed: ${err instanceof Error ? err.message : String(err)}`;
    }
  }
  // ---- Wake word: the user's own phrase, TESTED here before it is relied on. ----
  // The engine spells unusual names its own way; three tries show exactly what it hears,
  // and the spellings it produced can be accepted as aliases so the name works naturally.
  const wakeRec = new Recorder();
  let wakePhrase = $state("");
  let wakeAliases = $state<string[]>([]);
  let wakeTest = $state<"idle" | "listening" | "processing" | "done">("idle");
  let wakeRound = $state(0);
  let wakeHeard = $state<{ heard: string; hit: boolean }[]>([]);
  let wakeError = $state("");
  let wakeLevel = $state(0);
  let wakeSaved = $state("");
  let wakeLoud = false;
  let wakeLastLoud = 0;
  wakeRec.onLevel = (rms) => {
    wakeLevel = Math.min(1, rms * 6);
    if (rms > 0.012) wakeLoud = true;
    if (rms > 0.006) wakeLastLoud = performance.now();
  };
  const wakeHits = $derived(wakeHeard.filter((h) => h.hit).length);
  // Spellings the engine produced that the phrase did NOT already accept — the aliases on offer.
  const wakeNewSpellings = $derived(
    wakeHeard
      .filter((h) => !h.hit && h.heard)
      .map((h) => h.heard.split(" ").slice(0, wakePhrase.trim().split(/\s+/).length).join(" "))
      .filter((v, i, a) => v && a.indexOf(v) === i),
  );
  function saveWake() {
    saveWakeWord(wakePhrase, wakeAliases);
    wakeSaved = wakePhrase.trim() ? `Saved — “${wakePhrase.trim()}” is your wake word.` : "Wake word cleared.";
  }
  function acceptSpellings() {
    wakeAliases = [...new Set([...wakeAliases, ...wakeNewSpellings])];
    wakeHeard = wakeHeard.map((h) => ({ ...h, hit: h.hit || matchWake(h.heard, wakePhrase, wakeAliases).hit }));
    saveWake();
  }
  async function runWakeTest() {
    if (wakeTest === "listening" || wakeTest === "processing" || !wakePhrase.trim()) return;
    wakeError = "";
    wakeHeard = [];
    for (wakeRound = 1; wakeRound <= 3; wakeRound++) {
      try {
        await wakeRec.start();
      } catch (err) {
        wakeError = `Could not start the microphone: ${err instanceof Error ? err.message : String(err)}`;
        wakeTest = "idle";
        return;
      }
      wakeTest = "listening";
      wakeLoud = false;
      wakeLastLoud = performance.now();
      const started = performance.now();
      // Endpoint exactly like Chat: speech heard, then ~1.2 s of quiet; 8 s with nothing.
      await new Promise<void>((done) => {
        const t = setInterval(() => {
          const now = performance.now();
          if ((wakeLoud && now - wakeLastLoud > 1200) || now - started > 8000) {
            clearInterval(t);
            done();
          }
        }, 100);
      });
      wakeTest = "processing";
      try {
        const rec = await wakeRec.stop();
        wakeLevel = 0;
        const r = wakeLoud ? await api.voiceTranscribe(rec.blob) : { text: "" };
        const m = matchWake(r.text, wakePhrase, wakeAliases);
        wakeHeard = [...wakeHeard, { heard: m.heard, hit: m.hit }];
      } catch (err) {
        wakeError = err instanceof ApiError && err.message ? err.message : describeError(err);
        wakeTest = "done";
        return;
      }
    }
    wakeTest = "done";
  }
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
    micTestBlob = null;
    playSrc?.stop();
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
      micTestBlob = rec.blob;
      micTestUrl = URL.createObjectURL(rec.blob); // kept for a download affordance later
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
          <span>Built-in dictation model <span class="muted">(one-time ~141 MB download)</span></span>
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

        <div class="srow">
          <span>Playback speed <span class="muted">(spoken replies)</span></span>
          <select value={String(ttsRate)} onchange={setTtsRate} aria-label="Playback speed">
            <option value="0.8">0.8×</option>
            <option value="0.9">0.9×</option>
            <option value="1">1× (natural)</option>
            <option value="1.1">1.1×</option>
            <option value="1.2">1.2×</option>
            <option value="1.3">1.3×</option>
            <option value="1.4">1.4×</option>
            <option value="1.5">1.5×</option>
            <option value="1.75">1.75×</option>
            <option value="2">2×</option>
          </select>
        </div>

        <div class="srow" style="align-items:flex-start; flex-wrap:wrap; gap:0.5rem">
          <span>Wake word <span class="muted">(conversation mode — “Hey Siri”, but yours)</span></span>
          <span style="display:flex; gap:0.4rem; flex-wrap:wrap; align-items:center">
            <input
              type="text"
              bind:value={wakePhrase}
              placeholder="Hey SmartBrain"
              aria-label="Wake word"
              style="min-width:11rem"
            />
            <button class="secondary" onclick={saveWake} disabled={wakeTest === "listening" || wakeTest === "processing"}>Save</button>
            <button onclick={runWakeTest} disabled={!wakePhrase.trim() || wakeTest === "listening" || wakeTest === "processing"}>
              {wakeTest === "listening" ? `Say it now… (${wakeRound} of 3)` : wakeTest === "processing" ? "Checking…" : "Test recognition"}
            </button>
          </span>
        </div>
        {#if wakeSaved}
          <p class="muted" style="margin:0; font-size:0.85rem">{wakeSaved}</p>
        {/if}
        {#if wakeTest === "listening"}
          <div class="bar"><div class="fill" style={`width:${Math.round(wakeLevel * 100)}%`}></div></div>
          <p class="muted" style="margin:0; font-size:0.85rem">Say <strong>“{wakePhrase}”</strong> the way you naturally would — it stops when you pause.</p>
        {/if}
        {#if wakeHeard.length}
          <div class="rows" style="margin:0.25rem 0">
            {#each wakeHeard as h, i (i)}
              <p class="muted" style="margin:0; font-size:0.85rem">{i + 1}. heard <em>“{h.heard || "(nothing)"}”</em> — {h.hit ? "✓ recognised" : "✗ not recognised"}</p>
            {/each}
            {#if wakeTest === "done"}
              {#if wakeHits >= 2}
                <p style="margin:0; font-size:0.85rem"><strong>Works:</strong> {wakeHits} of 3 recognised. Turn on the Conversation pill above the message box in Chat and just say it.</p>
              {:else if wakeNewSpellings.length}
                <p style="margin:0; font-size:0.85rem"><strong>The engine spells it differently</strong> — it heard “{wakeNewSpellings.join("”, “")}”. Accept those spellings and the name works as you say it.</p>
                <p style="margin:0"><button onclick={acceptSpellings}>Accept these spellings</button></p>
              {:else}
                <p style="margin:0; font-size:0.85rem"><strong>Not recognised.</strong> Try a phrase with two clear words (“Hey Catherine”), a little closer to the microphone, or check the Mic &amp; speaker test below.</p>
              {/if}
            {/if}
          </div>
        {/if}
        {#if wakeAliases.length}
          <p class="muted" style="margin:0; font-size:0.85rem">Accepted spellings: {wakeAliases.join(", ")} <button class="secondary" style="margin-left:0.4rem" onclick={() => { wakeAliases = []; saveWake(); }}>clear</button></p>
        {/if}
        {#if wakeError}
          <p class="error" style="margin:0; font-size:0.85rem">{wakeError}</p>
        {/if}

        <!-- How to actually use it — the same words the Chat hint teaches. -->
        <div class="rows" style="margin-top:0.6rem">
          <p class="muted" style="margin:0; font-size:0.85rem"><strong>How to dictate:</strong> in Chat, tap the mic beside the message box (or hold <strong>Space</strong>) and talk — your words appear under the box as you speak; when you pause, the finished transcript lands in the message box.</p>
          <p class="muted" style="margin:0; font-size:0.85rem"><strong>Spoken controls:</strong> end with <em>“send”</em> to submit, say <em>“cancel”</em> to discard, <em>“start over”</em> to redo. <strong>Esc</strong> cancels a recording.</p>
          <p class="muted" style="margin:0; font-size:0.85rem"><strong>Hands-free</strong> (pill above the message box): every dictation sends itself when you pause. <strong>Speak replies</strong> (pill): answers are read aloud as they arrive; <em>Listen</em> under any answer reads just that one.</p>
          <p class="muted" style="margin:0; font-size:0.85rem"><strong>Conversation</strong> (pill above the message box): 100% voice — you talk, it answers aloud, then listens again. With a wake word set above, it waits for your phrase instead of listening all the time; say <em>“stop listening”</em> or <em>“goodbye”</em> to end. The first mic open needs one tap (a browser rule); after that, no buttons.</p>
        </div>

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
            {#if micTestBlob}
              <p class="muted" style="margin:0.5rem 0 0.25rem; font-size:0.85rem">
                <strong>1. Speaker check:</strong> press play — you should hear yourself, clearly.
              </p>
              <button class="secondary" onclick={togglePlayback}>{playing ? "Stop" : "▶ Play my recording"}</button>
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

    <div class="card">
      <h2 class="row"><span>Storage &amp; memory</span></h2>
      <div class="rows">
        <div class="srow"><span>Everything SmartBrain stores <span class="muted">({status.storage.data_dir})</span></span><strong>{human(status.storage.total_bytes)}</strong></div>
        <div class="srow"><span>Your encrypted database</span><strong>{human(status.storage.db_bytes)}</strong></div>
        <div class="srow"><span>Voice model</span><strong>{human(status.storage.models_bytes)}</strong></div>
        <div class="srow"><span>App memory (peak)</span><strong>{human(status.memory.rss_bytes)}</strong></div>
        {#if browserUsage !== null}
          <div class="srow"><span>This browser's cache</span><strong>{human(browserUsage)}</strong></div>
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
