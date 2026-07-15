# New-user test plan

How to test SmartBrain the way a stranger will actually experience it — and catch the friction that
is invisible from a developer's machine.

> **The cardinal rule: test on a machine that has never seen this project, and don't help the tester.**
> A dev machine has Docker running, models pulled, the repo cloned, secrets configured, and — worst of
> all — insider knowledge. A real new user has none of that. Everything below exists to reproduce the
> stranger's starting point and measure their friction, not ours.

---

## Who the tester is (personas)

- **Primary: privacy-conscious, low technical ability.** Comfortable installing a normal app; *not*
  comfortable with a terminal, Docker, or editing config. This is who adoption depends on.
- **Secondary: the technical self-hoster.** Fine with a terminal and Docker; will try the compose path
  and poke at edges. Useful for the Linux/compose route and for finding sharp corners.

Run the walkthrough as the **primary** persona unless a step is explicitly the technical route.

## How to get a clean read (best first)

1. **A real person who fits the persona, on their own computer, while you watch silently.** Gold
   standard — they get stuck in places you cannot predict. Your only job is to take notes, not to help.
2. **You, simulating it, on a fresh VM or a brand-new OS user account.** Works only with discipline:
   use *only* what the public landing page tells you, never a command you remember, and when you hit
   friction, **log it instead of fixing it**.

