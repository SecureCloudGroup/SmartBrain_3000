#!/usr/bin/env sh
# Bootstrap (macOS / Linux): find Python 3 and run the installer.
set -eu

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' 2>/dev/null; then
  PY=python   # only when `python` is actually Python 3 (not legacy python2)
else
  echo "Python 3 is required to run the installer. Install Python 3 and re-run." >&2
  exit 1
fi

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$PY" "$DIR/install.py" "$@"
