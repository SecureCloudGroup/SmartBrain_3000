#!/bin/bash
#
# SmartBrain_3000 — full reset ("start completely fresh")
# =======================================================
#
# WHAT THIS IS
#   A guided, five-step recovery path for a machine whose SmartBrain install has
#   gone wrong in a way that a normal restart or update will not fix:
#
#     1. Take (or point at) an encrypted backup, and VERIFY it.
#     2. Stop the launcher and any running stack.
#     3. Remove every SmartBrain install artifact.
#     4. Install the latest version cleanly.
#     5. Restore the backup and verify the data actually came back.
#
#   You almost certainly do not need this. Try these first, in order:
#     * Quit and reopen SmartBrain from the menu bar.
#     * brew update && brew upgrade --cask smartbrain
#     * A hard-reload of the browser tab (Cmd-Shift-R).
#   A full reset re-downloads the whole app and can take 10-30 minutes on a
#   normal connection. It is here for when nothing else works.
#
# WHAT IT WILL NEVER DO
#   * It will never delete your backup file.
#   * It will never delete your data without first moving a copy somewhere safe.
#   * It will never remove anything before showing you the exact list and
#     asking you to type a confirmation word.
#
# HOW TO RUN IT
#   bash installer/full-reset.sh --help        # this text
#   bash installer/full-reset.sh --inventory   # show what is on this machine; change nothing
#   bash installer/full-reset.sh --dry-run     # walk the whole plan; execute nothing
#   bash installer/full-reset.sh               # do it for real
#
#   Optional: --backup-file /path/to/smartbrain-backup.duckdb
#             skips the "where is your backup?" prompt.
#
# PLATFORM
#   macOS only. This script uses macOS-specific locations and tools. What would
#   differ on Windows is noted inline at each step, marked "WINDOWS:". A Windows
#   version would be a separate PowerShell script; the shape of the five steps
#   is identical, only the paths and the process/stop mechanics change.
#
# WRITTEN FOR SOMEONE READING IT AT 2AM
#   Linear, top to bottom. No functions doing surprising things. Every deletion
#   has a comment above it saying what the path is and why removing it is safe.

set -euo pipefail


# ---------------------------------------------------------------------------
# Section 0 — every path and name this script knows about
# ---------------------------------------------------------------------------
# These are read out of the app's own source, not guessed:
#   launcher/stack/stack.go, launcher/native/native.go, launcher/native/migrate.go,
#   launcher/update/update.go, app/smartbrain_3000/runtime.py, app/smartbrain_3000/db.py,
#   compose/docker-compose.release.yml, packaging/homebrew/Casks/smartbrain.rb.

# The app serves on loopback port 33000 in every install mode (Docker and native).
APP_URL="http://127.0.0.1:33000"
# The Settings page that hands out the encrypted backup.
BACKUP_PAGE_URL="$APP_URL/settings/account"

# The menu-bar launcher application bundle.
APP_BUNDLE="/Applications/SmartBrain.app"
# The launcher keeps exactly one previous version here when it self-updates.
APP_BUNDLE_PREVIOUS="/Applications/SmartBrain.app.previous"
# Half-finished self-updates leave a staging directory beside the bundle.
APP_UPDATE_STAGING_GLOB="/Applications/.smartbrain-update-*"

# Everything the launcher and the native (Docker-free) stack keep on disk.
# WINDOWS: this whole tree lives at %APPDATA%\SmartBrain instead.
SB_HOME="$HOME/Library/Application Support/SmartBrain"
SB_DATA="$SB_HOME/data"                                   # YOUR DATA. Moved, never deleted.
SB_NATIVE="$SB_HOME/native"                               # downloaded runtime + gateway + run/ pids and logs.
SB_COMPOSE_FILE="$SB_HOME/docker-compose.release.yml"     # written by the launcher on every start.
SB_NATIVE_MARKER="$SB_HOME/native-mode"                   # remembers "this machine runs native mode".

# The optional Apple-MLX embedding helper, if the user ever installed it by hand
# from tools/mlx_embed_server. Label: com.securecloudgroup.smartbrain.mlx-embed.
MLX_AGENT_PLIST="$HOME/Library/LaunchAgents/com.securecloudgroup.smartbrain.mlx-embed.plist"
MLX_DIR="$HOME/.smartbrain/mlx-embed"

# Docker artifacts. Container names are fixed in the compose file, so they carry
# no project prefix. Volume names DO carry the project prefix, which for a
# launcher-managed install is "smartbrain" (compose derives it from the
# directory basename, and the launcher runs compose from ~/Library/Application
# Support/SmartBrain).
DOCKER_CONTAINERS="smartbrain_3000 smartbrain_bifrost smartbrain_wireguard"
DOCKER_VOLUMES="smartbrain_smartbrain_data smartbrain_bifrost_data"
DOCKER_NETWORK="smartbrain_default"
DOCKER_IMAGES="ghcr.io/securecloudgroup/smartbrain_3000:latest ghcr.io/securecloudgroup/bifrost:v1.6.4 smartbrain_3000:dev"

# Homebrew.
BREW_CASK="smartbrain"
BREW_INSTALL_REF="securecloudgroup/tap/smartbrain"

# Browser state is handled in step 6 and is NOT a shell problem -- see that
# section for the exact worker, caches and storage keys involved. It is listed
# here only so the reader knows this script has not forgotten about it: a stale
# service worker is the single most common reason a freshly reinstalled
# SmartBrain still looks broken, because the old worker keeps serving the OLD
# app shell out of its cache.

# A DuckDB file carries the ASCII bytes "DUCK" at offset 8. Checked directly so
# this script needs no Python, no duckdb CLI, and no running app to tell a real
# backup from a truncated download or an HTML error page saved by mistake.
DUCKDB_MAGIC="DUCK"
DUCKDB_MAGIC_OFFSET=8
# The app's own migration code calls a database under 4096 bytes "suspiciously
# small" (launcher/native/migrate.go). We use the same floor as a hard refusal,
# and separately ask for confirmation below 1 MiB, because a real vault with the
# app's full schema is far larger than that.
BACKUP_MIN_BYTES=4096
BACKUP_SMALL_BYTES=1048576
# A backup older than this gets an extra confirmation. It is still allowed --
# an old backup is much better than none -- but you should know what you are
# about to restore.
BACKUP_STALE_DAYS=7


