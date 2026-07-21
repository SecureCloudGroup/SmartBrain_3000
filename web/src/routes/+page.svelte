<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import Spinner from "$lib/components/Spinner.svelte";

  // Dispatcher: route to setup / unlock based on vault state; Chat is the home page.
  async function decide() {
    await account.load();
    const s = account.status;
    if (!s) return; // load failed — the retry UI below is shown
    if (!s.initialized) goto("/setup");
    else if (!s.unlocked) goto("/unlock");
    else goto("/chat");
  }

  onMount(decide);
</script>

{#if account.status === null && account.error}
  <div class="card">
    <h1>Can&rsquo;t reach SmartBrain</h1>
    <p class="muted">{account.error}</p>
    <p style="margin-top:1rem"><button onclick={decide}>Retry</button></p>
  </div>
{:else}
  <Spinner block />
{/if}
