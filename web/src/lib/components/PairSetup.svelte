<script lang="ts">
  // One mobile setup path: open in the phone browser -> guided to Add to Home Screen ->
  // (flag remembers) -> open the installed app -> pair there with a code. iOS isolates the
  // installed app's storage from the browser, so pairing must happen in the installed app;
  // this funnels the user there instead of letting them pair in the browser and lose it.
  import { isInstalledApp, isMobile } from "$lib/remote/platform";
  import { pairByCode } from "$lib/remote/paircode";
  import { savePairing } from "$lib/remote/store";

  const installed = isInstalledApp();

  let acked = $state(typeof localStorage !== "undefined" && localStorage.getItem("sb-installed") === "1");
  let code = $state("");
  let pairing = $state(false);
  let error = $state("");

  function ackInstalled() {
    try {
      localStorage.setItem("sb-installed", "1");
    } catch {
      /* private mode — fine */
    }
    acked = true;
  }

  // Show the pairing form only in the installed app or a desktop browser. Mobile browsers
  // (Safari/Chrome on a phone) ALWAYS get install-first guidance — their storage is isolated
  // from the installed PWA, so pairing in the browser would be lost.
  const showPairForm = $derived(installed || !isMobile());

  async function submit() {
    if (pairing) return;
    pairing = true;
    error = "";
    try {
      await savePairing(await pairByCode(code));
      window.location.assign("/"); // full reload -> initRemote picks up the pairing -> connected
    } catch (e) {
      error = e instanceof Error ? e.message : "pairing failed";
    } finally {
      pairing = false;
    }
  }
</script>

<div class="card">
  {#if showPairForm}
    <h1>Pair this device</h1>
    <p class="muted">
      On your Desktop: <b>Settings &rarr; Remote access &rarr; Pair a new phone</b>, then enter the
      6-character code here.
    </p>
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
    <p style="margin-top:1rem">
      <button onclick={submit} disabled={pairing}>{pairing ? "Pairing…" : "Pair"}</button>
    </p>
    {#if error}<p class="error">{error}</p>{/if}
  {:else if !acked}
    <h1>Set up SmartBrain on your phone</h1>
    <p>For a reliable, one-tap app that works from anywhere, add SmartBrain to your Home Screen first:</p>
    <ul style="line-height:1.8">
      <li><b>iOS:</b> tap the <b>Share</b> button &rarr; <b>Add to Home Screen</b>.</li>
      <li><b>Android:</b> tap the <b>&vellip;</b> menu &rarr; <b>Install app</b> (or <b>Add to Home screen</b>).</li>
    </ul>
    <p style="margin-top:1rem"><button onclick={ackInstalled}>I&rsquo;ve added it to my Home Screen</button></p>
  {:else}
    <h1>Almost there</h1>
    <p>Open the <b>SmartBrain</b> icon from your Home Screen to finish setting up — you&rsquo;ll enter a code there.</p>
    <p class="muted" style="font-size:0.85rem; margin-top:0.75rem">
      Don&rsquo;t see it? <button class="link" onclick={() => (acked = false)}>Show the steps again.</button>
    </p>
  {/if}
</div>
