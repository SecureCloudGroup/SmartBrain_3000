# Changelog

All notable changes to SmartBrain_3000 are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Keep an `## [Unreleased]` section at the top; on each release, rename it to the version +
date and start a fresh `## [Unreleased]`. Call out **breaking changes** and any migration
step explicitly — SmartBrain runs forward-only, data-safe migrations, but users still need
to know when a release changes behavior.

## [Unreleased]

### Added
- Chat gained a Refresh button — and refreshes itself whenever you return to the app —
  so a conversation continued on your phone appears on the desktop (and vice versa)
  without a page reload. Injected scheduled-update notices survive the refresh.

### Changed
- Activity's "Always allowed" list is collapsed by default (with a count), keeping the
  page focused on what needs your attention.

### Fixed
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
