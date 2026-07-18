# Using SmartBrain_3000

Everything here runs locally and is encrypted at rest. Here's what each area does.

The **Desktop** is the main surface and shows everything below. On a **paired phone**
([Remote access](07-remote-access.md)) you get a trimmed set for use on the go — Chat,
Knowledge, Planner, Schedules, Email, and Activity — while Settings and setup stay on the Desktop.

## Chat

Talk to your assistant. Chat can optionally **use tools** to act on your behalf —
search your knowledge, **read or summarize a whole document**, **save a note back to
your knowledge**, add a task, fetch a public web page, send an email, and more. Replies
are formatted (headings, lists, tables, and code blocks render properly). You can
**Stop** an answer mid-stream, **Copy** any reply, **Regenerate** the latest one, and
**Rename** a saved chat.

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

A private, encrypted knowledge base. Drag in **PDFs, Word (.docx), PowerPoint (.pptx),
Excel (.xlsx), HTML and text files** — many files in one drop if you like — paste a URL,
or write a note. Uploads don't block: they land right away and a background indexer makes
them searchable within seconds. Adding the same content twice is a no-op — SmartBrain
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

**Try it:** open **Knowledge**, drag in a document, and search it. Then ask **Chat**
*"what does my knowledge say about …"* — the assistant searches it for you and tells you
which file and page it got the answer from.

![The Knowledge page: add a document, then search it](assets/05-knowledge.png)

![Drop in a file, search it, open the cited passage, then ask Chat — answers cite their sources](assets/gifs/04-add-knowledge.gif)

