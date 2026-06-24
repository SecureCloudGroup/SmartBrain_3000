# Privacy & security

SmartBrain_3000 is built to keep your data on your machine and under your
control. Here's the model in plain terms, including the real world limits.

## What protects your data

- **Local-first.** Everything runs in Docker on your machine. There's no SmartBrain
  cloud, no account server, and no telemetry.
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
  you turn it on, your Desktop dials out to a signaling node you run to broker the
  connection; it carries only connection metadata, never your data (the link is
  end-to-end encrypted). See [Remote access](07-remote-access.md).
- **Nothing else.** Beyond the above, the app makes no outbound calls.

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
  key at rest — are deliberate. See [Design limits](08-design-limits.md) for the
  full list and the reasoning.

## Reporting an issue

Found a security problem? Please report it privately — see
[`SECURITY.md`](https://github.com/SecureCloudGroup/SmartBrain_3000/blob/main/SECURITY.md)
(email `info@securecloudgroup.com`). Don't open a public issue for vulnerabilities.
