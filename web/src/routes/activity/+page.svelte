<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type AuditEntry, type PendingAction } from "$lib/api";
  import { pending as pendingBadge } from "$lib/pending.svelte";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";

  let entries = $state<AuditEntry[]>([]);
  let pending = $state<PendingAction[]>([]);
  let remembered = $state<string[]>([]);
  let busy = $state("");
  let error = $state("");

  async function load() {
    try {
      pending = (await api.listPending()).pending;
      pendingBadge.count = pending.length; // keep the nav badge in sync after approve/deny
      remembered = (await api.listRemembered()).tools;
      entries = (await api.getAudit(200)).entries;
    } catch (err) {
      error = describeError(err);
    }
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await load();
  });

  async function approve(p: PendingAction, remember = false) {
    if (
      p.tier === "irreversible" &&
      !(await confirmDialog({
        title: "Irreversible action",
        body: `Run ${p.tool}? This cannot be undone.`,
        confirmLabel: "Run",
        danger: true,
      }))
    )
      return;
    busy = p.id;
    error = "";
    try {
      await api.approveAction(p.id, p.tier === "irreversible" ? p.tool : null, remember);
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function deny(p: PendingAction) {
    busy = p.id;
    error = "";
    try {
      await api.denyAction(p.id);
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function forget(name: string) {
    busy = name;
    error = "";
    try {
      await api.forgetRemembered(name);
      await load();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  const tierColor: Record<string, string> = {
    observe: "var(--ok)",
    reviewed: "var(--accent)",
    irreversible: "var(--danger)",
  };

  // Show tool args as readable "key: value" lines instead of raw JSON. Accepts an
  // object (pending tiles) or a JSON string (history args_summary, already
  // redacted + capped server-side); long values are truncated for display.
  function fmtArgs(args: unknown): string {
    let obj: unknown = args;
    if (typeof args === "string") {
      if (!args.trim()) return "";
      try {
        obj = JSON.parse(args);
      } catch {
        return args; // truncated / non-JSON summary — show as-is
      }
    }
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      return Object.entries(obj as Record<string, unknown>)
        .map(([k, v]) => {
          const s = typeof v === "string" ? v : JSON.stringify(v);
          return `${k}: ${s.length > 200 ? s.slice(0, 200) + "…" : s}`;
        })
        .join("\n");
    }
    return typeof args === "string" ? args : JSON.stringify(args);
  }
</script>

{#if account.status?.unlocked}
  <h1>Activity</h1>
  <p class="muted">Every tool the assistant runs is recorded here. Args and results are encrypted at rest.</p>

  {#if pending.length > 0}
    <h2>Awaiting your approval</h2>
    {#each pending as p (p.id)}
      <div class="card" style="padding:0.75rem 1rem; border-color:{tierColor[p.tier] ?? 'var(--border)'}">
        <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
          <strong>{p.tool}</strong>
          <span class="badge" style="border-color:{tierColor[p.tier] ?? 'var(--border)'}; color:{tierColor[p.tier] ?? 'var(--muted)'}">{p.tier}</span>
          <span class="spacer" style="flex:1"></span>
          <button disabled={busy === p.id} onclick={() => approve(p)}>Approve</button>
          {#if p.tier === "reviewed"}
            <button disabled={busy === p.id} title="Approve and stop asking for this tool" onclick={() => approve(p, true)}>Always allow</button>
          {/if}
          <button class="secondary" disabled={busy === p.id} onclick={() => deny(p)}>Deny</button>
        </div>
        <p class="muted" style="margin:0.4rem 0 0; font-family:ui-monospace,monospace; font-size:0.8rem; word-break:break-word; white-space:pre-wrap">{fmtArgs(p.args)}</p>
      </div>
    {/each}
  {/if}

  {#if remembered.length > 0}
    <h2 style="margin-top:1.5rem">Always allowed</h2>
    <p class="muted" style="margin:0 0 0.5rem">These write tools run without asking. Irreversible actions (send email, delete) always ask.</p>
    <div class="card" style="padding:0.75rem 1rem">
      {#each remembered as name (name)}
        <div style="display:flex; gap:0.5rem; align-items:center; padding:0.2rem 0">
          <strong>{name}</strong>
          <span class="spacer" style="flex:1"></span>
          <button class="secondary" disabled={busy === name} onclick={() => forget(name)}>Stop allowing</button>
        </div>
      {/each}
    </div>
  {/if}

  <h2 style="margin-top:1.5rem">History</h2>
  {#if entries.length === 0}
    <p class="muted">No activity yet.</p>
  {/if}

  {#each entries as e (e.id)}
    <div class="card" style="padding:0.75rem 1rem">
      <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
        <strong>{e.tool}</strong>
        <span class="badge" style="border-color:{tierColor[e.tier] ?? 'var(--border)'}; color:{tierColor[e.tier] ?? 'var(--muted)'}">{e.tier}</span>
        <span class="muted">{e.decision}</span>
        <span class="muted" style="font-size:0.75rem">by {e.actor}</span>
        <span style="color:{e.ok ? 'var(--ok)' : 'var(--danger)'}">{e.ok ? "✓" : "✕"} {e.ok ? "ok" : "failed"}</span>
        <span class="spacer" style="flex:1"></span>
        <span class="muted" style="font-size:0.8rem">{e.ts}</span>
      </div>
      {#if e.args_summary}<p class="muted" style="margin:0.4rem 0 0; font-size:0.8rem; word-break:break-word; white-space:pre-wrap">{fmtArgs(e.args_summary)}</p>{/if}
      {#if e.error}<p class="error" style="margin:0.3rem 0 0; font-size:0.8rem">{e.error}</p>{/if}
    </div>
  {/each}

  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <p class="muted">Loading&hellip;</p>
{/if}
