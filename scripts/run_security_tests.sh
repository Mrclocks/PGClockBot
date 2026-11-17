#!/usr/bin/env bash
# Run the curated security regression test suite.
#
# This is the "no more security regressions" safety net requested after the
# 2026-08 security audit: every access-control / tenant-isolation /
# secret-handling fix gets a regression test, and every such test file is
# listed in scripts/security_test_manifest.txt so it always runs here — on
# every push/PR (see .github/workflows/security-tests.yml) and locally
# before shipping a patch.
#
# Usage:
#   scripts/run_security_tests.sh            # run the curated suite
#   scripts/run_security_tests.sh -k pattern  # forwarded to pytest
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

MANIFEST="scripts/security_test_manifest.txt"
if [ ! -f "$MANIFEST" ]; then
  echo "error: $MANIFEST not found" >&2
  exit 1
fi

mapfile -t FILES < <(grep -v '^\s*#' "$MANIFEST" | grep -v '^\s*$')

missing=0
for f in "${FILES[@]}"; do
  if [ ! -f "$f" ]; then
    echo "error: manifest lists missing file: $f" >&2
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "Fix scripts/security_test_manifest.txt before running." >&2
  exit 1
fi

# NOTE: older audit test files used to hard-pin __version__ to the exact
# version at the time they were written (e.g. "4.10.9"), which drifted on
# every release bump and had nothing to do with the security behavior those
# files actually guard. Those assertions were all converted to forward-
# compatible "is_same_or_newer" checks (2026-08), so no deselect list is
# needed here anymore — any failure below is a real regression.

# Spread the files over every CPU when pytest-xdist is installed (it is in
# requirements-dev.txt); PYTEST_WORKERS=0 runs them serially, for example to
# debug a failure without interleaved output.
WORKER_ARGS=()
if python3 -c "import xdist" >/dev/null 2>&1; then
  WORKER_ARGS=(-n "${PYTEST_WORKERS:-auto}")
fi

echo "Running ${#FILES[@]} security regression test files..."
python3 -m pytest "${FILES[@]}" -q "${WORKER_ARGS[@]}" "$@"
