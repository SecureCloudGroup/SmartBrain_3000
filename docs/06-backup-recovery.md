# Backup & recovery

![Download an encrypted backup, then unlock with your Recovery Key](assets/gifs/09-backup-recovery.gif)

Everything lives in one encrypted database on your machine. These tools, under
**Settings → Account & Data**, let you take it with you, restore it, and change
your passphrase — plus how to get back in if you forget it.

## Export your data

**Export data (JSON)** downloads your content — knowledge, chats, tasks,
memories, profile — as readable JSON. It's decrypted (it's yours), so keep the
file somewhere safe. Good for reading your data elsewhere or migrating out.
Because it hands out decrypted data, it runs on the **Desktop only** (never from a
paired phone) and **re-prompts for your passphrase** to authorize.

## Encrypted backup

**Download encrypted backup** gives you a complete, portable copy of the database
(a `.duckdb` file). It's still encrypted — it includes your wrapped keys — so it
restores with the **same passphrase**. This is the one to keep for disaster
recovery and to move your install to a new machine. Like Export, it's
**Desktop-only** and **re-prompts for your passphrase** before it hands over the vault.

## Restore

**Stage restore** takes a backup file, validates it, and applies it the **next
time SmartBrain_3000 restarts** (swapping the live database while it's running
isn't safe). Your current database is kept alongside as `*.pre-restore-<timestamp>`,
so a restore is reversible.

- Allowed when you're **unlocked**, or onto a **fresh install** (moving to a new
  machine) — never over a locked, initialized vault.
- After staging, restart SmartBrain — **Restart** in the menu-bar / tray menu (from
  source, `python3 installer/install.py update`) — and unlock with that backup's
  passphrase.
- A backup from a **newer version** of SmartBrain_3000 is **refused on purpose**
  (it would risk data loss under older code): upgrade this app first, then restore.

## Starting completely fresh

If an install is broken in a way that restarting and updating cannot fix, there is a full
reset: back up, remove everything SmartBrain put on the machine, install the latest
version, and restore your data.

**You almost certainly do not need this.** Try quitting and reopening SmartBrain from the
menu bar first, then `brew update && brew upgrade --cask smartbrain`, then a hard reload of
the browser tab. A full reset re-downloads the whole app and takes 10–30 minutes.

```sh
bash installer/full-reset.sh --inventory   # what is on this machine; changes nothing
bash installer/full-reset.sh --dry-run     # the whole plan, carried out on nothing
bash installer/full-reset.sh               # do it
```

It will not continue without a backup file it has checked, it shows every deletion before
it happens and asks you to type a confirmation word, and it never deletes your backup or
your data — your data folder is **moved** to `~/SmartBrain-reset-<timestamp>/`, not removed.

One step is manual, and skipping it is the usual reason a correct reinstall still looks
broken: **clear the browser**. SmartBrain installs a service worker that caches the app,
and it will keep serving the old version after a reinstall. The script prints a snippet to
paste into your browser console that unregisters it and clears the cached app, the
paired-device credential, and stored settings. A paired phone has to be paired again
afterwards.

macOS only for now. On Windows the same five steps apply, against `%APPDATA%\SmartBrain`
and the Scoop package.


## Chat Trash

Deleted chats (one at a time, or Chat's **Delete all…**) land here for **30 days** —
restore any of them, or **Empty trash now** to purge them immediately. After 30 days
they're removed for good automatically.

## Change your passphrase

**Change passphrase** re-wraps your master key under a new passphrase after
verifying the current one. Your data and your Recovery Key stay valid — only the
passphrase changes.

## Forgot your passphrase?

There is **no server and no reset**. Use your **Recovery Key** from the Emergency
Kit you saved during setup:

1. Lock / reopen the app and choose **Unlock with Recovery Key**.
2. Enter the key exactly as shown (dashes and letter case don't matter).
3. Once in, go to **Settings → Account & Data → Change passphrase** and use
   **"Forgot your current passphrase… Set a new one"** — that path sets a new
   passphrase from your unlocked session, so you don't need the old one. (The
   normal Change passphrase form still requires the current one.)

If you lose **both** the passphrase and the Recovery Key, the data cannot be
recovered — that's the cost of having no backdoor. Keep the Emergency Kit safe.

## Next

- [Privacy & security](07-privacy-security.md) — what's protected and what leaves your machine.
