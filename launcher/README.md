# SmartBrain launcher

A tiny menu-bar / system-tray app that makes SmartBrain one click to reach. It has no UI of its own —
the real app is the SvelteKit interface in your browser — so all it does is start the stack, wait
until it is healthy, and open `http://localhost:33000`.

The tray menu is just **Open**, the version, a status line, **Stop**, **Restart**, **Open logs** (the
folder the troubleshooting docs point at), **Get Docker…** (only where Docker is the stack),
**Check for updates** with **Install update now** / **Install on next start** once one is ready, and
**Quit launcher** (which leaves SmartBrain running).

## Two stacks, one launcher

**Native (Docker-free) is the default** wherever it can be assembled — macOS on Apple Silicon,
Windows x64 and Linux x86_64 (tray app, or headless under systemd via `install-linux.sh`). There the
launcher downloads a pinned Python runtime, a wheelhouse and the gateway binary, verifies each against
a checksum pinned in the source, and assembles a versioned install it can flip atomically (and roll
back). Nothing else has to be installed first. Updates keep the current and previous version and prune
older ones; on a Linux desktop the launcher relaunches itself after one.

**Docker is the fallback** for platforms with no pinned runtime — Intel Macs and arm Linux. There the
launcher writes **one** file, the release `docker-compose.release.yml`, into a per-user folder and
shells out to `docker compose` exactly as you would by hand.

That file also declares the page browser's renderer sidecar (ni-format §35) as the `render` profile,
on the image `ghcr.io/securecloudgroup/smartbrain_3000-render` that every release publishes with the
app image's tags. On the handshake's 30-second beat the launcher reads the app's
`/api/status/overview` (with its local token; the app answers it while locked too). When a card first
needs the page browser (`renderer.wanted`) and the sidecar is `missing` — or `stale`, left on another
build by an update — it runs `docker compose -f docker-compose.release.yml --profile render up -d
render`, with one log line; the service's `pull_policy: always` makes that same command recreate a
stale sidecar on the newer image. An app from before the `renderer` field is read by its
`browsers[]` rows instead (reason `docker_no_sidecar`), and a page browser switched off (every row
`disabled`) is never started. The service is named on purpose: a bare `--profile render up -d`
converges every service, and the app's `pull_policy: always` would then install a downloaded update
without the click. After any start the launcher waits 10 minutes before another — for a sidecar
still booting, a failed start, or a stale one whose newer image is not published yet — and the app
going away re-arms it; a failure is logged and never touches the app. **Stop** runs `down` with the
profile too (a plain `down` leaves the sidecar running), and `--no-render` (`smartbrain --no-render`,
`smartbrain run --no-render`) keeps the launcher from ever starting it.

`resolveNativeMode` (`main.go`) picks between them: native where supported, Docker otherwise.
`SMARTBRAIN_NATIVE=1` / `=0` forces it, which is for development rather than for users — which stack
runs is not a decision worth asking anyone to make.

Per-user state lives in:

- macOS: `~/Library/Application Support/SmartBrain/`
- Windows: `%APPDATA%\SmartBrain\`
- Linux (XDG, so two folders): the launcher and your data in `~/.local/share/smartbrain/` (data under
  `data/`); runtime, config and logs in `~/.config/SmartBrain/`

On the native path your knowledge is a file under that folder; on the Docker path it lives in named
volumes (`smartbrain_data`, `bifrost_data`) — not bind mounts, deliberately: on Linux a
compose-created bind directory is root-owned and the non-root containers cannot write to it. Back up
with the in-app encrypted backup (Settings → Account & Data); uninstalling the launcher never touches
either.

## Why it exists

The target user won't clone a repo or run a build. This gives them an app icon that Just Works, while
keeping the honest, transparent core: the launcher owns no hidden state, and every artifact it
downloads is verified against a checksum pinned in this repo before it runs.

## No code-signing certificate (on purpose)

There is no paid Apple/Microsoft signing cert. That's fine because of how it's delivered:

- **macOS** binaries are **ad-hoc signed** (`codesign -s -`) — free, needs no Apple account, and is all
  Apple Silicon requires to run. The app ships via **Homebrew**, which installs without applying the
  quarantine flag, so Gatekeeper never shows the "unidentified developer" wall.
- **Windows** ships via **Scoop** (winget pending re-publication), which doesn't apply
  Mark-of-the-Web, so SmartScreen's "Windows protected your PC" prompt never fires for an
  unsigned exe.

A browser-downloaded `.dmg`/`.exe` is the *one* channel that would still warn — so we don't use it.

## Layout

| Path | What |
|---|---|
| `main.go` | tray UI glue (systray); thin, CGO on macOS |
| `native/` | the Docker-free stack: verified downloads, atomic version flip, rollback |
| `stack/` | Docker orchestration — no GUI dependency, unit-tested on any platform |
| `update/` | launcher self-update, and the Ed25519 release-signature check |
| `cmd/sbsign/` | generates the release signing key and signs release checksums (CI) |
| `icon/` | the tray icons (SB monogram) + their generator; the Dock icon comes from `tools/brand/make_icons.py` |
| `docker-compose.release.yml` | embedded copy of the release stack; CI fails if it drifts from `compose/` |

## Build locally

Needs Go (see `go.mod`). On its native OS:

```sh
cd launcher
go test ./stack/...            # the orchestration logic
go build -o smartbrain .       # the app (macOS needs Xcode CLT for cgo; Windows is pure Go)
```

CI (`.github/workflows/launcher.yml`) builds the macOS universal `.app` (ad-hoc signed) and the
Windows `.exe`, and attaches them to the GitHub Release on a version tag.

> Icons: the menu-bar / tray glyph is a deliberate "SB" monogram template icon (macOS tints
> template icons for light/dark automatically — a detailed mark would render as mush there);
> the Dock/Finder icon is derived from the brand mascot by `tools/brand/make_icons.py`.
