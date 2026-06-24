<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import { account } from "$lib/account.svelte";
  import { remote } from "$lib/remote/connection.svelte";

  let { children } = $props();

  // Settings is Desktop-only (setup/config). On a paired phone, show an explanatory
  // card instead of a silent redirect so the user understands where to manage this.
  const remoteSession = $derived(remote.status !== "idle");

  // Guard non-remote sessions: ensure account is loaded + unlocked.
  onMount(async () => {
    if (remote.status !== "idle") return; // remote: render the "manage on Desktop" card below
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) goto("/setup");
    else if (s && !s.unlocked) goto("/unlock");
  });

  const tabs = [
    { href: "/settings/providers", label: "Cloud providers" },
    { href: "/settings/models", label: "Local models" },
    { href: "/settings/router", label: "Model routing" },
    { href: "/settings/memory", label: "Memory" },
    { href: "/settings/mcp", label: "Connections (MCP)" },
    { href: "/settings/devices", label: "Remote access" },
    { href: "/settings/account", label: "Account & Data" },
  ];
</script>

{#if remoteSession}
  <div class="card">
    <h1>Manage this on your Desktop</h1>
    <p class="muted">
      Settings (providers, models, routing, memory, connections, devices, account) are configured on
      your SmartBrain Desktop. Open SmartBrain on that machine to make changes here.
    </p>
    <p style="margin-top:1rem"><a href="/chat">Back to Chat</a></p>
  </div>
{:else if account.status?.unlocked}
  <nav class="tabs">
    {#each tabs as t (t.href)}
      <a href={t.href} class:active={page.url.pathname === t.href}>{t.label}</a>
    {/each}
  </nav>
  <!-- Children mount only once unlocked, so their API calls never 423-flash. -->
  {@render children()}
{:else}
  <p class="muted">Loading&hellip;</p>
{/if}
