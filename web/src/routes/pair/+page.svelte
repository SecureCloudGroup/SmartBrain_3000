<script lang="ts">
  // Two pairing paths land here:
  //  - QR deep link (Safari): the payload is in the URL fragment (never hits a server); we
  //    show WHAT is being paired and require a tap before storing it.
  //  - Code (installed home-screen app): iOS isolates the installed app's storage from
  //    Safari, so it can't inherit a QR pairing. With no fragment we show a code-entry form;
  //    the operator reads a 6-char code off the Desktop and we fetch the pairing over an
  //    encrypted WebRTC channel (see paircode.ts).
  import { onMount } from "svelte";
  import { decodePairingFragment, type PairingPayload } from "$lib/remote/pairing";
  import { pairByCode } from "$lib/remote/paircode";
  import { savePairing } from "$lib/remote/store";

  let phase = $state<"review" | "code" | "done" | "error">("review");
  let payload = $state<PairingPayload | null>(null);
  let error = $state("");
  let code = $state("");
  let pairing = $state(false);

  onMount(() => {
    if (!window.location.hash.replace(/^#/, "")) {
      phase = "code"; // no QR payload in the URL -> manual code entry (installed-app path)
      return;
    }
    try {
      payload = decodePairingFragment(window.location.hash);
      history.replaceState(null, "", window.location.pathname); // scrub the secret from the URL
    } catch (e) {
      error = e instanceof Error ? e.message : "invalid pairing link";
      phase = "error";
    }
  });

  function signalingHost(url: string): string {
    try {
      return new URL(url).host;
    } catch {
      return url;
    }
  }

  async function confirm() {
    if (!payload) return;
    try {
      // payload is a Svelte $state proxy (for the review UI); IndexedDB can't
      // structured-clone a proxy, so persist a plain snapshot of it.
      await savePairing($state.snapshot(payload));
      phase = "done";
    } catch (e) {
      error = e instanceof Error ? e.message : "could not save the pairing";
      phase = "error";
    }
  }

  async function submitCode() {
    if (pairing) return;
    pairing = true;
    error = "";
    try {
      await savePairing(await pairByCode(code));
      phase = "done";
    } catch (e) {
      error = e instanceof Error ? e.message : "pairing failed";
    } finally {
      pairing = false;
    }
  }
</script>

<div class="card">
  {#if phase === "review" && payload}
    <h1>Pair this phone?</h1>
    <p>You're about to pair this phone for remote access to:</p>
    <ul>
      <li>Desktop: <b>{payload.desktopId || "(unnamed)"}</b></li>
      <li>via <b>{signalingHost(payload.signalingUrl)}</b></li>
    </ul>
    <p class="muted" style="font-size:0.85rem">Only continue if you started this from your own
      Desktop's <b>Settings → Remote access</b>.</p>
    <p style="margin-top:1rem"><button onclick={confirm}>Pair this phone</button></p>
  {:else if phase === "code"}
    <h1>Pair this device</h1>
    <p>On your Desktop, open <b>Settings &rarr; Remote access</b> and tap <b>Pair via code</b>, then
      enter the 6-character code shown. Do this while on the same network as your Desktop.</p>
    <p style="margin-top:1rem">
      <input
        bind:value={code}
        placeholder="e.g. ABC234"
        autocapitalize="characters"
        autocomplete="off"
        autocorrect="off"
        spellcheck="false"
        maxlength="10"
        style="text-transform:uppercase;letter-spacing:0.15em;font-family:ui-monospace,monospace"
      />
    </p>
    <p style="margin-top:1rem"><button onclick={submitCode} disabled={pairing}>{pairing ? "Pairing…" : "Pair"}</button></p>
    {#if error}<p class="error">{error}</p>{/if}
  {:else if phase === "done"}
    <h1>Paired &check;</h1>
    <p>You're set. Open SmartBrain to start using it from anywhere:</p>
    <!-- Hard navigation (not client-side routing) so the layout's initRemote() re-runs and
         picks up the pairing we just saved; otherwise /api has no relay -> 404. -->
    <p style="margin-top:1rem"><button onclick={() => window.location.assign("/")}>Open SmartBrain</button></p>
  {:else}
    <h1>Pairing failed</h1>
    <p class="error">{error}</p>
    <p class="muted">Open your Desktop&rsquo;s <b>Settings &rarr; Remote access</b> and try again.</p>
  {/if}
</div>
