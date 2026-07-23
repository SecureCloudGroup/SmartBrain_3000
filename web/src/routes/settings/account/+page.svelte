<script lang="ts">
  import { onMount } from "svelte";
  import { api, type TrashedConversation } from "$lib/api";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";
  import { daysLeft, localTs } from "$lib/runs";

  let current = $state("");
  let next = $state("");
  let confirm = $state("");
  let busy = $state(false);
  let pwMsg = $state("");
  let error = $state("");
  let dataMsg = $state("");
  let egressPass = $state(""); // re-entered to authorize export/backup (sensitive egress)
  let restoreFile = $state<FileList | null>(null);
  let showReset = $state(false);
  let resetNext = $state("");
  let resetConfirm = $state("");
  let resetMsg = $state("");

  function focusById(id: string) {
    const el = document.getElementById(id);
    if (el instanceof HTMLInputElement) el.focus();
  }

  async function changePassphrase(event: Event) {
    event.preventDefault();
    pwMsg = "";
    error = "";
    if (next.length < 8) {
      error = "New passphrase must be at least 8 characters.";
      focusById("pw-next");
      return;
    }
    if (next !== confirm) {
      error = "New passphrase and confirmation do not match.";
      focusById("pw-confirm");
      return;
    }
    busy = true;
    try {
      await api.changePassphrase(current, next);
      pwMsg = "Passphrase changed. Your Recovery Key still works.";
      current = next = confirm = "";
    } catch (err) {
      error = describeError(err);
      focusById("pw-current");
    } finally {
      busy = false;
    }
  }

  async function resetPassphrase(event: Event) {
    event.preventDefault();
    resetMsg = "";
    error = "";
    if (resetNext.length < 8) {
      error = "New passphrase must be at least 8 characters.";
      focusById("pw-reset-next");
      return;
    }
    if (resetNext !== resetConfirm) {
      error = "New passphrase and confirmation do not match.";
      focusById("pw-reset-confirm");
      return;
    }
    busy = true;
    try {
      await api.resetPassphrase(resetNext);
      resetMsg = "Passphrase set. You can now unlock with it.";
      resetNext = resetConfirm = "";
      showReset = false;
    } catch (err) {
      error = describeError(err);
      focusById("pw-reset-next");
    } finally {
      busy = false;
    }
  }

  function saveBlob(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function exportData() {
    dataMsg = "";
    error = "";
    busy = true;
    try {
      const data = await api.exportData(egressPass);
      saveBlob(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }), "smartbrain-export.json");
      dataMsg = "Exported your data as JSON.";
      egressPass = "";
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function downloadBackup() {
    dataMsg = "";
    error = "";
    busy = true;
    try {
      saveBlob(await api.backup(egressPass), "smartbrain-backup.duckdb");
      dataMsg = "Downloaded an encrypted backup. Keep it safe — it unlocks with your passphrase.";
      egressPass = "";
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  // --- chat trash (deleted chats are restorable until the retention window lapses) ---
  let trash = $state<TrashedConversation[]>([]);
  let retentionDays = $state(30);
  let trashMsg = $state("");

  async function loadTrash() {
    try {
      const r = await api.listTrash();
      trash = r.trash;
      retentionDays = r.retention_days;
    } catch {
      // Locked or unreachable — the card just shows empty; the rest of the page still works.
    }
  }
  onMount(loadTrash);

  async function deleteAllChats() {
    trashMsg = "";
    error = "";
    if (
      !(await confirmDialog({
        title: "Delete all chats",
        body: `Move every conversation to the Trash? You can restore from Trash for ${retentionDays} days.`,
        confirmLabel: "Delete all",
        danger: true,
      }))
    )
      return;
    busy = true;
    try {
      const r = await api.deleteAllConversations();
      trashMsg = r.trashed ? `Moved ${r.trashed} chat${r.trashed === 1 ? "" : "s"} to the Trash.` : "No chats to delete.";
      await loadTrash();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function restoreChat(id: string) {
    trashMsg = "";
    error = "";
    busy = true;
    try {
      await api.restoreConversation(id);
      trashMsg = "Chat restored.";
      await loadTrash();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function emptyTrash() {
    trashMsg = "";
    error = "";
    if (
      !(await confirmDialog({
        title: "Empty trash",
        body: "Permanently delete every chat in the Trash? This cannot be undone.",
        confirmLabel: "Empty trash",
        danger: true,
      }))
    )
      return;
    busy = true;
    try {
      const r = await api.emptyTrash();
      trashMsg = `Permanently deleted ${r.deleted} chat${r.deleted === 1 ? "" : "s"}.`;
      await loadTrash();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function restore() {
    dataMsg = "";
    error = "";
    const file = restoreFile?.[0];
    if (!file) return;
    if (
      !(await confirmDialog({
        title: "Restore backup",
        body: "Restore will replace ALL current data with this backup when SmartBrain restarts. Continue?",
        confirmLabel: "Restore",
        danger: true,
      }))
    )
      return;
    busy = true;
    try {
      const r = await api.restore(file);
      dataMsg = r.message;
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

</script>

<h1>Account &amp; Data</h1>

<div class="card">
  <h2>Change passphrase</h2>
  <p class="muted">Re-wraps your master key under a new passphrase. Your data and Recovery Key stay valid.</p>
  <form onsubmit={changePassphrase} style="display:flex; flex-direction:column; gap:0.5rem; max-width:28rem">
    <input id="pw-current" type="password" bind:value={current} placeholder="Current passphrase" aria-label="Current passphrase" autocomplete="current-password" />
    <input id="pw-next" type="password" bind:value={next} placeholder="New passphrase (min 8)" aria-label="New passphrase" autocomplete="new-password" />
    <input id="pw-confirm" type="password" bind:value={confirm} placeholder="Confirm new passphrase" aria-label="Confirm new passphrase" autocomplete="new-password" />
    <button disabled={busy || !current || !next} type="submit">Change passphrase</button>
  </form>
  {#if pwMsg}<p class="notice">{pwMsg}</p>{/if}

  {#if !showReset}
    <p class="muted" style="margin-top:0.75rem">
      There&rsquo;s no remote password reset. But if you forgot your passphrase and got in with your
      Recovery Key, you can set a new one now.
      <button class="link" type="button" onclick={() => (showReset = true)}>Set a new one</button>.
    </p>
  {:else}
    <form onsubmit={resetPassphrase} style="display:flex; flex-direction:column; gap:0.5rem; max-width:28rem; margin-top:0.75rem">
      <p class="muted" style="margin:0">Set a new passphrase using your current unlocked session — no current passphrase needed.</p>
      <input id="pw-reset-next" type="password" bind:value={resetNext} placeholder="New passphrase (min 8)" aria-label="New passphrase" autocomplete="new-password" />
      <input id="pw-reset-confirm" type="password" bind:value={resetConfirm} placeholder="Confirm new passphrase" aria-label="Confirm new passphrase" autocomplete="new-password" />
      <button disabled={busy || !resetNext} type="submit">Set new passphrase</button>
    </form>
  {/if}
  {#if resetMsg}<p class="notice">{resetMsg}</p>{/if}
</div>

<div class="card">
  <h2>Export &amp; backup</h2>
  <p class="muted">
    <strong>Export</strong> downloads your content as readable JSON. <strong>Backup</strong> downloads the
    full encrypted database — a complete, portable copy that restores with your passphrase.
  </p>
  <p class="muted">
    These hand out your decrypted data and the whole vault, so re-enter your passphrase to authorize.
    They work on this Desktop only — never from a paired phone.
  </p>
  <p style="display:flex; gap:0.5rem; flex-wrap:wrap; align-items:center">
    <input
      id="egress-pass"
      type="password"
      bind:value={egressPass}
      placeholder="Your passphrase"
      aria-label="Your passphrase"
      autocomplete="current-password"
      style="max-width:16rem"
    />
    <button class="secondary" disabled={busy || !egressPass} onclick={exportData}>Export data (JSON)</button>
    <button disabled={busy || !egressPass} onclick={downloadBackup}>Download encrypted backup</button>
  </p>
</div>

<div class="card">
  <h2>Chat trash</h2>
  <p class="muted">
    Deleted chats land here and stay restorable for {retentionDays} days, then they are
    permanently removed.
  </p>
  {#if trash.length}
    {#each trash as t (t.id)}
      <div class="row">
        <span style="min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">
          {t.title}
          <span class="muted">
            — deleted {localTs(t.deleted_at)} ·
            {#if daysLeft(t.deleted_at, retentionDays) === 0}deletes soon{:else}deletes in {daysLeft(t.deleted_at, retentionDays)} day{daysLeft(t.deleted_at, retentionDays) === 1 ? "" : "s"}{/if}
          </span>
        </span>
        <span class="spacer"></span>
        <button class="secondary" disabled={busy} onclick={() => restoreChat(t.id)}>Restore</button>
      </div>
    {/each}
  {:else}
    <p class="muted">The trash is empty.</p>
  {/if}
  <p style="display:flex; gap:0.5rem; flex-wrap:wrap; margin-top:0.75rem">
    <button class="del" disabled={busy} onclick={deleteAllChats}>Delete all chats</button>
    {#if trash.length}
      <button class="del" disabled={busy} onclick={emptyTrash}>Empty trash</button>
    {/if}
  </p>
  {#if trashMsg}<p class="notice">{trashMsg}</p>{/if}
</div>

<div class="card">
  <h2>Restore</h2>
  <p class="muted">
    Replace all current data with a backup file. It is validated, then applied the next time
    SmartBrain restarts. The current database is kept as <code>*.pre-restore-&lt;timestamp&gt;</code>
    so this is reversible.
  </p>
  <p class="muted">
    To apply it, restart the app:
    <code>docker compose -f compose/docker-compose.yml restart smartbrain</code>
    (or restart Docker Desktop).
  </p>
  <p style="display:flex; gap:0.5rem; flex-wrap:wrap; align-items:center">
    <input type="file" accept=".duckdb" bind:files={restoreFile} />
    <button class="secondary" disabled={busy || !restoreFile?.length} onclick={restore}>Stage restore</button>
  </p>
</div>

{#if dataMsg}<p class="notice">{dataMsg}</p>{/if}
{#if error}<p class="error" role="alert">{error}</p>{/if}
