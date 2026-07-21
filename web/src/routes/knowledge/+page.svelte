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
    type KbDoc,
    type KbDocFull,
    type KbHit,
    type SearchMode,
    type Vault,
    type VaultMember,
  } from "$lib/api";
  import { describeError } from "$lib/errors";
  import { highlight, queryTerms } from "$lib/highlight";
  import { remote } from "$lib/remote/connection.svelte";
  import { confirmDialog } from "$lib/confirm.svelte";

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

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await Promise.all([loadDocs(), loadVaults()]);
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
  let indexTimer: ReturnType<typeof setInterval> | null = null;

  async function refreshIndexStatus() {
    try {
      const s = await api.indexStatus();
      indexPending = s.pending;
      indexTotal = s.total;
      if (s.pending > 0 && indexTimer === null) {
        indexTimer = setInterval(refreshIndexStatus, 4000);
      } else if (s.pending === 0 && indexTimer !== null) {
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

  async function removeVault(v: Vault) {
    const ok = await confirmDialog({
      title: `Delete “${v.name}”`,
      // The distinction that matters: this removes a grouping, not the files in it.
      body: "The documents in it stay in your knowledge — only the vault is removed.",
      confirmLabel: "Delete vault",
    });
    if (!ok) return;
    vaultBusy = v.id;
    error = "";
    try {
      await api.deleteVault(v.id);
      if (scope === v.id) scope = "";
      if (addTarget === v.id) addTarget = "";
      await loadVaults();
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
  // hosting hint instead of a key row.
  async function exportVault(v: Vault) {
    if (!exportPass) return;
    vaultBusy = v.id;
    shareError = "";
    notice = "";
    shownKey = "";
    publishedOpen = false;
    try {
      const blob = await api.exportVault(v.id, exportPass, exportMode);
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
    | { kind: "available"; from: number; to: number }
    | { kind: "updating" }
    | { kind: "applied"; summary: string }
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
            ? { kind: "available", from: r.seq, to: r.remote_seq }
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
      updates = { ...updates, [v.id]: { kind: "applied", summary: updateSummary(r) } };
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
      <p class="muted">PDF, Word, PowerPoint, Excel, text, Markdown, HTML, CSV, JSON</p>
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
    <p class="muted" style="margin-top:0.5rem; font-size:0.85rem">
      You can also ask in Chat: <em>“add this PDF to my knowledge: &lt;url&gt;”</em>.
    </p>
  </div>
  {/if}

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
        <p class="muted" style="margin-top:0.5rem">
          Meaning search needs the embedding model — showing keyword results. On the Desktop, run
          <code>ollama pull nomic-embed-text:v1.5</code>, then Reindex.
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
        <div class="docrow">
          <input
            type="checkbox"
            checked={picked.includes(d.id)}
            onchange={() => togglePick(d.id)}
            aria-label="Select {d.title}"
          />
          <div class="fic"><Icon name="file" /></div>
          <button class="dtitle" onclick={() => open(d.id)}>{d.title}</button>
          <div class="dactions">
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

    {#each vaults as v (v.id)}
      <div class="vault">
        <div class="vrow">
          <strong>{v.name}</strong>
          {#if v.kind === "imported"}
            {#if v.source?.url}
              <!-- Subscribed = it arrived from a URL and its publisher is PINNED. Never the badge
                   without the identity behind it, plus the host updates will come from. -->
              <Chip kind="ok" icon="check">Subscribed</Chip>
              <Chip mono title="The pinned publisher — every update must be signed by this identity">{v.pinned_fingerprint}</Chip>
              <span class="fp" title="Where this vault is hosted">{hostOf(v.source.url)}</span>
              {#if v.source?.seq != null}
                <span class="fp" title="The version you currently have (the seq you're pinned at)">v{v.source.seq}</span>
              {/if}
            {:else}
              <Chip>Imported</Chip>
            {/if}
          {/if}
          {#if v.published_open}
            <!-- Published open = irreversibly public. NEVER the label without the identity behind
                 it: the fingerprint is what a subscriber actually pins. -->
            <Chip kind="accent">Public</Chip>
            <Chip mono title="Your publisher fingerprint — how subscribers identify you">{v.publisher_fingerprint}</Chip>
            <span class="fp" title="The published version — subscribers pin this seq and pick up newer ones">v{v.version}</span>
          {/if}
          <button class="linklike" onclick={() => toggleMembers(v)} aria-expanded={openVaultId === v.id}>
            {v.doc_count} document{v.doc_count === 1 ? "" : "s"} <Icon name={openVaultId === v.id ? "chevron-down" : "chevron-right"} size={12} />
          </button>
          <span class="spacer"></span>
          {#if v.kind === "imported" && v.source?.url && !v.source?.blocked}
            <!-- Zip-host honesty: with no per-file tree, a "check" re-downloads the whole file. -->
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
              }}
            >Share…</button>
          {/if}
          <button class="secondary" disabled={vaultBusy === v.id} onclick={() => removeVault(v)}>Delete</button>
        </div>
        {#if v.description}<p class="muted vdesc">{v.description}</p>{/if}

        {#if v.kind === "imported" && v.source?.url && !v.source?.blocked}
          <!-- Opt-in scheduled auto-update (Stage E). Off by default; when on, the Desktop applies
               clean updates while unlocked and posts results into the Chat feed. A key change is
               NEVER applied on a timer — it blocks and waits for you. -->
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
              <strong>Update available (v{u.from} → v{u.to}).</strong>
              <button onclick={() => applyUpdate(v)}>Update now</button>
            {/if}
            {#if u.kind === "updating"}Updating…{/if}
            {#if u.kind === "applied"}Updated — {u.summary}.{/if}
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
          </div>
        {/if}
      </div>
    {/each}

    <!-- Organising (create / add / search a vault) works everywhere, phone included — it is not
         egress. Only export and import are Desktop-only, below. -->
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

    <!-- Import is ingestion and export is plaintext-equivalent egress (the backend refuses it from a
         paired phone), so both are Desktop work — same rule as the "Add to Knowledge" card. -->
    {#if remote.status === "idle"}
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
    {/if}
  </div>

  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <Spinner block />
{/if}

<style>
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
  .vdesc {
    margin: 0.35rem 0 0;
    font-size: 0.85rem;
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
