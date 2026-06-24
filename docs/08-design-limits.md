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
surface for little benefit at this scale. One owner, one key, one vault.

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

**Why:** this trades some unattended resilience for security. The upside is
that data at rest is never decryptable without your passphrase or Recovery Key,
even if someone copies the disk. The cost is that an unattended restart leaves
the app locked until you return.

## Append-only audit log (no hash chain)

Every tool attempt is recorded, and the audit log is **append-only at the API
surface** — the app exposes no way to edit or delete entries. It is **not** a
cryptographically chained, tamper-evident log (no per-entry hash chain).

**Why:** append-only-at-the-API gives you a faithful record for a single-owner
tool, where the threat isn't the owner forging their own history. A verifiable
hash chain is a reasonable post-MVP hardening, but it isn't needed to meet the
single-user transparency goal today.

## Encrypted search is linear (no index pushdown)

Because content is **encrypted at rest**, search scans and decrypts rows rather
than pushing predicates down into a database index. Lookups are **linear over
the scanned rows** — fine at personal scale, but it doesn't have the sublinear
behavior of an indexed plaintext store.

**Why:** indexing encrypted content without leaking it is hard, and a personal
knowledge base is small enough that a linear scan is fast. Keeping content
encrypted is worth more here than index-speed search.

## WebRTC signaling broker is single-operator

[Remote access](07-remote-access.md) uses a signaling broker that is
**single-operator** by design: devices register with a **shared registration
token**, and the cryptographic guarantee that your phone is really talking to
**your** Desktop is the **DTLS-fingerprint pin** captured at pairing — not the
broker. TURN relay uses **static credentials** bounded by quotas; those
credentials grant **relay bandwidth only**, never access to the app or your
data.

**Why:** the broker is content-blind — it only helps devices find each other.
The end-to-end security comes from the pinned fingerprint, so the broker doesn't
need per-user accounts to be safe. Static, quota-bounded TURN creds keep the
relay simple while ensuring a leaked credential can, at worst, consume some relay
bandwidth.

## Next

- [Using SmartBrain_3000](03-features.md) — what each area does, day to day.
- [Privacy & security](06-privacy-security.md) — what protects your data and the
  real world limits.
- Back to the [documentation index](README.md).