If you must use your own Mac, reset it to a clean state first — see
[Appendix A](#appendix-a--resetting-a-dev-machine-to-clean). Note that a Mac you already own has Docker
installed, so it does **not** test the true cold start (no Docker) — do that one on a fresh VM.

## What you need to set up

- **A clean environment per OS you support** — a fresh macOS VM/account, a clean Windows VM, optionally
  Linux. Each exercises a *different* install path (Homebrew / winget+Scoop / Docker one-liner), so they
  are genuinely different tests, not repeats.
- **Two environments for the Vault test** — sharing needs a "you" and a "friend": two VMs, or two OS
  user accounts, each with its **own passphrase**.
- **A phone** for the mobile-pairing test.
- **A friction log** (shared doc or sheet) and, ideally, **screen recording**. The friction log is the
  real instrument of this test — format at the end.

---

## The walkthrough

Do the stages in order; a blocker in an early stage usually invalidates the later ones. For each step,
record time taken and every point of friction (see [Recording findings](#recording-findings)).

### Stage 0 · Discovery → running (the cold start)
*The single most important stage: this is your time-to-first-chat, and what people judge you on.*

1. Open **`smartbrain.securecloudgroup.com`** in a browser. Read it cold: do you understand *what this
   is* and *what you need* (Docker) before clicking anything?
2. Follow the install command shown for your OS — and **nothing else** (no docs, no memory).
3. Launch it (open the app / go to `http://localhost:33000`).

**Watch for:** a security warning (Gatekeeper/SmartScreen — there should be **none** via brew/winget/
scoop); whether Docker is required and, if it isn't installed, whether you're *told* or just left with
an error; how many minutes until the app opens.
**Deliberately run the no-Docker case on at least one VM** — that is the real cold start.

### Stage 1 · First-run setup

1. Set a passphrase; save the Emergency Kit. Did you *understand* there is no password reset, and that
   this recovery key is your only way back in?
2. **Connect a model.** *Highest-risk step for a non-technical user* — they have no API key and no local
   model. Does the app get them to a working chat, or dead-end them? Note exactly where and what you'd
   have needed to know.
3. Send a first message. Do you get a real answer?

### Stage 2 · Knowledge (the core value)

1. Add documents a normal person actually has: a PDF, a Word doc, and a web page by URL.
2. Search for a specific fact — try both **Keyword** and **Meaning**. Do the results make sense? Click a
   citation — does it open the document **at the matching passage**?
3. Ask a question in chat that needs those documents. Does the answer **cite its sources**, and are the
   citations correct?

### Stage 3 · Vaults (the differentiator)

1. On environment **A**: create a vault, add documents, export it, note the `SBVK1-…` key.
2. On environment **B** (the "friend", **different passphrase**): import the file + key, then search
   inside the vault. **Time this** — can a non-technical person do it unaided?

### Stage 4 · Mobile

1. Pair a phone (QR). Open Knowledge on the phone, search, open a result.

**Watch for:** does pairing "just work" or is there fiddling? Does search actually return the desktop's
data over the connection?

### Stage 5 · Safety & lifecycle (the trust tests)

1. Lock, then unlock. Then simulate a **forgotten passphrase** and recover using the Emergency Kit.
2. Back up, then restore into a fresh instance — is your data intact?
3. Uninstall — is it clean, and is it obvious which folder holds your data (to keep or to delete)?

---

## Success criteria (falsifiable — pass/fail, not vibes)

| Stage | Passes if… |
|---|---|
| **0 · Install** | App is running in the browser **< 5 min** from opening the landing page, on a clean machine, with **zero security warnings**, and the tester never had to search the web or guess a command. |
| **1 · First model** | Tester reaches a **working chat answer** without help, and can state why they saved the recovery key. |
| **2 · Knowledge** | Mixed files ingest with **no duplicates**; tester finds a specific fact via search and gets an answer with a **correct, clickable citation** (target ≥ 90% of knowledge answers cited). |
| **3 · Vaults** | The "friend" imports a shared vault and searches it **unaided, in < 2 min**. |
| **4 · Mobile** | Phone returns a correct search result from the desktop's knowledge. |
| **5 · Recovery** | Forgotten-passphrase recovery works; data survives a backup → restore. |

**Overall gate before inviting real people: zero *Blockers* in Stages 0–2 on every OS you support.**
Those are the stages that decide whether someone ever reaches the value at all.

## Recording findings

For every hesitation, backtrack, error, or "wait — what do I do now," log one row:

```
when │ what they were trying to do │ what confused or broke │ severity
```

**Severity:** `Blocker` (couldn't continue) · `Major` (continued, but frustrated or needed help) ·
`Minor` (small confusion) · `Cosmetic`.

**Triage:** Blockers = must-fix before anyone else touches it · Majors = fix this cycle · Minor/Cosmetic
= backlog. Weight Stage-0/1 findings heaviest — those are the ones that kill adoption.

## Results log (one row per environment per run)

| Date | OS / how installed | Persona | S0 | S1 | S2 | S3 | S4 | S5 | Blockers | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| | macOS / Homebrew | primary | | | | | | | | |
| | Windows / winget | primary | | | | | | | | |
| | Windows / Scoop | technical | | | | | | | | |
| | Linux / Docker | technical | | | | | | | | |

---

## Appendix A · Resetting a dev machine to clean

To reuse your own Mac as a test box, return it to what a new user's machine looks like. **This destroys
the local instance's data.**

> ⚠️ **Back up first if there is anything real in it.** Use the in-app backup (Settings → account/data)
> or copy the data folder. Wiping is irreversible — there is no undo and no server-side copy.

```sh
# 1. Stop and remove the dev stack (run from the repo root)
docker compose -f compose/docker-compose.yml down          # removes the containers
docker rmi smartbrain_3000:dev 2>/dev/null                 # the locally-built image
docker rmi ghcr.io/securecloudgroup/smartbrain_3000:latest 2>/dev/null  # any pulled release image

# 2. Remove the data (BOTH possible locations)
rm -rf ./data                                              # dev/compose data (bind mount)
rm -rf ~/Library/Application\ Support/SmartBrain           # launcher / installed-app data

# 3. If you installed via Homebrew, remove that too
brew uninstall --cask smartbrain 2>/dev/null
brew untap securecloudgroup/tap 2>/dev/null
```

Keep Docker installed (you'll want it for the install test; the *no-Docker* cold start belongs on a
fresh VM). Optionally keep the repo clone for development — a real install test uses the Homebrew app,
which is independent of the checkout.

**Verify it's actually clean:**
```sh
docker ps -a | grep -i smartbrain          # (no output)
ls ~/Library/Application\ Support/SmartBrain  # No such file or directory
brew list --cask 2>/dev/null | grep -i smartbrain   # (no output)
curl -sI http://localhost:33000            # connection refused
```

## Appendix B · Automating Stage 0 (regression guard)

Stage 0 for Linux can be automated so it never silently regresses: a CI job on a fresh Ubuntu runner
that installs *only* via the public path (pull the prebuilt image + `docker-compose.release.yml`, no
repo build), waits for `/api/health`, and fails if the app doesn't come up. It won't catch UX friction
— that needs a human — but it catches "the one-line install is broken" before a user does. Not yet
built; see the note in the install work.