> Semantic search needs the embedding model pulled (the installer does this for you).
> If results say *"degraded"*, run `ollama pull nomic-embed-text:v1.5` on the Desktop
> and Reindex — see [Embeddings](02-models.md#embeddings-for-knowledge-search).

Your knowledge is also what external tools can read over [MCP](04-mcp.md).

## Vaults

![Vaults — tick documents into a vault, then publish it public: the no-key warning, a Public badge with your SB-… publisher fingerprint, and a version that bumps each time you export an update](assets/gifs/10-vaults.gif)

![Subscribe to a public vault by URL, then pull the publisher's verified updates — the docs land re-encrypted under your key, a keyword search hits, you make one copy yours with Detach, and Update now applies v2 all-or-nothing while keeping your copy](assets/gifs/11-vault-subscribe.gif)

A **vault** is a named set of your knowledge documents — the unit you scope a search to,
and the unit you share. Vaults live on the Knowledge page.

- **Create one and add documents.** Tick documents in your list, then add them to a new or
  existing vault — or click **Add documents** on the vault itself and it walks you to the list. A document can belong to several vaults; adding it to a vault never moves
  or copies the file, and deleting a vault never deletes its documents — it only removes the
  grouping.
- **See what's inside.** Click the document count on a vault to list its contents — open any of
  them, or remove one from the vault (the document itself is kept).
- **Search inside one.** Pick a vault next to the search box to search *only* its documents
  — e.g. keep a "Work" vault and a "Home" vault and ask each separately.
- **Share it.** **Export** a vault and SmartBrain seals it into a single `.sbvault` file and
  shows you a one-time key (starting `SBVK1-`). Send the file however you like, then give the
  person the key over a **different** channel — together they are the contents in the clear,
  so keep them apart.
- **Share it publicly.** Choose **Public** in the share panel instead: the export is the same
  `.sbvault` file with **no key at all** — anyone with the link can read everything in this
  vault, and there is **no taking it back**. Upload the file anywhere (Drive, S3, any web host)
  and share the link — or unzip it and upload the folder to a static host so future updates only
  re-upload what changed. Once published, the vault card shows a **Public** badge beside your
  publisher fingerprint (`SB-…`) and the published version — the identity and version readers will
  see. The file is still signed, so nobody else can publish an "update" to your vault in your name.
  To publish a **new version**, export it again (replacing the file where you host it): the version
  bumps automatically, and the button reads **Export update (v*N*)** so you know where it lands.
- **Import someone else's.** Pick the `.sbvault` file and paste the key. Its documents are
  **re-encrypted under your own passphrase** as they land (nothing you import can read or
  weaken your data), and anything you already have is kept as-is rather than overwritten. The
  result shows the publisher's fingerprint — the one thing that says *who* the knowledge came
  from. Imported documents are protected from accidental edits (rename/delete are refused);
  **Detach** one in the vault's member list to make that copy yours.
- **Subscribe to a public vault.** For a vault someone published **Public**, paste its URL
  instead of picking a file — no key needed. Link the `.sbvault` file itself, or — if the
  publisher hosts the unzipped folder on a static host — its `manifest.json`. SmartBrain fetches
  it (public internet hosts only, not localhost or LAN addresses), verifies the publisher's
  signature, and re-encrypts the documents under **your** passphrase as they land. The
  publisher's identity is **pinned on first contact** — the vault card shows a **Subscribed**
  badge with the pinned fingerprint and the host it came from — and future updates will only
  ever be accepted from that same publisher.
- **Keep a subscription up to date.** Click **Check for updates** on a subscribed vault; when the
  publisher has published a newer version, **Update now** fetches it, verifies everything against
  the pinned publisher identity, and applies it all-or-nothing — you are never left half-updated.
  Changed documents are updated **in place**, so citations and links to them keep working; new
  ones are added, and ones the publisher removed are deleted. **Anything you edited stays yours**:
  the update reports it as "kept" instead of overwriting it (same for documents you already had —
  your copy wins). On a `manifest.json` (folder) host only the changed files are downloaded; a
  single-file host re-downloads the whole file, and the card notes so. The card also shows how
  long ago it was last checked and flags a failed check ("host may be unreachable"), so a dead or
  stale host is easy to spot. If the
  publisher's **key ever changes**, updates stop with a warning showing both fingerprints — pinned
  (trusted) and offered (new), side by side — until you confirm the new key with the publisher
  out-of-band and choose **Trust new key** (Desktop + passphrase). A newer `.sbvault` *file* of a subscribed vault also applies as an
  update — importing it never creates a duplicate.
- **Scheduled auto-update (opt-in).** Turn on **Auto-update** on a subscribed vault card and pick a
  cadence (daily or weekly) to have SmartBrain check and apply clean updates for you. It is **off by
  default**, runs **only on the Desktop while unlocked**, and **never applies a publisher key change
  on its own** — a changed key still blocks and waits for you to confirm it. Each run reports what it
  did **in the chat feed** ("updated to v3 — 2 documents changed", or a "new publisher key" notice).

**Try it now — the official example vault.** This user guide is itself published as a public
vault. Use **Subscribe to a public vault** and paste
`https://smartbrain.securecloudgroup.com/vaults/smartbrain-docs.sbvault` — on first subscribe
you'll see the publisher fingerprint being pinned; ours is `SB-3WZM-7CEI-GPJ7-3MLC`. If it
matches, you're talking to us. The whole guide lands in your Knowledge, searchable and askable,
and new versions are offered as updates whenever the docs change.

Creating, adding, and searching a vault work everywhere, including a paired phone. **Exporting and
importing a vault are done on the Desktop** — sharing a vault's contents, or bringing new ones in, is
sensitive, so those actions live in the Desktop app.

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
none of your usage or token data leaves your machine — it's computed locally from your
token counts (the only network call is a local fetch of the model price list from the
on-device gateway).

## Activity

![The safety loop — the assistant proposes, you approve in Activity](assets/gifs/05-approve-an-action.gif)

Your audit + approvals view:

- **Pending approvals** — review and approve/deny what the assistant proposed.
- **Audit log** — an encrypted record of every tool attempt (what, when, outcome).

## Next

- [Connect external tools](04-mcp.md) via MCP.
- [Backup & recovery](05-backup-recovery.md).
