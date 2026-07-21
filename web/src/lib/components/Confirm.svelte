<script lang="ts">
  // App-wide confirm dialog. Mounted once in the root layout; driven by confirm.svelte.ts.
  // The shell is the shared Modal; this file only supplies the confirm semantics
  // (Enter = confirm, danger styling, the two buttons).
  import { confirmState, settleConfirm } from "$lib/confirm.svelte";
  import Modal from "./Modal.svelte";
</script>

{#if confirmState.current}
  <Modal
    open
    alert
    label={confirmState.current.title}
    onclose={() => settleConfirm(false)}
    onkeydown={(e) => {
      if (e.key === "Enter") settleConfirm(true);
    }}
  >
    <h2 class="modal-title">{confirmState.current.title}</h2>
    <p class="modal-body">{confirmState.current.body}</p>
    <div class="modal-actions">
      <button class="secondary" onclick={() => settleConfirm(false)}>Cancel</button>
      <button class:danger={confirmState.current.danger} onclick={() => settleConfirm(true)}>
        {confirmState.current.confirmLabel}
      </button>
    </div>
  </Modal>
{/if}
