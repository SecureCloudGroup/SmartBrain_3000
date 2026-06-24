<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import QRCode from "qrcode";
  import { api, type DeviceInfo, type PairingResponse } from "$lib/api";
  import { encodePairingFragment, type PairingPayload } from "$lib/remote/pairing";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";

  let devices = $state<DeviceInfo[]>([]);
  let label = $state("My phone");
  let busy = $state(false);
  let error = $state("");
  let qr = $state(""); // data URL of the latest pairing QR (shown once)
  let notConfigured = $state(false);
  let pairCode = $state(""); // 6-char code for the installed (home-screen) app pairing path
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

  function toPayload(r: PairingResponse): PairingPayload {
    return {
      v: 1,
      deviceId: r.device_id,
      credential: r.credential,
      desktopPubkey: r.desktop_pubkey,
      signalingUrl: r.signaling_url,
      desktopId: r.desktop_id,
      iceServers: r.ice_servers ?? [],
    };
  }

  async function mint() {
    busy = true;
    error = "";
    qr = "";
    notConfigured = false; // clear any stale "not configured" card from a prior attempt
    try {
      const r = await api.createDevice(label.trim() || "device");
      if (!r.signaling_url) {
        notConfigured = true;
        await load();
        return;
      }
      // Pair-link origin = the NODE that serves the shell (https form of the signaling
      // host), not wherever the operator views this page. Lets the QR be generated from
      // localhost while the phone loads the app from the always-reachable node.
      const shellOrigin = (() => {
        try {
          return `https://${new URL(r.signaling_url).host}`;
        } catch {
          return window.location.origin;
        }
      })();
      const url = `${shellOrigin}/pair#${encodePairingFragment(toPayload(r))}`;
      qr = await QRCode.toDataURL(url, { width: 240, margin: 1 });
      await load();
    } catch (e) {
      error = describeError(e);
    } finally {
      busy = false;
    }
  }

  async function pairViaCode() {
    codeBusy = true;
    error = "";
    pairCode = "";
    stopPairPolling();
    try {
      const r = await api.startPairCode(label.trim() || "device");
      pairCode = r.code;
      pairState = "waiting";
      pairRemaining = r.expires_in;
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
      qr = "";
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

{#if notConfigured}
  <div class="card">
    <p class="error">
      Remote access isn&rsquo;t configured yet. Start the stack with the WebRTC overlay and a
      signaling node (Help &rarr; Remote access), then pair a device.
    </p>
  </div>
{/if}

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
    <button disabled={busy} onclick={mint}>{busy ? "Creating…" : "Pair a new phone"}</button>
  </p>
  {#if qr}
    <p class="muted">On the phone, scan this with the camera — it opens the app from your node, so it works from anywhere:</p>
    <img src={qr} alt="Pairing QR code" width="240" height="240" />
    <p class="muted" style="font-size:0.8rem">Shows the device credential once — close it after the phone is paired.</p>
  {/if}
  <hr style="margin:1rem 0;border:none;border-top:1px solid var(--border)" />
  <p class="muted" style="font-size:0.9rem">Installed (Home Screen) app? It can&rsquo;t scan the QR, so pair it with a code:</p>
  <p><button class="secondary" disabled={codeBusy} onclick={pairViaCode}>{codeBusy ? "Starting…" : "Pair via code"}</button></p>
  {#if pairCode}
    <p class="muted">In the installed app tap <b>Pair with a code</b> and enter this, on the same Wi-Fi as this Desktop:</p>
    <p style="font:700 2rem/1.2 ui-monospace,monospace;letter-spacing:0.25em">{pairCode}</p>
    {#if pairState === "waiting"}
      <p class="muted">&#8987; Waiting for your phone&hellip; (expires in {fmtRemaining(pairRemaining)})</p>
    {:else if pairState === "paired"}
      <p style="color:var(--ok); font-weight:600">&check; Your phone connected.</p>
    {:else if pairState === "expired"}
      <p class="muted">Code expired &mdash; tap &ldquo;Pair via code&rdquo; for a new one.</p>
    {/if}
  {/if}
</div>

{#if error}<p class="error">{error}</p>{/if}