# ---------------------------------------------------------------------------
# Section 1 — modes and small helpers
# ---------------------------------------------------------------------------

MODE="run"            # run | dry-run | inventory | help
BACKUP_SRC=""         # --backup-file
STAMP="$(date +%Y%m%d-%H%M%S)"
# Recorded before anything is removed. The native-mode marker is one of the
# files we delete, and a fresh install without it goes back to Docker mode --
# so we have to remember the setting in order to tell the user about it.
WAS_NATIVE="no"
[ -f "$HOME/Library/Application Support/SmartBrain/native-mode" ] && WAS_NATIVE="yes"
# Where this script parks anything it takes away from you. It lives in your home
# directory, outside every path this script deletes, and it is never removed.
SAFE_DIR="$HOME/SmartBrain-reset-$STAMP"
BACKUP_KEPT=""        # the verified copy inside SAFE_DIR, set in step 1

say()  { printf '%s\n' "$*"; }
warn() { printf '  !  %s\n' "$*"; }
step() { printf '\n==================================================================\n%s\n==================================================================\n' "$*"; }
rule() { printf -- '------------------------------------------------------------------\n'; }

# Print a command the way a person would type it, quoting anything with a space
# so a path like "Application Support" cannot be misread as two arguments.
show_cmd() {
  local out="" arg
  for arg in "$@"; do
    case "$arg" in
      *[[:space:]]*) out="$out '$arg'" ;;
      *)             out="$out $arg" ;;
    esac
  done
  printf '      $%s\n' "$out"
}

# Echo a command, then run it -- or, in a dry run, only echo it.
run() {
  show_cmd "$@"
  if [ "$MODE" = "dry-run" ]; then
    return 0
  fi
  "$@"
}

# Same, but a non-zero exit is expected and fine (e.g. removing something that
# is already gone). Used only where failure genuinely does not matter.
run_ok() {
  show_cmd "$@"
  if [ "$MODE" = "dry-run" ]; then
    return 0
  fi
  "$@" || true
}

# Ask the user to type an exact word. Anything else aborts the whole script.
# In a dry run we say what would be asked and carry on, so that --dry-run can
# print the entire plan without a human at the keyboard.
confirm_typed() {
  local want="$1" prompt="$2" answer=""
  if [ "$MODE" = "dry-run" ]; then
    say ""
    say "  [dry run] At this point you would have to type: $want"
    return 0
  fi
  say ""
  printf '  %s\n  Type %s to continue, or anything else to stop: ' "$prompt" "$want"
  IFS= read -r answer || answer=""
  if [ "$answer" != "$want" ]; then
    say ""
    say "  Stopping. Nothing further was changed."
    exit 1
  fi
}

