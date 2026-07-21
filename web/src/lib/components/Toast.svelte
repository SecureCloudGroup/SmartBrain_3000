<script lang="ts">
  // Renders the toast queue (lib/toast.svelte.ts). One mount in the root layout.
  // Entry motion is CSS (not a Svelte transition) so prefers-reduced-motion can
  // switch it off; dismissal is instant on purpose.
  import { toastState, dismissToast } from "$lib/toast.svelte";
  import Icon from "./Icon.svelte";
</script>

{#if toastState.items.length > 0}
  <div class="toasts" aria-live="polite">
    {#each toastState.items as t (t.id)}
      <button class="toast {t.kind}" onclick={() => dismissToast(t.id)}>
        <Icon name={t.kind === "ok" ? "check" : "warn"} size={14} />
        {t.msg}
      </button>
    {/each}
  </div>
{/if}

<style>
  .toasts {
    position: fixed;
    bottom: var(--s-5);
    left: 50%;
    transform: translateX(-50%);
    z-index: var(--z-toast);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s-2);
  }
  .toast {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: var(--elevated);
    color: var(--text);
    border: 1px solid var(--border);
    border-left: 3px solid var(--ok);
    border-radius: var(--r-2);
    box-shadow: var(--shadow-modal);
    padding: 10px 16px;
    font-size: var(--f-label);
    font-weight: 500;
    min-height: 0;
    cursor: pointer; /* click to dismiss */
    animation: sb-rise-in var(--t-slow);
  }
  @media (prefers-reduced-motion: reduce) {
    .toast {
      animation: none;
    }
  }
  .toast :global(svg) { color: var(--ok); }
  .toast.error { border-left-color: var(--danger); }
  .toast.error :global(svg) { color: var(--danger); }
</style>
