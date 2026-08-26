# SmartBrain_3000 — user guide

SmartBrain_3000 is a personal AI assistant that runs on your own machine. Your chats,
documents, and credentials stay on-box, encrypted at rest under a passphrase only you
hold. It talks to a cloud AI provider only if you give it a key, and to nothing else
unless you turn that on.

These pages are the whole guide. They are also the app's **Help** page (`/help`) —
the app renders this same Markdown at build time, so the words are identical either way.

## Start here

If you are new, read three pages in order. It takes about fifteen minutes.

1. **[Getting started](01-getting-started.md)** — install the app, choose a passphrase,
   save your Recovery Key, and take the five-minute tour.
2. **[Connect a model](02-models.md)** — add a cloud provider key, or point SmartBrain at
   a local model already running on your machine.
3. **[Using SmartBrain_3000](03-features.md)** — what each area of the app does, and how
   the approval system keeps the assistant on a leash.

Everything after that is optional and can be read when you need it.

## The pages

| | Page | What's in it |
| --- | --- | --- |
| 1 | [Getting started](01-getting-started.md) | Requirements, install on macOS/Windows/Linux, first run, locking and unlocking, how updates install themselves, troubleshooting, uninstall. |
| 2 | [Connect a model](02-models.md) | Cloud provider keys (OpenAI, Anthropic, Google). Local models with MLX or Ollama, including one-tap connect. Model routing — which model does which job. Context length. Embedding models for semantic search. Voice: the on-device dictation engine and its one-time model download. |
| 3 | [Using SmartBrain_3000](03-features.md) | The tour: Chat and its tools, the approval tiers, Voice (dictate and listen), Knowledge, feeds (follow a website), Planner, Schedules, Info, Email, Memory, web search, self-improvement, Usage & cost, Activity. |
| 4 | [Share knowledge with Vaults](04-vaults.md) | Group documents into a vault, scope a search to it, share it as a sealed file, publish it publicly, subscribe to someone else's by URL and take verified updates. |
| 5 | [Connect external tools (MCP)](05-mcp.md) | Expose your Knowledge base read-only to a desktop AI client such as Claude Desktop or Cursor. |
| 6 | [Backup & recovery](06-backup-recovery.md) | Export your data as JSON, take an encrypted backup, restore one, change your passphrase, get back in with the Recovery Key, and start completely fresh. |
| 7 | [Privacy & security](07-privacy-security.md) | What protects your data, exactly what leaves your machine and when, and the honest limits. |
| 8 | [Remote access](08-remote-access.md) | Reach your assistant from your phone on Wi-Fi or cellular by pairing a device. Off by default. Plus the LAN/HTTPS path and self-hosting the signaling node. |
| 9 | [Design limits](09-design-limits.md) | The deliberate single-user, local-first scope choices — one unlock, one writer, no key at rest — and the reasoning behind each. |

## Find it fast

| If you want to… | Go to |
| --- | --- |
| Install it | [Getting started → Install](01-getting-started.md#install) |
| Fix "No models available yet" | [Connect a model](02-models.md) |
| Fix search showing only keyword results | [Connect a model → Embeddings](02-models.md#embeddings-for-knowledge-search) |
| Change which model does which job | [Connect a model → Model routing](02-models.md#which-model-does-what-model-routing) |
| Fix an install that won't start | [Getting started → Troubleshooting](01-getting-started.md#troubleshooting), then [what to try, in order](06-backup-recovery.md#when-something-is-broken-what-to-try-in-order) |
| Understand why an action is "awaiting approval" | [Using SmartBrain_3000 → Chat](03-features.md#chat) |
| Talk to it — dictation and spoken replies | [Using SmartBrain_3000 → Voice](03-features.md#voice) |
| Resend a message (Retry) | [Using SmartBrain_3000 → After an answer](03-features.md#after-an-answer) |
| Add documents and search them | [Using SmartBrain_3000 → Knowledge](03-features.md#knowledge) |
| Follow a website (feeds) | [Using SmartBrain_3000 → Follow websites](03-features.md#follow-websites-feeds) |
| Is something wrong? Settings → Status | [Getting started → Troubleshooting](01-getting-started.md#troubleshooting) |
| Run a prompt on a timer | [Using SmartBrain_3000 → Schedules](03-features.md#schedules) |
| Connect Gmail | [Using SmartBrain_3000 → Email](03-features.md#email-gmail) |
| Change what the assistant knows about you | [Using SmartBrain_3000 → Memory](03-features.md#memory) |
| See what your cloud models cost | [Using SmartBrain_3000 → Usage & cost](03-features.md#usage--cost) |
| Share documents with someone | [Vaults](04-vaults.md) |
| Let Claude Desktop read your notes | [MCP](05-mcp.md) |
| Back up, restore, or move to a new machine | [Backup & recovery](06-backup-recovery.md) |
| Recover a forgotten passphrase | [Backup & recovery → Forgot your passphrase?](06-backup-recovery.md#forgot-your-passphrase) |
| Know what leaves your machine | [Privacy & security](07-privacy-security.md) |
| Use it from your phone | [Remote access](08-remote-access.md) |
| Report a security problem | [`SECURITY.md`](https://github.com/SecureCloudGroup/SmartBrain_3000/blob/main/SECURITY.md) |
| Ask a question / get unstuck | [Discussions](https://github.com/SecureCloudGroup/SmartBrain_3000/discussions) |

## Two things worth knowing up front

- **There is no password reset.** No server holds your data and no one can unlock it for
  you. The Recovery Key you save during setup is the only way back in if you forget your
  passphrase. See [Getting started → First run](01-getting-started.md#first-run).
- **The assistant asks before it acts.** It reads freely, but anything that changes data
  or reaches out waits for your approval, and every attempt is recorded. See
  [Using SmartBrain_3000 → Chat](03-features.md#chat).
