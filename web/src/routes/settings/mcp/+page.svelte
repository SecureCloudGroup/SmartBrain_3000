<script lang="ts">
  import { onMount } from "svelte";
  import { api, type NiMcpServer } from "$lib/api";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";

  let endpoint = $state("/mcp");
  let token = $state<string | null>(null);
  let busy = $state(false);
  let error = $state("");
  let copied = $state(false);

  async function load() {
    try {
      const info = await api.mcpInfo();
      endpoint = info.endpoint;
      token = (await api.mcpToken()).token;
    } catch (err) {
      error = describeError(err);
    }
  }
  onMount(load);

  async function mint() {
    busy = true;
    error = "";
    try {
      token = (await api.mcpNewToken()).token;
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function revoke() {
    busy = true;
    error = "";
    try {
      await api.mcpRevokeToken();
      token = null;
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function copy() {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {
      /* clipboard unavailable — the user can select the text */
    }
  }

  const fullUrl = $derived(
    typeof window !== "undefined" ? `${window.location.origin}${endpoint}/` : `${endpoint}/`,
  );

  // --- Outbound MCP servers (ni-format §22) --------------------------------------------------
  // A registry of MCP servers the operator explicitly configured so Neural Interface cards can
  // call their tools as a source. Every write is Desktop-local (x-sb-local); the /settings tree
  // is already hidden behind the "manage on your Desktop" card for remote sessions, so no extra
  // gate here — a paired phone never sees this form. Add-form state lives beside the list.
  let servers = $state<NiMcpServer[]>([]);
  let outboundLoaded = $state(false);
  let outboundError = $state("");
  // Per-row transient flags so a rapid double-click on Delete or the enabled toggle can never
  // fire two writes for the same row (also keys the button label to "Working…").
  let rowBusy = $state<string | null>(null);
  // Add-form fields — kept as strings; a numeric transport is neither meaningful nor safe.
  let addLabel = $state("");
  let addTransport = $state<"stdio" | "http">("stdio");
  let addCommand = $state("");
  // One arg per line — the user's own MCP config style. Blank lines drop out on submit.
  let addArgsText = $state("");
  let addUrl = $state("");
  let addBusy = $state(false);
  let addError = $state("");

  async function loadServers(): Promise<void> {
    console.assert(typeof api.niMcpServers === "function", "loadServers: niMcpServers method present");
    console.assert(Array.isArray(servers), "loadServers: servers state is array");
    outboundError = "";
    try {
      const r = await api.niMcpServers();
      servers = r.servers;
    } catch (err) {
      outboundError = describeError(err);
    } finally {
      outboundLoaded = true;
    }
  }
  onMount(loadServers);

  function parseArgs(text: string): string[] {
    console.assert(typeof text === "string", "parseArgs: text is string");
    console.assert(text.length < 100_000, "parseArgs: text within sane bounds");
    // One arg per line; trim + drop blanks. Preserves internal spaces (a single "arg with
    // spaces" line is one token — mirrors user's own MCP config habit).
    return text
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }

  function resetAddForm(): void {
    console.assert(addBusy === false, "resetAddForm: never reset while a write is in flight");
    console.assert(typeof addTransport === "string", "resetAddForm: transport is string");
    addLabel = "";
    addTransport = "stdio";
    addCommand = "";
    addArgsText = "";
    addUrl = "";
    addError = "";
  }

  const addValid = $derived.by(() => {
    const label = addLabel.trim();
    if (label.length === 0) return false;
    if (addTransport === "stdio") return addCommand.trim().length > 0;
    return addUrl.trim().length > 0;
  });

  async function addServer(): Promise<void> {
    console.assert(addBusy === false, "addServer: no concurrent add");
    console.assert(addTransport === "stdio" || addTransport === "http", "addServer: transport is closed set");
    if (!addValid || addBusy) return;
    addBusy = true;
    addError = "";
    try {
      const label = addLabel.trim();
      const body =
        addTransport === "stdio"
          ? {
              label,
              transport: "stdio" as const,
              command: addCommand.trim(),
              args: parseArgs(addArgsText),
              enabled: true,
            }
          : { label, transport: "http" as const, url: addUrl.trim(), enabled: true };
      await api.niMcpAdd(body);
      resetAddForm();
      await loadServers();
    } catch (err) {
      addError = describeError(err);
    } finally {
      addBusy = false;
    }
  }

  async function toggleEnabled(s: NiMcpServer): Promise<void> {
    console.assert(typeof s.id === "string", "toggleEnabled: id is string");
    console.assert(typeof s.enabled === "boolean", "toggleEnabled: enabled is boolean");
    if (rowBusy) return;
    rowBusy = s.id;
    outboundError = "";
    try {
      // PUT replaces the row — a partial body (just {enabled}) 422s server-side.
      // Send the whole current row with `enabled` flipped, mirroring transport shape.
      const body =
        s.transport === "stdio"
          ? {
              label: s.label,
              transport: "stdio" as const,
              command: s.command ?? "",
              args: s.args ?? [],
              enabled: !s.enabled,
            }
          : {
              label: s.label,
              transport: "http" as const,
              url: s.url ?? "",
              enabled: !s.enabled,
            };
      await api.niMcpUpdate(s.id, body);
      await loadServers();
    } catch (err) {
      outboundError = describeError(err);
    } finally {
      rowBusy = null;
    }
  }

  async function removeServer(s: NiMcpServer): Promise<void> {
    console.assert(typeof s.id === "string", "removeServer: id is string");
    console.assert(typeof s.label === "string", "removeServer: label is string");
    const ok = await confirmDialog({
      title: "Remove this server?",
      body: `Remove “${s.label}” from your MCP servers? Cards that already use it will need to be updated before they run again.`,
      confirmLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    rowBusy = s.id;
    outboundError = "";
    try {
      await api.niMcpDelete(s.id);
      await loadServers();
    } catch (err) {
      // 409 detail names the items still using this server — surface it verbatim so the
      // user knows what to delete or update first.
      outboundError = describeError(err);
    } finally {
      rowBusy = null;
    }
  }
</script>

<h1>MCP access</h1>
<p class="muted">
  Lets a desktop AI client (e.g. Claude Desktop, Cursor) read your knowledge base — and nothing
  else — over MCP. Disabled until a token exists; every request needs it as a bearer token.
</p>

<div class="card">
  <h2>Endpoint</h2>
  <div class="kit">{fullUrl}</div>

  <h2 style="margin-top:1.25rem">Access token {token ? "· enabled" : "· disabled"}</h2>
  {#if token}
    <div class="kit">{token}</div>
    <p style="margin-top:0.75rem; display:flex; gap:0.5rem; flex-wrap:wrap">
      <button onclick={copy}>{copied ? "Copied!" : "Copy token"}</button>
      <button class="secondary" disabled={busy} onclick={mint}>Regenerate</button>
      <button class="secondary" disabled={busy} onclick={revoke}>Revoke</button>
    </p>
    <p class="muted" style="margin-top:0.75rem">
      Configure the client with header <code>Authorization: Bearer &lt;token&gt;</code>.
    </p>
  {:else}
    <p class="muted">No token — MCP access is off.</p>
    <p style="margin-top:0.75rem">
      <button disabled={busy} onclick={mint}>{busy ? "Generating…" : "Generate token"}</button>
    </p>
  {/if}
</div>

{#if error}<p class="error">{error}</p>{/if}

<!-- Outbound MCP: a clearly-separated section below the inbound content. The heading + explainer
     name the philosophy plainly (§22: no ambient MCP config, no discovery, credentials stay in
     the user's own server process). -->
<div class="section-break"></div>

<h1>Connect out: your MCP servers (for Neural Interface cards)</h1>
<p class="muted">
  SmartBrain never reads ambient MCP config — only servers you add here, and only for cards you
  approve. Credentials stay in YOUR server process.
</p>

<div class="card">
  <h2>Your servers</h2>
  {#if !outboundLoaded}
    <p class="muted">Loading…</p>
  {:else if servers.length === 0}
    <p class="muted">No servers yet. Add one below.</p>
  {:else}
    <ul class="srv-list">
      {#each servers as s (s.id)}
        <li class="srv-row">
          <div class="srv-head">
            <strong class="srv-label">{s.label}</strong>
            <span class="srv-chip">{s.transport}</span>
            {#if !s.enabled}<span class="srv-chip srv-chip-off">disabled</span>{/if}
          </div>
          {#if s.transport === "stdio"}
            <div class="srv-addr">
              <span class="kit">{s.command ?? ""}{s.args && s.args.length ? " " + s.args.join(" ") : ""}</span>
            </div>
          {:else}
            <div class="srv-addr"><span class="kit">{s.url ?? ""}</span></div>
          {/if}
          <div class="srv-actions">
            <button
              class="secondary"
              disabled={rowBusy === s.id}
              onclick={() => toggleEnabled(s)}
            >
              {rowBusy === s.id ? "Working…" : s.enabled ? "Disable" : "Enable"}
            </button>
            <button class="del" disabled={rowBusy === s.id} onclick={() => removeServer(s)}>
              {rowBusy === s.id ? "Working…" : "Remove"}
            </button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
  {#if outboundError}<p class="error">{outboundError}</p>{/if}
</div>

<div class="card">
  <h2>Add a server</h2>

  <label for="mcp-out-label">Label</label>
  <input id="mcp-out-label" bind:value={addLabel} placeholder="e.g. Home Postgres" />

  <label for="mcp-out-transport" style="margin-top:0.75rem">Transport</label>
  <select id="mcp-out-transport" bind:value={addTransport}>
    <option value="stdio">stdio (SmartBrain launches your command each run)</option>
    <option value="http">http (SmartBrain calls a URL you host)</option>
  </select>

  {#if addTransport === "stdio"}
    <label for="mcp-out-command" style="margin-top:0.75rem">Command</label>
    <input id="mcp-out-command" bind:value={addCommand} placeholder="/usr/local/bin/my-mcp-server" />

    <label for="mcp-out-args" style="margin-top:0.75rem">Arguments (one per line)</label>
    <textarea
      id="mcp-out-args"
      bind:value={addArgsText}
      rows="4"
      placeholder="--config&#10;/etc/my-mcp/config.toml"
    ></textarea>
  {:else}
    <label for="mcp-out-url" style="margin-top:0.75rem">URL</label>
    <input id="mcp-out-url" bind:value={addUrl} placeholder="http://127.0.0.1:8765" />
  {/if}

  <p style="margin-top:0.75rem">
    <button disabled={addBusy || !addValid} onclick={addServer}>
      {addBusy ? "Adding…" : "Add server"}
    </button>
  </p>
  {#if addError}<p class="error">{addError}</p>{/if}
</div>

<style>
  /* A quiet ruler between the inbound (top) and outbound (bottom) sections so the second
     heading doesn't read as a continuation of the first card. */
  .section-break {
    height: 1px;
    background: var(--border);
    margin: var(--s-5, 1.5rem) 0;
  }
  .srv-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s-3, 0.75rem);
  }
  .srv-row {
    display: flex;
    flex-direction: column;
    gap: var(--s-1, 0.25rem);
    padding: var(--s-2, 0.5rem) 0;
    border-bottom: 1px solid var(--border);
  }
  .srv-row:last-child { border-bottom: none; }
  .srv-head {
    display: flex;
    align-items: center;
    gap: var(--s-2, 0.5rem);
    flex-wrap: wrap;
  }
  .srv-label { font-weight: 600; }
  .srv-chip {
    font-size: var(--f-meta, 0.75rem);
    padding: 2px 8px;
    border: 1px solid var(--border);
    border-radius: var(--r-full, 999px);
    background: var(--panel);
    color: var(--muted);
    text-transform: lowercase;
  }
  .srv-chip-off { color: var(--warn, var(--muted)); }
  .srv-addr .kit {
    word-break: break-all;
  }
  .srv-actions {
    display: inline-flex;
    gap: var(--s-2, 0.5rem);
    margin-top: var(--s-1, 0.25rem);
    flex-wrap: wrap;
  }
</style>
