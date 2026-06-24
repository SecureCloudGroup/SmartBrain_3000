<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { account } from "$lib/account.svelte";
  import { chatSession } from "$lib/chat.svelte";
  import { resumeChat } from "$lib/chat-resume";
  import { refreshPending } from "$lib/pending.svelte";
  import { api, ApiError, type AgentResult, type ChatMessage, type Conversation, type DiscoveredModel } from "$lib/api";
  import { describeError } from "$lib/errors";
  import { remote } from "$lib/remote/connection.svelte";

  // Entry carries a stable id so {#each} can key on it (U16) — re-renders no longer
  // jump when a streaming assistant message mutates in place.
  type Entry = ChatMessage & { id: string; err?: boolean };

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

  // Stable client-side ids for entries we just appended (server-issued ids are used
  // for messages loaded from history). Monotonic counter; bounded by user actions.
  let entrySeq = 0;
  const nextEntryId = (kind: string): string => {
    entrySeq += 1;
    return `c-${kind}-${entrySeq}`;
  };

  // Starter prompts shown when the chat log is empty (U6). Kept short + concrete so
  // a clicked chip drops straight into the composer.
  const STARTERS = [
    "What can you do?",
    "Summarize my newest email.",
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
      log = msgs.map((m) => ({ id: m.id, role: m.role, content: m.content }));
      // resumeChat returns the newest page's messages (server default); if there are older
      // ones, re-fetch through getConversation to capture next_cursor/has_more.
      if (chatSession.currentId) await refreshOpenCursor(chatSession.currentId);
    } catch (err) {
      error = describeError(err);
    }
  });

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
    try {
      models = (await api.listModels()).models.filter((x) => x.chat); // embeddings/image can't chat
    } catch {
      models = [];
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
      const older = page.messages.map((m) => ({ id: m.id, role: m.role, content: m.content }));
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
    try {
      const convo = await api.getConversation(id);
      chatSession.currentId = id;
      log = convo.messages.map((m) => ({ id: m.id, role: m.role, content: m.content }));
      msgCursor = convo.next_cursor ?? null;
      msgHasMore = !!convo.has_more;
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
    error = "";
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
    const out = log.filter((e) => !e.err).map(({ role, content }) => ({ role, content }));
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

      const messages = buildTranscript();
      // Desktop/local -> stream tokens. Remote (WebRTC relay buffers SSE) -> non-stream.
      if (remote.status === "idle") {
        await streamTurn({ messages, cid });
      } else {
        const res = await api.agentTurn({ messages, model: modelId, conversation_id: cid });
        await handleAgentResult(res, cid);
      }
      await loadConversations(); // refresh recency/order
    } catch (err) {
      const text2 = describeError(err);
      if (text2) log.push({ id: nextEntryId("err"), role: "assistant", content: text2, err: true });
    } finally {
      busy = false;
    }
  }

  // Stream a single agent turn over SSE. On `delta` we mutate the open assistant
  // bubble in place; `done` finalizes + persists; `pending`/`tools` falls back to
  // the non-streaming endpoint so the existing approval/Resume flow still works.
  async function streamTurn(args: { messages: ChatMessage[]; cid: string }): Promise<void> {
    console.assert(Array.isArray(args.messages) && args.messages.length > 0, "streamTurn needs messages");
    console.assert(typeof args.cid === "string" && args.cid.length > 0, "streamTurn needs a conversation id");
    const res = await api.agentTurnStream({
      messages: args.messages,
      model: modelId,
      conversation_id: args.cid,
    });
    const body = res.body;
    if (!body) {
      // No streamable body — fall back so the user still gets an answer.
      const fallback = await api.agentTurn({ messages: args.messages, model: modelId, conversation_id: args.cid });
      await handleAgentResult(fallback, args.cid);
      return;
    }
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
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
          // Tools-needed/approval — discard any partial stream bubble and replay non-streaming.
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
      log.push({ id: nextEntryId("asst"), role: "assistant", content: reply });
      await api.addMessage(cid, "assistant", reply);
    }
  }

  async function resume() {
    if (!pendingTurnId || busy || chatSession.currentId === null) return;
    busy = true;
    error = "";
    try {
      const res = await api.agentResume(pendingTurnId);
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
        <button class="secondary" disabled={busy} title="Delete this chat" onclick={() => remove(chatSession.currentId!)}>Delete</button>
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

  {#if models.length === 0}
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
      <p class="row" style="justify-content:center">
        <button class="link" disabled={busy} onclick={loadOlderMessages}>Load older messages</button>
      </p>
    {/if}
    {#each log as entry (entry.id)}
      <div class="bubble {entry.err ? 'err' : entry.role}">{entry.content}</div>
    {/each}
    {#if busy}<div class="bubble assistant muted">…</div>{/if}
    {#if log.length === 0}
      <div class="starters">
        <p class="muted" style="margin:0 0 0.5rem">I can use tools — some actions ask for your approval first.</p>
        <div class="row" style="flex-wrap:wrap; gap:0.5rem">
          {#each STARTERS as s (s)}
            <button type="button" class="secondary starter" disabled={busy} onclick={() => useStarter(s)}>{s}</button>
          {/each}
        </div>
      </div>
    {/if}
  </div>

  {#if pendingTurnId}
    <div class="approval-banner">
      <strong>Approval needed:</strong>
      <span class="muted">review the request in Activity, then resume.</span>
      <span class="grow"></span>
      <a class="approval-link" href="/activity">Open Activity</a>
      <button class="secondary" disabled={busy} onclick={resume}>Resume after approval</button>
    </div>
  {/if}

  <div class="composer">
    <textarea
      bind:value={input}
      onkeydown={onKey}
      placeholder="Message…  (Enter to send, Shift+Enter for a newline)"
    ></textarea>
    <button disabled={busy || !input.trim() || !modelId} title={!modelId ? "Select a model first" : ""} onclick={send}>Send</button>
  </div>
  {#if modelNotice}<p class="notice">{modelNotice}</p>{/if}
  {#if error}<p class="error">{error}</p>{/if}
{:else}
  <p class="muted">Loading&hellip;</p>
{/if}

<style>
  /* Starter prompt chips — only visible on an empty chat log (U6). Match the existing
     .secondary button look but a touch smaller so a row of three reads as suggestions. */
  .starter {
    padding: 0.4rem 0.7rem;
    font-size: 0.9rem;
    font-weight: 500;
  }

  /* Approval banner (U8): a distinct strip placed JUST above the composer so the label
     and the Resume action are adjacent — no more chat-bubble notice + far-away footer link. */
  .approval-banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.75rem;
    margin: 0.75rem 0 0.5rem;
    padding: 0.6rem 0.9rem;
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 8px;
    background: color-mix(in srgb, var(--accent) 8%, transparent);
  }
  .approval-link {
    color: var(--accent);
    text-decoration: none;
  }
  .approval-link:hover {
    text-decoration: underline;
  }
</style>
