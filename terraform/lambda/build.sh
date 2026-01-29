#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/alert_ingest"
ZIP="$HERE/alert_ingest.zip"

rm -f "$ZIP"
# Zip the source (boto3 is in Lambda runtime; keep package minimal)
( cd "$HERE" && zip -r "alert_ingest.zip" "alert_ingest" -x "*.pyc" -x "__pycache__/*" ) >/dev/null
echo "Built $ZIP"
