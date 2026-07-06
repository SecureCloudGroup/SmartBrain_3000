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
- CI (GitHub Actions): backend ruff + pytest, hermetic installer tests, and web
  svelte-check + vitest + build on every PR — the required status check for `main`.
- Dependabot: grouped weekly updates for pip, npm, GitHub Actions, and Docker.

### Security
- Desktop-local fence extended to device-enrollment and MCP-token endpoints so a
  paired remote device cannot self-enrol/revoke devices or read/rotate the MCP token.

### Fixed
- Restore keeps the displaced database's WAL and quarantines future-schema backups;
  deterministic chat-message ordering; installer gating, custom-port, and failed-update
  recovery; assorted UX/accessibility and docs accuracy fixes.
