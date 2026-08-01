# Using SmartBrain_3000

Everything here runs locally and is encrypted at rest. Here's what each area does.

The **Desktop** is the main surface and shows everything below. On a **paired phone**
([Remote access](08-remote-access.md)) you get a trimmed set for use on the go — Chat,
Knowledge, Planner, Schedules, Email, and Activity — while Settings and setup stay on the Desktop.

## Chat

Talk to your assistant. Chat can optionally **use tools** to act on your behalf —
search your knowledge, **read or summarize a whole document**, **save a note back to
your knowledge**, add a task, fetch a public web page, send an email, and more. Replies
are formatted (headings, lists, tables, and code blocks render properly). You can
**Stop** an answer mid-stream, **Copy** any reply, **Regenerate** the latest one, and
**Rename** a saved chat. Deleting a chat — or using **Delete all…** next to the saved-chats
picker — moves it to the **Trash**, where it can be restored for 30 days from
Settings → Account & Data.

The assistant also knows what time it is **where you are**. Your browser reports its
timezone and every turn is told your local date and time, with UTC alongside for
cross-zone questions; scheduled runs get the same. There is nothing to configure — the
zone is read from your browser and stored locally, like any other setting.

Tools are **risk-tiered**, and this is the core safety idea:

- **Observe** (e.g. knowledge search) runs automatically — it only reads.
- **Reviewed** (e.g. add a task, search the web) is **never run automatically** until you
  say so. The assistant *proposes* it and it waits for your approval in **Activity**. If
  you get tired of approving the same tool, **Always allow** lets that one run without
  asking from then on — and **Stop allowing** takes it back.
- **Irreversible** (e.g. send an email, delete a task) always waits for your approval, with
  an extra confirmation, and can never be pre-authorized.

So the assistant can draft and suggest, but anything that changes data or reaches
out requires your explicit OK. Every tool attempt is written to the audit log.

**For example:** ask *"search my knowledge for the lease terms"* and the assistant
reads and answers immediately (Observe). Ask *"email the landlord about it"* and it
**drafts** the message but **parks it in Activity** — nothing sends until you open
Activity and approve (Irreversible, with an extra confirm).

## Knowledge

A private, encrypted knowledge base. Drag in **PDFs, Word (.docx), PowerPoint (.pptx),
Excel (.xlsx), HTML, Markdown, CSV/JSON and other text files** — many files in one drop if
you like — paste a URL, or write a note. **Big documents are welcome**: a
several-hundred-page PDF is fine. Uploads don't block: they land right away, keyword search
works within seconds, and meaning-search for a very large document fills in over the next
few minutes in the background (it resumes by itself after a restart). Adding the same content twice is a no-op — SmartBrain
recognises it and keeps the one copy rather than cluttering your results with duplicates.

Search your knowledge three ways:

- **Best** (default) — combines both of the below. Keyword search nails an exact name
  or invoice number; meaning search finds a paraphrase. Each misses what the other
  catches, so fusing them beats either alone.
- **Keyword** — ranks by relevance: rare words count for more, and a long document
  can't win just by being long. Needs no model at all.
- **Meaning** — matches by sense rather than wording, using an
  [embedding model](02-models.md).

**Results are citations.** Every hit shows where it came from — *"Lease.pdf · p.12"*
(a slide deck cites *slide 3*, a spreadsheet *sheet 2*) — and clicking it opens the
document **at the passage that matched**, highlighted, rather than at the top. Chat
answers that used your knowledge show the same source chips underneath the reply —
click one to open the document at the cited passage. The chips come from what the
assistant actually searched and read, not from what it *says* it did, so you can
check any claim against the original.

**Organize with tags.** Every document (and vault) has an inline tag editor — click the
tags line on a row to add or change them, and click any tag chip to filter the list to it.
Editing tags is instant and never re-indexes the document.

**Try it:** open **Knowledge**, drag in a document, and search it. Then ask **Chat**
*"what does my knowledge say about …"* — the assistant searches it for you and tells you
which file and page it got the answer from.

![The Knowledge page: add a document, then search it](assets/05-knowledge.png)

![Drop in a file, search it, open the cited passage, then ask Chat — answers cite their sources](assets/gifs/04-add-knowledge.gif)

