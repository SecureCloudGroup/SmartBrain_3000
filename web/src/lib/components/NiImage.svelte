<script lang="ts">
  // A §24 card image, loaded through api.niImageDataUrl (the relayed fetch) rather than
  // a plain <img src="/api/…">, which off the LAN would be asked of the relay's host.
  // `src` is server-written and already validated by validateBoundScene.
  import { api } from "$lib/api";

  let { src, alt }: { src: string; alt: string } = $props();
  let dataUrl = $state<string | null>(null);
  let failed = $state(false);

  $effect(() => {
    const wanted = src;
    let current = true;
    dataUrl = null;
    failed = false;
    api.niImageDataUrl(wanted).then(
      (url) => { if (current) dataUrl = url; },
      () => { if (current) failed = true; },
    );
    return () => { current = false; };
  });
</script>

{#if dataUrl}
  <img class="ni-image" src={dataUrl} {alt} />
{:else if failed}
  <span class="ni-image-missing muted">{alt}</span>
{:else}
  <span class="ni-image-loading" role="img" aria-label={alt}></span>
{/if}

<style>
  .ni-image {
    display: block;
    width: 100%;
    max-width: 100%;
    height: auto;
    border-radius: var(--r-2);
  }
  .ni-image-missing { font-size: var(--f-meta); }
  .ni-image-loading {
    display: block;
    width: 100%;
    aspect-ratio: 16 / 9;
    border-radius: var(--r-2);
    background: var(--field);
  }
</style>
