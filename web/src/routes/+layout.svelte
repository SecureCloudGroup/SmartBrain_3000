<script lang="ts">
  import "../app.css";
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import { account } from "$lib/account.svelte";
  import { api } from "$lib/api";
  import { displayVersion } from "$lib/version";
  import { theme, initTheme, cycleTheme } from "$lib/theme.svelte";
  import { pending, refreshPending } from "$lib/pending.svelte";
  import { scheduleUpdates, refreshScheduleUpdates } from "$lib/scheduleUpdates.svelte";
  import { initRemote, watchForSWUpdate } from "$lib/remote/sw-bridge";
  import { clearPairing } from "$lib/remote/store";
  import { remote } from "$lib/remote/connection.svelte"; // nav gating + remote status
  import RemoteStatus from "$lib/components/RemoteStatus.svelte";
  import PairSetup from "$lib/components/PairSetup.svelte";
  import Confirm from "$lib/components/Confirm.svelte";
  import Toast from "$lib/components/Toast.svelte";
  import Icon from "$lib/components/Icon.svelte";
  import Chip from "$lib/components/Chip.svelte";
  import type { IconName } from "$lib/icons";
  import { confirmDialog } from "$lib/confirm.svelte";

  let { children } = $props();
  let locking = $state(false);
  let appVersion = $state(""); // "vX.Y.Z" once /api/health answers; "" (hidden) until then / on failure
  let moreOpen = $state(false); // mobile: the More sheet above the tab bar
  // Chat + Help get the full-width container (chat for the log, help for its own
  // two-column nav+article layout, which caps itself); everything else uses the column.
  const wide = $derived(["/chat", "/help"].some((p) => page.url.pathname.startsWith(p)));

  // `remote: true` = shown on a paired phone; the rest are Desktop-only setup/review pages.
  // The single source of truth for BOTH the desktop sidebar and the mobile tabs/More sheet.
  const NAV: { href: string; label: string; icon: IconName; remote: boolean }[] = [
    { href: "/chat", label: "Chat", icon: "chat", remote: true },
    { href: "/knowledge", label: "Knowledge", icon: "book", remote: true },
    { href: "/planner", label: "Planner", icon: "tasks", remote: true },
    { href: "/schedules", label: "Schedules", icon: "clock", remote: true },
    { href: "/email", label: "Email", icon: "mail", remote: true },
    { href: "/activity", label: "Activity", icon: "activity", remote: true },
    { href: "/usage", label: "Usage", icon: "chart", remote: false },
    { href: "/settings", label: "Settings", icon: "sliders", remote: false },
  ];
  // Desktop is the primary surface (status "idle" -> full nav); a paired phone (remote
  // session) shows only the consume-on-the-go pages.
  const remoteSession = $derived(remote.status !== "idle");
  const nav = $derived(remoteSession ? NAV.filter((n) => n.remote) : NAV);
  // Mobile: the three thumb-zone tabs; everything else lives in the More sheet.
  const TAB_HREFS = ["/chat", "/knowledge", "/activity"];
  const tabNav = $derived(nav.filter((n) => TAB_HREFS.includes(n.href)));
  const moreNav = $derived(nav.filter((n) => !TAB_HREFS.includes(n.href)));
  const isActive = (href: string) =>
    page.url.pathname === href || page.url.pathname.startsWith(href + "/");
  // The sidebar appearing only after account.load() resolves shifted the whole main
  // area on cold load (CLS 0.155, measured in the Stage-13 sweep). Until the real
  // status arrives, fall back to the last session's nav state — wrong at most for one
  // frame after a lock elsewhere, and nav labels are not sensitive.
  const navHint = typeof localStorage !== "undefined" && localStorage.getItem("sbNav") === "1";
  const showNav = $derived(account.status ? Boolean(account.status.unlocked) : navHint);
  $effect(() => {
    if (account.status) localStorage.setItem("sbNav", account.status.unlocked ? "1" : "0");
  });

  const THEME_ICON = { system: "sun-moon", light: "sun", dark: "moon" } as const;

  const badgeFor = (href: string) =>
    href === "/activity" && pending.count > 0 ? pending.count
    : href === "/chat" && scheduleUpdates.count > 0 ? scheduleUpdates.count
    : 0;
  const badgeTitle = (href: string) =>
    href === "/activity" ? `${pending.count} awaiting approval` : `${scheduleUpdates.count} new scheduled updates`;

  // When the backend is unreachable, account.load() sets account.error with no
  // status — show a recoverable card instead of an indefinite per-page "Loading…".
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
    // Show the running version under the logo (best-effort — render nothing if health is unreachable).
    try {
      appVersion = displayVersion((await api.health()).version);
    } catch {
      // leave the version hidden rather than surface a broken "v"
    }
  });

  // Keep the Activity badge fresh: refresh the pending count on each route change
  // while unlocked (cheap, no timer; Chat + Activity also nudge it directly).
  $effect(() => {
    const path = page.url.pathname; // track navigation
    moreOpen = false; // navigating away always closes the sheet
    if (account.status?.unlocked) {
      refreshPending();
      // The Chat page pulls unseen updates into the conversation + clears the badge itself, so
      // skip the refresh on /chat — otherwise a stale in-flight unseen-count response could land
      // after the page cleared it and wrongly re-light the badge.
      if (!path.startsWith("/chat")) refreshScheduleUpdates();
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

<!-- Dismiss the mobile More sheet on Escape or an outside click (a11y: it otherwise
     trapped focus with no keyboard/pointer way out). Clicks inside the sheet or on its
     toggle are ignored so the toggle still opens/closes it. -->
<svelte:window
  onkeydown={(e) => { if (e.key === "Escape") moreOpen = false; }}
  onclick={(e) => {
    if (moreOpen && !(e.target as HTMLElement).closest(".more-sheet, .tab-more")) moreOpen = false;
  }}
/>

{#snippet brand()}
  <span class="brand">
    <img class="logo" src="/icons/mark-64.png" alt="SmartBrain" />
    <span class="titlewrap">
      <span class="title">SmartBrain_3000</span>
      {#if appVersion}<span class="appversion">{appVersion}</span>{/if}
    </span>
  </span>
{/snippet}

{#snippet controls()}
  <a class="navitem" href="/help"><Icon name="help" /> Help</a>
  <button class="navitem" title={`Theme: ${theme.mode}`} aria-label={`Theme: ${theme.mode}. Click to change.`} onclick={cycleTheme}>
    <Icon name={THEME_ICON[theme.mode]} /> Theme
  </button>
  {#if remoteSession}
    <button class="navitem" title="Forget this device's pairing" onclick={unpairDevice}><Icon name="link" /> Unpair</button>
  {/if}
  {#if account.status?.unlocked}
    <button class="navitem" disabled={locking} onclick={lock}><Icon name="lock" /> {locking ? "Locking…" : "Lock"}</button>
  {/if}
{/snippet}

{#if remote.status === "untrusted"}
  <!-- Possible MITM — render a full-width blocking banner so a phone user can't miss it. -->
  <div class="remote-banner" role="alert">
    Remote connection BLOCKED — couldn't verify your Desktop's identity. Re-pair if you reinstalled.
  </div>
{/if}

<div class="shell" class:with-side={showNav}>
  {#if showNav}
    <aside class="sidebar">
      {@render brand()}
      <nav class="side-nav" aria-label="Primary">
        {#each nav as n (n.href)}
          <a class="navitem" href={n.href} class:active={isActive(n.href)} aria-current={isActive(n.href) ? "page" : undefined}>
            <Icon name={n.icon} /> {n.label}
            {#if badgeFor(n.href) > 0}<span class="nav-badge" title={badgeTitle(n.href)}>{badgeFor(n.href)}</span>{/if}
          </a>
        {/each}
      </nav>
      <div class="side-foot">
        {@render controls()}
      </div>
    </aside>
  {/if}

  <div class="shell-main">
    <header class="topstrip" class:no-side={!showNav}>
      {@render brand()}
      <span class="spacer"></span>
      {#if showNav && !remoteSession}
        <Chip icon="shield" kind="ok" title="Your data is encrypted at rest and never leaves this machine">Encrypted · On-device</Chip>
      {/if}
      <RemoteStatus />
      {#if !showNav}
        <a class="navitem" href="/help"><Icon name="help" /> Help</a>
      {/if}
    </header>

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
  </div>
</div>

{#if showNav}
  <nav class="tabbar" aria-label="Primary">
    {#each tabNav as n (n.href)}
      <a class="tab" href={n.href} class:active={isActive(n.href)} aria-current={isActive(n.href) ? "page" : undefined}>
        {#if badgeFor(n.href) > 0}<span class="tab-dot" title={badgeTitle(n.href)}>{badgeFor(n.href)}</span>{/if}
        <Icon name={n.icon} size={20} /> {n.label}
      </a>
    {/each}
    <button class="tab tab-more" aria-expanded={moreOpen} onclick={() => (moreOpen = !moreOpen)}>
      <Icon name="more-horizontal" size={20} /> More
    </button>
  </nav>
  {#if moreOpen}
    <div class="more-sheet" role="menu" tabindex="-1">
      {#each moreNav as n (n.href)}
        <a class="navitem" href={n.href} class:active={isActive(n.href)}><Icon name={n.icon} /> {n.label}</a>
      {/each}
      <div class="sheet-divider"></div>
      {@render controls()}
    </div>
  {/if}
{/if}

<Confirm />
<Toast />
