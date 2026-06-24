<script lang="ts">
  // App-wide confirm dialog. Mounted once in the root layout; driven by confirm.svelte.ts.
  import { confirmState, settleConfirm } from "$lib/confirm.svelte";

  let dialog = $state<HTMLDivElement | null>(null);

  // Move focus into the dialog when it opens (a11y — keyboard/SR users land here).
  $effect(() => {
    if (confirmState.current) dialog?.focus();
  });

  function onKey(event: KeyboardEvent) {
    if (event.key === "Escape") settleConfirm(false);
    else if (event.key === "Enter") settleConfirm(true);
  }
</script>

{#if confirmState.current}
  <!-- Backdrop: click to dismiss. Keyboard users dismiss via Escape on the dialog below. -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="confirm-overlay" role="presentation" onclick={() => settleConfirm(false)}>
    <div
      class="confirm-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-label={confirmState.current.title}
      tabindex="-1"
      bind:this={dialog}
      onkeydown={onKey}
      onclick={(e) => e.stopPropagation()}
    >
      <h2>{confirmState.current.title}</h2>
      <p>{confirmState.current.body}</p>
      <div class="confirm-actions">
        <button class="secondary" onclick={() => settleConfirm(false)}>Cancel</button>
        <button class:danger={confirmState.current.danger} onclick={() => settleConfirm(true)}>
          {confirmState.current.confirmLabel}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .confirm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 1rem;
  }
  .confirm-dialog {
    /* Use the real theme vars (--card/--text-with-light-fallback didn't exist, so the panel
       was forced dark while text went dark in light mode -> unreadable). --panel + --text
       track the active theme, so the title + Cancel button contrast correctly in both. */
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem 1.5rem;
    max-width: 26rem;
    width: 100%;
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.4);
  }
  .confirm-dialog h2 {
    margin: 0 0 0.5rem;
    font-size: 1.05rem;
  }
  .confirm-dialog p {
    margin: 0 0 1rem;
    color: var(--muted);
  }
  .confirm-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }
  .confirm-actions button.danger {
    background: var(--danger, #c0392b);
    border-color: var(--danger, #c0392b);
    color: #fff;
  }
</style>
