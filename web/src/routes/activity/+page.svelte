<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type AuditEntry, type PendingAction, type RememberedSite } from "$lib/api";
  import { pending as pendingBadge } from "$lib/pending.svelte";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";
  import ActionCard from "$lib/components/ActionCard.svelte";
  import { fmtArgs, iconForTool } from "$lib/pendingCards";
  import Chip from "$lib/components/Chip.svelte";
  import EmptyState from "$lib/components/EmptyState.svelte";
  import Icon from "$lib/components/Icon.svelte";
  import Spinner from "$lib/components/Spinner.svelte";
  import type { IconName } from "$lib/icons";

  let entries = $state<AuditEntry[]>([]);
  let pending = $state<PendingAction[]>([]);
  let rememberedTools = $state<string[]>([]);
  let rememberedSites = $state<RememberedSite[]>([]);
  let busy = $state("");
  let error = $state("");

  async function load() {
    try {
      pending = (await api.listPending()).pending;
      pendingBadge.count = pending.length; // keep the nav badge in sync after approve/deny
      const r = await api.listRemembered();
      rememberedTools = r.tools;
      rememberedSites = r.sites;
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

  async function forget(name: string, host: string | null = null) {
    // busy tag combines tool + host so a site row's spinner doesn't lock every row that shares
    // the tool name (multiple hosts for the same tool live in the list side-by-side).
    busy = host ? `${name}@${host}` : name;
    error = "";
    try {
      await api.forgetRemembered(name, host);
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

</script>

{#if account.status?.unlocked}
  <h1>Activity</h1>
  <p class="muted">Every tool the assistant runs is recorded here. Args and results are encrypted at rest.</p>

  {#if pending.length > 0}
    <h2>Awaiting your approval</h2>
    {#each pending as p (p.id)}
      <ActionCard icon={iconForTool(p.tool)} title={p.tool} tier={p.tier === "irreversible" ? "irreversible" : "reviewed"} scope={fmtArgs(p.args)}>
        {#snippet actions()}
          {#if p.tier === "reviewed" && p.remember_mode === "tool"}
            <button class="ghost" disabled={busy === p.id} title="Approve and stop asking for this tool" onclick={() => approve(p, true)}>Always allow</button>
          {:else if p.tier === "reviewed" && p.remember_mode === "site" && p.remember_host}
            <!-- URL tool: consent is per-host, so the label names the exact destination.
                 The host is ellipsized inside the button so a long name can't push the card wide. -->
            <button class="ghost allow-site" disabled={busy === p.id} title={`Approve and stop asking for ${p.remember_host}`} onclick={() => approve(p, true)}>Always allow {p.remember_host}</button>
          {:else if p.tier === "reviewed" && p.remember_mode === null}
            <!-- The server refuses to remember this one, so don't offer a button that
                 silently wouldn't stick. Say why instead. -->
            <span class="muted" style="font-size:0.82rem" title="This tool can reach an address the assistant chooses, so each call is reviewed.">Always-allow unavailable</span>
          {/if}
          <button class="secondary" disabled={busy === p.id} onclick={() => deny(p)}>Deny</button>
          <button disabled={busy === p.id} onclick={() => approve(p)}>Approve</button>
        {/snippet}
      </ActionCard>
    {/each}
  {/if}

  {#if rememberedTools.length + rememberedSites.length > 0}
    <!-- Collapsed by default: this is reference material, not something to act on every
         visit — the count keeps it glanceable without the vertical space. -->
    <details class="section-gap">
      <summary><span class="allow-head">Always allowed · {rememberedTools.length + rememberedSites.length}</span></summary>
      <p class="muted hint-gap">These write tools run without asking. Irreversible actions (send email, delete) always ask; URL tools remember one site at a time so a call to a different address still parks for approval.</p>
      <div class="card tight">
        {#each rememberedTools as name (name)}
          <div class="arow">
            <strong>{name}</strong>
            <span class="grow"></span>
            <button class="secondary" disabled={busy === name} onclick={() => forget(name)}>Stop allowing</button>
          </div>
        {/each}
        {#each rememberedSites as s (`${s.tool}@${s.host}`)}
          <div class="arow">
            <strong>{s.tool}</strong>
            <span class="sep">·</span>
            <span class="host" title={s.host}>{s.host}</span>
            <span class="grow"></span>
            <button class="secondary" disabled={busy === `${s.tool}@${s.host}`} onclick={() => forget(s.tool, s.host)}>Stop allowing</button>
          </div>
        {/each}
      </div>
    </details>
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
  /* The collapsed summary reads at the same weight as the sibling h2 section heads. */
  .allow-head {
    font-size: var(--f-section);
    font-weight: 600;
  }
  details > .hint-gap {
    margin-top: var(--s-2);
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
  /* Site row: the host truncates instead of pushing "Stop allowing" off-card
     (tested with a 60-char host name in vitest). */
  .arow .sep {
    color: var(--muted);
  }
  .arow .host {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--muted);
  }
  /* Per-site Always-allow button: the host lives inside the label, so the whole
     button caps at the card width and ellipsizes rather than wrapping the layout. */
  .allow-site {
    max-width: 100%;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
