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
- Public vaults: publish a vault openly, subscribe to one by URL with trust-on-first-use
  publisher pinning, check for and apply updates (the pinned publisher enforces every
  update), opt in to scheduled auto-updates, and the finishing UI surfaces plus an MCP
  provenance door (#76, #77, #78, #79, #80).

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
