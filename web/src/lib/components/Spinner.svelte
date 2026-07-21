<script lang="ts">
  // The one loading indicator (replaces bare "Loading…" strings and the setup page's
  // private keyframes). Inline by default; `block` centers it with breathing room.
  let { size = 18, block = false, label = "Loading" }: { size?: number; block?: boolean; label?: string } = $props();
</script>

{#if block}
  <div class="block" role="status" aria-label={label}>
    <span class="spin" style="width:{size}px; height:{size}px"></span>
  </div>
{:else}
  <span class="spin" role="status" aria-label={label} style="width:{size}px; height:{size}px"></span>
{/if}

<style>
  .spin {
    display: inline-block;
    border: 2px solid var(--border-strong);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    vertical-align: -0.2em;
  }
  .block {
    display: grid;
    place-items: center;
    padding: var(--s-6) 0;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  @media (prefers-reduced-motion: reduce) {
    .spin { animation-duration: 1.6s; }
  }
</style>
