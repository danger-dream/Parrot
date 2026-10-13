#!/bin/sh
# Explicit source/systemd installation only. Never restarts a service or edits config.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${1:-"$ROOT/venv/bin/python"}
"$PYTHON" "$ROOT/scripts/sync_go_tls_vendor.py" --check
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
cp -R "$ROOT/vendor/parrot-go-tls-client/." "$TMP/"
"$PYTHON" -m pip install --no-deps "$TMP"
"$PYTHON" "$ROOT/scripts/check_go_tls.py" --load
