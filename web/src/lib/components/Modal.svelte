<script lang="ts">
  // THE modal — every overlay in the app renders through this one shell so backdrop,
  // z-order, focus, Escape, and sizing behave identically everywhere. (It replaces three
  // divergent implementations: Confirm's fixed z1000, email's z900 hand-rolled overlay,
  // and Knowledge's non-modal "overlay" card.)
  import type { Snippet } from "svelte";

  let {
    open = false,
    label = "",
    alert = false,
    size = "sm",
    onclose = () => {},
    onkeydown = (() => {}) as (e: KeyboardEvent) => void,
    children,
  }: {
    open?: boolean;
    label?: string;
    alert?: boolean; // role=alertdialog (confirm prompts) vs dialog (viewers/forms)
    size?: "sm" | "md" | "lg";
    onclose?: () => void;
    onkeydown?: (e: KeyboardEvent) => void;
    children?: Snippet;
  } = $props();

  let el = $state<HTMLDivElement | null>(null);

  // Move focus into the dialog when it opens (a11y — keyboard/SR users land here).
  $effect(() => {
    if (open) el?.focus();
  });

  function key(e: KeyboardEvent) {
    if (e.key === "Escape") onclose();
    else onkeydown(e);
  }
</script>

{#if open}
  <!-- Backdrop click dismisses; keyboard users dismiss via Escape on the dialog. -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" role="presentation" onclick={onclose}>
    <div
      class="modal size-{size}"
      role={alert ? "alertdialog" : "dialog"}
      aria-modal="true"
      aria-label={label}
      tabindex="-1"
      bind:this={el}
      onkeydown={key}
      onclick={(e) => e.stopPropagation()}
    >
      {@render children?.()}
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: var(--z-modal);
    padding: var(--s-4);
  }
  .modal {
    background: var(--elevated);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--r-3);
    box-shadow: var(--shadow-modal);
    padding: var(--s-5);
    width: 100%;
    max-height: 85vh;
    overflow-y: auto;
  }
  .size-sm { max-width: 26rem; }
  .size-md { max-width: 40rem; }
  .size-lg { max-width: 46rem; }
</style>
