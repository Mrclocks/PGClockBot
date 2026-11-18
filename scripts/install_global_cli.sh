#!/usr/bin/env bash
# Install / refresh the global `pgclock` command.
# Usage (from project root):
#   sudo bash scripts/install_global_cli.sh
#   sudo bash scripts/install_global_cli.sh /path/to/PGClockBot
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT="$(cd "${1:-$DEFAULT_ROOT}" && pwd)"
LIB="/usr/local/lib/pgclockbot"
BIN="/usr/local/bin/pgclock"
WRAPPER_SRC="${ROOT}/scripts/pgclock"

if [[ ! -f "${ROOT}/run.py" || ! -d "${ROOT}/app" ]]; then
  echo "Not a PGClockBot install: ${ROOT}" >&2
  exit 1
fi
if [[ ! -f "${WRAPPER_SRC}" ]]; then
  echo "Missing wrapper: ${WRAPPER_SRC}" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Re-run with sudo: sudo bash scripts/install_global_cli.sh ${ROOT}" >&2
  exit 1
fi

install -d -m 755 "${LIB}"
printf '%s\n' "${ROOT}" > "${LIB}/install_root"
chmod 644 "${LIB}/install_root"

# Write wrapper to a real temp file first — `install /dev/stdin` fails on some
# distros/sudo pipes with "No such file or directory".
tmp_bin="$(mktemp)"
cat > "${tmp_bin}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
MARKER="/usr/local/lib/pgclockbot/install_root"
ROOT="${PGCLOCK_HOME:-}"
if [[ -z "${ROOT}" && -f "${MARKER}" ]]; then
  ROOT="$(tr -d '\r' < "${MARKER}" | head -n1)"
fi
if [[ -z "${ROOT}" || ! -d "${ROOT}" ]]; then
  echo "pgclock: install root not found (marker ${MARKER})." >&2
  exit 1
fi
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3 || true)"
fi
if [[ -z "${PY}" ]]; then
  echo "pgclock: python not found" >&2
  exit 1
fi
export PGCLOCK_HOME="${ROOT}"
cd "${ROOT}"
exec "${PY}" -m app.cli "$@"
EOF
chmod 755 "${tmp_bin}"
install -m 755 "${tmp_bin}" "${BIN}"
rm -f "${tmp_bin}"

# Also keep a copy of the repo wrapper for reference / non-root use
install -m 755 "${WRAPPER_SRC}" "${LIB}/pgclock-wrapper"

echo "Installed global CLI:"
echo "  ${BIN}"
echo "  install_root → ${ROOT}"
echo
echo "Try: pgclock status"
echo "     pgclock doctor"
