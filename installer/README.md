# Installer

> **This is the from-source path, for contributors.** Most people should install the prebuilt app
> via **Homebrew** or **Scoop**, or run the release compose — see the main
> [README](../README.md#quickstart) or **https://smartbrain.securecloudgroup.com**. This installer
> builds the image locally from the repo (slower), and is what you want when developing on the code.

A small, dependency-free installer that gets SmartBrain_3000 running on your
machine. It only needs **Docker** and **Python 3** — everything else is built
locally from this repo (no GitHub or registry account required).

## Run it

macOS / Linux:

```sh
./installer/install.sh
```

Windows (PowerShell):

```powershell
.\installer\install.ps1
```

Or invoke the cross-platform core directly:

```sh
python3 installer/install.py          # install (default)
python3 installer/install.py update   # rebuild from current source + restart, then verify
python3 installer/install.py certs smartbrain.local 192.168.1.50   # TLS cert for LAN/mobile
python3 installer/install.py --no-open
```

## The doctor

`doctor` is **not** part of the from-source path and needs no repo, no Docker, and no
running app. It looks at the ordinary install a person actually has — the one the launcher
assembles under your user-data folder — and says what is wrong with it in plain words:

```sh
python3 installer/doctor.py            # report only; nothing is changed
python3 installer/doctor.py -v         # ...including the checks that passed
python3 installer/doctor.py --fix      # offer each safe repair, one at a time
python3 installer/install.py doctor    # the same tool, reached through the installer
```

It checks the assembled install and the `current` pointer, both processes and their pid
records, both ports and *who* is answering them, the vault's lock state, the database file,
the model gateway's providers and the local model servers behind them, disk space, staged
updates, leftovers from interrupted downloads, and the log for the handful of failures this
project has actually hit. It exits non-zero only when something is genuinely broken.

Repairs are offered one at a time, each printed in full before it runs, and **never** touch
your data directory. It is read-only unless you pass `--fix`.

## LAN / mobile access (HTTPS)

Desktop uses plain `http://localhost` (a secure context). To reach the app from a
phone on your network you need HTTPS with a cert your phone trusts:

1. **Make a cert** (needs [mkcert](https://github.com/FiloSottile/mkcert)):

   ```sh
   python3 installer/install.py certs <name>.local <your-LAN-IP>
   ```

   It writes `data/certs/` and prints the CA root (`rootCA.pem`) — install that on
   your phone so it trusts the app.

2. **Start the LAN profile** (binds the LAN, serves HTTPS, allows your hostname):

   ```sh
   SMARTBRAIN_ALLOWED_HOSTS=localhost,127.0.0.1,<name>.local \
     docker compose -f compose/docker-compose.yml -f compose/docker-compose.lan.yml up -d
   ```

Phone remote access (QR + pairing code over WebRTC) ships in the app — Settings →
Remote access, see [`docs/08-remote-access.md`](../docs/08-remote-access.md). WireGuard
remains a compose-level alternative; mDNS (`.local` discovery) is a possible future step.

**Updating:** `git pull` for newer code, then `install.py update` (rebuilds the
image and restarts). Back up first in the app — Settings → Account & Data →
Download encrypted backup. Updates run from the host (here), not from inside the
container, so the app never needs access to the Docker socket.

## What it does

1. **Prerequisite gate** — checks for the Docker CLI, a running Docker daemon,
   Docker Compose v2, and the compose file; prints clear remediation if anything
   is missing.
2. **Build + start** — `docker compose up -d --build` against
   [`compose/docker-compose.yml`](../compose/docker-compose.yml), building the
   image locally (pulls only public base images).
3. **Verify** — waits for `http://localhost:33000/api/health` to report healthy.
4. **Next steps** — opens the app and points you at first-run setup.

`install` and `update` refuse to run while a Docker-free SmartBrain is live on the
same machine — they would build a second stack onto the same port and the same
database. Stop SmartBrain from its menu first.

## What it does NOT do (by design)

- **It never handles your secrets.** Your passphrase, the Emergency Kit, provider
  API keys, and Gmail connection are all set up **in the app** at
  `http://localhost:33000` (and Settings), so secrets only ever live in the app's
  encrypted store — not in installer memory, arguments, or shell history.
- **Local models** (Ollama / Apple MLX) run on the *host*; install them yourself
  and wire them in **Settings → Local models**. The app reaches them via
  `host.docker.internal`.
- **Phone access** is configured in the app, not here: Settings → Remote access
  pairs a phone over WebRTC (see [`docs/08-remote-access.md`](../docs/08-remote-access.md)).
  The LAN/HTTPS path (local CA) and WireGuard are optional from-source alternatives.
