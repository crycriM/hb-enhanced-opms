#!/usr/bin/env bash
# Symlink deploy/hummingbot/* into a Hummingbot checkout (same relative paths).
# Re-run after a Hummingbot update. Never overwrites a real file.
set -euo pipefail
HB_ROOT="${1:-/home/christian/sources/hummingbot}"
SRC="$(cd "$(dirname "$0")/hummingbot" && pwd)"
cd "$SRC"
find . -type f ! -name '*.pyc' ! -path '*/__pycache__/*' | while read -r rel; do
  dest="$HB_ROOT/${rel#./}"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "refusing: $dest exists and is not a symlink" >&2; exit 1
  fi
  mkdir -p "$(dirname "$dest")"
  ln -sfn "$SRC/${rel#./}" "$dest"
  echo "linked $dest"
done
