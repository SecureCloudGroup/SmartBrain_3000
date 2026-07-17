#!/usr/bin/env bash
#
# vps-sync-web.sh — runs ON the RTC VPS (via the sb-web-sync systemd timer) to keep the served PWA
# shell and landing page in sync with the latest SmartBrain release. The VPS reaches OUT to GitHub
# (public repo, no credentials); nothing reaches IN — so this needs no GitHub secrets and no inbound
# SSH. One-time install: docs/internal/vps-web-sync.md.
#
# It deploys the content of the latest v* TAG (matching what the Desktop image ships). It extracts
# that content with `git archive` into a temp dir and rsyncs it out — it deliberately NEVER checks the
# repo working tree out to a tag, because older release tags don't contain this very script and a
# checkout would delete it mid-run. The working tree only ever tracks `main` (so the tooling itself
# stays current). Idempotent; refuses an empty build so `rsync --delete` can't wipe the live shell.
set -euo pipefail

REPO_DIR="${SB_REPO_DIR:-$HOME/sb-node/src/SmartBrain_3000}"
WEB_DEST="${SB_WEB_DEST:-$HOME/sb-node/compose/web}"
LANDING_DEST="${SB_LANDING_DEST:-$HOME/sb-node/landing}"
STAMP="${SB_STAMP:-$HOME/sb-node/.web-deployed-tag}"

cd "$REPO_DIR"
git fetch --quiet --tags --prune origin
# Keep the deploy tooling itself current — this checkout exists only to run this script.
git reset --quiet --hard origin/main

latest="$(git tag -l 'v*' | sort -V | tail -1)"
[ -n "$latest" ] || { echo "no v* release tag found — nothing to deploy"; exit 1; }
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$latest" ]; then
  echo "already on $latest — nothing to do"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git archive "$latest" app/smartbrain_3000/web | tar -x -C "$tmp"            # shell: required
git archive "$latest" landing 2>/dev/null | tar -x -C "$tmp" || true        # landing: optional

# Never let an empty/broken extract reach the rsync --delete below (it would wipe the live shell).
[ -f "$tmp/app/smartbrain_3000/web/index.html" ] && [ -d "$tmp/app/smartbrain_3000/web/_app" ] \
  || { echo "built shell missing at $latest — refusing to deploy"; exit 1; }

rsync -a --delete "$tmp/app/smartbrain_3000/web/" "$WEB_DEST/"
[ -f "$tmp/landing/index.html" ] && rsync -a "$tmp/landing/" "$LANDING_DEST/"

echo "$latest" > "$STAMP"
echo "deployed $latest -> shell $(cat "$tmp/app/smartbrain_3000/web/_app/version.json") + landing"
