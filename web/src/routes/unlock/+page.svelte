<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, ApiError } from "$lib/api";

  let mode = $state<"passphrase" | "recovery">("passphrase");
  let value = $state("");
  let error = $state("");
  let busy = $state(false);

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) goto("/setup");
    else if (s?.unlocked) goto("/chat");
  });

  async function submit(event: Event) {
    event.preventDefault();
    error = "";
    busy = true;
    try {
      await api.unlock(mode === "passphrase" ? { passphrase: value } : { recovery_key: value });
      await account.load();
      goto("/chat");
    } catch (err) {
      error = err instanceof ApiError && err.status === 401 ? "Incorrect credentials." : "Unlock failed.";
    } finally {
      busy = false;
    }
  }

  function toggle() {
    mode = mode === "passphrase" ? "recovery" : "passphrase";
    value = "";
    error = "";
  }
</script>

<div class="card">
  <h1>Unlock</h1>
  <form onsubmit={submit}>
    <label for="v">{mode === "passphrase" ? "Passphrase" : "Recovery key"}</label>
    <input id="v" type="password" bind:value autocomplete="current-password" />
    {#if error}<p class="error">{error}</p>{/if}
    <p style="margin-top:1rem; display:flex; gap:0.5rem; flex-wrap:wrap">
      <button disabled={busy || !value} type="submit">{busy ? "Unlocking…" : "Unlock"}</button>
      <button type="button" class="secondary" onclick={toggle}>
        Use {mode === "passphrase" ? "recovery key" : "passphrase"}
      </button>
    </p>
  </form>
</div>
