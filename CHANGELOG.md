# Changelog

All notable changes to SmartBrain_3000 are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Keep an `## [Unreleased]` section at the top; on each release, rename it to the version +
date and start a fresh `## [Unreleased]`. Call out **breaking changes** and any migration
step explicitly — SmartBrain runs forward-only, data-safe migrations, but users still need
to know when a release changes behavior.

## [Unreleased]

### Fixed
- **Native supervision now verifies instead of trusts.** Two colliding starts (a
  relaunch that didn't stop the running stack, then the first auto-update install)
  exposed the class: a health check passes whenever *something* answers the port, so
  a second start's own dying processes went unnoticed, the recorded pids ended up
  naming dead processes, and every later Stop silently stopped nothing. Starting now
  refuses when an instance already answers (retried, so a momentary stall can't read
  as "nothing is running") **and** when a previously recorded process is still alive
  (verified to be ours, so a pid file that outlived a reboot can never block a
  start). A health answer is credited to the process we started only once it has
  outlived the startup in which a doomed one dies — and its own exit is watched
  throughout, so a death is caught whenever it happens. Stopping now confirms a
  process actually exited before dropping its record; a survivor keeps its record so
  the launcher never loses its only handle on a running process.
- **Chat could fail in Safari while the backend answered perfectly.** A local model
  takes ~8 seconds to its first token on a plain "hi", and the chat stream sent
  *nothing at all* during that wait — no opening bytes, no keepalive, and (unlike the
  sibling tool-turn stream, which has always done both) no `Cache-Control: no-cache`.
  Safari drops an idle streamed response, so the browser showed "Couldn't reach
  SmartBrain — check your connection" for a turn the server had completed and
  recorded. The stream now puts bytes on the wire the moment it opens and sends a
  keepalive every 5 seconds until the model produces text. Belt and braces: if a
  streamed answer's connection breaks before a single word arrives, the app quietly
  re-requests it whole on the non-streaming endpoint instead of showing an error —
  chat now survives any browser or proxy that dislikes long-lived streams.

## [0.8.6] - 2026-07-29

### Added
- **Native installs now update the app themselves** — the answer to "will SmartBrain
  auto-update?" is now yes on every layer. The launcher's 6-hour check (which already
  updates the launcher binary) now also watches for new app releases in native mode:
  a newer release is assembled in the background into its own versioned directory
  (verified downloads; the running version keeps serving untouched) and offered
  through the same **Install update now / Install on next start** menu the Docker
  path has — installing is a supervised restart, and the previous version stays on
  disk as the rollback. Supervision is tightened along the way: exactly one watcher
  ever runs, and a deliberate **Stop** no longer fights it (the supervisor used to
  restart a stack the user had just stopped). A stale `SMARTBRAIN_NATIVE_VERSION`
  pin can bootstrap a first install or force an upgrade, but can never downgrade an
  auto-updated one.
- **Native mode now sticks.** It was env-only, so a reboot or plain relaunch of the
  desktop app silently fell back to Docker — observed live as a compose attempt
  colliding with the surviving native stack's ports and blaming the internet. A
  successful native start now writes a persistent marker: every later launch boots
  native with no env needed, and a relaunched launcher **adopts** a healthy running
  stack instead of spawning a second one into the same ports. `SMARTBRAIN_NATIVE=0`
  forces Docker for one run; deleting the marker rolls back for good.

## [0.8.5] - 2026-07-29

### Fixed
- **Native mode: "Save & connect" on a local model could take chat down** until the
  next unlock — and each retry made it worse. Three stacked defects: the settings
  page submitted the Docker-era `host.docker.internal` host, the save endpoints
  registered that URL untranslated (the probe path translated; the register path
  didn't), and the gateway refuses a provider whose hostname doesn't resolve —
  *after* the old registration was already deleted, leaving no provider at all.
  Saves now translate the URL for the runtime they're in (both directions, so
  rolling back to Docker keeps working), a refused registration restores the
  previous one instead of leaving nothing, and the degraded-catalog fallback
  translates too (it used to fail exactly when it was needed).
- **The assistant now tells the time in your timezone.** The chat's time note used to
  inject bare UTC and leave the conversion to the model — which couldn't know your zone
  and sometimes got the math wrong ("Good morning! It's 5:32 am" at 11:32 PM). The
  browser now reports its timezone, and the note states your local date and time
  outright (UTC kept alongside for cross-zone questions). Scheduled runs use it too.