# Refuse to remove anything that is not an absolute path inside one of the two
# places SmartBrain installs into. This is a seatbelt against an empty or
# mangled variable turning a removal into something catastrophic.
remove_path() {
  local target="$1" reason="$2"
  case "$target" in
    "$HOME"/*|/Applications/*) : ;;
    *)
      warn "refusing to remove an unexpected path: $target"
      return 0
      ;;
  esac
  if [ ! -e "$target" ] && [ ! -L "$target" ]; then
    say "      (already gone) $target"
    return 0
  fi
  say "      $reason"
  run rm -rf -- "$target"
}

# True when $1 is $2 or lives underneath it.
path_is_inside() {
  case "$1" in
    "$2"|"$2"/*) return 0 ;;
    *) return 1 ;;
  esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# Is the app answering on loopback right now?
app_is_up() {
  curl --silent --show-error --fail --max-time 3 --output /dev/null "$APP_URL/api/health" 2>/dev/null
}


# ---------------------------------------------------------------------------
# Section 2 — command line
# ---------------------------------------------------------------------------

# Print the comment block at the top of this file, minus the "#" markers, and
# stop at the first line that is not a comment. Derived from the file itself so
# the help text and the documentation can never drift apart.
usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)      MODE="help" ;;
    --dry-run)      MODE="dry-run" ;;
    --inventory)    MODE="inventory" ;;
    --backup-file)  shift; BACKUP_SRC="${1:-}" ;;
    --backup-file=*) BACKUP_SRC="${1#--backup-file=}" ;;
    *)
      say "Unknown option: $1"
      say "Run: bash installer/full-reset.sh --help"
      exit 2
      ;;
  esac
  shift
done

if [ "$MODE" = "help" ]; then
  usage
  exit 0
fi

if [ "$(uname -s)" != "Darwin" ]; then
  say "This script is for macOS."
  say "WINDOWS: the equivalent paths are %APPDATA%\\SmartBrain and the Scoop/winget"
  say "package 'SecureCloudGroup.SmartBrain'; stop the tray app from Task Manager."
  exit 2
fi

if ! have curl; then
  say "curl is required and was not found. Stopping."
  exit 2
fi

# Every path this script touches is built from $HOME. If it were empty, patterns
# like the pkill below would match far more than they should, so refuse outright.
if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
  say "HOME is not set to a real directory. Stopping."
  exit 2
fi


# ---------------------------------------------------------------------------
# Section 3 — inventory: what is actually on this machine
# ---------------------------------------------------------------------------
# Printed in every mode, before anything is touched, so you can see the real
# state of your machine rather than a generic list.

report_line() {
  # $1 = label, $2 = path
  if [ -e "$2" ] || [ -L "$2" ]; then
    printf '  present   %-58s %s\n' "$1" "$2"
  else
    printf '  absent    %-58s %s\n' "$1" "$2"
  fi
}

step "What is on this machine right now"

say "Launcher application:"
report_line "menu-bar launcher" "$APP_BUNDLE"
report_line "previous launcher kept by the self-updater" "$APP_BUNDLE_PREVIOUS"
for staging in $APP_UPDATE_STAGING_GLOB; do
  [ -e "$staging" ] || continue
  printf '  present   %-58s %s\n' "leftover update staging directory" "$staging"
done

say ""
say "Application-support tree ($SB_HOME):"
report_line "YOUR DATA (encrypted vault)" "$SB_DATA"
report_line "downloaded native runtime + gateway" "$SB_NATIVE"
report_line "launcher-written compose file" "$SB_COMPOSE_FILE"
report_line "native-mode marker" "$SB_NATIVE_MARKER"
if [ -f "$SB_DATA/smartbrain.duckdb" ]; then
  printf '  present   %-58s %s bytes\n' "the vault file itself" \
    "$(stat -f %z "$SB_DATA/smartbrain.duckdb")"
fi

say ""
say "Running processes:"
if pgrep -f "$APP_BUNDLE/Contents/MacOS/SmartBrain" >/dev/null 2>&1; then
  say "  present   the menu-bar launcher is running"
else
  say "  absent    the menu-bar launcher is not running"
fi
if pgrep -f "$SB_NATIVE" >/dev/null 2>&1; then
  say "  present   a native stack process is running"
else
  say "  absent    no native stack process is running"
fi
if app_is_up; then
  say "  present   the app answers on $APP_URL"
else
  say "  absent    nothing is answering on $APP_URL"
fi

say ""
say "Docker:"
if have docker && docker info >/dev/null 2>&1; then
  for c in $DOCKER_CONTAINERS; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
      say "  present   container   $c"
    else
      say "  absent    container   $c"
    fi
  done
  # The exact volume names above cover a launcher-managed install. Anyone who
  # ran `docker compose up` by hand from another directory gets volumes named
  # after THAT directory, so we also list anything that looks like ours.
  found_vols="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -i -E 'smartbrain|bifrost' || true)"
  if [ -n "$found_vols" ]; then
    say "  present   volumes that look like SmartBrain's:"
    printf '%s\n' "$found_vols" | sed 's/^/              /'
  else
    say "  absent    no SmartBrain-looking Docker volumes"
  fi
else
  say "  (Docker is not installed or its daemon is not running -- nothing to check.)"
fi

say ""
say "Homebrew:"
if have brew; then
  if brew list --cask 2>/dev/null | grep -qx "$BREW_CASK"; then
    say "  present   cask        $BREW_CASK"
  else
    say "  absent    cask        $BREW_CASK"
  fi
else
  say "  (Homebrew is not installed.)"
fi

say ""
say "Optional Apple-MLX embedding helper:"
report_line "launch agent" "$MLX_AGENT_PLIST"
report_line "helper directory" "$MLX_DIR"

if [ "$MODE" = "inventory" ]; then
  say ""
  say "Inventory only. Nothing was changed."
  exit 0
fi

if [ "$MODE" = "dry-run" ]; then
  say ""
  say "DRY RUN. Every command below is printed but not executed."
fi


# ---------------------------------------------------------------------------
# Section 4 — STEP 1: back up first, and verify it
# ---------------------------------------------------------------------------
# This step is the gate. The script will not go any further without a file that
# exists, is a real SmartBrain database, and is a plausible size. There is no
# flag to skip it.
#
# The backup comes from the app itself: Settings -> Account & Data ->
# "Download encrypted backup". It is the complete encrypted database, wrapped
# keys included, so it restores with the SAME passphrase you use today.
#
# We deliberately do NOT take the backup for you. That endpoint (POST /api/backup)
# requires your passphrase to be re-entered, and asking for a passphrase in a
# shell script -- then putting it in a JSON body -- is a worse idea than clicking
# a button in the app you already trust. So: you click, we verify.

step "STEP 1 of 5 — back up first"

say "SmartBrain keeps everything in one encrypted database. Once this script"
say "removes things, that database is only as recoverable as your backup, so we"
say "check the backup carefully before touching anything."
say ""
say "To take a fresh backup:"
say "  1. Open SmartBrain and unlock it."
say "  2. Go to Settings -> Account & Data."
say "  3. Under 'Export & backup', re-enter your passphrase and click"
say "     'Download encrypted backup'."
say "  4. It saves as smartbrain-backup.duckdb, normally in ~/Downloads."
say ""
say "The backup is encrypted and restores with the same passphrase you use now."
say "Keep your Emergency Kit (Recovery Key) handy as well -- without one of the"
say "two, an encrypted backup cannot be opened by anyone, including you."

if [ -z "$BACKUP_SRC" ] && app_is_up && [ "$MODE" != "dry-run" ]; then
  say ""
  printf '  The app is running. Open the backup page now? [y/N]: '
  IFS= read -r open_it || open_it=""
  case "$open_it" in
    y|Y) open "$BACKUP_PAGE_URL" >/dev/null 2>&1 || warn "could not open the browser; go to $BACKUP_PAGE_URL" ;;
  esac
elif ! app_is_up; then
  say ""
  warn "The app is not answering on $APP_URL right now."
  warn "If you cannot start it, you can still continue with an OLDER backup file."
  warn "You cannot continue without any backup at all."
fi

# Verify one candidate file. Prints exactly what it checked and why it failed.
# Returns 0 only if the file is a plausible SmartBrain backup.
verify_backup() {
  local f="$1" size head_magic age_days

  if [ -z "$f" ]; then
    warn "no file given."
    return 1
  fi
  # Expand a leading ~ typed by hand, since `read` does not do that for us. The
  # tilde here is deliberately a literal character to match against, not a path.
  # shellcheck disable=SC2088
  case "$f" in "~/"*) f="$HOME/${f#\~/}" ;; esac
  if [ ! -f "$f" ]; then
    warn "no such file: $f"
    return 1
  fi
  if [ ! -r "$f" ]; then
    warn "cannot read: $f"
    return 1
  fi

  # WINDOWS: stat -f is BSD syntax; on Linux/Git-Bash it would be `stat -c %s`.
  size="$(stat -f %z "$f")"
  say ""
  say "  Checking: $f"
  say "    size:     $size bytes"
  say "    modified: $(stat -f %Sm -t '%Y-%m-%d %H:%M:%S' "$f")"

  if [ "$size" -lt "$BACKUP_MIN_BYTES" ]; then
    warn "that is far too small to be a database. This is not a usable backup."
    return 1
  fi

  # The structural check: a DuckDB file has "DUCK" at byte offset 8. This is
  # what separates a real backup from a truncated download or a saved error page.
  head_magic="$(head -c $((DUCKDB_MAGIC_OFFSET + 4)) "$f" | tail -c 4)"
  if [ "$head_magic" != "$DUCKDB_MAGIC" ]; then
    warn "this is not a DuckDB file (its header does not say $DUCKDB_MAGIC)."
    warn "Make sure you downloaded 'Download encrypted backup' and not the JSON export."
    return 1
  fi
  say "    header:   $DUCKDB_MAGIC (a real DuckDB database)"

  # Second structural check: our vault always has a key_wraps table, and DuckDB
  # stores catalog names as plain text inside the file. This is the same table
  # the app's own restore validator looks for (db.is_smartbrain_db).
  if LC_ALL=C grep -q -a -m1 -- "key_wraps" "$f"; then
    say "    contents: found the key_wraps table (this is a SmartBrain vault)"
  else
    warn "could not find the key_wraps table name inside this file."
    warn "It is a DuckDB database, but it may not be a SmartBrain backup."
    confirm_typed "YES" "Use this file anyway?"
  fi

  if [ "$size" -lt "$BACKUP_SMALL_BYTES" ]; then
    warn "under 1 MB. A real vault is usually much bigger than this."
    confirm_typed "SMALL" "Continue with this unusually small backup?"
  fi

  # Age. An old backup is still far better than none; you just need to know.
  age_days=$(( ( $(date +%s) - $(stat -f %m "$f") ) / 86400 ))
  if [ "$age_days" -ge "$BACKUP_STALE_DAYS" ]; then
    warn "this backup is $age_days days old. Anything added since then is not in it."
    confirm_typed "OLD" "Continue with a $age_days-day-old backup?"
  fi

  BACKUP_SRC="$f"
  return 0
}

# Ask until we get a good file, or the user gives up. Bounded so this can never
# spin forever.
attempt=0
while [ "$attempt" -lt 5 ]; do
  if [ -n "$BACKUP_SRC" ] && verify_backup "$BACKUP_SRC"; then
    break
  fi
  BACKUP_SRC=""
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 5 ]; then
    say ""
    say "  No verified backup after 5 attempts. Stopping. Nothing was changed."
    exit 1
  fi
  say ""
  printf '  Full path to your backup file (or press Enter to stop): '
  IFS= read -r BACKUP_SRC || BACKUP_SRC=""
  if [ -z "$BACKUP_SRC" ]; then
    say ""
    say "  No backup, so there is nothing safe to do here. Stopping."
    say "  Nothing was changed."
    exit 1
  fi
done

# The backup must not be sitting inside anything we are about to remove. People
# do save backups into the app's own data folder; that would be a very bad day.
for danger in "$SB_HOME" "$APP_BUNDLE" "$APP_BUNDLE_PREVIOUS"; do
  if path_is_inside "$BACKUP_SRC" "$danger"; then
    say ""
    warn "Your backup is inside a folder this script removes:"
    warn "  $BACKUP_SRC"
    warn "Move it somewhere else first (your Desktop is fine), then run this again."
    exit 1
  fi
done

# Keep our own copy, in a directory nothing here ever deletes. Your original
# file is left exactly where it is -- this script never removes it.
say ""
say "  Keeping a second copy of your backup in a safe place:"
run mkdir -p "$SAFE_DIR"
BACKUP_KEPT="$SAFE_DIR/smartbrain-backup.duckdb"
run cp -p "$BACKUP_SRC" "$BACKUP_KEPT"
if [ "$MODE" != "dry-run" ]; then
  # Confirm the copy is byte-identical before we rely on it.
  if ! cmp -s "$BACKUP_SRC" "$BACKUP_KEPT"; then
    warn "the copy does not match the original. Stopping before anything is removed."
    exit 1
  fi
  say "      copy verified byte-for-byte"
  cat > "$SAFE_DIR/WHAT-IS-THIS.txt" <<EOF
This folder was created by SmartBrain's full-reset script on $STAMP.

  smartbrain-backup.duckdb   A verified copy of the encrypted backup used for
                             the reset. It opens with the passphrase you used
                             at the time it was taken.

  data-before-reset/         If present: the entire SmartBrain data folder as
                             it was before the reset, moved here rather than
                             deleted.

  docker-volume-*/           If present: a copy of the Docker volume contents
                             from before the reset.

Do not delete this folder until you have confirmed your data is back and the
app works. Nothing else on this machine depends on it.
EOF
  say "      wrote $SAFE_DIR/WHAT-IS-THIS.txt"
fi

say ""
say "  Backup accepted:"
say "    yours:  $BACKUP_SRC   (left untouched)"
say "    copy:   $BACKUP_KEPT"


# ---------------------------------------------------------------------------
# Section 5 — the single gate in front of everything destructive
# ---------------------------------------------------------------------------
# Everything above this line only reads. Everything below removes things. The
# full list is printed first so there are no surprises.

step "What will be removed"

say "Removed (all of it re-downloadable, none of it your content):"
say "  * $APP_BUNDLE"
say "      the menu-bar launcher application."
say "  * $APP_BUNDLE_PREVIOUS"
say "      the one previous launcher the self-updater keeps for rollback."
say "  * $APP_UPDATE_STAGING_GLOB"
say "      leftovers from an update that was interrupted."
say "  * $SB_NATIVE"
say "      the downloaded Python runtime, the app wheels, the Bifrost gateway"
say "      binary, its config, pid files and logs. All re-downloaded on install."
say "      Your model provider API keys are NOT lost: they live in the encrypted"
say "      vault and are pushed back into the gateway every time you unlock."
say "  * $SB_COMPOSE_FILE"
say "      the compose file. The launcher writes a fresh one on every start."
say "  * $SB_NATIVE_MARKER"
say "      a one-line marker recording which stack mode this machine uses."
say "  * Docker containers: $DOCKER_CONTAINERS"
say "      stopped app and gateway containers. Recreated on install."
say "  * Docker images: $DOCKER_IMAGES"
say "      cached images. Re-pulled on install. This is most of the download time."
say "  * Homebrew cask '$BREW_CASK' (reinstalled in step 4)."
say ""
say "Moved somewhere safe, never deleted:"
say "  * $SB_DATA"
say "      YOUR DATA -- the encrypted vault, its write-ahead log, and any"
say "      previous restore snapshots. Moved to:"
say "      $SAFE_DIR/data-before-reset/"
say ""
say "Asked about separately, because it cannot be undone:"
say "  * Docker volumes: $DOCKER_VOLUMES"
say "      For a Docker-mode install this IS your data. Copied out first where"
say "      possible, then removed, and only after you confirm a second time."
say ""
say "Not touched by this script:"
say "  * Your backup file, wherever you keep it."
say "  * $SAFE_DIR and everything in it."
say "  * Docker Desktop, Homebrew itself, Ollama, or anything not SmartBrain."
say "  * Browser state -- a browser cannot be cleared from a shell. Step 6"
say "    tells you exactly what to clear, and it matters: a stale service"
say "    worker will happily serve the OLD app after a clean reinstall."

confirm_typed "DELETE" "This removes the SmartBrain install from this machine."


# ---------------------------------------------------------------------------
# Section 6 — STEP 2: stop everything
# ---------------------------------------------------------------------------
# Order matters. The app must not be writing to the database while we move it,
# and Docker will refuse to remove a volume that a running container is using.

step "STEP 2 of 5 — stopping SmartBrain"

# 2a. The menu-bar launcher. Quitting it does NOT stop the stack (by design --
#     the stack processes are detached and outlive the launcher), so we stop the
#     stack separately below.
say "  Quitting the menu-bar launcher:"
run_ok pkill -f "$APP_BUNDLE/Contents/MacOS/SmartBrain"

# 2b. The native (Docker-free) stack: an app process and a gateway process,
#     both started detached by the launcher. Their pid files are here, but we
#     match on the install path instead, which also catches a process whose pid
#     file was lost. The pattern is scoped to the SmartBrain install directory
#     so an unrelated bifrost-http elsewhere on the machine is left alone.
# WINDOWS: taskkill /IM SmartBrain.exe and stop the child python/bifrost
#          processes; there is no pkill.
say "  Stopping the native stack (if this machine runs one):"
run_ok pkill -f "$SB_NATIVE"
if [ "$MODE" != "dry-run" ]; then
  # Give them a moment to close the database cleanly before we escalate.
  waited=0
  while [ "$waited" -lt 10 ] && pgrep -f "$SB_NATIVE" >/dev/null 2>&1; do
    sleep 1
    waited=$((waited + 1))
  done
  if pgrep -f "$SB_NATIVE" >/dev/null 2>&1; then
    warn "a native process did not exit after 10 seconds; forcing it."
    run_ok pkill -9 -f "$SB_NATIVE"
    sleep 2
  fi
fi

# 2c. The Docker stack. `compose down` from the launcher's own directory, so the
#     project name matches what the launcher used ("smartbrain"); then a direct
#     container removal as a fallback for an install whose compose file is gone.
if have docker && docker info >/dev/null 2>&1; then
  if [ -f "$SB_COMPOSE_FILE" ]; then
    say "  Stopping the Docker stack:"
    if [ "$MODE" = "dry-run" ]; then
      show_cmd docker compose -f "$SB_COMPOSE_FILE" down
    else
      ( cd "$SB_HOME" && docker compose -f "$SB_COMPOSE_FILE" down ) || \
        warn "compose down reported a problem; continuing with direct removal."
    fi
  fi
  say "  Removing any containers left behind:"
  for c in $DOCKER_CONTAINERS; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
      run_ok docker rm -f "$c"
    fi
  done
else
  say "  Docker is not running -- nothing to stop there."
fi

# 2d. Confirm the port really is free. If something still answers, the rest of
#     this script would be fighting a live process, so we stop instead.
if [ "$MODE" = "dry-run" ]; then
  say "  [dry run] Would then confirm nothing is answering on $APP_URL,"
  say "  [dry run] and stop here if something still is."
else
  sleep 2
  if app_is_up; then
    say ""
    warn "Something is still answering on $APP_URL."
    warn "Stopping here rather than removing files out from under a running app."
    warn "Restart the machine and run this script again."
    exit 1
  fi
  say "  Nothing is answering on $APP_URL."
fi


# ---------------------------------------------------------------------------
# Section 7 — STEP 3: remove the install
# ---------------------------------------------------------------------------

step "STEP 3 of 5 — removing the old install"

# 3a. Move YOUR DATA out of the way first, before anything is deleted. A move on
#     the same disk is instant and does not copy 34 MB around. If the script
#     dies immediately after this line, your data is still sitting in $SAFE_DIR.
say "  Moving your data somewhere safe (not deleting it):"
if [ -d "$SB_DATA" ]; then
  run mkdir -p "$SAFE_DIR"
  run mv "$SB_DATA" "$SAFE_DIR/data-before-reset"
  say "      your vault is now at $SAFE_DIR/data-before-reset/"
else
  say "      (no data folder at $SB_DATA -- nothing to move)"
fi

# 3b. The launcher application bundle. Code only, no data of yours inside it.
say ""
say "  Removing the launcher application:"
remove_path "$APP_BUNDLE" \
  "the menu-bar launcher application (reinstalled in step 4)"
remove_path "$APP_BUNDLE_PREVIOUS" \
  "the previous launcher the self-updater kept for rollback (code only)"
for staging in $APP_UPDATE_STAGING_GLOB; do
  [ -e "$staging" ] || continue
  remove_path "$staging" \
    "a staging directory left behind by an interrupted self-update (temporary files only)"
done

# 3c. The support tree, minus the data folder we already moved out.
say ""
say "  Removing the downloaded runtime and settings:"
remove_path "$SB_NATIVE" \
  "downloaded Python runtime, app wheels, gateway binary, its config, pids and logs -- all re-downloaded on install; provider keys are re-pushed from your vault at unlock"
remove_path "$SB_COMPOSE_FILE" \
  "the compose file the launcher writes on every start"
remove_path "$SB_NATIVE_MARKER" \
  "the one-line marker recording this machine's stack mode"
# Whatever is left in the support directory now is empty or unknown; remove the
# directory only if it is genuinely empty, so nothing unexpected is destroyed.
if [ -d "$SB_HOME" ] && [ "$MODE" != "dry-run" ]; then
  rmdir "$SB_HOME" 2>/dev/null && say "      removed the now-empty $SB_HOME" || \
    say "      leaving $SB_HOME (it still contains something this script did not create)"
fi

# 3d. Docker images. Removed after the containers, before the volumes, because
#     copying a volume out (below) may need one of these images.
if have docker && docker info >/dev/null 2>&1; then

  # 3e. Docker volumes. For a Docker-mode install this IS the vault, so it gets
  #     its own confirmation and its own copy-out.
  vols="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -i -E 'smartbrain|bifrost' || true)"
  if [ -n "$vols" ]; then
    say ""
    say "  These Docker volumes look like SmartBrain's:"
    printf '%s\n' "$vols" | sed 's/^/        /'
    say ""
    say "  On a Docker-mode install these hold your database. Removing them"
    say "  cannot be undone. You have a verified backup, and this script will"
    say "  try to copy their contents out first."

    # Copy each volume out using an image that is already on this machine. We do
    # not pull anything for this: if no suitable image is here, we say so plainly
    # rather than pretending a copy happened.
    copy_image=""
    for img in $DOCKER_IMAGES; do
      if docker image inspect "$img" >/dev/null 2>&1; then
        copy_image="$img"
        break
      fi
    done
    if [ -n "$copy_image" ]; then
      say ""
      say "  Copying volume contents to $SAFE_DIR (using the image you already have):"
      printf '%s\n' "$vols" | while IFS= read -r v; do
        [ -n "$v" ] || continue
        dest="$SAFE_DIR/docker-volume-$v"
        run mkdir -p "$dest"
        # Read-only mount of the volume, exactly as the app's own Docker->native
        # migration does it, so a failed copy cannot alter the volume.
        run_ok docker run --rm -v "$v:/from:ro" -v "$dest:/to" \
          --entrypoint sh "$copy_image" -c "cp -a /from/. /to/"
      done
    else
      say ""
      warn "No SmartBrain image is cached locally, so the volumes cannot be"
      warn "copied out without downloading one. Your verified backup is still"
      warn "the safety net here."
    fi

    confirm_typed "DELETE VOLUMES" "Remove the Docker volumes listed above. This cannot be undone."
    printf '%s\n' "$vols" | while IFS= read -r v; do
      [ -n "$v" ] || continue
      run_ok docker volume rm "$v"
    done
  fi

  say ""
  say "  Removing the cached Docker images (these are re-pulled on install):"
  for img in $DOCKER_IMAGES; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      run_ok docker rmi "$img"
    fi
  done

  say ""
  say "  Removing the stack's Docker network:"
  if docker network ls --format '{{.Name}}' | grep -qx "$DOCKER_NETWORK"; then
    run_ok docker network rm "$DOCKER_NETWORK"
  else
    say "      (already gone) $DOCKER_NETWORK"
  fi
fi

# 3f. The optional Apple-MLX embedding helper. Only present if you installed it
#     by hand from tools/mlx_embed_server. It is a separate program that serves
#     embeddings; SmartBrain works without it.
if [ -e "$MLX_AGENT_PLIST" ] || [ -d "$MLX_DIR" ]; then
  say ""
  say "  The optional Apple-MLX embedding helper is installed."
  say "  It is a separate background service you added by hand. Removing it is"
  say "  part of a full reset, but SmartBrain does not require it either way."
  confirm_typed "YES" "Remove the MLX embedding helper as well?"
  run_ok launchctl unload "$MLX_AGENT_PLIST"
  remove_path "$MLX_AGENT_PLIST" \
    "the launch agent that starts the MLX embedding helper at login"
  remove_path "$MLX_DIR" \
    "the MLX helper's own directory (its virtualenv, script and log)"
fi

# 3g. Homebrew. Uninstall the cask so step 4 installs it fresh. The tap stays --
#     it is just a source list, and step 4 needs it.
if have brew && brew list --cask 2>/dev/null | grep -qx "$BREW_CASK"; then
  say ""
  say "  Removing the Homebrew cask (reinstalled in the next step):"
  run_ok brew uninstall --cask "$BREW_CASK"
fi

say ""
say "  Removal complete."


# ---------------------------------------------------------------------------
# Section 8 — STEP 4: install the latest version
# ---------------------------------------------------------------------------

step "STEP 4 of 5 — installing the latest version"

if have brew; then
  say "  Installing with Homebrew. This downloads the app and can take a while."
  run brew install --cask "$BREW_INSTALL_REF"
  say ""
  # A fresh install has no native-mode marker, so it starts in Docker mode. If
  # this machine was running the Docker-free native stack, that is a change the
  # user never asked for -- and on a machine without Docker it would simply
  # fail to start. So put the marker back. This is the exact file the launcher
  # writes itself (one line, "1"): it is a recorded preference, not install
  # state, which is why it is restored rather than left deleted.
  #
  # Passing SMARTBRAIN_NATIVE=1 via `open --env` would work on recent macOS but
  # is the wrong tool twice over: that flag does not exist on the older macOS
  # versions this app supports, and an environment variable is gone the moment
  # the user relaunches from Finder. The launcher writes this marker for exactly
  # that reason, so we write the same thing.
  if [ "$WAS_NATIVE" = "yes" ]; then
    say "  This machine was running the Docker-free (native) stack, and a fresh"
    say "  install would start in Docker mode. Restoring your native-mode setting:"
    run mkdir -p "$SB_HOME"
    if [ "$MODE" = "dry-run" ]; then
      show_cmd printf '1\n' ">" "$SB_NATIVE_MARKER"
    else
      printf '1\n' > "$SB_NATIVE_MARKER"
      chmod 600 "$SB_NATIVE_MARKER"
      say "      wrote $SB_NATIVE_MARKER"
    fi
  fi
  say ""
  say "  Starting SmartBrain:"
  run_ok open -a "$APP_BUNDLE"
else
  say "  Homebrew is not installed, so this script cannot install for you."
  say ""
  say "  Install Homebrew, then run:"
  say "      brew install --cask $BREW_INSTALL_REF"
  say ""
  say "  Or run the Docker-only stack instead:"
  say "      curl -fsSLO https://raw.githubusercontent.com/SecureCloudGroup/SmartBrain_3000/main/compose/docker-compose.release.yml"
  say "      docker compose -f docker-compose.release.yml up -d"
  say ""
  say "  Once it is running, restore from inside the app -- do NOT run this"
  say "  script again, because it would remove the install you just made:"
  say "      Settings -> Account & Data -> Restore -> choose this file:"
  say "      $BACKUP_KEPT"
  say "  then restart SmartBrain and unlock with that backup's passphrase."
  say ""
  say "  Your data from before the reset is at:"
  say "      $SAFE_DIR/data-before-reset/"
  exit 1
fi

# Wait for the fresh install to come up. First start pulls or assembles the whole
# stack, so this can genuinely take several minutes.
say ""
say "  Waiting for the app to start (up to 15 minutes on a first install)..."
if [ "$MODE" != "dry-run" ]; then
  waited=0
  while [ "$waited" -lt 900 ]; do
    if app_is_up; then
      break
    fi
    sleep 5
    waited=$((waited + 5))
    if [ $((waited % 60)) -eq 0 ]; then
      say "      still waiting... ($((waited / 60)) min)"
    fi
  done
  if ! app_is_up; then
    say ""
    warn "The app did not come up within 15 minutes."
    warn "Nothing is lost. Your backup is safe at:"
    warn "  $BACKUP_KEPT"
    warn "Your data from before the reset is at:"
    warn "  $SAFE_DIR/data-before-reset/"
    warn ""
    warn "Open SmartBrain from the menu bar and wait for it to report Running."
    warn "Then restore from inside the app -- do NOT run this script again,"
    warn "because it would remove the install you just made:"
    warn "  Settings -> Account & Data -> Restore -> choose the backup above,"
    warn "  then restart SmartBrain and unlock with that backup's passphrase."
    exit 1
  fi
  say "  The app is up."
fi


# ---------------------------------------------------------------------------
# Section 9 — STEP 5: restore, and check the data really came back
# ---------------------------------------------------------------------------
# The restore endpoint validates the upload, then stages it. It is applied on the
# NEXT start, because swapping a live database underneath a running app is not
# safe. So: upload, restart, then check.

step "STEP 5 of 5 — restoring your data"

if [ "$MODE" = "dry-run" ]; then
  say "  [dry run] Would upload $BACKUP_KEPT to $APP_URL/api/restore,"
  say "  [dry run] restart the app, and then confirm the vault reports itself"
  say "  [dry run] as initialized (which a genuinely fresh install does not)."
else
  # Sanity check before we upload: a fresh install reports initialized=false. If
  # it reports true, something survived the removal and we would be restoring
  # over a vault that is already there -- worth knowing about.
  status_before="$(curl --silent --max-time 5 "$APP_URL/api/account/status" | tr -d ' \n')"
  say "  Vault state before restore: $status_before"
  case "$status_before" in
    *'"initialized":false'*)
      say "      good -- this is a genuinely fresh install."
      ;;
    *'"initialized":true'*)
      warn "this install already has a vault. Some old state survived the reset."
      warn "Restoring on top of it will replace it. Your copy is at:"
      warn "  $SAFE_DIR/data-before-reset/"
      confirm_typed "RESTORE" "Restore your backup over the vault that is already there?"
      ;;
  esac

  say ""
  say "  Uploading the backup:"
  # x-sb-local: 1 marks this as a request from the Desktop itself. The app
  # refuses restore/backup/export from a paired remote device, and the WebRTC
  # bridge strips this header, so a phone cannot forge it.
  restore_out="$(curl --silent --show-error --fail \
    --max-time 600 \
    --header 'x-sb-local: 1' \
    --header 'Content-Type: application/octet-stream' \
    --data-binary "@$BACKUP_KEPT" \
    "$APP_URL/api/restore" 2>&1)" || {
      say ""
      warn "The upload was rejected:"
      warn "  $restore_out"
      warn "Nothing was lost. Your backup is at: $BACKUP_KEPT"
      warn "Your previous data is at: $SAFE_DIR/data-before-reset/"
      exit 1
    }
  say "      the app replied: $restore_out"
  case "$restore_out" in
    *'"ok":true'*) : ;;
    *)
      warn "that is not the success reply we expected. Stopping."
      exit 1
      ;;
  esac

  # The staged file is applied at the next start, so restart now.
  say ""
  say "  Restarting SmartBrain so the restore is applied:"
  run_ok pkill -f "$APP_BUNDLE/Contents/MacOS/SmartBrain"
  run_ok pkill -f "$SB_NATIVE"
  if have docker && docker info >/dev/null 2>&1 && [ -f "$SB_COMPOSE_FILE" ]; then
    ( cd "$SB_HOME" && docker compose -f "$SB_COMPOSE_FILE" down ) || true
  fi
  sleep 3
  run_ok open -a "$APP_BUNDLE"

  say "  Waiting for it to come back..."
  waited=0
  while [ "$waited" -lt 300 ]; do
    if app_is_up; then
      break
    fi
    sleep 5
    waited=$((waited + 5))
  done
  if ! app_is_up; then
    warn "The app did not come back within 5 minutes."
    warn "Open it from the menu bar and check Settings -> Account & Data."
    warn "Your backup is still at: $BACKUP_KEPT"
    exit 1
  fi

  # THE ACTUAL VERIFICATION. A fresh install has no vault, so it reports
  # initialized=false. If it now reports initialized=true, the restored database
  # is in place and its key wraps were read -- that is the data coming back, not
  # this script saying so.
  say ""
  say "  Checking that your data actually came back:"
  status_after="$(curl --silent --max-time 10 "$APP_URL/api/account/status" | tr -d ' \n')"
  say "      vault state after restore: $status_after"
  case "$status_after" in
    *'"initialized":true'*)
      say "      the restored vault is in place."
      ;;
    *)
      warn "the app still reports no vault. The restore did not take effect."
      warn "Your backup is unharmed at: $BACKUP_KEPT"
      warn "Your previous data is unharmed at: $SAFE_DIR/data-before-reset/"
      exit 1
      ;;
  esac

  # Second, independent check: applying a staged restore consumes the staged
  # file and leaves the displaced database behind as a *.pre-restore-* snapshot.
  # Seeing that pair is proof the swap ran, not just that some vault exists.
  # This only applies to a native install -- in Docker mode the database lives
  # inside a Docker volume, not here, so we say so rather than claim a check we
  # did not make.
  if [ -d "$SB_DATA" ]; then
    if [ -f "$SB_DATA/smartbrain.duckdb.restore" ]; then
      warn "the staged restore file is still there, so it was not applied yet."
      warn "Quit SmartBrain from the menu bar and open it again."
    else
      say "      the staged restore file was consumed (the swap ran)."
    fi
    if ls "$SB_DATA"/smartbrain.duckdb.pre-restore-* >/dev/null 2>&1; then
      say "      the database it replaced was kept as a *.pre-restore-* snapshot."
    fi
    if [ -f "$SB_DATA/smartbrain.duckdb" ]; then
      say "      new vault size: $(stat -f %z "$SB_DATA/smartbrain.duckdb") bytes"
    fi
  else
    say "      (this install keeps its database in a Docker volume, so there are"
    say "       no files to check here -- the vault state above is the answer.)"
  fi
