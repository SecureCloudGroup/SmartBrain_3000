#!/usr/bin/env bash
#
# vps-sync-web.sh — runs ON the RTC VPS (via the sb-web-sync systemd timer) to keep the served PWA
# shell and landing page in sync with the latest SmartBrain release. The VPS reaches OUT to GitHub
# (public repo, no credentials); nothing reaches IN — which is why this deploy needs no GitHub
# secrets and no inbound SSH. See docs/internal/vps-web-sync.md for the one-time install.
#
# It checks out the latest v* TAG (matching what the Desktop image ships) and rsyncs the committed,
# already-built shell + landing into the Caddy-served dirs. Idempotent: a no-op when already current.
set -euo pipefail

REPO_DIR="${SB_REPO_DIR:-$HOME/sb-node/src/SmartBrain_3000}"
WEB_DEST="${SB_WEB_DEST:-$HOME/sb-node/compose/web}"
LANDING_DEST="${SB_LANDING_DEST:-$HOME/sb-node/landing}"

cd "$REPO_DIR"
git fetch --tags --prune --quiet origin

latest="$(git tag -l 'v*' | sort -V | tail -1)"
[ -n "$latest" ] || { echo "no v* release tag found — nothing to deploy"; exit 1; }

current="$(git describe --tags --exact-match 2>/dev/null || echo 'none')"
if [ "$current" = "$latest" ]; then
  echo "already on $latest — nothing to do"
  exit 0
fi

git checkout --quiet --force "$latest"

# A guard: an empty/broken checkout must NEVER reach the rsync --delete below (it would wipe the
# live shell). Refuse unless the built shell is actually present at this tag.
[ -f app/smartbrain_3000/web/index.html ] && [ -d app/smartbrain_3000/web/_app ] \
  || { echo "built shell missing at $latest — refusing to deploy"; exit 1; }

rsync -a --delete app/smartbrain_3000/web/ "$WEB_DEST/"
[ -f landing/index.html ] && rsync -a landing/ "$LANDING_DEST/"

echo "deployed $latest -> shell $(cat app/smartbrain_3000/web/_app/version.json) + landing"
