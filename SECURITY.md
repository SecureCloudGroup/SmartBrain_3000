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
