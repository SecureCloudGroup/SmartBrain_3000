# Security Policy

SmartBrain_3000 is a **local-first** application: your data and credentials stay
on your own machine, and secrets are encrypted at rest. Outbound calls happen only
for: the AI providers you configure; Google's APIs if you connect Gmail; the
web-research tools (web search via DuckDuckGo, fetch a page, ingest a URL) **when
the assistant proposes them and you approve each call** — these are blocked by an
SSRF guard from reaching private/internal addresses; and, if you enable remote phone
access (**off by default**), a content-blind signaling node — the SecureCloudGroup-hosted
broker (`rtc.securecloudgroup.com`) by default, or your own via `SMARTBRAIN_SIGNALING_URL`
— which sees connection metadata only. We take security seriously and welcome
responsible disclosure.

## Trust model — what "content-blind" does and does not cover

Being explicit about the edges, because "the node sees connection metadata only"
is true of the broker protocol and still leaves things worth knowing:

- **The phone app is served from the same origin as the broker.** When you pair a
  phone it loads the web app from the signaling node's domain, and its pairing
  credential is stored by that origin. The broker never sees your traffic — it is
  relayed end-to-end encrypted and your phone pins the Desktop's key — but whoever
  controls that host serves the code doing the pinning. A compromised node (or its
  operator) could serve modified JavaScript and read the stored credential. Running
  your own node via `SMARTBRAIN_SIGNALING_URL` moves that trust to you; using the
  Desktop only avoids it entirely.
- **A desktop id is a routing key, not a secret.** It travels in the pairing QR and
  in every phone hello. The broker refuses to re-register an id that already holds a
  live socket, but you should not treat one as confidential.
- **Releases are signed, but the app is not OS-signed.** Each release's checksum
  file carries an Ed25519 signature, and the public key is compiled into the
  launcher, so an update that was not produced by us is refused rather than
  installed — a checksum alone could not do this, since it ships from the same
  release as the file it describes. You can verify any release yourself with the
  stock minisign tool; see `docs/internal/release-signing.md`. What this does *not*
  do is make the binaries signed in the operating system's eyes: they are not, which
  is why the Homebrew cask clears the quarantine attribute (macOS would otherwise
  block a binary built in public CI) and why a browser download warns. Platform
  code-signing certificates are a paid, separate step and are on the roadmap.
- **Secret redaction matches argument names, not values.** Tool arguments named like
  credentials (`api_key`, `token`, `password`, `passphrase`, `secret`, …) are
  redacted before display and audit. This is defence in depth on top of the
  structural credential firewall — not content inspection. A secret you paste into
  free text (an email body, a note) is not detected as one.

## Reporting a vulnerability

**Please report security issues privately. Do not open a public GitHub issue
for a vulnerability.**

Email **info@securecloudgroup.com** with:

- a description of the issue and its impact,
- steps to reproduce (a proof-of-concept if possible),
- the affected version or commit, and
- any suggested remediation.

We aim to acknowledge reports within **3 business days** and to share a
remediation timeline after triage. Please give us a reasonable opportunity to
release a fix before any public disclosure.

## Scope

**In scope:** the SmartBrain_3000 application in this repository — backend, web
app, installer, the MCP server, and packaging.

**Out of scope:** vulnerabilities in third-party dependencies or services
(please report those upstream), and issues that require an already-compromised
host operating system or physical access to the user's machine.

## Supported versions

SmartBrain_3000 is in early development. Only the latest `main` is supported;
security fixes land there.
