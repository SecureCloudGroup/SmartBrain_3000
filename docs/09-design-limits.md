# Design limits

SmartBrain_3000 is built as a **single-user, local-first, personal-scale** app.
Some of its boundaries are deliberate scope decisions — the kind of tradeoffs
that keep a personal tool simple, predictable, and safe — rather than missing
features. This page documents those choices and the reasoning behind each, so
there are no surprises.

These are intentional for the single-user model. They are **not** the right
tradeoffs for a multi-tenant or team deployment; SmartBrain_3000 isn't built for
that.

## Single-user global unlock

There is **one master key per running process**. When you unlock, the whole app
is unlocked; there is no per-user isolation, no separate accounts, and no
sandboxing of one "user" from another within the same instance.

**Why:** the product is a personal assistant for one owner on one machine.
Adding multi-user identity, per-user keys, and access control would add a large
surface for little benefit at this scale. One owner, one key, one encrypted store.

## Single-writer embedded database (DuckDB)

Data lives in an **embedded DuckDB** file. There is effectively **one concurrent
writer** — the app — and the database is sized for personal use, not for many
clients writing at once.

**Why:** an embedded, file-based store keeps the install trivial (no separate
database server) and matches a single-user workload. Concurrency that a
multi-client server would need isn't a goal here.

## No key at rest (restart returns to locked)

The encryption key is **never written to disk**. It lives only in memory while
you're unlocked. So a **restart** (or a crash, or `Lock`) returns the app to the
**locked** state, and any **in-flight approvals are invalidated** — a parked
action won't silently run after a restart; you'll unlock and re-approve.

A parked action also **expires after an hour** on its own. Approval is consent to something
happening *now*, and an hour-old "yes" to a half-remembered request is not the same thing.

**Why:** this trades some unattended resilience for security. The upside is
that data at rest is never decryptable without your passphrase or Recovery Key,
even if someone copies the disk. The cost is that an unattended restart leaves
the app locked until you return, and that nothing — no schedule, no vault
auto-update, no self-review — happens while it is locked.

## Append-only audit log (no hash chain)

Every tool attempt is recorded, and the audit log is **append-only at the API
surface** — the app exposes no way to edit or delete entries. It is **not** a
cryptographically chained, tamper-evident log (no per-entry hash chain).

**Why:** append-only-at-the-API gives you a faithful record for a single-owner
tool, where the threat isn't the owner forging their own history. A verifiable
hash chain is a reasonable post-MVP hardening, but it isn't needed to meet the
single-user transparency goal today.

## The search index lives in memory (rebuilt on each unlock)

Because content is **encrypted at rest**, we can't push search predicates down into a
plaintext database index. Instead, the corpus is decrypted **once per unlock** into an
in-memory index — a BM25 keyword index plus a matrix of chunk vectors — and every query is
answered from RAM. Only the handful of documents actually returned are decrypted again, to
cut their snippets.

The trade-offs that follow from that:

- **The first search after unlocking pays a one-time build.** Roughly 0.2s for 1,000
  documents and ~1.8s for 10,000. Searches after that are single-digit milliseconds.
- **The index costs RAM** — dominated by the vectors (~30 MB per 1,000 documents at 768
  dimensions). Very large libraries are bounded by an explicit ceiling — **100,000
  documents** — and if a corpus exceeds it that is **reported, not silently ignored**.
- **Nothing is written to disk.** The index is never persisted, so encryption at rest is
  unchanged: it exists only while the vault is unlocked and dies with the master key.

**Why:** indexing encrypted content on disk without leaking it is hard. Rebuilding in memory
keeps the encryption promise intact while still giving fast, whole-corpus search.

## One local-model request at a time

A local model server — Ollama, MLX, oMLX — serves **one request at a time**. SmartBrain has
several things that might want it at once: your chat, the background indexer embedding new
documents, the summary builder, a scheduled run. They are **queued**, never overlapped, so a
second caller can't provoke the "model is busy" failure that would break the first.

Your chat has priority: background work steps aside the moment a chat arrives and picks up
where it left off afterwards. The visible cost is that a large indexing backlog can still
make an answer feel slower than usual while it drains.

**Why:** the alternative is either failed requests or a queue the user can't see. Cloud
providers have no such limit and are unaffected — this applies only to local models.

## Voice: one dictation at a time, 120 seconds, one download

Dictation runs on your own machine, and three limits follow from that:

- **One dictation is transcribed at a time.** Like the local model, the speech engine is
  queued, never overlapped — a second recording waits for the first to be turned into text.
- **A single recording is capped at 120 seconds.** Dictation normally stops itself when you
  pause; the cap is the backstop for a mic left open. Say it in two pieces if you need more.
- **The voice model is a one-time ~141 MB download.** It starts at app launch on every OS,
  unconditionally, so voice is zero-setup — and the price of zero-setup is that disk.

**Why:** running speech recognition locally is what keeps your voice on-box. Bounding each
recording and serializing transcription keeps memory and CPU predictable on an ordinary
laptop; downloading the model without asking is what makes the mic simply work.

## A turn is bounded

One request to the assistant gets at most **eight tool steps**. When those run out — or when
what it has gathered would no longer fit in the model's context — it stops asking for tools
and writes an answer from what it has, saying plainly what it couldn't finish. It never
loops, and it never quietly gives up.

**Why:** an unbounded agent is a way to spend an afternoon and a lot of money on a question
that needed one search. A hard step count makes the worst case predictable. Where it isn't
enough, the answer says so and you can ask a narrower question — which is nearly always
faster than letting it wander.

## WebRTC signaling broker is single-operator

[Remote access](08-remote-access.md) uses a signaling broker that is
**single-operator** by design. The hosted broker is **tokenless** (open
registration, bounded by a desktop-count cap and per-registration rate limits),
and the cryptographic guarantee that your phone is really talking to **your**
Desktop is the **DTLS-fingerprint pin** captured at pairing — not the broker.
TURN relay uses **ephemeral credentials** (coturn `use-auth-secret`, minted per
connection and short-lived); those credentials grant **relay bandwidth only**,
never access to the app or your data. A **self-hosted** node may instead run with
a shared registration token and static, quota-bounded TURN creds.

**Why:** the broker is content-blind — it only helps devices find each other.
The end-to-end security comes from the pinned fingerprint, so the broker doesn't
need per-user accounts to be safe. Ephemeral, per-connection TURN creds keep the
relay simple while ensuring a leaked credential can, at worst, consume some relay
bandwidth before it expires.

## Next

- [Using SmartBrain_3000](03-features.md) — what each area does, day to day.
- [Privacy & security](07-privacy-security.md) — what protects your data and the
  real world limits.
- Back to the [documentation index](README.md).
