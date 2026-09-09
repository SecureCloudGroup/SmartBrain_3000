<script lang="ts">
  // Neural Interface board — every item on one card grid. Items render their BOUND
  // scene (§4.3, delivered by GET /api/ni/board) through <NiScene>; nothing here
  // touches the untrusted payload except to pass it to the validated renderer.
  //
  // Polls softly (10s, visible tab + unlocked only, errors swallowed). The card CTAs
  // — Run now, Pause/Resume, Delete, and the commissioning C2 "Looks right/Something's
  // wrong" strip — mirror ni-format §10 verbs exactly.
  import { onDestroy, onMount } from "svelte";
  import { goto } from "$app/navigation";
  import Chip from "$lib/components/Chip.svelte";
  import EmptyState from "$lib/components/EmptyState.svelte";
  import Modal from "$lib/components/Modal.svelte";
  import NiScene from "$lib/components/NiScene.svelte";
  import Spinner from "$lib/components/Spinner.svelte";
  import { account } from "$lib/account.svelte";
  import { api, type NiBoardItem, type NiState } from "$lib/api";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";
  import { isStale, relTime } from "$lib/ni/time";

  let items = $state<NiBoardItem[]>([]);
  let loaded = $state(false);
  let error = $state("");
  let busyId = $state<string | null>(null);
  // Set while the "Something's wrong" verdict POST is in flight — disables the modal
  // Send button so a double-tap can't fire two /validate calls for the same item.
  let sendingNote = $state(false);
  let timer: ReturnType<typeof setInterval> | null = null;
  // Request-generation counter — a stale load() response never overwrites newer state
  // (e.g. a delete + re-poll racing an earlier slow board fetch would resurrect the card).
  let gen = 0;
  // Small "Something's wrong" note prompt (C2 verdict, §6).
  let noteFor = $state<NiBoardItem | null>(null);
  let noteText = $state("");

  async function load() {
    console.assert(typeof api.niBoard === "function", "load: niBoard method present");
    console.assert(Array.isArray(items), "load: items array");
    if (!account.status?.unlocked) return;
    const g = ++gen;
    try {
      const r = await api.niBoard();
      if (g !== gen) return; // a newer load() started while we awaited — drop this response
      // position asc — the spec orders the board that way; the server MAY already sort,
      // but a client-side re-sort keeps a mid-migration server honest.
      items = [...r.items].sort((a, b) => a.position - b.position);
      error = "";
    } catch (err) {
      if (g !== gen) return; // stale failure — the newer request will paint the truth
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      if (g === gen) loaded = true;
    }
  }

  function onVisible() {
    console.assert(typeof document !== "undefined", "onVisible: DOM available");
    console.assert(document.visibilityState !== undefined, "onVisible: visibilityState present");
    if (document.visibilityState === "visible") void load();
  }

  onMount(async () => {
    // Mount guard mirrors schedules/+page.svelte: without the account status, a cold
    // navigation would sit on the spinner until the next 10s tick (load() bails when
    // status is null). Redirect uninitialized/locked before any board fetch fires.
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    void load();
    // Poll only while the tab is visible AND the vault is unlocked (guarded inside load).
    timer = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 10_000);
    document.addEventListener("visibilitychange", onVisible);
  });
  onDestroy(() => {
    if (timer) clearInterval(timer);
    document.removeEventListener("visibilitychange", onVisible);
  });

  // Cold-navigate → unlock flow: when the vault flips from locked to unlocked in this
  // tab, re-trigger load() so the board paints immediately instead of waiting up to 10s.
  $effect(() => {
    console.assert(typeof account.status?.unlocked !== "undefined" || account.status === null, "$effect: unlocked accessed");
    console.assert(gen >= 0, "$effect: gen counter present");
    if (account.status?.unlocked) void load();
  });

  // Health chip: state (+ stale check) collapses to one calm pill on the card header.
  type ChipDescriptor = { kind: "" | "accent" | "ok" | "warn" | "danger"; label: string };
  function healthChip(item: NiBoardItem): ChipDescriptor {
    console.assert(typeof item.state === "string", "healthChip: state is string");
    console.assert(typeof item.interval_minutes === "number", "healthChip: interval is number");
    const s: NiState = item.state;
    // Chip labels are Title-cased for the whole set (Live / Stale / Degraded / Failing /
    // Broken / Commissioning / Preview / Paused) — a mid-line lowercase pill (the old
    // "stale" / "live" / "failing") reads as unfinished next to the others.
    if (s === "draft") return { kind: "", label: "Preview" };
    if (s === "commissioning") return { kind: "accent", label: "Commissioning" };
    if (s === "broken") return { kind: "danger", label: "Broken" };
    if (s === "failing") return { kind: "warn", label: "Failing" };
    if (s === "degraded") return { kind: "warn", label: "Degraded" };
    if (s === "paused") return { kind: "", label: "Paused" };
    // live: fresh if payload is inside 2x cadence; else stale.
    if (isStale(item.payload_at, item.interval_minutes)) return { kind: "warn", label: "Stale" };
    return { kind: "ok", label: "Live" };
  }

  async function runNow(item: NiBoardItem) {
    console.assert(typeof item.id === "string", "runNow: id is string");
    console.assert(busyId === null || busyId === item.id, "runNow: busy id matches or null");
    busyId = item.id;
    try {
      const r = await api.niRun(item.id);
      // A 200 with status: "error" is a real failure — the payload didn't refresh,
      // and staying silent reads as "did nothing" to the operator. Kind is the
      // host-free error class (e.g. "http_5xx", "contract"); shown when present.
      if (r.status === "error") {
        error = r.kind ? `Run failed (${r.kind}).` : "Run failed.";
      }
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      busyId = null;
    }
  }

  async function togglePause(item: NiBoardItem) {
    console.assert(typeof item.id === "string", "togglePause: id is string");
    console.assert(typeof item.enabled === "boolean", "togglePause: enabled is boolean");
    busyId = item.id;
    try {
      await api.niPatch(item.id, { enabled: !item.enabled });
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      busyId = null;
    }
  }

  async function remove(item: NiBoardItem) {
    console.assert(typeof item.id === "string", "remove: id is string");
    console.assert(typeof item.title === "string", "remove: title is string");
    const ok = await confirmDialog({
      title: "Delete this item?",
      body: `Delete “${item.title}” from your Neural Interface? Its history is removed too.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    // Lock the card's buttons while the DELETE is in flight — otherwise the second
    // click can fire before the row disappears, and it would 404 on the same id.
    busyId = item.id;
    try {
      await api.niDelete(item.id);
      items = items.filter((x) => x.id !== item.id);
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      busyId = null;
    }
  }

  async function activate(item: NiBoardItem) {
    console.assert(item.state === "draft", "activate: only drafts");
    console.assert(typeof item.id === "string", "activate: id is string");
    busyId = item.id;
    try {
      await api.niCommission(item.id);
      await load();
    } catch (err) {
      // 409 detail names the unfilled credential (or other reason it won't commission);
      // describeError passes 4xx messages through verbatim, which is what we want here.
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      busyId = null;
    }
  }

  async function validateLooksRight(item: NiBoardItem) {
    console.assert(item.state === "commissioning", "validateLooksRight: only commissioning");
    console.assert(typeof item.id === "string", "validateLooksRight: id is string");
    busyId = item.id;
    try {
      await api.niValidate(item.id, true);
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      busyId = null;
    }
  }

  function openWrongNote(item: NiBoardItem) {
    console.assert(item.state === "commissioning", "openWrongNote: only commissioning");
    console.assert(noteFor === null, "openWrongNote: no other note prompt open");
    noteFor = item;
    noteText = "";
  }
  async function submitWrongNote() {
    console.assert(noteFor !== null, "submitWrongNote: a target must be set");
    console.assert(typeof noteText === "string", "submitWrongNote: note is string");
    if (!noteFor || sendingNote) return;
    const target = noteFor;
    busyId = target.id;
    sendingNote = true;
    try {
      await api.niValidate(target.id, false, noteText.trim() || undefined);
      noteFor = null;
      noteText = "";
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      busyId = null;
      sendingNote = false;
    }
  }
</script>

{#if account.status?.unlocked}
  <h1>Neural Interface</h1>
  <p class="muted">
    Live data you asked for, on one board. Ask in chat — “show me AAPL every 5 minutes”.
  </p>

  {#if !loaded}
    <Spinner block />
  {:else if items.length === 0}
    <EmptyState
      icon="monitor"
      title="Nothing on your Neural Interface yet"
      body="Ask in chat — “show me AAPL every 5 minutes” — and approve the source. Items appear here."
    >
      <button onclick={() => goto("/chat")}>Open chat</button>
    </EmptyState>
  {:else}
    <div class="ni-grid2">
      {#each items as item (item.id)}
        {@const health = healthChip(item)}
        {@const wide = item.display.size === "wide"}
        {@const preview = item.state === "draft"}
        <div class="card ni-card" class:wide class:preview>
          <div class="ni-head">
            <strong class="ni-title">{item.title}</strong>
            <Chip kind={health.kind}>{health.label}</Chip>
          </div>

          {#if preview}
            <div class="ni-preview-tag">
              <Chip kind="">Preview — sample data</Chip>
            </div>
          {/if}

          <div class="ni-body">
            {#if item.payload}
              <NiScene node={item.payload} />
            {:else}
              <p class="muted" style="margin:0; font-size:var(--f-label)">Waiting for the first run…</p>
            {/if}
          </div>

          {#if preview}
            <div class="ni-actions">
              <button
                disabled={busyId === item.id}
                onclick={() => activate(item)}
                title="Activate — start fetching for real"
              >{busyId === item.id ? "Activating…" : "Activate"}</button>
            </div>
          {/if}

          {#if item.state === "commissioning" && item.payload && item.payload_slot !== "preview"}
            <div class="ni-commission">
              <p style="margin:0 0 var(--s-2); font-size:var(--f-label)">This is live data — is it right?</p>
              <div class="ni-actions">
                <button
                  class="secondary"
                  disabled={busyId === item.id}
                  onclick={() => validateLooksRight(item)}
                >Looks right</button>
                <button
                  class="ghost"
                  disabled={busyId === item.id}
                  onclick={() => openWrongNote(item)}
                >Something’s wrong</button>
              </div>
            </div>
          {/if}

          <div class="ni-foot">
            <span class="muted ni-fresh">{relTime(item.payload_at)}</span>
            <span class="ni-actions">
              <button
                class="ghost"
                disabled={busyId === item.id}
                onclick={() => runNow(item)}
                title="Run now"
              >{busyId === item.id ? "Running…" : "Run now"}</button>
              <button
                class="ghost"
                disabled={busyId === item.id}
                onclick={() => togglePause(item)}
                title={item.enabled ? "Pause" : "Resume"}
              >{item.enabled ? "Pause" : "Resume"}</button>
              <button
                class="ghost"
                disabled={busyId === item.id}
                onclick={() => remove(item)}
                title="Delete"
              >Delete</button>
            </span>
          </div>
        </div>
      {/each}
    </div>
  {/if}

  {#if error}<p class="error">{error}</p>{/if}

  {#if noteFor}
    <Modal
      open
      label="What went wrong?"
      onclose={() => { noteFor = null; noteText = ""; }}
    >
      <h2 class="modal-title">Something’s wrong</h2>
      <p class="modal-body">
        Say what looked off — the assistant redrafts the item from your note.
      </p>
      <textarea
        bind:value={noteText}
        rows="3"
        placeholder="e.g. wrong ticker, or the number is way off"
        aria-label="Note"
      ></textarea>
      <div class="modal-actions" style="margin-top: var(--s-4)">
        <button class="secondary" disabled={sendingNote} onclick={() => { noteFor = null; noteText = ""; }}>Cancel</button>
        <button disabled={sendingNote} onclick={submitWrongNote}>{sendingNote ? "Sending…" : "Send"}</button>
      </div>
    </Modal>
  {/if}
{:else}
  <Spinner block />
{/if}

<style>
  /* Card grid lifted from settings/status .grid2 — auto-fit means the same page reflows
     from three columns on a wide monitor to one on a phone with no breakpoints. */
  .ni-grid2 {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
    gap: var(--s-3);
    margin: var(--s-4) 0;
  }
  .ni-card {
    /* Card padding + border come from .card in app.css; we only add layout inside. */
    display: flex;
    flex-direction: column;
    gap: var(--s-3);
    margin: 0;
  }
  .ni-card.wide { grid-column: span 2; }
  /* Wide cards collapse back to a single column on narrow viewports so a two-span
     card never overflows the grid. Raised from 480px so a two-column layout with a
     14rem minimum track (~448px + gutters ≈ 480–520px) doesn't try to render a
     span-2 card into ~230px and clip the content. */
  @media (max-width: 560px) {
    .ni-card.wide { grid-column: auto; }
  }
  .ni-card.preview {
    border-style: dashed;
    border-color: var(--border-strong);
  }
  .ni-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-2);
  }
  .ni-title {
    font-size: var(--f-label);
    font-weight: 600;
    /* An over-long title (a full URL a template inlined) truncates inside the card
       instead of pushing the grid track wider. */
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }
  .ni-preview-tag { display: flex; }
  .ni-body { min-width: 0; }
  .ni-commission {
    padding: var(--s-3);
    background: var(--accent-tint);
    border-radius: var(--r-1);
  }
  .ni-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-2);
    margin-top: auto; /* keeps footers aligned across cards of different body heights */
    padding-top: var(--s-2);
    border-top: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .ni-fresh { font-size: var(--f-meta); }
  .ni-actions {
    display: inline-flex;
    gap: var(--s-1);
    flex-wrap: wrap;
  }
</style>
