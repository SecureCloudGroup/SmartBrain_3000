# Using SmartBrain_3000

Everything here runs locally and is encrypted at rest. Here's what each area does.

The **Desktop** is the main surface and shows everything below. On a **paired phone**
([Remote access](07-remote-access.md)) you get a trimmed set for use on the go — Chat,
Knowledge, Planner, Email, and Activity — while Settings and setup stay on the Desktop.

## Chat

Talk to your assistant. Chat can optionally **use tools** to act on your behalf —
search your knowledge, add a task, fetch a public web page, send an email, etc.

Tools are **risk-tiered**, and this is the core safety idea:

- **Observe** (e.g. knowledge search) runs automatically — it only reads.
- **Reviewed / Irreversible** (e.g. add a task, send an email, delete a task) are
  **never run automatically**. The assistant *proposes* them and they wait for
  your approval in **Activity**. Irreversible actions need an extra confirmation.

So the assistant can draft and suggest, but anything that changes data or reaches
out requires your explicit OK. Every tool attempt is written to the audit log.

**For example:** ask *"search my knowledge for the lease terms"* and the assistant
reads and answers immediately (Observe). Ask *"email the landlord about it"* and it
**drafts** the message but **parks it in Activity** — nothing sends until you open
Activity and approve (Irreversible, with an extra confirm).

## Knowledge

A private, encrypted knowledge base. Add documents (notes, references, anything),
then search them two ways:

- **Lexical** — fast keyword match.
- **Semantic** — meaning-based, using an [embedding model](02-models.md). Use
  **Reindex** after adding content so semantic search can find it.

**Try it:** open **Knowledge**, drag in a PDF (or paste a note), and click
**Reindex**. Then switch search to **Semantic** and ask in your own words — or ask
**Chat** *"what does my knowledge say about …"* and the assistant searches it for you.

![The Knowledge page: add a document, then search it](assets/05-knowledge.png)

> Semantic search needs the embedding model pulled (the installer does this for you).
> If results say *"degraded"*, run `ollama pull nomic-embed-text:v1.5` on the Desktop
> and Reindex — see [Embeddings](02-models.md#embeddings-for-knowledge-search).

Your knowledge is also what external tools can read over [MCP](04-mcp.md).

## Planner

Simple task tracking — add tasks with optional due dates; they group into Today /
This week / Later. The assistant can propose new tasks (which you approve).

## Schedules

Run a prompt on a timer — e.g. "every morning, summarize my open tasks." A
schedule fires an assistant turn on its cadence. Two things to know:

- Schedules only run **while the app is unlocked** (a locked vault can't decrypt
  or act — there's no background access to your data).
- If a scheduled run wants to do something **dangerous** (send, delete, etc.), it
  **parks for your approval** in Activity just like in chat — it won't act alone.

Use **Run now** to fire one immediately.

## Email (Gmail)

Connect a Gmail account with **your own** Google OAuth client. The whole flow is
loopback-only — the authorization happens on your machine and nothing leaves it except
the calls to Google. SmartBrain asks for just two scopes: **read** and **send** (no
archive, delete, or label changes). It's optional; most people run SmartBrain without it.

**One-time setup** (the in-app **Email** page walks you through these):

1. Open [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials),
   then **Create credentials → OAuth client ID**, and choose type **Desktop app**. A Desktop-app
   client needs **no redirect URL** — Google handles loopback automatically.
2. On the **OAuth consent screen**, add the `gmail.readonly` and `gmail.send` scopes and set
   **Publishing status** to **In production** — otherwise Google signs you out every 7 days.
3. In the app's **Email** page, paste the client **ID** and **secret** and click **Connect Gmail**.
   A Google sign-in opens; if it warns the app is "unverified" (it's your own client), choose
   **Advanced → Continue**, then approve the two scopes.

Once connected you can read recent mail and compose/send:

- **You** sending from the app is a direct action.
- The **assistant** sending email is an **Irreversible** tool — it always parks
  for your approval first. It can draft; you approve the send.

## Usage & cost

A running estimate of what your **cloud** models cost. **Usage** shows estimated
spend per model over a date range (today, last 5/10/30 days, or a custom range),
computed from each provider's live pricing, with a total. **Local models (Ollama,
MLX) are free** and show as such. Usage appears here after you chat with a model;
nothing is sent anywhere to produce it — it's computed on your machine from local
token counts.

## Activity

Your audit + approvals view:

- **Pending approvals** — review and approve/deny what the assistant proposed.
- **Audit log** — an encrypted record of every tool attempt (what, when, outcome).

## Next

- [Connect external tools](04-mcp.md) via MCP.
- [Backup & recovery](05-backup-recovery.md).
