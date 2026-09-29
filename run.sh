#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Usage: ./run.sh daily --data-root /path/to/data-checkout [--dry-run]
exec "${PYTHON:-python3}" -m arxiv_daily.runner "$@"
