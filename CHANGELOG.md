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
