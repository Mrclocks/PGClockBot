#!/usr/bin/env bash
# PGClockBot bootstrap — clone if missing, update if present, then open the menu.
#
# Default channel is main (stable 0.5). Development builds use PGCLOCK_CHANNEL=dev.
#
# Fresh or existing server (from any directory):
#   bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/main/get.sh)
#   PGCLOCK_CHANNEL=dev bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/dev/get.sh)
#
# Also works (TTY is reattached automatically):
#   curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/main/get.sh | bash
#
# Or locally (inside / next to the repo):
#   bash get.sh
#   bash get.sh update
#   bash get.sh install-main
#
set -euo pipefail

REPO_URL="${PGCLOCK_REPO:-https://github.com/Mrclocks/PGClockBot.git}"
DIR_NAME="${PGCLOCK_DIR:-PGClockBot}"
# Channel: main (stable) or dev. PGCLOCK_CHANNEL aliases PGCLOCK_BRANCH.
CHANNEL_FROM_ENV=0
if [[ -n "${PGCLOCK_CHANNEL:-${PGCLOCK_BRANCH:-}}" ]]; then
  CHANNEL_FROM_ENV=1
fi
_raw_branch="${PGCLOCK_CHANNEL:-${PGCLOCK_BRANCH:-main}}"
case "$(printf '%s' "$_raw_branch" | tr '[:upper:]' '[:lower:]')" in
  dev|develop|development|unstable) REMOTE_BRANCH="dev" ;;
  *) REMOTE_BRANCH="main" ;;
esac
unset _raw_branch
# Only export when the operator set a channel — otherwise keep the interactive prompt.
if [[ "$CHANNEL_FROM_ENV" -eq 1 ]]; then
  export PGCLOCK_CHANNEL="$REMOTE_BRANCH"
fi
# Optional pin: a release tag or full commit SHA. When set, sync checks out
# this ref instead of tracking the branch tip — prefer for production installs.
# Exported so pgclock.sh install/update keep the pin instead of moving to the tip.
REMOTE_REF="${PGCLOCK_REF:-}"
if [[ -n "$REMOTE_REF" ]]; then
  export PGCLOCK_REF="$REMOTE_REF"
fi

info() { echo "  > $*"; }
ok()   { echo "  + $*"; }
warn() { echo "  ! $*"; }
err()  { echo "  x $*" >&2; }

# Resolve project root
if [[ -f "./pgclock.sh" || -f "./install.sh" || -f "./run.py" ]]; then
  ROOT="$(pwd)"
else
  ROOT="$(pwd)/${DIR_NAME}"
fi

backup_runtime() {
  # Preserve config + DB outside of git reset
  RUNTIME_BAK="$(mktemp -d "${TMPDIR:-/tmp}/pgclock-runtime.XXXXXX")"
  # Never copy .venv — it can be hundreds of MB and slows force-sync badly.
  for item in .env data; do
    if [[ -e "$item" ]]; then
      cp -a "$item" "$RUNTIME_BAK/" 2>/dev/null || true
    fi
  done
  # Keep recent .env backups too
  shopt -s nullglob
  local bak
  for bak in .env.bak.*; do
    cp -a "$bak" "$RUNTIME_BAK/" 2>/dev/null || true
  done
  shopt -u nullglob
  ok "Runtime backup → ${RUNTIME_BAK}"
}

restore_runtime() {
  local bak_dir="${1:-}"
  [[ -n "$bak_dir" && -d "$bak_dir" ]] || return 0
  local copy_failed=0
  if [[ -e "${bak_dir}/.env" && ! -e .env ]]; then
    cp -a "${bak_dir}/.env" .env || copy_failed=1
    ok "Restored .env"
  elif [[ -e "${bak_dir}/.env" && -e .env ]]; then
    # Prefer existing .env in tree; keep backup copy nearby
    cp -a "${bak_dir}/.env" ".env.restored.from.backup" || copy_failed=1
  fi
  if [[ -d "${bak_dir}/data" ]]; then
    mkdir -p data
    cp -a "${bak_dir}/data/." data/ || copy_failed=1
    ok "Restored data/"
  fi
  shopt -s nullglob
  local bak
  for bak in "${bak_dir}"/.env.bak.*; do
    cp -a "$bak" . || copy_failed=1
  done
  shopt -u nullglob
  # The copy holds secrets, so drop it once everything is back in the tree.
  if [[ "$copy_failed" -eq 0 ]]; then
    rm -rf "$bak_dir"
  else
    warn "Some runtime files were not restored — copy kept at ${bak_dir}"
  fi
}

