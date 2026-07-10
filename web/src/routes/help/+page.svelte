<script lang="ts">
  import { page } from "$app/state";
  import { docs } from "$lib/docs.generated";

  // Public page: no unlock guard — new users need setup help before they have a vault.
  // The selected section follows the URL hash (#slug), so both the sidebar and
  // inter-doc links are plain anchors: keyboard-accessible, no click handlers.
  // The URL hash selects the section. A plain "#slug" (or "#"/unknown) picks a top-level
  // doc; a cross-doc deep link arrives as "#slug__heading-id" — split it so we switch to
  // the section AND scroll to the heading. (No same-doc "#anchor" links exist in /docs, so a
  // hash without "__" is always a section slug.)
  const hashParts = $derived(page.url.hash.replace(/^#/, "").split("__"));
  const current = $derived(docs.find((d) => d.slug === hashParts[0]) ?? docs[0]);
  const headingId = $derived(hashParts[1]);

  // Reduced motion: the help GIFs auto-play and loop past 5s (WCAG 2.2.2). When the user
  // prefers reduced motion, freeze each GIF to its first-frame poster — a CSS rule can't stop
  // a GIF, only swapping the src does. Re-applies whenever the rendered section changes.
  let article: HTMLElement | undefined = $state();
  $effect(() => {
    current; // re-run after {@html} swaps in a new section
    if (typeof window === "undefined" || !article) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => {
      for (const img of article!.querySelectorAll("img")) {
        const src = img.getAttribute("src") ?? "";
        if (mq.matches && src.endsWith(".gif")) {
          img.dataset.gif = src;
          img.setAttribute("src", src.replace(/\.gif$/, ".poster.png"));
        } else if (!mq.matches && img.dataset.gif) {
          img.setAttribute("src", img.dataset.gif);
          delete img.dataset.gif;
        }
      }
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  });

  // Deep link to a sub-heading (#slug__heading-id): once the section has rendered, bring the
  // target heading into view. Guarded to a slug shape so a crafted hash can't break the query.
  $effect(() => {
    current; // re-run when the section swaps in
    const id = headingId;
    if (typeof window === "undefined" || !article || !id || !/^[\w-]+$/.test(id)) return;
    article.querySelector(`[id="${id}"]`)?.scrollIntoView();
  });
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
  <article class="help-body card" bind:this={article}>
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
  /* Screenshots/GIFs are ~2760px wide; without this they clip (don't scale) on a phone —
     and /help is the no-unlock onboarding page a paired phone lands on. */
  .help-body :global(img) {
    max-width: 100%;
    height: auto;
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