- Setup wording no longer says your passphrase "encrypts everything on this device" —
  it encrypts your SmartBrain data (chats, documents, settings), and now says exactly
  that.

## [0.8.4] - 2026-07-29

### Added
- Users still on an old desktop app get a **one-time, self-retiring banner** in the
  app ("your desktop app needs a one-time update", with the download link and the
  brew/scoop one-liners). It ships in the app image — which every install already
  updates automatically — and disappears forever the moment a self-updating desktop
  app talks to the backend. After that one click, nobody performs a manual update
  again.

### Added
- **The desktop app now updates itself — the last thing that didn't.** The app
  image always self-updated; the launcher binary required a package-manager
  command, which is why new capabilities (like native mode) only reached users
  who happened to run `brew upgrade`. The launcher's 6-hour check now also
  updates the launcher itself: verified download from the release (sha256
  sidecar), atomic swap with the previous version kept as backup, seamless
  relaunch — the running stack is untouched and outlives the handover. Dev
  builds never self-update; every failure path leaves the current install
  running. From this version on, no one runs `brew upgrade` again.

### Fixed
- The first live docker→native migration failed while unpacking the Python runtime
  (its archive lists `bin/` symlinks before anything creates `bin/`; the unpacker
  now creates a symlink's parent directory like it always did for files) — and a
  failed takeover now discards its half-made data copy, so a later retry re-copies
  fresh instead of ever booting a stale snapshot. Docker data was and remains
  untouched throughout; the failed run cost ~10 minutes of downtime and rolled
  back cleanly.

## [0.8.3] - 2026-07-29

### Changed
- Native mode can now take over an existing Docker install: on its first run it
  COPIES your data out of the volumes (which are mounted read-only during the copy
  and left byte-for-byte untouched as the rollback), and stored local-model
  addresses translate automatically between the Docker and native worlds in both
  directions — migrating there and rolling back both work without re-entering
  anything.

### Changed
- Native mode (still behind its opt-in flag) grew up: the launcher now writes the
  gateway's no-request-logging config before it ever starts (the native equivalent
  of the container fix — a fresh native gateway was observed booting with logging
  on by default), watches both processes and restarts a crashed one (bounded — a
  crash loop reports instead of spinning), and detaches them so quitting the
  launcher no longer takes SmartBrain down with it.

### Changed
- The launcher can now run SmartBrain **without Docker** behind a hidden opt-in
  (`SMARTBRAIN_NATIVE=1` — deliberately not in the menu yet while it matures): it
  assembles an install from the pinned standalone-Python runtime, the release's
  wheelhouse, and the mirrored gateway binary — every download sha256-verified,
  installed offline into a versioned directory whose `current` pointer only flips
  on success, so a failed upgrade leaves the previous version running (a rollback
  the Docker path never had). Docker remains the default and is untouched.

## [0.8.2] - 2026-07-28

### Changed
- Groundwork for running SmartBrain WITHOUT Docker (no user-visible change today —
  inside the container everything behaves byte-for-byte as before): the app now
  detects where it's running, and natively defaults to loopback URLs for the
  gateway and local model servers, a per-OS user-data directory for the database,
  and a file-based version stamp. A new CI job boots the app natively on macOS,
  Windows, and Linux — using the exact standalone-Python runtime a future
  Docker-free install would ship — and walks the full first-run flow on each.

### Changed
- Self-improvement's learning is now repeatable: the critique call pins a low
  sampling temperature (identical evidence used to yield nothing on one run and a
  high-confidence finding on the next), and the critique now sees how the
  unsatisfying requests classify — plus clearer rules on when a pattern is a
  global preference versus a per-request-type strategy. Verified live: two
  identical probe runs now produce the identical finding.

## [0.8.1] - 2026-07-28

### Added
- **Settings → Self-improvement**: the framework's home in the app — switches for the
  self-review and the prompt optimizer, the full record of every improvement it has
  proposed, applied, kept, or reverted, and each learned strategy with its status.
  No more API-only toggles.

### Changed
- **Docs truth pass** across the user guide: the MLX-only embeddings path now documents
  the simple settled setup (an encoder model served directly on your MLX server) with the
  bundled Qwen3 shim as the fallback; MLX chat setup leads with a server app instead of
  `pip install`; the privacy page's "what leaves your machine" now includes web
  search/fetch; document **tags**, the chat **Trash**, **big-document** behavior,
  where scheduled output lands, the Web-search and Memory settings, and the whole
  self-improvement framework; plus an **Uninstall** section, README links to the
  changelog/contributing/security policy, and assorted small fixes.

