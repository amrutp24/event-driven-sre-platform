#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/runbook_action"
ZIP="$HERE/runbook_action.zip"

rm -f "$ZIP"
# Build a minimal vendor dir for dependencies
TMP="$(mktemp -d)"
python3 -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --upgrade pip >/dev/null
"$TMP/venv/bin/pip" install -r "$SRC/requirements.txt" -t "$SRC/vendor" >/dev/null

( cd "$HERE" && zip -r "runbook_action.zip" "runbook_action" -x "*.pyc" -x "__pycache__/*" ) >/dev/null
rm -rf "$SRC/vendor"
rm -rf "$TMP"
echo "Built $ZIP"
