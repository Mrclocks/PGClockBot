#!/usr/bin/env bash
# Install / refresh the global `pgclock` command.
# Usage (from project root):
#   sudo bash scripts/install_global_cli.sh
#   sudo bash scripts/install_global_cli.sh /path/to/PGClockBot
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT="$(cd "${1:-$DEFAULT_ROOT}" && pwd)"
# Overridable only so tests can install into a temporary prefix.
LIB="${PGCLOCK_CLI_LIB:-/usr/local/lib/pgclockbot}"
BIN="${PGCLOCK_CLI_BIN:-/usr/local/bin/pgclock}"
WRAPPER_SRC="${ROOT}/scripts/pgclock"

if [[ ! -f "${ROOT}/run.py" || ! -d "${ROOT}/app" || ! -f "${ROOT}/pgclock.sh" ]]; then
  echo "Not a PGClockBot install: ${ROOT}" >&2
  exit 1
fi
if [[ ! -f "${WRAPPER_SRC}" ]]; then
  echo "Missing wrapper: ${WRAPPER_SRC}" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 && -z "${PGCLOCK_CLI_LIB:-}" ]]; then
  echo "Re-run with sudo: sudo bash scripts/install_global_cli.sh ${ROOT}" >&2
  exit 1
fi

install -d -m 755 "${LIB}"
printf '%s\n' "${ROOT}" > "${LIB}/install_root"
chmod 644 "${LIB}/install_root"

# One wrapper for every command: scripts/pgclock finds the install from the
# marker, so it works from any directory.
install -d -m 755 "$(dirname "${BIN}")"
install -m 755 "${WRAPPER_SRC}" "${BIN}"
# Older installs kept a second copy here; one wrapper is enough.
rm -f "${LIB}/pgclock-wrapper"

echo "Installed global CLI:"
echo "  ${BIN}"
echo "  install_root → ${ROOT}"
echo
echo "From any directory:"
echo "  pgclock            # menu"
echo "  pgclock update     # safe update"
echo "  pgclock status"
echo "  pgclock help"
