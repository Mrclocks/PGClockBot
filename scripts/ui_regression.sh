#!/usr/bin/env bash
# Panel UI regression check: screenshot and inventory two versions of the panel under identical
# conditions, then compare them pixel for pixel and control for control.
#
#   scripts/ui_regression.sh [BASE_REF] [HEAD_REF]      (defaults: origin/dev and HEAD)
#
# Each ref is checked out into its own temporary worktree with its own throwaway database, the
# same sample data (scripts/ui_fixture.py) and the same local fake panel (scripts/ui_fake_panel.py),
# served on loopback and captured by scripts/ui_capture.cjs. Commit the change first: uncommitted
# edits are not part of HEAD_REF. Exit status 0 means both versions look and behave the same.
#
# Needs: TEST_DATABASE_URL-style access to a local PostgreSQL (UI_PG_URL, default
# postgresql+asyncpg://postgres@127.0.0.1:5433), createdb/dropdb, node with playwright, Pillow.
# Nothing touches this checkout's .env or data/. Results: $UI_OUT (default /tmp/pgclock-ui-regression).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$(pwd)"
BASE_REF="${1:-origin/dev}"
HEAD_REF="${2:-HEAD}"
PG_URL="${UI_PG_URL:-postgresql+asyncpg://postgres@127.0.0.1:5433}"
OUT="${UI_OUT:-/tmp/pgclock-ui-regression}"
WEB_PORT_BASE="${UI_WEB_PORT:-9311}"
PANEL_PORT="${UI_PANEL_PORT:-9319}"
WIDTHS="${UI_WIDTHS:-390,1440}"
THEMES="${UI_THEMES:-light,dark}"
USER_NAME=uiadmin
PASSWORD="$(python3 -c 'import sys; sys.path.insert(0, "scripts"); import ui_fixture; print(ui_fixture.ADMIN_PASSWORD)')"

pg_host_args() {
  python3 - "$PG_URL" <<'PY'
import sys
from urllib.parse import urlsplit
u = urlsplit(sys.argv[1].replace("+asyncpg", ""))
print(f"-h {u.hostname or '127.0.0.1'} -p {u.port or 5432} -U {u.username or 'postgres'}")
PY
}
PGARGS="$(pg_host_args)"

PIDS=()
WORKTREES=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  for wt in "${WORKTREES[@]:-}"; do [ -n "$wt" ] && git -C "$REPO" worktree remove --force "$wt" 2>/dev/null || true; done
}
trap cleanup EXIT

python3 scripts/ui_fake_panel.py "$PANEL_PORT" &
PIDS+=($!)
export UI_FAKE_PANEL_URL="http://127.0.0.1:$PANEL_PORT"

# Both versions run from the same path on the same port: the panel shows both (SSL and system settings).
CHECKOUT="$OUT/checkout"

capture() {
  local label="$1" ref="$2" port="$3"
  # One database name for both: the backup settings page shows it.
  local wt="$CHECKOUT" db="pgclock_ui_regression"
  git -C "$REPO" worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
  git worktree add --quiet --detach "$wt" "$ref"
  WORKTREES+=("$wt")
  # shellcheck disable=SC2086
  dropdb $PGARGS --if-exists "$db" >/dev/null 2>&1 || true
  # shellcheck disable=SC2086
  createdb $PGARGS "$db"
  (cd "$wt" && DATABASE_URL="$PG_URL/$db" PYTHONPATH="$wt" python3 "$REPO/scripts/ui_fixture.py" "$wt" "$port" >/dev/null)
  # Outbound HTTP goes to a dead proxy, so checks against GitHub fail the same way on every run;
  # loopback (the app itself and the fake panel) stays direct.
  (cd "$wt" && PYTHONPATH="$wt" HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 \
    NO_PROXY=127.0.0.1,localhost exec python3 -m app.main >"$OUT/$label-server.log" 2>&1) &
  PIDS+=($!)
  for _ in $(seq 1 90); do
    curl -fsS -o /dev/null "http://127.0.0.1:$port/login" 2>/dev/null && break
    sleep 1
  done
  rm -rf "${OUT:?}/$label"
  node "$REPO/scripts/ui_capture.cjs" --base "http://127.0.0.1:$port" --user "$USER_NAME" --pass "$PASSWORD" \
    --out "$OUT/$label" --widths "$WIDTHS" --themes "$THEMES"
  kill "${PIDS[-1]}" 2>/dev/null || true
  wait "${PIDS[-1]}" 2>/dev/null || true
  # shellcheck disable=SC2086
  dropdb $PGARGS --if-exists "$db" >/dev/null 2>&1 || true
  git -C "$REPO" worktree remove --force "$wt" 2>/dev/null || true
}

mkdir -p "$OUT"
capture before "$BASE_REF" "$WEB_PORT_BASE"
capture after "$HEAD_REF" "$WEB_PORT_BASE"
python3 scripts/ui_compare.py "$OUT/before" "$OUT/after"
