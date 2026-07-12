<script lang="ts">
  import "../app.css";
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import { account } from "$lib/account.svelte";
  import { api } from "$lib/api";
  import { theme, initTheme, cycleTheme } from "$lib/theme.svelte";
  import { pending, refreshPending } from "$lib/pending.svelte";
  import { scheduleUpdates, refreshScheduleUpdates } from "$lib/scheduleUpdates.svelte";
  import { initRemote, watchForSWUpdate } from "$lib/remote/sw-bridge";
  import { clearPairing } from "$lib/remote/store";
  import { remote } from "$lib/remote/connection.svelte"; // nav gating + remote status
  import RemoteStatus from "$lib/components/RemoteStatus.svelte";
  import PairSetup from "$lib/components/PairSetup.svelte";
  import Confirm from "$lib/components/Confirm.svelte";
  import { confirmDialog } from "$lib/confirm.svelte";

  let { children } = $props();
  let locking = $state(false);
  let overflowOpen = $state(false); // mobile ≤640px: secondary controls collapse into ⋯ menu
  // Chat gets the full-width container; everything else stays in the narrow column.
  const wide = $derived(page.url.pathname.startsWith("/chat"));

  // `remote: true` = shown on a paired phone; the rest are Desktop-only setup/review pages.
  const NAV = [
    { href: "/chat", label: "Chat", remote: true },
    { href: "/knowledge", label: "Knowledge", remote: true },
    { href: "/planner", label: "Planner", remote: true },
    { href: "/schedules", label: "Schedules", remote: true },
    { href: "/email", label: "Email", remote: true },
    { href: "/activity", label: "Activity", remote: true },
    { href: "/usage", label: "Usage", remote: false },
    { href: "/settings", label: "Settings", remote: false },
  ];
  // Desktop is the primary surface (status "idle" -> full nav); a paired phone (remote
  // session) shows only the consume-on-the-go pages.
  const remoteSession = $derived(remote.status !== "idle");
  const nav = $derived(remoteSession ? NAV.filter((n) => n.remote) : NAV);
  const isActive = (href: string) =>
    page.url.pathname === href || page.url.pathname.startsWith(href + "/");

  const THEME_ICON = { system: "🌓", light: "☀", dark: "🌙" };

  // When the backend is unreachable, account.load() sets account.error with no
  // status — show a recoverable card instead of an indefinite per-page "Loading…".
  // /help is bundled (works offline), so never block it.
  // /help and /pair work without a backend (bundled help; pairing stores a payload),
  // so never block them with the outage card.
  const offline = $derived(["/help", "/pair"].some((p) => page.url.pathname.startsWith(p)));
  const outage = $derived(Boolean(account.error) && account.status === null && !offline);
  // A fresh phone / installed app off the LAN with no pairing: show a friendly pairing
  // welcome instead of the "can't reach" outage card.
  const needsPairing = $derived(remote.needsPairing && !offline);
  // A paired phone whose connection attempts were exhausted (status "offline" + a reason):
  // show the reason + Retry instead of a perpetual "connecting…" / blank "Loading…".
  const remoteDown = $derived(remote.status === "offline" && !offline && !needsPairing);

  onMount(async () => {
    initTheme();
    watchForSWUpdate(); // pick up a freshly deployed service worker (iOS keeps the old one)
    // Set up remote mode FIRST so the page's fetch override relays /api over WebRTC before
    // account.load() makes its first request (off the LAN there's no direct backend).
    await initRemote();
    account.load();
  });

  // Keep the Activity badge fresh: refresh the pending count on each route change
  // while unlocked (cheap, no timer; Chat + Activity also nudge it directly).
  $effect(() => {
    const path = page.url.pathname; // track navigation
    if (account.status?.unlocked) {
      refreshPending();
      // The Scheduled updates feed owns its badge while open (it marks runs seen + zeroes the
      // count), so skip the refresh there — otherwise a stale in-flight unseen-count response
      // could land after the page cleared it and wrongly re-light the badge.
      if (!path.startsWith("/chat/updates")) refreshScheduleUpdates();
    }
  });

  async function unpairDevice() {
    console.assert(typeof clearPairing === "function", "clearPairing import missing");
    console.assert(typeof confirmDialog === "function", "confirmDialog import missing");
    const ok = await confirmDialog({
      title: "Unpair device",
      body: "You'll need to pair again with a code to use SmartBrain here.",
      confirmLabel: "Unpair",
      danger: true,
    });
    if (!ok) return;
    await clearPairing();
    window.location.assign("/"); // full reload -> unpaired -> pair-with-code flow
  }

  async function lock() {
    locking = true;
    try {
      await api.lock();
    } catch {
      // ignore — reload status below to reflect server truth either way
    } finally {
      await account.load();
      locking = false;
      goto("/unlock"); // defense in depth: leave the unlocked views regardless
    }
  }
