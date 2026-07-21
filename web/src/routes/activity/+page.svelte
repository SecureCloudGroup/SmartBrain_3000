<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type AuditEntry, type PendingAction } from "$lib/api";
  import { pending as pendingBadge } from "$lib/pending.svelte";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";
  import ActionCard from "$lib/components/ActionCard.svelte";
  import Chip from "$lib/components/Chip.svelte";
  import EmptyState from "$lib/components/EmptyState.svelte";
  import Icon from "$lib/components/Icon.svelte";
  import Spinner from "$lib/components/Spinner.svelte";
  import type { IconName } from "$lib/icons";

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
      await load(); // refresh: a stale/expired item (e.g. orphaned by a Desktop restart) drops off
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
      await load(); // refresh: a stale/expired item (e.g. orphaned by a Desktop restart) drops off
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
      await load(); // refresh: a stale/expired item (e.g. orphaned by a Desktop restart) drops off
    } finally {
      busy = "";
    }
  }

  // Tier -> Chip voice: observe reads calm (auto-run, read-only), reviewed carries the
  // accent (waits for approval), irreversible is the only red on the page.
  const tierKind: Record<string, "ok" | "accent" | "danger"> = {
    observe: "ok",
    reviewed: "accent",
    irreversible: "danger",
  };

  // A rough tool->icon mapping so pending cards read at a glance; pencil is the
  // honest default for "changes something".
  function iconForTool(tool: string): IconName {
    const t = tool.toLowerCase();
    if (t.includes("mail") || t.includes("email")) return "mail";
    if (t.includes("task")) return "tasks";
    if (t.includes("schedule")) return "clock";
    if (t.includes("kb") || t.includes("knowledge") || t.includes("note") || t.includes("doc")) return "book";
    if (t.includes("web") || t.includes("fetch") || t.includes("search")) return "search";
    if (t.includes("vault")) return "vault";
    return "pencil";
  }

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
      <ActionCard icon={iconForTool(p.tool)} title={p.tool} tier={p.tier === "irreversible" ? "irreversible" : "reviewed"} scope={fmtArgs(p.args)}>
        {#snippet actions()}
          {#if p.tier === "reviewed"}
            <button class="ghost" disabled={busy === p.id} title="Approve and stop asking for this tool" onclick={() => approve(p, true)}>Always allow</button>
          {/if}
          <button class="secondary" disabled={busy === p.id} onclick={() => deny(p)}>Deny</button>
          <button disabled={busy === p.id} onclick={() => approve(p)}>Approve</button>
        {/snippet}
      </ActionCard>
    {/each}
  {/if}

  {#if remembered.length > 0}
    <h2 class="section-gap">Always allowed</h2>
    <p class="muted hint-gap">These write tools run without asking. Irreversible actions (send email, delete) always ask.</p>
    <div class="card tight">
      {#each remembered as name (name)}
        <div class="arow">
          <strong>{name}</strong>
          <span class="grow"></span>
          <button class="secondary" disabled={busy === name} onclick={() => forget(name)}>Stop allowing</button>
        </div>
      {/each}
    </div>
  {/if}

  <h2 class="section-gap">History</h2>
  {#if entries.length === 0}
    <EmptyState icon="activity" title="Nothing recorded yet" body="Every tool the assistant runs lands here — reads run freely, changes wait for your approval first." />
  {:else}
    <div class="card tight">
      {#each entries as e (e.id)}
        <div class="hrow">
          <div class="hmain">
            <strong>{e.tool}</strong>
            <Chip kind={tierKind[e.tier] ?? ""}>{e.tier}</Chip>
            <span class="muted">{e.decision}</span>
            <span class="meta">by {e.actor}</span>
            <span class="status" class:ok={e.ok} class:bad={!e.ok}>
              <Icon name={e.ok ? "check" : "x"} size={13} /> {e.ok ? "ok" : "failed"}
            </span>
            <span class="grow"></span>
            <span class="meta">{e.ts}</span>
          </div>
          {#if e.args_summary}<pre class="hargs">{fmtArgs(e.args_summary)}</pre>{/if}
          {#if e.error}<p class="herr">{e.error}</p>{/if}
        </div>
      {/each}
    </div>
  {/if}

  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <Spinner block />
{/if}

<style>
  .section-gap {
    margin-top: var(--s-5);
  }
  .hint-gap {
    margin: 0 0 var(--s-2);
  }
  .card.tight {
    padding: var(--s-2) var(--s-4);
  }
  .grow {
    flex: 1;
  }
  .arow {
    display: flex;
    align-items: center;
    gap: var(--s-2);
    padding: var(--s-2) 0;
  }
  .arow + .arow {
    border-top: 1px solid var(--border);
  }
  .hrow {
    padding: var(--s-3) 0;
  }
  .hrow + .hrow {
    border-top: 1px solid var(--border);
  }
  .hmain {
    display: flex;
    align-items: center;
    gap: var(--s-2);
    flex-wrap: wrap;
    font-size: var(--f-label);
  }
  .status {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-weight: 500;
  }
  .status.ok {
    color: var(--ok);
  }
  .status.bad {
    color: var(--danger);
  }
  .hargs {
    margin: var(--s-1) 0 0;
    font-family: var(--font-mono);
    font-size: var(--f-meta);
    color: var(--muted);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .herr {
    margin: var(--s-1) 0 0;
    font-size: var(--f-meta);
    color: var(--danger);
  }
</style>
