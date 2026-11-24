#!/usr/bin/env bash
# Run the standalone browser-logic tests (tests/*.test.cjs) with Node.
#
# Tests that a Python test drives (it passes them generated input, like the service worker) are left
# to pytest; every other tests/*.test.cjs runs here, one after another. Exit status is non-zero when
# any of them fails, and the failing files are listed at the end.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
driven="$(grep -ho '[a-z0-9_]*\.test\.cjs' tests/*.py | sort -u)"
failed=()
ran=0
for test in tests/*.test.cjs; do
  name="$(basename "$test")"
  if grep -qx "$name" <<<"$driven"; then
    continue
  fi
  ran=$((ran + 1))
  if ! node "$test" >/tmp/node-test-$$.log 2>&1; then
    failed+=("$name")
    echo "FAIL $name"
    tail -n 20 /tmp/node-test-$$.log
  fi
done
rm -f /tmp/node-test-$$.log
if [ "${#failed[@]}" -gt 0 ]; then
  echo "node tests: ${#failed[@]} of $ran failed: ${failed[*]}" >&2
  exit 1
fi
echo "node tests: $ran passed (the rest run from pytest)"
