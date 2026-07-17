<p align="center">
  <img src="assets/SmartBrain_Avatar.png" alt="SmartBrain_3000 logo" width="160" />
</p>

# SmartBrain_3000

A personal AI assistant that runs **entirely on your own machine** — your
knowledge, your AI models, and your credentials stay on your hardware, encrypted
at rest under a passphrase only you hold. Your data never leaves your hardware — the
one exception is **optional** remote phone access: if you turn it on, your Desktop
connects to a **content-blind** signaling node to set up an end-to-end-encrypted link
to your phone (the SecureCloudGroup-hosted `rtc.securecloudgroup.com` by default, or
your own via `SMARTBRAIN_SIGNALING_URL`). It only ever relays encrypted connection
setup — it cannot read your data.

<p align="center">
  <img src="docs/assets/gifs/01-install-to-unlocked.gif" alt="One command installs SmartBrain; set a passphrase, save your Emergency Kit, and land on an unlocked Chat" width="760" />
  <br/><em>From one command to your unlocked Chat.</em>
</p>

## What it is

SmartBrain_3000 is a **fully local, single-user** AI assistant you run in Docker
on your own computer — macOS, Linux, or Windows.

- **Your choice of AI.** Bring your own API keys for OpenAI, Anthropic, or
  Google — or run **fully local models** with Ollama or Apple MLX. A built-in
  gateway routes between them.
- **Your data, encrypted on-device.** Your knowledge base, notes, plans, and
  secrets live in a local database, encrypted at rest under a passphrase only
  you hold.
- **Chat with tools, under your control.** The assistant can search your
  knowledge, track tasks, and act on your behalf — but anything that changes
  data or reaches out parks for your approval first, and every attempt is
  audited.
- **Group and share knowledge with Vaults.** Bundle documents into a **vault** to
  scope a search, share it as an encrypted file, or **publish it publicly** so others
  can **subscribe by URL** and stay up to date — signed, verified, and re-encrypted
  under each reader's own passphrase. See [Vaults](docs/03-features.md#vaults).

## Quickstart

The only prerequisite is **[Docker](https://docs.docker.com/get-docker/)**, running. Then
install the SmartBrain app from **https://smartbrain.securecloudgroup.com**, or with one command:

**macOS** — in the Terminal app:

```sh
brew install --cask securecloudgroup/tap/smartbrain
```

**Windows** — in Terminal or PowerShell, via [Scoop](https://scoop.sh):

```powershell
scoop bucket add securecloudgroup https://github.com/SecureCloudGroup/scoop-bucket
scoop install securecloudgroup/smartbrain
```

_(`winget install SecureCloudGroup.SmartBrain` is coming soon.)_

**Any OS, straight from Docker** — download the release compose file and run it (no app, no clone):

```sh
curl -fsSLO https://raw.githubusercontent.com/SecureCloudGroup/SmartBrain_3000/main/compose/docker-compose.release.yml
docker compose -f docker-compose.release.yml up -d
```

Homebrew and Scoop install a small **desktop launcher** (a menu-bar / tray app) that starts the
stack and opens the app for you. The app runs at **http://localhost:33000** — the first run
**downloads a prebuilt image** (a minute or two), then starts instantly. In the app:

1. **Set a passphrase** and **save your Emergency Kit** (a one-time Recovery
   Key). There is no password reset — the Recovery Key is the only way back in
   if you forget your passphrase, so store it somewhere safe and offline.
2. **Connect a model** under **Settings**: add a cloud provider API key, or run a
   local model — **MLX** on an Apple-Silicon Mac, or **Ollama** on any OS. If one is
   already running, SmartBrain **detects it and offers a one-tap connect** right on the
   Chat screen.
3. **Start chatting.**

> **Building from source?** Contributors can `git clone` the repo and run
> `python3 installer/install.py install` (this also needs git + Python, and compiles the image
> locally). See [Install from source](docs/01-getting-started.md#install-from-source-for-contributors).

### See it in action

Short, silent clips (~15s each) for every step — the five **Quickstart** clips take you
from install to fully working; the rest are optional power-ups:

| Quickstart | Then |
| --- | --- |
| [1 · Install → unlocked](docs/assets/gifs/01-install-to-unlocked.gif) | [6 · Planner](docs/assets/gifs/06-planner.gif) |
| [2 · Connect a model](docs/assets/gifs/02-connect-a-model.gif) | [7 · Schedules](docs/assets/gifs/07-schedule-a-prompt.gif) |
| [3 · Your first chat](docs/assets/gifs/03-first-chat.gif) | [8 · Pair a phone](docs/assets/gifs/08-pair-a-phone.gif) |
| [4 · Add knowledge & search](docs/assets/gifs/04-add-knowledge.gif) | [9 · Backup & recovery](docs/assets/gifs/09-backup-recovery.gif) |
| [5 · Approve an action](docs/assets/gifs/05-approve-an-action.gif) | [10 · Vaults — share knowledge](docs/assets/gifs/10-vaults.gif) |
| | [11 · Vaults — subscribe & update](docs/assets/gifs/11-vault-subscribe.gif) |

### Updating

- **Homebrew:** `brew update && brew upgrade --cask smartbrain` · **Scoop:** `scoop update smartbrain` — then reopen the app.
- **Release compose / launcher:** it pulls the latest image on start, so updating is just a **restart** (the launcher's **Restart**, or re-run the compose command).
- **From source:** `python3 installer/install.py update` backs up your encrypted data first, then rebuilds, restarts, and verifies — prompting before changes, on the host, never inside the container.

Your data is kept in Docker volumes on your machine and is left untouched by an update.

## Going further (optional)

These are advanced tiers — none are needed for the Quickstart above:

- **Gmail** — connect a Gmail account (your own Google OAuth client, loopback
  flow) so the assistant can read and draft mail. See the
  [docs](docs/03-features.md#email-gmail).
- **Your phone, from anywhere** — pair a phone to reach your assistant on Wi-Fi
  or cellular over an end-to-end-encrypted WebRTC link, with no router
  port-forwarding. Off by default. See [Remote access](docs/07-remote-access.md).

Full guide: **[docs/](docs/README.md)** (also available in-app under **Help**).

## License — source-available (not open source)

SmartBrain_3000 is licensed under the **Elastic License 2.0 (ELv2)**. It is
**source-available, not OSI "open source."** In plain terms:

- ✅ You may use, self-host, and modify it for free — including inside a
  business, for your own use.
- ❌ You may **not** offer it to others as a hosted or managed service, resell
  it, or ship a competing product built from it.

See [LICENSE](LICENSE) for the full terms.

© 2026 The Frels Holdings LLC. "SmartBrain", "SmartBrain_3000", and
"SmartBrain AI" are trademarks of The Frels Holdings LLC.

## Security

Please report security issues **privately** — see [SECURITY.md](SECURITY.md)
(contact: `info@securecloudgroup.com`). Do not open public issues for
vulnerabilities.