## [0.8.0] - 2026-07-28

### Added
- **The Prompt Optimizer can now go live — with evidence, on trial, and visibly.**
  A shadow strategy earns activation only when it kept firing (8+ matched asks) AND
  the problem it targets persisted (a quarter or more of its matched turns going
  badly); activation is announced in the digest and starts a measured trial against
  that baseline — kept only if things measurably improve, auto-disabled (and
  announced) if they don't or get worse. One trial at a time across the whole
  framework, so every measurement stays attributable. When guidance shapes an
  answer it rides a tiny trailing note (the prompt cache is never touched) and the
  answer shows a quiet **"guided · …" chip** — hover it to read the exact steering
  sentence. Everything remains behind the optimizer switch, off by default.

### Added
- **Prompt Optimizer groundwork — shadow mode** (off by default:
  `PUT /api/selfimprove/optimizer {"enabled": true}`). When enabled, each incoming
  ask is bucketed by a zero-latency rule-based classifier (factual / multi-step /
  code / retrieval / ambiguous) and the self-review may learn a per-type steering
  strategy from flagged windows — but every strategy is **shadow**: it never touches
  a live prompt. Shadow strategies only count the turns they *would* have applied
  to, building the evidence the go-live gate (next phase) will judge them on before
  anything ever changes a real answer. All observation rows are content-free;
  strategy text is encrypted at rest; `GET /api/selfimprove/optimizer` shows
  everything learned.

### Added
- **The self-review now makes suggestions, not just fixes.** Two new detectors — both
  pure pattern-matching over your own messages, no model involved, and neither ever
  acts on its own:
  - **Routine spotting**: an ask you've typed 3+ times on a daily or weekly rhythm
    ("summarize my open tasks…") becomes a ready-made schedule **waiting for your
    approval in Activity** — approve it and it's created; ignore or decline it and it
    is never offered again.
  - **Knowledge gaps**: when several searches come back empty, the digest names the
    topics your knowledge base couldn't answer so you know what's worth adding.
  Both appear under a "Suggested:" section of the self-review digest and in the
  improvement ledger.

## [0.7.0] - 2026-07-27

### Security
- **The gateway no longer keeps a plaintext copy of your prompts.** Bifrost (the
  built-in model gateway) ships with request logging on by default, which had been
  writing every prompt and reply — chat, memory, knowledge content sent for
  embedding — into an unencrypted `logs.db` inside its data volume, alongside
  SmartBrain's encrypted database. Fixed at three layers: the stack now starts
  Bifrost with its logging store disabled at the source and **destroys any existing
  `logs.db` on startup**; the app itself re-enforces no-logging over the gateway's
  admin API at every startup and unlock (so installs still on an older stack
  definition are protected as soon as their app image updates — content capture
  stops immediately and historical rows are purged by the gateway's own 1-day
  retention cleaner); and the weekly fresh-install test now fails if a `logs.db`
  ever reappears. No user action needed beyond restarting SmartBrain.

### Added
- **SmartBrain now reviews and improves itself** (off by default — turn it on with
  `PUT /api/selfimprove {"enabled": true}`; a Settings toggle is coming). Three pieces:
  - **It finally measures itself**: every chat/agent turn records content-free speed &
    quality telemetry (latency, steps, degraded/step-budget outcomes, per-turn tokens),
    and stopping or regenerating an answer is recorded as implicit feedback.
  - **An 8-hour self-review** (while unlocked) scores Chat, Knowledge, and Tools from
    that telemetry — pure SQL over plaintext metadata — and surfaces a digest through
    the scheduled-updates feed ONLY when something needs attention; silence is normal.
  - **A careful improvement loop**: when a window is flagged, a LOCAL model (never a
    cloud one — your chats don't leave the machine, and only messages YOU wrote are
    used as evidence) may propose one durable preference. High-confidence findings are
    applied as a visible "(learned) …" memory fact, put on trial, measured against the
    dissatisfaction rate they were applied under, auto-reverted if things get worse
    (or if no evidence accumulates), and every applied/reverted change is announced in
    the digest. One change at a time, at most 10 learned facts, hand-deleting a fact
    in Settings → Memory permanently rejects it, and a reverted preference is never
    re-applied. Kill-switch and privacy gates fail closed.

