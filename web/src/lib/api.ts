// Thin same-origin client for the SmartBrain backend. Every call returns parsed
// JSON or throws ApiError(status, detail) so callers can branch on status. The
// backend never returns secret values — only names. A 423 (vault locked) is
// handled centrally here (redirect to /unlock) so no page can forget it.

import { goto } from "$app/navigation";

import { remoteReady } from "$lib/remote/sw-bridge";

// The vault can relock mid-session (an update restart, a Lock from another device).
// Client truth must flip the INSTANT the first 423 arrives: an open tab that keeps
// believing "unlocked" re-fires its guarded effects on every redirect and stampedes
// the server (observed live at ~37 req/s: fetch → 423 → goto → navigation → effect →
// fetch, so fast the unlock page never finished loading). The account store registers
// itself here at startup — a static import of it would be circular.
let onLocked: (() => void) | null = null;
export function registerLockedHandler(fn: () => void): void {
  onLocked = fn;
}

// EVERY 423, from every fetch path, goes through here — req() and both streaming
// endpoints alike. An audit found the streaming endpoints doing their own bare
// goto("/unlock") without the state flip: bounded today, but exactly the bug class
// that stampeded a tab at 37 req/s, waiting to be copy-pasted into the next
// raw-fetch endpoint. One handler, no copies to drift.
function handleLocked(): void {
  onLocked?.(); // client state flips to locked BEFORE any effect can re-fire a fetch
  const onUnlockPage =
    typeof window !== "undefined" && window.location.pathname.startsWith("/unlock");
  if (!onUnlockPage) {
    goto("/unlock"); // vault locked mid-session — bounce to unlock (idempotent)
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  await remoteReady; // off-LAN, ensure the /api->WebRTC fetch override is installed first
  const res = await fetch(path, {
    ...opts,
    headers: { "content-type": "application/json", ...(opts.headers ?? {}) },
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const detail = (data && (data as { detail?: string }).detail) || `request failed (${res.status})`;
    if (res.status === 423) {
      handleLocked();
    }
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export interface AccountStatus {
  initialized: boolean;
  unlocked: boolean;
  has_recovery: boolean;
}

export interface EmergencyKit {
  recovery_key: string;
  emergency_kit: string;
}

export interface ModelProvider {
  configured: boolean;
  reachable: boolean;
  models: string[];
  url: string; // configured server URL as stored (any this-machine host form); "" if unset
  detected: boolean; // not configured, but a server answered on the default port — offer 1-tap connect
  default_url: string; // the default host URL the server was detected on
}

export interface LocalModels {
  ollama: ModelProvider;
  mlx: ModelProvider;
  mlxe: ModelProvider; // the dedicated MLX EMBEDDINGS server (tools/mlx_embed_server)
}

export interface DiscoveredModel {
  id: string; // "provider/model"
  name: string;
  provider: string; // gateway provider name (e.g. "gemini", "ollama")
  context_length: number | null;
  pricing: { prompt: number; completion: number } | null; // per-token; null = local/free
  chat: boolean; // chat-capable (vs embedding/image/audio)
  embed: boolean; // embedding-capable (for semantic search)
}

export interface UsageRow {
  model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost: number; // USD, computed from live catalog pricing
  local: boolean; // local provider (no cost)
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

export interface ChatResponse {
  choices: { message: ChatMessage }[];
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

// A conversation sitting in the trash: restorable until the retention window lapses.
export interface TrashedConversation {
  id: string;
  title: string;
  deleted_at: string;
}

// A citation extracted server-side from a knowledge tool's RESULT during an agent turn —
// deterministic (never parsed out of model prose), so it exists with any model. Field
// meanings mirror KbHit's citation block; `offset` deep-links Knowledge to the passage.
export interface Source {
  id: string;
  title?: string | null;
  source?: string | null;
  page?: number | null;
  page_label?: string | null;
  offset?: number | null;
}

export interface StoredMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  sources?: Source[]; // present only on assistant messages that cited knowledge
}

export interface ConversationFull extends Conversation {
  messages: StoredMessage[];
}

export interface AgentResult {
  status: "complete" | "awaiting_approval" | "max_steps" | "error";
  message?: string;
  detail?: string; // present on a scheduled-run error
  turn_id?: string;
  pending?: { id: string; tool: string; tier: string }[];
  degraded?: boolean;
  sources?: Source[]; // citations from the turn's tool results ([] when no knowledge was used)
  guidance?: { request_type: string; directive: string }; // optimizer steering that shaped this answer
}

// Allowed self-review cadences (the Settings segmented control's hour options). Kept in
// lockstep with selfreview.ALLOWED_INTERVAL_HOURS; anything else 422s server-side.
export type SelfImproveInterval = 2 | 4 | 8 | 24;

export interface Improvement {
  id: string;
  created_at: string;
  category: string; // preference | workflow | knowledge | prompt
  component: string;
  lever_type: string;
  status: string; // proposed | active | reverted | rejected | superseded
  confidence: number;
  description: string;
  applied_at: string | null;
  reverted_at: string | null;
}

export interface OptimizerStrategy {
  id: string;
  request_type: string;
  status: string; // shadow | active | disabled
  fired: number;
  directive: string;
  created_at: string;
}

export interface Memory {
  id: string;
  text: string;
  created_at: string;
  updated_at: string;
}

export interface Profile {
  assistant_name: string;
  user_name: string;
  instructions: string;
}

export type TaskPriority = "low" | "medium" | "high";
export type TaskRecur = "none" | "daily" | "weekly";

export interface Task {
  id: string;
  title: string;
  notes: string;
  tags: string[];
  status: "open" | "done";
  due_date: string | null;
  due_time: string | null;
  priority: TaskPriority;
  recur: TaskRecur;
  created_at: string;
  updated_at: string;
}

export interface TaskInput {
  title: string;
  notes: string;
  due_date: string | null;
  due_time: string | null;
  priority: TaskPriority;
  recur: TaskRecur;
  tags: string[];
}

export interface PendingAction {
  id: string;
  tool: string;
  tier: string;
  created_at: string;
  turn_id: string | null; // the parked agent turn this approval belongs to (lets chat re-find its banner)
  conversation_id: string | null;
  args: Record<string, unknown>;
  // Whether consent would actually store an "always allow" for this tool. The server
  // decides; offering the button where it would be refused reads as a broken button.
  // Superseded by `remember_mode` for the web UI; kept for back-compat readers.
  rememberable?: boolean;
  // The consent shape the server would apply. "tool" = whole-tool remember, "site" =
  // per-host remember (button reads "Always allow <host>"), null = never remembered.
  remember_mode?: "tool" | "site" | null;
  // The host parsed from this pending call's URL, when remember_mode is "site".
  remember_host?: string | null;
}

// One site-scoped consent entry: URL tools remember per-host, so the same tool can
// have several — each row is deleted independently (DELETE with ?host=).
// Voice server status (STT/TTS through the user's LOCAL audio server). reachable is
// null unless the caller asked for a live probe (settings page); the chat page's
// per-mount call never pays one.
export interface VoiceStatus {
  configured: boolean;
  source: "voice" | "mlx" | "";
  reachable: boolean | null;
  stt_model: string;
  tts_model: string; // "" = server TTS off (browser system voices only)
  tts_voice: string;
  // Probed only: the configured (or any whisper-family) transcription model is loaded
  // on the server. false = reachable but dictation WILL fail — the settings card warns.
  stt_ready: boolean | null;
  // Dictation is always available: the in-process local engine backs everything.
  stt_available: boolean;
  // Which engine the NEXT dictation will use (a failing server is skipped for a while).
  engine: "server" | "local";
  // The local engine's readiness: download percent, then a brief engine-load phase.
  local: { phase: "absent" | "downloading" | "loading" | "ready" | "error"; pct: number; error: string };
}

// The aggregate Settings → Status payload (see status_routes.py). Sections beyond
// voice_local appear only when unlocked.
export interface AppStatus {
  version: string;
  unlocked: boolean;
  voice_local: { phase: "absent" | "downloading" | "loading" | "ready" | "error"; pct: number; error: string };
  storage: { data_dir: string; db_bytes: number; models_bytes: number; total_bytes: number };
  memory: { rss_bytes: number; python: string };
  voice?: { engine: "server" | "local"; server_configured: boolean; stt_model: string; tts_model: string };
  local_models?: { ollama_configured: boolean; mlx_configured: boolean; mlxe_configured: boolean };
  knowledge?: { documents: number; embedded_chunks: number };
  schedules?: { enabled: number; total: number };
  feeds?: { count: number; errors?: number };
  devices?: { paired: number };
}

export interface RememberedSite {
  tool: string;
  host: string;
}

export interface Schedule {
  id: string;
  title: string;
  prompt: string;
  model: string | null;
  enabled: boolean;
  interval_minutes: number;
  next_run: string;
  last_run: string | null;
}

export interface ScheduleInput {
  title: string;
  prompt: string;
  interval_minutes: number;
  start_in_minutes: number;
  model: string | null;
}

export interface ScheduleRun {
  id: string;
  ran_at: string;
  status: string;
  message: string;
  error: string | null;
}

// A run tagged with its parent schedule — the aggregate "Output" feed across all schedules.
// `seen` drives the Scheduled-updates chat feed's "New" marker + the nav badge.
export interface RecentScheduleRun extends ScheduleRun {
  schedule_id: string;
  schedule_title: string;
  seen: boolean;
}

export interface AuditEntry {
  id: string;
  ts: string;
  actor: string;
  tool: string;
  tier: string;
  decision: string;
  ok: boolean;
  conversation_id: string | null;
  args_summary: string;
  result_summary: string;
  error: string;
}

export interface EmailStatus {
  connected: boolean;
  address: string | null;
  has_creds: boolean;
  redirect_uri: string;
}

export interface EmailMessage {
  id: string;
  thread_id: string;
  from: string;
  subject: string;
  date: string;
  snippet?: string;
  body?: string;
}

export interface KbDoc {
  id: string;
  title: string;
  tags: string[]; // manual labels; lexical-searchable and click-to-filter in the UI
  created_at: string;
  updated_at: string;
}

export interface KbDocFull extends KbDoc {
  content: string;
}

// `duplicate` = the text was already in the knowledge base, so the EXISTING document is returned
// instead of a second copy (which would then turn up in every search forever).
export interface IngestResult {
  id: string;
  title: string;
  chars: number;
  duplicate: boolean;
}

// Keyword and vector search miss in opposite directions, so "hybrid" (rank-fused) is the default.
export type SearchMode = "hybrid" | "lexical" | "semantic";

export interface KbHit {
  id: string;
  title: string;
  score: number;
  snippet: string;
  // Citation: WHERE the match came from. `source` is the original filename or URL, `page` the
  // 1-based page (null when the document has no pages), and `offset` the character position of the
  // matched passage — which is what lets the viewer open the document AT the match.
  chunk_idx: number | null;
  source: string;
  page: number | null;
  page_label: string; // what a "page" is in this format: page | slide | sheet
  offset: number;
}

// A Vault: a named, selectable subset of your knowledge. The unit you scope a search to, and the
// unit you export and share. `kind` is "local" (you made it) or "imported" (it came from someone).
export interface Vault {
  id: string;
  kind: "local" | "imported";
  version: number; // legacy alias for internal_seq (kept for callers that predate the lifecycle split)
  // internal_seq: every export bumps this. published_seq: only OPEN publishes move it, so the
  // "public version" a subscriber sees is truthful even after private sealed shares. null when
  // this vault has never been open-published.
  internal_seq: number;
  published_seq: number | null;
  published_at: string | null; // UTC calendar date the last open publish stamped (YYYY-MM-DD)
  // Publisher-side one-way marker: this vault was retired to every subscriber. A later normal
  // open publish un-retires it (see backend spec §5).
  retired_published: boolean;
  // Sealed-share history: has this vault ever been sealed-shared, and at what seq (the UI warns
  // that a sealed re-export mints a new key that orphans previous recipients).
  shared_sealed: boolean;
  sealed_seq: number | null;
  // Publisher's own name/description (imported/subscribed vaults) — signed by the publisher and
  // distinct from the local ``name``/``description`` this user can edit. Empty string when unknown.
  publisher_name: string;
  publisher_description: string;
  // Publisher-side note (open-published vaults): where the user uploaded the .sbvault. Local
  // metadata only — never travels in the export. Empty string = unset. Settable via PATCH
  // (validated with the same public-internet rules subscribe applies); read by verify-hosted.
  hosted_url: string;
  name: string;
  description: string;
  tags: string[]; // the local user's labels — never travel in an export

  // Where an imported vault came from — pinned at import/subscribe time. null for a vault you made
  // yourself. `url` is present only on a URL subscription (fragment-stripped before it was stored).
  source: {
    vault_id?: string;
    publisher_pubkey?: string;
    seq?: number;
    url?: string;
    mode?: string;
    added_at?: string;
    last_checked?: string | null;
    // Opt-in scheduled auto-update (Stage E). Default OFF: the background timer only touches a
    // subscription once the user turns this on. Interval is floored to 1h server-side. `last_error`
    // is a HOST-ONLY staleness/failure note (never a URL path) so the card can flag a dead host.
    auto_update?: boolean;
    check_interval_seconds?: number;
    last_error?: string | null;
    // Set when an update check met a DIFFERENT publisher key. While present, check/update refuse
    // (409) and the card shows the key-change warning; trust-publisher must echo this exact key.
    blocked?: { offered_pubkey: string } | null;
    // The publisher retired the vault: an applied retire-export flips this on, the auto-update
    // timer excludes retired subscriptions, and the docs stay in Knowledge (this is a channel
    // stop, not a shred). A later non-retired higher-seq apply un-sets it (publisher came back).
    retired?: boolean;
    // The host proved dead. `took_down` = HTTP 410 Gone (publisher intentionally removed it);
    // `dead_host` = eight consecutive failures spanning ≥ 7 days. Both drop the subscription
    // from the auto-update pass; a manual check still runs, and a success clears the flag.
    unreachable?: boolean;
    unreachable_reason?: "took_down" | "dead_host";
    consecutive_failures?: number;
    first_failure_at?: string | null;
  } | null;
  // True once this vault has been published OPEN (a plaintext file, no key). Never clears —
  // publishing is irreversible — and the UI must show the fingerprint beside any "Public" badge.
  published_open: boolean;
  publisher_fingerprint?: string; // present only when published_open: the identity subscribers pin
  // The PINNED publisher of an imported/subscribed vault — the identity every update must match.
  // Never show a "Subscribed" badge without it.
  pinned_fingerprint?: string;
  // The OFFERED key's fingerprint while a key change is blocked — always shown beside the pinned
  // one (a human decides between identities, never from one alone).
  blocked_fingerprint?: string;
  doc_count: number;
  created_at: string;
  updated_at: string;
}

// One document's membership in a vault. `origin` says who owns the COPY: "import" = it came with
// the vault (read-only; a vault update may replace it — Detach to claim it), "owner" = the user's.
// A feed subscription: a vault that fills itself. The URL is stored encrypted server-side;
// cadence fields are plaintext so the scheduler can find due feeds without the master key.
export interface Feed {
  id: string;
  vault_id: string;
  url: string;
  title: string;
  enabled: boolean;
  last_checked: string | null; // null = never fetched yet
  last_status: string; // "ok", "ok: N new", or "error: reason" — shown verbatim on the row
  tags: string[]; // stamped on every document the feed ingests; set at subscribe time
  created_at: string;
}

export interface VaultMember {
  id: string;
  origin: "owner" | "import";
}

export interface VaultImportResult {
  id: string;
  name: string;
  publisher: string; // the publisher FINGERPRINT (SB-...) — what the user is actually asked to trust
  added: number;
  duplicates: number;
  vectors_used: boolean;
  // Present when the file's vault_id matched an existing pin: it was applied as an UPDATE to that
  // vault (never a duplicate), and the update counts below say what changed.
  update?: boolean;
  updated?: number;
  deleted?: number;
  kept_yours?: number;
  seq?: number;
}

// "Is there a newer version?" — seq is the version pinned locally, remote_seq what the host
// serves. `rollback` = the host is serving something OLDER than the pin (refused, never applied).
// `retired` is True iff the remote manifest carries the publisher's retire marker; `kind` is the
// one-word disposition for the UI (retired short-circuits over update — applying a retire-export
// leaves the vault in the retired state, so a "click Update" button that means "stop following
// this" would mislead).
export interface VaultCheckResult {
  behind: boolean;
  remote_seq: number;
  seq: number;
  rollback: boolean;
  retired: boolean;
  kind: "update" | "up-to-date" | "rollback" | "retired";
}

// What an applied update did. `kept_yours` = documents that stayed the user's own (they edited
// them, or already had the same text) — an update never overwrites those. `retired` = the applied
// update was a retire-export (the subscription is now in the retired state, no longer auto-
// checked). `renamed_from` = the publisher's previous name when the update carried a rename.
export interface VaultUpdateResult {
  added: number;
  updated: number;
  deleted: number;
  kept_yours: number;
  seq: number;
  retired: boolean;
  renamed_from: string | null;
}

// Publisher-side "does the file up there match what I last published?" — powered by
// vault_sync.check against the vault's hosted_url, verified against THIS install's own key.
// `matches` = hosted seq == published_seq; `behind` = hosted seq < published_seq (the classic
// forgot-to-upload). A hosted seq > local one is a genuine anomaly (matches=false, behind=false).
// Unreachable (404/410/timeout) is a clean `reachable:false, seq:null` row; a hosted file signed
// by a different key is `reachable:true, matches:false, seq:null` with the offered fingerprint
// in `detail`. `detail` is always a human-ready sentence — the UI renders it verbatim.
export interface VerifyHostedResult {
  reachable: boolean;
  seq: number | null;
  matches: boolean;
  behind: boolean;
  retired: boolean;
  detail: string;
}

// Metadata a vault export response header carries — the file is the body, these describe the
// export mode + whether the file duplicates the previous content and whether the export minted
// a fresh Vault Key (a sealed re-export orphans anyone holding the old key).
export interface ExportHeaders {
  seq: number | null;
  mode: "sealed" | "open" | null;
  unchanged: boolean;
  rotatedKey: boolean;
  retired: boolean;
}

// Pure parser for the x-sb-export-* headers the backend attaches to /export + /retire.
// Extracted so a test can pin the exact header names — a silent rename on either side would
// otherwise reduce the UI to "no warning ever fires" without failing any type check.
export function parseExportHeaders(h: Headers): ExportHeaders {
  console.assert(h instanceof Headers, "parseExportHeaders: real Headers instance required");
  console.assert(typeof h.get === "function", "parseExportHeaders: h.get missing");
  const rawSeq = h.get("x-sb-export-seq");
  const parsedSeq = rawSeq === null ? NaN : Number.parseInt(rawSeq, 10);
  const mode = h.get("x-sb-export-mode");
  return {
    seq: Number.isFinite(parsedSeq) ? parsedSeq : null,
    mode: mode === "sealed" || mode === "open" ? mode : null,
    unchanged: h.get("x-sb-export-unchanged") === "1",
    rotatedKey: h.get("x-sb-export-rotated-key") === "1",
    retired: h.get("x-sb-export-retired") === "1",
  };
}

// Subscribe-by-URL result: an import, plus the host the vault came from (host only — never the
// full URL, whose path can name the topic as plainly as the vault name would).
export interface VaultSubscribeResult extends VaultImportResult {
  url_host: string;
}

export interface DeviceInfo {
  device_id: string;
  label: string;
  created_at: string;
  last_seen: string | null; // last successful connection (hourly granularity), null = never
}

// POST /api/devices response = the full pairing payload (shown once, encoded into the QR).
export interface PairingResponse extends DeviceInfo {
  credential: string;
  desktop_pubkey: string;
  signaling_url: string;
  desktop_id: string;
  ice_servers: RTCIceServer[];
}

// The browser's IANA timezone, reported on health so the assistant can speak the
// user's local time (the server otherwise only knows its own clock).
function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone ?? "";
  } catch {
    return "";
  }
}

export const api = {
  health: () =>
    req<{
      status: string;
      version: string;
      launcher_update_needed?: boolean;
      update_ready?: string;      // a version the desktop launcher has downloaded and can install
      update_requested?: string;  // someone at the desk already asked for it
    }>("/api/health", {
      headers: browserTimezone() ? { "x-smartbrain-timezone": browserTimezone() } : {},
    }),
  accountStatus: () => req<AccountStatus>("/api/account/status"),
  // Ask the desktop launcher to install the update it has staged. Desktop-local only: the
  // marker header is stripped by the phone bridge, so a paired device gets a 403 rather
  // than the power to restart the machine.
  installUpdate: () =>
    req<{ ok: boolean; version: string }>("/api/update/install", {
      method: "POST",
      headers: { "x-sb-local": "1" },
      body: JSON.stringify({}),
    }),
  setup: (passphrase: string) =>
    req<EmergencyKit>("/api/account/setup", { method: "POST", body: JSON.stringify({ passphrase }) }),
  unlock: (body: { passphrase?: string; recovery_key?: string }) =>
    req<{ unlocked: boolean }>("/api/account/unlock", { method: "POST", body: JSON.stringify(body) }),
  lock: () => req<{ unlocked: boolean }>("/api/account/lock", { method: "POST" }),
  changePassphrase: (current_passphrase: string, new_passphrase: string) =>
    req<{ ok: boolean }>("/api/account/passphrase", {
      method: "POST",
      body: JSON.stringify({ current_passphrase, new_passphrase }),
    }),
  resetPassphrase: (new_passphrase: string) =>
    req<{ ok: boolean }>("/api/account/passphrase/reset", {
      method: "POST",
      // X-SB-Local marks a Desktop-local request; the WebRTC bridge strips it, so a
      // remote/paired device cannot reset the passphrase (Security B8/F7).
      headers: { "x-sb-local": "1" },
      body: JSON.stringify({ new_passphrase }),
    }),

  // data portability (export JSON, download an encrypted backup, restore one).
  // Export + backup are sensitive egress (decrypted plaintext / whole-vault file), so both
  // are Desktop-local only (x-sb-local; the WebRTC bridge strips it) AND re-require the
  // passphrase — passed in the POST body and re-verified server-side (Security B8/F7).
  exportData: (passphrase: string) =>
    req<Record<string, unknown>>("/api/export", {
      method: "POST",
      headers: { "x-sb-local": "1" },
      body: JSON.stringify({ passphrase }),
    }),
  backup: async (passphrase: string): Promise<Blob> => {
    await remoteReady;
    const res = await fetch("/api/backup", {
      method: "POST",
      headers: { "x-sb-local": "1", "content-type": "application/json" },
      body: JSON.stringify({ passphrase }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new ApiError(res.status, (data as { detail?: string })?.detail || `backup failed (${res.status})`);
    }
    return res.blob();
  },
  restore: async (file: File): Promise<{ ok: boolean; message: string }> => {
    await remoteReady;
    // X-SB-Local: Desktop-local only — the WebRTC bridge strips it so a remote device
    // cannot replace the vault (Security B8/F7).
    const res = await fetch("/api/restore", { method: "POST", headers: { "x-sb-local": "1" }, body: file });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, (data as { detail?: string })?.detail || `restore failed (${res.status})`);
    return data as { ok: boolean; message: string };
  },

  // secrets (provider API keys are named provider:<name>:api_key)
  listSecrets: () => req<{ keys: string[] }>("/api/secrets"),
  putSecret: (key: string, value: string) =>
    req<{ ok: boolean; gateway_synced?: boolean }>(`/api/secrets/${encodeURIComponent(key)}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  deleteSecret: (key: string) =>
    req<{ ok: boolean }>(`/api/secrets/${encodeURIComponent(key)}`, { method: "DELETE" }),

  // local models (Ollama / MLX, run on the host, fronted by Bifrost)
  localModels: () => req<LocalModels>("/api/local-models"),
  // degraded: the gateway catalog was unreachable and the list came from direct local-server probes.
  listModels: () => req<{ models: DiscoveredModel[]; degraded?: boolean }>("/api/models"),
  getRoutes: () => req<{ routes: Record<string, string>; labels: Record<string, string> }>("/api/routes"),
  putRoutes: (routes: Record<string, string>) =>
    req<{ ok: boolean; routes: Record<string, string> }>("/api/routes", {
      method: "PUT",
      body: JSON.stringify({ routes }),
    }),
  // per-model context length (tokens). Sizes the dynamic tool-result cap so a big-context model can
  // read/summarize far more per step; MLX auto-detects, others fall back to `default` until overridden.
  getContextLengths: () => req<{ lengths: Record<string, number>; default: number }>("/api/model-context-lengths"),
  putContextLengths: (lengths: Record<string, number>) =>
    req<{ ok: boolean; lengths: Record<string, number> }>("/api/model-context-lengths", {
      method: "PUT",
      body: JSON.stringify({ lengths }),
    }),
  getUsage: (range?: { since?: string; until?: string }) => {
    const q = new URLSearchParams();
    if (range?.since) q.set("since", range.since);
    if (range?.until) q.set("until", range.until);
    const qs = q.toString();
    return req<{ usage: UsageRow[]; total_cost: number }>(`/api/usage${qs ? `?${qs}` : ""}`);
  },
  putOllama: (url: string) =>
    req<{ ok: boolean; gateway_synced?: boolean }>("/api/local-models/ollama", { method: "PUT", body: JSON.stringify({ url }) }),
  putMlx: (url: string, api_key: string) =>
    req<{ ok: boolean; gateway_synced?: boolean }>("/api/local-models/mlx", {
      method: "PUT",
      body: JSON.stringify({ url, api_key }),
    }),
  putMlxe: (url: string, api_key: string) =>
    req<{ ok: boolean; gateway_synced?: boolean }>("/api/local-models/mlxe", {
      method: "PUT",
      body: JSON.stringify({ url, api_key }),
    }),
  deleteLocalModel: (name: "ollama" | "mlx" | "mlxe") =>
    req<{ ok: boolean; gateway_synced?: boolean }>(`/api/local-models/${name}`, { method: "DELETE" }),

  // chat (stateless on the server today — the client sends the full transcript)
  chat: (body: { messages: ChatMessage[]; model?: string; capability?: string }) =>
    req<ChatResponse>("/api/chat", { method: "POST", body: JSON.stringify(body) }),

  // agentic tool-calling turn (OBSERVE auto-runs; dangerous tools park for approval)
  agentTurn: (body: { messages: ChatMessage[]; model?: string; capability?: string; conversation_id?: string | null; reply_length?: string }) =>
    req<AgentResult>("/api/agent/turn", { method: "POST", body: JSON.stringify(body) }),
  // Best-effort implicit-feedback signal (Stop / Regenerate) — telemetry only, never blocks the UI.
  feedback: (kind: "stop" | "regenerate" | "retry", conversation_id?: string | null) =>
    req<{ ok: boolean }>("/api/feedback", { method: "POST", body: JSON.stringify({ kind, conversation_id }) }).catch(() => undefined),
  agentResume: (turnId: string) =>
    req<AgentResult>(`/api/agent/resume/${encodeURIComponent(turnId)}`, { method: "POST" }),
  // SSE token streaming for a turn (Desktop/local only — the WebRTC relay buffers, so
  // callers fall back to agentTurn over a remote session). Returns the raw Response;
  // the caller reads the text/event-stream body. 423/4xx still surface as ApiError.
  // `signal` lets the caller abort mid-stream (chat's Stop button).
  agentTurnStream: async (body: {
    messages: ChatMessage[];
    model?: string;
    capability?: string;
    conversation_id?: string | null;
    reply_length?: string; // spoken replies: "short" | "medium" | "long"
  }, signal?: AbortSignal): Promise<Response> => {
    await remoteReady;
    const res = await fetch("/api/agent/turn/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => null) as { detail?: string } | null)?.detail;
      if (res.status === 423) handleLocked();
      throw new ApiError(res.status, detail || `stream failed (${res.status})`);
    }
    return res;
  },

  // The WHOLE tool loop as SSE: `tool` activity frames while it works, then one
  // terminal `final` (an AgentResult) or `error` frame. Same fallback posture as
  // agentTurnStream — remote sessions use the JSON endpoint instead.
  agentTurnEvents: async (body: {
    messages: ChatMessage[];
    model?: string;
    capability?: string;
    conversation_id?: string | null;
    // One-time token from the stream's "pending" frame: lets the server reuse the first
    // model response it already produced instead of asking for it again.
    primed?: string | null;
    reply_length?: string;
  }): Promise<Response> => {
    await remoteReady;
    const res = await fetch("/api/agent/turn/events", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => null) as { detail?: string } | null)?.detail;
      if (res.status === 423) handleLocked();
      throw new ApiError(res.status, detail || `turn failed (${res.status})`);
    }
    return res;
  },

  // web-search provider configuration (keys ride the generic secrets endpoints)
  getWebSearch: () =>
    req<{ engine: string; searxng_url: string; configured: string[]; engines: string[] }>("/api/websearch"),
  putWebSearch: (body: { engine: string; searxng_url: string }) =>
    req<{ ok: boolean }>("/api/websearch", { method: "PUT", body: JSON.stringify(body) }),

  // self-improvement (the reviewer + prompt optimizer; all switches fail closed server-side)
  // Cadence is a fixed set the settings UI exposes as a segmented control; the server 422s
  // anything else. `enabled` and `interval_hours` are independent — a PUT that names one
  // MUST never flip the other, so both callers below and the tests pin the exact body.
  getSelfImprove: () =>
    req<{ enabled: boolean; interval_hours: SelfImproveInterval; last_run: string | null }>(
      "/api/selfimprove",
    ),
  putSelfImprove: (patch: { enabled?: boolean; interval_hours?: SelfImproveInterval }) =>
    req<{ enabled: boolean; interval_hours: SelfImproveInterval; last_run: string | null }>(
      "/api/selfimprove",
      { method: "PUT", body: JSON.stringify(patch) },
    ),
  getOptimizer: () =>
    req<{ enabled: boolean; strategies: OptimizerStrategy[] }>("/api/selfimprove/optimizer"),
  putOptimizer: (enabled: boolean) =>
    req<{ enabled: boolean }>("/api/selfimprove/optimizer", { method: "PUT", body: JSON.stringify({ enabled }) }),
  listImprovements: () => req<{ improvements: Improvement[] }>("/api/selfimprove/improvements"),

  // chat history (encrypted conversations + messages; keyset pagination via before/limit)
  listConversations: (opts: { before?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (opts.before) qs.set("before", opts.before);
    if (opts.limit) qs.set("limit", String(opts.limit));
    const q = qs.toString();
    return req<{ conversations: Conversation[]; next_cursor?: string | null; has_more?: boolean }>(
      `/api/conversations${q ? `?${q}` : ""}`,
    );
  },
  createConversation: (title?: string) =>
    req<{ id: string }>("/api/conversations", { method: "POST", body: JSON.stringify({ title }) }),
  getConversation: (id: string, opts: { before?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (opts.before) qs.set("before", opts.before);
    if (opts.limit) qs.set("limit", String(opts.limit));
    const q = qs.toString();
    return req<ConversationFull & { next_cursor?: string | null; has_more?: boolean }>(
      `/api/conversations/${encodeURIComponent(id)}${q ? `?${q}` : ""}`,
    );
  },
  renameConversation: (id: string, title: string) =>
    req<{ ok: boolean }>(`/api/conversations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  deleteConversation: (id: string) =>
    req<{ ok: boolean }>(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  // Trash: deletes are reversible for the server's retention window, then purged.
  deleteAllConversations: () =>
    req<{ ok: boolean; trashed: number }>("/api/conversations", { method: "DELETE" }),
  listTrash: () =>
    req<{ trash: TrashedConversation[]; retention_days: number }>("/api/conversations/trash"),
  emptyTrash: () =>
    req<{ ok: boolean; deleted: number }>("/api/conversations/trash", { method: "DELETE" }),
  restoreConversation: (id: string) =>
    req<{ ok: boolean }>(`/api/conversations/${encodeURIComponent(id)}/restore`, { method: "POST" }),
  // `sources` (citations from the agent turn's tool results) persist with the assistant
  // message so a reloaded conversation shows the same chips as the live one.
  addMessage: (id: string, role: "user" | "assistant" | "system", content: string, sources?: Source[]) =>
    req<{ id: string }>(`/api/conversations/${encodeURIComponent(id)}/messages`, {
      method: "POST",
      body: JSON.stringify(sources?.length ? { role, content, sources } : { role, content }),
    }),

  // tools + audit (H4)
  listToolDefs: () => req<{ tools: { name: string; description: string; tier: string }[] }>("/api/tools"),
  invokeTool: (name: string, args: Record<string, unknown>) =>
    req<{ result: unknown }>("/api/tools/invoke", { method: "POST", body: JSON.stringify({ name, args }) }),
  getAudit: (limit = 100) => req<{ entries: AuditEntry[] }>(`/api/audit?limit=${limit}`),
  // voice — dictation + spoken replies through the user's local audio server. The
  // phone's audio rides the WebRTC bridge like any upload; nothing touches a third party.
  // `prepare` re-arms the local model download after an error (idempotent).
  voiceStatus: (probe = false, prepare = false) => {
    const qs = [probe ? "probe=1" : "", prepare ? "prepare=1" : ""].filter(Boolean).join("&");
    return req<VoiceStatus>(`/api/voice/status${qs ? `?${qs}` : ""}`);
  },
  appStatus: () => req<AppStatus>("/api/status/overview"),
  // `partial`: a mid-utterance snapshot — the server answers fast and rough; the final
  // pass at the pause is the one that counts.
  voiceTranscribe: async (audio: Blob, opts: { keep?: boolean; partial?: boolean } = {}): Promise<{ text: string }> => {
    await remoteReady;
    // Hard timeout: a hung transcription request once wedged the mic for five minutes
    // of dead clicks. 45 s is generous for any healthy engine; past it, fail loudly.
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 45000);
    let res: Response;
    try {
      res = await fetch(opts.partial ? "/api/voice/transcribe?partial=1" : "/api/voice/transcribe", {
        method: "POST",
        headers: { "content-type": "audio/wav", ...(opts.keep ? { "x-sb-voice-keep": "1" } : {}) },
        body: audio,
        signal: abort.signal,
      });
    } catch (err) {
      if (abort.signal.aborted) throw new ApiError(504, "transcription timed out — try again");
      throw err;
    } finally {
      clearTimeout(timer);
    }
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      if (res.status === 423) handleLocked();
      throw new ApiError(res.status, (data as { detail?: string })?.detail || `transcription failed (${res.status})`);
    }
    return data as { text: string };
  },
  // One spoken chunk from the server voice; null = no server voice configured (the
  // caller falls back or stays quiet — this is the LAST link of the fallback chain).
  voiceSpeak: async (text: string): Promise<Blob | null> => {
    await remoteReady;
    const res = await fetch("/api/voice/speak", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) return null;
    return res.blob();
  },
  putVoiceConfig: (body: { url: string; api_key: string; stt_model: string; tts_model: string; tts_voice: string }) =>
    req<{ ok: boolean; status: VoiceStatus }>("/api/local-models/voice", {
      method: "PUT",
      headers: { "x-sb-local": "1" },
      body: JSON.stringify(body),
    }),
  deleteVoiceConfig: () =>
    req<{ ok: boolean }>("/api/local-models/voice", { method: "DELETE", headers: { "x-sb-local": "1" } }),

  listPending: () => req<{ pending: PendingAction[] }>("/api/agent/pending"),
  approveAction: (id: string, confirmTool: string | null = null, remember = false) =>
    req<{ status: string; result: unknown }>(`/api/agent/pending/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body: JSON.stringify({ confirm_tool: confirmTool, remember }),
    }),
  denyAction: (id: string) =>
    req<{ ok: boolean }>(`/api/agent/pending/${encodeURIComponent(id)}/deny`, { method: "POST" }),
  listRemembered: () => req<{ tools: string[]; sites: RememberedSite[] }>("/api/agent/remembered"),
  // `host` scopes the delete to one site-scoped entry (URL tools remember per-host).
  // Without it, the WHOLE-tool consent for `name` is dropped.
  forgetRemembered: (name: string, host: string | null = null) => {
    const q = host ? `?host=${encodeURIComponent(host)}` : "";
    return req<{ ok: boolean }>(`/api/agent/remembered/${encodeURIComponent(name)}${q}`, { method: "DELETE" });
  },

  // planner tasks (encrypted; status + due_date plaintext)
  listTasks: () => req<{ tasks: Task[] }>("/api/tasks"),
  addTask: (body: TaskInput) =>
    req<{ id: string }>("/api/tasks", { method: "POST", body: JSON.stringify(body) }),
  updateTask: (id: string, body: TaskInput) =>
    req<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  setTaskStatus: (id: string, status: "open" | "done") =>
    req<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  deleteTask: (id: string) =>
    req<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(id)}`, { method: "DELETE" }),

  // schedules (encrypted prompt; fires an agent turn while unlocked)
  listSchedules: () => req<{ schedules: Schedule[] }>("/api/schedules"),
  addSchedule: (body: ScheduleInput) =>
    req<{ id: string }>("/api/schedules", { method: "POST", body: JSON.stringify(body) }),
  updateSchedule: (id: string, body: ScheduleInput) =>
    req<{ ok: boolean }>(`/api/schedules/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  setScheduleEnabled: (id: string, enabled: boolean) =>
    req<{ ok: boolean }>(`/api/schedules/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  deleteSchedule: (id: string) =>
    req<{ ok: boolean }>(`/api/schedules/${encodeURIComponent(id)}`, { method: "DELETE" }),
  runSchedule: (id: string) =>
    req<AgentResult>(`/api/schedules/${encodeURIComponent(id)}/run`, { method: "POST" }),
  listScheduleRuns: (id: string) =>
    req<{ runs: ScheduleRun[] }>(`/api/schedules/${encodeURIComponent(id)}/runs`),
  recentScheduleRuns: () => req<{ runs: RecentScheduleRun[] }>("/api/schedules/runs/recent"),
  // Scheduled-updates feed (Chat): unseen count drives the nav badge; POST marks all seen.
  unseenScheduleUpdates: () => req<{ count: number }>("/api/schedules/updates/unseen-count"),
  markScheduleUpdatesSeen: () =>
    req<{ ok: boolean; marked: number }>("/api/schedules/updates/seen", { method: "POST" }),

  // memory + identity (facts injected into chat server-side)
  listMemories: () => req<{ memories: Memory[] }>("/api/memories"),
  addMemory: (text: string) =>
    req<{ id: string }>("/api/memories", { method: "POST", body: JSON.stringify({ text }) }),
  deleteMemory: (id: string) =>
    req<{ ok: boolean }>(`/api/memories/${encodeURIComponent(id)}`, { method: "DELETE" }),
  getProfile: () => req<Profile>("/api/profile"),
  setProfile: (p: Profile) => req<{ ok: boolean }>("/api/profile", { method: "PUT", body: JSON.stringify(p) }),

  // knowledge base (encrypted at rest; lexical + semantic search)
  listDocs: () => req<{ documents: KbDoc[] }>("/api/kb"),
  addDoc: (title: string, content: string) =>
    req<{ id: string }>("/api/kb", { method: "POST", body: JSON.stringify({ title, content }) }),
  ingestUrl: (url: string) =>
    req<IngestResult>("/api/kb/ingest-url", { method: "POST", body: JSON.stringify({ url }) }),
  uploadDoc: (file: File) =>
    req<IngestResult>(
      `/api/kb/upload?filename=${encodeURIComponent(file.name)}`,
      { method: "POST", body: file, headers: { "content-type": "application/octet-stream" } },
    ),
  // How much of the knowledge base is semantically indexed. Uploads no longer block on embedding,
  // so the UI polls this to say "indexing 12 of 40" instead of looking done while semantic search
  // still can't see the new documents.
  indexStatus: () =>
    req<{ total: number; pending: number; indexed: number; model: string;
          summarized: number; summary_total: number }>("/api/kb/index-status"),
  getDoc: (id: string) => req<KbDocFull>(`/api/kb/${encodeURIComponent(id)}`),
  renameDoc: (id: string, title: string) =>
    req<{ ok: boolean }>(`/api/kb/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  setDocTags: (id: string, tags: string[]) =>
    req<{ ok: boolean }>(`/api/kb/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ tags }),
    }),
  deleteDoc: (id: string) =>
    req<{ ok: boolean }>(`/api/kb/${encodeURIComponent(id)}`, { method: "DELETE" }),
  // `vault` scopes the search to one vault's documents ("" = all knowledge).
  searchKb: (q: string, mode: SearchMode = "hybrid", limit = 10, vault = "") =>
    req<{ results: KbHit[]; degraded?: boolean }>(
      `/api/kb/search?${new URLSearchParams({
        q,
        mode,
        limit: String(limit),
        ...(vault ? { vault } : {}),
      }).toString()}`,
    ),
  // Bounded by a wall-clock budget server-side, so it always returns; `pending` says what's left
  // (the background indexer finishes it) rather than pretending the whole backlog is done.
  reindexKb: () =>
    req<{ embedded: number; skipped: number; failed: number; error: string; pending: number }>(
      "/api/kb/reindex",
      { method: "POST" },
    ),

  // vaults — a named subset of knowledge you can scope a search to, export, and share
  listVaults: () => req<{ vaults: Vault[] }>("/api/vaults"),
  createVault: (name: string, description = "") =>
    req<Vault>("/api/vaults", { method: "POST", body: JSON.stringify({ name, description }) }),
  // Rename / re-describe / re-tag / re-note-hosted-URL a vault. Each field is INDEPENDENT: absent
  // = untouched (a rename must not wipe tags or the hosted URL); `hosted_url: ""` clears the note.
  // Server validates hosted_url against the same netguard rules subscribe uses (http(s) only, no
  // localhost/LAN) — a bad URL is a 400 with a plain-words detail the UI surfaces inline.
  updateVaultMeta: (
    id: string,
    body: { name: string; description?: string; tags?: string[]; hosted_url?: string },
  ) =>
    req<Vault>(`/api/vaults/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  // `remove_docs: true` also deletes the vault's IMPORT-origin documents (owner-origin copies
  // always stay — "delete grouping vs shred files" applies to the user's own documents whatever
  // they asked for). Default is false — the historical "keep documents" behavior.
  deleteVault: (id: string, opts: { remove_docs?: boolean } = {}) => {
    const qs = opts.remove_docs ? "?remove_docs=1" : "";
    return req<{ ok: boolean; removed_docs: number }>(
      `/api/vaults/${encodeURIComponent(id)}${qs}`,
      { method: "DELETE" },
    );
  },
  // One vault plus WHICH documents are in it — the list view only carries a count. `members`
  // carries each membership's origin so the UI shows Detach only on imported rows.
  getVault: (id: string) =>
    req<Vault & { doc_ids: string[]; members: VaultMember[] }>(
      `/api/vaults/${encodeURIComponent(id)}`,
    ),
  removeFromVault: (id: string, docId: string) =>
    req<{ ok: boolean; doc_count: number }>(
      `/api/vaults/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}`,
      { method: "DELETE" },
    ),
  // Make an imported copy the user's own: rename/delete work again, and a future update from the
  // vault's publisher will skip it instead of replacing it.
  detachFromVault: (id: string, docId: string) =>
    req<{ ok: boolean; origin: string }>(
      `/api/vaults/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}/detach`,
      { method: "POST" },
    ),
  addToVault: (id: string, doc_ids: string[]) =>
    req<{ added: number; doc_count: number }>(`/api/vaults/${encodeURIComponent(id)}/documents`, {
      method: "POST",
      body: JSON.stringify({ doc_ids }),
    }),

  // feeds — subscribe to an RSS/Atom URL; new posts land as documents in the feed's own vault.
  // Adding and deleting are Desktop-local (the paste IS the consent for background refreshes,
  // so it must come from the machine's owner); reading and refreshing work from any surface.
  listFeeds: () => req<{ feeds: Feed[] }>("/api/feeds"),
  // `tags` (optional) are stamped on every document the feed ever ingests.
  addFeed: (url: string, tags: string[] = []) =>
    req<{ id: string; title: string; vault_id: string; items: number }>("/api/feeds", {
      method: "POST",
      headers: { "x-sb-local": "1" },
      body: JSON.stringify(tags.length ? { url, tags } : { url }),
    }),
  refreshFeed: (id: string) =>
    req<{ items: number }>(`/api/feeds/${encodeURIComponent(id)}/refresh`, { method: "POST" }),
  // Same keep-vs-shred rule as deleteVault: the grouping goes, the articles stay unless asked.
  deleteFeed: (id: string, opts: { remove_docs?: boolean } = {}) =>
    req<{ deleted: boolean; docs_removed: number }>(
      `/api/feeds/${encodeURIComponent(id)}${opts.remove_docs ? "?remove_docs=1" : ""}`,
      { method: "DELETE", headers: { "x-sb-local": "1" } },
    ),

  // Export hands out content that is plaintext-equivalent to whoever holds the key — and in
  // "open" (public) mode IS the plaintext, with no key at all — so, like backup, it is
  // Desktop-local (x-sb-local, which the WebRTC bridge cannot forward) and requires the
  // passphrase again. Returns the .sbvault file itself.
  exportVault: async (
    id: string,
    passphrase: string,
    mode: "sealed" | "open" = "sealed",
  ): Promise<{ blob: Blob; headers: ExportHeaders }> => {
    await remoteReady;
    const res = await fetch(`/api/vaults/${encodeURIComponent(id)}/export`, {
      method: "POST",
      headers: { "x-sb-local": "1", "content-type": "application/json" },
      body: JSON.stringify({ passphrase, mode }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new ApiError(res.status, (data as { detail?: string })?.detail || `export failed (${res.status})`);
    }
    // Read headers BEFORE consuming the body — a blob() call can invalidate the response object
    // in some implementations, and the UI needs both (blob to download, headers to warn on).
    const headers = parseExportHeaders(res.headers);
    return { blob: await res.blob(), headers };
  },
  // Retire a published vault: produces a FINAL open export with content intact and the retired
  // marker set. Subscribers apply the final content and drop out of auto-checks; publishing again
  // un-retires. Same Desktop-local + passphrase gate as /export — the file is decrypted plaintext.
  retireVault: async (
    id: string,
    passphrase: string,
  ): Promise<{ blob: Blob; headers: ExportHeaders }> => {
    await remoteReady;
    const res = await fetch(`/api/vaults/${encodeURIComponent(id)}/retire`, {
      method: "POST",
      headers: { "x-sb-local": "1", "content-type": "application/json" },
      body: JSON.stringify({ passphrase }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new ApiError(res.status, (data as { detail?: string })?.detail || `retire failed (${res.status})`);
    }
    const headers = parseExportHeaders(res.headers);
    return { blob: await res.blob(), headers };
  },
  // Fetch the vault's hosted_url and check the hosted file against THIS install's last publish.
  // Desktop-local only (x-sb-local; the bridge strips it) — the endpoint names this install's own
  // publisher key in its verdict, so a paired device must not be able to trigger the read.
  // Read-only: never touches subscription state, never re-pins anything.
  verifyHostedVault: (id: string) =>
    req<VerifyHostedResult>(`/api/vaults/${encodeURIComponent(id)}/verify-hosted`, {
      method: "POST",
      headers: { "x-sb-local": "1" },
    }),
  vaultKey: async (id: string, passphrase: string): Promise<string> => {
    await remoteReady;
    const res = await fetch(`/api/vaults/${encodeURIComponent(id)}/key`, {
      method: "POST",
      headers: { "x-sb-local": "1", "content-type": "application/json" },
      body: JSON.stringify({ passphrase }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, (data as { detail?: string })?.detail || "could not read the key");
    return (data as { key: string }).key;
  },
  // Subscribe to a PUBLIC vault by URL. Ingress like importVault (unlock only, not Desktop-local):
  // the documents are verified against the publisher's signature and re-encrypted under the user's
  // own passphrase as they land, and the publisher is PINNED on this first contact.
  subscribeVault: (url: string) =>
    req<VaultSubscribeResult>("/api/vaults/subscribe", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  // `key` may be empty for a PUBLIC (open) .sbvault file — those have no key at all.
  importVault: async (file: File, key: string): Promise<VaultImportResult> => {
    await remoteReady;
    const res = await fetch(`/api/vaults/import?key=${encodeURIComponent(key)}`, {
      method: "POST",
      body: file,
      headers: { "content-type": "application/octet-stream" },
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, (data as { detail?: string })?.detail || "import failed");
    return data as VaultImportResult;
  },
  // Ask the pinned URL whether a newer version exists (writes nothing but last_checked). A 409
  // means updates are blocked: the publisher's key changed, or the host serves an older version.
  checkVaultUpdates: (id: string) =>
    req<VaultCheckResult>(`/api/vaults/${encodeURIComponent(id)}/check-updates`, { method: "POST" }),
  // Fetch + verify + apply a newer version, all-or-nothing. Documents the user edited stay
  // theirs (reported in kept_yours), and the publisher's signature is checked against the PIN.
  updateVault: (id: string) =>
    req<VaultUpdateResult>(`/api/vaults/${encodeURIComponent(id)}/update`, { method: "POST" }),
  // Opt-in scheduled auto-update for a subscription (Stage E). Off by default; the interval is
  // floored to 1h server-side. Auto-update runs only on the Desktop, only while unlocked, and never
  // applies a publisher key change on its own — it blocks and reports that in the feed instead.
  setSubscription: (id: string, opts: { auto_update?: boolean; check_interval_seconds?: number }) =>
    req<Vault>(`/api/vaults/${encodeURIComponent(id)}/subscription`, {
      method: "PATCH",
      body: JSON.stringify(opts),
    }),
  // Re-pin a subscription to a NEW publisher key the user confirmed out-of-band. The most
  // consequential act in the vault system, so it gates like export: Desktop-local (x-sb-local)
  // + passphrase re-entry — and it names the exact key it blesses, so a host that rotated again
  // since the user checked is refused instead of silently trusted.
  trustVaultPublisher: (id: string, offered_pubkey: string, passphrase: string) =>
    req<{ ok: boolean; pinned_fingerprint: string }>(
      `/api/vaults/${encodeURIComponent(id)}/trust-publisher`,
      {
        method: "POST",
        headers: { "x-sb-local": "1" },
        body: JSON.stringify({ offered_pubkey, passphrase }),
      },
    ),

  // email (Gmail via loopback OAuth; reads + user-initiated send; agent send is gated)
  emailStatus: () => req<EmailStatus>("/api/email/status"),
  emailConnect: (client_id: string, client_secret: string) =>
    req<{ auth_url: string }>("/api/email/connect", {
      method: "POST",
      body: JSON.stringify({ client_id, client_secret }),
    }),
  emailReconnect: () => req<{ auth_url: string }>("/api/email/reconnect", { method: "POST" }),
  emailDisconnect: () => req<{ ok: boolean }>("/api/email/disconnect", { method: "DELETE" }),
  emailMessages: (limit = 10) => req<{ messages: EmailMessage[] }>(`/api/email/messages?limit=${limit}`),
  emailMessage: (id: string) => req<EmailMessage>(`/api/email/messages/${encodeURIComponent(id)}`),
  emailSend: (to: string, subject: string, body: string) =>
    req<{ id: string; thread_id: string }>("/api/email/send", {
      method: "POST",
      body: JSON.stringify({ to, subject, body }),
    }),

  // MCP access token (read-only Knowledge for external tools)
  mcpInfo: () => req<{ endpoint: string; enabled: boolean }>("/api/mcp"),
  // The whole MCP-token verb-set is Desktop-local only (x-sb-local; the WebRTC bridge strips it):
  // read + mint return the raw bearer token in the body, and revoke would let a paired phone rotate
  // away the operator's token — so a phone can neither exfiltrate nor DoS it (Security B8).
  mcpToken: () => req<{ token: string | null }>("/api/mcp/token", { headers: { "x-sb-local": "1" } }),
  mcpNewToken: () => req<{ token: string }>("/api/mcp/token", { method: "POST", headers: { "x-sb-local": "1" } }),
  mcpRevokeToken: () => req<{ ok: boolean }>("/api/mcp/token", { method: "DELETE", headers: { "x-sb-local": "1" } }),

  // device pairing (remote access via WebRTC)
  // Enrolling/revoking devices + hosting a pairing session are Desktop-local only (x-sb-local;
  // the WebRTC bridge strips it), so a paired phone can't self-mint a credential or revoke the
  // Desktop's devices (Security B8). The metadata reads (listDevices, pairCodeStatus) stay open.
  listDevices: () => req<{ devices: DeviceInfo[] }>("/api/devices"),
  createDevice: (label: string) =>
    req<PairingResponse>("/api/devices", { method: "POST", headers: { "x-sb-local": "1" }, body: JSON.stringify({ label }) }),
  deleteDevice: (id: string) =>
    req<{ ok: boolean }>(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE", headers: { "x-sb-local": "1" } }),
  startPairCode: (label: string) =>
    req<{ code: string; expires_in: number; signaling_url: string }>("/api/devices/pair-code", { method: "POST", headers: { "x-sb-local": "1" }, body: JSON.stringify({ label }) }),
  cancelPairCode: () => req<{ ok: boolean }>("/api/devices/pair-code", { method: "DELETE", headers: { "x-sb-local": "1" } }),
  pairCodeStatus: () => req<{ state: "none" | "waiting" | "paired" | "expired" }>("/api/devices/pair-code"),
};
