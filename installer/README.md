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
python3 installer/install.py doctor   # re-check prerequisites + the running stack
python3 installer/install.py update   # rebuild from current source + restart, then verify
python3 installer/install.py certs smartbrain.local 192.168.1.50   # TLS cert for LAN/mobile
python3 installer/install.py --no-open
```

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

mDNS (`.local` discovery), QR pairing, and remote access (WireGuard) are the next
steps — see [`docs/08-remote-access.md`](../docs/08-remote-access.md).

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

`doctor` re-runs the prerequisite checks plus a stack-health probe and exits
non-zero if anything is red — handy for re-running any time.

## What it does NOT do (by design)

- **It never handles your secrets.** Your passphrase, the Emergency Kit, provider
  API keys, and Gmail connection are all set up **in the app** at
  `http://localhost:33000` (and Settings), so secrets only ever live in the app's
  encrypted store — not in installer memory, arguments, or shell history.
- **Local models** (Ollama / Apple MLX) run on the *host*; install them yourself
  and wire them in **Settings → Local models**. The app reaches them via
  `host.docker.internal`.
- **Mobile (HTTPS via a local CA + mDNS) and remote access (WireGuard)** are a
  later, device-dependent step and are not configured here.
