<script lang="ts">
  // Pairing runs entirely by 8-char code (shown ABCD-EFGH; the dash is optional when typing): the Desktop shows a code and QR whose only job is
  // to open this site on the phone so the operator can install the PWA. On iOS an installed
  // Home Screen app has its own storage isolated from Safari, so pairing must happen in the
  // installed app; we fetch the pairing over an encrypted WebRTC channel (see paircode.ts).
  import { pairByCode } from "$lib/remote/paircode";
  import { savePairing } from "$lib/remote/store";

  let phase = $state<"code" | "done" | "error">("code");
  let error = $state("");
  let code = $state("");
  let pairing = $state(false);

  async function submitCode() {
    if (pairing) return;
    pairing = true;
    error = "";
    try {
      await savePairing(await pairByCode(code));
      phase = "done";
    } catch (e) {
      // A locked Desktop can't pair (its device store is encrypted) and the phone only
      // sees a timeout — name the likeliest cause rather than a bare "failed".
      error = e instanceof Error && e.message !== "pairing failed"
        ? e.message
        : "Pairing didn't complete — make sure your Desktop is UNLOCKED and showing a pairing code, then try again.";
    } finally {
      pairing = false;
    }
  }
</script>

<div class="card">
  {#if phase === "code"}
    <h1>Pair this device</h1>
    <p>On your Desktop, open <b>Settings &rarr; Remote access &rarr; Pair a new phone</b>, then enter
      the 8-character code shown (with or without the dash).</p>
    <p style="margin-top:1rem">
      <input
        bind:value={code}
        placeholder="e.g. ABCD-EFGH"
        aria-label="Pairing code"
        autocapitalize="characters"
        autocomplete="off"
        autocorrect="off"
        spellcheck="false"
        maxlength="12"
        style="text-transform:uppercase;letter-spacing:0.15em;font-family:var(--font-mono)"
      />
    </p>
    <p style="margin-top:1rem"><button onclick={submitCode} disabled={pairing}>{pairing ? "Pairing…" : "Pair"}</button></p>
    {#if error}<p class="error" role="alert">{error}</p>{/if}
  {:else if phase === "done"}
    <h1>Paired &check;</h1>
    <p>You're set. Open SmartBrain to start using it from anywhere:</p>
    <!-- Hard navigation (not client-side routing) so the layout's initRemote() re-runs and
         picks up the pairing we just saved; otherwise /api has no relay -> 404. -->
    <p style="margin-top:1rem"><button onclick={() => window.location.assign("/")}>Open SmartBrain</button></p>
  {:else}
    <h1>Pairing failed</h1>
    <p class="error" role="alert">{error}</p>
    <p class="muted">Open your Desktop&rsquo;s <b>Settings &rarr; Remote access</b> and try again.</p>
  {/if}
</div>
