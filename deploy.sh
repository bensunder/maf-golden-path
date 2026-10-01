#!/usr/bin/env bash
# Install the whyaidata.com homepage into a web root, keeping a backup of what was there.
#   sudo bash deploy.sh /path/to/web/root
set -euo pipefail
ROOT="${1:?usage: deploy.sh /path/to/web/root}"
[ -d "$ROOT" ] || { echo "no such folder: $ROOT"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKUP="$ROOT/../whyaidata-backup-$(date +%Y%m%d-%H%M%S)"
cp -a "$ROOT" "$BACKUP"
echo "backed up the current site to $BACKUP"
cp "$HERE/index.html" "$ROOT/index.html"
mkdir -p "$ROOT/fonts" "$ROOT/img" && cp "$HERE"/fonts/* "$ROOT/fonts/" && cp "$HERE"/img/* "$ROOT/img/"
echo "installed index.html, fonts/ and img/ into $ROOT"
echo "undo: rm -rf '$ROOT' && mv '$BACKUP' '$ROOT'"
