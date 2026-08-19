#!/usr/bin/env sh
# SmartBrain — Linux install (x86_64, Docker-free).
#
# What this does, in order, so you can audit it before running it:
#   1. Checks this is Linux/x86_64 with glibc (the bundled Python runtime needs it).
#   2. Downloads the latest release tarball + its sha256 sidecar + minisign signature.
#   3. Verifies the signature (when minisign is installed) and ALWAYS the sha256.
#   4. Unpacks to ~/.local/share/smartbrain/launcher/ and symlinks ~/.local/bin/smartbrain.
#      The private folder is required: self-updates swap the binary's WHOLE parent
#      directory atomically, so the binary must never sit directly in a shared bin dir
#      (the launcher also refuses to update itself out of one).
#   5. Installs a desktop entry + icon; with --headless it instead writes and starts a
#      systemd --user unit running `smartbrain run`.
#
# The app itself is assembled on first start FROM THE SAME RELEASE: a pinned Python
# runtime, the release's wheelhouse, and the local LLM gateway — every download
# sha256-verified by the launcher. Everything stays on your machine; the app serves on
# 127.0.0.1 only.
#
#   sh install-linux.sh              desktop install (tray app in your app menu)
#   sh install-linux.sh --headless   server install (systemd --user unit, no tray)
#   sh install-linux.sh --uninstall  remove the launcher; your data stays
#   sh install-linux.sh --purge      remove the launcher AND all data
set -eu

REPO="SecureCloudGroup/SmartBrain_3000"
ASSET="SmartBrain-linux-x86_64.tar.gz"
BASE="https://github.com/$REPO/releases/latest/download"
# The minisign public key releases are signed with. It must match the key compiled
# into the launcher (launcher/update/signature.go) — CI fails any release where the
# two drift apart.
MINISIGN_PUBKEY="RWRetWVGVuVVMv0qZngZ+daVts4oOfS2Oa6aKvGVrxaumYebL2Abn6kL"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
LAUNCHER_DIR="$DATA_HOME/smartbrain/launcher"
BIN_LINK="$HOME/.local/bin/smartbrain"
DESKTOP_FILE="$DATA_HOME/applications/smartbrain.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/512x512/apps/smartbrain.png"
UNIT_FILE="$CONFIG_HOME/systemd/user/smartbrain.service"

MODE=install
HEADLESS=0
for arg in "$@"; do
  case "$arg" in
    --headless) HEADLESS=1 ;;
    --uninstall) MODE=uninstall ;;
    --purge) MODE=purge ;;
    -h|--help)
      sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [ "$MODE" != "install" ]; then
  # Stop whatever is running, then remove only what THIS script created. Data is
  # never touched by --uninstall; --purge removes it after saying exactly what goes.
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now smartbrain.service 2>/dev/null || true
  fi
  if [ -x "$LAUNCHER_DIR/smartbrain" ]; then
    "$LAUNCHER_DIR/smartbrain" stop >/dev/null 2>&1 || true
  fi
  rm -f "$BIN_LINK" "$DESKTOP_FILE" "$ICON_FILE" "$UNIT_FILE"
  rm -rf "$LAUNCHER_DIR" "$LAUNCHER_DIR.previous"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  if [ "$MODE" = "purge" ]; then
    rm -rf "$DATA_HOME/smartbrain" "$CONFIG_HOME/SmartBrain"
    echo "SmartBrain removed, including all data."
  else
    echo "SmartBrain launcher removed. Your data is untouched:"
    echo "  knowledge + settings:  $DATA_HOME/smartbrain/data"
    echo "  runtime + gateway:     $CONFIG_HOME/SmartBrain (assembled program files, logs)"
    echo "Re-run this script to reinstall against it, or --purge to remove everything."
  fi
  exit 0
fi

# ---- preflight ------------------------------------------------------------------
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) ;;
  *)
    echo "This native build is Linux x86_64 only (you have: $(uname -s)/$(uname -m))." >&2
    echo "Other platforms run the Docker install — see the getting-started guide:" >&2
    echo "  https://github.com/$REPO#quick-start" >&2
    exit 1 ;;
