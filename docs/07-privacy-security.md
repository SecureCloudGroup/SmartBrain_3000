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
  extra confirm for irreversible actions. Everything it attempts is audited. A parked
  action expires after an hour, and locking cancels every pending one.
- **Credential firewall.** Tools and connected MCP clients act on your behalf but
  never receive your raw keys or tokens. On top of that, any tool setting *named*
  like a credential — `api_key`, `token`, `password`, `passphrase`, `secret` — is
  stripped before an action is shown to you or written to the audit log. That match
  is on the name, not the content: a secret you type into ordinary text, like the
  body of an email, isn't recognised as one, so treat free text as visible.
- **Web-fetch guard.** The web-fetch tool refuses private/internal addresses and
  doesn't follow redirects into them (anti-SSRF).
- **The model gateway keeps no transcript.** Bifrost ships with request logging on, which
  would write every prompt and reply to an unencrypted file beside your database.
  SmartBrain starts it with that store disabled, destroys any log database it finds on
  startup, and re-asserts the setting at every start and unlock.

### What is encrypted, and what isn't

Content is encrypted; the small amount of bookkeeping needed to find and schedule things is
not. Being precise about the line matters more than claiming everything:

- **Encrypted** (AES-256-GCM, under your key): documents and their text, chat messages,
  task titles, notes and tags, memories and your profile, schedule titles and prompts,
  scheduled-run output, provider and search API keys, the Gmail token, the MCP token,
  and the arguments and results recorded in the audit log.
