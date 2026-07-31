<script lang="ts">
  import "../app.css";
  import { onMount, onDestroy } from "svelte";
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
  // SmartBrain updates itself underneath an open tab, which leaves the page — and the version
  // under the logo — showing the OLD build until someone reloads. That reads as "the update
  // didn't work" (it happened twice in one day). Notice it and say so plainly instead.
  let updatedVersion = $state(""); // set when the backend reports a version we didn't load with
  let launcherNudge = $state(false); // one-time "update the desktop app" banner (legacy launchers only)
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
    { href: "/info", label: "Info", icon: "info", remote: true },
    { href: "/activity", label: "Activity", icon: "activity", remote: true },
    { href: "/usage", label: "Usage", icon: "chart", remote: false },
    { href: "/settings", label: "Settings", icon: "sliders", remote: false },
  ];
  // Desktop is the primary surface (status "idle" -> full nav); a paired phone (remote
  // session) shows only the consume-on-the-go pages.
  const remoteSession = $derived(remote.status !== "idle");
  const nav = $derived(remoteSession ? NAV.filter((n) => n.remote) : NAV);
  // Mobile: the four thumb-zone tabs (plus More); everything else lives in the More sheet.
  const TAB_HREFS = ["/chat", "/knowledge", "/info", "/activity"];
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

  let updateTimer: ReturnType<typeof setInterval> | null = null;
  // A version the desktop launcher has downloaded and can install. Shown here because the
  // menu-bar item was the only place it ever appeared, so most people never saw it.
  let updateReady = $state("");
  let installing = $state(false);
  let installFailed = $state(false);

  // Registered during initialisation (the only place Svelte allows it) so the version poll
  // cannot outlive the page.
  onDestroy(() => {
    if (updateTimer) clearInterval(updateTimer);
  });

  onMount(async () => {
    initTheme();
    watchForSWUpdate(); // pick up a freshly deployed service worker (iOS keeps the old one)
    // Set up remote mode FIRST so the page's fetch override relays /api over WebRTC before
    // account.load() makes its first request (off the LAN there's no direct backend).
    await initRemote();
    account.load();
    // Show the running version under the logo (best-effort — render nothing if health is unreachable).
    try {
      const health = await api.health();
      appVersion = displayVersion(health.version);
      loadedVersion = health.version;
      noteUpdate(health.update_ready);
      watchForAppUpdate();
      // One-time nudge for desktop apps too old to update themselves (they predate the
      // self-update channel, so only the user can perform this last manual update).
      launcherNudge = !!health.launcher_update_needed &&
        sessionStorage.getItem("launcher-nudge-dismissed") !== "1";
    } catch (err) {
      // Leave the version hidden rather than surface a broken "v" — but SAY something. An
      // empty catch here swallowed a genuine programming error for a full release: a throw
      // from watchForAppUpdate silently skipped the lines below it, and the missing banner
      // looked like a design choice rather than a bug.
      console.warn("startup health check failed:", err);
    }
  });

  // The version this tab loaded with; a change means the app updated under our feet.
  let loadedVersion = "";

  // Notice an update that landed while this tab was open. Cheap: one health call a minute,
  // the same request the page already makes on load, and it stops as soon as it fires.
  //
  // The cleanup is registered at the TOP of this component, not here: onDestroy must run
  // during component initialisation, and this function is called from onMount AFTER an
  // await, where that context is gone. Calling it here threw — silently, into onMount's
  // catch — which killed every line after it, including the legacy-launcher banner.
  // Remember a staged version unless it was already waved away. Dismissal is per version,
  // so "not now" today does not silence a genuinely newer release tomorrow.
  function noteUpdate(version: string | undefined): void {
    if (!version) { updateReady = ""; return; }
    if (localStorage.getItem("update-dismissed") === version) return;
    updateReady = version;
  }

  async function startInstall(): Promise<void> {
    installing = true;
    installFailed = false;
    try {
      await api.installUpdate();
      pollFasterWhileInstalling(); // the desktop app acts within ~30s, then restarts
    } catch {
      installing = false;
      installFailed = true; // the launcher may be closed; the menu bar is the other way in
    }
  }

  // While an install runs the backend goes away and returns on a new version. Checking once
  // a minute would leave the page looking stuck for most of it.
  function pollFasterWhileInstalling(): void {
    if (updateTimer) clearInterval(updateTimer);
    updateTimer = setInterval(async () => {
      try {
        const health = await api.health();
        if (loadedVersion && health.version && health.version !== loadedVersion) {
          location.reload(); // asked for here, so no second click to finish it
        }
      } catch {
        /* the restart is in progress — it will answer again shortly */
      }
    }, 2_000);
  }

  function watchForAppUpdate(): void {
    updateTimer = setInterval(async () => {
      try {
        const health = await api.health();
        noteUpdate(health.update_ready);
        if (loadedVersion && health.version && health.version !== loadedVersion) {
          updatedVersion = displayVersion(health.version);
          if (updateTimer) clearInterval(updateTimer); // said once; the banner stays until the reload
        }
      } catch {
        /* offline or restarting — try again next minute */
      }
    }, 60_000);
  }

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
      {#if updateReady && !updatedVersion}
        <div class="launcher-nudge">
          {#if installing}
            <span>Installing SmartBrain {displayVersion(updateReady)}. It restarts in a moment
              and this page will come back on its own.</span>
          {:else if installFailed}
            <span>Couldn&rsquo;t reach the desktop app to start the install. Open the SmartBrain
              icon in your menu bar and choose <strong>Install update now</strong>.</span>
            <button class="nudge-dismiss" title="Hide"
              onclick={() => { installFailed = false; updateReady = ""; }}>✕</button>
          {:else}
            <span>SmartBrain <strong>{displayVersion(updateReady)}</strong> is ready to install.
              Installing restarts it — under a minute, and you&rsquo;ll unlock again afterwards.</span>
            <button onclick={startInstall}>Install now</button>
            <button class="nudge-dismiss" title="Not now"
              onclick={() => { localStorage.setItem("update-dismissed", updateReady); updateReady = ""; }}>✕</button>
          {/if}
        </div>
      {/if}
      {#if updatedVersion}
        <div class="launcher-nudge">
          <span>SmartBrain updated to <strong>{updatedVersion}</strong> while this page was
            open — reload to use the new version.</span>
          <button onclick={() => location.reload()}>Reload</button>
          <button class="nudge-dismiss" title="Keep using this page"
            onclick={() => { updatedVersion = ""; }}>✕</button>
        </div>
      {/if}
      {#if launcherNudge}
        <!-- Pre-self-update desktop apps can't reach new capabilities on their own; this is
             the ONE manual update a user ever does. Dismiss lasts the session; the flag
             clears itself forever once a modern launcher talks to the backend. -->
        <div class="launcher-nudge">
          <span>Your SmartBrain <strong>desktop app</strong> needs a one-time update to keep
            updating itself automatically.</span>
          <a href="https://github.com/SecureCloudGroup/SmartBrain_3000/releases/latest"
             target="_blank" rel="noreferrer">Download the new app</a>
          <span class="muted">or run <code>brew upgrade --cask smartbrain</code> /
            <code>scoop update smartbrain</code></span>
          <button class="nudge-dismiss" title="Hide for this session"
            onclick={() => { launcherNudge = false; sessionStorage.setItem("launcher-nudge-dismissed", "1"); }}>
            ✕</button>
        </div>
      {/if}
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