</script>

<!-- Dismiss the mobile ⋯ overflow menu on Escape or an outside click (a11y: it otherwise
     trapped focus with no keyboard/pointer way out). Clicks inside .appbar-overflow — including
     the toggle — are ignored so the toggle still opens/closes it. -->
<svelte:window
  onkeydown={(e) => { if (e.key === "Escape") overflowOpen = false; }}
  onclick={(e) => {
    if (overflowOpen && !(e.target as HTMLElement).closest(".appbar-overflow")) overflowOpen = false;
  }}
/>

{#if remote.status === "untrusted"}
  <!-- Possible MITM — render a full-width blocking banner so a phone user can't miss it. -->
  <div class="remote-banner" role="alert">
    Remote connection BLOCKED — couldn't verify your Desktop's identity. Re-pair if you reinstalled.
  </div>
{/if}

<header class="appbar">
  <img class="logo" src="/icons/icon-192.png" alt="SmartBrain" />
  <span class="title">SmartBrain_3000</span>
  {#if account.status?.unlocked}
    <nav class="appnav">
      {#each nav as n (n.href)}
        <a href={n.href} class:active={isActive(n.href)} aria-current={isActive(n.href) ? "page" : undefined}>
          {n.label}{#if n.href === "/activity" && pending.count > 0}<span class="nav-badge" title="{pending.count} awaiting approval">{pending.count}</span>{/if}{#if n.href === "/chat" && scheduleUpdates.count > 0}<span class="nav-badge" title="{scheduleUpdates.count} new scheduled updates">{scheduleUpdates.count}</span>{/if}
        </a>
      {/each}
    </nav>
  {/if}
  <span class="spacer"></span>
  <RemoteStatus />
  <!-- Desktop (>640px): secondary controls visible inline. Mobile (≤640px): collapsed into ⋯ menu below. -->
  <div class="appbar-secondary">
    <a class="help-link" href="/help">Help</a>
    <button
      class="theme-toggle"
      title={`Theme: ${theme.mode}`}
      aria-label={`Theme: ${theme.mode}. Click to change.`}
      onclick={cycleTheme}
    >{THEME_ICON[theme.mode]}</button>
    {#if remoteSession}
      <button class="secondary" onclick={unpairDevice} title="Forget this device's pairing">Unpair</button>
    {/if}
    {#if account.status?.unlocked}
      <button class="secondary" disabled={locking} onclick={lock}>{locking ? "Locking…" : "Lock"}</button>
    {/if}
  </div>
  <div class="appbar-overflow">
    <button
      class="secondary overflow-toggle"
      aria-label="More controls"
      aria-expanded={overflowOpen}
      title="More"
      onclick={() => (overflowOpen = !overflowOpen)}
    >⋯</button>
    {#if overflowOpen}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div class="overflow-menu" role="menu" tabindex="-1" onclick={() => (overflowOpen = false)}>
        <a class="help-link" href="/help">Help</a>
        <button
          class="theme-toggle"
          title={`Theme: ${theme.mode}`}
          aria-label={`Theme: ${theme.mode}. Click to change.`}
          onclick={cycleTheme}
        >{THEME_ICON[theme.mode]} Theme</button>
        {#if remoteSession}
          <button class="secondary" onclick={unpairDevice} title="Forget this device's pairing">Unpair</button>
        {/if}
        {#if account.status?.unlocked}
          <button class="secondary" disabled={locking} onclick={lock}>{locking ? "Locking…" : "Lock"}</button>
        {/if}
      </div>
    {/if}
  </div>
</header>

<Confirm />

<main class:wrap={!wide} class:wrap-wide={wide}>
  {#if needsPairing}
    <PairSetup />
  {:else if remoteDown}
    <div class="card">
      <h1>Can&rsquo;t reach your Desktop</h1>
      <p class="muted">{remote.detail || "The remote connection couldn't be established."}</p>
      <p style="margin-top:1rem"><button onclick={() => window.location.reload()}>Retry</button></p>
      <p class="muted" style="margin-top:1rem">Still stuck? <a href="/pair">Re-pair this device</a>.</p>
    </div>
  {:else if outage}
    <div class="card">
      <h1>Can&rsquo;t reach SmartBrain</h1>
      <p class="muted">{account.error}</p>
      <p style="margin-top:1rem"><button onclick={() => account.load()}>Retry</button></p>
      <p class="muted" style="margin-top:1rem">First time on this device? <a href="/pair">Pair with a code</a>.</p>
    </div>
  {:else}
    {@render children()}
  {/if}
</main>
