<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, ApiError, type EmergencyKit } from "$lib/api";
  import Spinner from "$lib/components/Spinner.svelte";

  let passphrase = $state("");
  let confirm = $state("");
  let error = $state("");
  let busy = $state(false);
  let kit = $state<EmergencyKit | null>(null);
  let saved = $state(false);
  let copied = $state(false);
  let kitTouched = $state(false); // U15: flips true after Download or Copy is used at least once
  let copiedTimer: ReturnType<typeof setTimeout> | null = null;
  let ppInput = $state<HTMLInputElement | null>(null);
  let cfInput = $state<HTMLInputElement | null>(null);

  function downloadKit() {
    console.assert(typeof URL !== "undefined", "URL API required for download");
    console.assert(kit === null || typeof kit.emergency_kit === "string", "kit shape");
    if (!kit) return;
    const url = URL.createObjectURL(new Blob([kit.emergency_kit], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "smartbrain-emergency-kit.txt";
    a.click();
    URL.revokeObjectURL(url);
    kitTouched = true;
  }

  async function copyKit() {
    console.assert(kit === null || typeof kit.emergency_kit === "string", "kit shape");
    console.assert(typeof navigator !== "undefined", "navigator required");
    if (!kit) return;
    try {
      await navigator.clipboard.writeText(kit.emergency_kit);
      copied = true;
      kitTouched = true;
      if (copiedTimer) clearTimeout(copiedTimer);
      copiedTimer = setTimeout(() => {
        copied = false;
        copiedTimer = null;
      }, 1500);
    } catch {
      /* clipboard unavailable — the user can select the text */
    }
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    if (account.status?.initialized) goto("/unlock"); // already set up
  });

  async function submit(event: Event) {
    console.assert(event instanceof Event, "submit expects a DOM Event");
    console.assert(typeof passphrase === "string", "passphrase must be a string");
    event.preventDefault();
    error = "";
    if (passphrase.length < 8) {
      error = "Passphrase must be at least 8 characters.";
      ppInput?.focus();
      return;
    }
    if (passphrase !== confirm) {
      error = "Passphrases do not match.";
      cfInput?.focus();
      return;
    }
    busy = true;
    try {
      kit = await api.setup(passphrase);
      await account.load();
    } catch (err) {
      error = err instanceof ApiError ? err.message : "Setup failed.";
    } finally {
      busy = false;
    }
  }
</script>

{#if kit}
  <div class="card">
    <h1>Save your Emergency Kit</h1>
    <p class="warn">
      <strong>This is shown only once. There is no remote reset.</strong> If you forget your
      passphrase, this Recovery Key is the <strong>only</strong> way back into your vault. Save it
      somewhere safe (a password manager, or print it) <strong>before continuing</strong>.
    </p>
    <div class="kit">{kit.emergency_kit}</div>
    <p style="margin-top:0.75rem; display:flex; gap:0.5rem; flex-wrap:wrap">
      <button class="secondary" onclick={downloadKit}>Download (.txt)</button>
      <button class="secondary" onclick={copyKit}>{copied ? "Copied ✓" : "Copy"}</button>
    </p>
    <label style="display:flex; gap:0.5rem; align-items:center; margin-top:0.75rem">
      <input type="checkbox" bind:checked={saved} disabled={!kitTouched} />
      I&rsquo;ve saved my Emergency Kit somewhere safe
    </label>
    {#if !kitTouched}
      <p class="muted" style="margin-top:0.25rem; font-size:0.85rem">
        Download or copy the kit first to enable this.
      </p>
    {/if}
    <p style="margin-top:1rem">
      <button disabled={!saved} onclick={() => goto("/chat")}>Continue</button>
    </p>
  </div>
{:else}
  <div class="card">
    <h1>Set up SmartBrain</h1>
    <p class="muted">Choose a passphrase. It encrypts everything on this device.</p>
    <form onsubmit={submit}>
      <label for="pp">Passphrase</label>
      <input id="pp" type="password" bind:value={passphrase} bind:this={ppInput} autocomplete="new-password" />
      <label for="cf">Confirm passphrase</label>
      <input id="cf" type="password" bind:value={confirm} bind:this={cfInput} autocomplete="new-password" />
      {#if error}<p class="error" role="alert">{error}</p>{/if}
      <p style="margin-top:1rem; display:flex; gap:0.5rem; align-items:center">
        <button disabled={busy} type="submit">{busy ? "Setting up…" : "Create vault"}</button>
        {#if busy}<Spinner size={16} /><span class="muted">Working…</span>{/if}
      </p>
    </form>
    <p class="muted" style="margin-top:0.75rem; font-size:0.85rem">
      New here? Read the <a href="/help">quick guide</a>.
    </p>
  </div>
{/if}

<style>
  .warn {
    border: 1px solid var(--danger, #c0392b);
    background: color-mix(in srgb, var(--danger, #c0392b) 10%, transparent);
    color: var(--text);
    padding: 0.75rem 1rem;
    border-radius: 8px;
    margin: 0.25rem 0 0.75rem;
  }
</style>