## [0.6.9] - 2026-07-24

_This section rolls up everything shipped across tags 0.6.1-0.6.9; earlier practice left these under Unreleased after tagging._

### Added
- "Delete all…" now lives on the Chat page itself (next to the saved-chats picker, on
  desktop and phone) — behind a confirmation, and everything still lands in the Trash
  with 30 days to restore. The Trash itself stays in Settings → Account & Data.

### Fixed
- Chats and agent turns answer noticeably sooner on local models: the live clock in
  the system prompt was invalidating the model server's prompt cache every minute
  (measured 11% cache efficiency — the model re-read the entire conversation on
  almost every step, ~12 seconds of pure re-processing per agent step on a long
  context). The prompt head is now byte-stable and the current time rides a tiny
  trailing note instead, so the cached prefix survives across steps and turns.

### Fixed
- The assistant can no longer present an unrelated document as a finding: when it
  asks for a focused summary of a document that never mentions the focus topic, the
  result now says so plainly ("the focus does not appear anywhere in this document"),
  giving the model a clean basis to drop it — seen live when a "summarize the Tribeca
  doc" reply confidently included a document with zero Tribeca references. And a
  document-scoped search called with a title instead of an id now returns a corrective
  error instead of a silent empty result the model reads as "the document doesn't
  mention it."

### Added
- **MLX-only semantic search**: a new "MLX embeddings" local provider plus a bundled
  one-command server (`tools/mlx_embed_server/install.sh`) serve Qwen3-Embedding on
  Apple Silicon with correct pooling — chat servers like oMLX refuse this model class.
  Connect it under Settings → Local models, route the Embedding slot to
  `mlxe/qwen3-embedding-0.6b`, Reindex, and the whole stack runs without Ollama.
  The provider is embeddings-only by construction: it can never be offered a chat turn.

### Fixed
- Hundreds-of-pages documents are now FULLY searchable by meaning. Semantic coverage
  used to stop silently at ~256k characters (64 chunks) and ingest truncated files at
  1M characters — most of a big S-1 was invisible to meaning search. Both limits now
  cover ~4M characters (about a thousand dense pages), and they match, so every stored
  character is reachable. Big documents embed INCREMENTALLY in the background: adding
  one returns instantly, the indexer works between your chats and resumes exactly where
  it left off after a restart or re-lock, the "Indexing X of Y" count now stays honest
  until every chunk of every document is really done (it used to claim done after the
  first chunk), and renaming a huge document no longer stalls while it re-embeds.
- Upgrade safety: the database is checkpointed immediately after schema migrations —
  a migration left in the write-ahead log could make the database fail to reopen after
  an unclean stop (found while testing; never reported in the field).

### Added
- Chat gained a Refresh button — and refreshes itself whenever you return to the app —
  so a conversation continued on your phone appears on the desktop (and vice versa)
  without a page reload. Injected scheduled-update notices survive the refresh.

### Changed
- Activity's "Always allowed" list is collapsed by default (with a count), keeping the
  page focused on what needs your attention.

### Fixed
- "response exceeds channel limit" is gone from the phone: big responses (a grown
  Activity feed, a large document opened remotely) now stream over the encrypted
  connection in ordered parts and reassemble on the phone, instead of being refused
  once they outgrew a single message. Bounded at 8 MB per response; an older phone
  app keeps the old clean error until it updates.
