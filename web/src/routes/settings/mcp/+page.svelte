<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "$lib/api";
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