esac
# The launcher binary is fully static, but the Python runtime it assembles is a glibc
# build — musl systems (Alpine) get an honest no rather than a confusing crash later.
if [ ! -e /lib64/ld-linux-x86-64.so.2 ] && [ ! -e /lib/ld-linux-x86-64.so.2 ]; then
  echo "glibc not found — Alpine/musl systems aren't supported natively yet." >&2
  echo "The Docker install works there: https://github.com/$REPO#quick-start" >&2
  exit 1
fi
command -v curl >/dev/null 2>&1 || { echo "curl is required." >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "tar is required." >&2; exit 1; }

# ---- download + verify ----------------------------------------------------------
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
echo "Downloading the latest SmartBrain release..."
for f in "$ASSET" "$ASSET.sha256" "$ASSET.sha256.minisig"; do
  curl -fsSL -o "$TMP/$f" "$BASE/$f"
done

cd "$TMP"
if command -v minisign >/dev/null 2>&1; then
  # The signature covers the checksum sidecar; the checksum covers the tarball.
  minisign -Vm "$ASSET.sha256" -P "$MINISIGN_PUBKEY" >/dev/null
  echo "Signature verified (minisign)."
else
  echo "NOTE: minisign is not installed, so the release signature was NOT checked." >&2
  echo "      The sha256 checksum still is, over TLS from GitHub. For full" >&2
  echo "      verification: install minisign and re-run this script." >&2
fi
sha256sum -c "$ASSET.sha256"

# ---- install --------------------------------------------------------------------
mkdir -p "$LAUNCHER_DIR" "$HOME/.local/bin" \
  "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")" "$(dirname "$UNIT_FILE")"
tar -xzf "$ASSET" -C "$LAUNCHER_DIR"
ln -sf "$LAUNCHER_DIR/smartbrain" "$BIN_LINK"

# Desktop entry + icon: harmless on a server, and means the app menu Just Works the
# moment a desktop exists. Exec gets the real path — .desktop files don't do $PATH.
sed "s|__LAUNCHER__|$LAUNCHER_DIR/smartbrain|" "$LAUNCHER_DIR/smartbrain.desktop" > "$DESKTOP_FILE"
cp "$LAUNCHER_DIR/smartbrain.png" "$ICON_FILE"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true

case ":$PATH:" in
  *:"$HOME/.local/bin":*) ;;
  *)
    echo "NOTE: $HOME/.local/bin is not on your PATH yet. On most distros (Ubuntu,"
    echo "      Debian) just log out and back in — your shell adds it automatically"
    echo "      once the directory exists. Until then, use the full path:"
    echo "      $HOME/.local/bin/smartbrain" ;;
esac

VERSION=$("$LAUNCHER_DIR/smartbrain" version | head -1)
echo "Installed: $VERSION -> $LAUNCHER_DIR"

if [ "$HEADLESS" = "1" ]; then
  # A --user unit, not a system one: SmartBrain is per-user by design (your data
  # lives in YOUR home, the ports are loopback). Under systemd the launcher's
  # self-update swaps the binary and exits; Restart= brings up the new version.
  cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=SmartBrain (local-first AI assistant)

[Service]
ExecStart=$LAUNCHER_DIR/smartbrain run
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
  if ! systemctl --user daemon-reload 2>/dev/null; then
    echo "systemd --user isn't reachable in this session (common over bare SSH)." >&2
    echo "The unit is written to $UNIT_FILE — from a login session run:" >&2
    echo "  systemctl --user enable --now smartbrain" >&2
    exit 1
  fi
  systemctl --user enable --now smartbrain.service
  echo
  echo "SmartBrain is starting (first run downloads the app — give it a minute),"
  echo "then serves at http://127.0.0.1:33000 on this machine."
  echo "To keep it running after you log out (and start it at boot):"
  echo "  sudo loginctl enable-linger $USER"
  echo "Manage it with: systemctl --user status|restart|stop smartbrain"
else
  echo
  echo "Start it with: smartbrain start   (or launch SmartBrain from your app menu)"
  echo "First run downloads the app, then your browser opens at http://localhost:33000."
fi
