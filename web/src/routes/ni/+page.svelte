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
  import {
    api,
    type NiBoardItem,
    type NiItemDetail,
    type NiLibraryState,
    type NiState,
    type NiTemplate,
  } from "$lib/api";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";
  import { friendlyErrorClass } from "$lib/ni/errors";
  import { flowStageLabel, isFlowActive } from "$lib/ni/flow";
  import {
    filterTemplates,
    formatFingerprint,
    paramValuesForInstall,
    templateCategories,
    validateParamForm,
  } from "$lib/ni/library";
  import { formatStageJson, stagesFromSpec } from "$lib/ni/proposal";
  import { isStale, relTime } from "$lib/ni/time";
  import { runStatusLabel } from "$lib/runs";
  import { toast } from "$lib/toast.svelte";

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

  // --- Global Library sheet (ni-format §19/§20) ------------------------------------------------
  // One hosted pack, TOFU-pinned server-side; the sheet is a Modal size lg so it never fights
  // the board grid underneath. Every mutating flow is inline (search/filter/install/trust/apply
  // update) — the library never bounces the user off /ni.
  const DEFAULT_LIBRARY_URL = "https://smartbrain.securecloudgroup.com/ni/library.json";
  let libraryOpen = $state(false);
  let library = $state<NiLibraryState | null>(null);
  let libraryLoading = $state(false);
  let libraryError = $state("");
  let libraryConnectUrl = $state(DEFAULT_LIBRARY_URL);
  let libraryBusy = $state(false);
  let librarySearch = $state("");
  let libraryCategory = $state("");
  // Install sub-view: the picked template + its param values. `installNotice` is the success
  // sentence the sheet paints AFTER a POST returns (the new draft appears on the board via the
  // normal poll — the sheet just points at it).
  let installTemplate = $state<NiTemplate | null>(null);
  let installParams = $state<Record<string, string>>({});
  let installBusy = $state(false);
  let installError = $state("");
  let installNotice = $state("");
  // Re-pin after KeyChanged: passphrase re-entry, one at a time, inline error.
  let trustPass = $state("");
  let trustBusy = $state(false);
  let trustError = $state("");
  // Fleet-healing per-item confirm modal (§20): applying resets the item to draft.
  let updatePromptFor = $state<NiBoardItem | null>(null);
  let applyingUpdate = $state(false);

  // L2 proposal review modal (ni-format §23). Opening fetches the item detail so the
  // diff view has the CURRENT extract/transform to render alongside the proposed ones
  // (the /board response ships bound payloads, not the spec). Apply is a TRIAL — the
  // engine auto-reverts on the next failing run — so the copy in the modal says so.
  let proposalFor = $state<NiBoardItem | null>(null);
  let proposalDetail = $state<NiItemDetail | null>(null);
  let proposalLoading = $state(false);
  let proposalBusy = $state(false);
  // The current stages come from proposalDetail.spec; the proposed stages come from
  // proposalDetail.spec._l2_proposal.stages (sealed body — served alongside the spec).
  const proposalCurrent = $derived(
    proposalDetail ? stagesFromSpec(proposalDetail.spec) : { extract: null, transform: null },
  );
  const proposalProposedStages = $derived(readProposedStages(proposalDetail));

  // Per-card repair-policy modal (§23). The overflow row on every card opens this —
  // it lazily fetches niItem(id) so the checkboxes reflect the CURRENT spec's
  // repair_policy (the board row deliberately doesn't carry it — this lever is a
  // detail-view concern, not a first-glance chip). Save posts through the
  // Desktop-local niRepairPolicy; the WebRTC bridge blocks it from a paired phone
  // (which surfaces as a 403 the modal paints via describeError).
  let repairFor = $state<NiBoardItem | null>(null);
  let repairDetail = $state<NiItemDetail | null>(null);
  let repairLoading = $state(false);
  let repairBusy = $state(false);
  let repairError = $state("");
  let repairL1 = $state(true);
  let repairL2Frontier = $state(false);

  // Add-key modal (Status-truth amendments — the previously missing UI). Opens per
  // missing secret from the card's needs_credentials list; the host is prefilled from
  // the item detail's source.url hostname (§10 spec.source), and the value is a
  // password-typed input that is NEVER echoed anywhere and cleared on close.
  let credentialFor = $state<NiBoardItem | null>(null);
  let credentialName = $state("");
  let credentialLabel = $state("");
  let credentialValue = $state("");
  let credentialHost = $state("");
  let credentialLoading = $state(false);
  let credentialBusy = $state(false);
  let credentialError = $state("");

  // NI Foreman P1: composer state — the on-page creation surface.
  let composerText = $state("");
  let composerBusy = $state(false);
  let composerError = $state("");

  async function submitComposer(): Promise<void> {
    console.assert(composerBusy === false, "submitComposer: no concurrent submit");
    const text = composerText.trim();
    if (text.length < 3 || composerBusy) return;
    composerBusy = true;
    composerError = "";
    try {
      await api.niIntake(text);
      composerText = "";
      toast("Card started — watch it build below.");
      await load();
    } catch (err) {
      composerError = describeError(err);
    } finally {
      composerBusy = false;
    }
  }

  async function retryFlow(item: NiBoardItem): Promise<void> {
    console.assert(typeof item.id === "string", "retryFlow: id is string");
    busyId = item.id;
    try {
      await api.niFlowRetry(item.id);
      toast("Retrying the card build.");
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      busyId = null;
    }
  }

  // Fill-param modal (needs_params, 2026-09-14) — the non-secret sibling of Add key.
  // Opens per unfilled referenced param from the card's needs_params list; plain
  // text input (values are ordinary spec data like a ticker or a city, never keys).
  let paramFor = $state<NiBoardItem | null>(null);
  let paramName = $state("");
  let paramLabel = $state("");
  let paramValue = $state("");
  let paramBusy = $state(false);
  let paramError = $state("");

  // Read-only run-history modal — lists the item's `ni_runs` rows (§10 detail),
  // newest first: ts (relTime) + runStatusLabel + friendly error class. Opened from
  // the small "History" link in every card footer; lazily fetches item detail.
  let historyFor = $state<NiBoardItem | null>(null);
  let historyDetail = $state<NiItemDetail | null>(null);
  let historyLoading = $state(false);
  let historyError = $state("");

  // Filtered view of the pinned pack's templates — derived so the search input and category
  // dropdown re-render inline without any imperative "if changed then refilter" bookkeeping.
  const filteredTemplates = $derived(
    library?.templates ? filterTemplates(library.templates, librarySearch, libraryCategory) : [],
  );
  const categories = $derived(library?.templates ? templateCategories(library.templates) : []);
  // The install form's live error surface (the Install button also disables on it). Read only
  // when a template is picked; keyed by param name (empty when nothing wrong).
  const paramFormError = $derived(
    installTemplate ? validateParamForm(installTemplate.params, installParams) : null,
  );

  // Slice the proposed stages out of the item spec's sealed `_l2_proposal` (§23).
  // The detail endpoint serves this alongside the spec; the shape is closed
  // ({stages: {extract?, transform?}}) but the top-level spec type is unstructured,
  // so we defensively pattern-match. Returns null-null when the item has no proposal.
  function readProposedStages(detail: NiItemDetail | null): {
    extract: Record<string, unknown> | null;
    transform: unknown[] | null;
  } {
    console.assert(detail === null || typeof detail === "object", "readProposedStages: detail is object|null");
    console.assert(detail === null || detail.spec !== undefined, "readProposedStages: spec present when detail present");
    if (!detail) return { extract: null, transform: null };
    const proposal = detail.spec._l2_proposal;
    if (!proposal || typeof proposal !== "object" || Array.isArray(proposal)) {
      return { extract: null, transform: null };
    }
    const stages = (proposal as Record<string, unknown>).stages;
    if (!stages || typeof stages !== "object" || Array.isArray(stages)) {
      return { extract: null, transform: null };
    }
    const s = stages as Record<string, unknown>;
    const extract = s.extract && typeof s.extract === "object" && !Array.isArray(s.extract)
      ? (s.extract as Record<string, unknown>)
      : null;
    const transform = Array.isArray(s.transform) ? s.transform : null;
    return { extract, transform };
  }

  async function openLibrary(): Promise<void> {
    console.assert(typeof api.niLibrary === "function", "openLibrary: niLibrary present");
    console.assert(libraryOpen === false || libraryOpen === true, "openLibrary: libraryOpen boolean");
    libraryOpen = true;
    libraryError = "";
    if (library !== null) return; // already loaded — reuse the cached state
    libraryLoading = true;
    try {
      library = await api.niLibrary();
      if (library.url) libraryConnectUrl = library.url;
    } catch (err) {
      libraryError = describeError(err);
    } finally {
      libraryLoading = false;
    }
  }
  function closeLibrary(): void {
    console.assert(libraryBusy === false, "closeLibrary: never close while a request is in flight");
    console.assert(installBusy === false, "closeLibrary: never close mid-install");
    libraryOpen = false;
    // Reset the transient sheet-local state so a re-open is a clean surface (search/filter,
    // install draft, trust panel, notices) — but keep `library` cached so we don't refetch.
    librarySearch = "";
    libraryCategory = "";
    installTemplate = null;
    installParams = {};
    installError = "";
    installNotice = "";
    trustPass = "";
    trustError = "";
    libraryError = "";
  }

  async function connectLibrary(): Promise<void> {
    console.assert(typeof libraryConnectUrl === "string", "connectLibrary: url is string");
    console.assert(libraryBusy === false, "connectLibrary: no concurrent request");
    const url = libraryConnectUrl.trim();
    if (!url || libraryBusy) return;
    libraryBusy = true;
    libraryError = "";
    try {
      library = await api.niLibraryConnect(url);
    } catch (err) {
      libraryError = describeError(err);
    } finally {
      libraryBusy = false;
    }
  }
  async function checkLibraryNow(): Promise<void> {
    console.assert(library?.connected === true, "checkLibraryNow: only when connected");
    console.assert(libraryBusy === false, "checkLibraryNow: no concurrent request");
    if (libraryBusy) return;
    libraryBusy = true;
    libraryError = "";
    try {
      library = await api.niLibraryCheck();
    } catch (err) {
      libraryError = describeError(err);
    } finally {
      libraryBusy = false;
    }
  }
  async function disconnectLibrary(): Promise<void> {
    console.assert(library?.connected === true, "disconnectLibrary: only when connected");
    console.assert(libraryBusy === false, "disconnectLibrary: no concurrent request");
    const ok = await confirmDialog({
      title: "Disconnect from this library?",
      body: "Templates you already installed as items stay on your board. You can reconnect any time.",
      confirmLabel: "Disconnect",
      danger: true,
    });
    if (!ok) return;
    libraryBusy = true;
    libraryError = "";
    try {
      await api.niLibraryDisconnect();
      library = { connected: false };
      librarySearch = "";
      libraryCategory = "";
    } catch (err) {
      libraryError = describeError(err);
    } finally {
      libraryBusy = false;
    }
  }
  async function trustLibraryKey(): Promise<void> {
    console.assert(!!library?.blocked, "trustLibraryKey: only while blocked");
    console.assert(typeof trustPass === "string", "trustLibraryKey: passphrase is string");
    const offered = library?.blocked?.offered_fingerprint;
    if (!offered || !trustPass || trustBusy) return;
    trustBusy = true;
    trustError = "";
    try {
      library = await api.niLibraryTrustKey(offered, trustPass);
      trustPass = "";
    } catch (err) {
      trustError = describeError(err);
    } finally {
      trustBusy = false;
    }
  }

  function startInstall(t: NiTemplate): void {
    console.assert(typeof t.id === "string", "startInstall: template id is string");
    console.assert(Array.isArray(t.params), "startInstall: params is array");
    installTemplate = t;
    installParams = {};
    installError = "";
    installNotice = "";
  }
  function cancelInstall(): void {
    console.assert(installBusy === false, "cancelInstall: not while installing");
    console.assert(typeof installError === "string", "cancelInstall: installError is string");
    installTemplate = null;
    installParams = {};
    installError = "";
  }
  async function submitInstall(): Promise<void> {
    console.assert(installTemplate !== null, "submitInstall: template picked");
    console.assert(installBusy === false, "submitInstall: no concurrent install");
    const t = installTemplate;
    if (!t || installBusy) return;
    const err = validateParamForm(t.params, installParams);
    if (err) { installError = err.message; return; }
    installBusy = true;
    installError = "";
    try {
      const body = paramValuesForInstall(t.params, installParams);
      const r = await api.niLibraryInstall(t.id, body);
      const needs = r.needs_credentials?.length ? " (credentials still needed)" : "";
      installNotice = `Added as a draft — open its card to add credentials and Activate.${needs}`;
      installTemplate = null;
      installParams = {};
      await load(); // the new draft joins the board immediately, not on the next 10s tick
    } catch (e) {
      installError = describeError(e);
    } finally {
      installBusy = false;
    }
  }

  function openTemplateUpdate(item: NiBoardItem): void {
    console.assert(item.template_update === true, "openTemplateUpdate: chip must be present");
    console.assert(updatePromptFor === null, "openTemplateUpdate: no other update prompt open");
    updatePromptFor = item;
  }
  async function applyTemplateUpdate(): Promise<void> {
    console.assert(updatePromptFor !== null, "applyTemplateUpdate: target must be set");
    console.assert(applyingUpdate === false, "applyTemplateUpdate: no concurrent apply");
    const target = updatePromptFor;
    if (!target || applyingUpdate) return;
    applyingUpdate = true;
    busyId = target.id;
    try {
      await api.niApplyTemplateUpdate(target.id);
      updatePromptFor = null;
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      applyingUpdate = false;
      busyId = null;
    }
  }

  // Read the spec's repair_policy defensively (spec is Record<string, unknown>).
  // Server default: l1 = true, l2_frontier = false — mirrored here so a spec that
  // pre-dates the field renders with the same defaults the backend would apply.
  function readRepairPolicy(detail: NiItemDetail | null): { l1: boolean; l2_frontier: boolean } {
    console.assert(detail === null || typeof detail === "object", "readRepairPolicy: detail is object|null");
    console.assert(detail === null || detail.spec !== undefined, "readRepairPolicy: spec present when detail present");
    const fallback = { l1: true, l2_frontier: false };
    if (!detail) return fallback;
    const raw = detail.spec.repair_policy;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fallback;
    const rec = raw as Record<string, unknown>;
    const l1 = typeof rec.l1 === "boolean" ? rec.l1 : fallback.l1;
    const l2 = typeof rec.l2_frontier === "boolean" ? rec.l2_frontier : fallback.l2_frontier;
    return { l1, l2_frontier: l2 };
  }

  async function openRepair(item: NiBoardItem): Promise<void> {
    console.assert(typeof item.id === "string", "openRepair: id is string");
    console.assert(repairFor === null, "openRepair: no other repair modal open");
    repairFor = item;
    repairDetail = null;
    repairError = "";
    repairLoading = true;
    try {
      repairDetail = await api.niItem(item.id);
      const p = readRepairPolicy(repairDetail);
      repairL1 = p.l1;
      repairL2Frontier = p.l2_frontier;
    } catch (err) {
      repairError = describeError(err);
    } finally {
      repairLoading = false;
    }
  }
  function closeRepair(): void {
    console.assert(repairBusy === false, "closeRepair: not while save in flight");
    console.assert(typeof repairLoading === "boolean", "closeRepair: repairLoading is boolean");
    repairFor = null;
    repairDetail = null;
    repairError = "";
  }
  async function saveRepair(): Promise<void> {
    console.assert(repairFor !== null, "saveRepair: a target must be set");
    console.assert(repairBusy === false, "saveRepair: no concurrent save");
    const target = repairFor;
    if (!target || repairBusy) return;
    repairBusy = true;
    repairError = "";
    try {
      await api.niRepairPolicy(target.id, { l1: repairL1, l2_frontier: repairL2Frontier });
      repairFor = null;
      repairDetail = null;
    } catch (err) {
      // 403 arrives when a paired phone tried the Desktop-local route; describeError
      // passes the backend's human sentence through verbatim.
      repairError = describeError(err);
    } finally {
      repairBusy = false;
    }
  }

  async function openProposal(item: NiBoardItem): Promise<void> {
    console.assert(item.l2_proposal === true, "openProposal: chip must be present");
    console.assert(proposalFor === null, "openProposal: no other proposal open");
    proposalFor = item;
    proposalDetail = null;
    proposalLoading = true;
    try {
      proposalDetail = await api.niItem(item.id);
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
      proposalFor = null;
    } finally {
      proposalLoading = false;
    }
  }
  function closeProposal(): void {
    console.assert(proposalBusy === false, "closeProposal: not while apply/dismiss in flight");
    console.assert(typeof proposalLoading === "boolean", "closeProposal: proposalLoading is boolean");
    proposalFor = null;
    proposalDetail = null;
  }
  async function applyProposal(): Promise<void> {
    console.assert(proposalFor !== null, "applyProposal: a target must be set");
    console.assert(proposalBusy === false, "applyProposal: no concurrent apply");
    const target = proposalFor;
    if (!target || proposalBusy) return;
    proposalBusy = true;
    busyId = target.id;
    try {
      await api.niL2Apply(target.id);
      proposalFor = null;
      proposalDetail = null;
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      proposalBusy = false;
      busyId = null;
    }
  }
  async function dismissProposal(): Promise<void> {
    console.assert(proposalFor !== null, "dismissProposal: a target must be set");
    console.assert(proposalBusy === false, "dismissProposal: no concurrent dismiss");
    const target = proposalFor;
    if (!target || proposalBusy) return;
    const ok = await confirmDialog({
      title: "Dismiss proposed fix?",
      body: "Dismiss this proposed fix? It won't be offered again for this breakage.",
      confirmLabel: "Dismiss",
      danger: true,
    });
    if (!ok) return;
    proposalBusy = true;
    busyId = target.id;
    try {
      await api.niL2Dismiss(target.id);
      proposalFor = null;
      proposalDetail = null;
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      proposalBusy = false;
      busyId = null;
    }
  }

  // Parse a hostname out of the item spec's source.url — best-effort, empty on
  // anything the URL constructor refuses (unset, non-http source, malformed). The
  // Add-key modal prefills this into an editable host field (the credential is
  // host-bound; §3 secret_host_mismatch is the failure mode).
  function hostFromSpec(detail: NiItemDetail | null): string {
    console.assert(detail === null || typeof detail === "object", "hostFromSpec: detail is object|null");
    console.assert(detail === null || typeof detail.spec === "object", "hostFromSpec: spec present when detail present");
    if (!detail) return "";
    const source = detail.spec.source;
    if (!source || typeof source !== "object" || Array.isArray(source)) return "";
    const url = (source as Record<string, unknown>).url;
    if (typeof url !== "string" || url.length === 0) return "";
    try {
      return new URL(url).hostname;
    } catch {
      return "";
    }
  }

  // Card status truth (§ "Status truth" amendments). Returns the sentence that
  // replaces the old "Waiting for the first run…" lie, plus a boolean the template
  // uses to gate rendering. A card that has run at least once (last_checked set) AND
  // has no payload AND has a positive failure streak is showing a broken first-run
  // truth: "First run failed — <friendly> (<raw>), N attempts · last tried Xm ago".
  // Everything else keeps the honest "Waiting…" copy.
  type FailureLine = { show: boolean; friendly: string; raw: string; attempts: number; tried: string };
  function firstRunFailure(item: NiBoardItem): FailureLine {
    console.assert(typeof item.state === "string", "firstRunFailure: state is string");
    console.assert(typeof item.consecutive_failures === "number", "firstRunFailure: failures is number");
    const eligibleState =
      item.state === "commissioning" ||
      item.state === "live" ||
      item.state === "degraded" ||
      item.state === "failing";
    if (!eligibleState) return { show: false, friendly: "", raw: "", attempts: 0, tried: "" };
    if (item.payload !== null) return { show: false, friendly: "", raw: "", attempts: 0, tried: "" };
    if (item.consecutive_failures <= 0) return { show: false, friendly: "", raw: "", attempts: 0, tried: "" };
    const raw = typeof item.last_status === "string" ? item.last_status : "";
    return {
      show: true,
      friendly: friendlyErrorClass(raw),
      raw,
      attempts: item.consecutive_failures,
      tried: relTime(item.last_checked),
    };
  }

  // Card footer fresh line. When the item has a payload_at, keep the existing "Xm
  // ago" — that's the moment the data on screen was fetched. Otherwise, if the
  // engine tried at all (last_checked set), the amendments require "last tried
  // <relTime>" so the footer stops rendering the "—" lie of "we haven't started yet".
  function footerFresh(item: NiBoardItem): string {
    console.assert(typeof item === "object" && item !== null, "footerFresh: item is object");
    console.assert(item.last_checked === null || typeof item.last_checked === "string", "footerFresh: last_checked shape");
    if (item.payload_at) return relTime(item.payload_at);
    if (item.last_checked) return `last tried ${relTime(item.last_checked)}`;
    return "—";
  }

  async function openCredential(item: NiBoardItem, entry: { name: string; label: string }): Promise<void> {
    console.assert(typeof entry.name === "string", "openCredential: name is string");
    console.assert(credentialFor === null, "openCredential: no other credential modal open");
    credentialFor = item;
    credentialName = entry.name;
    credentialLabel = entry.label;
    credentialValue = "";
    credentialHost = "";
    credentialError = "";
    credentialLoading = true;
    try {
      const detail = await api.niItem(item.id);
      credentialHost = hostFromSpec(detail);
    } catch (err) {
      // Prefill failure isn't fatal — the host field is editable — but surface it
      // so the user knows they'll need to type the host themselves.
      credentialError = describeError(err);
    } finally {
      credentialLoading = false;
    }
  }
  function closeCredential(): void {
    console.assert(credentialBusy === false, "closeCredential: not while save in flight");
    console.assert(typeof credentialValue === "string", "closeCredential: value is string");
    credentialFor = null;
    credentialName = "";
    credentialLabel = "";
    credentialValue = ""; // never persist the secret past the modal's lifetime
    credentialHost = "";
    credentialError = "";
  }
  async function submitCredential(): Promise<void> {
    console.assert(credentialFor !== null, "submitCredential: a target must be set");
    console.assert(credentialBusy === false, "submitCredential: no concurrent save");
    const target = credentialFor;
    const value = credentialValue;
    const host = credentialHost.trim();
    if (!target || !value || !host || credentialBusy) return;
    credentialBusy = true;
    credentialError = "";
    try {
      await api.niPutCredential(target.id, credentialName, value, host);
      const label = credentialLabel;
      closeCredential();
      toast(`Saved the ${label} key.`);
      await load();
    } catch (err) {
      // 403 arrives when a paired phone tried the Desktop-local route; describeError
      // passes the backend's human sentence through verbatim ("add keys on your desktop").
      credentialError = describeError(err);
    } finally {
      credentialBusy = false;
    }
  }

  function openParam(item: NiBoardItem, entry: { name: string; label: string }): void {
    console.assert(typeof entry.name === "string", "openParam: name is string");
    console.assert(paramFor === null, "openParam: no other param modal open");
    paramFor = item;
    paramName = entry.name;
    paramLabel = entry.label;
    paramValue = "";
    paramError = "";
  }
  function closeParam(): void {
    console.assert(paramBusy === false, "closeParam: not while save in flight");
    paramFor = null;
    paramName = "";
    paramLabel = "";
    paramValue = "";
    paramError = "";
  }
  async function submitParam(): Promise<void> {
    console.assert(paramFor !== null, "submitParam: a target must be set");
    console.assert(paramBusy === false, "submitParam: no concurrent save");
    const target = paramFor;
    const value = paramValue.trim();
    if (!target || !value || paramBusy) return;
    paramBusy = true;
    paramError = "";
    try {
      await api.niPutParam(target.id, paramName, value);
      const label = paramLabel;
      closeParam();
      toast(`Saved ${label}.`);
      await load();
    } catch (err) {
      paramError = describeError(err);
    } finally {
      paramBusy = false;
    }
  }

  async function openHistory(item: NiBoardItem): Promise<void> {
    console.assert(typeof item.id === "string", "openHistory: id is string");
    console.assert(historyFor === null, "openHistory: no other history modal open");
    historyFor = item;
    historyDetail = null;
    historyError = "";
    historyLoading = true;
    try {
      historyDetail = await api.niItem(item.id);
    } catch (err) {
      historyError = describeError(err);
    } finally {
      historyLoading = false;
    }
  }
  function closeHistory(): void {
    console.assert(typeof historyLoading === "boolean", "closeHistory: loading is boolean");
    console.assert(historyFor === null || historyFor !== undefined, "closeHistory: target shape");
    historyFor = null;
    historyDetail = null;
    historyError = "";
  }

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

  // Bounded fast-poll: while ANY item is walking a creation/remap flow (seconds-to-
  // a-minute in the backend), poll every 3s so the card's stage label tracks the
  // engine's progress. The effect's cleanup tears the timer down the moment the last
  // flow settles — no perpetual 3s poll on an idle board. Visible-tab + unlocked
  // guards mirror the primary 10s timer's guard (load() itself bails if locked).
  $effect(() => {
    console.assert(Array.isArray(items), "$effect fast-poll: items array");
    console.assert(typeof isFlowActive === "function", "$effect fast-poll: helper present");
    const anyActive = items.some((it) => isFlowActive(it.flow ?? null));
    if (!anyActive) return;
    const fast = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 3_000);
    return () => clearInterval(fast);
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

  async function approveFlowSource(item: NiBoardItem) {
    console.assert(item.flow?.state === "confirm_source", "approveFlowSource: pause required");
    busyId = item.id;
    try {
      const res = await api.niFlowConfirmSource(item.id);
      toast(res.state === "ready"
        ? "Source approved — the card is being commissioned."
        : "Source approved.");
      await load();
    } catch (err) {
      const msg = describeError(err);
      if (msg) error = msg;
    } finally {
      busyId = null;
    }
  }
  async function declineFlowSource(item: NiBoardItem) {
    console.assert(item.flow?.state === "confirm_source", "declineFlowSource: pause required");
    busyId = item.id;
    try {
      await api.niFlowDeclineSource(item.id);
      toast("Source declined — retry in chat with a different source, or delete the card.");
      await load();
    } catch (err) {
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
      const res = await api.niValidate(item.id, true);
      // F1: the verdict kicks the C3 proof run server-side — say what happened.
      toast(res.state === "live"
        ? "Confirmed — the card is live."
        : "Confirmed — verifying with a fresh refresh.");
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
  <div class="ni-page-head">
    <h1>Neural Interface</h1>
    <!-- The Library ghost button opens the Global Library sheet (§20). Ghost so it
         reads as "browse" not "primary action" — the primary act is still "ask in
         chat"; installing a curated template is the shortcut. -->
    <button class="ghost" onclick={openLibrary}>Library</button>
  </div>
  <p class="muted">
    Live data you asked for, on one board. Type it below — “show me AAPL every 5 minutes”.
  </p>

  <!-- NI Foreman P1 (2026-09-16): the composer IS the creation surface — the
       request goes straight to the deterministic flow engine; the card appears
       immediately and every later step is a tap on the card itself. No chat
       model anywhere in the path. -->
  <form
    class="ni-composer"
    onsubmit={(e) => { e.preventDefault(); void submitComposer(); }}
  >
    <input
      type="text"
      bind:value={composerText}
      placeholder="What do you want to watch? — e.g. NVDA stock price every 28 minutes"
      maxlength="2000"
      disabled={composerBusy}
      aria-label="Describe the card you want"
    />
    <button type="submit" disabled={composerBusy || composerText.trim().length < 3}>
      {composerBusy ? "Starting…" : "Create card"}
    </button>
  </form>
  {#if composerError}<p class="error" style="margin:var(--s-2) 0 0">{composerError}</p>{/if}

  {#if !loaded}
    <Spinner block />
  {:else if items.length === 0}
    <EmptyState
      icon="monitor"
      title="Nothing on your Neural Interface yet"
      body="Type what you want to watch in the box above — “show me AAPL every 5 minutes” — then approve the source on the card."
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
            <span class="ni-chips">
              <Chip kind={health.kind}>{health.label}</Chip>
              {#if item.interpreted}
                <Chip
                  kind=""
                  title="This card includes a language model's reading of the data, not pure arithmetic."
                >Interpreted</Chip>
              {/if}
              {#if item.template_update}
                <!-- Fleet healing (§20): the pack fixed/changed this card's template. Applying is
                     per-item and user-driven — click opens the confirm modal (draft + re-Activate). -->
                <Chip
                  kind="accent"
                  onclick={() => openTemplateUpdate(item)}
                  title="The library updated this card's template — click to review and apply"
                >Update available</Chip>
              {/if}
              {#if item.l2_proposal}
                <!-- L2 frontier repair (§23): a proposed fix is parked. NEVER auto-applied —
                     the chip opens the review modal (diff + Apply as trial / Dismiss). -->
                <Chip
                  kind="accent"
                  onclick={() => openProposal(item)}
                  title="A proposed fix from Claude — click to review"
                >Fix proposed</Chip>
              {/if}
              {#if item.template_gone}
                <!-- Informational only: the library retired this template. The card keeps working
                     until the user chooses to delete it — no auto-anything. -->
                <Chip kind="" title="This card's template is no longer in the library">No longer in library</Chip>
              {/if}
            </span>
          </div>

          {#if preview}
            <div class="ni-preview-tag">
              <Chip kind="">Preview — sample data</Chip>
            </div>
          {/if}

          <div class="ni-body">
            {#if item.flow}
              <!-- Creation/remap flow in progress or ended abnormally: the progress
                   line replaces the payload/first-run copy so the card tells one
                   truth at a time. failed/unsupported render the honest error via
                   friendlyErrorClass; every other state has a calm stage label. -->
              {#if item.flow.state === "failed" || item.flow.state === "unsupported"}
                {@const friendly = friendlyErrorClass(item.flow.error ?? "")}
                <p class="ni-status-fail" style="margin:0; font-size:var(--f-label)">
                  {item.flow.state === "unsupported" ? "Can’t build this card yet" : "Setup failed"}
                  {#if friendly}
                    — <span class="ni-status-class">{friendly}</span>
                  {/if}
                </p>
                {#if item.shell}
                  <div class="ni-actions" style="margin-top: var(--s-2)">
                    <button
                      class="secondary"
                      disabled={busyId === item.id}
                      onclick={() => retryFlow(item)}
                    >{busyId === item.id ? "Retrying…" : "Retry"}</button>
                  </div>
                {/if}
              {:else if item.flow.state === "confirm_source"}
                <!-- Card-consent (2026-09-15): the flow's own approval affordance,
                     rendered by CODE the instant the pause happens — the exact URL
                     unmissable, the optional place lookup and any not-covered
                     fields disclosed on the same surface the tap approves. -->
                <div class="ni-commission">
                  <p style="margin:0 0 var(--s-2); font-size:var(--f-label)">
                    {item.flow.recipe_title ? `Vetted source: ${item.flow.recipe_title}` : "Source found"}
                  </p>
                  <p style="margin:0 0 var(--s-2); font-size:var(--f-label); word-break:break-all">
                    Fetches: <strong>{item.flow.filled_url ?? item.flow.source_url}</strong>
                  </p>
                  {#if item.flow.fills}
                    <p class="muted" style="margin:0 0 var(--s-2); font-size:var(--f-label)">
                      {#each Object.entries(item.flow.fills) as [name, value] (name)}
                        <span style="margin-right: var(--s-3)">{name}: <strong>{value}</strong></span>
                      {/each}
                    </p>
                  {/if}
                  {#if item.flow.geocode_query}
                    <p class="muted" style="margin:0 0 var(--s-2); font-size:var(--f-label)">
                      Also looks up “{item.flow.geocode_query}” via {item.flow.geocode_host} to fill the location.
                    </p>
                  {/if}
                  {#if item.flow.not_covered && item.flow.not_covered.length > 0}
                    <p class="muted" style="margin:0 0 var(--s-2); font-size:var(--f-label)">
                      Won’t include: {item.flow.not_covered.join(", ")} (this source doesn’t provide it).
                    </p>
                  {/if}
                  <div class="ni-actions">
                    <button
                      class="secondary"
                      disabled={busyId === item.id}
                      onclick={() => approveFlowSource(item)}
                    >{busyId === item.id ? "Building…" : "Approve source"}</button>
                    <button
                      class="ghost"
                      disabled={busyId === item.id}
                      onclick={() => declineFlowSource(item)}
                    >Not this source</button>
                  </div>
                </div>
              {:else}
                <p class="muted" style="margin:0; font-size:var(--f-label)">{flowStageLabel(item.flow)}</p>
              {/if}
            {:else if item.payload}
              <NiScene node={item.payload} />
            {:else}
              {@const failure = firstRunFailure(item)}
              {#if failure.show}
                <!-- Status truth (§ amendments): a real failure class + attempt count +
                     when it was tried — replaces the "Waiting for the first run…" lie. -->
                <p class="ni-status-fail" style="margin:0; font-size:var(--f-label)">
                  First run failed —
                  <span class="ni-status-class">{failure.friendly} ({failure.raw})</span>,
                  {failure.attempts} attempt{failure.attempts === 1 ? "" : "s"} · last tried {failure.tried}
                </p>
              {:else}
                <p class="muted" style="margin:0; font-size:var(--f-label)">Waiting for the first run…</p>
              {/if}
            {/if}
          </div>

          {#if item.needs_credentials && item.needs_credentials.length > 0}
            <!-- Add-key entry point (§ Status-truth amendments). One row per missing
                 credential — the modal itself is host-bound + Desktop-local, so a
                 paired phone gets a 403 the modal surfaces verbatim. -->
            <div class="ni-needs-key">
              {#each item.needs_credentials as need (need.name)}
                <div class="ni-needs-row">
                  <span class="ni-needs-copy">Needs your <strong>{need.label}</strong>{need.label.toLowerCase().endsWith("key") ? "" : " key"}</span>
                  <button
                    class="secondary"
                    disabled={busyId === item.id || credentialFor !== null}
                    onclick={() => openCredential(item, need)}
                  >Add key</button>
                </div>
              {/each}
            </div>
          {/if}

          {#if item.needs_params && item.needs_params.length > 0}
            <!-- Fill-param entry point (needs_params, 2026-09-14). The non-secret
                 sibling of Add key: the engine refuses runs and Activate 409s while
                 any referenced slot is empty, so the card says exactly what it
                 needs instead of "Waiting for the first run…". -->
            <div class="ni-needs-key">
              {#each item.needs_params as need (need.name)}
                <div class="ni-needs-row">
                  <span class="ni-needs-copy">Needs <strong>{need.label}</strong></span>
                  <button
                    class="secondary"
                    disabled={busyId === item.id || paramFor !== null}
                    onclick={() => openParam(item, need)}
                  >Fill</button>
                </div>
              {/each}
            </div>
          {/if}

          {#if preview && item.shell && (!item.flow || item.flow.state === "failed" || item.flow.state === "unsupported")}
            <!-- W2 (2026-09-15) + W-A (2026-09-17): a flow shell whose creation
                 never finished must not offer Activate — but this copy showed
                 even while the flow was PAUSED awaiting the user's Approve tap
                 (field: it rendered under the consent block and read as a
                 contradiction). Terminal-or-absent flows only; Retry lives on
                 the failure block above. -->
            <p class="muted" style="margin:0; font-size:var(--f-label)">
              Creation didn’t finish — retry above, or delete this card.
            </p>
          {:else if preview && !item.shell}
            <div class="ni-actions">
              <button
                disabled={busyId === item.id}
                onclick={() => activate(item)}
                title="Activate — start fetching for real"
              >{busyId === item.id ? "Activating…" : "Activate"}</button>
            </div>
          {/if}

          {#if item.state === "commissioning" && item.payload && item.payload_slot !== "preview"}
            <!-- F2 (2026-09-15): after "Looks right" the verdict is recorded but the
                 card may briefly stay commissioning while the C3 run verifies — the
                 banner must acknowledge instead of re-asking (the field run logged a
                 user tapping a "dead" button three times). -->
            {#if item.c2_ok}
              <div class="ni-commission">
                <p style="margin:0; font-size:var(--f-label)">Confirmed — verifying, this card goes live after the next successful refresh.</p>
              </div>
            {:else}
            <div class="ni-commission">
              <p style="margin:0 0 var(--s-2); font-size:var(--f-label)">This is live data — is it right?</p>
              <div class="ni-actions">
                <button
                  class="secondary"
                  disabled={busyId === item.id}
                  onclick={() => validateLooksRight(item)}
                >{busyId === item.id ? "Checking…" : "Looks right"}</button>
                <button
                  class="ghost"
                  disabled={busyId === item.id}
                  onclick={() => openWrongNote(item)}
                >Something’s wrong</button>
              </div>
            </div>
            {/if}
          {/if}

          <div class="ni-foot">
            <!-- W-G (2026-09-17): one calm meta line — cadence + freshness —
                 and a condensed action row (the five-button spread wrapped to
                 two noisy lines in the field). Rarely-used verbs live in a
                 native details overflow: zero new state, keyboard accessible. -->
            <span class="muted ni-fresh">
              every {item.interval_minutes}m
              {#if !item.enabled}· paused{/if}
              · {footerFresh(item)}
            </span>
            <span class="ni-actions">
              <button
                class="linklike ni-history"
                disabled={busyId === item.id || historyFor !== null}
                onclick={() => openHistory(item)}
                title="Show recent runs"
              >History</button>
              <button
                class="ghost"
                disabled={busyId === item.id}
                onclick={() => runNow(item)}
                title="Run now"
              >{busyId === item.id ? "Running…" : "Run now"}</button>
              <details class="ni-more">
                <summary title="More actions" aria-label="More actions">⋯</summary>
                <div class="ni-more-menu">
                  <button
                    class="ghost"
                    disabled={busyId === item.id}
                    onclick={() => togglePause(item)}
                  >{item.enabled ? "Pause" : "Resume"}</button>
                  <button
                    class="ghost"
                    disabled={busyId === item.id}
                    onclick={() => openRepair(item)}
                  >Repair settings</button>
                  <button
                    class="ghost ni-danger"
                    disabled={busyId === item.id}
                    onclick={() => remove(item)}
                  >Delete</button>
                </div>
              </details>
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

  <!-- Fleet-healing confirm (§20). Applying resets the item to a draft: the existing
       draft affordances then walk the user through Activate again — a template update
       NEVER inherits standing consent. -->
  {#if updatePromptFor}
    <Modal
      open
      alert
      label="Apply library update?"
      onclose={() => { if (!applyingUpdate) updatePromptFor = null; }}
    >
      <h2 class="modal-title">Apply library update?</h2>
      <p class="modal-body">
        The library fixed or changed this card’s template. Applying resets
        <strong>“{updatePromptFor.title}”</strong> to a draft for your review and re-activation.
        Your parameter values and credentials carry over.
      </p>
      <div class="modal-actions">
        <button class="secondary" disabled={applyingUpdate} onclick={() => { updatePromptFor = null; }}>Cancel</button>
        <button disabled={applyingUpdate} onclick={applyTemplateUpdate}>
          {applyingUpdate ? "Applying…" : "Apply update"}
        </button>
      </div>
    </Modal>
  {/if}

  <!-- L2 proposal review (§23). PARK-ONLY: rendering the diff never applies anything.
       Apply runs the proposed stages as a TRIAL — the next refresh must pass this
       card's captured contract, else the engine auto-reverts. -->
  {#if proposalFor}
    <Modal
      open
      size="md"
      label="A proposed fix from Claude"
      onclose={() => { if (!proposalBusy) closeProposal(); }}
    >
      <h2 class="modal-title">A proposed fix from Claude</h2>
      <p class="modal-body">
        Your card kept failing, so (with your per-card permission) Claude proposed new
        data mappings. Nothing has been applied. Applying runs it as a trial — kept only
        if the next refresh passes this card’s validated contract, automatically reverted
        otherwise.
      </p>
      {#if proposalLoading}
        <Spinner block />
      {:else if !proposalDetail}
        <p class="error">Couldn’t load this proposal.</p>
      {:else}
        <div class="prop-diff">
          <div class="prop-col">
            <p class="prop-col-label">Current mapping</p>
            <p class="prop-sub-label">extract</p>
            <pre class="prop-json">{formatStageJson(proposalCurrent.extract)}</pre>
            <p class="prop-sub-label">transform</p>
            <pre class="prop-json">{formatStageJson(proposalCurrent.transform)}</pre>
          </div>
          <div class="prop-col">
            <p class="prop-col-label">Proposed mapping</p>
            <p class="prop-sub-label">extract</p>
            <pre class="prop-json">{formatStageJson(proposalProposedStages.extract)}</pre>
            <p class="prop-sub-label">transform</p>
            <pre class="prop-json">{formatStageJson(proposalProposedStages.transform)}</pre>
          </div>
        </div>
      {/if}
      <div class="modal-actions" style="margin-top: var(--s-4)">
        <button class="ghost" disabled={proposalBusy} onclick={closeProposal}>Cancel</button>
        <button
          class="secondary"
          disabled={proposalBusy || proposalLoading || !proposalDetail}
          onclick={dismissProposal}
        >Dismiss</button>
        <button
          disabled={proposalBusy || proposalLoading || !proposalDetail}
          onclick={applyProposal}
        >{proposalBusy ? "Applying…" : "Apply"}</button>
      </div>
    </Modal>
  {/if}

  <!-- Per-card repair settings (§23). The board row deliberately doesn't carry
       repair_policy — this lever is a detail-view concern, so opening the modal
       lazily fetches the item detail. Saving is Desktop-local (x-sb-local); a
       paired phone gets a 403 the modal surfaces verbatim. -->
  {#if repairFor}
    <Modal
      open
      label="Repair settings"
      onclose={() => { if (!repairBusy) closeRepair(); }}
    >
      <h2 class="modal-title">Repair settings — “{repairFor.title}”</h2>
      {#if repairLoading}
        <Spinner block />
      {:else if !repairDetail}
        <p class="error">{repairError || "Couldn’t load this card."}</p>
      {:else}
        <p class="modal-body">
          When a card keeps failing, SmartBrain can try to fix it. Local repair only
          touches your own machine. Frontier repair sends this card’s goal, data
          mappings, and failure details to Anthropic under your Claude sign-in — only
          after local repair fails, and fixes are always proposed for your review,
          never applied.
        </p>
        <label class="repair-row">
          <input type="checkbox" bind:checked={repairL1} disabled={repairBusy} />
          <span><strong>Local repair</strong> — retry small mapping fixes on this machine.</span>
        </label>
        <label class="repair-row">
          <input type="checkbox" bind:checked={repairL2Frontier} disabled={repairBusy} />
          <span><strong>Frontier repair via Claude</strong> — ask Anthropic for a proposed fix when local repair fails.</span>
        </label>
        {#if repairError}<p class="error" style="margin:var(--s-3) 0 0">{repairError}</p>{/if}
      {/if}
      <div class="modal-actions" style="margin-top: var(--s-4)">
        <button class="secondary" disabled={repairBusy} onclick={closeRepair}>Cancel</button>
        <button disabled={repairBusy || repairLoading || !repairDetail} onclick={saveRepair}>
          {repairBusy ? "Saving…" : "Save"}
        </button>
      </div>
    </Modal>
  {/if}

  <!-- Add-key modal (§ Status-truth amendments). Desktop-local via x-sb-local — the
       WebRTC bridge strips that header, so a paired phone gets a 403 the modal paints
       verbatim. The value input is password-typed and NEVER echoed back into any DOM
       node; closeCredential() zeroes it. -->
  {#if credentialFor}
    <Modal
      open
      label="Add key"
      onclose={() => { if (!credentialBusy) closeCredential(); }}
    >
      <h2 class="modal-title">Add key — {credentialLabel}</h2>
      <p class="modal-body">
        Keys are stored on this computer only and used exactly for this card’s source
        (host-bound). If you type them elsewhere, they won’t work.
      </p>
      {#if credentialLoading}
        <Spinner block />
      {:else}
        <label class="cred-row" for="ni-cred-value">
          <span>{credentialLabel}</span>
          <input
            id="ni-cred-value"
            type="password"
            autocomplete="off"
            bind:value={credentialValue}
            placeholder="Paste your key"
            disabled={credentialBusy}
          />
        </label>
        <label class="cred-row" for="ni-cred-host">
          <span>Host</span>
          <input
            id="ni-cred-host"
            type="text"
            bind:value={credentialHost}
            placeholder="api.example.com"
            disabled={credentialBusy}
          />
        </label>
        {#if credentialError}<p class="error" style="margin:var(--s-3) 0 0">{credentialError}</p>{/if}
      {/if}
      <div class="modal-actions" style="margin-top: var(--s-4)">
        <button class="secondary" disabled={credentialBusy} onclick={closeCredential}>Cancel</button>
        <button
          disabled={credentialBusy || credentialLoading || !credentialValue || !credentialHost.trim()}
          onclick={submitCredential}
        >{credentialBusy ? "Saving…" : "Save"}</button>
      </div>
    </Modal>
  {/if}

  <!-- Fill-param modal (needs_params, 2026-09-14). Desktop-local like Add key, but
       a plain text input — these are ordinary spec values (a ticker, a city), never
       secrets (the server refuses secret params on this route). -->
  {#if paramFor}
    <Modal
      open
      label="Fill in"
      onclose={() => { if (!paramBusy) closeParam(); }}
    >
      <h2 class="modal-title">Fill in — {paramLabel}</h2>
      <p class="modal-body">
        This card needs a value before it can run. It becomes part of the card’s
        source and shows up in its address.
      </p>
      <label class="cred-row" for="ni-param-value">
        <span>{paramLabel}</span>
        <input
          id="ni-param-value"
          type="text"
          autocomplete="off"
          bind:value={paramValue}
          placeholder="e.g. AAPL"
          disabled={paramBusy}
        />
      </label>
      {#if paramError}<p class="error" style="margin:var(--s-3) 0 0">{paramError}</p>{/if}
      <div class="modal-actions" style="margin-top: var(--s-4)">
        <button class="secondary" disabled={paramBusy} onclick={closeParam}>Cancel</button>
        <button
          disabled={paramBusy || !paramValue.trim()}
          onclick={submitParam}
        >{paramBusy ? "Saving…" : "Save"}</button>
      </div>
    </Modal>
  {/if}

  <!-- Run history modal (§10 GET /items/{id}). Read-only list of ni_runs rows in
       reverse-chronological — ts (relTime) + runStatusLabel + friendly error class.
       No verbs; the card footer already exposes Run now / Pause / Delete. -->
  {#if historyFor}
    <Modal
      open
      label="Run history"
      onclose={closeHistory}
    >
      <h2 class="modal-title">Recent runs — “{historyFor.title}”</h2>
      {#if historyLoading}
        <Spinner block />
      {:else if historyError && !historyDetail}
        <p class="error">{historyError}</p>
      {:else if !historyDetail || historyDetail.runs.length === 0}
        <p class="muted">No runs recorded yet.</p>
      {:else}
        <ul class="hist-list">
          {#each historyDetail.runs as run, i (i)}
            {@const friendly = friendlyErrorClass(run.error)}
            <li class="hist-row">
              <span class="hist-when muted">{relTime(run.ts)}</span>
              <span class="hist-status">{runStatusLabel(run.status)}</span>
              {#if friendly}<span class="hist-error muted">— {friendly}</span>{/if}
            </li>
          {/each}
        </ul>
      {/if}
      <div class="modal-actions" style="margin-top: var(--s-4)">
        <button class="secondary" onclick={closeHistory}>Close</button>
      </div>
    </Modal>
  {/if}

  {#if libraryOpen}
    <Modal
      open
      size="lg"
      label="Global Library"
      onclose={() => { if (!libraryBusy && !installBusy) closeLibrary(); }}
    >
      <h2 class="modal-title">Global Library</h2>

      {#if libraryLoading}
        <Spinner block />
      {:else if !library}
        <p class="error">{libraryError || "Couldn't load the library."}</p>
      {:else if library.blocked}
        <!-- KeyChanged (§19): pinned vs offered fingerprints, side by side, labeled — a human
             decides between identities, never from one alone. Copy mirrors knowledge/+page.svelte
             (the vault trust-publisher panel is the precedent) with the plain warning up front. -->
        <div class="warn">
          <p style="margin:0"><strong>The library publisher’s key changed — updates are blocked.</strong></p>
          <div class="lib-fp-compare">
            <div class="lib-fp-row">
              <span class="lib-fp-label">Pinned (trusted)</span>
              <span class="lib-fp">{library.fingerprint ?? "(unknown)"}</span>
            </div>
            <div class="lib-fp-row">
              <span class="lib-fp-label">Offered (new)</span>
              <span class="lib-fp">{library.blocked.offered_fingerprint}</span>
            </div>
          </div>
          <p style="margin:var(--s-2) 0 0">
            This is either the publisher rotating their key — or someone impersonating them. Verify
            the offered fingerprint with the publisher out-of-band before trusting it.
          </p>
          <label for="ni-lib-trust-pass" style="display:block; margin:var(--s-3) 0 var(--s-1)">
            Enter your <strong>SmartBrain passphrase</strong> to pin the new key:
          </label>
          <div class="lib-trust-row">
            <input
              id="ni-lib-trust-pass"
              type="password"
              bind:value={trustPass}
              placeholder="Your passphrase"
              autocomplete="current-password"
              onkeydown={(e) => e.key === "Enter" && trustPass && trustLibraryKey()}
            />
            <button disabled={trustBusy || !trustPass} onclick={trustLibraryKey}>
              {trustBusy ? "Pinning…" : "Trust new key"}
            </button>
          </div>
          {#if trustError}<p class="error" style="margin:var(--s-2) 0 0">{trustError}</p>{/if}
        </div>
      {:else if !library.connected}
        <!-- Disconnected: the operator pastes the URL (or accepts the prefilled default) and
             connects. The pin happens server-side on this first fetch — the returned fingerprint
             is shown prominently after (identity IS the fingerprint; the label is decoration). -->
        <p class="modal-body">
          Templates the SmartBrain project curates — one connect, then install any of them as a
          draft card. The publisher key is pinned on first fetch (TOFU).
        </p>
        <label for="ni-lib-url" class="lib-field-label">Library URL</label>
        <div class="lib-connect-row">
          <input
            id="ni-lib-url"
            type="url"
            bind:value={libraryConnectUrl}
            placeholder={DEFAULT_LIBRARY_URL}
            onkeydown={(e) => e.key === "Enter" && !libraryBusy && connectLibrary()}
          />
          <button disabled={libraryBusy || !libraryConnectUrl.trim()} onclick={connectLibrary}>
            {libraryBusy ? "Connecting…" : "Connect"}
          </button>
        </div>
        {#if libraryError}<p class="error" style="margin:var(--s-2) 0 0">{libraryError}</p>{/if}
      {:else if installTemplate}
        <!-- Install sub-view: params form + the sources repeated (§20: every host+path unmissable
             at install too — the sheet doesn't rely on the user remembering what they saw). -->
        {@const t = installTemplate}
        <div class="lib-install-head">
          <button class="linklike" onclick={cancelInstall}>← Back to templates</button>
          <strong>{t.title}</strong>
        </div>
        <p class="modal-body" style="margin-top:var(--s-2)">{t.goal}</p>

        <p class="lib-section-label">This card will fetch:</p>
        <ul class="lib-sources">
          {#each t.sources as s, i (i)}
            <li><span class="lib-source">{s.host ? `${s.host}${s.path}` : s.type === "internal.kb" ? "your knowledge base (no network)" : s.type === "model" ? "generated by your local model (no fetch)" : s.type}</span></li>
          {/each}
        </ul>

        {#if t.params.length > 0}
          <p class="lib-section-label">Fill in:</p>
          <div class="lib-params">
            {#each t.params as p (p.name)}
              {#if p.kind === "secret"}
                <div class="lib-param-secret">
                  <strong>{p.label}</strong>
                  <span class="muted"> — credential — you’ll add it on the card after install.</span>
                </div>
              {:else}
                <label class="lib-param">
                  <span>{p.label}</span>
                  <input
                    type={p.kind === "number" ? "number" : "text"}
                    bind:value={installParams[p.name]}
                    placeholder={p.label}
                  />
                </label>
              {/if}
            {/each}
          </div>
        {/if}

        <div class="lib-preview">
          <p class="lib-section-label">Preview</p>
          <NiScene node={t.preview_payload} />
        </div>

        {#if installError}<p class="error" style="margin:var(--s-3) 0 0">{installError}</p>{/if}
        <div class="modal-actions" style="margin-top: var(--s-4)">
          <button class="secondary" disabled={installBusy} onclick={cancelInstall}>Cancel</button>
          <button disabled={installBusy || !!paramFormError} onclick={submitInstall}>
            {installBusy ? "Installing…" : "Install as draft"}
          </button>
        </div>
      {:else}
        <!-- Connected: identity header + Check-now/Disconnect, search + category filter, list. -->
        <div class="lib-header">
          <span class="lib-fp-chip" title={library.url ?? ""}>
            {formatFingerprint(library.fingerprint)}
          </span>
          {#if library.seq !== undefined}
            <Chip kind="">v{library.seq}</Chip>
          {/if}
          {#if library.last_checked}
            <span class="muted lib-checked">Last checked {relTime(library.last_checked)}</span>
          {/if}
          <span class="spacer"></span>
          <button class="ghost" disabled={libraryBusy} onclick={checkLibraryNow}>
            {libraryBusy ? "Checking…" : "Check now"}
          </button>
          <button class="ghost" disabled={libraryBusy} onclick={disconnectLibrary}>Disconnect</button>
        </div>

        {#if installNotice}<p class="lib-notice">{installNotice}</p>{/if}
        {#if libraryError}<p class="error">{libraryError}</p>{/if}
        <!-- A failed background/manual check must be visible: "Last checked just
             now" alone would read as healthy while checks are failing. -->
        {#if library.last_error}
          <p class="error">Last check failed: {library.last_error}</p>
        {:else if library.unreachable}
          <p class="error">The library host has been unreachable for a while — checks are paused. Check now to retry.</p>
        {/if}

        <div class="lib-filters">
          <input
            type="search"
            bind:value={librarySearch}
            placeholder="Search templates"
            aria-label="Search templates"
          />
          {#if categories.length > 0}
            <select bind:value={libraryCategory} aria-label="Filter by category">
              <option value="">All categories</option>
              {#each categories as c (c)}
                <option value={c}>{c}</option>
              {/each}
            </select>
          {/if}
        </div>

        {#if (library.templates ?? []).length === 0}
          <EmptyState icon="monitor" title="No templates yet" body="The library is connected but empty right now — try Check now." />
        {:else if filteredTemplates.length === 0}
          <p class="muted">No templates match that search.</p>
        {:else}
          <ul class="lib-list">
            {#each filteredTemplates as t (t.id)}
              <li class="lib-item">
                <div class="lib-item-head">
                  <strong>{t.title}</strong>
                  <Chip kind="">{t.category}</Chip>
                </div>
                <p class="lib-goal">{t.goal}</p>
                <!-- Sources line: EVERY host+path, mono, bold. Never abbreviated — the operator
                     needs to read what they're about to authorize (§19/§20 install-sheet law). -->
                <p class="lib-section-label">Fetches from:</p>
                <ul class="lib-sources">
                  {#each t.sources as s, i (i)}
                    <li><span class="lib-source">{s.host ? `${s.host}${s.path}` : s.type === "internal.kb" ? "your knowledge base (no network)" : s.type === "model" ? "generated by your local model (no fetch)" : s.type}</span></li>
                  {/each}
                </ul>
                {#if t.notes}<p class="muted lib-notes">{t.notes}</p>{/if}
                <div class="lib-item-preview">
                  <NiScene node={t.preview_payload} />
                </div>
                <div class="modal-actions" style="margin-top: var(--s-2)">
                  <button onclick={() => startInstall(t)}>Install…</button>
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      {/if}

      <div class="modal-actions" style="margin-top: var(--s-4)">
        <button class="secondary" disabled={libraryBusy || installBusy} onclick={closeLibrary}>Close</button>
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
    /* W-G (2026-09-17): cards size to their content instead of stretching to
       the row's tallest sibling — the field board showed short quote cards
       with huge empty middles. */
    align-items: start;
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
    flex-wrap: wrap;
  }
  .ni-chips {
    display: inline-flex;
    align-items: center;
    gap: var(--s-1);
    flex-wrap: wrap;
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
  .ni-more {
    position: relative;
    display: inline-block;
  }
  .ni-more > summary {
    list-style: none;
    cursor: pointer;
    padding: 0 var(--s-2);
    border-radius: var(--r-1);
    color: var(--muted);
    line-height: 1.6;
  }
  .ni-more > summary::-webkit-details-marker { display: none; }
  .ni-more > summary:hover { background: var(--bg); }
  .ni-more[open] > .ni-more-menu {
    position: absolute;
    right: 0;
    bottom: 1.8rem;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 2px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--r-2);
    padding: var(--s-1);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
    z-index: 5;
    min-width: 10rem;
  }
  .ni-more-menu button { text-align: left; }
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
  /* Page header row: title + Library ghost button sitting on the same baseline. Wraps on
     narrow viewports so the button never elbows the title off the page. */
  .ni-page-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-2);
    flex-wrap: wrap;
  }
  .ni-page-head h1 { margin: 0; }
  /* Repair-settings modal checkbox row — top-aligned so the sentence wraps under the
     control (not beside a raised baseline). */
  .repair-row {
    display: flex;
    align-items: flex-start;
    gap: var(--s-2);
    margin: var(--s-3) 0 0;
    cursor: pointer;
  }
  .repair-row input[type="checkbox"] {
    margin-top: 0.2em;
    flex-shrink: 0;
  }
  /* --- Global Library sheet (Modal size lg body) ------------------------------------------ */
  .lib-header {
    display: flex;
    align-items: center;
    gap: var(--s-2);
    flex-wrap: wrap;
    margin: 0 0 var(--s-3);
  }
  .lib-header .spacer { flex: 1; }
  /* The fingerprint is monospace + bordered so it reads as a verbatim identifier, not decoration. */
  .lib-fp-chip {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--f-meta);
    padding: 2px 10px;
    border: 1px solid var(--border);
    border-radius: var(--r-full);
    background: var(--panel);
    color: var(--text);
  }
  .lib-checked { font-size: var(--f-meta); }
  .lib-notice {
    margin: 0 0 var(--s-3);
    padding: var(--s-2) var(--s-3);
    background: var(--accent-tint);
    color: var(--accent);
    border-radius: var(--r-1);
    font-size: var(--f-label);
  }
  .lib-filters {
    display: flex;
    gap: var(--s-2);
    margin: 0 0 var(--s-3);
    flex-wrap: wrap;
  }
  .lib-filters input[type="search"] { flex: 1; min-width: 12rem; }
  .lib-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s-3);
  }
  .lib-item {
    padding: var(--s-3);
    border: 1px solid var(--border);
    border-radius: var(--r-2);
    background: var(--panel);
  }
  .lib-item-head {
    display: flex;
    align-items: center;
    gap: var(--s-2);
    flex-wrap: wrap;
    margin-bottom: var(--s-1);
  }
  .lib-goal { margin: 0 0 var(--s-2); font-size: var(--f-label); }
  .lib-section-label {
    margin: var(--s-2) 0 var(--s-1);
    font-size: var(--f-meta);
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  /* Sources list — bold + mono so every host+path is unmissable (§19/§20 install-sheet law). */
  .lib-sources {
    list-style: none;
    padding: 0;
    margin: 0 0 var(--s-2);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .lib-source {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-weight: 700;
    font-size: var(--f-label);
    word-break: break-all;
    color: var(--text);
  }
  .lib-notes { margin: var(--s-1) 0 var(--s-2); font-size: var(--f-meta); }
  .lib-item-preview {
    padding: var(--s-2);
    border: 1px dashed var(--border);
    border-radius: var(--r-1);
    background: var(--elevated);
  }
  .lib-connect-row {
    display: flex;
    gap: var(--s-2);
    align-items: center;
    flex-wrap: wrap;
  }
  .lib-connect-row input { flex: 1; min-width: 16rem; }
  .lib-field-label {
    display: block;
    font-size: var(--f-meta);
    color: var(--muted);
    margin: 0 0 var(--s-1);
  }
  .lib-install-head {
    display: flex;
    gap: var(--s-2);
    align-items: baseline;
    flex-wrap: wrap;
  }
  .lib-params {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
    margin: 0 0 var(--s-3);
  }
  .lib-param { display: flex; flex-direction: column; gap: 2px; }
  .lib-param span { font-size: var(--f-meta); color: var(--muted); }
  .lib-param-secret {
    padding: var(--s-2);
    border: 1px dashed var(--border);
    border-radius: var(--r-1);
    font-size: var(--f-label);
  }
  .lib-preview {
    margin: var(--s-2) 0 0;
    padding: var(--s-2);
    border: 1px dashed var(--border);
    border-radius: var(--r-1);
  }
  /* KeyChanged comparison — same shape as the vault trust-publisher panel in knowledge/. */
  .lib-fp-compare {
    margin: var(--s-2) 0 0;
    display: grid;
    gap: 4px;
  }
  .lib-fp-row {
    display: flex;
    gap: var(--s-2);
    align-items: baseline;
    flex-wrap: wrap;
  }
  .lib-fp-label {
    min-width: 8rem;
    font-size: var(--f-meta);
    font-weight: 600;
  }
  .lib-fp {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--f-label);
    color: var(--text);
    word-break: break-all;
  }
  .lib-trust-row {
    display: flex;
    gap: var(--s-2);
    align-items: center;
    flex-wrap: wrap;
  }
  .lib-trust-row input { flex: 1; min-width: 10rem; }
  /* --- L2 proposal review modal (§23) --------------------------------------------------- */
  /* Side-by-side on wide viewports, stacked on narrow. Every mapping is verbatim JSON in
     a mono <pre>: the approval surface must show the EXACT values that would be applied
     (approval-surface law) — no highlighting/collapsing masks the change. */
  .prop-diff {
    display: grid;
    gap: var(--s-3);
    grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
    margin: var(--s-3) 0 0;
  }
  .prop-col {
    padding: var(--s-3);
    border: 1px solid var(--border);
    border-radius: var(--r-2);
    background: var(--panel);
    min-width: 0;
  }
  .prop-col-label {
    margin: 0 0 var(--s-2);
    font-weight: 600;
    font-size: var(--f-label);
  }
  .prop-sub-label {
    margin: var(--s-2) 0 var(--s-1);
    font-size: var(--f-meta);
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .prop-json {
    margin: 0;
    padding: var(--s-2);
    background: var(--elevated);
    border: 1px dashed var(--border);
    border-radius: var(--r-1);
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--f-meta);
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-x: auto;
    max-height: 20rem;
  }
  /* Status-truth first-run failure line: muted text with the class in the warn color
     so the truth is legible without shouting. (§ Status-truth amendments.) */
  .ni-status-fail {
    color: var(--muted);
  }
  .ni-status-class {
    color: var(--warn);
  }
  /* Needs-your-key row: sits between the body and any actions. Wraps on narrow cards
     so the label stacks above the button rather than truncating. */
  .ni-composer {
    display: flex;
    gap: var(--s-2);
    margin: var(--s-3) 0 var(--s-4);
  }
  .ni-composer input {
    flex: 1;
    min-width: 0;
  }
  .ni-needs-key {
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .ni-needs-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-2);
    padding: var(--s-2) var(--s-3);
    background: var(--accent-tint);
    border-radius: var(--r-1);
    flex-wrap: wrap;
  }
  .ni-needs-copy { font-size: var(--f-label); }
  /* History linklike sits inside the actions row — same size as the ghost buttons
     around it so the baseline stays clean. */
  .ni-history { font-size: var(--f-meta); }
  /* Add-key modal form rows — label above input, matches the schedules field layout. */
  .cred-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin: var(--s-3) 0 0;
  }
  .cred-row span { font-size: var(--f-meta); color: var(--muted); }
  /* Run-history list — one row per run, tabular baseline (no zebra: the timestamps
     already carry rhythm). */
  .hist-list {
    list-style: none;
    padding: 0;
    margin: var(--s-2) 0 0;
    display: flex;
    flex-direction: column;
    gap: var(--s-1);
    max-height: 24rem;
    overflow-y: auto;
  }
  .hist-row {
    display: flex;
    align-items: baseline;
    gap: var(--s-2);
    font-size: var(--f-label);
    flex-wrap: wrap;
  }
  .hist-when { min-width: 6rem; font-size: var(--f-meta); }
  .hist-status { font-weight: 600; }
  .hist-error { font-size: var(--f-meta); }
  /* The one warning panel this page owns (matches knowledge/ .warn treatment). */
  .warn {
    border: 1px solid var(--danger);
    background: color-mix(in srgb, var(--danger) 10%, transparent);
    color: var(--text);
    padding: var(--s-2) var(--s-3);
    border-radius: var(--r-2);
  }
</style>
