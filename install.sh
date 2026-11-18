#!/usr/bin/env bash
# Compatibility wrapper — use: bash pgclock.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/pgclock.sh" install
