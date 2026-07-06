<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { api, type KbDoc, type KbDocFull, type KbHit } from "$lib/api";
  import { describeError } from "$lib/errors";
  import { remote } from "$lib/remote/connection.svelte";
  import { confirmDialog } from "$lib/confirm.svelte";

  let docs = $state<KbDoc[]>([]);
  let query = $state("");
  let mode = $state<"lexical" | "semantic">("lexical");
  let results = $state<KbHit[] | null>(null);
  let degraded = $state(false);
  let selected = $state<KbDocFull | null>(null);
  let newTitle = $state("");
  let newContent = $state("");
  let url = $state("");
  let fileInput = $state<HTMLInputElement | null>(null);
  let dragging = $state(false);
  let status = $state("");
  let error = $state("");
  let notice = $state("");
  let busy = $state("");
  let renameId = $state<string | null>(null); // inline rename of a document
  let renameValue = $state("");
  let scoreHelpOpen = $state(false); // U12: visible score-meaning popover, no hover needed
  let docOverlay = $state<HTMLDivElement | null>(null); // U4: focus target for opened doc

  const ACCEPT = ".pdf,.txt,.md,.markdown,.html,.htm,.csv,.json,.log,.rst";
  const _MAX_FILES = 20; // bounded per drop

  async function loadDocs() {
    try {
      docs = (await api.listDocs()).documents;
    } catch (err) {
      error = describeError(err);
    }
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await loadDocs();
  });

  async function addUrl() {
    const u = url.trim();
    if (!u || busy) return;
    busy = "url";
    error = "";
    status = `Fetching ${u}…`;
    try {
      const r = await api.ingestUrl(u);
      status = `Added “${r.title}” (${r.chars.toLocaleString()} chars).`;
      url = "";
      await loadDocs();
    } catch (err) {
      error = describeError(err);
      status = "";
    } finally {
      busy = "";
    }
  }

  async function uploadFiles(list: FileList) {
    if (busy) return;
    busy = "upload";
    error = "";
    const files = Array.from(list).slice(0, _MAX_FILES);
    let added = 0;
    for (const file of files) {
      status = `Reading ${file.name}…`;
      try {
        await api.uploadDoc(file);
        added += 1;
      } catch (err) {
        error = `${file.name}: ${describeError(err)}`;
      }
    }
    status = added ? `Added ${added} file${added > 1 ? "s" : ""} to your knowledge.` : "";
    busy = "";
    if (added) await loadDocs();
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    dragging = false;
    const files = event.dataTransfer?.files;
    if (files?.length) uploadFiles(files);
  }

  function onPick(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    if (input.files?.length) uploadFiles(input.files);
    input.value = ""; // allow re-picking the same file
  }

  async function search(event: Event) {
    event.preventDefault();
    const q = query.trim();
    if (!q) {
      results = null;
      return;
    }
    busy = "search";
    error = "";
    try {
      const r = await api.searchKb(q, mode);
      results = r.results;
      degraded = Boolean(r.degraded);
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function open(id: string) {
    console.assert(typeof id === "string" && id.length > 0, "open: id required");
    console.assert(typeof api.getDoc === "function", "open: api.getDoc must exist");
    error = "";
    try {
      selected = await api.getDoc(id);
    } catch (err) {
      error = describeError(err);
    }
  }

  // U4: when the doc overlay opens, move focus into it for keyboard/SR users.
  $effect(() => {
    if (selected) docOverlay?.focus();
  });

  // U4: Escape closes the opened-doc overlay.
  function onDocKey(event: KeyboardEvent) {
    console.assert(event instanceof KeyboardEvent, "onDocKey expects a KeyboardEvent");
    console.assert(typeof event.key === "string", "event.key must be a string");
    if (event.key === "Escape") selected = null;
  }

  async function add(event: Event) {
    event.preventDefault();
    if (!newTitle.trim() || !newContent.trim()) return;
    busy = "add";
    error = "";
    notice = "";
    try {
      await api.addDoc(newTitle.trim(), newContent.trim());
      newTitle = "";
      newContent = "";
      notice = "Note added.";
      await loadDocs();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function remove(id: string) {
    console.assert(typeof id === "string" && id.length > 0, "remove: id required");
    console.assert(busy === "" || busy === id, "remove: no other op in flight");
    const ok = await confirmDialog({
      title: "Delete document",
      body: "This can't be undone.",
      confirmLabel: "Delete",
    });
    if (!ok) return;
    busy = id;
    error = "";
    try {
      await api.deleteDoc(id);
      if (selected?.id === id) selected = null;
      results = results?.filter((r) => r.id !== id) ?? null;
      await loadDocs();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  function startRename(d: KbDoc) {
    renameId = d.id;
    renameValue = d.title;
    error = "";
  }
  function cancelRename() {
    renameId = null;
  }
  async function saveRename(id: string) {
    const t = renameValue.trim();
    if (!t) return;
    busy = id;
    error = "";
    try {
      await api.renameDoc(id, t);
      renameId = null;
      if (selected?.id === id) selected = { ...selected, title: t };
      results = results?.map((r) => (r.id === id ? { ...r, title: t } : r)) ?? null;
      await loadDocs();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function reindex() {
    busy = "reindex";
    error = "";
    notice = "";
    try {
      const r = await api.reindexKb();
      if (r.failed) {
        notice = `Reindexed ${r.embedded}; ${r.failed} failed${r.error ? ` (${r.error})` : ""}. ` +
          "Check the embedding model is loaded and selected under Settings → Model routing.";
      } else if (r.embedded === 0) {
        notice = "Knowledge is already up to date — nothing needed reindexing.";
      } else {
        notice = `Reindexed ${r.embedded} document${r.embedded === 1 ? "" : "s"} for semantic search.`;
      }
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }
</script>

{#if account.status?.unlocked}
  <h1>Knowledge</h1>

  <!-- Ingesting (drag-drop, file picker, URL) is Desktop work; on a phone, keep search + view. -->
  {#if remote.status === "idle"}
  <div class="card">
    <h2>Add to Knowledge</h2>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="drop"
      class:drag={dragging}
      role="button"
      tabindex="0"
      onclick={() => fileInput?.click()}
      onkeydown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          fileInput?.click();
        }
      }}
      ondragover={(e) => {
        e.preventDefault();
        dragging = true;
      }}
      ondragleave={() => (dragging = false)}
      ondrop={onDrop}
    >
      <input bind:this={fileInput} type="file" multiple accept={ACCEPT} style="display:none" onchange={onPick} />
      <strong>Drop a file here</strong> or click to choose
      <p class="muted">PDF, text, Markdown, HTML, CSV, JSON</p>
    </div>

    <label for="kburl" style="margin-top:1rem">…or add a web page / PDF by URL</label>
    <div style="display:flex; gap:0.5rem">
      <input
        id="kburl"
        style="flex:1; min-width:10rem"
        bind:value={url}
        placeholder="https://…"
        onkeydown={(e) => e.key === "Enter" && addUrl()}
      />
      <button disabled={busy === "url" || !url.trim()} onclick={addUrl}>{busy === "url" ? "Adding…" : "Add"}</button>
    </div>

    <details style="margin-top:1rem">
      <summary>…or write a note</summary>
      <form onsubmit={add}>
        <label for="t">Title</label>
        <input id="t" bind:value={newTitle} />
        <label for="c">Content</label>
        <textarea id="c" rows="5" bind:value={newContent}></textarea>
        <p style="margin-top:0.5rem">
          <button disabled={busy === "add" || !newTitle.trim() || !newContent.trim()} type="submit">
            {busy === "add" ? "Adding…" : "Add note"}
          </button>
        </p>
      </form>
    </details>

    {#if status}<p class="muted" style="margin-top:0.75rem">{status}</p>{/if}
    <p class="muted" style="margin-top:0.5rem; font-size:0.85rem">
      You can also ask in Chat: <em>“add this PDF to my knowledge: &lt;url&gt;”</em>.
    </p>
  </div>
  {/if}

  <div class="card">
    <h2>Search</h2>
    <form onsubmit={search} style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
      <input style="flex:1; min-width:12rem" bind:value={query} placeholder="Search your knowledge…" aria-label="Search your knowledge" />
      <select bind:value={mode} style="width:auto">
        <option value="lexical">Keyword</option>
        <option value="semantic">Meaning</option>
      </select>
      <button disabled={busy === "search"} type="submit">Search</button>
    </form>
    {#if mode === "semantic"}
      <p class="muted" style="margin-top:0.4rem; font-size:0.85rem">
        Meaning search ranks by similarity. It needs a local embedding model — pull the exact tag
        <code>ollama pull nomic-embed-text:v1.5</code> (the bare name won't resolve). Without one it
        falls back to keyword. New uploads are indexed automatically — use <em>Reindex</em> below if
        a result is missing.
      </p>
    {/if}
    {#if results}
      {#if degraded}
        <p class="muted" style="margin-top:0.5rem">
          Semantic search needs the embedding model — showing keyword results. On the Desktop, run
          <code>ollama pull nomic-embed-text:v1.5</code>, then Reindex.
        </p>
      {/if}
      {#if results.length === 0}
        <p class="muted" style="margin-top:0.5rem">No matches.</p>
      {/if}
      {#if results.length > 0}
        <p class="muted" style="margin-top:0.5rem; font-size:0.85rem; display:flex; gap:0.4rem; align-items:center; flex-wrap:wrap">
          <span>
            {mode === "semantic" && !degraded
              ? "Meaning match (0–1): higher = closer in meaning."
              : "Keyword score: how many times your terms appear."}
          </span>
          <button
            class="qhelp"
            type="button"
            aria-expanded={scoreHelpOpen}
            aria-label="More about search scores"
            onclick={() => (scoreHelpOpen = !scoreHelpOpen)}
          >?</button>
        </p>
        {#if scoreHelpOpen}
          <p class="muted" style="margin:0.25rem 0 0; font-size:0.85rem">
            <strong>Keyword</strong> counts term occurrences in each document.
            <strong>Meaning</strong> uses local embeddings (0–1) to rank by semantic similarity to your query.
          </p>
        {/if}
      {/if}
      {#each results as r (r.id)}
        <div style="margin-top:0.75rem">
          <button class="linklike" onclick={() => open(r.id)}>{r.title}</button>
          <span class="muted"> · {mode === "semantic" && !degraded ? r.score.toFixed(3) : r.score}</span>
          <p class="muted" style="margin:0.15rem 0 0">{r.snippet}</p>
        </div>
      {/each}
    {/if}
  </div>

  {#if selected}
    <div
      class="card doc-overlay"
      role="dialog"
      aria-modal="false"
      aria-label={selected.title}
      tabindex="-1"
      bind:this={docOverlay}
      onkeydown={onDocKey}
    >
      <h2>{selected.title}</h2>
      <div class="kit">{selected.content}</div>
      <p class="doc-actions">
        <button class="secondary" onclick={() => (selected = null)}>Close</button>
        <button class="secondary" disabled={busy === selected.id} onclick={() => remove(selected!.id)}>Delete</button>
      </p>
    </div>
  {/if}

  <div class="card">
    <h2 class="row">
      <span>All documents <span class="muted" style="font-weight:400">· {docs.length}</span></span>
      <span class="spacer"></span>
      <button disabled={busy === "reindex"} onclick={reindex}>
        {busy === "reindex" ? "Reindexing…" : "Reindex (semantic)"}
      </button>
    </h2>
    {#if docs.length === 0}
      <p class="muted">No documents yet — drop a file or paste a URL above.</p>
    {/if}
    {#each docs as d (d.id)}
      {#if renameId === d.id}
        <div style="display:flex; gap:0.5rem; align-items:center; margin-top:0.5rem">
          <input
            style="flex:1"
            bind:value={renameValue}
            onkeydown={(e) => e.key === "Enter" && saveRename(d.id)}
          />
          <button disabled={busy === d.id || !renameValue.trim()} onclick={() => saveRename(d.id)}>Save</button>
          <button class="secondary" onclick={cancelRename}>Cancel</button>
        </div>
      {:else}
        <div style="display:flex; gap:0.5rem; align-items:center; margin-top:0.5rem">
          <button class="secondary" style="flex:1; text-align:left; padding:0.4rem 0.6rem" onclick={() => open(d.id)}>{d.title}</button>
          <button class="secondary" disabled={busy === d.id} onclick={() => startRename(d)}>Rename</button>
          <button class="secondary" disabled={busy === d.id} onclick={() => remove(d.id)}>Delete</button>
        </div>
      {/if}
    {/each}
  </div>

  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <p class="muted">Loading&hellip;</p>
{/if}

<style>
  .drop {
    border: 2px dashed var(--border);
    border-radius: 12px;
    padding: 1.5rem;
    text-align: center;
    cursor: pointer;
    color: var(--muted);
    transition: border-color 0.15s, background 0.15s;
  }
  .drop:hover,
  .drop:focus-visible {
    border-color: var(--accent);
  }
  .drop.drag {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 10%, transparent);
  }
  .drop p {
    margin: 0.35rem 0 0;
    font-size: 0.85rem;
  }
  .linklike {
    padding: 0;
    border: 0;
    background: none;
    color: var(--accent);
    cursor: pointer;
  }
  .qhelp {
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 50%;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    padding: 0;
    font-size: 0.8rem;
    line-height: 1;
  }
  .qhelp:hover,
  .qhelp:focus-visible {
    border-color: var(--accent);
    color: var(--accent);
  }
  .doc-overlay {
    max-height: 80vh;
    overflow-y: auto;
    position: relative;
  }
  .doc-actions {
    position: sticky;
    bottom: 0;
    margin: 0.75rem -1rem -1rem;
    padding: 0.75rem 1rem;
    background: var(--panel);
    border-top: 1px solid var(--border);
    display: flex;
    gap: 0.5rem;
  }
</style>
