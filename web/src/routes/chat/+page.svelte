<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { chatSession } from "$lib/chat.svelte";
  import { resumeChat } from "$lib/chat-resume";
  import { refreshPending } from "$lib/pending.svelte";
  import { api, ApiError, type AgentResult, type ChatMessage, type Conversation, type DiscoveredModel, type RecentScheduleRun, type Source } from "$lib/api";
  import { finalAssistantId, transcriptUpToLastUser } from "$lib/chat-log";
  import { describeError } from "$lib/errors";
  import Markdown from "$lib/Markdown.svelte";
  import { remote } from "$lib/remote/connection.svelte";
  import { scheduleUpdates } from "$lib/scheduleUpdates.svelte";
  import ActionCard from "$lib/components/ActionCard.svelte";
  import Chip from "$lib/components/Chip.svelte";
  import EmptyState from "$lib/components/EmptyState.svelte";
  import Icon from "$lib/components/Icon.svelte";
  import Spinner from "$lib/components/Spinner.svelte";

  // Entry carries a stable id so {#each} can key on it (U16) — re-renders no longer
  // jump when a streaming assistant message mutates in place. `schedule` marks a fired
  // scheduled-run update injected into the view (display-only; excluded from the transcript).
  // `sources` = the turn's deterministic citations (from tool results), rendered as chips.
  type Entry = ChatMessage & { id: string; err?: boolean; schedule?: boolean; sources?: Source[] };

  let conversations = $state<Conversation[]>([]);
  let convosCursor = $state<string | null>(null); // next-older page cursor for the saved list
  let convosHasMore = $state(false);
  let log = $state<Entry[]>([]);
  let msgCursor = $state<string | null>(null); // next-older page cursor for the open conversation
  let msgHasMore = $state(false);
  let input = $state("");
  let busy = $state(false);
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
  // seen clears the Chat nav badge. The durable copy always lives on the Schedules → Output tab.
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
  // The id of the assistant entry currently streaming (the newest one while a stopper exists).
  const streamingId = $derived(
    stopper ? [...log].reverse().find((e) => e.role === "assistant" && !e.err)?.id ?? null : null,
  );
  const lastLen = $derived(log.length ? log[log.length - 1].content.length : 0);
  $effect(() => {
    void lastLen; // track every streamed delta + new entries
    void log.length;
    if (atBottom) requestAnimationFrame(() => logEnd?.scrollIntoView({ block: "end" }));
  });
  function jumpToLatest() {
    logEnd?.scrollIntoView({ behavior: "smooth", block: "end" });
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
  const startDate = (iso: string) => new Date(iso.slice(0, 19).replace(" ", "T") + "Z").toLocaleDateString();

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
      await restorePendingBanner(chatSession.currentId); // re-show Resume if a parked turn survived a nav away
    } catch (err) {
      error = describeError(err);
    }
    await pullScheduleUpdates(); // surface anything that fired while away, right here in the chat
    updatesTimer = setInterval(pullScheduleUpdates, 25000); // keep new ones arriving live while viewing Chat
  });

  onDestroy(() => {
    if (updatesTimer) clearInterval(updatesTimer);
  });

  // Re-derive the approval banner from the server (pendingTurnId is component-local and is
  // lost when the user follows "Open Activity" and returns): if the open conversation has a
  // parked turn awaiting approval, restore its Resume affordance. Best-effort — the server's
  // pending list is the source of truth, so this also survives a full reload.
  async function restorePendingBanner(cid: string | null): Promise<void> {
    if (!cid) return;
    try {
      const { pending } = await api.listPending();
      pendingTurnId = pending.find((p) => p.conversation_id === cid && p.turn_id)?.turn_id ?? null;
    } catch {
      // a transient failure just leaves the banner absent (Activity still lists the approval)
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

  async function select(id: string) {
    error = "";
    pendingTurnId = null; // never carry a parked turn across a conversation switch
    resumeNotice = "";
    renaming = false; // a half-typed rename belongs to the conversation being left
    try {
      const convo = await api.getConversation(id);
      chatSession.currentId = id;
      log = convo.messages.map((m) => ({ id: m.id, role: m.role, content: m.content, sources: m.sources }));
      msgCursor = convo.next_cursor ?? null;
      msgHasMore = !!convo.has_more;
      await restorePendingBanner(id); // this conversation may have a parked turn awaiting approval
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
            // Tools-needed/approval — discard any partial stream bubble and replay
            // non-streaming (not interruptible, so drop the Stop affordance first).
            stopper = null;
            if (streamId) log = log.filter((e) => e.id !== streamId);
            const replay = await api.agentTurn({ messages: args.messages, model: modelId, conversation_id: args.cid });
            await handleAgentResult(replay, args.cid);
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
      if (!isAbort(err)) throw err;
      if (streamId && streamText) {
        const stoppedText = `${streamText} (stopped)`;
        const target = log.find((x) => x.id === streamId);
        if (target) target.content = stoppedText;
        await api.addMessage(args.cid, "assistant", stoppedText);
      }
    } finally {
      stopper = null;
    }
  }

  // An aborted fetch/read rejects with a DOMException named "AbortError" — that's the
  // user's Stop click, never something to paint as an error.
  function isAbort(err: unknown): boolean {
    return err instanceof DOMException && err.name === "AbortError";
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
      } else if (e.event === "done") {
        await finalizeStream({ data: e.data, cid: opts.cid, streamText: opts.streamRef(), ensureStreamBubble: opts.ensureStreamBubble });
        return "terminal";
      } else if (e.event === "pending" || e.event === "tools") {
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
    if (target) target.content = finalText;
    await api.addMessage(opts.cid, "assistant", finalText);
  }

  async function handleAgentResult(res: AgentResult, cid: string) {
    refreshPending(); // update the Activity badge (a turn may have parked/cleared approvals)
    if (res.status === "awaiting_approval") {
      pendingTurnId = res.turn_id ?? null;
      // U8: the banner near the composer announces the approval; we no longer push a
      // chat-bubble notice with a far-away footer link.
    } else {
      pendingTurnId = null;
      if (res.degraded) modelNotice = DEGRADED_NOTICE;
      const reply = res.message || "I didn't get a response — try again.";
      // Citations came from the turn's TOOL RESULTS (server-side, deterministic) — keep
      // them on the live entry and persist them with the message so a reload shows the
      // same chips.
      const sources = res.sources?.length ? res.sources : undefined;
      log.push({ id: nextEntryId("asst"), role: "assistant", content: reply, sources });
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
        // Clicked Resume before approving in Activity — say so, don't silently no-op.
        resumeNotice = "Still waiting on your approval — open Activity, approve the action, then Resume.";
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
          <button class="secondary" disabled={busy} title="Delete this chat" onclick={() => remove(chatSession.currentId!)}>Delete</button>
        {/if}
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
            {#if stopper && entry.id === streamingId}<span class="state">· streaming<span class="caret"></span></span>{/if}
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
          </div>
        </div>
      {:else if entry.err}
        <div class="msg">
          <div class="who">SmartBrain <span class="state">· error</span></div>
          <div class="errline"><Icon name="warn" size={15} /> {entry.content}</div>
        </div>
      {:else}
        <div class="msg user">
          <div class="who">You</div>
          <div class="body">{entry.content}</div>
        </div>
      {/if}
    {/each}
    {#if busy && !stopper}
      <!-- Non-streamed / pre-first-token wait: an alive "thinking" signal, not a bare ellipsis. -->
      <div class="msg">
        <div class="who">SmartBrain <span class="state">· thinking</span></div>
        <div class="body"><span class="thinking"><i></i><i></i><i></i></span></div>
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

  {#if !atBottom && log.length > 0}
    <button class="jump" onclick={jumpToLatest}><Icon name="arrow-down" size={14} /> Jump to latest</button>
  {/if}

  {#if pendingTurnId}
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
      <textarea
        bind:value={input}
        onkeydown={onKey}
        placeholder="Message SmartBrain…"
        aria-label="Message"
      ></textarea>
      {#if stopper}
        <!-- A streamed turn is in flight: Send becomes Stop. Aborting keeps + persists the
             partial answer (see streamTurn); non-streamed turns keep the plain disabled Send. -->
        <button class="stop" title="Stop generating" aria-label="Stop generating" onclick={() => stopper?.abort()}>
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


</style>
