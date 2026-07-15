# Getting started

SmartBrain_3000 is a **local-first, single-user AI assistant** that runs entirely
on your own machine within a container (Docker). Your data and credentials stay on-box, encrypted
at rest. The only outbound calls it makes are to services you explicitly opt into:
the AI providers you configure, and Google's APIs if you connect Gmail. See
[Privacy &amp; security](06-privacy-security.md) for the full picture.

## What you need

**Docker** — that's the only requirement. Install
[Docker Desktop](https://docs.docker.com/get-docker/) (macOS/Windows) or Docker Engine /
Colima / OrbStack (Linux/macOS), and make sure it's running.

That's it — no accounts, and no config files to edit. SmartBrain runs as a small,
prebuilt image it downloads for you; your data stays on your machine.

## Install

The easiest way is a package manager. It installs the SmartBrain **desktop app** — a small
menu-bar / system-tray launcher that starts Docker if needed, brings up the stack, and opens
the app in your browser. The download page is **https://smartbrain.securecloudgroup.com**, or
run the command for your system:

**macOS** — in the Terminal app:

```sh
brew install --cask securecloudgroup/tap/smartbrain
```

**Windows** — in Terminal or PowerShell, using [Scoop](https://scoop.sh):

```powershell
scoop bucket add securecloudgroup https://github.com/SecureCloudGroup/scoop-bucket
scoop install securecloudgroup/smartbrain
```

(`winget install SecureCloudGroup.SmartBrain` is coming soon.)

**Any OS, straight from Docker** — download the release compose file and run it (no app, no
clone):

```sh
curl -fsSLO https://raw.githubusercontent.com/SecureCloudGroup/SmartBrain_3000/main/compose/docker-compose.release.yml
docker compose -f docker-compose.release.yml up -d
```

Open the app (the launcher does this for you) at **http://localhost:33000**. The first run
**downloads the app image** — a minute or two — and after that it starts instantly. Then
complete first-run setup below.

### Install from source (for contributors)

Building from the repo is slower — it compiles the image locally — and additionally needs
**git** and **Python 3**. Use it when you're developing on the code:

```sh
git clone https://github.com/SecureCloudGroup/SmartBrain_3000.git
cd SmartBrain_3000
python3 installer/install.py install
```

`python3 installer/install.py doctor` checks and offers to fix common problems (start Docker,
restart the stack, pull the embedding model). See [installer/](../installer/README.md).

## First run

The first time you open the app it walks you through setup:

1. **Choose a passphrase** (at least 8 characters). This encrypts everything.
2. **Save your Emergency Kit.** You'll be shown a **Recovery Key** *once*. Store it
   somewhere safe and offline (print it, or put it in a password manager).
   - There is **no server and no password reset**. If you forget your passphrase,
     the Recovery Key is the *only* way back into your data.
3. You're now **unlocked** and ready to use the app.

## Your first 5 minutes

A quick path from zero to seeing what SmartBrain does:

1. **Connect a model.** Open **Chat**. If you're already running Ollama, you'll see
   *"Found Ollama running on this machine"* — tap **Connect** and you're set. No
   Ollama? Add a cloud key under **Settings → Cloud providers**, or
   [install Ollama](https://ollama.com/download) and pull a model
   (`ollama pull llama3.1:8b`). See [Connect a model](02-models.md).

   ![Chat offering a one-tap connect for a detected Ollama server](assets/01-chat-connect.png)

![Your first chat — tap a suggestion, get a reply](assets/gifs/03-first-chat.gif)
2. **Send your first message.** Ask it anything — e.g. *"What can you help me with?"*
3. **Add something to Knowledge.** Open **Knowledge**, add a note or drop in a PDF — it's
   indexed automatically within seconds. Now ask Chat about it.
4. **Watch the approval flow.** Ask the assistant to *"add a task to call the dentist
   tomorrow."* Because creating a task changes data, it **parks for your approval** in
   **Activity** instead of acting on its own. Open **Activity** and approve it.
5. **That's the core loop:** the assistant can read freely, but anything that changes
   data or reaches out waits for your **OK** — and every attempt is audited.

## Locking and unlocking

- Use **Lock** (top right) to drop the key from memory — your data is sealed until
  you unlock again. Locking also clears your provider keys from the gateway.
- **Unlock** with your passphrase. Forgot it? Choose **Use recovery key**
  and enter the key from your Emergency Kit (dashes and letter case don't matter).

## Updating

How you update depends on how you installed:

- **Homebrew (macOS):** `brew upgrade --cask smartbrain`, then reopen the app.
- **Scoop (Windows):** `scoop update smartbrain`, then reopen the app.
- **Release compose / desktop app:** it pulls the latest image on start, so updating is just a
  **restart** — use the launcher's **Restart**, or run `docker compose -f
  docker-compose.release.yml up -d` again.
- **From source:** `python3 installer/install.py update` — it **backs up your encrypted data
  first**, pulls the latest code, rebuilds the image, restarts the stack, and verifies it's
  healthy. It prompts before making changes and runs on the host, never inside the container.

Your data is kept in Docker volumes on your machine and is left untouched by an update. (More on
backups: [Backup &amp; recovery](05-backup-recovery.md).)

## Troubleshooting

Most first-run problems are one of these:

- **"Docker daemon not reachable" / it fails immediately.** Docker isn't running. Start
  Docker Desktop (or `colima start`), then click **Restart** in the menu (or reopen the app).
  Note: Docker Desktop's very first launch asks you to accept its terms — do that first.
- **The page won't load at http://localhost:33000.** Give a first run another minute (it's
  downloading the image) — the menu's status line says what it's doing. If it reads *"Still
  warming up"*, click **Open SmartBrain** again in a moment. Still stuck? Check the logs:
  `docker compose -f docker-compose.release.yml logs smartbrain` (from source, use
  `compose/docker-compose.yml`).
- **Chat says "No models available yet."** You haven't connected a model. If Ollama
  is running, the Chat screen offers a one-tap **Connect**; otherwise add a cloud key
  under **Settings → Cloud providers**. See [Connect a model](02-models.md).
- **Semantic search returns keyword results ("degraded").** The embedding model isn't
  pulled. On the Desktop run `ollama pull nomic-embed-text:v1.5` (the installer and
  `doctor` try to do this for you), then **Reindex** in Knowledge.
- **The browser warns about the certificate** (only if you set up LAN/HTTPS). Trust
  the local mkcert CA — see [Remote access](07-remote-access.md).
- **"Database is newer than this app" / a restore is refused.** Pointing an older build
  at a newer data directory, or restoring a backup from a newer version, is refused on
  purpose to prevent data loss. Upgrade SmartBrain_3000 first (`install.py update`), then
  reopen or retry the restore.

## Next

- [Connect a model](02-models.md) — add a cloud provider key or a local model.
- [Using SmartBrain_3000](03-features.md) — chat, knowledge, planner, schedules, email.
