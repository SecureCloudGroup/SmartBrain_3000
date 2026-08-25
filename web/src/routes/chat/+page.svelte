<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { chatSession } from "$lib/chat.svelte";
  import { resumeChat } from "$lib/chat-resume";
  import { refreshPending } from "$lib/pending.svelte";
  import { api, ApiError, type AgentResult, type ChatMessage, type Conversation, type DiscoveredModel, type PendingAction, type RecentScheduleRun, type Source, type VoiceStatus } from "$lib/api";
  import { finalAssistantId, mergeRefreshedLog, transcriptUpToLastUser } from "$lib/chat-log";
  import { parseTs } from "$lib/runs";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";
  import Markdown from "$lib/Markdown.svelte";
  import { remote } from "$lib/remote/connection.svelte";
  import { scheduleUpdates } from "$lib/scheduleUpdates.svelte";
  import ActionCard from "$lib/components/ActionCard.svelte";
  import { fmtArgs, iconForTool } from "$lib/pendingCards";
  import { Recorder } from "$lib/audio/recorder";
  import { Speaker, speechAvailable } from "$lib/audio/speaker";
  import Chip from "$lib/components/Chip.svelte";
  import EmptyState from "$lib/components/EmptyState.svelte";
  import Icon from "$lib/components/Icon.svelte";
  import Spinner from "$lib/components/Spinner.svelte";
  import type { IconName } from "$lib/icons";

  // Entry carries a stable id so {#each} can key on it (U16) — re-renders no longer
  // jump when a streaming assistant message mutates in place. `schedule` marks a fired
  // scheduled-run update injected into the view (display-only; excluded from the transcript).
  // `sources` = the turn's deterministic citations (from tool results), rendered as chips.
  type Entry = ChatMessage & {
    id: string; err?: boolean; schedule?: boolean; sources?: Source[];
    // Optimizer transparency: which learned guidance shaped this answer (live-session
    // only — not persisted with the message; the ledger keeps the durable record).
    guidance?: { request_type: string; directive: string };
  };
  let liveGuidance: { request_type: string; directive: string } | null = null;

  let conversations = $state<Conversation[]>([]);
  let convosCursor = $state<string | null>(null); // next-older page cursor for the saved list
  let convosHasMore = $state(false);
  let log = $state<Entry[]>([]);
  let msgCursor = $state<string | null>(null); // next-older page cursor for the open conversation
  let msgHasMore = $state(false);
  let input = $state("");
  let busy = $state(false);
  // Token for a first model response the server parked during the streaming phase (see applyEvents).
  let primedToken: string | null = null;
  let error = $state("");
  // Set when a turn ran WITHOUT tools (the model can't call them) — otherwise the
  // assistant can sound like it acted when nothing did. Surfaced as a notice.
  let modelNotice = $state("");
  const DEGRADED_NOTICE =
    "This model can't use tools, so it answered from its own knowledge only — " +
    "web search, tasks, knowledge, and email actions won't run. Pick a tool-capable model above for those.";
  let pendingTurnId = $state<string | null>(null);
  let resumeNotice = $state(""); // transient: shown if Resume is clicked before the action is approved
  // Non-null only while a STREAMED turn is in flight — flips the composer's Send into Stop.
  // The non-streaming paths (remote/WebRTC, approval fallback) can't be interrupted mid-flight,
  // so they never set it.
  let stopper = $state<AbortController | null>(null);
  let copiedId = $state<string | null>(null); // entry whose Copy just succeeded ("Copied ✓" flip)
  // Live tool-activity lines for the current tool-running turn (the events endpoint
  // narrates: "Searching the web… ✓"). Cleared when the turn resolves.
  let activity = $state<{ tool: string; detail: string; done: boolean; ok: boolean }[]>([]);
  const TOOL_LABELS: Record<string, string> = {
    web_search: "Searching the web", web_research: "Researching the web",
    web_fetch: "Reading a page", kb_search: "Searching your knowledge",
    read_document: "Reading a document", summarize_document: "Summarizing a document",
    list_documents: "Listing documents", list_tasks: "Checking your tasks",
    email_list: "Checking email", email_read: "Reading an email",
    __answering: "Writing the answer",
  };
  const TOOL_ICONS: Record<string, IconName> = {
    web_search: "search", web_research: "search", web_fetch: "link",
    kb_search: "book", read_document: "file", summarize_document: "file",
    list_documents: "book", list_tasks: "tasks", email_list: "mail", email_read: "mail",
    __answering: "pencil",
  };
  let renaming = $state(false); // inline rename of the open conversation (Knowledge's idiom)
  let renameValue = $state("");
  // The only answer Regenerate is offered on — regenerating an older one would fork the thread.
  const lastAnswerId = $derived(finalAssistantId(log));

  // Stable client-side ids for entries we just appended (server-issued ids are used
  // for messages loaded from history). Monotonic counter; bounded by user actions.
  let entrySeq = 0;
  const nextEntryId = (kind: string): string => {
    entrySeq += 1;
    return `c-${kind}-${entrySeq}`;
  };

  // Scheduled-run updates surface right in the open chat, each wrapped in a
  // "### Scheduled Item … ###" header/footer so it reads as a distinct, just-ran notice rather
  // than a normal reply. Display-only: never persisted or sent back to the model (buildTranscript
  // drops schedule entries), so they can't pollute a conversation's saved thread. Opening chat
  // pulls anything unseen; a light poll surfaces new ones live while you sit here. Marking them
  // seen clears the Chat nav badge. The durable copy always lives on the Info page.
  let pulling = false; // guards against overlapping pulls (mount + interval)
  let updatesTimer: ReturnType<typeof setInterval> | null = null;

  function wrapScheduleUpdate(run: RecentScheduleRun): string {
    const body = run.error
      ? run.error
      : run.status === "awaiting_approval"
        ? "Awaiting your approval — open Activity to review."
        : run.message || "(no output)";
    return `### Scheduled Item ${run.schedule_title} ###\n\n${body}\n\n### End of Scheduled Item ${run.schedule_title} ###`;
  }

  async function pullScheduleUpdates(): Promise<void> {
    if (pulling || busy || !account.status?.unlocked) return; // don't interleave with a live turn
    pulling = true;
    try {
      const { count } = await api.unseenScheduleUpdates(); // cheap plaintext count first
      if (count === 0) {
        scheduleUpdates.count = 0;
        return;
      }
      const fresh = (await api.recentScheduleRuns()).runs.filter((r) => !r.seen);
      // recentScheduleRuns is newest-first; append oldest-first so they read in order at the bottom.
      for (const run of fresh.reverse()) {
        log.push({ id: `sched-${run.id}`, role: "assistant", schedule: true, content: wrapScheduleUpdate(run) });
      }
      await api.markScheduleUpdatesSeen(); // one-time notice; also clears the badge
      scheduleUpdates.count = 0;
    } catch {
      /* locked / offline — leave the badge as-is and try again on the next tick */
    } finally {
      pulling = false;
    }
  }

  // Starter prompts shown when the chat log is empty (U6). Kept short + concrete so
  // a clicked chip drops straight into the composer.
  // ---- Scroll management (view-layer only; the streaming machine below is untouched). ----
  // A sentinel at the log's end drives both behaviors: auto-stick while the reader is at
  // the bottom, and a "Jump to latest" pill the moment they scroll up during a stream.
  let logEnd = $state<HTMLElement | null>(null);
  let atBottom = $state(true);
  $effect(() => {
    if (!logEnd) return;
    const io = new IntersectionObserver(([e]) => (atBottom = e.isIntersecting), {
      rootMargin: "0px 0px 160px 0px", // "near enough" — the sticky composer covers the tail
    });
    io.observe(logEnd);
    return () => io.disconnect();
  });
  // The id of the assistant entry currently streaming — set when the stream bubble is
  // actually created (first delta), not when the request starts. Deriving it from "the
  // newest assistant entry while a stopper exists" mislabeled the PREVIOUS answer as
  // streaming during the pre-first-token wait, and hid the thinking row with it.
  let streamEntryId = $state<string | null>(null);
  const lastLen = $derived(log.length ? log[log.length - 1].content.length : 0);
  // Scroll the WINDOW to the true end of the document. scrollIntoView on the sentinel
  // put it at the viewport bottom — which the sticky composer covers, so "jump to
  // latest" always hid the newest message behind the input. Document-end can't: at full
  // scroll the composer sits at its natural in-flow position below the log.
  function scrollToBottom(smooth = false) {
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: document.documentElement.scrollHeight,
                      behavior: smooth && !reduce ? "smooth" : "auto" });
  }
  $effect(() => {
    void lastLen; // track every streamed delta + new entries
    void log.length;
    if (atBottom) requestAnimationFrame(() => scrollToBottom(false));
  });
  function jumpToLatest() {
    scrollToBottom(true);
  }
  // "Back to top" appears once the reader is meaningfully into the history.
  let showTop = $state(false);
  function onWindowScroll() {
    showTop = window.scrollY > 500;
  }
  function jumpToTop() {
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: reduce ? "auto" : "smooth" });
  }

  // ---- Voice (V1): push-to-talk dictation + spoken replies. ----
  // STT runs on the user's LOCAL audio server (see Settings → Local models); the mic
  // only shows when one is configured. TTS prefers the browser's system voices and
  // falls back to the server voice — the Speaker owns that chain.
  const recorder = new Recorder();
  let recState = $state<"idle" | "recording" | "transcribing">("idle");
  let voiceInfo = $state<VoiceStatus | null>(null);
  let voicePollTimer: ReturnType<typeof setInterval> | null = null;
  // The mic is USABLE when a server will take the call, or the local engine is loaded.
  // Anything else renders as live progress ON the button — never a dead click.
  const micUsable = $derived(
    !!voiceInfo && (voiceInfo.engine === "server" || voiceInfo.local?.phase === "ready"),
  );

  // While the local engine is absent/downloading/loading, poll so the button's percent
  // moves and the mic enables itself the moment the engine is ready (field lesson: an
  // invisible download reads as a broken product).
  function syncVoicePolling(): void {
    const settling = voiceInfo && !micUsable && voiceInfo.local?.phase !== "error";
    if (settling && !voicePollTimer) {
      voicePollTimer = setInterval(async () => {
        voiceInfo = await api.voiceStatus().catch(() => voiceInfo);
        syncVoicePolling();
      }, 2000);
    } else if (!settling && voicePollTimer) {
      clearInterval(voicePollTimer);
      voicePollTimer = null;
    }
  }

  function retryVoiceModel(): void {
    // Error state: the button becomes "retry the download" — one tap, polling resumes.
    void api
      .voiceStatus(false, true)
      .then((v) => {
        voiceInfo = v;
        syncVoicePolling();
      })
      .catch(() => undefined);
  }
  let speechPossible = $state(false);
  let autoSpeak = $state(false);
  let listeningId = $state<string | null>(null); // which message the Listen button is reading
  const speaker = new Speaker(
    (text) => (voiceInfo?.tts_model ? api.voiceSpeak(text) : Promise.resolve(null)),
    () => (listeningId = null),
  );

  async function toggleMic(): Promise<void> {
    if (recState === "transcribing" || busy) return;
    if (recState === "idle") {
      speaker.stop(); // barge-in: talking to it interrupts it
      listeningId = null;
      error = "";
      try {
        await recorder.start();
        recState = "recording";
      } catch (err) {
        // Name the ACTUAL failure — the first field test hit a CSP refusal that a
        // generic "check mic permission" message sent the user chasing in the
        // wrong direction entirely.
        console.error("mic start failed:", err);
        if (err instanceof DOMException && (err.name === "NotAllowedError" || err.name === "SecurityError")) {
          error = "Microphone access was denied — allow it for this site in the browser.";
        } else if (err instanceof DOMException && err.name === "NotFoundError") {
          error = "No microphone was found on this device.";
        } else {
          error = `Could not start the microphone: ${err instanceof Error ? err.message : String(err)}`;
        }
      }
      return;
    }
    recState = "transcribing";
    try {
      const blob = await recorder.stop();
      const r = await api.voiceTranscribe(blob);
      // Into the composer, editable — dictation proposes, the user disposes (V1:
      // no auto-send; reviewing the transcript before it runs is the safe default).
      if (r.text) input = input ? `${input.trimEnd()} ${r.text}` : r.text;
    } catch (err) {
      // The server's own words, verbatim: describeError's friendly 502 wording hid
      // "Model 'whisper-…' not found" behind "couldn't reach the model" in the field.
      error = err instanceof ApiError && err.message ? err.message : describeError(err);
      // A not-ready failure means state moved server-side — re-sync so the button
      // shows the live phase instead of leaving the user to guess.
      voiceInfo = await api.voiceStatus().catch(() => voiceInfo);
      syncVoicePolling();
    } finally {
      recState = "idle";
    }
  }

  function toggleAutoSpeak(): void {
    autoSpeak = !autoSpeak;
    try {
      localStorage.setItem("sb:autospeak", autoSpeak ? "1" : "0");
    } catch {
      /* storage unavailable — the toggle still works for this visit */
    }
    if (autoSpeak) {
      // iOS only allows speech that a user gesture started — this click IS the
      // gesture, so prime the engine with a silent utterance while we have it.
      if (typeof speechSynthesis !== "undefined") speechSynthesis.speak(new SpeechSynthesisUtterance(" "));
    } else {
      speaker.stop();
      listeningId = null;
    }
  }

  function listen(entry: Entry): void {
    if (listeningId === entry.id) {
      speaker.stop();
      listeningId = null;
      return;
    }
    speaker.stop();
    listeningId = entry.id;
    speaker.say(entry.content);
  }

  const STARTERS = [
    "What can you do?",
    "Save a note to my knowledge base.",
    "Add 'buy milk' to my tasks.",
  ];

  // Two-step model selection: pick a provider, then one of its models. Both
  // selectors always show a concrete model in use (defaulted to the routed chat
  // model). Every message runs as an agent turn — the model decides per-message
  // whether a tool is needed (tool_choice: auto), degrading to a plain answer
  // for models that can't use tools.
  let models = $state<DiscoveredModel[]>([]);
  let provider = $state("");
  let modelId = $state("");
  let routedChat = $state(""); // the server-side default chat model (Settings → Model routing)
  // When chat has zero models but a local server is running on its default port, offer a
  // one-tap connect right here instead of sending the user off to Settings (the all-local
  // first-run cliff). null = nothing detected.
  let detected = $state<{ provider: "ollama" | "mlx"; url: string } | null>(null);
  // Until the probe answers, show NOTHING rather than "add a cloud key" guidance that a detected
  // Ollama will contradict a second later — first-run is the worst moment for conflicting advice.
  let probed = $state(false);
  let modelsDegraded = $state(false); // catalog fell back to direct local probes
  let modelsError = $state(""); // the model LIST fetch itself failed (distinct from an empty catalog)
  let connecting = $state(false);
  const providers = $derived([...new Set(models.map((m) => m.provider))].sort());
  const providerModels = $derived(models.filter((m) => m.provider === provider));

  // Conversation start date (created_at is UTC; show it in the user's local date).
  const startDate = (iso: string) => parseTs(iso)?.toLocaleDateString() ?? iso;

  onMount(async () => {
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await Promise.all([loadModels(), loadConversations()]);
    // Resume the open chat by fetching it DIRECTLY (not gated on the list — a transient list
    // failure must not masquerade as a new chat). resumeChat drops currentId only on a 404;
    // a transient error keeps it so the next visit retries (regression #11).
    try {
      const msgs = await resumeChat(chatSession, api.getConversation, (e) => e instanceof ApiError && e.status === 404);
      log = msgs.map((m) => ({ id: m.id, role: m.role, content: m.content, sources: m.sources }));
      // resumeChat returns the newest page's messages (server default); if there are older
      // ones, re-fetch through getConversation to capture next_cursor/has_more.
      if (chatSession.currentId) await refreshOpenCursor(chatSession.currentId);
      await loadPendingActions(chatSession.currentId); // re-show approvals if a parked turn survived a nav away
    } catch (err) {
      error = describeError(err);
    }
    // Open at the newest message, unconditionally. The auto-stick effect above also fires,
    // but rendered markdown settles its final heights a beat after first paint — the second
    // pass lands the log truly at the bottom instead of "almost".
    if (log.length > 0) {
      requestAnimationFrame(() => scrollToBottom(false));
      setTimeout(() => scrollToBottom(false), 200);
    }
    window.addEventListener("scroll", onWindowScroll, { passive: true });
    // Voice availability (no live probe — configured-ness is enough to show the mic).
    void api
      .voiceStatus()
      .then(async (v) => {
        voiceInfo = v;
        syncVoicePolling();
        speechPossible = await speechAvailable(!!v.tts_model);
        try {
          autoSpeak = speechPossible && localStorage.getItem("sb:autospeak") === "1";
        } catch {
          autoSpeak = false;
        }
      })
      .catch(() => undefined);
    await pullScheduleUpdates(); // surface anything that fired while away, right here in the chat
    updatesTimer = setInterval(pullScheduleUpdates, 25000); // keep new ones arriving live while viewing Chat
    // Returning to the app (phone unlock, tab switch) re-syncs what the OTHER device did.
    document.addEventListener("visibilitychange", onVisibility);
  });

  onDestroy(() => {
    if (updatesTimer) clearInterval(updatesTimer);
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("scroll", onWindowScroll);
    speaker.stop(); // navigating away must not leave a voice talking
    if (voicePollTimer) clearInterval(voicePollTimer);
  });

  // The open conversation's blocked actions, rendered as inline approval cards above the
  // composer (U8 follow-up: approving no longer requires a trip to Activity). The server's
  // pending list is the source of truth — re-derived on mount, on park, and after every
  // verdict, so it survives reloads and nav-aways.
  let pendingActions = $state<PendingAction[]>([]);
  let approvalBusy = $state(""); // pending-action id, or "all", while a verdict is in flight
  // Approve-all only when every card is reviewed-tier: an irreversible action must keep
  // its own deliberate confirm, never ride a batch.
  const allReviewed = $derived(pendingActions.length > 1 && pendingActions.every((p) => p.tier !== "irreversible"));

  async function loadPendingActions(cid: string | null): Promise<void> {
    if (!cid) {
      pendingActions = [];
      pendingTurnId = null;
      return;
    }
    try {
      const { pending } = await api.listPending();
      pendingActions = pending.filter((p) => p.conversation_id === cid);
      pendingTurnId = pendingActions.find((p) => p.turn_id)?.turn_id ?? null;
    } catch {
      // a transient failure just leaves the cards absent (Activity still lists the approval)
    }
  }

  // One verdict, then continue: when the LAST card resolves, the parked turn resumes by
  // itself — approve/deny in place of the old approve-in-Activity-then-click-Resume trek.
  async function resolveApproval(p: PendingAction, verdict: "approve" | "deny", remember = false) {
    if (approvalBusy || busy) return;
    if (
      verdict === "approve" &&
      p.tier === "irreversible" &&
      !(await confirmDialog({
        title: "Irreversible action",
        body: `Run ${p.tool}? This cannot be undone.`,
        confirmLabel: "Run",
        danger: true,
      }))
    )
      return;
    approvalBusy = p.id;
    error = "";
    const turn = pendingTurnId; // survives the list going empty below
    try {
      if (verdict === "approve") {
        await api.approveAction(p.id, p.tier === "irreversible" ? p.tool : null, remember);
      } else {
        await api.denyAction(p.id);
      }
      pendingActions = pendingActions.filter((x) => x.id !== p.id);
      refreshPending(); // nav badge
      if (pendingActions.length === 0 && turn) {
        approvalBusy = "";
        await resume();
      }
    } catch (err) {
      error = describeError(err);
      await loadPendingActions(chatSession.currentId); // stale/expired items drop off
    } finally {
      approvalBusy = "";
    }
  }

  async function approveAll() {
    if (approvalBusy || busy || !allReviewed) return;
    approvalBusy = "all";
    error = "";
    const turn = pendingTurnId;
    try {
      for (const p of [...pendingActions]) {
        await api.approveAction(p.id, null, false);
        pendingActions = pendingActions.filter((x) => x.id !== p.id);
      }
      refreshPending();
      if (turn) {
        approvalBusy = "";
        await resume();
      }
    } catch (err) {
      error = describeError(err);
      await loadPendingActions(chatSession.currentId);
    } finally {
      approvalBusy = "";
    }
  }

  // Pull next_cursor/has_more for the open conversation without disturbing `log`
  // (resumeChat itself doesn't expose pagination metadata).
  async function refreshOpenCursor(id: string): Promise<void> {
    console.assert(typeof id === "string" && id.length > 0, "refreshOpenCursor needs an id");
    try {
      const convo = await api.getConversation(id);
      console.assert(Array.isArray(convo.messages), "convo.messages must be an array");
      msgCursor = convo.next_cursor ?? null;
      msgHasMore = !!convo.has_more;
    } catch {
      msgCursor = null;
      msgHasMore = false;
    }
  }

  // Probe for an unconfigured-but-running local server so the empty state can offer to
  // connect it in one tap. Best-effort: any failure just leaves `detected` null.
  async function detectLocal() {
    console.assert(typeof api.localModels === "function", "localModels API missing");
    console.assert(models.length === 0, "detectLocal only runs when no models are available");
    try {
      const local = await api.localModels();
      if (local.ollama.detected) detected = { provider: "ollama", url: local.ollama.default_url };
      else if (local.mlx.detected) detected = { provider: "mlx", url: local.mlx.default_url };
      else detected = null;
    } catch {
      detected = null; // locked / gateway not ready — fall back to the Settings guidance
    } finally {
      probed = true;
    }
  }

  async function connectLocal() {
    if (!detected) return;
    console.assert(detected.url.length > 0, "connectLocal needs a detected url");
    console.assert(detected.provider === "ollama" || detected.provider === "mlx", "unknown local provider");
    connecting = true;
    try {
      if (detected.provider === "ollama") await api.putOllama(detected.url);
      else await api.putMlx(detected.url, "");
      await loadModels(); // re-list; the gateway may take a moment to surface the models
    } catch (err) {
      error = describeError(err);
    } finally {
      connecting = false;
    }
  }

  async function loadModels() {
    modelsError = "";
    try {
      const res = await api.listModels();
      models = res.models.filter((x) => x.chat); // embeddings/image can't chat
      modelsDegraded = res.degraded === true;
    } catch (err) {
      models = [];
      modelsDegraded = false;
      modelsError = describeError(err); // shown in place of the misleading "add a key" empty state
    }
    if (models.length === 0) {
      await detectLocal(); // offer a one-tap connect if a local server is running
      return; // nothing routed/selectable yet
    }
    detected = null;
    try {
      routedChat = (await api.getRoutes()).routes?.chat ?? "";
    } catch {
      /* routing unavailable — fall back to first model */
    }
    // The routed "chat" model (Settings → Model routing) is the authoritative DEFAULT and the
    // single source of truth shared by Desktop + PWA (stored server-side, in backups, survives
    // reboots/upgrades). It always wins on load so changing the routing default propagates here;
    // a manual pick below is session-only and never persisted (a stale local pick used to
    // silently override the routed default — e.g. defaulting to gemini after routing to MLX).
    const def = models.find((x) => x.id === routedChat) || models[0];
    if (def) {
      provider = def.provider;
      modelId = def.id;
    }
  }

  // On provider change, default to that provider's first model (or clear it when the
  // provider exposes no chat-capable models — U6's disabled placeholder handles the UI).
  function onProvider() {
    const list = models.filter((m) => m.provider === provider);
    modelId = list.length ? list[0].id : "";
  }

  async function loadConversations(): Promise<boolean> {
    try {
      const page = await api.listConversations();
      conversations = page.conversations;
      convosCursor = page.next_cursor ?? null;
      convosHasMore = !!page.has_more;
      return true;
    } catch (err) {
      error = describeError(err);
      return false; // transient failure — caller must not treat as "no conversations"
    }
  }

  // M4: append the next-older page to the saved-conversations list using the
  // server-issued cursor. No-op when has_more is false.
  async function loadOlderConversations(): Promise<void> {
    console.assert(convosHasMore, "loadOlderConversations called with no more pages");
    console.assert(convosCursor !== null, "loadOlderConversations needs a cursor");
    if (!convosHasMore || !convosCursor) return;
    try {
      const page = await api.listConversations({ before: convosCursor });
      conversations = [...conversations, ...page.conversations];
      convosCursor = page.next_cursor ?? null;
      convosHasMore = !!page.has_more;
    } catch (err) {
      error = describeError(err);
    }
  }

  // M4: prepend the next-older page of messages for the open conversation. The server
  // returns pages oldest-first within the page, so prepending preserves chronological order.
  async function loadOlderMessages(): Promise<void> {
    console.assert(msgHasMore, "loadOlderMessages called with no more pages");
    console.assert(chatSession.currentId !== null, "loadOlderMessages needs an open conversation");
    if (!msgHasMore || !msgCursor || chatSession.currentId === null) return;
    try {
      const page = await api.getConversation(chatSession.currentId, { before: msgCursor });
      const older = page.messages.map((m) => ({ id: m.id, role: m.role, content: m.content, sources: m.sources }));
      log = [...older, ...log];
      msgCursor = page.next_cursor ?? null;
      msgHasMore = !!page.has_more;
    } catch (err) {
      error = describeError(err);
    }
  }

  // Refresh from the server: the conversation list + the open thread. This is how a
  // desktop picks up messages sent from the paired phone (and vice versa) without a
  // full reload — via the toolbar button, or automatically when the app becomes
  // visible again. Guarded so it never races a live turn, a schedule pull, or an
  // in-flight rename.
  async function refreshChat(): Promise<void> {
    if (busy || pulling || renaming) return;
    error = "";
    await loadConversations();
    const cid = chatSession.currentId;
    if (!cid) return;
    try {
      const convo = await api.getConversation(cid);
      const server = convo.messages.map((m) => ({ id: m.id, role: m.role, content: m.content, sources: m.sources }));
      log = mergeRefreshedLog(server, log) as typeof log;
      msgCursor = convo.next_cursor ?? null;
      msgHasMore = !!convo.has_more;
      await loadPendingActions(cid);
    } catch (err) {
      // Gone (deleted elsewhere) -> drop it, matching select(); transient -> surface.
      if (err instanceof ApiError && err.status === 404) chatSession.currentId = null;
      else error = describeError(err);
    }
  }

  function onVisibility(): void {
    if (document.visibilityState === "visible") void refreshChat();
  }

  async function select(id: string) {
    error = "";
    pendingTurnId = null; // never carry a parked turn across a conversation switch
    pendingActions = [];
    resumeNotice = "";
    renaming = false; // a half-typed rename belongs to the conversation being left
    try {
      const convo = await api.getConversation(id);
      chatSession.currentId = id;
      log = convo.messages.map((m) => ({ id: m.id, role: m.role, content: m.content, sources: m.sources }));
      msgCursor = convo.next_cursor ?? null;
      msgHasMore = !!convo.has_more;
      await loadPendingActions(id); // this conversation may have a parked turn awaiting approval
    } catch (err) {
      // Gone (deleted) -> drop it so it can't error forever; transient -> keep it for next visit.
      if (err instanceof ApiError && err.status === 404) chatSession.currentId = null;
      else error = describeError(err);
    }
  }

  function newChat() {
    chatSession.currentId = null;
    log = [];
    msgCursor = null;
    msgHasMore = false;
    pendingTurnId = null;
    pendingActions = [];
    resumeNotice = "";
    renaming = false;
    error = "";
  }

  // Inline rename of the OPEN conversation — same Rename → input + Save/Cancel + Enter
  // idiom as Knowledge's document rename.
  function startRename() {
    const current = conversations.find((c) => c.id === chatSession.currentId);
    if (!current) return;
    renameValue = current.title;
    renaming = true;
    error = "";
  }
  function cancelRename() {
    renaming = false;
  }
  async function saveRename(): Promise<void> {
    const t = renameValue.trim();
    const cid = chatSession.currentId;
    if (!t || cid === null) return;
    error = "";
    try {
      await api.renameConversation(cid, t);
      renaming = false;
      await loadConversations(); // re-list so the picker shows the server's (source-of-truth) title
    } catch (err) {
      error = describeError(err);
    }
  }

  // Move EVERY chat to the Trash (Settings -> Account & Data holds restore for 30 days).
  // Lives here because chats are deleted where chats live; the confirm is non-negotiable.
  async function removeAll() {
    const ok = await confirmDialog({
      title: "Delete all chats",
      body: "Move every chat to the Trash? You can restore them from Settings \u2192 Account & Data for 30 days.",
      confirmLabel: "Delete all",
      danger: true,
    });
    if (!ok) return;
    error = "";
    try {
      await api.deleteAllConversations();
      newChat();
      await loadConversations();
    } catch (err) {
      error = describeError(err);
    }
  }

  async function remove(id: string) {
    error = "";
    try {
      await api.deleteConversation(id);
      if (chatSession.currentId === id) newChat();
      await loadConversations();
    } catch (err) {
      error = describeError(err);
    }
  }

  // Click a starter chip: fill the composer (don't auto-send — give the user a beat to edit).
  function useStarter(text: string): void {
    console.assert(typeof text === "string" && text.length > 0, "useStarter needs a non-empty prompt");
    console.assert(!busy, "useStarter should be inert while a turn is in flight");
    if (busy) return;
    input = text;
  }

  // Build the message transcript the agent turn endpoints expect (errored bubbles excluded —
  // they were never persisted server-side either).
  function buildTranscript(): ChatMessage[] {
    console.assert(Array.isArray(log), "log must be an array");
    // Exclude errored bubbles AND injected scheduled-update notices — neither was persisted
    // server-side, and a scheduled update is a display-only notice, not part of this chat's thread.
    const out = log.filter((e) => !e.err && !e.schedule).map(({ role, content }) => ({ role, content }));
    console.assert(out.length >= 1, "transcript must contain at least the user's new turn");
    return out;
  }

  async function send() {
    const text = input.trim();
    if (!text || busy || !modelId) return; // need a concrete model selected
    busy = true;
    error = "";
    modelNotice = "";
    input = "";
    try {
      if (chatSession.currentId === null) {
        chatSession.currentId = (await api.createConversation(text.slice(0, 60))).id;
        await loadConversations();
      }
      const cid = chatSession.currentId;
      log.push({ id: nextEntryId("user"), role: "user", content: text });
      await api.addMessage(cid, "user", text);

      await runTurn(buildTranscript(), cid);
      await loadConversations(); // refresh recency/order
    } catch (err) {
      const text2 = describeError(err);
      if (text2) log.push({ id: nextEntryId("err"), role: "assistant", content: text2, err: true });
    } finally {
      busy = false;
    }
  }

  // One agent turn, dispatched the right way (shared by send + regenerate):
  // Desktop/local -> stream tokens. Remote (WebRTC relay buffers SSE) -> non-stream.
  async function runTurn(messages: ChatMessage[], cid: string): Promise<void> {
    speaker.stop(); // a new turn silences the previous answer (its reply will speak)
    listeningId = null;
    if (remote.status === "idle") {
      await streamTurn({ messages, cid });
    } else {
      const res = await api.agentTurn({ messages, model: modelId, conversation_id: cid });
      await handleAgentResult(res, cid);
    }
  }

  // Regenerate the thread's final answer: re-run the turn from the history up to (and
  // including) the last user message. There is no delete-message route server-side, so
  // the fresh answer APPENDS below the old one — what you see is exactly what a reload
  // shows (visually replacing it would diverge from the stored thread).
  async function regenerate(): Promise<void> {
    const cid = chatSession.currentId;
    if (busy || !modelId || cid === null) return;
    const messages = transcriptUpToLastUser(log);
    if (!messages) return; // no user message to regenerate from
    void api.feedback("regenerate", cid); // implicit-dissatisfaction signal (best-effort)
    busy = true;
    error = "";
    modelNotice = "";
    try {
      await runTurn(messages, cid);
      await loadConversations(); // refresh recency/order
    } catch (err) {
      const text = describeError(err);
      if (text) log.push({ id: nextEntryId("err"), role: "assistant", content: text, err: true });
    } finally {
      busy = false;
    }
  }

  // Re-send a previous user message verbatim (the Retry pill beside "You"): a nudge
  // that didn't land shouldn't need retyping. It appends as a NEW user message —
  // exactly as if retyped and sent — so what you see stays what a reload shows.
  async function retryMessage(entry: Entry): Promise<void> {
    const cid = chatSession.currentId;
    if (busy || !modelId || cid === null) return;
    void api.feedback("retry", cid); // implicit-dissatisfaction signal (best-effort)
    busy = true;
    error = "";
    modelNotice = "";
    try {
      log.push({ id: nextEntryId("user"), role: "user", content: entry.content });
      await api.addMessage(cid, "user", entry.content);
      await runTurn(buildTranscript(), cid);
      await loadConversations(); // refresh recency/order
    } catch (err) {
      const text = describeError(err);
      if (text) log.push({ id: nextEntryId("err"), role: "assistant", content: text, err: true });
    } finally {
      busy = false;
    }
  }

  // Copy an answer's RAW markdown (entry content, not rendered HTML) — same clipboard +
  // 1.5s "Copied ✓" flip idiom as Settings → MCP's copy().
  async function copyMessage(entry: Entry): Promise<void> {
    try {
      await navigator.clipboard.writeText(entry.content);
      copiedId = entry.id;
      setTimeout(() => {
        if (copiedId === entry.id) copiedId = null; // don't clobber a newer copy's flip
      }, 1500);
    } catch {
      /* clipboard unavailable — the user can select the text */
    }
  }

  // Stream a single agent turn over SSE. On `delta` we mutate the open assistant
  // bubble in place; `done` finalizes + persists; `pending`/`tools` falls back to
  // the non-streaming endpoint so the existing approval/Resume flow still works.
  // While the stream is live, `stopper` can abort it (the composer's Stop button):
  // the partial answer is kept and persisted — a Stop is a choice, not an error.
  async function streamTurn(args: { messages: ChatMessage[]; cid: string }): Promise<void> {
    console.assert(Array.isArray(args.messages) && args.messages.length > 0, "streamTurn needs messages");
    console.assert(typeof args.cid === "string" && args.cid.length > 0, "streamTurn needs a conversation id");
    // Lazily-created streaming assistant bubble; we only show it once the first `delta` lands
    // so the bubble doesn't appear empty on a turn that immediately parks for approval.
    let streamId: string | null = null;
    let streamText = "";
    const ensureStreamBubble = (): string => {
      if (streamId) return streamId;
      const id = nextEntryId("asst");
      streamId = id;
      streamEntryId = id;
      log.push({ id, role: "assistant", content: "" });
      return id;
    };
    const controller = new AbortController();
    stopper = controller;
    try {
      const res = await api.agentTurnStream({
        messages: args.messages,
        model: modelId,
        conversation_id: args.cid,
      }, controller.signal);
      const body = res.body;
      if (!body) {
        // No streamable body — fall back so the user still gets an answer. Not
        // interruptible, so drop the Stop affordance first.
        stopper = null;
        const fallback = await api.agentTurn({ messages: args.messages, model: modelId, conversation_id: args.cid });
        await handleAgentResult(fallback, args.cid);
        return;
      }
      const reader = body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      try {
        // Bounded loop: each iteration consumes one chunk from the server. The server
        // terminates with a `done` event (or `error`), and we break on stream EOF.
        for (let guard = 0; guard < 100_000; guard += 1) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const events = sliceEvents(buf);
          buf = events.remainder;
          const outcome = await applyEvents({
            events: events.events,
            cid: args.cid,
            ensureStreamBubble,
            streamRef: () => streamText,
            setStream: (next) => { streamText = next; },
          });
          if (outcome === "terminal") return;
          if (outcome === "fallback") {
            // Tools-needed/approval — discard any partial stream bubble and replay on
            // the narrated tool path (not interruptible, so drop the Stop affordance).
            stopper = null;
            streamEntryId = null;
            if (streamId) log = log.filter((e) => e.id !== streamId);
            await runToolTurn(args.messages, args.cid);
            return;
          }
        }
      } finally {
        // Release the underlying connection regardless of how we leave the loop.
        try { reader.releaseLock(); } catch { /* already released */ }
      }
    } catch (err) {
      // Stop clicked: keep whatever streamed, mark it, and persist it through the
      // normal path so a reload shows exactly what the user saw. Nothing streamed
      // yet -> no bubble exists and nothing is persisted. Real errors still throw
      // to send()/regenerate()'s red-bubble handler.
      if (!isAbort(err) && !(err instanceof ApiError) && !streamText) {
        // The streamed BODY broke before a single token landed — a transport drop, not
        // a server error (those arrive as ApiError). The turn itself is fine: deliver it
        // whole on the non-streaming endpoint rather than showing the user a red
        // "couldn't reach SmartBrain" for an answer the backend produced happily.
        stopper = null;
        streamEntryId = null;
        if (streamId) log = log.filter((e) => e.id !== streamId);
        const whole = await api.agentTurn({ messages: args.messages, model: modelId, conversation_id: args.cid });
        await handleAgentResult(whole, args.cid);
        return;
      }
      if (!isAbort(err)) throw err;
      if (streamId && streamText) {
        const stoppedText = `${streamText} (stopped)`;
        const target = log.find((x) => x.id === streamId);
        if (target) target.content = stoppedText;
        await api.addMessage(args.cid, "assistant", stoppedText);
      }
    } finally {
      stopper = null;
      streamEntryId = null;
    }
  }

  // An aborted fetch/read rejects with a DOMException named "AbortError" — that's the
  // user's Stop click, never something to paint as an error.
  function isAbort(err: unknown): boolean {
    return err instanceof DOMException && err.name === "AbortError";
  }

  // The tools path with a live narrative: run the WHOLE loop on the events endpoint,
  // painting "Searching the web…" lines as they happen. The terminal frame is exactly
  // an AgentResult and flows through handleAgentResult like the JSON endpoint's reply.
  async function runToolTurn(messages: ChatMessage[], cid: string): Promise<void> {
    activity = [];
    const primed = primedToken;
    primedToken = null;  // one-time: a retry must ask the model afresh
    try {
      const res = await api.agentTurnEvents({ messages, model: modelId, conversation_id: cid, primed });
      const body = res.body;
      if (!body) {
        // No streamable body — the silent JSON path still answers.
        const fallback = await api.agentTurn({ messages, model: modelId, conversation_id: cid });
        await handleAgentResult(fallback, cid);
        return;
      }
      const reader = body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      try {
        // Bounded loop: one chunk per iteration; the server ends with final/error.
        for (let guard = 0; guard < 100_000; guard += 1) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const sliced = sliceEvents(buf);
          buf = sliced.remainder;
          for (const ev of sliced.events) {
            if (ev.event === "tool") {
              const data = JSON.parse(ev.data) as { kind?: string; state: string; tool?: string; detail?: string; ok?: boolean };
              if (data.kind === "phase" && data.state === "answering") {
                // The last long call (writing the answer from everything gathered) is
                // visible too — otherwise the tail of a big turn looks hung.
                activity.push({ tool: "__answering", detail: "", done: false, ok: true });
              } else if (data.state === "start") {
                activity.push({ tool: data.tool ?? "", detail: data.detail || "", done: false, ok: true });
              } else {
                const open = [...activity].reverse().find((a) => a.tool === data.tool && !a.done);
                if (open) { open.done = true; open.ok = data.ok !== false; }
              }
            } else if (ev.event === "final") {
              await handleAgentResult(JSON.parse(ev.data) as AgentResult, cid);
              return;
            } else if (ev.event === "error") {
              const detail = (JSON.parse(ev.data) as { detail?: string }).detail;
              throw new ApiError(502, detail || "turn failed");
            }
          }
        }
        throw new ApiError(502, "turn stream ended unexpectedly");
      } finally {
        try { reader.releaseLock(); } catch { /* already released */ }
      }
    } finally {
      activity = [];
    }
  }

  // Split a buffer into complete SSE events (separated by a blank line). Returns the
  // parsed events and the trailing remainder that's still mid-frame.
  function sliceEvents(buf: string): { events: { event: string; data: string }[]; remainder: string } {
    console.assert(typeof buf === "string", "sliceEvents needs a string buffer");
    const events: { event: string; data: string }[] = [];
    let rest = buf;
    // Bounded: at most one event per ~16 bytes of buffer — far below this hard cap.
    for (let i = 0; i < 10_000; i += 1) {
      const sep = rest.indexOf("\n\n");
      if (sep === -1) break;
      const frame = rest.slice(0, sep);
      rest = rest.slice(sep + 2);
      const parsed = parseFrame(frame);
      if (parsed) events.push(parsed);
    }
    console.assert(typeof rest === "string", "remainder must be a string");
    return { events, remainder: rest };
  }

  // Parse one SSE frame: lines of `event: <name>` / `data: <payload>` (data may repeat).
  function parseFrame(frame: string): { event: string; data: string } | null {
    console.assert(typeof frame === "string", "parseFrame needs a string frame");
    if (!frame) return null;
    let evt = "message";
    const dataLines: string[] = [];
    const lines = frame.split("\n");
    for (const raw of lines) {
      const line = raw.replace(/\r$/, "");
      if (!line || line.startsWith(":")) continue; // comment / keepalive
      if (line.startsWith("event:")) evt = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    return { event: evt, data: dataLines.join("\n") };
  }

  // Apply a batch of SSE events. Returns "terminal" when the stream finished
  // ("done" / "error" / "[DONE]"), "fallback" when the server asked us to switch
  // to the non-streaming path, or "continue" to keep reading.
  async function applyEvents(opts: {
    events: { event: string; data: string }[];
    cid: string;
    ensureStreamBubble: () => string;
    streamRef: () => string;
    setStream: (next: string) => void;
  }): Promise<"continue" | "terminal" | "fallback"> {
    console.assert(Array.isArray(opts.events), "applyEvents needs events");
    console.assert(typeof opts.cid === "string", "applyEvents needs a cid");
    for (const e of opts.events) {
      if (e.event === "delta") {
        const piece = readDelta(e.data);
        if (!piece) continue;
        const id = opts.ensureStreamBubble();
        const next = opts.streamRef() + piece;
        opts.setStream(next);
        const target = log.find((x) => x.id === id);
        if (target) target.content = next;
        if (autoSpeak) speaker.feed(piece); // complete sentences speak as they stream
      } else if (e.event === "done") {
        await finalizeStream({ data: e.data, cid: opts.cid, streamText: opts.streamRef(), ensureStreamBubble: opts.ensureStreamBubble });
        return "terminal";
      } else if (e.event === "meta") {
        // Optimizer guidance announcement for this streamed answer (chip on finalize).
        try { liveGuidance = (JSON.parse(e.data) as { guidance?: Entry["guidance"] }).guidance ?? null; }
        catch { liveGuidance = null; }
      } else if (e.event === "pending" || e.event === "tools") {
        // The server may have parked the first model response for us; passing its token to
        // the tool turn saves re-asking the model the same 4,000-token question.
        try { primedToken = (JSON.parse(e.data) as { primed?: string }).primed ?? null; }
        catch { primedToken = null; }
        return "fallback";
      } else if (e.event === "error") {
        const msg = describeStreamError(e.data);
        log.push({ id: nextEntryId("err"), role: "assistant", content: msg, err: true });
        return "terminal";
      } else if (e.data === "[DONE]") {
        return "terminal";
      }
    }
    return "continue";
  }

  function readDelta(data: string): string {
    console.assert(typeof data === "string", "readDelta needs a string");
    if (!data) return "";
    try {
      const obj = JSON.parse(data) as { delta?: string };
      return typeof obj.delta === "string" ? obj.delta : "";
    } catch {
      return "";
    }
  }

  function describeStreamError(data: string): string {
    console.assert(typeof data === "string", "describeStreamError needs a string");
    try {
      const obj = JSON.parse(data) as { detail?: string; message?: string };
      const detail = obj.detail || obj.message;
      if (detail) return detail;
    } catch {
      /* not JSON — fall through */
    }
    return "Something went wrong on my end — please try again.";
  }

  // Persist + clean up after a successful stream. The server's terminal `done` event
  // carries the canonical message + conversation_id; we trust those over what we
  // accumulated locally if they differ.
  async function finalizeStream(opts: {
    data: string;
    cid: string;
    streamText: string;
    ensureStreamBubble: () => string;
  }): Promise<void> {
    console.assert(typeof opts.cid === "string" && opts.cid.length > 0, "finalizeStream needs a cid");
    console.assert(typeof opts.streamText === "string", "finalizeStream needs streamText");
    refreshPending(); // a streamed turn can still flip pending/remembered state
    let finalText = opts.streamText;
    try {
      const obj = JSON.parse(opts.data) as { message?: string; degraded?: boolean };
      if (typeof obj.message === "string" && obj.message.length > 0) finalText = obj.message;
      if (obj.degraded) modelNotice = DEGRADED_NOTICE;
    } catch {
      /* no payload / non-JSON — keep the streamed text */
    }
    if (!finalText) finalText = "I didn't get a response — try again.";
    const id = opts.ensureStreamBubble();
    const target = log.find((x) => x.id === id);
    if (target) {
      target.content = finalText;
      if (liveGuidance) target.guidance = liveGuidance; // transparency chip on the live bubble
    }
    liveGuidance = null;
    if (autoSpeak) speaker.flush(); // the unfinished last sentence
    await api.addMessage(opts.cid, "assistant", finalText);
  }

  async function handleAgentResult(res: AgentResult, cid: string) {
    refreshPending(); // update the Activity badge (a turn may have parked/cleared approvals)
    if (res.status === "awaiting_approval") {
      pendingTurnId = res.turn_id ?? null;
      // The blocked actions render as inline approval cards above the composer.
      await loadPendingActions(cid);
    } else {
      pendingTurnId = null;
      pendingActions = [];
      if (res.degraded) modelNotice = DEGRADED_NOTICE;
      const reply = res.message || "I didn't get a response — try again.";
      // Citations came from the turn's TOOL RESULTS (server-side, deterministic) — keep
      // them on the live entry and persist them with the message so a reload shows the
      // same chips.
      const sources = res.sources?.length ? res.sources : undefined;
      log.push({ id: nextEntryId("asst"), role: "assistant", content: reply, sources,
                 guidance: res.guidance });
      if (autoSpeak) speaker.say(reply); // non-streamed path: speak the whole answer
      liveGuidance = null; // the result's own field wins over any stream meta
      await api.addMessage(cid, "assistant", reply, sources);
    }
  }

  // Same rule as Knowledge's locator(): name the section by what it IS in this format —
  // a deck has slides and a spreadsheet has sheets, so "p.3" would miscall a slide.
  function locator(s: Source): string {
    return s.page_label && s.page_label !== "page" ? `${s.page_label} ${s.page}` : `p.${s.page}`;
  }

  // A chip opens the cited document in Knowledge AT the cited passage (offset); a
  // read/summary citation has no offset and opens the document at the top.
  function openSource(s: Source): void {
    console.assert(typeof s.id === "string" && s.id.length > 0, "openSource needs a document id");
    goto(`/knowledge?doc=${encodeURIComponent(s.id)}&offset=${s.offset ?? ""}`);
  }

  async function resume() {
    if (!pendingTurnId || busy || chatSession.currentId === null) return;
    busy = true;
    error = "";
    resumeNotice = "";
    try {
      const res = await api.agentResume(pendingTurnId);
      if (res.status === "awaiting_approval") {
        // Resumed into another blocked action (or one is still unresolved) — the cards say so.
        resumeNotice = "Still waiting on your approval — approve or deny the action above.";
      }
      await handleAgentResult(res, chatSession.currentId);
      await loadConversations();
    } catch (err) {
      const text = describeError(err);
      if (text) log.push({ id: nextEntryId("err"), role: "assistant", content: text, err: true });
    } finally {
      busy = false;
    }
  }

  function onKey(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }
</script>

{#if account.status?.unlocked}
  <h1>Chat</h1>

  <div class="chat-toolbar">
    <button class="secondary" disabled={busy} onclick={newChat}>+ New chat</button>
    <button class="secondary" disabled={busy} title="Reload chats and this thread (syncs changes made on your other device)" onclick={refreshChat}>
      <Icon name="refresh" size={14} /> Refresh
    </button>
    {#if conversations.length}
      {#if renaming && chatSession.currentId}
        <!-- Inline rename replaces the picker (Knowledge's idiom) so the title being
             edited and the list can't disagree mid-edit. Enter submits. -->
        <input
          style="flex:1; max-width:24rem"
          aria-label="Chat title"
          bind:value={renameValue}
          onkeydown={(e) => e.key === "Enter" && saveRename()}
        />
        <button disabled={busy || !renameValue.trim()} onclick={saveRename}>Save</button>
        <button class="secondary" onclick={cancelRename}>Cancel</button>
      {:else}
        <select
          aria-label="Saved chats"
          disabled={busy}
          value={chatSession.currentId ?? ""}
          onchange={(e) => select((e.currentTarget as HTMLSelectElement).value)}
        >
          <option value="" disabled>Saved chats…</option>
          {#each conversations as c (c.id)}<option value={c.id}>{c.title} ({startDate(c.created_at)})</option>{/each}
        </select>
        {#if convosHasMore}
          <button class="secondary" disabled={busy} onclick={loadOlderConversations}>Load older</button>
        {/if}
        {#if chatSession.currentId}
          <button class="secondary" disabled={busy} title="Rename this chat" onclick={startRename}>Rename</button>
          <button class="secondary" disabled={busy} title="Move this chat to the Trash (restorable for 30 days in Settings → Account)" onclick={() => remove(chatSession.currentId!)}>Delete</button>
        {/if}
        <button class="del" disabled={busy} title="Move ALL chats to the Trash (restorable for 30 days in Settings → Account & Data)" onclick={removeAll}>Delete all…</button>
      {/if}
    {/if}
    <span class="grow"></span>
    <span class="field">
      <label for="provider">Provider</label>
      <select id="provider" bind:value={provider} onchange={onProvider}>
        {#each providers as p (p)}<option value={p}>{p}</option>{/each}
      </select>
    </span>
    <span class="field">
      <label for="model">Model</label>
      <select id="model" bind:value={modelId} disabled={providerModels.length === 0}>
        {#if providerModels.length === 0}
          <option value="" disabled>No models for this provider</option>
        {:else}
          {#each providerModels as m (m.id)}<option value={m.id}>{m.name}</option>{/each}
        {/if}
      </select>
    </span>
  </div>

  {#if modelsDegraded && models.length > 0}
    <p class="muted" style="margin:0.25rem 0 0; font-size:0.85rem">
      Model list is degraded — the gateway catalog isn&rsquo;t responding, so only local models are
      shown. A stale server entry under <a href="/settings/models">Settings → Local models</a> is the
      usual cause.
    </p>
  {/if}

  {#if models.length === 0 && (detected || probed)}
    <div class="card">
      {#if detected}
        {@const name = detected.provider === "ollama" ? "Ollama" : "MLX"}
        <strong>Found {name} running on this machine</strong>
        <p class="muted" style="margin:0.4rem 0 0">
          Connect it to start chatting — your prompts stay fully on your machine.
        </p>
        <p style="margin-top:0.75rem; display:flex; gap:0.5rem">
          <button disabled={connecting} onclick={connectLocal}>{connecting ? "Connecting…" : `Connect ${name}`}</button>
          <button class="secondary" disabled={connecting} onclick={loadModels}>Refresh</button>
        </p>
      {:else if modelsError}
        <strong>Couldn&rsquo;t load the model list</strong>
        <p class="muted" style="margin:0.4rem 0 0">
          {modelsError} Your models are likely fine — check
          <a href="/settings/models">Settings → Local models</a> for a stale or unreachable server entry.
        </p>
        <p style="margin-top:0.75rem">
          <button class="secondary" disabled={busy} onclick={loadModels}>Retry</button>
        </p>
      {:else}
        <strong>No models available yet</strong>
        <p class="muted" style="margin:0.4rem 0 0">
          Add a cloud provider key under <a href="/settings/providers">Settings → Cloud providers</a>, or a
          local model under <a href="/settings/models">Settings → Local models</a>. Just added one? It can
          take a moment to appear. <a href="/help#models">Learn more</a>.
        </p>
        <p style="margin-top:0.75rem">
          <button class="secondary" disabled={busy} onclick={loadModels}>Refresh models</button>
        </p>
      {/if}
    </div>
  {/if}

  <div class="chat-log">
    {#if msgHasMore}
      <p class="loadmore">
        <button class="link" disabled={busy} onclick={loadOlderMessages}>Load older messages</button>
      </p>
    {/if}
    {#each log as entry (entry.id)}
      {#if entry.role === "assistant" && !entry.err}
        <!-- Full-width message row (no bubble): role label + rendered markdown. -->
        <div class="msg">
          <div class="who">
            SmartBrain
            {#if stopper && entry.id === streamEntryId}<span class="state">· streaming<span class="caret"></span></span>{/if}
          </div>
          <div class="body"><Markdown content={entry.content} /></div>
          {#if entry.sources?.length}
            <!-- Citation chips: where the answer's knowledge came from, straight from the
                 tool results. Click = open the document at the passage. -->
            <div class="cites">
              {#each entry.sources as s (`${s.id}:${s.offset ?? ""}`)}
                <Chip icon="file" title="Open in Knowledge at this passage" onclick={() => openSource(s)}>
                  {s.source || s.title || "document"}{#if s.page != null}&nbsp;·&nbsp;{locator(s)}{/if}
                </Chip>
              {/each}
            </div>
          {/if}
          {#if entry.guidance}
            <!-- Optimizer transparency: a learned, measured-on-trial guidance shaped this
                 answer. Hover shows the directive itself; the ledger keeps the history. -->
            <div class="guided" title={entry.guidance.directive}>
              guided · {entry.guidance.request_type.replace("_", " ")}
            </div>
          {/if}
          <!-- Quiet per-answer actions. Copy grabs the raw markdown (not rendered HTML);
               Regenerate only exists on the thread's final answer. -->
          <div class="msg-actions">
            <button class="msg-action" title="Copy the message text" onclick={() => copyMessage(entry)}>
              {copiedId === entry.id ? "Copied ✓" : "Copy"}
            </button>
            {#if entry.id === lastAnswerId && !busy}
              <button class="msg-action" title="Ask again — get a fresh answer to your last message" onclick={regenerate}>
                Regenerate
              </button>
            {/if}
            {#if speechPossible && !entry.schedule}
              <button class="msg-action" title={listeningId === entry.id ? "Stop reading" : "Read this answer aloud"} onclick={() => listen(entry)}>
                {listeningId === entry.id ? "Stop" : "Listen"}
              </button>
            {/if}
          </div>
        </div>
      {:else if entry.err}
        <div class="msg">
          <div class="who">SmartBrain <span class="state">· error</span></div>
          <div class="errline"><Icon name="warn" size={15} /> {entry.content}</div>
        </div>
      {:else}
        <div class="msg user">
          <div class="who">
            You
            <button class="retry" title="Send this message again" disabled={busy || !modelId} onclick={() => retryMessage(entry)}>
              <Icon name="refresh" size={11} /> Retry
            </button>
          </div>
          <div class="body">{entry.content}</div>
        </div>
      {/if}
    {/each}
    {#if busy && !streamEntryId}
      <!-- The wait for real content, streamed or not: an alive "thinking" signal — with a
           live narrative of tool activity when the events endpoint is doing the work. The
           streamed path used to hide this the moment the request started (stopper set),
           leaving the desktop silent until the first token — many seconds on a big model. -->
      <div class="msg">
        <div class="who">SmartBrain <span class="state">· {activity.length ? "working" : "thinking"}</span></div>
        <div class="body">
          {#if activity.length}
            <div class="activity">
              {#each activity as a, i (i)}
                <div class="actline" class:done={a.done}>
                  <Icon name={TOOL_ICONS[a.tool] ?? "activity"} size={14} />
                  <span>{TOOL_LABELS[a.tool] ?? a.tool}{a.detail ? ` — ${a.detail}` : ""}</span>
                  {#if a.done}<Icon name={a.ok ? "check" : "warn"} size={14} />{/if}
                </div>
              {/each}
              <span class="thinking"><i></i><i></i><i></i></span>
            </div>
          {:else}
            <span class="thinking"><i></i><i></i><i></i></span>
          {/if}
        </div>
      </div>
    {/if}
    {#if log.length === 0}
      <EmptyState
        icon="chat"
        title="Ask your assistant"
        body="It can search your knowledge, manage tasks, and act on your behalf — anything that changes data waits for your approval."
      >
        {#each STARTERS as s (s)}
          <Chip onclick={() => useStarter(s)}>{s}</Chip>
        {/each}
      </EmptyState>
    {/if}
    <div class="log-end" bind:this={logEnd} aria-hidden="true"></div>
  </div>

  {#if log.length > 0 && (showTop || !atBottom)}
    <div class="jump-row">
      {#if showTop}
        <button class="jump" onclick={jumpToTop}><Icon name="arrow-up" size={14} /> Top</button>
      {/if}
      {#if !atBottom}
        <button class="jump" onclick={jumpToLatest}><Icon name="arrow-down" size={14} /> Latest</button>
      {/if}
    </div>
  {/if}

  {#if pendingActions.length > 0}
    <!-- The blocked actions themselves, right where the conversation paused — approve,
         always-allow, or deny without leaving chat. Resolving the last one resumes the
         turn automatically. Activity still lists everything. -->
    {#each pendingActions as p (p.id)}
      <ActionCard icon={iconForTool(p.tool)} title={p.tool} tier={p.tier === "irreversible" ? "irreversible" : "reviewed"} scope={fmtArgs(p.args)}>
        {#snippet actions()}
          {#if p.tier === "reviewed" && p.remember_mode === "tool"}
            <button class="ghost" disabled={approvalBusy !== "" || busy} title="Approve and stop asking for this tool" onclick={() => resolveApproval(p, "approve", true)}>Always allow</button>
          {:else if p.tier === "reviewed" && p.remember_mode === "site" && p.remember_host}
            <button class="ghost allow-site" disabled={approvalBusy !== "" || busy} title={`Approve and stop asking for ${p.remember_host}`} onclick={() => resolveApproval(p, "approve", true)}>Always allow {p.remember_host}</button>
          {/if}
          <button class="secondary" disabled={approvalBusy !== "" || busy} onclick={() => resolveApproval(p, "deny")}>Deny</button>
          <button disabled={approvalBusy !== "" || busy} onclick={() => resolveApproval(p, "approve")}>Approve</button>
        {/snippet}
      </ActionCard>
    {/each}
    {#if allReviewed}
      <p class="approve-all">
        <button disabled={approvalBusy !== "" || busy} onclick={approveAll}>
          {approvalBusy === "all" ? "Approving…" : `Approve all ${pendingActions.length}`}
        </button>
      </p>
    {/if}
    {#if resumeNotice}<p class="muted resume-notice">{resumeNotice}</p>{/if}
  {:else if pendingTurnId}
    <!-- Parked turn whose pending list couldn't load (transient) — keep the old fallback. -->
    <ActionCard icon="activity" title="The assistant is waiting for your approval" badge={false}>
      {#snippet actions()}
        <button class="secondary" onclick={() => goto("/activity")}>Open Activity</button>
        <button disabled={busy} onclick={resume}>Resume after approval</button>
      {/snippet}
    </ActionCard>
    {#if resumeNotice}<p class="muted resume-notice">{resumeNotice}</p>{/if}
  {/if}

  <div class="composer">
    <div class="inner">
      {#if voiceInfo?.stt_available}
        {#if micUsable}
          <button
            class="voice mic"
            class:recording={recState === "recording"}
            disabled={recState === "transcribing" || busy}
            title={recState === "recording" ? "Stop and transcribe" : "Dictate (push to talk)"}
            aria-label={recState === "recording" ? "Stop and transcribe" : "Dictate"}
            onclick={toggleMic}
          >
            <Icon name={recState === "transcribing" ? "clock" : "mic"} size={16} />
          </button>
        {:else if voiceInfo.local?.phase === "error"}
          <button
            class="voice mic voice-error"
            title={`Voice model download failed (${voiceInfo.local.error}) — tap to retry`}
            aria-label="Retry the voice model download"
            onclick={retryVoiceModel}
          >
            <Icon name="refresh" size={16} />
          </button>
        {:else}
          <!-- Preparing: live percent IN the button, spinner while the engine loads.
               Disabled but never mysterious — the title says exactly what's happening. -->
          <button
            class="voice mic preparing"
            disabled
            title={voiceInfo.local?.phase === "loading"
              ? "Voice is almost ready — loading the engine"
              : `Preparing voice — one-time download (${voiceInfo.local?.pct ?? 0}%)`}
            aria-label="Preparing voice"
          >
            {#if voiceInfo.local?.phase === "loading"}
              <Icon name="clock" size={14} />
            {:else}
              <span class="mic-pct">{voiceInfo.local?.pct ?? 0}%</span>
            {/if}
          </button>
        {/if}
      {/if}
      {#if speechPossible}
        <button
          class="voice autospeak"
          class:active={autoSpeak}
          title={autoSpeak ? "Stop speaking replies aloud" : "Speak replies aloud"}
          aria-label={autoSpeak ? "Stop speaking replies aloud" : "Speak replies aloud"}
          onclick={toggleAutoSpeak}
        >
          <Icon name="speaker" size={16} />
        </button>
      {/if}
      <textarea
        bind:value={input}
        onkeydown={onKey}
        placeholder="Message SmartBrain…"
        aria-label="Message"
      ></textarea>
      {#if stopper}
        <!-- A streamed turn is in flight: Send becomes Stop. Aborting keeps + persists the
             partial answer (see streamTurn); non-streamed turns keep the plain disabled Send. -->
        <button class="stop" title="Stop generating" aria-label="Stop generating" onclick={() => { void api.feedback("stop", chatSession.currentId); speaker.stop(); stopper?.abort(); }}>
          <Icon name="stop" />
        </button>
      {:else}
        <button
          class="send"
          disabled={busy || !input.trim() || !modelId}
          title={!modelId ? "Select a model first" : "Send"}
          aria-label="Send"
          onclick={send}
        >
          <Icon name="send" />
        </button>
      {/if}
    </div>
    <p class="hint">⏎ send · ⇧⏎ newline — replies stream in; Stop is always here while they do</p>
  </div>
  {#if modelNotice}<p class="notice">{modelNotice}</p>{/if}
  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <Spinner block />
{/if}

<style>
  /* Citation chips under an assistant bubble — the same pill idiom as the Knowledge
     page's .cite, so "where this came from" looks identical in both places. The row is
     a flex sibling of the bubble in .chat-log, tucked up against it (the log's gap
     would otherwise read as a separate message). */
  .cites {
    align-self: flex-start;
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-top: -0.45rem;
    max-width: min(46rem, 85%); /* track the bubble width so chips never outdent it */
  }
  /* Optimizer transparency chip — same tucked-under placement as .cites, quieter still:
     it's a footnote ("this answer was steered"), not a control. Hover reveals the text. */
  .guided {
    align-self: flex-start;
    font-size: 0.72rem;
    color: var(--muted);
    opacity: 0.75;
    margin-top: -0.35rem;
    cursor: help;
  }
  /* Per-answer actions (Copy / Regenerate) — same tucked-under-the-bubble placement as
     .cites. Always visible (hover-only reveals fail on touch) but quiet: bare muted text. */
  .msg-actions {
    align-self: flex-start;
    display: flex;
    gap: 0.75rem;
    margin-top: -0.45rem;
  }
  .msg-action {
    background: transparent;
    border: 0;
    padding: 0;
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--muted);
    cursor: pointer;
  }
  .msg-action:hover {
    color: var(--text);
  }

  /* Retry pill beside "You" — deliberately COLORED (accent) so it reads as an action
     among the muted meta labels; the only colored control in the message rows. */
  .retry {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    border: 0;
    background: var(--accent-tint);
    color: var(--accent-strong);
    border-radius: var(--r-full);
    padding: 2px 10px;
    min-height: 0;
    font-size: 0.72rem;
    font-weight: 600;
    cursor: pointer;
  }
  .retry:hover:not(:disabled) {
    background: var(--accent-strong);
    color: #fff;
  }
  .retry:disabled {
    opacity: 0.45;
    cursor: default;
  }

  /* Composer voice buttons: quiet circles that match the field, mic goes danger-red
     while recording (the one state that must be unmissable), auto-speak fills accent
     while on. */
  .voice {
    align-self: flex-end;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    /* The global button rule's padding (10px 16px) inside a fixed 34px width left a
       2px content box — the icon rendered as a sliver. Zero it and size like Send. */
    width: 40px;
    height: 40px;
    min-width: 40px;
    min-height: 40px;
    padding: 0;
    flex: none;
    border: 0;
    border-radius: var(--r-full);
    background: transparent;
    color: var(--muted);
    cursor: pointer;
  }
  .voice:hover:not(:disabled) {
    color: var(--text);
    background: var(--accent-tint);
  }
  .voice.mic.recording {
    background: var(--danger);
    color: #fff;
    animation: mic-pulse 1.6s ease-in-out infinite;
  }
  .voice.autospeak.active {
    background: var(--accent-strong);
    color: #fff;
  }
  .voice.preparing {
    background: var(--accent-tint);
    color: var(--accent-strong);
  }
  .mic-pct {
    font-size: 0.62rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .voice.voice-error {
    background: var(--danger);
    color: #fff;
  }
  @keyframes mic-pulse {
    50% { opacity: 0.65; }
  }
  @media (prefers-reduced-motion: reduce) {
    .voice.mic.recording { animation: none; }
  }

  /* Same ellipsis rule as Activity's card: a long host can't push the card wide. */
  .allow-site {
    max-width: 16rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .approve-all {
    display: flex;
    justify-content: flex-end;
    margin: 0 0 var(--s-3);
  }
</style>
