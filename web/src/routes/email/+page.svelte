<script lang="ts">
  import { onMount, tick } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, ApiError, type EmailStatus, type EmailMessage } from "$lib/api";
  import { describeError } from "$lib/errors";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { remote } from "$lib/remote/connection.svelte";

  let status = $state<EmailStatus | null>(null);
  let messages = $state<EmailMessage[]>([]);
  let open = $state<EmailMessage | null>(null);
  let clientId = $state("");
  let clientSecret = $state("");
  let to = $state("");
  let subject = $state("");
  let body = $state("");
  let busy = $state(false);
  let error = $state("");
  let notice = $state("");
  let needsReconnect = $state(false); // refresh token died (401) — show a Reconnect banner

  // Element refs for focus management on failed submits + the open-email overlay (U3/U4).
  let clientIdEl = $state<HTMLInputElement | null>(null);
  let clientSecretEl = $state<HTMLInputElement | null>(null);
  let toEl = $state<HTMLInputElement | null>(null);
  let openCloseEl = $state<HTMLButtonElement | null>(null);
  // Remember which element opened the message so we can return focus on close (U4 a11y).
  let openReturnFocus: HTMLElement | null = null;

  // Translate the OAuth redirect `error` query value (Google's machine codes, or our own
  // backend strings) into a plain sentence. We never paint a raw code at the user (U17).
  function describeOAuthError(raw: string): string {
    console.assert(typeof raw === "string", "describeOAuthError: raw must be a string");
    const code = raw.trim().toLowerCase();
    console.assert(code.length < 200, "describeOAuthError: code unexpectedly long");
    if (!code) return "Couldn't connect Gmail. Please try again.";
    if (code === "access_denied") return "Gmail connection cancelled. You can try again any time.";
    if (code === "invalid_grant" || code === "invalid_request" || code === "invalid_client") {
      return "Gmail connection failed — your OAuth client ID or secret looks wrong. Double-check them and try again.";
    }
    if (code === "redirect_uri_mismatch") {
      return "Gmail connection failed — your OAuth client's redirect settings don't match. Make sure you created a Desktop app client (it needs no redirect URL), then try again.";
    }
    if (code === "admin_policy_enforced" || code === "disallowed_useragent") {
      return "Gmail connection blocked by a Google policy on this account. Try a personal Google account, or check with your admin.";
    }
    return "Couldn't connect Gmail. Please try again.";
  }

  async function load() {
    try {
      status = await api.emailStatus();
    } catch (err) {
      error = describeError(err);
      return;
    }
    if (!status.connected) return;
    try {
      messages = (await api.emailMessages(15)).messages;
      needsReconnect = false;
    } catch (err) {
      // A dead refresh token comes back 401 — guide a one-tap reconnect rather than
      // a raw error buried at the page bottom.
      if (err instanceof ApiError && err.status === 401) needsReconnect = true;
      else error = describeError(err);
    }
  }

  async function reconnect() {
    busy = true;
    error = "";
    try {
      const { auth_url } = await api.emailReconnect();
      window.location.href = auth_url; // re-grant on Google with the stored client creds
    } catch {
      // Don't leak raw backend strings — phrase the reconnect failure plainly (U17).
      error = "Couldn't start the Gmail reconnect. Please try again.";
      busy = false;
    }
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected")) notice = "Gmail connected.";
    const oauthErr = params.get("error");
    if (oauthErr) error = describeOAuthError(oauthErr);
    await load();
  });

  // Focus the first invalid field on a failed submit (U3 a11y).
  async function focusFirstInvalidConnect() {
    console.assert(typeof clientId === "string", "connect: clientId state must be a string");
    console.assert(typeof clientSecret === "string", "connect: clientSecret state must be a string");
    await tick();
    if (!clientId.trim()) clientIdEl?.focus();
    else if (!clientSecret.trim()) clientSecretEl?.focus();
  }

  async function connect(event: Event) {
    event.preventDefault();
    if (!clientId.trim() || !clientSecret.trim()) {
      error = "Enter your OAuth client ID and secret before connecting.";
      await focusFirstInvalidConnect();
      return;
    }
    busy = true;
    error = "";
    try {
      const { auth_url } = await api.emailConnect(clientId.trim(), clientSecret.trim());
      window.location.href = auth_url; // hand off to Google's consent screen
    } catch (err) {
      // 4xx from our backend is human-phrased; everything else gets a plain fallback (U17).
      error = err instanceof ApiError && err.status >= 400 && err.status < 500
        ? describeError(err)
        : "Couldn't start the Gmail connection. Please try again.";
      await focusFirstInvalidConnect();
      busy = false;
    }
  }

  async function disconnect() {
    const ok = await confirmDialog({
      title: "Disconnect Gmail",
      body: "Stored credentials will be removed.",
      confirmLabel: "Disconnect",
    });
    if (!ok) return;
    try {
      await api.emailDisconnect();
      messages = [];
      open = null;
      await load();
    } catch (err) {
      error = describeError(err);
    }
  }

  async function read(m: EmailMessage, event: MouseEvent) {
    console.assert(typeof m.id === "string" && m.id.length > 0, "read: message id required");
    console.assert(event instanceof Event, "read: event must be an Event");
    error = "";
    openReturnFocus = event.currentTarget as HTMLElement; // restore focus to opener on close
    try {
      open = await api.emailMessage(m.id);
      await tick();
      openCloseEl?.focus(); // U4: move focus into the opened-email overlay
    } catch (err) {
      open = null;
      error = describeError(err);
    }
  }

  function closeOpen() {
    console.assert(open !== null, "closeOpen: called with no open message");
    console.assert(openCloseEl === null || openCloseEl instanceof HTMLButtonElement, "closeOpen: ref invariant");
    open = null;
    openReturnFocus?.focus(); // U4: return focus to the row the user came from
    openReturnFocus = null;
  }

  function onOverlayKey(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeOpen();
    }
  }

  async function send(event: Event) {
    event.preventDefault();
    if (!to.trim() || !to.includes("@")) {
      error = "Enter a valid email address before sending.";
      await tick();
      toEl?.focus(); // U3: focus the first invalid field
      return;
    }
    busy = true;
    error = "";
    notice = "";
    try {
      await api.emailSend(to.trim(), subject, body);
      notice = `Sent to ${to.trim()}.`;
      to = subject = body = "";
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) needsReconnect = true;
      else {
        error = describeError(err);
        await tick();
        toEl?.focus(); // U3: focus the first field so the user can retry from the top
      }
    } finally {
      busy = false;
    }
  }