fi


# ---------------------------------------------------------------------------
# Section 10 — STEP 6: clear the browser, or the old app comes back
# ---------------------------------------------------------------------------
# A shell script cannot reach into a browser's storage. This has to be done by
# hand, and skipping it is the most common way a correct reinstall still looks
# broken: the service worker registered by the OLD build keeps serving the OLD
# app shell from its cache. You reload, and nothing appears to have changed.

step "One more thing — clear the browser"

say "SmartBrain installs a service worker in your browser so it works offline."
say "That worker caches the old app and will keep serving it after a reinstall."
say "Clearing it is the difference between 'reset worked' and 'nothing changed'."
say ""
say "Do this in every browser you have opened SmartBrain in, and for every"
say "address you used it at (http://localhost:33000, http://127.0.0.1:33000,"
say "and any https://<your-mac>:33000 you set up for your phone)."
say ""
say "The quick way, in Chrome, Edge or Safari:"
say "  1. Open $APP_URL"
say "  2. Open the developer console (Cmd-Option-J in Chrome/Edge,"
say "     Cmd-Option-C in Safari -- Safari needs the Develop menu enabled)."
say "  3. Paste this, press Enter, then close the tab and reopen it:"
rule
cat <<'BROWSER_SNIPPET'
(async () => {
  // Unregister the service worker. Nothing in the app does this itself.
  const regs = await navigator.serviceWorker.getRegistrations();
  for (const r of regs) await r.unregister();

  // Delete its caches. "sb-pairing" holds your phone-pairing credential and is
  // deliberately kept across normal updates, so it has to go explicitly here.
  for (const k of await caches.keys()) await caches.delete(k);

  // The legacy pairing store.
  indexedDB.deleteDatabase("smartbrain-remote");

  // Saved preferences and UI state.
  ["theme", "sb-installed", "sbNav", "update-dismissed"]
    .forEach((k) => localStorage.removeItem(k));
  sessionStorage.removeItem("launcher-nudge-dismissed");

  console.log("SmartBrain browser state cleared. Close this tab and reopen the app.");
})();
BROWSER_SNIPPET
rule
say ""
say "The thorough way, if the snippet does not stick:"
say "  Chrome/Edge: Settings -> Privacy -> Third-party cookies -> See all site"
say "               data -> search 'localhost' -> delete."
say "  Safari:      Settings -> Privacy -> Manage Website Data -> search"
say "               'localhost' -> Remove."
say ""
say "If you ever installed SmartBrain as an app from the browser ('Install' in"
say "Chrome/Edge, 'Add to Dock' in Safari), remove that too -- it keeps its own"
say "separate storage. Look for 'SmartBrain_3000' in ~/Applications or in your"
say "browser's Chrome Apps folder."
say ""
say "After pairing caches are cleared, any phone you had paired must be paired"
say "again from Settings -> Remote access."


