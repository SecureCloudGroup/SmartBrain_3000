<script lang="ts">
  import { page } from "$app/state";
  import { docs } from "$lib/docs.generated";

  // Public page: no unlock guard — new users need setup help before they have a vault.
  // The selected section follows the URL hash (#slug), so both the sidebar and
  // inter-doc links are plain anchors: keyboard-accessible, no click handlers.
  const current = $derived(
    docs.find((d) => d.slug === page.url.hash.replace(/^#/, "")) ?? docs[0],
  );
</script>

<div class="help">
  <nav class="help-nav" aria-label="Help sections">
    <h2>Help</h2>
    {#each docs as section (section.slug)}
      <a
        class="help-link"
        class:active={section.slug === current.slug}
        aria-current={section.slug === current.slug ? "page" : undefined}
        href={`#${section.slug}`}
      >
        {section.title}
      </a>
    {/each}
  </nav>

  <!-- Doc HTML is rendered from our own Markdown at build time (no scripts); CSP-safe. -->
  <article class="help-body card">
    {@html current.html}
  </article>
</div>

<style>
  .help {
    display: flex;
    gap: 1.5rem;
    align-items: flex-start;
  }
  .help-nav {
    flex: 0 0 14rem;
    position: sticky;
    top: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .help-nav h2 {
    margin: 0 0 0.5rem;
  }
  .help-link {
    text-align: left;
    padding: 0.4rem 0.6rem;
    border-radius: 0.4rem;
    text-decoration: none;
    color: var(--muted);
  }
  .help-link:hover {
    color: var(--text);
    background: rgba(127, 127, 127, 0.12);
  }
  .help-link.active {
    background: rgba(127, 127, 127, 0.2);
    color: var(--text);
    font-weight: 600;
  }
  .help-body {
    flex: 1;
    min-width: 0;
    line-height: 1.6;
    overflow-wrap: break-word; /* long URLs / inline code shouldn't overflow + clip on mobile */
  }
  .help-body :global(pre) {
    overflow-x: auto;
    padding: 0.75rem;
    border-radius: 0.5rem;
    background: rgba(127, 127, 127, 0.12);
  }
  .help-body :global(code) {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    overflow-wrap: anywhere;
  }
  .help-body :global(h1):first-child {
    margin-top: 0;
  }
  .help-body :global(h2) {
    margin: 1.5rem 0 0.5rem;
  }
  .help-body :global(ul),
  .help-body :global(ol) {
    padding-left: 1.25rem;
    margin: 0.5rem 0;
  }
  .help-body :global(li) {
    margin: 0.25rem 0;
  }
  .help-body :global(li > ul),
  .help-body :global(li > ol) {
    margin: 0.25rem 0;
  }
  @media (max-width: 720px) {
    .help {
      flex-direction: column;
    }
    .help-nav {
      position: static;
      flex-direction: row;
      flex-wrap: nowrap;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
    .help-nav .help-link {
      white-space: nowrap;
    }
  }
</style>
