# SmartBrain launcher

A tiny menu-bar / system-tray app that makes SmartBrain one click to reach. It has no UI of its own —
the real app is the SvelteKit interface in your browser — so all it does is start the stack, wait
until it is healthy, and open `http://localhost:33000`.

The tray menu is just **Open**, a status line, **Stop**, **Restart**, and **Quit launcher** (which
leaves SmartBrain running).

## Two stacks, one launcher

**Native (Docker-free) is the default** wherever it can be assembled — macOS on Apple Silicon and
Windows today. There the launcher downloads a pinned Python runtime, a wheelhouse and the gateway
binary, verifies each against a checksum pinned in the source, and assembles a versioned install it
can flip atomically (and roll back). Nothing else has to be installed first.

**Docker is the fallback** for platforms with no pinned runtime — Linux and Intel Macs. There the
launcher writes **one** file, the release `docker-compose.release.yml`, into a per-user folder and
shells out to `docker compose` exactly as you would by hand.

`resolveNativeMode` (`main.go`) picks between them: native where supported, Docker otherwise.
`SMARTBRAIN_NATIVE=1` / `=0` forces it, which is for development rather than for users — which stack
runs is not a decision worth asking anyone to make.

Per-user state lives in:

- macOS: `~/Library/Application Support/SmartBrain/`
- Windows: `%APPDATA%\SmartBrain\`

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
- **Windows** ships via **winget / Scoop**, which don't apply Mark-of-the-Web, so SmartScreen's
  "Windows protected your PC" prompt never fires for an unsigned exe.

A browser-downloaded `.dmg`/`.exe` is the *one* channel that would still warn — so we don't use it.

## Layout

| Path | What |
|---|---|
| `main.go` | tray UI glue (systray); thin, CGO on macOS |
| `native/` | the Docker-free stack: verified downloads, atomic version flip, rollback |
| `stack/` | Docker orchestration — no GUI dependency, unit-tested on any platform |
| `update/` | launcher self-update, and the Ed25519 release-signature check |
| `cmd/sbsign/` | generates the release signing key and signs release checksums (CI) |
| `icon/` | the tray icons + the stdlib generator that made them (placeholder mark) |
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

> The icon is a neutral placeholder (a rounded square); swap in a designed asset when there is one.
