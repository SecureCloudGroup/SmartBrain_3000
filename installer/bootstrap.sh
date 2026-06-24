#!/bin/sh
# SmartBrain_3000 one-line bootstrap — clone the repo and run the installer.
#
#   curl -fsSL https://raw.githubusercontent.com/SecureCloudGroup/SmartBrain_3000/main/installer/bootstrap.sh | sh
#
# It checks the three prerequisites (git, Python 3, Docker), clones the repo into the
# current directory, and hands off to installer/install.py — which builds the image,
# starts the stack, and opens the app. It installs nothing system-wide and edits no files;
# if a prerequisite is missing it tells you exactly what to install, then stops.
set -eu

REPO_URL="https://github.com/SecureCloudGroup/SmartBrain_3000.git"
TARGET="${SMARTBRAIN_DIR:-SmartBrain_3000}"

say() { printf '\033[36m::\033[0m %s\n' "$1"; }
die() { printf '\033[31mError:\033[0m %s\n' "$1" >&2; exit 1; }

say "Checking prerequisites…"
command -v git >/dev/null 2>&1 || \
  die "git is required — install it (https://git-scm.com/downloads), then re-run this command."
command -v python3 >/dev/null 2>&1 || \
  die "Python 3 is required — install it (https://www.python.org/downloads/), then re-run this command."
command -v docker >/dev/null 2>&1 || \
  die "Docker is required — install Docker Desktop or Engine (https://docs.docker.com/get-docker/), start it, then re-run."

if [ -d "$TARGET/.git" ]; then
  say "Found an existing checkout in '$TARGET' — updating it."
  git -C "$TARGET" pull --ff-only || say "Couldn't fast-forward; using the existing checkout."
else
  say "Cloning into '$TARGET'…"
  git clone --depth 1 "$REPO_URL" "$TARGET"
fi

say "Starting the installer (first run builds the image — a few minutes)…"
cd "$TARGET"
exec python3 installer/install.py install