> Semantic search needs an embedding model set up for your backend. If results say
> *"degraded"*, set one up — see
> [Embeddings](02-models.md#embeddings-for-knowledge-search) — then **Reindex**.

Your knowledge is also what external tools can read over [MCP](05-mcp.md).
Group documents into **vaults** to scope a search — and to share them, privately
or publicly: see [Share knowledge with Vaults](04-vaults.md).

## Planner

![Planner — tasks grouped Today / This week / by due date](assets/gifs/06-planner.gif)

Simple task tracking — add tasks with optional due dates; they group into Today /
This week / Later. The assistant can propose new tasks (which you approve).

## Schedules

![Schedules — run a prompt on a timer, then Run now](assets/gifs/07-schedule-a-prompt.gif)

Run a prompt on a timer — e.g. "every morning, summarize my open tasks." A
schedule fires an assistant turn on its cadence. Two things to know:

- Schedules only run **while the app is unlocked** (a locked vault can't decrypt
  or act — there's no background access to your data).
- If a scheduled run wants to do something **dangerous** (send, delete, etc.), it
  **parks for your approval** in Activity just like in chat — it won't act alone.

Use **Run now** to fire one immediately.

A run's output lands in four places: it appears **in your open Chat** (as a
"Scheduled Item" notice), in the schedule's **Output** tab, as a durable copy on the
**Info** page, and the Chat tab shows a badge while results are unseen.

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

## Memory

**Settings → Memory** holds who the assistant is for: your name, its name, standing
custom instructions, and a list of remembered facts. Everything there is encrypted and
injected into every conversation, so it's the place to look when you wonder "why does it
keep doing that?" — including any *"(learned) …"* facts self-improvement added (delete
one to permanently reject it).

## Web search

The assistant's web tools search with **DuckDuckGo by default — no key needed**. Under
**Settings → Web search** you can switch engines: bring your own **Brave Search** or
**Tavily** key, or point at a self-hosted **SearXNG**. Searches only happen when the
assistant actually uses the web tools in a turn; see
[Privacy & security](07-privacy-security.md) for exactly what leaves your machine.

## Self-improvement

SmartBrain can review its own recent performance and carefully improve — **off by
default**, and switched on under **Settings → Self-improvement**. Every 8 hours (while
unlocked) it scores Chat, Knowledge, and Tools from private, on-device telemetry. Quiet
periods stay silent; when something needs attention you get a short digest in the chat
feed. From a flagged period it may act — always within hard bounds:

- **Learned preferences** — a local model (never a cloud one, and only from messages
  *you* wrote) may learn one durable preference, applied as a visible *"(learned) …"*
  fact in Settings → Memory, measured against your satisfaction, and **auto-reverted if
  it doesn't help**. Deleting the fact yourself permanently rejects it.
- **Suggested routines** — an ask you repeat on a daily/weekly rhythm becomes a
  ready-made schedule **waiting for your approval in Activity**; decline it once and it
  is never offered again.
- **Knowledge gaps** — searches your knowledge couldn't answer get named in the digest.
- **Prompt optimizer** (its own switch) — learns how kinds of requests go and may steer
  them with a short guidance note; a strategy watches in *shadow* first, goes live only
  after a measured trial, is turned off automatically if it doesn't help, and guided
  answers always show a small **"guided · …"** chip.

One change is ever on trial at a time, everything is reversible, every applied or
reverted change is announced, and **Settings → Self-improvement** shows the record of
what it has done under **What it has done**.

## Usage & cost

A running estimate of what your **cloud** models cost. **Usage** shows estimated
spend per model over a date range (today, last 5/10/30 days, or a custom range),
computed from each provider's live pricing, with a total. **Local models (Ollama,
MLX) are free** and show as such. Usage appears here after you chat with a model;
none of your usage or token data leaves your machine — it's computed locally from your
token counts (the only network call is a local fetch of the model price list from the
on-device gateway).

## Activity

![The safety loop — the assistant proposes, you approve in Activity](assets/gifs/05-approve-an-action.gif)

Your audit + approvals view:

- **Awaiting your approval** — review what the assistant proposed and **Approve** or
  **Deny** it. **Always allow** approves it and stops asking for that tool from then on;
  anything pre-authorized this way is listed under **Always allowed**, where **Stop
  allowing** takes the permission back. Irreversible tools can't be pre-authorized —
  they ask every time.
- **Audit log** — an encrypted record of every tool attempt (what, when, outcome).

## Next

- [Share knowledge with Vaults](04-vaults.md) — sealed shares, public publishing, subscriptions.
- [Connect external tools](05-mcp.md) via MCP.
- [Backup & recovery](06-backup-recovery.md).
