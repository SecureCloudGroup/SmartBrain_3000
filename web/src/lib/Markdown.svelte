<script lang="ts">
  // An assistant bubble whose markdown is rendered (headings, lists, tables, code fences) instead of
  // shown as raw `###`/`**` characters. Safe by construction: the HTML comes from renderMarkdown(),
  // which sanitizes it — see lib/markdown.ts for why that is non-negotiable.
  //
  // Re-renders on every streaming delta (content is mutated in place while a reply arrives), so the
  // code-block copy buttons are re-attached after each {@html} swap replaces the inner DOM.
  import { renderMarkdown } from "./markdown";

  let { content }: { content: string } = $props();

  let el: HTMLElement | undefined = $state();
  const html = $derived(renderMarkdown(content));

  $effect(() => {
    html; // re-run whenever the rendered HTML changes (i.e. on each delta)
    if (!el) return;
    for (const pre of el.querySelectorAll("pre")) {
      if (pre.querySelector(".copy-code")) continue; // already enhanced this render
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copy-code";
      btn.textContent = "Copy";
      btn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(pre.querySelector("code")?.textContent ?? "");
          btn.textContent = "Copied";
          setTimeout(() => (btn.textContent = "Copy"), 1200);
        } catch {
          btn.textContent = "Press ⌘C"; // clipboard denied (insecure context / permissions)
        }
      });
      pre.appendChild(btn);
    }
  });
</script>

<div class="bubble assistant md" bind:this={el}>{@html html}</div>