- **Not encrypted, outside the database:** the speech model files (about 141 MB under
  the data folder's `models/`) — public model weights, nothing of yours — and, if you
  run the Mic & speaker check, its last three-second test recording, kept beside them
  so a bad capture can be diagnosed from the audio itself.
- **Not encrypted** (plaintext metadata in the same local database): timestamps, a
  schedule's cadence and next-run time, a task's due date, priority and status, which
  model is routed to what, and, in the audit log, the tool's name, its risk tier, what you
  decided and whether it worked. Someone with your disk learns *that* a tool ran and
  when — not what it was given or what came back.

## What leaves your machine (and when)

- **Cloud model calls.** If you use an OpenAI/Anthropic/Google model, your prompts
  and the content you send go to that provider. Use a **local model** (Ollama/MLX)
  to keep everything on-box. Five jobs use a model, and each is routed separately
  under Settings → Model routing: chat, scheduled runs, embeddings for search,
  background document summaries, and Neural Interface cards that ask a model. Point
  any of them at a cloud provider and that job's content goes there — the embedding
  and summary slots are the easy ones to overlook, because they run over your
  documents in the background rather than in front of you. (Two Neural Interface
  jobs refuse to be cloud-routed at all: a card's in-pipeline language-model step
  and local self-repair run on a local model or not at all.)
- **Claude Code models.** The `claudecode/*` models send the conversation to Anthropic
  under your own Claude sign-in, exactly like a cloud provider — configured beside the
  local servers, but not local in the privacy sense. SmartBrain drives the `claude`
  command with an empty tool set and session persistence off, so the CLI itself cannot
  read files, run commands, or keep the conversation on disk; the conversation text
  still goes to Anthropic. See [Connect a model](02-models.md#claude-code-sends-your-chats-to-anthropic).
- **Email.** If you connect Gmail, the app talks to Google's APIs to read/send your
  mail — over a loopback OAuth flow, with your own OAuth client.
- **Remote access (only if you enable it).** Phone access is **off by default**. When
  you turn it on, your Desktop dials out to a content-blind signaling node to broker the
  connection — the SecureCloudGroup-hosted node (`rtc.securecloudgroup.com`) by default,
  or your own via `SMARTBRAIN_SIGNALING_URL`. It carries only connection metadata — the
  offers and answers that name your phone's and Desktop's IP addresses, and when they
  connect — never your data (the link is end-to-end encrypted), and it keeps no log of
  who connected. See [Remote access](08-remote-access.md) for exactly what it sees.
- **Public vaults (only if you subscribe).** Subscribing to a vault by URL — and any
  **Check for updates** or scheduled auto-update on it — fetches the vault from the host
  in that URL (public internet hosts only — the network guard blocks localhost and LAN
  everywhere, with exactly one carve-out: the MCP servers you configure yourself, below).
  Recurring checks happen only if you turned auto-update on.
- **Website feeds (only the ones you subscribed).** Each RSS/Atom feed you follow is
  fetched about every six hours, directly from this machine, from the public URL you
  pasted — and from nowhere else. **Refresh** on a feed's row fetches right now.
  Unsubscribing stops the checking.
- **Neural Interface cards (only the sources you approved).** Each card fetches its
  own source — the exact address you saw frozen on the approval card — at the cadence
  you set, through the same network guard as feeds. Nothing else is contacted: a
  card watching a page fetches that page; a card watching an image fetches that
  image; cards built on your schedules, your knowledge, or other cards fetch nothing
  at all. A card with a credential sends it only to the host it was entered for,
  only over HTTPS, and never follows a redirect while carrying it.
- **Card creation, when no vetted source fits (a bounded search).** While you are
  *creating* a card and nothing in the vetted catalog matches, the build engine
  searches **your own words** through the web-search engine below and reads up to
  four of the result pages — once, through the same network guard, with safe
  search forced on — to show you candidates with evidence from each page. That
  pre-read is part of the search; the address a card fetches *recurringly* is
  still only the one you tap or paste. No search happens when a vetted source
  matched, when you pasted a URL, or after the card exists.
- **The card template library (only if you connect one).** A connected library is
  checked at the URL you pinned about once a day (plus your manual "Check now").
  The check downloads the signed template pack; nothing about you or your cards is
  sent — it is the same file anyone can fetch.
- **Your MCP servers (the deliberate localhost/LAN exception).** The app's network
  guard refuses private and internal addresses everywhere — with one exception,
  stated plainly: an MCP server **you** add on Settings → Connections (MCP) may be
  a loopback or LAN address, because pointing SmartBrain at your own server is the
  entire point. The gates: the address is typed by you in a desktop-only act and
  frozen; each card calls exactly one tool with arguments you approved and that
  are frozen too; redirects are refused; nothing a model writes can ever reach or
  change that address. What travels is the tool call and its reply — your database
  credentials stay inside your own server process and are never held by SmartBrain.
- **Frontier card repair (only per-card, only if you opted in).** If you switch on
  "Frontier repair via Claude" for a specific card *and* Claude Code is connected,
  a card that keeps failing sends Anthropic a bounded repair request: the card's
  goal, its data mappings, the failure class, and a short excerpt of the failing
  data — never your knowledge, never credentials, and for cards built on internal
  sources not even the excerpt. The reply is only ever a parked proposal; nothing
  is applied without your review.
- **Web search & fetch (only when the assistant uses those tools).** A web search goes
  to the engine you chose — **DuckDuckGo by default**, or your own Brave/Tavily key or
  self-hosted SearXNG (Settings → Web search) — and a web fetch goes to that page's
  host. Dangerous fetches are approval-gated and SSRF-guarded; nothing is searched or
  fetched outside a turn that calls for it.
- **The speech model, once.** At launch, SmartBrain fetches the Whisper dictation model
  (about 141 MB, four files) from the public Hugging Face repository
  `Systran/faster-whisper-base`, and checks every file against a pinned hash. The request
  carries no identity and nothing about you; it happens once, and never again while the
  files are in place. Air-gapped or network-forbidden deployments set
  `SMARTBRAIN_NO_VOICE_PREFETCH=1` to skip it (dictation is then unavailable until a
  voice server is configured). **Your voice itself never leaves your machines**: dictation
  is transcribed on the Desktop, spoken replies use your device's own voices, and a
  phone's audio travels only the end-to-end encrypted link to your Desktop.
- **Update checks — by the desktop app, not by SmartBrain.** Every six hours — or when you
  choose **Check for updates** in its menu — the menu-bar
  launcher asks GitHub whether a newer release exists, and downloads it from GitHub if so.
  That request carries no identity and nothing about you or your data; it is the same
  public release page anyone can open. SmartBrain itself makes no such call — it hears
  about a waiting update from the launcher over the local heartbeat they already exchange.
- **Nothing else.** Beyond the above, the app makes no outbound calls. Self-improvement
  (if you enable it) is fully local by design: its reviews and learning run on your
  machine against a local model only — it never sends your activity anywhere.

Two things that sound like they'd leave and don't:

- **MCP (inbound).** A connected desktop AI client reads your knowledge over a
  loopback connection on your own machine. SmartBrain sends nothing outward for it.
  What that *client* then does with what it read is its business, not SmartBrain's —
  see [MCP](05-mcp.md). (The *outbound* direction — your own MCP servers as card
  sources — is listed above, because that one does connect out.)
- **Publishing a vault.** Export writes a file to your disk. Nothing is uploaded;
  where it goes afterwards is entirely your doing. See [Vaults](04-vaults.md).

## Honest limits

- **Your host machine.** If your computer or OS is compromised, local encryption
  can't fully protect a running, unlocked session. Keep your machine secure.
- **No recovery backdoor.** Lose both your passphrase and Recovery Key and the data
  is unrecoverable — by design. Keep the Emergency Kit safe and offline.
- **Prompt injection.** Content the assistant reads (web pages, emails, documents,
  feed items) could try to manipulate it, and no model is immune. What SmartBrain does
  about it: outside text is marked as *data, not instructions* at the moment it enters
  the model's context, and the model is told so; the approval gates are the backstop —
  nothing consequential happens without your sign-off, approval cards show every
  argument in full, an injected web address always parks (sites are allowed one at a
  time, exactly), and unattended runs can never write a memory or a schedule on a
  standing grant; replies cannot load remote images. Hidden text in files (white-on-white
  PDF text, hidden spreadsheet rows) is still read — that is why the gates exist.
- **Single-user, personal scale.** SmartBrain_3000 is built for one owner on one
  machine. Several boundaries — one global unlock, a single-writer database, no
  key at rest — are deliberate. See [Design limits](09-design-limits.md) for the
  full list and the reasoning.

## Reporting an issue

Found a security problem? Please report it privately — see
[`SECURITY.md`](https://github.com/SecureCloudGroup/SmartBrain_3000/blob/main/SECURITY.md)
(email `info@securecloudgroup.com`). Don't open a public issue for vulnerabilities.
