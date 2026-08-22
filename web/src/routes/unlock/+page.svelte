<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, ApiError } from "$lib/api";

  let mode = $state<"passphrase" | "recovery">("passphrase");
  let value = $state("");
  let error = $state("");
  let busy = $state(false);
  let watchTimer: ReturnType<typeof setInterval> | null = null;

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) goto("/setup");
    else if (s?.unlocked) goto("/chat");
    // One vault, one lock: unlocking from ANY device (the phone, another tab) unlocks
    // it here too — so this screen watches for that and walks in on its own instead of
    // sitting locked until a manual refresh. Light poll, cleared on unmount.
    watchTimer = setInterval(async () => {
      try {
        if ((await api.accountStatus()).unlocked) {
          if (watchTimer) clearInterval(watchTimer);
          watchTimer = null;
          await account.load();
          goto("/chat");
        }
      } catch {
        /* backend restarting or offline — keep watching */
      }
    }, 3_000);
  });

  onDestroy(() => {
    if (watchTimer) clearInterval(watchTimer);
    watchTimer = null;
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
  <p class="muted" style="margin-top:0.75rem; font-size:0.85rem">
    One vault, one lock: unlocking here also unlocks your paired phone — and unlocking
    there unlocks here, on its own.
  </p>
</div>
