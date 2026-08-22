<script lang="ts">
  import Icon from "$lib/components/Icon.svelte";
  import Chip from "$lib/components/Chip.svelte";
  import EmptyState from "$lib/components/EmptyState.svelte";
  import Modal from "$lib/components/Modal.svelte";
  import Spinner from "$lib/components/Spinner.svelte";
  import { onDestroy, onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import {
    api,
    type ExportHeaders,
    type Feed,
    type KbDoc,
    type KbDocFull,
    type KbHit,
    type SearchMode,
    type Vault,
    type VaultMember,
    type VerifyHostedResult,
  } from "$lib/api";
  import { describeError } from "$lib/errors";
  import { highlight, queryTerms } from "$lib/highlight";
  import { remote } from "$lib/remote/connection.svelte";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { strToTags, tagsToStr } from "$lib/tags";
  import {
    retiredSubscriptionNote,
    unreachableSubscriptionNote,
    vaultChips,
    vaultChipsSummary,
  } from "$lib/vaultChips";

  let docs = $state<KbDoc[]>([]);
  let query = $state("");
  let mode = $state<SearchMode>("hybrid");
  let results = $state<KbHit[] | null>(null);
  let degraded = $state(false);
  let selected = $state<KbDocFull | null>(null);
  // The terms that produced the current results (frozen at search time, so editing the box doesn't
  // re-highlight against a query that wasn't run) + where in the open document the match sits.
  let hitTerms = $state<string[]>([]);
  let hitOffset = $state<number | null>(null);
  let markEl = $state<HTMLElement | null>(null);
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
  let tagEditId = $state<string | null>(null); // inline tag editor of a document (same idiom)
  let tagEditValue = $state("");
  // Click-to-filter: clicking any tag chip narrows BOTH lists (documents + vaults) to that tag.
  let tagFilter = $state("");
  let failures = $state<string[]>([]); // per-file errors from a bulk drop
  let scoreHelpOpen = $state(false); // U12: visible score-meaning popover, no hover needed

  // --- vaults: a named subset of knowledge you can search inside, and share -------------------
  let vaults = $state<Vault[]>([]);
  let scope = $state(""); // the vault the search is restricted to; "" = all knowledge
  let picked = $state<string[]>([]); // multi-selected document ids, for "add to vault"
  let addTarget = $state(""); // which vault the selection goes into
  let newVaultName = $state("");
  let exportId = $state<string | null>(null); // the vault whose export row is open
  let exportPass = $state(""); // re-auth: an export hands out plaintext-equivalent content
  let exportMode = $state<"sealed" | "open">("sealed"); // private (sealed) is ALWAYS the default
  let shownKey = $state(""); // the SBVK1- key, revealed after an export
  let publishedOpen = $state(false); // a public export just finished — show the hosting hint
  // The x-sb-export-* headers of the LAST export in this share panel — powers the "you re-published
  // with no changes" nudge and the sealed re-key warning. Cleared when the share panel toggles or
  // when the panel switches vaults, so a warning from a previous vault never bleeds across.
  let lastExportHeaders = $state<ExportHeaders | null>(null);
  // Retire flow: same panel, its own passphrase field + explanatory copy. The confirmed=false step
  // shows the plain-words explanation and requires an explicit "Yes, retire" click before the
  // passphrase field appears — so a mis-click on Retire… never dumps a passphrase-prompt at the user.
  let retireOpenId = $state<string | null>(null);
  let retirePass = $state("");
  let retireError = $state("");
  // Hosted-URL note (published-open vaults, inside the Share panel): the draft the user is
  // editing, an inline save error next to the Save button, and the last verify-hosted verdict
  // whose `detail` is rendered verbatim (the backend phrases every case for a human). Reset
  // whenever the Share panel toggles or switches vaults so a previous vault's verdict never
  // bleeds across.
  let hostedDraft = $state("");
  let hostedError = $state("");
  // "" | "save" | "verify" — which button is in flight, so both can disable together but their
  // labels can independently say "Saving…" / "Checking…" without a stale label on the wrong one.
  let hostedBusy = $state<"" | "save" | "verify">("");
  let hostedResult = $state<VerifyHostedResult | null>(null);
  // Delete flow for subscribed/imported: two-option confirm. Local vaults still use the simple
  // confirmDialog — they have no imported documents for the option to apply to.
  let deleteOpenId = $state<string | null>(null);
  let deleteRemoveDocs = $state(false);
  // Panel-local errors: "incorrect passphrase" must appear NEXT TO the passphrase field, not at the
  // bottom of a long page (a live tester read it as a broken page, not a wrong password).
  let shareError = $state("");
  let importError = $state("");
  let keyCopied = $state(false); // "Copied ✓" feedback — every other copy button in the app has it

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(shownKey);
      keyCopied = true;
      setTimeout(() => (keyCopied = false), 1500);
    } catch {
      /* clipboard unavailable — the key text is selectable */
    }
  }
  let importInput = $state<HTMLInputElement | null>(null);
  let docsCard = $state<HTMLDivElement | null>(null); // scroll target for "Add documents" on a vault
  let importKey = $state("");
  let subUrl = $state(""); // subscribe-by-URL: the public vault's address
  let subscribeError = $state(""); // inline, next to the URL field — same rule as importError
  let vaultBusy = $state("");

  // Feeds — a vault that fills itself. Pasting the URL here IS the consent for the 6-hourly
  // background refresh of that host, so add/unsubscribe are Desktop-local like import/export.
  let feeds = $state<Feed[]>([]);
  let feedUrl = $state("");
  let feedError = $state(""); // inline, next to the URL field — same rule as subscribeError
  let feedBusy = $state(""); // "add" | "<feed id>" while a request is in flight
  let feedConfirmId = $state<string | null>(null); // unsubscribe: one open keep-vs-delete panel

  const ACCEPT = ".pdf,.docx,.pptx,.xlsx,.txt,.md,.markdown,.html,.htm,.csv,.json,.log,.rst";
  const _MAX_FILES = 200; // bounded per drop (uploads no longer block on embedding, so this can be generous)

  async function loadDocs() {
    try {
      docs = (await api.listDocs()).documents;
    } catch (err) {
      error = describeError(err);
    }
  }

  async function loadVaults() {
    try {
      vaults = (await api.listVaults()).vaults;
    } catch (err) {
      error = describeError(err);
    }
  }

  async function loadFeeds() {
    try {
      feeds = (await api.listFeeds()).feeds;
    } catch (err) {
      error = describeError(err);
    }
  }

  async function addFeed() {
    const url = feedUrl.trim();
    if (!url || feedBusy) return;
    feedBusy = "add";
    feedError = "";
    try {
      const r = await api.addFeed(url);
      notice = `Subscribed to “${r.title}” — ${r.items} article${r.items === 1 ? "" : "s"} saved to its vault. New posts are picked up automatically.`;
      feedUrl = "";
      await Promise.all([loadFeeds(), loadDocs(), loadVaults()]);
      refreshIndexStatus(); // the new articles embed in the background
    } catch (err) {
      feedError = describeError(err);
    } finally {
      feedBusy = "";
    }
  }

  async function refreshFeed(f: Feed) {
    if (feedBusy) return;
    feedBusy = f.id;
    try {
      const r = await api.refreshFeed(f.id);
      notice = r.items
        ? `“${f.title}”: ${r.items} new article${r.items === 1 ? "" : "s"}.`
        : `“${f.title}”: nothing new.`;
      await Promise.all([loadFeeds(), loadDocs(), loadVaults()]);
      if (r.items) refreshIndexStatus();
    } catch (err) {
      feedError = describeError(err);
      await loadFeeds(); // the row's status just recorded the failure — show it
    } finally {
      feedBusy = "";
    }
  }

  async function unsubscribeFeed(f: Feed, removeDocs: boolean) {
    if (feedBusy) return;
    feedBusy = f.id;
    feedError = "";
    try {
      const r = await api.deleteFeed(f.id, { remove_docs: removeDocs });
      notice = removeDocs
        ? `Unsubscribed from “${f.title}” and deleted its ${r.docs_removed} article${r.docs_removed === 1 ? "" : "s"}.`
        : `Unsubscribed from “${f.title}”. Its saved articles stay in your knowledge.`;
      feedConfirmId = null;
      await Promise.all([loadFeeds(), loadDocs(), loadVaults()]);
    } catch (err) {
      feedError = describeError(err);
    } finally {
      feedBusy = "";
    }
  }

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await Promise.all([loadDocs(), loadVaults(), loadFeeds()]);
    refreshIndexStatus();
    // Deep link from a chat citation chip: /knowledge?doc=<id>&offset=<n> opens the
    // document at the cited passage (no offset -> at the top). Plain window.location —
    // the same idiom the email page uses; goto() mounts this page fresh.
    const params = new URLSearchParams(window.location.search);
    const doc = params.get("doc");
    if (doc) {
      // "offset=" (a citation with no passage) must open at the top — but Number("") is 0,
      // so an empty/garbled offset maps to null rather than a phantom mark at position 0.
      const raw = params.get("offset");
      const off = raw ? Number(raw) : NaN;
      await open(doc, Number.isFinite(off) ? off : null);
    }
  });

  async function addUrl() {
    const u = url.trim();
    if (!u || busy) return;
    busy = "url";
    error = "";
    status = `Fetching ${u}…`;
    try {
      const r = await api.ingestUrl(u);
      status = r.duplicate
        ? `“${r.title}” is already in your knowledge — not added again.`
        : `Added “${r.title}” (${r.chars.toLocaleString()} chars).`;
      url = "";
      await loadDocs();
      refreshIndexStatus();
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
    failures = [];
    const files = Array.from(list).slice(0, _MAX_FILES);
    let added = 0;
    let duplicates = 0;
    for (const [i, file] of files.entries()) {
      status = `Adding ${i + 1} of ${files.length} — ${file.name}…`;
      try {
        const r = await api.uploadDoc(file);
        if (r.duplicate) duplicates += 1;
        else added += 1;
      } catch (err) {
        // One bad file must not abandon the rest of the drop, and the user needs to know WHICH.
        failures.push(`${file.name}: ${describeError(err)}`);
      }
    }
    const parts: string[] = [];
    if (added) parts.push(`Added ${added} file${added > 1 ? "s" : ""}`);
    if (duplicates) parts.push(`${duplicates} already in your knowledge`);
    if (failures.length) parts.push(`${failures.length} couldn't be read`);
    status = parts.join(" · ");
    busy = "";
    if (added) await loadDocs();
    refreshIndexStatus(); // uploads don't embed inline any more — show the indexing catch-up
  }

  // Uploaded documents are keyword-searchable at once, but their vectors are added by the background
  // indexer. Poll while there's a backlog so the page can say so instead of looking finished.
  let indexPending = $state(0);
  let indexTotal = $state(0);
  let summarized = $state(0);
  let summaryTotal = $state(0);
  let indexTimer: ReturnType<typeof setInterval> | null = null;

  async function refreshIndexStatus() {
    try {
      const s = await api.indexStatus();
      indexPending = s.pending;
      indexTotal = s.total;
      summarized = s.summarized ?? 0;
      summaryTotal = s.summary_total ?? 0;
      const working = s.pending > 0 || (s.summary_total ?? 0) > (s.summarized ?? 0);
      if (working && indexTimer === null) {
        indexTimer = setInterval(refreshIndexStatus, 4000);
      } else if (!working && indexTimer !== null) {
        clearInterval(indexTimer);
        indexTimer = null;
      }
    } catch {
      /* locked / offline — leave the last known state alone */
    }
  }

  onDestroy(() => {
    if (indexTimer) clearInterval(indexTimer);
  });

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
      const r = await api.searchKb(q, mode, 10, scope);
      results = r.results;
      degraded = Boolean(r.degraded);
      hitTerms = queryTerms(q);
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  // A section means different things per format, so cite it by its real name: a deck has slides and
  // a spreadsheet has sheets. Calling a slide "p.3" is just wrong.
  function locator(r: KbHit): string {
    return r.page_label && r.page_label !== "page" ? `${r.page_label} ${r.page}` : `p.${r.page}`;
  }

  // `offset` opens the document AT the passage that matched instead of at the top — the whole point
  // of tracking provenance. Opening from the "All documents" list passes no offset.
  async function open(id: string, offset: number | null = null) {
    console.assert(typeof id === "string" && id.length > 0, "open: id required");
    console.assert(typeof api.getDoc === "function", "open: api.getDoc must exist");
    error = "";
    hitOffset = offset;
    try {
      selected = await api.getDoc(id);
    } catch (err) {
      error = describeError(err);
    }
  }

  // Split the open document around the matched passage so it can be marked and scrolled to.
  const MARK_CHARS = 320;
  const docParts = $derived.by(() => {
    if (!selected) return null;
    const text = selected.content;
    if (hitOffset === null) return { before: text, mark: "", after: "" };
    const start = Math.max(0, Math.min(hitOffset, text.length));
    const end = Math.min(start + MARK_CHARS, text.length);
    return { before: text.slice(0, start), mark: text.slice(start, end), after: text.slice(end) };
  });

  // The doc viewer renders through the shared Modal (focus + Escape live there).
  // Bring the matched passage into view once it's rendered (a long document opens at the top
  // otherwise, which defeats the citation).
  $effect(() => {
    if (selected && markEl) markEl.scrollIntoView({ block: "center" });
  });

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
      picked = picked.filter((p) => p !== id);
      // The backend drops the document from every vault that held it, so the counts have moved.
      await Promise.all([loadDocs(), loadVaults()]);
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  const shownDocs = $derived(tagFilter ? docs.filter((d) => (d.tags ?? []).includes(tagFilter)) : docs);
  const shownVaults = $derived(tagFilter ? vaults.filter((v) => (v.tags ?? []).includes(tagFilter)) : vaults);

  function startRename(d: KbDoc) {
    renameId = d.id;
    renameValue = d.title;
    tagEditId = null;
    error = "";
  }
  function cancelRename() {
    renameId = null;
  }

  function startTagEdit(d: KbDoc) {
    tagEditId = d.id;
    tagEditValue = tagsToStr(d.tags);
    renameId = null;
    error = "";
  }
  async function saveTags(id: string) {
    busy = id;
    error = "";
    try {
      await api.setDocTags(id, strToTags(tagEditValue));
      tagEditId = null;
      await loadDocs();
    } catch (err) {
      error = describeError(err); // a vault-owned copy 409s here, naming the vault
    } finally {
      busy = "";
    }
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
      } else if (r.embedded === 0 && r.pending === 0) {
        notice = "Knowledge is already up to date — nothing needed reindexing.";
      } else if (r.pending > 0) {
        // The request is time-boxed so it always returns; the background indexer finishes the rest.
        notice = `Indexed ${r.embedded} document${r.embedded === 1 ? "" : "s"} — ${r.pending} still ` +
          "to go, continuing in the background.";
      } else {
        notice = `Reindexed ${r.embedded} document${r.embedded === 1 ? "" : "s"} for semantic search.`;
      }
      refreshIndexStatus();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  // --- vaults -----------------------------------------------------------------------------------

  function togglePick(id: string) {
    picked = picked.includes(id) ? picked.filter((p) => p !== id) : [...picked, id];
  }

  // "Add documents" on a vault row. The checkboxes live up in the documents list, which is
  // invisible when you're looking at the vault itself — a real tester got stuck exactly here. Arm
  // the selection for THIS vault and take the user to where the ticking happens.
  function startAdding(v: Vault) {
    addTarget = v.id;
    picked = [];
    // CSS can't reach an explicit behavior:"smooth", so honor reduced motion here.
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    docsCard?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }

  // Inline vault tag editor — same comma-string idiom as the document one.
  let vaultTagEditId = $state<string | null>(null);
  let vaultTagEditValue = $state("");

  function startVaultTagEdit(v: Vault) {
    vaultTagEditId = vaultTagEditId === v.id ? null : v.id;
    vaultTagEditValue = tagsToStr(v.tags);
    error = "";
  }
  async function saveVaultTags(v: Vault) {
    vaultBusy = v.id;
    error = "";
    try {
      // name/description ride along unchanged — the PATCH requires a name, and tags-only
      // updates must not clear the rest.
      await api.updateVaultMeta(v.id, { name: v.name, description: v.description, tags: strToTags(vaultTagEditValue) });
      vaultTagEditId = null;
      await loadVaults();
    } catch (err) {
      error = describeError(err);
    } finally {
      vaultBusy = "";
    }
  }

  // Which vault's member list is expanded, and its documents (with each membership's origin, so
  // imported rows can offer Detach). A count alone ("2 documents") tells the user nothing about
  // WHAT they're about to share or search — a real tester was confused by exactly that, so the
  // count itself opens the list.
  let openVaultId = $state<string | null>(null);
  let members = $state<VaultMember[]>([]);

  async function toggleMembers(v: Vault) {
    if (openVaultId === v.id) {
      openVaultId = null;
      return;
    }
    error = "";
    try {
      members = (await api.getVault(v.id)).members;
      openVaultId = v.id;
    } catch (err) {
      error = describeError(err);
    }
  }

  function titleOf(id: string): string {
    return docs.find((d) => d.id === id)?.title ?? "(deleted document)";
  }

  async function removeFromVault(v: Vault, docId: string) {
    error = "";
    try {
      await api.removeFromVault(v.id, docId);
      members = members.filter((m) => m.id !== docId);
      await loadVaults(); // the count on the row just changed
    } catch (err) {
      error = describeError(err);
    }
  }

  // Claim an imported copy as the user's own: the row stops being read-only, and a future update
  // from the vault's publisher will skip it instead of replacing it.
  async function detachFromVault(v: Vault, docId: string) {
    error = "";
    try {
      await api.detachFromVault(v.id, docId);
      members = members.map((m) => (m.id === docId ? { ...m, origin: "owner" } : m));
    } catch (err) {
      error = describeError(err);
    }
  }

  async function addToVault() {
    console.assert(picked.length > 0, "addToVault: nothing selected");
    if (!addTarget || picked.length === 0) return;
    vaultBusy = "add";
    error = "";
    notice = "";
    try {
      const r = await api.addToVault(addTarget, picked);
      const name = vaults.find((v) => v.id === addTarget)?.name ?? "the vault";
      // Adding a document twice is a no-op, so say what actually landed rather than what was clicked.
      notice = `Added ${r.added} of ${picked.length} to “${name}” — it now holds ${r.doc_count}.`;
      picked = [];
      addTarget = "";
      await loadVaults();
    } catch (err) {
      error = describeError(err);
    } finally {
      vaultBusy = "";
    }
  }

  async function createVault() {
    const name = newVaultName.trim();
    if (!name) return;
    vaultBusy = "create";
    error = "";
    notice = "";
    const count = picked.length;
    try {
      const v = await api.createVault(name);
      newVaultName = ""; // the vault exists now — never leave a name sitting there to be created twice
      if (count > 0) {
        // If THIS fails the vault still exists, empty. Leave the selection alone so the error is
        // recoverable with "Add to vault" — and so a retry can't mint a second empty vault.
        await api.addToVault(v.id, picked);
        picked = [];
        notice = `Created “${name}” with ${count} document${count === 1 ? "" : "s"}.`;
      } else {
        notice = `Created “${name}”. Tick documents above to add them.`;
      }
    } catch (err) {
      error = describeError(err);
    } finally {
      await loadVaults(); // whatever happened, show what actually exists
      vaultBusy = "";
    }
  }

  // Local vaults keep the simple confirm — there are no imported documents to choose about.
  async function removeVault(v: Vault) {
    console.assert(v && typeof v.id === "string", "removeVault: vault required");
    if (v.kind === "imported") {
      // Open the inline two-option panel: Keep documents (default) or Also remove them.
      // A confirmDialog is single-boolean; a "remove_docs" choice needs two clicks worth of
      // clarity, and the inline panel matches the trust/share/retire pattern on this card.
      deleteOpenId = v.id;
      deleteRemoveDocs = false;
      return;
    }
    const ok = await confirmDialog({
      title: `Delete “${v.name}”`,
      // The distinction that matters: this removes a grouping, not the files in it.
      body: "The documents in it stay in your knowledge — only the vault is removed.",
      confirmLabel: "Delete vault",
    });
    if (!ok) return;
    await doDeleteVault(v, false);
  }

  // The commit half of the delete flow — shared between the local confirmDialog path and the
  // subscribed/imported inline panel. `remove_docs` is the opt-in that also shreds import-origin
  // documents (owner-origin copies always stay — the backend enforces this too).
  async function doDeleteVault(v: Vault, removeDocs: boolean) {
    console.assert(v && typeof v.id === "string", "doDeleteVault: vault required");
    console.assert(typeof removeDocs === "boolean", "doDeleteVault: removeDocs must be a boolean");
    vaultBusy = v.id;
    error = "";
    try {
      const r = await api.deleteVault(v.id, { remove_docs: removeDocs });
      if (scope === v.id) scope = "";
      if (addTarget === v.id) addTarget = "";
      deleteOpenId = null;
      notice = removeDocs
        ? `Deleted “${v.name}” and removed ${r.removed_docs} imported document${r.removed_docs === 1 ? "" : "s"}.`
        : `Deleted “${v.name}”. Its documents remain in your knowledge.`;
      // Docs may have moved (remove_docs deletes them), so refresh both lists — the docs list
      // for the ones that vanished, the vaults list for the count on this row.
      await Promise.all([loadDocs(), loadVaults()]);
    } catch (err) {
      error = describeError(err);
    } finally {
      vaultBusy = "";
    }
  }

  function saveBlob(blob: Blob, filename: string) {
    const href = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = href;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(href);
  }

  // Export downloads the .sbvault. Sealed: then shows its key — the two must travel separately,
  // whoever holds both holds the contents. Public/open: there IS no key, so the follow-up is a
  // hosting hint instead of a key row. The response headers carry the flags the UI warns on:
  // x-sb-export-unchanged (identical to the previous export) and x-sb-export-rotated-key
  // (a sealed re-export minted a NEW Vault Key, orphaning previous recipients).
  async function exportVault(v: Vault) {
    console.assert(v && typeof v.id === "string", "exportVault: vault required");
    console.assert(typeof exportPass === "string", "exportVault: passphrase state must be a string");
    if (!exportPass) return;
    vaultBusy = v.id;
    shareError = "";
    notice = "";
    shownKey = "";
    publishedOpen = false;
    lastExportHeaders = null;
    try {
      const { blob, headers } = await api.exportVault(v.id, exportPass, exportMode);
      lastExportHeaders = headers;
      saveBlob(blob, `${v.name.replace(/[^\w -]/g, "") || "vault"}.sbvault`);
      if (exportMode === "sealed") {
        shownKey = await api.vaultKey(v.id, exportPass);
      } else {
        publishedOpen = true;
      }
      exportPass = "";
      await loadVaults(); // the card's Public badge may have just appeared
    } catch (err) {
      shareError = describeError(err);
    } finally {
      vaultBusy = "";
    }
  }

  // Retire a published-open vault. Produces a FINAL open export marked retired; the user hosts
  // this file in place of the previous one, and subscribers apply the last content update and
  // stop auto-checking. Same Desktop-local + passphrase gate as export — the produced file is
  // decrypted plaintext.
  async function retireVault(v: Vault) {
    console.assert(v && typeof v.id === "string", "retireVault: vault required");
    console.assert(v.published_open, "retireVault: only meaningful on published-open vaults");
    if (!retirePass) return;
    vaultBusy = v.id;
    retireError = "";
    notice = "";
    lastExportHeaders = null;
    try {
      const { blob, headers } = await api.retireVault(v.id, retirePass);
      lastExportHeaders = headers;
      saveBlob(blob, `${v.name.replace(/[^\w -]/g, "") || "vault"}-retired.sbvault`);
      retirePass = "";
      retireOpenId = null;
      notice = `Retired “${v.name}”. Replace the hosted file with this one — it tells subscribers you've retired the vault.`;
      await loadVaults(); // the card's chip just flipped from Public to Retired
    } catch (err) {
      retireError = describeError(err);
    } finally {
      vaultBusy = "";
    }
  }

  // Save the publisher's hosted-URL note: the same PATCH the rename/tags editors use, only the
  // hosted_url field carried (name is required by the endpoint's shape so it rides along
  // unchanged). Empty string == cleared. A refusal (bad scheme, LAN/localhost) is a 400 whose
  // detail the server phrased for a human; render it inline next to Save, not page-bottom.
  async function saveHostedUrl(v: Vault) {
    console.assert(v && typeof v.id === "string", "saveHostedUrl: vault required");
    console.assert(typeof hostedDraft === "string", "saveHostedUrl: draft must be a string");
    hostedBusy = "save";
    hostedError = "";
    hostedResult = null; // a stale verdict against the old URL would mislead
    try {
      await api.updateVaultMeta(v.id, {
        name: v.name, description: v.description, hosted_url: hostedDraft.trim(),
      });
      await loadVaults();
    } catch (err) {
      hostedError = describeError(err);
    } finally {
      hostedBusy = "";
    }
  }

  // Fetch the hosted file and check it against this install's last publish. The server's `detail`
  // is human-ready for every branch (matches / behind / anomaly / wrong-signature / unreachable);
  // the UI just picks a state-appropriate style for it. Never surfaces a raw exception — every
  // network/parse failure comes back as a well-typed row.
  async function verifyHosted(v: Vault) {
    console.assert(v && typeof v.id === "string", "verifyHosted: vault required");
    console.assert(v.published_open, "verifyHosted: only meaningful on published-open vaults");
    hostedBusy = "verify";
    hostedError = "";
    hostedResult = null;
    try {
      hostedResult = await api.verifyHostedVault(v.id);
    } catch (err) {
      hostedError = describeError(err);
    } finally {
      hostedBusy = "";
    }
  }

  // The verdict's style: ok when matches, warn on any anomaly (behind / newer-than-local /
  // wrong-signature), muted when the host is unreachable — mirrors the three categories the
  // backend distinguishes in its response shape.
  function hostedResultClass(r: VerifyHostedResult): string {
    console.assert(r !== null && typeof r === "object", "hostedResultClass: verdict required");
    console.assert(typeof r.reachable === "boolean", "hostedResultClass: reachable must be boolean");
    if (!r.reachable) return "muted";
    return r.matches ? "hosted-ok" : "hosted-warn";
  }

  async function importVault() {
    const file = importInput?.files?.[0];
    if (!file) return;
    vaultBusy = "import";
    importError = "";
    notice = "";
    try {
      // The key may be empty: a PUBLIC (open) .sbvault has no key at all.
      const r = await api.importVault(file, importKey.trim());
      if (r.update) {
        // The file's vault identity was already pinned here, so it applied as an UPDATE to that
        // vault (§7: it's an update, not an import) — never a duplicate.
        const changed = (r.added ?? 0) + (r.updated ?? 0) + (r.deleted ?? 0) + (r.kept_yours ?? 0);
        notice = changed
          ? `That file is a newer version of “${r.name}” — applied as an update: ${updateSummary(r)}.`
          : `“${r.name}” is already up to date (v${r.seq}).`;
      } else {
        // Name the publisher fingerprint: it is the only thing that says WHO this knowledge came from.
        notice =
          `Imported “${r.name}” from publisher ${r.publisher} — ${r.added} new document` +
          `${r.added === 1 ? "" : "s"}${r.duplicates ? `, ${r.duplicates} you already had` : ""}. ` +
          (r.vectors_used
            ? "It is searchable now."
            : "Meaning search needs a reindex (the vault was built with a different embedding model).");
      }
      importKey = "";
      if (importInput) importInput.value = "";
      await Promise.all([loadDocs(), loadVaults()]);
      refreshIndexStatus();
    } catch (err) {
      importError = describeError(err);
    } finally {
      vaultBusy = "";
    }
  }

  // Subscribe to a PUBLIC vault by its URL. The success notice names the publisher FINGERPRINT —
  // the identity pinned on this first contact, which every later update must match.
  async function subscribeVault() {
    const url = subUrl.trim();
    if (!url) return;
    vaultBusy = "subscribe";
    subscribeError = "";
    notice = "";
    try {
      const r = await api.subscribeVault(url);
      notice =
        `Subscribed to “${r.name}” from ${r.url_host} — publisher ${r.publisher} (now pinned) — ` +
        `${r.added} new document${r.added === 1 ? "" : "s"}${r.duplicates ? `, ${r.duplicates} you already had` : ""}. ` +
        (r.vectors_used
          ? "It is searchable now."
          : "Meaning search needs a reindex (the vault was built with a different embedding model).");
      subUrl = "";
      await Promise.all([loadDocs(), loadVaults()]);
      refreshIndexStatus();
    } catch (err) {
      subscribeError = describeError(err);
    } finally {
      vaultBusy = "";
    }
  }

  // The host a subscription updates from — shown on the card. Never the full URL: its path can
  // name the topic as plainly as the vault name would.
  function hostOf(url: string): string {
    try {
      return new URL(url).hostname;
    } catch {
      return "";
    }
  }

  // --- subscription updates: check, apply, and (after a key change) trust -----------------------

  // Per-vault inline state for the check/update flow. Inline is the hard rule: the result, the
  // "Update now" button, and any error live ON the card that was clicked — never page-bottom.
  type UpdState =
    | { kind: "checking" }
    | { kind: "uptodate" }
    | { kind: "available"; from: number; to: number; retired: boolean }
    | { kind: "updating" }
    | { kind: "applied"; summary: string; retired: boolean; renamedFrom: string | null }
    | { kind: "rollback" }
    | { kind: "error"; message: string };
  let updates = $state<Record<string, UpdState>>({});
  // Trusting a NEW publisher key: one open confirm panel at a time, passphrase re-entered.
  let trustOpenId = $state<string | null>(null);
  let trustPass = $state("");
  let trustError = $state(""); // inline, next to the passphrase field — same rule as shareError
  let trustBusy = $state(false);

  // A URL ending /manifest.json is a hosted TREE: checking fetches only that small file. Anything
  // else is a single-file host, and honesty demands the tooltip say a check re-downloads it all.
  function isTreeHost(url: string): boolean {
    return url.endsWith("/manifest.json");
  }

  function updateSummary(r: { added?: number; updated?: number; deleted?: number; kept_yours?: number }): string {
    const parts: string[] = [];
    if (r.updated) parts.push(`${r.updated} updated`);
    if (r.added) parts.push(`${r.added} added`);
    if (r.deleted) parts.push(`${r.deleted} removed`);
    // kept_yours = documents that stayed the user's own (edited, detached, or already theirs).
    if (r.kept_yours) parts.push(`${r.kept_yours} kept (yours — your edits stay yours)`);
    return parts.length ? parts.join(", ") : "nothing changed";
  }

  async function checkUpdates(v: Vault) {
    updates = { ...updates, [v.id]: { kind: "checking" } };
    try {
      const r = await api.checkVaultUpdates(v.id);
      updates = {
        ...updates,
        [v.id]: r.rollback
          ? { kind: "rollback" }
          : r.behind
            ? { kind: "available", from: r.seq, to: r.remote_seq, retired: r.retired }
            : { kind: "uptodate" },
      };
      await loadVaults(); // last_checked moved
    } catch (err) {
      updates = { ...updates, [v.id]: { kind: "error", message: describeError(err) } };
      await loadVaults(); // a 409 may have just BLOCKED the pin — the card must show the warning
    }
  }

  async function applyUpdate(v: Vault) {
    updates = { ...updates, [v.id]: { kind: "updating" } };
    try {
      const r = await api.updateVault(v.id);
      updates = { ...updates, [v.id]: {
        kind: "applied", summary: updateSummary(r), retired: r.retired, renamedFrom: r.renamed_from,
      } };
      await Promise.all([loadDocs(), loadVaults()]);
      refreshIndexStatus(); // changed documents re-embed in the background
    } catch (err) {
      updates = { ...updates, [v.id]: { kind: "error", message: describeError(err) } };
      await loadVaults();
    }
  }

  async function trustPublisher(v: Vault) {
    const offered = v.source?.blocked?.offered_pubkey;
    if (!offered || !trustPass || trustBusy) return;
    trustBusy = true;
    trustError = "";
    try {
      // The exact key being blessed rides along: if the host rotated AGAIN since this warning was
      // rendered, the backend refuses rather than pinning a key nobody confirmed.
      const r = await api.trustVaultPublisher(v.id, offered, trustPass);
      notice = `Re-pinned “${v.name}” to the new publisher key ${r.pinned_fingerprint}. Check for updates again.`;
      trustOpenId = null;
      trustPass = "";
      const next = { ...updates };
      delete next[v.id];
      updates = next;
      await loadVaults();
    } catch (err) {
      trustError = describeError(err);
    } finally {
      trustBusy = false;
    }
  }

  // --- opt-in scheduled auto-update (Stage E) ---------------------------------------------------
  // Off by default. When on, a background pass on the Desktop applies CLEAN updates while unlocked
  // and reports what it did in the Chat feed. It never applies a publisher key change on its own —
  // that still blocks and waits for you. Errors are inline on the card (never page-bottom).
  let subBusy = $state<Record<string, boolean>>({});
  let subErr = $state<Record<string, string>>({});

  async function saveSubscription(
    v: Vault,
    opts: { auto_update?: boolean; check_interval_seconds?: number },
  ) {
    subBusy = { ...subBusy, [v.id]: true };
    subErr = { ...subErr, [v.id]: "" };
    try {
      await api.setSubscription(v.id, opts);
      await loadVaults();
    } catch (err) {
      subErr = { ...subErr, [v.id]: describeError(err) };
    } finally {
      subBusy = { ...subBusy, [v.id]: false };
    }
  }

  // "Last checked" text for the card — a relative phrase ("2 hours ago"), which reads at a glance
  // and is itself the "is this stale?" signal. last_checked is a UTC timestamp (null = never yet).
  function relativeSince(iso: string | null | undefined): string {
    if (!iso) return "never";
    const then = new Date(iso).getTime();
    if (!Number.isFinite(then)) return "never";
    const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (secs < 60) return "just now";
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    const days = Math.round(hours / 24);
    return `${days} day${days === 1 ? "" : "s"} ago`;
  }
  function lastCheckedText(v: Vault): string {
    return `Last checked ${relativeSince(v.source?.last_checked)}`;
  }
  // Absolute timestamp for the hover title — the exact time backs up the relative phrase.
  function lastCheckedAbs(v: Vault): string {
    return v.source?.last_checked ? new Date(v.source.last_checked).toLocaleString() : "";
  }
</script>

{#if account.status?.unlocked}
  <h1>Knowledge</h1>

  <!-- Adding by note, file, or URL works on the phone too — the backend allows it (unlock-only,
       25 MB body cap on the bridge). The desktop-only fences are further down: sharing/exporting a
       vault, and trusting a rotated publisher key. -->
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
      <p class="muted">PDF, Word, PowerPoint, Excel, text, Markdown, HTML, CSV, JSON</p>
    </div>

    <label for="kburl" style="margin-top:1rem">…or add a web page / PDF by URL</label>
    <div style="display:flex; gap:0.5rem; flex-wrap:wrap">
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
    {#if failures.length}
      <!-- One unreadable file must not silently swallow the rest of a drop — name the ones that failed. -->
      <ul class="muted" style="margin:0.35rem 0 0; padding-left:1.1rem; font-size:0.85rem">
        {#each failures.slice(0, 5) as f (f)}<li>{f}</li>{/each}
        {#if failures.length > 5}<li>…and {failures.length - 5} more</li>{/if}
      </ul>
    {/if}
    {#if indexPending > 0}
      <!-- Uploads return as soon as the document is stored; the vectors follow. Say so, rather than
           looking finished while semantic search still can't see the new documents. -->
      <p class="muted" style="margin-top:0.5rem; font-size:0.85rem">
        Indexing for meaning search — {indexTotal - indexPending} of {indexTotal} done. Keyword
        search already finds them.
      </p>
    {/if}
    {#if summaryTotal > 0 && summarized < summaryTotal}
      <!-- The background summary tree builds document-by-document; until a document's tree is
           done, Chat summarizes what's covered and says so. -->
      <p class="muted" style="margin-top:0.25rem; font-size:0.85rem">
        Preparing instant summaries — {summarized} of {summaryTotal} documents ready.
      </p>
    {/if}
    <p class="muted" style="margin-top:0.5rem; font-size:0.85rem">
      You can also ask in Chat: <em>“add this PDF to my knowledge: &lt;url&gt;”</em>.
    </p>
  </div>

  <div class="card">
    <h2>Search</h2>
    <form onsubmit={search} style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
      <input style="flex:1; min-width:12rem" bind:value={query} placeholder="Search your knowledge…" aria-label="Search your knowledge" />
      {#if vaults.length > 0}
        <!-- Scope: search everything, or only inside one vault. -->
        <select bind:value={scope} style="width:auto" aria-label="Search in">
          <option value="">All knowledge</option>
          {#each vaults as v (v.id)}
            <option value={v.id}>{v.name}</option>
          {/each}
        </select>
      {/if}
      <select bind:value={mode} style="width:auto" aria-label="Search mode">
        <option value="hybrid">Best</option>
        <option value="lexical">Keyword</option>
        <option value="semantic">Meaning</option>
      </select>
      <button disabled={busy === "search"} type="submit">Search</button>
    </form>
    {#if scope}
      <p class="muted" style="margin-top:0.4rem; font-size:0.85rem">
        Searching only inside <strong>{vaults.find((v) => v.id === scope)?.name}</strong>.
        <button class="linklike" type="button" onclick={() => (scope = "")}>Search everything instead</button>
      </p>
    {/if}
    {#if mode !== "lexical"}
      <p class="muted" style="margin-top:0.4rem; font-size:0.85rem">
        {mode === "hybrid" ? "Best" : "Meaning"} search uses a local embedding model — pull the exact tag
        <code>ollama pull nomic-embed-text:v1.5</code> (the bare name won't resolve). Without one it
        falls back to keyword. New uploads are indexed automatically — use <em>Reindex</em> below if
        a result is missing.
      </p>
    {/if}
    {#if results}
      {#if degraded}
        <!-- The old copy assumed Ollama. Someone who set up a cloud key never installed it,
             so "run ollama pull …" was a command for software they don't have — a dead end
             on the feature the product leads with. Send them to the setting instead. -->
        <p class="muted" style="margin-top:0.5rem">
          Showing keyword results — meaning search needs an embedding model.
          <a href="/settings/models">Choose one in Settings → Models</a>, then Reindex.
          A cloud provider you've already connected can do this; a local model keeps it on
          your machine.
        </p>
      {/if}
      {#if results.length === 0}
        <p class="muted" style="margin-top:0.5rem">No matches.</p>
      {/if}
      {#if results.length > 0}
        <p class="muted" style="margin-top:0.5rem; font-size:0.85rem; display:flex; gap:0.4rem; align-items:center; flex-wrap:wrap">
          <span>Click a result to open the document at the matching passage.</span>
          <button
            class="qhelp"
            type="button"
            aria-expanded={scoreHelpOpen}
            aria-label="More about search modes"
            onclick={() => (scoreHelpOpen = !scoreHelpOpen)}
          >?</button>
        </p>
        {#if scoreHelpOpen}
          <p class="muted" style="margin:0.25rem 0 0; font-size:0.85rem">
            <strong>Best</strong> combines both, and is what you usually want — keyword search nails an
            exact name or number, meaning search finds a paraphrase, and each misses what the other catches.
            <strong>Keyword</strong> ranks by relevance (rare words count for more, and a long document
            can't win just by being long). <strong>Meaning</strong> uses local embeddings to match by
            sense rather than wording.
          </p>
        {/if}
      {/if}
      {#each results as r (r.id)}
        <div class="hit">
          <button class="linklike" onclick={() => open(r.id, r.offset)}>{r.title}</button>
          <!-- The citation: which file, which page. Clicking opens the document AT the passage. -->
          {#if r.source || r.page !== null}
            <Chip icon="file" kind="accent" onclick={() => open(r.id, r.offset)} title="Open at this passage">
              {r.source}{#if r.source && r.page !== null}&nbsp;·&nbsp;{/if}{#if r.page !== null}{locator(r)}{/if}
            </Chip>
          {/if}
          <p class="snippet">
            {#each highlight(r.snippet, hitTerms) as seg}{#if seg.hit}<mark>{seg.t}</mark>{:else}{seg.t}{/if}{/each}
          </p>
        </div>
      {/each}
    {/if}
  </div>

  <Modal open={!!selected} label={selected?.title ?? "Document"} size="lg" onclose={() => (selected = null)}>
    {#if selected}
      <h2 class="modal-title">{selected.title}</h2>
      {#if hitOffset !== null}
        <p class="muted opened-at">Opened at the matching passage.</p>
      {/if}
      <div class="kit">
        {#if docParts}
          {docParts.before}{#if docParts.mark}<mark class="passage" bind:this={markEl}>{docParts.mark}</mark>{/if}{docParts.after}
        {/if}
      </div>
      <div class="modal-actions">
        <button class="secondary" disabled={busy === selected.id} onclick={() => remove(selected!.id)}>Delete</button>
        <button onclick={() => (selected = null)}>Close</button>
      </div>
    {/if}
  </Modal>

  <div class="card" bind:this={docsCard}>
    <h2 class="row">
      <span>All documents <span class="muted" style="font-weight:400">· {docs.length}</span></span>
      <span class="spacer"></span>
      <button disabled={busy === "reindex"} onclick={reindex}>
        {busy === "reindex" ? "Reindexing…" : "Reindex (semantic)"}
      </button>
    </h2>
    {#if docs.length === 0}
      <EmptyState icon="book" title="Build your knowledge" body="Drop in a PDF or write a note above — it's encrypted on your device and searchable in seconds." />
    {/if}

    {#if tagFilter}
      <p class="muted" style="margin:0 0 0.25rem; font-size:0.85rem">
        Showing {shownDocs.length} document{shownDocs.length === 1 ? "" : "s"} tagged
        <strong>{tagFilter}</strong>.
        <button class="linklike" type="button" onclick={() => (tagFilter = "")}>Show everything instead</button>
      </p>
    {/if}

    {#if picked.length > 0 || addTarget}
      <!-- The selection only means something in terms of vaults, so the bar that appears offers
           exactly that: put these in a vault. It also shows while a vault's "Add documents" is
           armed but nothing is ticked yet — that state must TELL the user what to do next. -->
      <div class="pickbar">
        {#if picked.length === 0}
          <strong>Tick documents below to add them to “{vaults.find((v) => v.id === addTarget)?.name}”</strong>
          <span class="spacer"></span>
          <button class="secondary" onclick={() => (addTarget = "")}>Cancel</button>
        {:else}
        <strong>{picked.length} selected</strong>
        {#if vaults.length > 0}
          <select bind:value={addTarget} aria-label="Vault to add to">
            <option value="">Choose a vault…</option>
            {#each vaults as v (v.id)}
              <option value={v.id}>{v.name}</option>
            {/each}
          </select>
          <button disabled={vaultBusy === "add" || !addTarget} onclick={addToVault}>
            {vaultBusy === "add" ? "Adding…" : "Add to vault"}
          </button>
        {:else}
          <span class="muted">Name a vault below to create one with these.</span>
        {/if}
        <span class="spacer"></span>
        <button class="secondary" onclick={() => { picked = []; addTarget = ""; }}>Clear</button>
        {/if}
      </div>
    {/if}

    {#each shownDocs as d (d.id)}
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
      {:else if tagEditId === d.id}
        <div style="display:flex; gap:0.5rem; align-items:center; margin-top:0.5rem">
          <input
            style="flex:1"
            bind:value={tagEditValue}
            placeholder="Tags, comma-separated (e.g. property, 2024)"
            aria-label="Tags for {d.title}"
            onkeydown={(e) => e.key === "Enter" && saveTags(d.id)}
          />
          <button disabled={busy === d.id} onclick={() => saveTags(d.id)}>Save</button>
          <button class="secondary" onclick={() => (tagEditId = null)}>Cancel</button>
        </div>
      {:else}
        <div class="docrow">
          <input
            type="checkbox"
            checked={picked.includes(d.id)}
            onchange={() => togglePick(d.id)}
            aria-label="Select {d.title}"
          />
          <div class="fic"><Icon name="file" /></div>
          <button class="dtitle" onclick={() => open(d.id)}>{d.title}</button>
          {#each d.tags ?? [] as t (t)}
            <Chip onclick={() => (tagFilter = t)} title="Show only items tagged “{t}”">{t}</Chip>
          {/each}
          <div class="dactions">
            <button class="ghost" disabled={busy === d.id} onclick={() => startTagEdit(d)}>Tags</button>
            <button class="ghost" disabled={busy === d.id} onclick={() => startRename(d)}>Rename</button>
            <button class="ghost" disabled={busy === d.id} onclick={() => remove(d.id)}>Delete</button>
          </div>
        </div>
      {/if}
    {/each}
  </div>

  <div class="card">
    <h2>Vaults <span class="muted" style="font-weight:400">· {vaults.length}</span></h2>
    <p class="muted" style="margin:0 0 0.75rem; font-size:0.9rem">
      A vault is a named set of your documents. Search inside just that set — or seal the whole thing
      into one file and share it with someone, who can import it and search it themselves.
    </p>

    {#if vaults.length === 0}
      <EmptyState icon="vault" title="Group and share with vaults" body="Tick documents above and name a vault below — search inside just that set, or share it sealed or public." />
    {/if}

    {#if tagFilter}
      <p class="muted" style="margin:0 0 0.25rem; font-size:0.85rem">
        Showing {shownVaults.length} vault{shownVaults.length === 1 ? "" : "s"} tagged
        <strong>{tagFilter}</strong>.
        <button class="linklike" type="button" onclick={() => (tagFilter = "")}>Show everything instead</button>
      </p>
    {/if}

    {#each shownVaults as v (v.id)}
      <div class="vault">
        <div class="vrow">
          <strong class="vname">{v.name}</strong>
          <!-- One state-chip list per vault, from the pure vaultChips mapper: Private / Public /
               Retired / Subscribed / Blocked / Retired-by-publisher / Unreachable / Imported. The
               ordering (dominant chip, then fingerprint, then version) lives inside the helper. -->
          {#each vaultChips(v) as c (c.key)}
            <Chip kind={c.kind} icon={c.icon ?? ""} mono={c.mono ?? false} title={c.title ?? ""}>
              {c.label}
            </Chip>
          {/each}
          {#if v.kind === "imported" && v.source?.url}
            <span class="fp" title="Where this vault is hosted">{hostOf(v.source.url)}</span>
          {/if}
          {#if vaultChipsSummary(v)}
            <span class="fp" title="The date the publisher stamped this version">{vaultChipsSummary(v)}</span>
          {/if}
          {#each v.tags ?? [] as t (t)}
            <Chip onclick={() => (tagFilter = t)} title="Show only items tagged “{t}”">{t}</Chip>
          {/each}
          <button class="linklike" onclick={() => toggleMembers(v)} aria-expanded={openVaultId === v.id}>
            {v.doc_count} document{v.doc_count === 1 ? "" : "s"} <Icon name={openVaultId === v.id ? "chevron-down" : "chevron-right"} size={12} />
          </button>
          <span class="spacer"></span>
          <button class="secondary" onclick={() => startVaultTagEdit(v)} aria-expanded={vaultTagEditId === v.id}>Tags</button>
          {#if v.kind === "imported" && v.source?.url && !v.source?.blocked && !v.source?.retired}
            <!-- Zip-host honesty: with no per-file tree, a "check" re-downloads the whole file.
                 Retired subscriptions hide auto-update entirely, but a MANUAL check still runs
                 on unreachable ones (a dead host coming back clears the flag server-side). -->
            <button
              class="secondary"
              disabled={updates[v.id]?.kind === "checking" || updates[v.id]?.kind === "updating"}
              title={isTreeHost(v.source.url)
                ? "Checks the vault's small manifest file on the host"
                : "This host serves the vault as one file, so checking re-downloads the whole file"}
              onclick={() => checkUpdates(v)}
            >{updates[v.id]?.kind === "checking" ? "Checking…" : "Check for updates"}</button>
          {/if}
          <button class="secondary" onclick={() => startAdding(v)}>Add documents</button>
          <button class="secondary" onclick={() => (scope = v.id)} disabled={scope === v.id}>
            {scope === v.id ? "Searching this" : "Search this"}
          </button>
          {#if remote.status === "idle"}
            <button
              class="secondary"
              onclick={() => {
                exportId = exportId === v.id ? null : v.id;
                exportPass = "";
                exportMode = "sealed"; // private is the default every time the panel opens
                shownKey = "";
                publishedOpen = false;
                shareError = "";
                lastExportHeaders = null; // last export's warnings must not survive a panel toggle
                retireOpenId = null;      // and neither must a half-open Retire panel
                retirePass = "";
                retireError = "";
                // Seed the hosted-URL editor from the vault (empty when unset), and clear any
                // verdict/error a previous vault's Share panel left behind.
                hostedDraft = v.hosted_url ?? "";
                hostedError = "";
                hostedResult = null;
              }}
            >Share…</button>
          {/if}
          <button class="secondary" disabled={vaultBusy === v.id} onclick={() => removeVault(v)}>Delete</button>
        </div>
        {#if v.description}<p class="muted vdesc">{v.description}</p>{/if}

        {#if vaultTagEditId === v.id}
          <div style="display:flex; gap:0.5rem; align-items:center; margin-top:0.5rem">
            <input
              style="flex:1"
              bind:value={vaultTagEditValue}
              placeholder="Tags, comma-separated (e.g. reference, work)"
              aria-label="Tags for {v.name}"
              onkeydown={(e) => e.key === "Enter" && saveVaultTags(v)}
            />
            <button disabled={vaultBusy === v.id} onclick={() => saveVaultTags(v)}>Save</button>
            <button class="secondary" onclick={() => (vaultTagEditId = null)}>Cancel</button>
          </div>
        {/if}

        {#if v.kind === "imported" && v.source?.url && !v.source?.blocked && !v.source?.retired}
          <!-- Opt-in scheduled auto-update (Stage E). Off by default; when on, the Desktop applies
               clean updates while unlocked and posts results into the Chat feed. A key change is
               NEVER applied on a timer — it blocks and waits for you. Hidden on a retired
               subscription: the timer stops server-side too (spec §5), and offering a control
               that only ever no-ops would read as a broken button. -->
          <div class="autoupd">
            <label class="autoupd-toggle">
              <input
                type="checkbox"
                checked={v.source?.auto_update ?? false}
                disabled={subBusy[v.id]}
                onchange={(e) => saveSubscription(v, { auto_update: e.currentTarget.checked })}
              />
              Auto-update
            </label>
            {#if v.source?.auto_update}
              <select
                class="autoupd-interval"
                value={String(v.source?.check_interval_seconds ?? 86400)}
                disabled={subBusy[v.id]}
                onchange={(e) => saveSubscription(v, { check_interval_seconds: Number(e.currentTarget.value) })}
                aria-label="How often to check for updates"
              >
                <option value="86400">Daily</option>
                <option value="604800">Weekly</option>
              </select>
            {/if}
            <span class="muted autoupd-when" title={lastCheckedAbs(v)}>{lastCheckedText(v)}</span>
            {#if v.source?.last_error}
              <!-- Staleness: the last check couldn't reach a fresh vault. The backend keeps the
                   detail HOST-only (never a URL path); it rides the hover title. -->
              <span class="stale autoupd-stale" title={v.source.last_error}>· Last check failed — host may be unreachable</span>
            {/if}
            {#if !isTreeHost(v.source.url)}
              <!-- Zip-host honesty: no per-file tree, so a check re-downloads the whole file. -->
              <span class="muted autoupd-note" title="This host serves the vault as one file — checking re-downloads all of it">· checking re-downloads the whole file</span>
            {/if}
            {#if subErr[v.id]}<span class="error autoupd-err">{subErr[v.id]}</span>{/if}
          </div>
        {/if}

        {#if retiredSubscriptionNote(v)}
          <!-- The publisher retired this vault: no more updates from them, but the documents
               they already gave you stay in your knowledge and remain readable. The chip
               above says the state; this line says what it means for the reader. -->
          <p class="muted subnote">{retiredSubscriptionNote(v)}</p>
        {/if}
        {#if unreachableSubscriptionNote(v)}
          <!-- Two distinct copies, per unreachable_reason: "took_down" (HTTP 410 Gone — an
               intentional publisher takedown) reads differently from "dead_host" (eight
               consecutive failures across a week). Manual Check for updates stays available. -->
          <p class="muted subnote">{unreachableSubscriptionNote(v)}</p>
        {/if}

        {#if v.source?.blocked}
          <!-- The one interruption the design allows itself: a key change must never silently
               succeed, so updates stop and BOTH identities sit side by side until the human
               verifies the new one with the publisher over a channel they trust. -->
          <div class="warn" style="margin-top:0.5rem; font-size:0.85rem">
            <p style="margin:0"><strong>The publisher's key changed — updates are blocked.</strong></p>
            <!-- Both identities side by side, labeled: a human decides between them, never from one
                 fingerprint alone. The pinned one is what they trusted; the offered one is new. -->
            <div class="fp-compare">
              <div class="fp-row">
                <span class="fp-label">Pinned (trusted)</span>
                <span class="fp">{v.pinned_fingerprint}</span>
              </div>
              <div class="fp-row">
                <span class="fp-label">Offered (new)</span>
                <span class="fp">{v.blocked_fingerprint}</span>
              </div>
            </div>
            <p style="margin:0.35rem 0 0">
              This is either the publisher rotating their key — or someone impersonating them.
              Verify the offered fingerprint with the publisher out-of-band (call them, ask in
              person) before trusting it.
            </p>
            {#if remote.status === "idle"}
              {#if trustOpenId === v.id}
                <label for="trust-pass-{v.id}" style="display:block; margin:0.6rem 0 0.25rem">
                  Confirm it's you — enter your <strong>SmartBrain passphrase</strong> to pin the
                  new key (every future update will be trusted from it):
                </label>
                <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
                  <input
                    id="trust-pass-{v.id}"
                    type="password"
                    style="flex:1; min-width:10rem"
                    bind:value={trustPass}
                    placeholder="Your passphrase"
                    autocomplete="current-password"
                    onkeydown={(e) => e.key === "Enter" && trustPass && trustPublisher(v)}
                  />
                  <button disabled={trustBusy || !trustPass} onclick={() => trustPublisher(v)}>
                    {trustBusy ? "Pinning…" : "Trust new key"}
                  </button>
                  <button class="secondary" onclick={() => { trustOpenId = null; trustPass = ""; trustError = ""; }}>
                    Cancel
                  </button>
                </div>
                {#if trustError}<p class="error" style="margin:0.4rem 0 0">{trustError}</p>{/if}
              {:else}
                <p style="margin:0.6rem 0 0">
                  <button class="secondary" onclick={() => { trustOpenId = v.id; trustPass = ""; trustError = ""; }}>
                    I confirmed with the publisher out-of-band that this is really them
                  </button>
                </p>
              {/if}
            {:else}
              <p class="muted" style="margin:0.6rem 0 0">
                Trusting a new key is done on the Desktop, not from a paired device.
              </p>
            {/if}
          </div>
        {:else if updates[v.id]}
          {@const u = updates[v.id]}
          <!-- Inline, on the card that was clicked — the hard rule: never a page-bottom message. -->
          <p class="upd">
            {#if u.kind === "checking"}Checking…{/if}
            {#if u.kind === "uptodate"}Up to date (v{v.source?.seq}).{/if}
            {#if u.kind === "available"}
              {#if u.retired}
                <!-- The pending update is a RETIRE-export: applying it captures the final content
                     and stops auto-updates. Say so before the button, so "Apply" doesn't read as
                     "get more updates". -->
                <strong>The publisher retired this vault at v{u.to} — applying captures the final content and stops checking.</strong>
                <button onclick={() => applyUpdate(v)}>Apply retirement</button>
              {:else}
                <strong>Update available (v{u.from} → v{u.to}).</strong>
                <button onclick={() => applyUpdate(v)}>Update now</button>
              {/if}
            {/if}
            {#if u.kind === "updating"}Updating…{/if}
            {#if u.kind === "applied"}
              {#if u.retired}
                Retired by publisher — your documents remain in Knowledge. {u.summary}.
              {:else}
                Updated — {u.summary}.
              {/if}
              {#if u.renamedFrom}<span class="muted"> (renamed from “{u.renamedFrom}”)</span>{/if}
            {/if}
            {#if u.kind === "rollback"}
              The host is serving an <strong>older</strong> version than you already have — refused.
            {/if}
            {#if u.kind === "error"}<span class="error">{u.message}</span>{/if}
          </p>
        {/if}

        {#if openVaultId === v.id}
          <!-- The vault's contents: what you'd be sharing or searching. Removing takes the document
               out of the GROUPING only — the document itself stays in your knowledge. -->
          <ul class="vmembers">
            {#each members as m (m.id)}
              <li>
                <button class="linklike" onclick={() => open(m.id)}>{titleOf(m.id)}</button>
                {#if m.origin === "import"}
                  <!-- Import-origin = the vault owns this copy: it is read-only and a future vault
                       update may replace it. Detach hands it to the user instead. -->
                  <button
                    class="secondary vremove"
                    title="Make this copy yours — future vault updates will no longer touch it"
                    onclick={() => detachFromVault(v, m.id)}
                  >Detach</button>
                {/if}
                <button
                  class="secondary vremove"
                  title="Remove from this vault (the document itself is kept)"
                  onclick={() => removeFromVault(v, m.id)}
                >Remove</button>
              </li>
            {:else}
              <li class="muted">No documents yet — click “Add documents”.</li>
            {/each}
          </ul>
        {/if}

        {#if exportId === v.id}
          <div class="share">
            <!-- Private (sealed) stays the default and unchanged; Public is an explicit, warned
                 choice — the warning sits BEFORE the export, because after it there is no undo. -->
            <div role="radiogroup" aria-label="How to share" style="display:flex; gap:1.25rem; flex-wrap:wrap; margin-bottom:0.5rem; font-size:0.9rem">
              <label>
                <input type="radio" bind:group={exportMode} value="sealed" /> Private — sealed file + a separate key
              </label>
              <label>
                <input type="radio" bind:group={exportMode} value="open" /> Public — a plain file, no key
              </label>
            </div>
            {#if exportMode === "sealed"}
              <p class="muted" style="margin:0 0 0.5rem; font-size:0.85rem">
                This seals the vault into a single <code>.sbvault</code> file. The file and its key must
                travel <strong>separately</strong> — together they are the contents in the clear. Send the
                file however you like, then read the key out over a different channel.
              </p>
              {#if v.shared_sealed}
                <!-- Every sealed export mints a FRESH Vault Key — anyone holding the previous
                     key can no longer open the new file. Warn BEFORE the export, not after, so
                     the user isn't distributing a file that silently orphaned their friends. -->
                <p class="warn" style="margin:0 0 0.5rem; font-size:0.85rem">
                  <strong>Re-sealing issues a new key.</strong> Anyone holding the old key will need
                  the new one to open this file — the previous file is not affected.
                </p>
              {/if}
            {:else}
              <p class="warn" style="margin:0 0 0.5rem; font-size:0.85rem">
                <strong>Public:</strong> anyone with the link can read everything in this vault. There is
                <strong>no key</strong>, and there is <strong>no taking it back</strong>.
              </p>
            {/if}
            <label for="share-pass-{v.id}" style="display:block; margin-bottom:0.25rem; font-size:0.85rem">
              Confirm it's you — enter your <strong>SmartBrain passphrase</strong> (exporting hands
              out everything in this vault):
            </label>
            <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
              <input
                id="share-pass-{v.id}"
                type="password"
                style="flex:1; min-width:10rem"
                bind:value={exportPass}
                placeholder="Your passphrase"
                autocomplete="current-password"
                onkeydown={(e) => e.key === "Enter" && exportPass && exportVault(v)}
              />
              <button disabled={vaultBusy === v.id || !exportPass} onclick={() => exportVault(v)}>
                {#if vaultBusy === v.id}
                  {exportMode === "open" ? "Publishing…" : "Sealing…"}
                {:else if exportMode === "open" && v.published_open}
                  <!-- Already public: a re-export is the NEXT version. The seq auto-bumps server-side
                       (bump_version), so the label just names where it lands. -->
                  Export update (v{v.version + 1})
                {:else}
                  Export
                {/if}
              </button>
            </div>
            {#if shareError}<p class="error" style="margin:0.4rem 0 0">{shareError}</p>{/if}
            {#if lastExportHeaders?.unchanged && lastExportHeaders?.seq !== null}
              <!-- Unchanged republish: server-side content-only fingerprint matched the previous
                   export. Not an error — just a "did you mean to?" nudge before the user distributes
                   a file that will look like an update but ship no changes. -->
              <p class="muted" style="margin:0.4rem 0 0; font-size:0.85rem">
                Nothing changed since v{lastExportHeaders.seq} — you published an identical version.
              </p>
            {/if}
            {#if lastExportHeaders?.rotatedKey}
              <!-- A sealed re-export always mints a fresh key. Say so AFTER the fact too so a user
                 who dismissed the pre-export warning still sees the consequence beside the new key. -->
              <p class="warn" style="margin:0.4rem 0 0; font-size:0.85rem">
                <strong>This issued a NEW key</strong> — the previous key no longer opens the new file.
              </p>
            {/if}
            {#if shownKey}
              <p style="margin:0.75rem 0 0.25rem; font-size:0.9rem">
                <strong>Vault key.</strong> Send this to them <em>separately</em> from the file:
              </p>
              <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
                <code class="key">{shownKey}</code>
                <button class="secondary" onclick={copyKey}>{keyCopied ? "Copied ✓" : "Copy key"}</button>
              </div>
            {:else if publishedOpen}
              <!-- No key row: there is nothing to copy, and pretending otherwise would imply a
                   protection that doesn't exist. Hosting is docs, not an uploader (Stage B). -->
              <p class="muted" style="margin:0.75rem 0 0; font-size:0.85rem">
                <strong>Published.</strong> Upload the file anywhere (Drive, S3, any web host) and share
                the link — or unzip it and upload the folder to a static host so future updates only
                re-upload what changed. Replace the file in place to publish a new version; anyone
                subscribed picks it up on their next update check.
              </p>
            {/if}

            {#if v.published_open}
              <!-- Publisher-local note of WHERE the .sbvault was uploaded, so verify-hosted can
                   catch the classic "published v9 but forgot to upload the new file" gap. The
                   URL never travels in the export — it's just this install's memo. -->
              <div class="hosted">
                <label for="hosted-{v.id}" style="display:block; margin-bottom:0.25rem; font-size:0.85rem">
                  <strong>Hosted at.</strong> Where you uploaded this vault. Only this install sees it —
                  it's a note so SmartBrain can check the hosted copy against your last publish.
                </label>
                <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
                  <input
                    id="hosted-{v.id}"
                    style="flex:1; min-width:14rem"
                    bind:value={hostedDraft}
                    placeholder="https://example.com/vaults/my-vault.sbvault"
                    aria-label="Where this vault is hosted"
                    onkeydown={(e) => e.key === "Enter" && saveHostedUrl(v)}
                  />
                  <button disabled={hostedBusy !== ""} onclick={() => saveHostedUrl(v)}>
                    {hostedBusy === "save" ? "Saving…" : "Save"}
                  </button>
                  {#if v.hosted_url}
                    <!-- Verify only lights up once a URL is stored — a button that immediately
                         400s on click would read as broken. -->
                    <button class="secondary" disabled={hostedBusy !== ""} onclick={() => verifyHosted(v)}>
                      {hostedBusy === "verify" ? "Checking…" : "Verify hosted copy"}
                    </button>
                  {/if}
                </div>
                {#if hostedError}<p class="error" style="margin:0.4rem 0 0">{hostedError}</p>{/if}
                {#if hostedResult}
                  <!-- The server phrased every verdict for a human ("hosted file matches…",
                       "did you forget to upload…", "signature isn't yours…", "upstream 410"),
                       so the UI renders `detail` verbatim and just picks a style: ok when it
                       matches, warn on any anomaly, muted when the host is unreachable. -->
                  <p class="hosted-note {hostedResultClass(hostedResult)}" style="margin:0.4rem 0 0; font-size:0.85rem">
                    {hostedResult.detail}
                  </p>
                {/if}
              </div>
            {/if}

            {#if v.published_open && !v.retired_published}
              <!-- Retire: the last publish. Only meaningful on a live public vault, and only from
                   the Desktop (same gate as export — the produced file is decrypted plaintext).
                   Two-step: an explainer BEFORE the passphrase field, so a mis-click on Retire…
                   never opens a passphrase prompt on the user unannounced. -->
              <div class="retire">
                {#if retireOpenId === v.id}
                  <p class="muted" style="margin:0 0 0.4rem; font-size:0.85rem">
                    <strong>Retire this vault.</strong> This produces one final version subscribers
                    apply — the content they already have stays in their Knowledge, and their
                    check-for-updates stops. You can publish again later to un-retire.
                  </p>
                  <label for="retire-pass-{v.id}" style="display:block; margin-bottom:0.25rem; font-size:0.85rem">
                    Confirm it's you — enter your <strong>SmartBrain passphrase</strong> to retire this vault:
                  </label>
                  <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
                    <input
                      id="retire-pass-{v.id}"
                      type="password"
                      style="flex:1; min-width:10rem"
                      bind:value={retirePass}
                      placeholder="Your passphrase"
                      autocomplete="current-password"
                      onkeydown={(e) => e.key === "Enter" && retirePass && retireVault(v)}
                    />
                    <button disabled={vaultBusy === v.id || !retirePass} onclick={() => retireVault(v)}>
                      {vaultBusy === v.id ? "Retiring…" : "Retire vault"}
                    </button>
                    <button class="secondary" onclick={() => { retireOpenId = null; retirePass = ""; retireError = ""; }}>
                      Cancel
                    </button>
                  </div>
                  {#if retireError}<p class="error" style="margin:0.4rem 0 0">{retireError}</p>{/if}
                {:else}
                  <button class="secondary" onclick={() => { retireOpenId = v.id; retirePass = ""; retireError = ""; }}>
                    Retire…
                  </button>
                {/if}
              </div>
            {/if}
          </div>
        {/if}

        {#if deleteOpenId === v.id}
          <!-- Two-option delete for subscribed/imported vaults: keep documents (default,
               historical behavior) or also remove the vault's import-origin documents. Owner-
               origin copies always stay — the backend enforces this too. -->
          <div class="warn" style="margin-top:0.5rem; font-size:0.85rem">
            <p style="margin:0"><strong>Delete “{v.name}”?</strong></p>
            <div role="radiogroup" aria-label="What to do with the documents" style="display:flex; flex-direction:column; gap:0.3rem; margin:0.5rem 0">
              <label>
                <input type="radio" name="del-{v.id}" checked={!deleteRemoveDocs} onchange={() => (deleteRemoveDocs = false)} />
                Keep documents — the vault is removed but everything it grouped stays in your Knowledge.
              </label>
              <label>
                <input type="radio" name="del-{v.id}" checked={deleteRemoveDocs} onchange={() => (deleteRemoveDocs = true)} />
                Also remove the vault's imported documents — anything you authored yourself stays either way.
              </label>
            </div>
            <div style="display:flex; gap:0.5rem; flex-wrap:wrap">
              <button disabled={vaultBusy === v.id} onclick={() => doDeleteVault(v, deleteRemoveDocs)}>
                {vaultBusy === v.id ? "Deleting…" : (deleteRemoveDocs ? "Delete vault + documents" : "Delete vault")}
              </button>
              <button class="secondary" onclick={() => (deleteOpenId = null)}>Cancel</button>
            </div>
          </div>
        {/if}
      </div>
    {/each}

    <!-- Organising (create / add / search a vault) works everywhere, phone included — it is not
         egress. Only export (Share…, above) stays Desktop-only. -->
    <div style="display:flex; gap:0.5rem; align-items:center; margin-top:1rem; flex-wrap:wrap">
      <input
        style="flex:1; min-width:10rem"
        bind:value={newVaultName}
        placeholder="New vault name…"
        aria-label="New vault name"
        onkeydown={(e) => e.key === "Enter" && createVault()}
      />
      <button disabled={vaultBusy === "create" || !newVaultName.trim()} onclick={createVault}>
        {#if vaultBusy === "create"}Creating…{:else if picked.length > 0}Create with {picked.length} selected{:else}Create vault{/if}
      </button>
    </div>

    <!-- Import (a .sbvault file, or a subscribe-by-URL) is ingestion — the backend allows it from
         a paired phone. Export/sharing is plaintext-equivalent egress and stays Desktop-only above. -->
    <details style="margin-top:1rem">
      <summary>Add someone else's vault — import a file, or subscribe to a public URL</summary>
      <p class="muted" style="margin:0.5rem 0; font-size:0.85rem">
        Pick the <code>.sbvault</code> file and paste the key they sent you (a <strong>public</strong>
        file has no key — leave it empty). Its documents are re-encrypted under <em>your</em>
        passphrase as they land, and anything you already have is kept as-is rather than
        overwritten. A newer file of a vault you already have applies as an <em>update</em> to it.
      </p>
      <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
        <input bind:this={importInput} type="file" accept=".sbvault" aria-label="Vault file" />
        <input
          style="flex:1; min-width:10rem"
          bind:value={importKey}
          placeholder="SBVK1-… (empty for a public file)"
          aria-label="Vault key"
        />
        <button disabled={vaultBusy === "import"} onclick={importVault}>
          {vaultBusy === "import" ? "Importing…" : "Import"}
        </button>
      </div>
      {#if importError}<p class="error" style="margin:0.4rem 0 0">{importError}</p>{/if}

      <p class="muted" style="margin:0.9rem 0 0.35rem; font-size:0.85rem">
        …or add a <strong>public</strong> vault by URL — no file, no key. Paste the link to the
        <code>.sbvault</code> file, or — if the publisher hosts the unzipped folder — to its
        <code>manifest.json</code> (updates then download only what changed). It is fetched from
        the public internet, checked against its publisher's signature, and re-encrypted under
        <em>your</em> passphrase as it lands. The publisher is <strong>pinned on first
        contact</strong>: future updates must come from the same identity.
      </p>
      <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
        <input
          style="flex:1; min-width:12rem"
          bind:value={subUrl}
          placeholder="https://example.com/expert-pack.sbvault"
          aria-label="Public vault URL"
          onkeydown={(e) => e.key === "Enter" && subUrl.trim() && subscribeVault()}
        />
        <button disabled={vaultBusy === "subscribe" || !subUrl.trim()} onclick={subscribeVault}>
          {vaultBusy === "subscribe" ? "Subscribing…" : "Subscribe"}
        </button>
      </div>
      {#if subscribeError}<p class="error" style="margin:0.4rem 0 0">{subscribeError}</p>{/if}
    </details>

    <!-- Feeds: a vault that fills itself. Fetches happen from THIS machine on its own schedule
         (nothing goes through any SmartBrain server), and articles are stored encrypted like
         every other document. Add/unsubscribe are Desktop-local; the paste is the consent. -->
    <details style="margin-top:1rem" open={feeds.length > 0}>
      <summary>Follow a website — subscribe to its RSS/Atom feed{feeds.length ? ` · ${feeds.length}` : ""}</summary>
      <p class="muted" style="margin:0.5rem 0; font-size:0.85rem">
        Paste a feed URL and new posts are saved into their own vault as searchable documents —
        checked every few hours, fetched directly from this machine, encrypted like everything
        else. Ask about them in chat, or use them in schedules.
      </p>
      <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">
        <input
          style="flex:1; min-width:12rem"
          bind:value={feedUrl}
          placeholder="https://example.com/feed.xml"
          aria-label="Feed URL"
          onkeydown={(e) => e.key === "Enter" && feedUrl.trim() && addFeed()}
        />
        <button disabled={feedBusy === "add" || !feedUrl.trim()} onclick={addFeed}>
          {feedBusy === "add" ? "Subscribing…" : "Subscribe"}
        </button>
      </div>
      {#if feedError}<p class="error" style="margin:0.4rem 0 0">{feedError}</p>{/if}
      {#each feeds as f (f.id)}
        <div class="feed-row">
          <div style="flex:1; min-width:10rem">
            <strong>{f.title}</strong>
            <span class="muted" style="font-size:0.85rem"> · {new URL(f.url).hostname}</span>
            <div class="muted" style="font-size:0.8rem">
              {f.last_checked ? `checked ${f.last_checked.slice(0, 16)} — ${f.last_status}` : "not checked yet"}
            </div>
          </div>
          <button disabled={feedBusy === f.id} onclick={() => refreshFeed(f)}>
            {feedBusy === f.id ? "Working…" : "Refresh"}
          </button>
          <button disabled={feedBusy === f.id} onclick={() => (feedConfirmId = feedConfirmId === f.id ? null : f.id)}>
            Unsubscribe
          </button>
        </div>
        {#if feedConfirmId === f.id}
          <!-- Same two-option rule as vault delete: the grouping goes; the articles are the
               user's documents and stay unless they explicitly ask for them gone. -->
          <div class="feed-confirm">
            <span class="muted" style="font-size:0.85rem">Stop following, and its saved articles?</span>
            <button disabled={feedBusy === f.id} onclick={() => unsubscribeFeed(f, false)}>Keep articles</button>
            <button class="danger" disabled={feedBusy === f.id} onclick={() => unsubscribeFeed(f, true)}>
              Delete articles too
            </button>
            <button onclick={() => (feedConfirmId = null)}>Cancel</button>
          </div>
        {/if}
      {/each}
    </details>
  </div>

  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <Spinner block />
{/if}

<style>
  /* --- feeds ----------------------------------------------------------------------------- */
  .feed-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    margin-top: 0.75rem;
  }
  .feed-confirm {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    margin-top: 0.4rem;
  }

  /* --- vaults ---------------------------------------------------------------------------- */
  .pickbar {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    margin-top: 0.75rem;
    padding: var(--s-2) var(--s-3);
    border: 1px solid var(--accent);
    border-radius: var(--r-1);
    background: var(--accent-tint);
  }

  /* Document rows: list/card hybrid — icon chip, title as the row's action, quiet
     Rename/Delete that don't shout on every line. */
  .docrow {
    display: flex;
    align-items: center;
    gap: var(--s-3);
    padding: var(--s-2) var(--s-1);
    border-radius: var(--r-1);
    transition: background var(--t-fast);
  }
  .docrow:hover {
    background: var(--elevated);
  }
  .docrow + .docrow {
    border-top: 1px solid var(--border);
  }
  .docrow .fic {
    width: 30px;
    height: 30px;
    flex: none;
    border-radius: var(--r-1);
    background: var(--accent-tint);
    color: var(--accent);
    display: grid;
    place-items: center;
  }
  .docrow .dtitle {
    flex: 1;
    min-width: 0;
    text-align: left;
    background: transparent;
    border: 0;
    padding: 6px 0;
    color: var(--text);
    font-size: var(--f-label);
    font-weight: 550;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: pointer;
  }
  .docrow .dtitle:hover {
    color: var(--accent);
    filter: none;
  }
  .docrow .dactions {
    display: flex;
    gap: 2px;
    flex: none;
  }
  .opened-at {
    margin: 0 0 0.5rem;
    font-size: 0.85rem;
  }

  .vault {
    margin-top: 0.6rem;
    padding: 0.6rem;
    border: 1px solid var(--border);
    border-radius: 6px;
  }
  .vrow {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
  }
  /* The vault name shrinks (rather than pushing chips off the row) on a narrow layout — same
     ellipsis idiom .docrow .dtitle uses, so a long name never breaks the row. */
  .vrow .vname {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .vdesc {
    margin: 0.35rem 0 0;
    font-size: 0.85rem;
  }
  /* Subscription state note: the muted sentence that expands on a chip whose meaning matters
     ("Retired by publisher — your documents remain…", "Unreachable — dead host / took down"). */
  .subnote {
    margin: 0.35rem 0 0;
    font-size: 0.85rem;
  }
  /* Retire… lives at the bottom of the share panel: quiet button on a live public vault, and
     an inline explanation + passphrase when opened. Same top rule as .share, so the panels
     read as sibling steps rather than a nested overlay. */
  .retire {
    margin-top: 0.6rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--border);
  }
  /* Hosted-URL editor: same sibling-step rule as .retire — an inline row inside the Share
     panel, separated by a divider so it doesn't look grafted onto the export controls. */
  .hosted {
    margin-top: 0.6rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--border);
  }
  /* Verify-hosted verdict lines: the backend picks the words; the UI picks the color. Matches
     reads green (safe to distribute), any anomaly reads warn (behind / newer / wrong-signature
     — the user has to decide something), unreachable is muted (a network fact, not the user's
     fault). Keep the same font-size as .subnote so it sits in the same visual register. */
  .hosted-note.hosted-ok {
    color: var(--ok);
  }
  .hosted-note.hosted-warn {
    color: var(--warn);
  }
  /* Phones: the .vrow can hold many chips (state, fingerprint, version, publish date, tags),
     and desktop-only flex-wrap alone still lets the actions push off the right edge before
     the chips wrap — same failure .docrow already fixed. Force a compact scale so state
     stays readable, then let the actions flow to their own line. */
  @media (max-width: 480px) {
    .vrow {
      gap: 0.35rem;
    }
    .vrow .spacer {
      /* On a phone the spacer would leave the action buttons stranded on the right; wrapping
         them to the next line reads better than a stretched-out top row. */
      flex-basis: 100%;
      height: 0;
    }
  }

  /* Inline check/update result — it lives ON the card that was clicked, never page-bottom. */
  .upd {
    margin: 0.5rem 0 0;
    font-size: 0.85rem;
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
  }

  /* Opt-in auto-update controls — a quiet row under the subscription, same compact scale as .upd. */
  .autoupd {
    margin: 0.4rem 0 0;
    font-size: 0.85rem;
    display: flex;
    gap: 0.6rem;
    align-items: center;
    flex-wrap: wrap;
  }
  .autoupd-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .autoupd-interval {
    font-size: 0.8rem;
    padding: 0.1rem 0.3rem;
  }
  .autoupd-when,
  .autoupd-stale,
  .autoupd-note {
    font-size: 0.8rem;
  }
  .autoupd-err {
    font-size: 0.8rem;
  }
  /* A failed check: not an error the user caused, but a signal the card must not hide. */
  .stale {
    color: var(--danger, #c0392b);
  }

  /* The expanded "what's inside" list — compact, one document per row. */
  .vmembers {
    margin: 0.5rem 0 0;
    padding-left: 1.1rem;
    font-size: 0.9rem;
  }
  .vmembers li {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-top: 0.25rem;
  }
  .vremove {
    padding: 0 0.45rem;
    font-size: 0.75rem;
  }

  /* The publisher fingerprint (SB-…): monospace because it is read/compared character by
     character — it is the identity subscribers pin, never mere decoration. */
  .fp {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.7rem;
    color: var(--muted);
  }

  /* The key-change comparison: the pinned and offered fingerprints on their own labeled rows, so
     the two identities are read side by side (the one human trust decision the model rests on). */
  .fp-compare {
    margin: 0.5rem 0 0;
    display: grid;
    gap: 0.25rem;
  }
  .fp-row {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    flex-wrap: wrap;
  }
  .fp-label {
    min-width: 8rem;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .fp-row .fp {
    font-size: 0.85rem;
    color: var(--text);
  }

  /* The no-take-backs warning shown BEFORE a public export (same treatment as setup's .warn). */
  .warn {
    border: 1px solid var(--danger, #c0392b);
    background: color-mix(in srgb, var(--danger, #c0392b) 10%, transparent);
    color: var(--text);
    padding: 0.5rem 0.75rem;
    border-radius: 8px;
  }

  .share {
    margin-top: 0.6rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--border);
  }

  /* The vault key. Monospace and wrapping: it gets read aloud or copied, and a clipped key is a
     key the recipient cannot use. */
  .key {
    flex: 1;
    min-width: 12rem;
    padding: 0.35rem 0.5rem;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--field);
    font-size: 0.8rem;
    word-break: break-all;
  }

  /* --- search hits as citations --------------------------------------------------------- */
  .hit {
    margin-top: 0.9rem;
  }

  .snippet {
    margin: 0.2rem 0 0;
    color: var(--muted);
    line-height: 1.45;
    overflow-wrap: anywhere; /* a long URL in a snippet must wrap, not widen the page */
  }

  /* Matched terms in a snippet, and the matched passage inside an opened document. */
  .snippet mark,
  .kit mark {
    background: color-mix(in srgb, var(--warn) 35%, transparent);
    color: var(--text);
    border-radius: 3px;
    padding: 0 0.1em;
  }
  .kit mark.passage {
    background: color-mix(in srgb, var(--warn) 22%, transparent);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--warn) 22%, transparent);
  }

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
  /* .linklike is now global (app.css) — one text-button voice everywhere. */
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

</style>