- Phone screens no longer clip content off the right edge: a long unbreakable token
  (a URL in a schedule's output, a full source link in a search citation) or a wide
  control (the seven-tab Settings strip, a file picker) could silently inflate the
  page past the viewport — Info, Knowledge, every Settings page, and Help were all
  affected. Pages now stay at screen width everywhere: run output and snippets wrap
  long tokens, citation chips clip inside their pill, document tag rows flow to a
  second line, and Help's section nav scrolls in place. Audited: every route is
  clean at phone width, desktop layouts unchanged.
- A phone could keep running an old version of the app for hours after a release —
  even after deleting and re-adding it: the hosted origin served the app's HTML
  without a no-cache header, so iOS was allowed to treat stale HTML as fresh
  (and "Add to Home Screen" copies Safari's cached site data into the new app).
  The origin now forces a revalidation of the HTML on every launch, and the
  build's content-hashed assets are marked immutable so they cache forever safely.
- A paired phone stays connected instead of quietly dying after a short idle: the
  connection now sends a small keepalive every 20 seconds (idle NAT/firewall mappings
  were expiring in as little as 30 seconds, killing the path while the status still
  said "connected"), a dead path is noticed within 45 seconds and reconnects on its
  own, the retry budget doubled to about three minutes of patience (a phone radio
  waking from sleep needs more than three quick attempts), switching apps for up to
  three minutes no longer drops the session (was 15 seconds), and even after the
  retries give up, the next tap in the app restarts the connection by itself instead
  of failing until you find Retry.
- Citations under an answer now reflect what the assistant actually READ, not
  everything its searches merely surfaced: a broad question no longer sprays chips
  for every unrelated document in the knowledge base, the same document found by two
  searches shows one chip per page, and a document that was read keeps its precise
  page links instead of a redundant whole-document chip. Search-only answers still
  cite their snippet hits — there, the snippets were the evidence.
- Tool-using turns stopped taking ten minutes: the agent now stops asking for more
  tools the moment its gathered results reach the model's context budget and writes
  the answer (every extra round-trip past that point re-fed the model a prompt it
  couldn't hold — pure prefill waste), a "Writing the answer…" line shows during that
  final long call, and the background summary-tree builder now waits for five quiet
  minutes before touching the local model — its 30-second chunk calls were making
  chats queue behind them on oMLX's single request slot.

### Added
- **Tags on documents and vaults**: label anything in Knowledge with your own tags
  (a Tags button on every document row and vault card — comma-separated, up to 20).
  Tags show as chips; clicking one filters both lists to that tag, and keyword +
  Best search match tags instantly with no reindex. Tags are encrypted like
  everything else, survive renames, never travel in a shared vault export, and
  can't be put on an imported vault's copies (a vault update would overwrite
  them — Detach first to make the copy yours).
- **Chat trash**: deleting a chat — or every chat at once with the new "Delete all
  chats" action in Settings → Account & Data — now moves it to a Trash instead of
  destroying it. Trashed chats disappear from every list but stay restorable for
  30 days from the new Trash card, which shows when each was deleted and how long it
  has left; after that the scheduler purges them for good, or "Empty trash" does it
  immediately.
- **Info page**: schedule output moved out of Schedules into a new Info page — in the
  sidebar and, on a phone, its own bottom tab. The All tab shows every run newest-first;
  a tab per schedule shows just that schedule's output. Schedules keeps Items + Create,
  and "Run now" points at Info for results. (New scheduled-run notices still appear in
  Chat exactly as before.)
- Book-scale documents, summarized instantly: every document now gets a background
  **summary tree** (chunk summaries reduced into one whole-document summary), built a
  piece at a time by the scheduler — encrypted like everything else, resumable, yielding
  to your live chats, and shown as "Preparing instant summaries — X of Y" on the
  Knowledge page. Asking Chat to summarize becomes an instant cached lookup at any size;
  a still-building document answers from what's covered and says so; focus questions
  ("summarize the fees") run over the stored tree in seconds. A new **Document
  summaries** slot in Model routing lets a big-context model build trees fast while the
  local model stays the private default; `kb_search` can now search INSIDE one document
  (the right way to find one fact in a thousand pages); and tool-using turns get two
  more steps now that an exhausted budget degrades to an answer.
- Web tooling that meets what users expect of a modern assistant:
  **web pages read as articles** (fetches now return clean extracted prose + title via
  the same reader ingestion uses, instead of raw HTML soup); **pluggable search
  providers** — SearXNG (self-hosted), Brave, and Tavily (bring-your-own keys, stored
  encrypted) with DuckDuckGo always anchoring the fallback chain, configured on a new
  Settings → Web search page; a **one-step `web_research` tool** that searches, then
  fetches and extracts the top pages (one per site, bounded) so a research question
  no longer burns the step budget page by page; and **live tool activity in Chat** —
  "Searching the web… ✓ / Reading a page…" narrated in place of the silent thinking
  dots while the assistant works.

### Fixed
- A bot-blocked website can no longer convince the assistant it has "no web access":
  page fetches now send the full browser-consistent header set (many WAFs 403 a
  browser User-Agent that arrives with a bare client fingerprint), and when a site
  still refuses, the error fed back to the model says exactly that — this one site
  refused, web access works, try a different result — instead of a bare HTTP status
  that small local models read as a dead internet and give up on.
- Huge documents no longer defeat the budget rescue: the recovery answer is now
  built from a prompt REBUILT to fit the model's context (the question, the first
  tool result, and the newest work — a 170k-character document had overflowed a
  32k-token model so badly that the rescue call itself failed), and reading a
  document several times larger than the context now says so in the result and
  points the model at summarize_document, which chunks and covers the whole file.
- "step budget exhausted" can no longer be an entire chat reply: when the assistant
  runs out of tool steps mid-task it now writes a real answer from everything it
  already gathered (saying what it couldn't finish); document reading no longer
  starves the budget — a timidly small page request is raised to the largest window
  the model's context fits, so long documents take one or two reads instead of five —
  and the read tool now points models at summarize_document for whole-document
  overviews.

## [0.6.0] - 2026-07-21

### Added
- All docs media regenerated in the new design, dark theme throughout: the 11
  quickstart GIFs (with their reduced-motion posters) and the five guide screenshots
  are re-shot on the redesigned app — sidebar shell, message rows, chips, modals. The
  recorder now pins the dark theme explicitly and its storyboards follow the new
  surfaces (settings tabs, the icon Send button, chip citations and badges, and the
  document viewer that is now a true modal).
- An accessibility and performance sweep, verified by axe across every route in both
  themes and viewports (now zero violations): the app version, composer hint, and
  mobile tab labels meet AA contrast; light-theme green/red are retuned so status
  chips pass on their tinted backgrounds (enforced forever by new contrast tests);
  dialogs return focus to what opened them; task checkboxes are labeled for screen
  readers and — with all native toggles — bigger and theme-colored; Help's scrollable
  code blocks are keyboard-reachable; and the sidebar no longer pops in after load
  (layout shift 0.157 → 0.002 on a cold open).
- A quiet motion pass: dialogs, the mobile More sheet, toasts, and the "Jump to latest"
  pill now ease in (120–200ms, transform/opacity only) while dismissal stays instant;
  tab hovers stopped snapping. Every animation — including the two smooth-scroll
  jumps — honors `prefers-reduced-motion`.
- One brand mark everywhere: a new generator (`tools/brand/make_icons.py`) derives every
  raster asset from the one mascot source — PWA icons with a **properly maskable** variant
  (no more white bleed or clipped wordmark on Android), an apple-touch icon, a real
  favicon, a face-tight header mark that's legible at 30px, and the macOS Dock icon.
  The app manifest moved to the design palette (#121212) and gained the 192px maskable
  size; iOS gets proper standalone metas (app title, translucent status bar). The
  landing page now runs the app's design system — Inter, the token palettes in both
  themes, the real mark instead of an abstract gradient chip, icon pillars instead of
  emoji — plus a favicon and social-preview (Open Graph) card, and a fix for a
  pre-existing bug where the long install commands forced the whole page to scroll
  sideways (phones included). The tray monogram is untouched.
- Settings and onboarding joined the system: the settings section tabs are the same
  pill strip as everywhere else (one scrollable row on phones), and the last bare
  "Loading…" texts — root dispatcher, Settings, Usage, and the Setup busy state —
  became the shared spinner.
- Planner, Schedules, and Email joined the system: one tab style everywhere, real
  empty states, spinners instead of bare "Loading…", and the email reader now opens
  in the same focused modal as every other dialog.
- Knowledge on the same system: document rows with quiet actions, search-result
  citations as chips, vault identity as proper Subscribed/Public chips with monospace
  fingerprints, real empty states — and the document viewer is now a true focused
  modal that still opens at the cited passage.
- Chat reads like a modern assistant: full-width labeled message rows on a reading
  measure (bubbles retired), visible thinking/streaming states, a redesigned composer
  with an always-visible Stop during generation, citation chips, an inline approval
  card, and a "Jump to latest" pill with scroll-aware auto-follow.
- Approvals got their proper surface: pending actions in Activity render as deliberate
  "Action Cards" — tool, plain scope lines, a reversibility badge, and a clear
  Approve / Deny / Always-allow hierarchy (red is reserved for irreversible actions).
- The app shell is rebuilt: a desktop sidebar rail (icons, labels, badges) with a slim
  top strip carrying an "Encrypted · On-device" trust chip, and on phones a bottom tab
  bar in the thumb zone (Chat · Knowledge · Activity · More) with proper notch/home-bar
  safe areas — replacing the wall-of-links top bar on every screen size.
- A shared component library (Modal, Tabs, Field, Toast, EmptyState, Spinner, Chip,
  ActionCard): one modal shell now backs every dialog — starting with the app-wide
  Confirm — ending the era of three competing overlay implementations.
- A real icon system: a vendored Lucide subset (ISC) rendered by one Icon component
  — the emoji-as-icons era (\U0001F313\u2600\U0001F319\u22EF\u2715) is over; icons inherit theme color and weight.
- The app's typeface is now Inter Variable, self-hosted (latin subset, 97 KB, OFL
  license shipped alongside) — no font CDN, matching the privacy posture.
- A new visual foundation — "calm precision-minimal": tonal dark (#121212 family) and
  warm-white light themes with a single muted-teal accent, a real type/spacing/radius
  token system, a visible keyboard-focus ring everywhere, and WCAG-AA contrast enforced
  by a permanent test in both themes. Every page reskins; nothing moves yet.
- Public vaults: publish a vault openly, subscribe to one by URL with trust-on-first-use
  publisher pinning, check for and apply updates (the pinned publisher enforces every
  update), opt in to scheduled auto-updates, and the finishing UI surfaces plus an MCP
  provenance door (#76, #77, #78, #79, #80).
- The official **example vault**: the user guide itself, published as a public vault at
  `https://smartbrain.securecloudgroup.com/vaults/smartbrain-docs.sbvault` — subscribe by
  URL to try Vaults in one paste — plus the builder that mints and updates it
  (`tools/example-vaults/build.py`).

### Changed
- Docs & landing truth pass: the privacy page discloses vault-subscription fetches, the
  landing page gains a Vaults pillar and drops an overstated "nothing is sent to us, ever"
  (the optional phone-access relay exists), the MCP page notes imported-content provenance
  labels, and getting-started notes the paired phone updates itself.
- Vaults are now a first-class guide: **docs/04-vaults.md** ("Share knowledge with Vaults" —
  it also headlines the in-app Help) instead of a subsection buried in Using SmartBrain_3000;
  the later guides renumbered 05–09 with every cross-link updated.

### Fixed
- One dead local-provider URL can no longer blank the whole model list: the gateway
  catalog call is now time-bounded (it previously inherited the pooled client's 60s
  timeout), and when the catalog fails, `/api/models` degrades to directly-probed local
  models — Chat keeps working on local models and shows an honest "couldn't load the
  model list / degraded" notice instead of a misleading "add a key" empty state. Save
  failures on the Model-routing page now land inline next to their button instead of a
  vague connection error at the page bottom.

**Migration:** subscribing to a public vault records its upstream source in new encrypted
columns — additive and forward-only, applied automatically on first launch (#77).

## [0.4.6] - 2026-07-16

### Added
- Deterministic citations — source chips on chat answers that drew on your knowledge (#68).
- Chat controls: stop, copy, regenerate, and rename a message (#70).
- Public-vaults groundwork: an "open" mode in the vault format (the transport half of
  public vaults), imported-document protections that must predate any update path, and
  SSRF-guarded vault-fetch transport helpers (#72, #74, #75).

### Changed
- Docs: a Windows dev-VM runbook, and refreshed onboarding GIFs including the Vaults
  clip (#69, #71).

### Fixed
- Vault updates preserve unknown body fields via read-modify-write (#73).

## [0.4.5] - 2026-07-15

Re-release of v0.4.4 on the same commit (a release-pipeline retag); no source or behavior
changes.

## [0.4.4] - 2026-07-15

### Changed
- CI: a Linux install smoke test runs the documented cold start on every release and as a
  weekly drift canary (#67).

### Fixed
- Vaults: visible membership, guided add, inline errors, and a labeled export — from
  live-test findings (#65).
- A corrupted vault container now returns a clean 400 instead of an unhandled 500 (#66).

## [0.4.3] - 2026-07-15

### Changed
- Packaging manifests bumped to v0.4.2; the release job no longer fails when Actions
  can't open the packaging-bump PR (#62, #63).

### Fixed
- Three UX papercuts: tool-leak guidance, inline save feedback, and Homebrew update
  docs (#64).

## [0.4.2] - 2026-07-15

### Added
- Installer polish: auto-launch after install, a download notification, a real app icon,
  and fewer privacy prompts (#61).

### Changed
- Bifrost mirrored to GHCR and pinned to v1.6.4 for a single-registry install (#59).

### Security
- Vault name no longer leaks in the plaintext manifest and is preserved on import;
  first-run polish from end-to-end testing (#58).

### Fixed
- Local models reach routing again on bifrost v1.6.4 — they had been blocked by its
  SSRF guard (#60).

## [0.4.1] - 2026-07-15

**Migration:** the Docker layout moved from bind-mounts to named volumes to fix a
Linux-only install crash-loop. Your data now lives in named volumes; back up with the
in-app encrypted backup, and note that uninstall never touches the data volumes (#57).

### Added
- Download/landing page for the app, with an opt-in Caddy overlay for the RTC signaling
  node (#51).

### Changed
- One-command install packaging: a Homebrew cask plus winget and Scoop manifests, with a
  release workflow that auto-bumps Homebrew/Scoop/packaging on a version tag and
  auto-submits winget updates (#50, #52, #53).
- Docs refreshed for the v0.4.0 install and Vaults, with a new-user test plan (#54, #55).

### Fixed
- The launcher finds Docker when started from a Finder-launched app (#56).
- Named volumes replace bind-mounts so a fresh Linux install no longer crash-loops on
  volume ownership; plus launcher hardening and doc corrections (#57).

## [0.4.0] - 2026-07-13

The knowledge release: a real knowledge base with citations, shareable Vaults, chat
document tools, schedules, and prebuilt-image distribution.

**Migration:** first launch creates the new vault tables and a per-document ownership
column automatically — forward-only and data-safe (schema migrations 20–22).

### Added
- Knowledge search that is fast and honest — hybrid BM25 + vector ranking over an
  in-memory index, with citations that open a document at the passage that matched
  (#38, #40, #48).
- Ingest Word, PowerPoint, and Excel files, with deduped, non-blocking uploads and a
  background indexer that actually drains (#42, #48).
- Vaults: group documents into collections and export or import a sealed vault to share
  knowledge with another person (#44, #48).
- Chat can read, list, and summarize a document of any length, and save a note or
  summary back into your knowledge (#34, #35, #36).
- Chat renders assistant markdown instead of raw `###` and `**` (#37).
- Schedules: Output / Create / Items tabs with PWA parity, approval-gated chat tools to
  read and manage schedules, and fired-schedule output delivered into the chat window
  (#24, #26, #27).
- A dedicated Agent-tasks model route, with a cold-load timeout fix (#25).

### Changed
- Distribution: prebuilt multi-arch images published to GHCR with a pull-based compose,
  so install no longer builds from source (#46, #49).
- `.dockerignore` keeps the vault and secrets out of image builds (#22).
- Dependency and toolchain maintenance: the web build on Node 22, Vite 8 (Rolldown),
  marked 18, pinned Dependabot toolchain majors, and the signaling image on Python 3.14
  (#11, #12, #15, #17, #19, #20, #21).

### Fixed
- Chat can keyword-search any saved document, not just the semantic index (#29).
- Reindex waits out a cold local embedding model instead of failing the first try (#32).
- Local-model calls are serialized, fixing oMLX "model is busy" chat failures (#33).
- PDF uploads are no longer titled from a stale `.docx` filename in metadata (#30).
- Scheduled output appears in the chat window; the separate tab was removed (#28).
- In-app Help deep links resolve to the right section and heading (#14).
- Gmail OAuth completes over HTTPS via a loopback redirect helper (#23).
- End-to-end PDF ingest, indexing, and search test coverage (#31).

## [0.1.0] - 2026-07-09

First tagged public release. SmartBrain_3000 is a local-first, single-user, self-hosted
encrypted AI assistant that runs in Docker on your own machine.

### Added
- CI (GitHub Actions): backend ruff + pytest, hermetic installer tests, web
  svelte-check + vitest + build, and a build + test of the shipped Docker image on
  every PR — the required status checks that guard `main`.
- Dependabot: grouped weekly updates for pip, npm, GitHub Actions, and Docker.

### Changed
- Base image bumped to `python:3.14-slim`; web dev toolchain upgraded to Vite 7.

### Security
- Desktop-local fence extended to device-enrollment and MCP-token endpoints so a
  paired remote device cannot self-enrol/revoke devices or read/rotate the MCP token.

### Fixed
- Restore keeps the displaced database's WAL and quarantines future-schema backups;
  deterministic chat-message ordering; installer gating, custom-port, and failed-update
  recovery; assorted UX/accessibility and docs accuracy fixes.