</script>

{#if account.status?.unlocked}
  <h1>Email</h1>

  {#if notice}<p class="notice">{notice}</p>{/if}

  {#if status && !status.connected && remote.status !== "idle"}
    <div class="card"><p class="muted">Email isn&rsquo;t connected yet. Gmail signs in through your computer, so set it up once on <strong>SmartBrain on your Desktop</strong> — after that, your mail shows up here automatically.</p></div>
  {/if}

  {#if status && !status.connected && remote.status === "idle"}
    <div class="card">
      <h2>Connect Gmail <span class="muted" style="font-weight:400; font-size:0.85rem">· optional</span></h2>
      <p class="muted">Read and send mail from SmartBrain. Your Google account stays on this device — nothing routes through us. Most people skip this and use SmartBrain without email.</p>
      <details style="margin-top:0.25rem">
        <summary class="muted" style="font-size:0.9rem; cursor:pointer">Set up Gmail</summary>
        <p class="muted" style="font-size:0.85rem; margin:0.5rem 0">A one-time Google setup (~3 min). SmartBrain asks for two permissions only — <strong>read your inbox</strong> and <strong>send mail as you</strong>; no deleting, archiving, or labels.</p>
        <ol class="muted" style="padding-left:1.25rem; line-height:1.7; margin:0.5rem 0">
          <li>
            Open
            <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">Google Cloud Console → Credentials</a>,
            then <strong>Create credentials → OAuth client ID</strong> and choose type <strong>Desktop app</strong>.
            <span style="display:block; font-size:0.85rem">A Desktop-app client needs no redirect URL — Google handles it.</span>
          </li>
          <li>
            On the <strong>OAuth consent screen</strong>, add the <code>gmail.readonly</code> and <code>gmail.send</code> scopes
            and set <strong>Publishing status</strong> to <strong>In production</strong> (otherwise Google signs you out every 7 days).
          </li>
          <li>Paste the client <strong>ID</strong> and <strong>secret</strong> below and connect.</li>
        </ol>
        <p class="muted" style="font-size:0.8rem; margin:0 0 0.5rem">When you connect, Google may say the app is &ldquo;unverified&rdquo; — it&rsquo;s your own client, so tap <strong>Advanced → Continue</strong>. <a href="/help#features">More on email setup</a>.</p>
        <form onsubmit={connect} style="display:flex; flex-direction:column; gap:0.5rem">
          <input bind:value={clientId} bind:this={clientIdEl} placeholder="Client ID" />
          <input bind:value={clientSecret} bind:this={clientSecretEl} type="password" placeholder="Client secret" />
          <button disabled={busy || !clientId.trim() || !clientSecret.trim()} type="submit">
            {busy ? "Redirecting…" : "Connect Gmail"}
          </button>
        </form>
      </details>
    </div>
  {/if}

  {#if status?.connected}
    {#if needsReconnect}
      <div class="card" style="border-color:var(--warn,#b9770e)">
        <h2 style="margin-top:0">Gmail needs reconnecting</h2>
        <p class="muted">
          Google signed SmartBrain out of your mail. Reconnect to restore it — one click, and you
          won&rsquo;t need to re-enter your client ID or secret.
        </p>
        {#if remote.status === "idle"}
          <button disabled={busy} onclick={reconnect}>{busy ? "Redirecting…" : "Reconnect Gmail"}</button>
        {:else}
          <p class="muted">Reconnect once from SmartBrain on your Desktop — it&rsquo;ll start working here again automatically.</p>
        {/if}
      </div>
    {/if}

    <div class="card" style="display:flex; align-items:center; gap:0.5rem">
      <span>Connected as <strong>{status.address}</strong></span>
      <span class="spacer"></span>
      <button class="secondary" onclick={disconnect}>Disconnect</button>
    </div>

    <div class="card">
      <h2>Compose</h2>
      <form onsubmit={send} style="display:flex; flex-direction:column; gap:0.5rem">
        <input bind:value={to} bind:this={toEl} placeholder="To (email address)" aria-label="To (email address)" />
        <input bind:value={subject} placeholder="Subject" aria-label="Subject" />
        <textarea bind:value={body} rows="4" placeholder="Message…" aria-label="Message body"></textarea>
        <button disabled={busy || !to.includes('@')} type="submit">Send</button>
      </form>
    </div>

    <div class="card">
      <h2>Inbox <span class="muted" style="font-weight:400">· {messages.length}</span></h2>
      {#each messages as m (m.id)}
        <div style="border-top:1px solid var(--border, #2a2a2a); padding:0.5rem 0">
          <button class="link" style="text-align:left; width:100%" onclick={(e) => read(m, e)}>
            <strong>{m.subject || "(no subject)"}</strong>
            <span class="muted"> — {m.from}</span>
            {#if m.snippet}<div class="muted" style="font-size:0.9em">{m.snippet}</div>{/if}
          </button>
        </div>
      {/each}
      {#if messages.length === 0}<p class="muted">No messages.</p>{/if}
    </div>
  {/if}

  {#if error}<p class="error" role="alert">{error}</p>{/if}
{:else}
  <p class="muted">Loading&hellip;</p>
{/if}

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="email-overlay" role="none" onclick={closeOpen}>
    <div
      class="email-dialog"
      role="dialog"
      aria-modal="true"
      aria-label={open.subject || "(no subject)"}
      tabindex="-1"
      onkeydown={onOverlayKey}
      onclick={(e) => e.stopPropagation()}
    >
      <header class="email-dialog-header">
        <div style="min-width:0">
          <strong style="display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">{open.subject || "(no subject)"}</strong>
          <span class="muted" style="font-size:0.85rem">{open.from}</span>
        </div>
        <button class="secondary" bind:this={openCloseEl} onclick={closeOpen} aria-label="Close message">Close</button>
      </header>
      <div class="email-dialog-body">
        <pre style="white-space:pre-wrap; margin:0">{open.body || "(empty)"}</pre>
      </div>
    </div>
  </div>
{/if}

<style>
  .email-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 900;
    padding: 1rem;
  }
  .email-dialog {
    background: var(--panel);
    color: var(--text, #eee);
    border: 1px solid var(--border, #333);
    border-radius: 12px;
    width: 100%;
    max-width: 40rem;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.4);
  }
  /* Header is sticky inside the dialog so Close stays reachable on long bodies (U4). */
  .email-dialog-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border, #2a2a2a);
    position: sticky;
    top: 0;
    background: var(--panel);
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
  }
  .email-dialog-header > div {
    flex: 1;
  }
  .email-dialog-body {
    overflow: auto;
    padding: 0.75rem 1rem 1rem;
  }
</style>
