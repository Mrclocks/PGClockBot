#!/usr/bin/env bash
# Query profile of the web panel and the bot: how many SQL statements each page, request and button
# runs, which statements repeat inside one of them, and which grow with the amount of data.
#
#   scripts/query_profile.sh [REF]        (default: HEAD; results in $QP_OUT, default /tmp/pgclock-query-profile)
#
# Web: REF is checked out into a temporary worktree, prepared with the sample data of the UI
# regression check (scripts/ui_fixture.py) and the local fake panel, then served through
# scripts/query_profile_server.py while scripts/ui_capture.cjs opens every page, tab and modal.
# This runs twice, with QP_SMALL (default 20) and QP_SCALE (default 200) more customers and rows
# (scripts/query_profile_data.py): both runs have data, so only work done per row differs.
# Bot: scripts/query_profile_bot.py tours every menu button and admin section the same two ways. scripts/query_profile_report.py writes report.md from both.
#
# Needs what scripts/ui_regression.sh needs, plus TEST_DATABASE_URL for the bot tour.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$(pwd)"
REF="${1:-HEAD}"
PG_URL="${UI_PG_URL:-postgresql+asyncpg://postgres@127.0.0.1:5433}"
OUT="${QP_OUT:-/tmp/pgclock-query-profile}"
SMALL="${QP_SMALL:-20}"
SCALE="${QP_SCALE:-200}"
WEB_PORT="${UI_WEB_PORT:-9311}"
PANEL_PORT="${UI_PANEL_PORT:-9319}"
# The customer the sample data opens at /users/11/edit gets its own share of the extra rows.
PERSON_USER_ID=11
DB=pgclock_query_profile
PASSWORD="$(python3 -c 'import sys; sys.path.insert(0, "scripts"); import ui_fixture; print(ui_fixture.ADMIN_PASSWORD)')"
PGARGS="$(python3 - "$PG_URL" <<'PY'
import sys
from urllib.parse import urlsplit
u = urlsplit(sys.argv[1].replace("+asyncpg", ""))
print(f"-h {u.hostname or '127.0.0.1'} -p {u.port or 5432} -U {u.username or 'postgres'}")
PY
)"

PIDS=()
WT="$OUT/checkout"
cleanup() {
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || true
  # shellcheck disable=SC2086
  dropdb $PGARGS --if-exists "$DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

rm -rf "$OUT" && mkdir -p "$OUT"
python3 scripts/ui_fake_panel.py "$PANEL_PORT" &
PIDS+=($!)
export UI_FAKE_PANEL_URL="http://127.0.0.1:$PANEL_PORT"
git worktree add --quiet --detach "$WT" "$REF"

profile_web() {
  local scale="$1"
  # shellcheck disable=SC2086
  dropdb $PGARGS --if-exists "$DB" >/dev/null 2>&1 || true
  # shellcheck disable=SC2086
  createdb $PGARGS "$DB"
  (cd "$WT" && DATABASE_URL="$PG_URL/$DB" PYTHONPATH="$WT" python3 "$REPO/scripts/ui_fixture.py" "$WT" "$WEB_PORT" >/dev/null)
  if [ "$scale" -gt 0 ]; then
    (cd "$WT" && DATABASE_URL="$PG_URL/$DB" PYTHONPATH="$WT" python3 "$REPO/scripts/query_profile_data.py" "$scale" "$PERSON_USER_ID")
  fi
  (cd "$WT" && PYTHONPATH="$WT" QUERY_PROFILE_OUT="$OUT/web.jsonl" QUERY_PROFILE_SCALE="$scale" \
    HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 NO_PROXY=127.0.0.1,localhost \
    exec python3 "$REPO/scripts/query_profile_server.py" >"$OUT/server-$scale.log" 2>&1) &
  PIDS+=($!)
  for _ in $(seq 1 90); do
    curl -fsS -o /dev/null "http://127.0.0.1:$WEB_PORT/login" 2>/dev/null && break
    sleep 1
  done
  node "$REPO/scripts/ui_capture.cjs" --base "http://127.0.0.1:$WEB_PORT" --user uiadmin --pass "$PASSWORD" \
    --out "$OUT/capture-$scale" --widths 1440 --themes light
  kill "${PIDS[-1]}" 2>/dev/null || true
  wait "${PIDS[-1]}" 2>/dev/null || true
}

profile_web "$SMALL"
profile_web "$SCALE"
python3 scripts/query_profile_bot.py "$OUT/bot.jsonl" --scale "$SMALL" --scale "$SCALE"
{
  echo "# Query profile of $(git rev-parse --short "$REF")"
  echo
  python3 scripts/query_profile_report.py --title "Web panel requests" "$OUT/web.jsonl"
  python3 scripts/query_profile_report.py --title "Bot taps" "$OUT/bot.jsonl"
} >"$OUT/report.md"
echo "query-profile: $OUT/report.md"
