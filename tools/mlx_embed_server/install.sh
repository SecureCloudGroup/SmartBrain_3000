#!/bin/sh
# Install the SmartBrain MLX embeddings server as a macOS login service (LaunchAgent).
# Creates a venv under ~/.smartbrain/mlx-embed, installs mlx-embeddings, downloads the
# model on first start, and loads a LaunchAgent that keeps the server running at login.
# Idempotent: rerun to update. Uninstall: launchctl unload the plist and delete the dir.
set -eu

BASE="$HOME/.smartbrain/mlx-embed"
HERE="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.securecloudgroup.smartbrain.mlx-embed.plist"
PY="$(command -v python3.12 || command -v python3)"
MODEL="${SB_MLXE_MODEL:-mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ}"
PORT="${SB_MLXE_PORT:-8899}"

echo "python: $PY"
mkdir -p "$BASE"
[ -d "$BASE/.venv" ] || "$PY" -m venv "$BASE/.venv"
"$BASE/.venv/bin/pip" install --quiet --upgrade pip
"$BASE/.venv/bin/pip" install --quiet mlx-embeddings
cp "$HERE/serve.py" "$BASE/serve.py"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.securecloudgroup.smartbrain.mlx-embed</string>
  <key>ProgramArguments</key><array>
    <string>$BASE/.venv/bin/python</string>
    <string>$BASE/serve.py</string>
    <string>--model</string><string>$MODEL</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$BASE/server.log</string>
  <key>StandardErrorPath</key><string>$BASE/server.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "loaded LaunchAgent; first start downloads the model (~500 MB) — tail $BASE/server.log"
echo "then in SmartBrain: Settings -> Local models -> MLX embeddings -> Connect (port $PORT),"
echo "route Settings -> Model routing -> Embedding -> mlxe/qwen3-embedding-0.6b, and Reindex."