# Fetch the target and print its commit; nothing in the work tree changes yet.
fetch_sync_target() {
  if [[ -n "$REMOTE_REF" ]]; then
    if [[ "$REMOTE_REF" == -* || "$REMOTE_REF" =~ [[:space:]] ]]; then
      err "PGCLOCK_REF is not a valid tag or commit: ${REMOTE_REF}"
      return 1
    fi
    # The ref lands on FETCH_HEAD; a --no-tags clone has no local tag to check out.
    if ! git fetch --no-tags origin "$REMOTE_REF" >&2; then
      err "Could not fetch ${REMOTE_REF}. Use an existing tag or a full commit SHA."
      return 1
    fi
    if ! git rev-parse --verify -q 'FETCH_HEAD^{commit}'; then
      err "${REMOTE_REF} does not point at a commit"
      return 1
    fi
    return 0
  fi
  # Explicit refspec is required: a single-branch clone only tracks main, so
  # `git fetch origin dev` succeeds onto FETCH_HEAD but never creates origin/dev.
  local refspec="+refs/heads/${REMOTE_BRANCH}:refs/remotes/origin/${REMOTE_BRANCH}"
  if ! git config --get-all remote.origin.fetch 2>/dev/null | grep -qx "$refspec"; then
    git config --add remote.origin.fetch "$refspec" 2>/dev/null || true
  fi
  # Targeted fetch (no tags / no --all) — much faster on weak VPS links.
  if ! git fetch --no-tags origin "$refspec" >&2 \
      && ! git fetch --no-tags origin '+refs/heads/*:refs/remotes/origin/*' >&2; then
    err "Could not fetch origin/${REMOTE_BRANCH}. Check network / GitHub access."
    return 1
  fi
  if ! git rev-parse --verify -q "origin/${REMOTE_BRANCH}^{commit}"; then
    err "origin/${REMOTE_BRANCH} missing after fetch"
    return 1
  fi
}

force_sync_to_remote() {
  local target="${REMOTE_REF:-origin/${REMOTE_BRANCH}}"
  info "Syncing hard to ${target} (keeps .env + data)..."
  git remote set-url origin "$REPO_URL" 2>/dev/null || git remote add origin "$REPO_URL" 2>/dev/null || true
  local commit
  # Fetch before touching anything: a network failure leaves the tree as it was.
  commit="$(fetch_sync_target)" || return 1

  backup_runtime
  local bak="$RUNTIME_BAK" switched=0
  if [[ -n "$REMOTE_REF" ]]; then
    git checkout -f -q --detach "$commit" && git reset --hard -q "$commit" && switched=1
  else
    # Start from the remote-tracking ref so the branch keeps its upstream.
    git checkout -f -q -B "$REMOTE_BRANCH" "origin/${REMOTE_BRANCH}" \
      && git reset --hard -q "$commit" && switched=1
  fi
  if [[ "$switched" -eq 1 ]]; then
    git clean -fd --exclude=.env --exclude=data --exclude=.venv --exclude='.env.bak.*' \
      --exclude='.env.restored.*' >/dev/null || true
  fi
  restore_runtime "$bak"
  if [[ "$switched" -ne 1 ]]; then
    err "Could not switch the code to ${target}"
    return 1
  fi
  ok "Code synced to ${target} (${commit:0:12})"
}

git_update() {
  if [[ ! -d .git ]]; then
    warn "Not a git repo — skipping pull"
    return 0
  fi

  info "Updating from GitHub..."
  git remote set-url origin "$REPO_URL" 2>/dev/null || true

  # Explicit channel/branch env → hard-sync to that tip (install/update to dev).
  if [[ "$CHANNEL_FROM_ENV" -eq 1 || -n "$REMOTE_REF" ]]; then
    force_sync_to_remote
    return 0
  fi

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$REMOTE_BRANCH")"
  if [[ "$branch" == "HEAD" ]]; then
    branch="$REMOTE_BRANCH"
  fi

  git fetch --no-tags --prune origin "$branch" 2>/dev/null \
    || git fetch --no-tags --prune origin "$REMOTE_BRANCH" 2>/dev/null \
    || true

  if git pull --ff-only "origin" "$branch" 2>/dev/null \
    || git pull --ff-only 2>/dev/null; then
    ok "Code updated (branch: ${branch})"
    return 0
  fi

  warn "Fast-forward failed (local changes or divergent history)."
  force_sync_to_remote
}

