# Privacy & security

SmartBrain_3000 is built to keep your data on your machine and under your
control. Here's the model in plain terms, including the real world limits.

## What protects your data

- **Local-first.** Everything runs on your machine — the app, the model gateway, and your
  database — with no account server and no telemetry. The only SmartBrain-operated service
  is the optional, content-blind signaling node for remote phone access — off by default
  (see below).
- **Verified at install.** The desktop app assembles SmartBrain from a pinned Python
  runtime, the release's own packages, and the model gateway. Every download is checked
  against a known checksum before it is used, and a version only becomes the live one once
  all of it succeeded — so a failed or tampered download leaves the previous version
  running rather than replacing it.
- **Encrypted at rest.** Your knowledge, chats, tasks, memories, email
  credentials, and provider keys are encrypted (AES-256-GCM) in the local
  database. The encryption key is derived from your passphrase (a slow, modern
  key-derivation function) and also wrapped under your Recovery Key.
- **Locked by default.** On startup the app holds no key. Unlocking loads it into
  memory for the session; **Lock** drops it again.
- **Loopback-only.** The app binds to `localhost` and validates the request host,
  which blocks DNS-rebinding attacks from web pages you visit. It isn't exposed to
  your network.
- **Approval gates.** The assistant can read freely but can't change data or reach
  out (send email, delete, fetch the web) without your explicit approval, with an
  extra confirm for irreversible actions. Everything it attempts is audited.
- **Credential firewall.** Tools and connected MCP clients act on your behalf but
  never receive your raw keys or tokens.
- **Web-fetch guard.** The web-fetch tool refuses private/internal addresses and
  doesn't follow redirects into them (anti-SSRF).

## What leaves your machine (and when)

- **Cloud model calls.** If you use an OpenAI/Anthropic/Google model, your prompts
  and the content you send go to that provider. Use a **local model** (Ollama/MLX)
  to keep everything on-box.
- **Email.** If you connect Gmail, the app talks to Google's APIs to read/send your
  mail — over a loopback OAuth flow, with your own OAuth client.
- **Remote access (only if you enable it).** Phone access is **off by default**. When
  you turn it on, your Desktop dials out to a content-blind signaling node to broker the
  connection — the SecureCloudGroup-hosted node (`rtc.securecloudgroup.com`) by default,
  or your own via `SMARTBRAIN_SIGNALING_URL`. It carries only connection metadata, never
  your data (the link is end-to-end encrypted). See [Remote access](08-remote-access.md).
- **Public vaults (only if you subscribe).** Subscribing to a vault by URL — and any
  **Check for updates** or scheduled auto-update on it — fetches the vault from the host
  in that URL (public internet hosts only, never localhost or LAN addresses). Recurring
  checks happen only if you turned auto-update on.
- **Web search & fetch (only when the assistant uses those tools).** A web search goes
  to the engine you chose — **DuckDuckGo by default**, or your own Brave/Tavily key or
  self-hosted SearXNG (Settings → Web search) — and a web fetch goes to that page's
  host. Dangerous fetches are approval-gated and SSRF-guarded; nothing is searched or
  fetched outside a turn that calls for it.
- **Update checks — by the desktop app, not by SmartBrain.** Every six hours the menu-bar
  launcher asks GitHub whether a newer release exists, and downloads it from GitHub if so.
  That request carries no identity and nothing about you or your data; it is the same
  public release page anyone can open. SmartBrain itself makes no such call — it hears
  about a waiting update from the launcher over the local heartbeat they already exchange.
- **Nothing else.** Beyond the above, the app makes no outbound calls. Self-improvement
  (if you enable it) is fully local by design: its reviews and learning run on your
  machine against a local model only — it never sends your activity anywhere.

## Honest limits

- **Your host machine.** If your computer or OS is compromised, local encryption
  can't fully protect a running, unlocked session. Keep your machine secure.
- **No recovery backdoor.** Lose both your passphrase and Recovery Key and the data
  is unrecoverable — by design. Keep the Emergency Kit safe and offline.
- **Prompt injection.** Content the assistant reads (web pages, emails, documents)
  could try to manipulate it. The approval gates are the backstop: nothing
  consequential happens without your sign-off.
- **Single-user, personal scale.** SmartBrain_3000 is built for one owner on one
  machine. Several boundaries — one global unlock, a single-writer database, no
  key at rest — are deliberate. See [Design limits](09-design-limits.md) for the
  full list and the reasoning.

## Reporting an issue

Found a security problem? Please report it privately — see
[`SECURITY.md`](https://github.com/SecureCloudGroup/SmartBrain_3000/blob/main/SECURITY.md)
(email `info@securecloudgroup.com`). Don't open a public issue for vulnerabilities.