# ---------------------------------------------------------------------------
# Section 11 — where everything ended up
# ---------------------------------------------------------------------------

step "Done"

if [ "$MODE" = "dry-run" ]; then
  say "That was a dry run. Nothing on this machine was changed."
  say "Run the same command without --dry-run to do it for real."
  exit 0
fi

say "SmartBrain has been reinstalled and your backup restored."
say ""
say "What to do now:"
say "  1. Open SmartBrain and unlock it with the passphrase that backup was"
say "     taken with -- not a new one. If that fails, use your Recovery Key"
say "     from the Emergency Kit."
say "  2. Look at your chats, knowledge and tasks and confirm they are there."
say "     This script can confirm the vault loaded; only you can confirm the"
say "     contents are the ones you expected."
say "  3. Re-pair any phone from Settings -> Remote access."
say ""
say "Kept for you, and safe to delete once you are happy:"
say "  $SAFE_DIR"
say "    smartbrain-backup.duckdb    the backup this reset used"
say "    data-before-reset/          your data folder exactly as it was"
say "    docker-volume-*/            Docker volume contents, if you had any"
say ""
say "Your own backup file was not touched:"
say "  $BACKUP_SRC"
say ""
say "If something is still wrong, that folder is the way back. Do not delete it"
say "until you are sure."