# Changing files under a live panel mixes old and new code (lazy imports,
# templates). pgclock.sh update stops the service before it syncs.
panel_service_running() {
  command -v systemctl >/dev/null 2>&1 \
    && systemctl is-active --quiet pgclockbot 2>/dev/null
}

# Clone next to the folder first: if the clone fails, the old folder (with
# .env and data/) is still there untouched.
reclone_in_place() {
  local staging="${ROOT}.clone.$$"
  rm -rf "$staging"
  if ! git clone -b "$REMOTE_BRANCH" --single-branch "$REPO_URL" "$staging"; then
    rm -rf "$staging"
    err "Clone failed — ${ROOT} was left as it was."
    exit 1
  fi
  backup_runtime
  local bak="$RUNTIME_BAK"
  cd "$(dirname "$ROOT")"
  rm -rf "$ROOT"
  mv "$staging" "$ROOT"
  cd "$ROOT"
  restore_runtime "$bak"
  ok "Re-cloned and restored runtime files (branch: ${REMOTE_BRANCH})"
}

ensure_repo() {
  if [[ -d "$ROOT" ]]; then
    cd "$ROOT"
    ok "Found existing folder: ${ROOT}"

    if [[ -d .git ]]; then
      if panel_service_running; then
        info "Panel service is running — code left as is (Update in the menu applies new code safely)"
      else
        git_update
      fi
    elif [[ ! -f pgclock.sh && ! -f run.py ]]; then
      err "Folder exists but is empty/incomplete and has no .git: ${ROOT}"
      err "Remove or rename it, then re-run:"
      err "  mv ${DIR_NAME} ${DIR_NAME}.bak.\$(date +%Y%m%d)"
      err "  bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/main/get.sh)"
      exit 1
    else
      warn "Folder exists without .git — re-cloning into place"
      reclone_in_place
    fi
    return 0
  fi

  info "Cloning ${REPO_URL} (${REMOTE_BRANCH}) → ${ROOT}"
  git clone -b "$REMOTE_BRANCH" --single-branch "$REPO_URL" "$ROOT"
  cd "$ROOT"
  ok "Clone complete (branch: ${REMOTE_BRANCH})"
  if [[ -n "$REMOTE_REF" ]]; then
    force_sync_to_remote
  fi
}

ensure_repo

# Final safety: if manager still missing, force sync once more
if [[ ! -f pgclock.sh ]]; then
  warn "pgclock.sh missing after update — forcing sync to origin/${REMOTE_BRANCH}"
  if [[ -d .git ]]; then
    force_sync_to_remote
  fi
fi

if [[ ! -f pgclock.sh ]]; then
  err "pgclock.sh still missing in ${ROOT}"
  err "Manual fix:"
  err "  cd ~ && mv ${DIR_NAME} ${DIR_NAME}.bak.\$(date +%Y%m%d)"
  err "  bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/main/get.sh)"
  exit 1
fi

chmod +x pgclock.sh get.sh install.sh update.sh 2>/dev/null || true

# When the operator set PGCLOCK_CHANNEL=dev|main, rewrite bare install/update
# so pgclock.sh does not fall back to main and undo the channel sync above.
_args=("$@")
if [[ "$CHANNEL_FROM_ENV" -eq 1 && ${#_args[@]} -ge 1 ]]; then
  case "${_args[0]}" in
    update|u)   _args[0]="update-${REMOTE_BRANCH}" ;;
    install|i)  _args[0]="install-${REMOTE_BRANCH}" ;;
  esac
fi

# Pass through optional subcommand (install/update/...), default = menu
# Re-attach TTY so the menu can read keyboard even when launched via curl|bash
# (/dev/tty can exist without a controlling terminal, e.g. under cron: probe it.)
if [[ ! -t 0 ]] && { : </dev/tty; } 2>/dev/null; then
  exec bash pgclock.sh "${_args[@]}" </dev/tty
fi
exec bash pgclock.sh "${_args[@]}"
