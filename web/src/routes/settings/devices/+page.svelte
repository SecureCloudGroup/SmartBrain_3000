<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import QRCode from "qrcode";
  import { api, type DeviceInfo } from "$lib/api";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";

  let devices = $state<DeviceInfo[]>([]);
  let label = $state("My phone");
  let error = $state("");
  let originQr = $state(""); // data URL of the node-origin QR (just opens the site on the phone)
  let originHost = $state(""); // host shown in step (1) as a fallback for typing
  let pairCode = $state(""); // 6-char code entered in the installed PWA
  let codeBusy = $state(false);
  let pairState = $state<"idle" | "waiting" | "paired" | "expired">("idle");
  let pairRemaining = $state(0); // seconds until the code expires
  let pairTimer: ReturnType<typeof setInterval> | undefined;

  function stopPairPolling() {
    if (pairTimer) clearInterval(pairTimer);
    pairTimer = undefined;
  }
  function fmtRemaining(s: number): string {
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  // created_at is a UTC string — show it in the user's locale.
  function localCreatedAt(s: string): string {
    if (!s) return "";
    const d = new Date(s.slice(0, 19).replace(" ", "T") + "Z");
    return Number.isNaN(d.getTime()) ? s : d.toLocaleString();
  }
  onDestroy(stopPairPolling);

  async function load() {
    error = "";
    try {
      devices = (await api.listDevices()).devices;
    } catch (e) {
      error = describeError(e);
    }
  }
  onMount(load);

  async function startPairing() {
    codeBusy = true;
    error = "";
    pairCode = "";
    originQr = "";
    originHost = "";
    stopPairPolling();
    try {
      const r = await api.startPairCode(label.trim() || "device");
      pairCode = r.code;
      pairState = "waiting";
      pairRemaining = r.expires_in;
      // The QR encodes ONLY the node origin (bare https URL). Its sole job is to open the
      // site on the phone so the user can install the PWA; pairing itself happens by code.
      const host = (() => {
        try {
          return new URL(r.signaling_url).host;
        } catch {
          return window.location.host;
        }
      })();
      originHost = host;
      originQr = await QRCode.toDataURL(`https://${host}`, { width: 240, margin: 1 });
      // Live feedback: count the code down + poll for the phone connecting (every 2s).
      let tick = 0;
      pairTimer = setInterval(async () => {
        pairRemaining = Math.max(0, pairRemaining - 1);
        if (pairRemaining <= 0) {
          pairState = "expired";
          stopPairPolling();
          return;
        }
        if (++tick % 2 !== 0) return;
        try {
          const s = (await api.pairCodeStatus()).state;
          if (s === "paired") {
            pairState = "paired";
            stopPairPolling();
            await load(); // the new device shows up below
          } else if (s === "expired" || s === "none") {
            pairState = "expired";
            stopPairPolling();
          }
        } catch {
          /* transient — keep polling */
        }
      }, 1000);
      await load();
    } catch (e) {
      error = describeError(e);
    } finally {
      codeBusy = false;
    }
  }

  async function revoke(id: string) {
    if (
      !(await confirmDialog({
        title: "Revoke device",
        body: "Revoke this device? It will no longer be able to connect.",
        confirmLabel: "Revoke",
        danger: true,
      }))
    )
      return;
    try {
      await api.deleteDevice(id);
      await load();
    } catch (e) {
      error = describeError(e);
    }
  }
</script>

<h1>Remote access</h1>
<p class="muted">
  Pair a phone to reach this Desktop from anywhere over an end-to-end-encrypted WebRTC
  connection — nothing to install beyond the web app, and no router setup.
  <a href="/help#remote-access">Learn more</a>.
</p>

{#if devices.length}
  <div class="card">
    <h2>Paired devices</h2>
    {#each devices as d (d.device_id)}
      <div class="row">
        <span>
          <span style="color:var(--ok)" aria-hidden="true">●</span>
          {d.label} <span class="muted">({localCreatedAt(d.created_at)})</span>
          <span style="color:var(--ok); font-weight:600">· Paired</span>
        </span>
        <button class="del" onclick={() => revoke(d.device_id)}>Revoke</button>
      </div>
    {/each}
  </div>
{/if}

<div class="card">
  <h2>Pair a new phone</h2>
  <label for="dev-label">Device name</label>
  <input id="dev-label" bind:value={label} placeholder="My phone" />
  <p style="margin-top:0.75rem">
    <button disabled={codeBusy} onclick={startPairing}>{codeBusy ? "Starting…" : "Pair a new phone"}</button>
  </p>
  {#if pairCode}
    <div style="display:flex; gap:1.5rem; align-items:flex-start; flex-wrap:wrap; margin-top:0.5rem">
      <div>
        {#if originQr}
          <img src={originQr} alt="QR code to open SmartBrain on your phone" width="240" height="240" />
          <p class="muted" style="font-size:0.85rem; max-width:240px">Scan to open SmartBrain on your phone.</p>
        {/if}
      </div>
      <ol style="line-height:1.7; margin:0; padding-left:1.25rem; flex:1; min-width:16rem">
        <li>Scan this — or go to <b>{originHost}</b> — on your phone.</li>
        <li><b>Add to Home Screen</b>, then open the app.</li>
        <li>
          Enter this code:
          <div style="font-family:var(--font-mono); font-weight:700; font-size:2rem; line-height:1.2; letter-spacing:0.25em; margin-top:0.25rem">{pairCode}</div>
          <span class="muted">expires in {fmtRemaining(pairRemaining)}</span>
        </li>
      </ol>
    </div>
    {#if pairState === "waiting"}
      <p class="muted" style="margin-top:0.75rem">&#8987; Waiting for your phone&hellip;</p>
    {:else if pairState === "paired"}
      <p style="color:var(--ok); font-weight:600; margin-top:0.75rem">&check; Your phone connected.</p>
    {:else if pairState === "expired"}
      <p class="muted" style="margin-top:0.75rem">Code expired &mdash; tap &ldquo;Pair a new phone&rdquo; for a new one.</p>
    {/if}
  {/if}
</div>

{#if error}<p class="error">{error}</p>{/if}
