#!/usr/bin/env bash
# PGClockBot — single entry CLI (Ubuntu 22.04+)
# Usage:
#   bash pgclock.sh
#   bash pgclock.sh install|update|env|web|service|uninstall|status|help
set -euo pipefail
set +H

# curl|bash leaves stdin as a dead pipe. For interactive menus, attach to TTY.
# `install` / `update` / `status` / `help` can run without a TTY.
_CMD="${1:-}"
if [[ ! -t 0 ]]; then
  case "${_CMD}" in
    install|i|update|u|status|s|help|h|-h|--help) ;;
    *)
      if [[ -r /dev/tty ]]; then
        exec </dev/tty
      else
        echo "  x No interactive terminal (TTY). Run: bash pgclock.sh install" >&2
        exit 1
      fi
      ;;
  esac
fi
unset _CMD

R=$'\033[0;31m'; G=$'\033[0;32m'; C=$'\033[0;36m'
Y=$'\033[1;33m'; B=$'\033[1;37m'; D=$'\033[2m'; N=$'\033[0m'
BOLD=$'\033[1m'

# Prefer /dev/tty so menus stay clean when stdout is captured; fall back so
# SUCCESS banners still appear under curl|bash / no-TTY edge cases.
_tty() {
  if [[ -w /dev/tty ]]; then
    printf '%b' "$*" > /dev/tty 2>/dev/null || printf '%b' "$*"
  else
    printf '%b' "$*"
  fi
}
info()  { _tty "  ${C}>${N} $*\n"; }
ok()    { _tty "  ${G}+${N} $*\n"; }
warn()  { _tty "  ${Y}!${N} $*\n"; }
err()   { _tty "  ${R}x${N} $*\n"; }
step()  { _tty "\n${BOLD}${C}-- $* --${N}\n\n"; }
pause() {
  printf '\n  Press Enter to continue... ' > /dev/tty
  read -r _ < /dev/tty || true
}

prompt_read() {
  # Always talk to the real terminal — prompts must NOT go to stdout
  # (values are captured with VAR="$(ask ...)")
  # -e enables readline so Backspace / arrows work when editing mistakes
  local __var="$1"
  shift
  printf '%b' "$*" > /dev/tty
  if ! read -e -r "$__var" < /dev/tty; then
    printf -v "$__var" '%s' ""
  fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
SERVICE_NAME="pgclockbot"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
PY="${SCRIPT_DIR}/.venv/bin/python"
PIP="${SCRIPT_DIR}/.venv/bin/pip"

# ── helpers ─────────────────────────────────────────────
ask() {
  local prompt="$1"
  local has_default=0
  local default=""
  if [[ $# -ge 2 ]]; then
    has_default=1
    default="$2"
  fi
  local var
  if [[ "$has_default" -eq 0 ]]; then
    while true; do
      prompt_read var "  ${B}${prompt}${N}: "
      if [[ -n "${var}" ]]; then
        printf '%s\n' "$var"
        return
      fi
      err "This field is required."
    done
  else
    if [[ -n "$default" ]]; then
      prompt_read var "  ${B}${prompt}${N} ${D}[${default}]${N}: "
    else
      prompt_read var "  ${B}${prompt}${N} ${D}[Enter to skip]${N}: "
    fi
    if [[ -z "${var}" ]]; then
      printf '%s\n' "$default"
    else
      printf '%s\n' "$var"
    fi
  fi
}

ask_optional() {
  local prompt="$1"
  local var=""
  prompt_read var "  ${B}${prompt}${N} ${D}[Enter to skip]${N}: "
  printf '%s\n' "${var}"
}

ask_secret() {
  local prompt="$1"
  local var
  while true; do
    printf '  %b: ' "${B}${prompt}${N}" > /dev/tty
    read -r -s var < /dev/tty || true
    printf '\n' > /dev/tty
    # strip accidental CR / leading-trailing whitespace from paste
    var="${var//$'\r'/}"
    var="${var#"${var%%[![:space:]]*}"}"
    var="${var%"${var##*[![:space:]]}"}"
    if [[ -n "$var" ]]; then
      printf '%s\n' "$var"
      return
    fi
    err "This field is required."
  done
}

ask_password() {
  local prompt="$1"
  local p1 p2
  while true; do
    p1="$(ask_secret "$prompt")"
    p2="$(ask_secret "Confirm password")"
    if [[ "$p1" != "$p2" ]]; then
      err "Passwords do not match."
      continue
    fi
    printf '%s\n' "$p1"
    return
  done
}

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    head -c 48 /dev/urandom | xxd -p | tr -d '\n' | head -c 48
  fi
}

sudo_wrap() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

ask_yn() {
  # ask_yn "Prompt" Y|N
  local prompt="$1"
  local default="${2:-N}"
  local hint var
  if [[ "${default^^}" == "Y" ]]; then
    hint="Y/n"
  else
    hint="y/N"
  fi
  prompt_read var "  ${B}${prompt}${N} ${D}[${hint}]${N}: "
  if [[ -z "${var}" ]]; then
    var="$default"
  fi
  case "${var,,}" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

# Cached so Setup URL + SUCCESS banner never disagree on the public IP.
_DETECTED_SERVER_IP=""
detect_server_ip() {
  if [[ -n "${_DETECTED_SERVER_IP:-}" ]]; then
    printf '%s\n' "$_DETECTED_SERVER_IP"
    return 0
  fi
  local ip=""
  # Prefer kernel route source (stable on VPS); then public echo services.
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
  fi
  if [[ -z "$ip" || "$ip" == 127.* ]]; then
    ip="$(curl -4 -fsS --max-time 4 https://api.ipify.org 2>/dev/null || true)"
  fi
  if [[ -z "$ip" || "$ip" == 127.* ]]; then
    ip="$(curl -4 -fsS --max-time 4 https://ifconfig.me 2>/dev/null || true)"
  fi
  if [[ -z "$ip" || "$ip" == 127.* ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -z "$ip" ]]; then
    ip="YOUR_SERVER_IP"
  fi
  _DETECTED_SERVER_IP="$ip"
  printf '%s\n' "$ip"
}

# Existing .env from failed installs often has WEB_HOST=127.0.0.1 — panel then
# answers only on loopback and every public Setup URL looks "dead".
ensure_public_web_host() {
  [[ -f .env ]] || return 0
  local host
  host="$(env_get WEB_HOST "0.0.0.0")"
  case "$host" in
    0.0.0.0|"::"|'*') return 0 ;;
  esac
  python3 - <<'PY'
from pathlib import Path
import re
path = Path(".env")
text = path.read_text(encoding="utf-8")
line = 'WEB_HOST="0.0.0.0"'
if re.search(r"^WEB_HOST=", text, flags=re.M):
    text = re.sub(r"^WEB_HOST=.*$", line, text, count=1, flags=re.M)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
path.write_text(text, encoding="utf-8")
print("updated")
PY
  ok "WEB_HOST set to 0.0.0.0 (was '${host}' — unreachable from the internet)"
}

# Install-time panel port + SSL mode (domain LE / temp IP / none).
# Env overrides (non-interactive): PGCLOCK_WEB_PORT, PGCLOCK_SSL_MODE,
# PGCLOCK_SSL_DOMAIN, PGCLOCK_SSL_EMAIL.
# Sets: WEB_PORT, INSTALL_SSL_MODE, INSTALL_SSL_DOMAIN, INSTALL_SSL_EMAIL
prompt_install_port_and_ssl() {
  INSTALL_SSL_MODE="${PGCLOCK_SSL_MODE:-none}"
  INSTALL_SSL_DOMAIN="${PGCLOCK_SSL_DOMAIN:-}"
  INSTALL_SSL_EMAIL="${PGCLOCK_SSL_EMAIL:-}"

  local port_in mode_in
  if [[ -n "${PGCLOCK_WEB_PORT:-}" ]]; then
    WEB_PORT="${PGCLOCK_WEB_PORT}"
  elif [[ -r /dev/tty ]]; then
    while true; do
      port_in="$(ask "Panel TCP port" "${WEB_PORT:-9000}")"
      if [[ "$port_in" =~ ^[0-9]+$ ]] && (( port_in >= 1 && port_in <= 65535 )); then
        WEB_PORT="$port_in"
        break
      fi
      err "Enter a port number between 1 and 65535."
    done
  else
    WEB_PORT="${WEB_PORT:-9000}"
  fi

  if [[ -n "${PGCLOCK_SSL_MODE:-}" ]]; then
    case "${PGCLOCK_SSL_MODE,,}" in
      1|domain|letsencrypt|le) INSTALL_SSL_MODE="domain" ;;
      2|ip|self_signed|self-signed|temp_ip|self_signed_ip) INSTALL_SSL_MODE="ip" ;;
      3|none|off|http|"") INSTALL_SSL_MODE="none" ;;
      *)
        err "PGCLOCK_SSL_MODE must be domain|ip|none (got: ${PGCLOCK_SSL_MODE})"
        return 1
        ;;
    esac
  elif [[ -r /dev/tty ]]; then
    info "SSL certificate for the panel:"
    printf '    %s1%s) Domain — Let'\''s Encrypt (DNS → this server, port 80 free)\n' "$B" "$N" > /dev/tty
    printf '    %s2%s) Temporary self-signed for this server IP (browser warning OK)\n' "$B" "$N" > /dev/tty
    printf '    %s3%s) No certificate — HTTP only\n' "$B" "$N" > /dev/tty
    while true; do
      mode_in="$(ask "Select SSL mode" "3")"
      case "${mode_in,,}" in
        1|domain|letsencrypt|le) INSTALL_SSL_MODE="domain"; break ;;
        2|ip|self_signed|self-signed|temp_ip) INSTALL_SSL_MODE="ip"; break ;;
        3|none|off|http|"") INSTALL_SSL_MODE="none"; break ;;
        *) err "Choose 1, 2, or 3." ;;
      esac
    done
  else
    INSTALL_SSL_MODE="none"
  fi

  if [[ "$INSTALL_SSL_MODE" == "domain" ]]; then
    if [[ -z "$INSTALL_SSL_DOMAIN" ]]; then
      if [[ ! -r /dev/tty ]]; then
        err "PGCLOCK_SSL_DOMAIN is required for domain SSL in non-interactive mode."
        return 1
      fi
      while true; do
        INSTALL_SSL_DOMAIN="$(ask "Panel domain (e.g. panel.example.com)")"
        INSTALL_SSL_DOMAIN="${INSTALL_SSL_DOMAIN,,}"
        INSTALL_SSL_DOMAIN="${INSTALL_SSL_DOMAIN#http://}"
        INSTALL_SSL_DOMAIN="${INSTALL_SSL_DOMAIN#https://}"
        INSTALL_SSL_DOMAIN="${INSTALL_SSL_DOMAIN%%/*}"
        INSTALL_SSL_DOMAIN="${INSTALL_SSL_DOMAIN%%:*}"
        if [[ "$INSTALL_SSL_DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(\.[a-z0-9-]+)+$ ]]; then
          break
        fi
        err "Invalid domain."
      done
    fi
    if [[ -z "$INSTALL_SSL_EMAIL" ]]; then
      if [[ ! -r /dev/tty ]]; then
        err "PGCLOCK_SSL_EMAIL is required for domain SSL in non-interactive mode."
        return 1
      fi
      while true; do
        INSTALL_SSL_EMAIL="$(ask "Let's Encrypt email")"
        if [[ "$INSTALL_SSL_EMAIL" == *@*.* ]]; then
          break
        fi
        err "Enter a valid email."
      done
    fi
  fi

  ok "Port ${WEB_PORT} · SSL mode=${INSTALL_SSL_MODE}"
  return 0
}

ssl_is_enabled_on_disk() {
  [[ -f data/certs/meta.json && -f data/certs/fullchain.pem && -f data/certs/privkey.pem ]] || return 1
  python3 - <<'PY' 2>/dev/null
import json
from pathlib import Path
p = Path("data/certs/meta.json")
try:
    m = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if m.get("ssl_enabled") else 1)
PY
}

panel_public_base_url() {
  # Prefer live HTTPS meta; else http://IP:PORT
  local port ip https
  port="$(env_get WEB_PORT "${WEB_PORT:-9000}")"
  ip="$(detect_server_ip)"
  if ssl_is_enabled_on_disk; then
    https="$(
      python3 - <<'PY' 2>/dev/null || true
import json
from pathlib import Path
m = json.loads(Path("data/certs/meta.json").read_text(encoding="utf-8"))
print((m.get("public_https") or "").strip().rstrip("/"))
PY
    )"
    if [[ -n "$https" ]]; then
      printf '%s\n' "$https"
      return 0
    fi
    local host
    host="$(
      python3 - <<'PY' 2>/dev/null || true
import json
from pathlib import Path
m = json.loads(Path("data/certs/meta.json").read_text(encoding="utf-8"))
print((m.get("domain") or m.get("host") or "").strip())
PY
    )"
    if [[ -n "$host" ]]; then
      if [[ "$port" == "443" ]]; then
        printf 'https://%s\n' "$host"
      else
        printf 'https://%s:%s\n' "$host" "$port"
      fi
      return 0
    fi
  fi
  printf 'http://%s:%s\n' "$ip" "$port"
}

apply_install_ssl() {
  # Issue cert + wire PUBLIC_BASE_URL before the panel starts (restart=False).
  local mode="${INSTALL_SSL_MODE:-none}"
  local domain="${INSTALL_SSL_DOMAIN:-}"
  local email="${INSTALL_SSL_EMAIL:-}"
  local ip
  ip="$(detect_server_ip)"

  if [[ "$mode" == "none" ]]; then
    info "SSL: skipped (HTTP only)"
    return 0
  fi

  local py="$PY"
  if [[ ! -x "$py" ]]; then
    py="${SCRIPT_DIR}/.venv/bin/python"
  fi
  if [[ ! -x "$py" ]]; then
    err "Python venv missing — cannot configure SSL"
    return 1
  fi

  if [[ "$mode" == "domain" ]]; then
    step "SSL · Let's Encrypt (${domain})"
    # ACME HTTP-01 needs inbound :80 (and free locally).
    if command -v ufw >/dev/null 2>&1; then
      sudo_wrap ufw allow 80/tcp >/dev/null 2>&1 || true
    fi
    if ! command -v certbot >/dev/null 2>&1; then
      info "Installing certbot…"
      sudo_wrap apt-get install -y certbot >/dev/null 2>&1 \
        || sudo_wrap apt-get install -y certbot || true
    fi
  else
    step "SSL · temporary self-signed for ${ip}"
  fi

  info "SSL progress will print below (success or error)…"
  local result_json
  # Progress lines go to stderr → /dev/tty; final JSON on stdout.
  result_json="$(
    cd "$SCRIPT_DIR" && \
    PGCLOCK_SSL_INSTALL=1 \
    PGCLOCK_INSTALL_SSL_MODE="$mode" \
    PGCLOCK_INSTALL_SSL_DOMAIN="$domain" \
    PGCLOCK_INSTALL_SSL_EMAIL="$email" \
    PGCLOCK_INSTALL_SSL_IP="$ip" \
    PGCLOCK_INSTALL_WEB_PORT="${WEB_PORT}" \
    PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$py" - <<'PY' 2>/dev/tty
import json, os, sys
from app.services.ssl_certs import configure_for_install, verify_tls_material

mode = (os.environ.get("PGCLOCK_INSTALL_SSL_MODE") or "none").strip()
result = configure_for_install(
    mode=mode,
    domain=os.environ.get("PGCLOCK_INSTALL_SSL_DOMAIN") or "",
    email=os.environ.get("PGCLOCK_INSTALL_SSL_EMAIL") or "",
    ip=os.environ.get("PGCLOCK_INSTALL_SSL_IP") or "",
    web_port=os.environ.get("PGCLOCK_INSTALL_WEB_PORT") or "9000",
)
if result.get("ok") and mode not in ("", "none", "off", "http", "3"):
    check = verify_tls_material()
    if not check.get("ok"):
        result = {"ok": False, "error": check.get("error") or "TLS material check failed"}
        print(f"  [ssl] TLS verify FAILED · {result['error'][:300]}", file=sys.stderr, flush=True)
    else:
        result["tls_ok"] = True
        print("  [ssl] TLS material verified (cert+key load OK)", file=sys.stderr, flush=True)
print(json.dumps(result, ensure_ascii=False))
sys.exit(0 if result.get("ok") else 1)
PY
  )" || true

  if [[ -z "$result_json" ]]; then
    err "SSL configuration produced no output"
    return 1
  fi
  if ! printf '%s' "$result_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") else 1)' 2>/dev/null; then
    err "SSL certificate FAILED:"
    printf '%s\n' "$result_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("   ", (d.get("error") or d))' 2>/dev/null \
      || printf '%s\n' "$result_json"
    warn "Fix DNS/port 80, or re-run install with SSL mode 3 (HTTP only) / mode 2 (temp IP)."
    return 1
  fi

  local https_url expires_at
  https_url="$(printf '%s' "$result_json" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("public_https") or "").strip())' 2>/dev/null || true)"
  expires_at="$(printf '%s' "$result_json" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("expires_at") or "").strip())' 2>/dev/null || true)"
  if [[ -n "$https_url" ]]; then
    PUBLIC_BASE_URL="$https_url"
    ok "SSL certificate READY · ${https_url}${expires_at:+ · expires ${expires_at}}"
  else
    ok "SSL mode ${mode} applied"
  fi
  # Absolute paths + readable by service user
  chmod 644 data/certs/fullchain.pem 2>/dev/null || true
  chmod 600 data/certs/privkey.pem 2>/dev/null || true
  chmod 600 data/certs/meta.json 2>/dev/null || true
  return 0
}

rewrite_socket_database_url() {
  # asyncpg + ?host=/var/run/postgresql → Errno 2 on common Ubuntu/Hetzner layouts.
  # Rewrite to TCP 127.0.0.1 so the panel can actually connect.
  [[ -f .env ]] || return 0
  python3 - <<'PY'
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

path = Path(".env")
text = path.read_text(encoding="utf-8")
m = re.search(r"^DATABASE_URL=(.*)$", text, flags=re.M)
if not m:
    raise SystemExit(0)
raw = m.group(1).strip().strip('"').strip("'")
u = urlparse(raw)
qs = dict(parse_qsl(u.query, keep_blank_values=True))
host_q = (qs.get("host") or "").strip()
is_socket = host_q.startswith("/") or (not u.hostname and host_q.startswith("/"))
if not is_socket:
    raise SystemExit(0)
password = unquote(u.password) if u.password is not None else ""
user = unquote(u.username or "pgclock")
db = (u.path or "/pgclock").lstrip("/") or "pgclock"
qs.pop("host", None)
query = urlencode(qs)
auth = quote(user, safe="")
if password != "":
    auth += ":" + quote(password, safe="")
scheme = u.scheme or "postgresql+asyncpg"
netloc = f"{auth}@127.0.0.1:5432"
new = urlunparse((scheme, netloc, "/" + db, "", query, ""))
escaped = new.replace("\\", "\\\\").replace('"', '\\"')
line = f'DATABASE_URL="{escaped}"'
text2 = re.sub(r"^DATABASE_URL=.*$", line, text, count=1, flags=re.M)
path.write_text(text2, encoding="utf-8")
print(new.split("@", 1)[-1])
PY
}

ensure_db_schema() {
  # Run Alembic/init_db BEFORE systemd starts — uvicorn only binds after init_db.
  # Doing it here surfaces DB errors during install instead of a silent dead panel.
  step "Database schema"
  local py="$PY"
  if [[ ! -x "$py" ]]; then
    py="${SCRIPT_DIR}/.venv/bin/python"
  fi
  if [[ ! -x "$py" ]]; then
    err "Python venv missing — cannot migrate schema"
    return 1
  fi

  local rewritten
  rewritten="$(rewrite_socket_database_url || true)"
  if [[ -n "$rewritten" ]]; then
    warn "Rewrote socket DATABASE_URL → TCP ${rewritten} (asyncpg cannot use /var/run/postgresql)"
    export PGCLOCK_DATABASE_URL="$(env_get DATABASE_URL "")"
  fi

  info "Connecting to PostgreSQL and applying migrations (timeout 120s)…"
  if ! (
    cd "$SCRIPT_DIR" && \
    PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$py" - <<'PY'
import asyncio, sys

# Clear settings BEFORE importing app.db.session (engine is built at import time).
from app.config import get_settings

get_settings.cache_clear()
s = get_settings()
url = (s.database_url or "").strip()
safe = url.split("@", 1)[-1] if "@" in url else "(no url)"
print(f"  [db] dialect target · {safe}", flush=True)
if not url.startswith(("postgresql://", "postgresql+asyncpg://", "postgres://")):
    print("  [db] FAILED · DATABASE_URL is not PostgreSQL", file=sys.stderr, flush=True)
    raise SystemExit(1)
if "host=/var/run/postgresql" in url or ("@/" in url and "?host=/" in url):
    print(
        "  [db] FAILED · Unix-socket DATABASE_URL is not supported by the panel "
        "(use 127.0.0.1:5432)",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(1)

from app.db.session import init_db

async def main() -> None:
    await asyncio.wait_for(init_db(), timeout=120)
    print("  [db] schema OK", flush=True)

try:
    asyncio.run(main())
except asyncio.TimeoutError:
    print("  [db] FAILED · timed out after 120s — PostgreSQL unreachable or auth stuck", file=sys.stderr, flush=True)
    raise SystemExit(1)
except Exception as exc:
    print(f"  [db] FAILED · {exc}", file=sys.stderr, flush=True)
    raise SystemExit(1)
PY
  ); then
    err "Database schema migration failed — panel would never open without this."
    warn "Check: systemctl status postgresql — and DATABASE_URL in .env"
    warn "journal tip: PGPASSWORD=… psql -h 127.0.0.1 -U pgclock -d pgclock -c 'SELECT 1'"
    return 1
  fi
  ok "Database schema ready"
  return 0
}

wait_port_listen() {
  # wait_port_listen <port> <seconds>
  local port="$1"
  local seconds="${2:-60}"
  local i
  for i in $(seq 1 "$seconds"); do
    if command -v ss >/dev/null 2>&1; then
      if ss -ltn 2>/dev/null | grep -qE ":${port}\\b"; then
        return 0
      fi
    elif python3 - <<PY 2>/dev/null
import socket
s=socket.socket(); s.settimeout(0.3)
try:
    # connect_ex == 0 means something accepts on the port
    raise SystemExit(0 if s.connect_ex(("127.0.0.1", int("${port}"))) == 0 else 1)
finally:
    s.close()
PY
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

probe_panel_health() {
  # Sets: health_ok (0/1), health_scheme (http|https), health_url
  # Tries HTTPS first when SSL meta is on, then HTTP fallback (detects TLS mismatch).
  local port="${WEB_PORT:-9000}"
  health_ok=0
  health_scheme="http"
  health_url="http://127.0.0.1:${port}/health"
  local want_ssl=0
  if ssl_is_enabled_on_disk; then
    want_ssl=1
  fi
  local body
  if [[ "$want_ssl" -eq 1 ]]; then
    body="$(curl -skS --max-time 2 "https://127.0.0.1:${port}/health" 2>/dev/null || true)"
    if [[ "$body" == *'"ok"'* ]] || [[ "$body" == *'ok'* && "$body" == *'true'* ]]; then
      health_ok=1
      health_scheme="https"
      health_url="https://127.0.0.1:${port}/health"
      return 0
    fi
  fi
  body="$(curl -sS --max-time 2 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
  if [[ "$body" == *'"ok"'* ]] || [[ "$body" == *'ok'* && "$body" == *'true'* ]]; then
    health_ok=1
    health_scheme="http"
    health_url="http://127.0.0.1:${port}/health"
    if [[ "$want_ssl" -eq 1 ]]; then
      warn "Panel answers HTTP but SSL meta is on — TLS not active in the running process"
    fi
    return 0
  fi
  if [[ "$want_ssl" -eq 1 ]]; then
    health_scheme="https"
    health_url="https://127.0.0.1:${port}/health"
  fi
  return 1
}

read_app_version() {
  # Prefer VERSION file (works before venv); fallback to app.version after deps exist.
  local v=""
  if [[ -f "${SCRIPT_DIR}/VERSION" ]]; then
    v="$(tr -d '\r\n[:space:]' < "${SCRIPT_DIR}/VERSION")"
  fi
  if [[ -z "$v" && -n "${PY:-}" && -x "${PY}" ]]; then
    v="$("${PY}" -c 'from app.version import __version__; print(__version__)' 2>/dev/null || true)"
  fi
  if [[ -z "$v" ]]; then
    v="unknown"
  fi
  # Belt-and-suspenders: only print semver-like tokens to the terminal.
  if [[ ! "$v" =~ ^[0-9]+(\.[0-9]+)*([a-zA-Z0-9.-]*)?$ ]]; then
    v="unknown"
  fi
  printf '%s' "$v"
}

web_username() {
  if [[ -f data/web_admin.json ]] && [[ -f "$PY" || -x "$PY" ]]; then
    "$PY" - <<'PY' 2>/dev/null || echo admin
from app.services.web_auth import load_web_admin
print(load_web_admin().get("username") or "admin")
PY
  else
    env_get WEB_ADMIN_USER admin
  fi
}

setup_wizard_url() {
  # Emit setup URL when setup_complete.flag is absent. Do NOT call
  # is_setup_complete() here — that helper can auto-create the flag from
  # leftover .env credentials and then return an empty URL.
  #
  # Optional $1 = base URL override (use the same IP/scheme as the SUCCESS banner).
  # Reuses ensure_setup_gate_token() so status/menu calls do not invalidate the
  # previously printed one-time link before TTL expiry.
  if [[ -f data/setup_complete.flag ]]; then
    return 0
  fi
  local port ip py base
  port="$(env_get WEB_PORT "${WEB_PORT:-9000}")"
  ip="$(detect_server_ip)"
  base="${1:-}"
  if [[ -z "$base" ]]; then
    base="$(panel_public_base_url)"
  fi
  if [[ -z "$base" ]]; then
    base="http://${ip}:${port}"
  fi
  base="${base%/}"
  py="$PY"
  if [[ ! -x "$py" ]]; then
    py="${SCRIPT_DIR}/.venv/bin/python"
  fi
  if [[ ! -x "$py" ]]; then
    py="${SYSTEM_PY:-python3}"
  fi
  local errf url
  errf="$(mktemp)"
  url="$(
    cd "$SCRIPT_DIR" && PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$py" - <<PY 2>"$errf"
from app.services.setup_wizard import (
    SETUP_ENTRY_FILE,
    SETUP_FLAG,
    build_setup_entry_url,
    ensure_setup_gate_token,
    _ensure_data_dir,
)

if SETUP_FLAG.exists():
    raise SystemExit(0)

base = """${base}""".rstrip("/")
token = ensure_setup_gate_token()
if not token:
    raise SystemExit(0)
url = build_setup_entry_url(base, token=token)
_ensure_data_dir()
SETUP_ENTRY_FILE.write_text(url + chr(10), encoding="utf-8")
try:
    SETUP_ENTRY_FILE.chmod(0o600)
except OSError:
    pass
print(url)
PY
  )" || true
  url="$(printf '%s' "$url" | tr -d '\r\n')"
  if [[ -n "$url" && "$url" == *"?gate="* ]]; then
    rm -f "$errf"
    printf '%s\n' "$url"
    return 0
  fi
  if [[ -s "$errf" ]]; then
    warn "Could not build setup URL (python): $(head -n 2 "$errf" | tr '\n' ' ')"
  fi
  rm -f "$errf"
  return 0
}

print_success() {
  # print_success "Title" [extra lines...]
  # Always prints panel + health URLs. Extra lines are English operator hints.
  local title="$1"
  shift || true
  # Back-compat: ignore legacy --setup-only flag if callers still pass it.
  if [[ "${1:-}" == "--setup-only" ]]; then
    shift || true
  fi
  local port ip user panel_base health_local
  port="$(env_get WEB_PORT "${WEB_PORT:-9000}")"
  ip="$(detect_server_ip)"
  user="$(web_username)"
  panel_base="$(panel_public_base_url)"
  if ssl_is_enabled_on_disk; then
    health_local="https://127.0.0.1:${port}/health"
  else
    health_local="http://127.0.0.1:${port}/health"
  fi
  local body
  body="$(
    echo ""
    printf '%s==========================================%s\n' "$G" "$N"
    printf '%s  SUCCESS · %s%s\n' "$G" "$title" "$N"
    printf '%s==========================================%s\n' "$G" "$N"
    printf '  Web panel:  %s%s/%s\n' "$B" "$panel_base" "$N"
    printf '  Health:     %s%s%s\n' "$B" "$health_local" "$N"
    if [[ -f data/setup_complete.flag ]]; then
      printf '  Username:   %s%s%s\n' "$B" "$user" "$N"
    else
      printf '  Next step:  %sopen the Setup URL below (first-run wizard)%s\n' "$B" "$N"
    fi
    if [[ $# -gt 0 ]]; then
      echo ""
      local line
      for line in "$@"; do
        printf '  %b\n' "$line"
      done
    fi
    printf '%s==========================================%s\n' "$G" "$N"
    echo ""
  )"
  _tty "$body"
  # Also mirror to stdout so captured install logs retain the URLs.
  printf '%s' "$body"
}

require_ubuntu_22_plus() {
  if [[ ! -f /etc/os-release ]]; then
    err "Unsupported system: /etc/os-release not found."
    return 1
  fi
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != "ubuntu" ]]; then
    err "This tool supports Ubuntu only. Detected: ${PRETTY_NAME:-unknown}"
    return 1
  fi
  local major
  major="$(echo "${VERSION_ID:-0}" | cut -d. -f1)"
  if [[ -z "$major" || "$major" -lt 22 ]]; then
    err "Ubuntu 22.04 or newer is required. Detected: ${PRETTY_NAME:-unknown}"
    return 1
  fi
  ok "OS: ${PRETTY_NAME}"
}

ensure_apt_packages() {
  info "Checking system packages..."
  if ! command -v apt-get >/dev/null 2>&1; then
    err "apt-get not found."
    return 1
  fi
  # Keep a full PATH for apt/debconf postinst scripts (Ubuntu 24+/26 can
  # invoke postgresql.config with a stripped PATH → "pg_lsclusters: not found").
  export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

  # Core packages required for install/run. nano/certbot are optional (edit/SSL).
  # postgresql-common MUST be present before the server package configures —
  # otherwise debconf prints: pg_lsclusters: not found
  local required=(
    python3 python3-venv python3-pip ca-certificates curl git openssl
    postgresql-common postgresql postgresql-contrib postgresql-client
  )
  local missing=()
  local pkg
  for pkg in "${required[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done
  if [[ ${#missing[@]} -eq 0 ]] \
    && command -v python3 >/dev/null 2>&1 \
    && command -v curl >/dev/null 2>&1 \
    && command -v git >/dev/null 2>&1 \
    && command -v psql >/dev/null 2>&1 \
    && command -v pg_dump >/dev/null 2>&1 \
    && command -v pg_restore >/dev/null 2>&1 \
    && command -v pg_lsclusters >/dev/null 2>&1; then
    ok "Prerequisites already installed (skip apt)"
    return 0
  fi
  export DEBIAN_FRONTEND=noninteractive
  sudo_wrap apt-get update -y >/dev/null

  # Phase 1: postgresql-common first (provides /usr/bin/pg_lsclusters).
  if ! dpkg -s postgresql-common >/dev/null 2>&1 \
    || ! command -v pg_lsclusters >/dev/null 2>&1; then
    info "Installing postgresql-common (pg_lsclusters)…"
    if ! sudo_wrap apt-get install -y postgresql-common; then
      err "Failed to install postgresql-common"
      return 1
    fi
  fi

  # Phase 2: remaining packages (including postgresql server/client).
  missing=()
  for pkg in "${required[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    info "Installing: ${missing[*]}"
    if ! sudo_wrap env PATH="$PATH" DEBIAN_FRONTEND=noninteractive \
      apt-get install -y "${missing[@]}"; then
      err "apt-get install failed"
      return 1
    fi
  fi

  if ! command -v psql >/dev/null 2>&1; then
    err "psql still missing after apt — install postgresql-client manually"
    return 1
  fi
  if ! command -v pg_dump >/dev/null 2>&1 || ! command -v pg_restore >/dev/null 2>&1; then
    warn "pg_dump/pg_restore missing — installing postgresql-client"
    sudo_wrap env PATH="$PATH" DEBIAN_FRONTEND=noninteractive \
      apt-get install -y postgresql-client || true
  fi
  if ! command -v pg_dump >/dev/null 2>&1 || ! command -v pg_restore >/dev/null 2>&1; then
    err "pg_dump/pg_restore still missing — install postgresql-client manually (backups will fail)"
    return 1
  fi
  if ! command -v pg_lsclusters >/dev/null 2>&1; then
    warn "pg_lsclusters still missing — installing postgresql-common again"
    sudo_wrap apt-get install -y --reinstall postgresql-common || true
  fi
  if command -v pg_lsclusters >/dev/null 2>&1; then
    ok "Prerequisites ready (pg_lsclusters=$(command -v pg_lsclusters))"
  else
    warn "Prerequisites installed but pg_lsclusters not in PATH — setup_postgres will use /etc/postgresql fallback"
    ok "Prerequisites ready"
  fi
}

# Lightweight: ensure backup tools exist (safe to call from Update on existing installs).
ensure_pg_client_tools() {
  export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
  if command -v pg_dump >/dev/null 2>&1 && command -v pg_restore >/dev/null 2>&1; then
    ok "pg_dump/pg_restore available"
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "pg_dump missing and apt-get unavailable — backups will fail"
    return 1
  fi
  info "Installing postgresql-client (required for backups)…"
  export DEBIAN_FRONTEND=noninteractive
  sudo_wrap apt-get update -y >/dev/null || true
  if sudo_wrap env PATH="$PATH" DEBIAN_FRONTEND=noninteractive \
    apt-get install -y postgresql-client; then
    if command -v pg_dump >/dev/null 2>&1 && command -v pg_restore >/dev/null 2>&1; then
      ok "postgresql-client installed (pg_dump=$(command -v pg_dump))"
      return 0
    fi
  fi
  warn "Could not install postgresql-client — run: sudo apt install postgresql-client"
  return 1
}

# Start local PostgreSQL and ensure a DATABASE_URL for fresh installs.
# Honors PGCLOCK_DATABASE_URL (external/managed Postgres). Never scaffolds SQLite.
ensure_postgresql() {
  info "Preparing PostgreSQL..."

  if [[ -n "${PGCLOCK_DATABASE_URL:-}" ]]; then
    case "${PGCLOCK_DATABASE_URL}" in
      postgresql://*|postgresql+asyncpg://*|postgres://*)
        ok "Using PGCLOCK_DATABASE_URL from environment (will verify TCP)"
        ;;
      *)
        err "PGCLOCK_DATABASE_URL must be a PostgreSQL URL (got non-Postgres value)."
        return 1
        ;;
    esac
  fi

  if [[ -z "${PGCLOCK_DATABASE_URL:-}" && -f .env ]]; then
    local existing
    existing="$(env_get DATABASE_URL "")"
    case "$existing" in
      postgresql://*|postgresql+asyncpg://*|postgres://*)
        export PGCLOCK_DATABASE_URL="$existing"
        ok "Found PostgreSQL DATABASE_URL in existing .env"
        ;;
      sqlite*|*"sqlite"*)
        err "Existing .env still points at SQLite. New installs require PostgreSQL."
        err "Set DATABASE_URL to postgresql+asyncpg://… or remove .env and re-run install."
        return 1
        ;;
      "")
        ;;
      *)
        err "Unsupported DATABASE_URL in .env (PostgreSQL required)."
        return 1
        ;;
    esac
  fi

  # Local server for default installs (and when .env already has local PG URL).
  if systemctl list-unit-files postgresql.service >/dev/null 2>&1 \
    || dpkg -s postgresql >/dev/null 2>&1; then
    sudo_wrap systemctl enable postgresql >/dev/null 2>&1 || true
    sudo_wrap systemctl start postgresql >/dev/null 2>&1 || true
    # Debian multi-cluster: also start the cluster on 5432 if pg_ctlcluster exists.
    if command -v pg_lsclusters >/dev/null 2>&1 && command -v pg_ctlcluster >/dev/null 2>&1; then
      local _pg_line _pg_ver _pg_name
      _pg_line="$(pg_lsclusters --no-header 2>/dev/null | awk '$3=="5432"{print; exit}')"
      if [[ -n "$_pg_line" ]]; then
        _pg_ver="$(awk '{print $1}' <<<"$_pg_line")"
        _pg_name="$(awk '{print $2}' <<<"$_pg_line")"
        sudo_wrap pg_ctlcluster "$_pg_ver" "$_pg_name" start >/dev/null 2>&1 || true
      fi
    fi
  fi

  if command -v pg_isready >/dev/null 2>&1; then
    local i ready=0
    for i in $(seq 1 45); do
      # Prefer TCP probe — works as root or non-root without `sudo -u` pitfalls.
      if pg_isready -h 127.0.0.1 -q 2>/dev/null; then
        ready=1
        break
      fi
      # Socket-only clusters still count as "postgres up"; setup_postgres.sh
      # will enable listen_addresses=localhost before emitting DATABASE_URL.
      # Use `sudo -u` even as root — sudo_wrap drops -u when already root.
      if sudo -u postgres pg_isready -q 2>/dev/null \
        || pg_isready -q 2>/dev/null; then
        ready=1
        break
      fi
      sleep 1
    done
    if [[ "$ready" -ne 1 ]]; then
      err "PostgreSQL did not become ready. Check: systemctl status postgresql"
      return 1
    fi
  fi

  # Reuse existing URL only if TCP auth still works; else re-provision local.
  # Never skip verify for env-provided URLs (that caused silent dead panels).
  if [[ -n "${PGCLOCK_DATABASE_URL:-}" ]]; then
    if DATABASE_URL="${PGCLOCK_DATABASE_URL}" python3 - <<'PY' 2>/dev/null
import os, subprocess, urllib.parse
raw = os.environ["DATABASE_URL"].strip()
for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
    if raw.startswith(prefix):
        raw = "postgresql://" + raw[len(prefix):]
        break
u = urllib.parse.urlparse(raw)
user = urllib.parse.unquote(u.username or "")
password = urllib.parse.unquote(u.password or "") if u.password is not None else ""
host = u.hostname or "127.0.0.1"
port = str(u.port or 5432)
dbname = (u.path or "/pgclock").lstrip("/") or "pgclock"
# Reject Unix-socket URLs — panel/asyncpg cannot use them.
qs = urllib.parse.parse_qs(u.query)
host_q = (qs.get("host") or [""])[0]
if host_q.startswith("/") or (not u.hostname and host_q.startswith("/")):
    raise SystemExit(1)
env = os.environ.copy()
env["PGPASSWORD"] = password
# Clear conflicting libpq overrides from the installer shell.
for k in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGCLUSTER", "PGPASSFILE"):
    env.pop(k, None)
r = subprocess.run(
    ["psql", "-h", host, "-p", port, "-U", user, "-d", dbname, "-w", "-v", "ON_ERROR_STOP=1", "-c", "SELECT 1"],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
)
raise SystemExit(0 if r.returncode == 0 else 1)
PY
    then
      ok "PostgreSQL ready (existing DATABASE_URL)"
      return 0
    fi
    # Only auto-reprovision when the URL points at local Postgres.
    local _db_host
    _db_host="$(
      DATABASE_URL="${PGCLOCK_DATABASE_URL}" python3 - <<'PY'
import os, urllib.parse
raw = os.environ["DATABASE_URL"].strip()
for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
    if raw.startswith(prefix):
        raw = "postgresql://" + raw[len(prefix):]
        break
u = urllib.parse.urlparse(raw)
print(u.hostname or "")
PY
    )"
    case "${_db_host}" in
      127.0.0.1|localhost|"" )
        warn "Existing DATABASE_URL is not reachable — re-provisioning local Postgres role/db"
        unset PGCLOCK_DATABASE_URL
        ;;
      *)
        err "PGCLOCK_DATABASE_URL / DATABASE_URL is not reachable at ${_db_host}."
        err "Fix the remote Postgres auth/URL, or unset it to provision local Postgres."
        return 1
        ;;
    esac
  fi

  local emit_file
  # Prefer project data/ over /tmp: sticky + fs.protected_regular blocks root
  # from overwriting another user's pre-created file under /tmp.
  mkdir -p "${SCRIPT_DIR}/data"
  emit_file="${SCRIPT_DIR}/data/.pgclock_database_url.tmp"
  rm -f "$emit_file"
  # Run as root so role/db creation works; read URL back via sudo (file may be root-owned).
  # Clear PG* so a polluted installer shell cannot point setup at the wrong cluster.
  if ! sudo_wrap env -u PGHOST -u PGHOSTADDR -u PGPORT -u PGCLUSTER -u PGPASSWORD -u PGPASSFILE \
    PGCLOCK_EMIT_URL_FILE="$emit_file" \
    bash "${SCRIPT_DIR}/scripts/setup_postgres.sh" pgclock pgclock; then
    sudo_wrap rm -f "$emit_file" 2>/dev/null || rm -f "$emit_file"
    err "Failed to provision PostgreSQL database (scripts/setup_postgres.sh)."
    return 1
  fi
  if ! sudo_wrap test -s "$emit_file"; then
    sudo_wrap rm -f "$emit_file" 2>/dev/null || rm -f "$emit_file"
    err "setup_postgres.sh did not emit DATABASE_URL."
    return 1
  fi
  export PGCLOCK_DATABASE_URL
  PGCLOCK_DATABASE_URL="$(sudo_wrap cat "$emit_file" | tr -d '\r\n')"
  sudo_wrap rm -f "$emit_file" 2>/dev/null || rm -f "$emit_file"
  case "${PGCLOCK_DATABASE_URL}" in
    postgresql://*|postgresql+asyncpg://*|postgres://*)
      ok "PostgreSQL database provisioned (role/db: pgclock)"
      ;;
    *)
      err "Invalid URL from setup_postgres.sh"
      return 1
      ;;
  esac
}

ensure_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    err "python3 is not available."
    return 1
  fi
  SYSTEM_PY=python3
  ok "Python $($SYSTEM_PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
}

ensure_venv() {
  if [[ ! -d .venv ]]; then
    info "Creating virtualenv..."
    "$SYSTEM_PY" -m venv .venv
    ok "venv created"
  else
    ok "venv exists"
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  local hash_file=".venv/.requirements.sha256"
  local new_hash=""
  if command -v sha256sum >/dev/null 2>&1; then
    new_hash="$(sha256sum requirements.txt | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    new_hash="$(shasum -a 256 requirements.txt | awk '{print $1}')"
  fi
  if [[ -n "$new_hash" && -f "$hash_file" && "$(cat "$hash_file" 2>/dev/null)" == "$new_hash" && -x .venv/bin/python ]]; then
    ok "Python dependencies unchanged (skip pip)"
  else
    if [[ ! -f "$hash_file" ]]; then
      pip install -U pip wheel -q --disable-pip-version-check --no-input || true
    fi
    pip install -r requirements.txt -q --disable-pip-version-check --no-input
    if [[ -n "$new_hash" ]]; then
      printf '%s\n' "$new_hash" > "$hash_file"
    fi
    ok "Python dependencies installed"
  fi
  PY="${SCRIPT_DIR}/.venv/bin/python"
  PIP="${SCRIPT_DIR}/.venv/bin/pip"
}

service_installed() {
  [[ -f "$SERVICE_PATH" ]] || systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"
}

service_active() {
  systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

env_get() {
  local key="$1"
  local fallback="${2:-}"
  if [[ ! -f .env ]]; then
    printf '%s\n' "$fallback"
    return
  fi
  local line
  line="$(grep -E "^${key}=" .env | tail -n1 || true)"
  if [[ -z "$line" ]]; then
    printf '%s\n' "$fallback"
    return
  fi
  local val="${line#*=}"
  val="${val%\"}"
  val="${val#\"}"
  val="${val%\'}"
  val="${val#\'}"
  printf '%s\n' "$val"
}

write_env_file() {
  local py="${PY:-}"
  if [[ -z "$py" || ! -x "$py" ]]; then
    py="${SYSTEM_PY:-python3}"
  fi
  WEB_ADMIN_PASSWORD="$WEB_ADMIN_PASSWORD" \
  BOT_TOKEN="$BOT_TOKEN" \
  BOT_USERNAME="$BOT_USERNAME" \
  ADMIN_IDS="$ADMIN_IDS" \
  PG_BASE_URL="$PG_BASE_URL" \
  PG_USERNAME="$PG_USERNAME" \
  PG_PASSWORD="$PG_PASSWORD" \
  WEB_PORT="$WEB_PORT" \
  WEB_SECRET="$WEB_SECRET" \
  WEB_ADMIN_USER="$WEB_ADMIN_USER" \
  PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
  CURRENCY="$CURRENCY" \
  "$py" - <<'PY'
import json, os, subprocess, sys
from pathlib import Path
payload = {
    "BOT_TOKEN": os.environ["BOT_TOKEN"],
    "BOT_USERNAME": os.environ["BOT_USERNAME"],
    "ADMIN_IDS": os.environ["ADMIN_IDS"],
    "PG_BASE_URL": os.environ["PG_BASE_URL"],
    "PG_USERNAME": os.environ["PG_USERNAME"],
    "PG_PASSWORD": os.environ["PG_PASSWORD"],
    "WEB_HOST": "0.0.0.0",
    "WEB_PORT": os.environ["WEB_PORT"],
    "WEB_SECRET": os.environ["WEB_SECRET"],
    "WEB_ADMIN_USER": os.environ["WEB_ADMIN_USER"],
    "WEB_ADMIN_PASSWORD": os.environ["WEB_ADMIN_PASSWORD"],
    # Fresh installs always write PostgreSQL (provisioned by ensure_postgresql).
    "DATABASE_URL": os.environ.get("PGCLOCK_DATABASE_URL", "").strip(),
    "WEBHOOK_URL": "",
    "WEBHOOK_PATH": "/telegram/webhook",
    "PUBLIC_BASE_URL": os.environ.get("PUBLIC_BASE_URL", ""),
    "CURRENCY": os.environ.get("CURRENCY", "Toman"),
    "DEFAULT_LOCALE": "fa",
}
db_url = payload["DATABASE_URL"]
if not db_url.startswith(("postgresql://", "postgresql+asyncpg://", "postgres://")):
    print(
        "DATABASE_URL missing or not PostgreSQL — ensure_postgresql must run first",
        file=sys.stderr,
    )
    sys.exit(1)
proc = subprocess.run(
    [sys.executable, str(Path("scripts/write_env.py"))],
    input=json.dumps(payload),
    text=True,
    check=True,
    capture_output=True,
)
print(proc.stdout.strip())
PY
}

install_restart_helper() {
  local service_user="${1:-$(whoami)}"
  local ctl_src="${SCRIPT_DIR}/scripts/pgclockbot-ctl"
  local ctl_dst="/usr/local/lib/pgclockbot/ctl"
  local sudoers_path="/etc/sudoers.d/pgclockbot"
  if [[ ! -f "$ctl_src" ]]; then
    warn "restart helper source missing: ${ctl_src}"
    return 1
  fi
  local tmp_ctl tmp_sudoers
  tmp_ctl="$(mktemp)"
  tmp_sudoers="$(mktemp)"
  cp "$ctl_src" "$tmp_ctl"
  chmod 755 "$tmp_ctl"
  cat > "$tmp_sudoers" <<EOF
# Managed by PGClockBot — passwordless service control for panel SSL/updates/CLI
${service_user} ALL=(root) NOPASSWD: ${ctl_dst} *
${service_user} ALL=(root) NOPASSWD: /bin/systemctl start ${SERVICE_NAME}, /usr/bin/systemctl start ${SERVICE_NAME}, /bin/systemctl stop ${SERVICE_NAME}, /usr/bin/systemctl stop ${SERVICE_NAME}, /bin/systemctl restart ${SERVICE_NAME}, /usr/bin/systemctl restart ${SERVICE_NAME}, /bin/systemctl try-restart ${SERVICE_NAME}, /usr/bin/systemctl try-restart ${SERVICE_NAME}, /bin/systemctl enable ${SERVICE_NAME}, /usr/bin/systemctl enable ${SERVICE_NAME}, /bin/systemctl is-active ${SERVICE_NAME}, /usr/bin/systemctl is-active ${SERVICE_NAME}
EOF
  chmod 440 "$tmp_sudoers"
  sudo_wrap install -d -m 755 /usr/local/lib/pgclockbot
  sudo_wrap install -m 755 "$tmp_ctl" "$ctl_dst"
  sudo_wrap install -m 440 "$tmp_sudoers" "$sudoers_path"
  if sudo_wrap visudo -cf "$sudoers_path" >/dev/null 2>&1; then
    ok "Passwordless restart helper installed for ${service_user}"
  else
    warn "sudoers validation failed — removing ${sudoers_path}"
    sudo_wrap rm -f "$sudoers_path" || true
  fi
  rm -f "$tmp_ctl" "$tmp_sudoers"
  # Global CLI (best-effort)
  if [[ -f "${SCRIPT_DIR}/scripts/install_global_cli.sh" ]]; then
    sudo_wrap bash "${SCRIPT_DIR}/scripts/install_global_cli.sh" "${SCRIPT_DIR}" \
      && ok "Global pgclock CLI installed → /usr/local/bin/pgclock" \
      || warn "Global CLI install skipped/failed"
  fi
}

install_systemd() {
  local service_user="${1:-$(whoami)}"
  local content
  content="[Unit]
Description=PGClockBot — PasarGuard Telegram shop
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=${service_user}
WorkingDirectory=${SCRIPT_DIR}
# Include system bins so pg_dump/pg_restore (postgresql-client) are visible to the bot.
Environment=PATH=${SCRIPT_DIR}/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PGCLOCKBOT_SERVICE_USER=${service_user}
# Fail fast in journal if boot/init_db hangs (uvicorn binds only after lifespan).
TimeoutStartSec=180
ExecStart=${SCRIPT_DIR}/.venv/bin/python run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"
  local tmp
  tmp="$(mktemp)"
  printf '%s\n' "$content" > "$tmp"
  sudo_wrap cp "$tmp" "$SERVICE_PATH"
  rm -f "$tmp"
  sudo_wrap systemctl daemon-reload
  sudo_wrap systemctl enable --now "$SERVICE_NAME"
  install_restart_helper "$service_user" || true
  # Ensure global CLI points at this install
  if [[ -f "${SCRIPT_DIR}/scripts/install_global_cli.sh" ]]; then
    sudo_wrap bash "${SCRIPT_DIR}/scripts/install_global_cli.sh" "${SCRIPT_DIR}" || true
  fi
  ok "systemd service enabled: ${SERVICE_NAME}"
}

restart_service_if_any() {
  if service_installed; then
    info "Restarting ${SERVICE_NAME}..."
    sudo_wrap systemctl restart "$SERVICE_NAME"
    ok "Service restarted"
  else
    warn "systemd service not installed. Start manually:"
    echo -e "    ${C}source .venv/bin/activate && python run.py${N}"
  fi
}

# ── actions ─────────────────────────────────────────────
cmd_install() {
  banner
  require_ubuntu_22_plus || return 1
  ensure_apt_packages || return 1
  ensure_python || return 1

  # Defaults — all bot/admin/PasarGuard config is done in the web wizard (/setup)
  WEB_PORT="${WEB_PORT:-9000}"
  WEB_SECRET="$(gen_secret)"
  BOT_TOKEN=""
  BOT_USERNAME=""
  ADMIN_IDS=""
  PG_BASE_URL=""
  PG_USERNAME=""
  PG_PASSWORD=""
  WEB_ADMIN_USER="admin"
  WEB_ADMIN_PASSWORD=""
  PUBLIC_BASE_URL=""
  CURRENCY="Toman"
  INSTALL_SSL_MODE="none"
  INSTALL_SSL_DOMAIN=""
  INSTALL_SSL_EMAIL=""

  local fresh=0
  if [[ -f .env ]]; then
    warn ".env already exists — keeping it (no overwrite)."
    WEB_PORT="$(env_get WEB_PORT "${WEB_PORT}")"
    ok "Using existing config · port=${WEB_PORT}"
  else
    fresh=1
    step "Panel port & SSL"
    prompt_install_port_and_ssl || return 1
  fi

  step "PostgreSQL"
  ensure_postgresql || return 1

  step "Python packages"
  ensure_venv || return 1
  mkdir -p data

  if [[ "$fresh" -eq 1 ]]; then
    step "Scaffold configuration"
    write_env_file
    rm -f data/web_admin.json data/setup_complete.flag data/setup_in_progress.flag \
      data/setup_gate.token data/setup_gate.json data/setup_entry.url 2>/dev/null || true
    ok ".env scaffold written · PostgreSQL · finish setup in the browser"
  elif [[ -n "${PGCLOCK_DATABASE_URL:-}" && -f .env ]]; then
    # Re-provision may have rotated the role password — keep .env in sync.
    local cur_db
    cur_db="$(env_get DATABASE_URL "")"
    if [[ "$cur_db" != "$PGCLOCK_DATABASE_URL" ]]; then
      DATABASE_URL="$PGCLOCK_DATABASE_URL" python3 - <<'PY'
import os, re
from pathlib import Path
path = Path(".env")
text = path.read_text(encoding="utf-8")
url = os.environ["DATABASE_URL"].replace("\\", "\\\\").replace('"', '\\"')
line = f'DATABASE_URL="{url}"'
if re.search(r"^DATABASE_URL=", text, flags=re.M):
    text = re.sub(r"^DATABASE_URL=.*$", line, text, count=1, flags=re.M)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
path.write_text(text, encoding="utf-8")
path.chmod(0o600)
print("updated")
PY
      ok "Synced DATABASE_URL into existing .env"
    fi
  fi

  # Critical: loopback bind makes every public Setup URL fail (ERR_EMPTY_RESPONSE).
  ensure_public_web_host

  # Cert + PUBLIC_BASE_URL before the panel process starts (TLS from first boot).
  if [[ "$fresh" -eq 1 && "${INSTALL_SSL_MODE:-none}" != "none" ]]; then
    apply_install_ssl || {
      err "SSL setup failed — install aborted (fix DNS/port 80 or choose mode 3)."
      return 1
    }
    # Reload WEB_PORT / PUBLIC_BASE_URL after Python wrote them.
    WEB_PORT="$(env_get WEB_PORT "${WEB_PORT}")"
    PUBLIC_BASE_URL="$(env_get PUBLIC_BASE_URL "${PUBLIC_BASE_URL}")"
  fi

  # Migrate schema BEFORE systemd — otherwise uvicorn never binds while init_db hangs.
  ensure_db_schema || return 1

  step "systemd service"
  svc_user="$(whoami)"
  if [[ "$svc_user" == "root" ]]; then
    warn "Installing as root is discouraged — prefer a dedicated non-root user for the panel service"
  fi
  install_systemd "$svc_user" || true
  if service_installed; then
    sudo_wrap systemctl enable --now "$SERVICE_NAME" || true
    if service_active; then
      ok "Service is running"
    else
      warn "Service installed but not active — check: journalctl -u ${SERVICE_NAME} -n 50"
    fi
  else
    warn "systemd unit missing — starting panel in background"
    nohup "${SCRIPT_DIR}/.venv/bin/python" "${SCRIPT_DIR}/run.py" \
      >/tmp/pgclock-panel.log 2>&1 &
    ok "Panel started (pid $!) · log: /tmp/pgclock-panel.log"
  fi

  step "Panel health"
  local health_ok=0
  local health_url="" health_scheme="http"
  local hi
  # Wait until something accepts on WEB_PORT (lifespan/DB can take a while).
  if ! wait_port_listen "${WEB_PORT}" 75; then
    warn "Nothing listening on :${WEB_PORT} yet — restarting service once"
    if service_installed; then
      sudo_wrap systemctl restart "$SERVICE_NAME" || true
      wait_port_listen "${WEB_PORT}" 45 || true
    fi
  fi
  for hi in $(seq 1 45); do
    if probe_panel_health; then
      break
    fi
    sleep 1
  done
  # One more restart if SSL expected but still dead (common after cert copy).
  if [[ "$health_ok" -ne 1 ]] && ssl_is_enabled_on_disk && service_installed; then
    warn "HTTPS health still failing — restarting ${SERVICE_NAME} and retrying"
    sudo_wrap systemctl restart "$SERVICE_NAME" || true
    wait_port_listen "${WEB_PORT}" 45 || true
    for hi in $(seq 1 30); do
      if probe_panel_health; then
        break
      fi
      sleep 1
    done
  fi
  if [[ "$health_ok" -eq 1 ]]; then
    ok "Panel health OK on ${health_scheme}://127.0.0.1:${WEB_PORT}"
  else
    err "Panel did not answer /health on ${health_url:-127.0.0.1:${WEB_PORT}}"
    warn "Check logs: journalctl -u ${SERVICE_NAME} -n 80 --no-pager"
    warn "DB URL / Postgres: grep DATABASE_URL .env && systemctl status postgresql --no-pager"
    if command -v journalctl >/dev/null 2>&1; then
      info "Last service log lines:"
      sudo_wrap journalctl -u "$SERVICE_NAME" -n 25 --no-pager 2>/dev/null \
        | sed 's/^/    /' || true
    fi
    # Still continue — Setup URL must always be printed below.
  fi

  step "Firewall"
  if command -v ufw >/dev/null 2>&1; then
    sudo_wrap ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 || true
    ok "UFW: allowed ${WEB_PORT}/tcp (if UFW is active)"
    if [[ "${INSTALL_SSL_MODE:-}" == "domain" ]] || ssl_is_enabled_on_disk; then
      # Keep 80 open for future LE renewals when domain cert is in use.
      if [[ -f data/certs/meta.json ]] && grep -q 'letsencrypt' data/certs/meta.json 2>/dev/null; then
        sudo_wrap ufw allow 80/tcp >/dev/null 2>&1 || true
        ok "UFW: allowed 80/tcp (ACME renewals)"
      fi
    fi
  else
    info "UFW not installed — open port ${WEB_PORT} manually if needed"
  fi
  info "Cloud firewall (Hetzner/AWS/…): also allow inbound TCP ${WEB_PORT} in the provider panel"

  local ip setup_url panel_url health_hint
  _DETECTED_SERVER_IP=""  # refresh once for this SUCCESS block
  ip="$(detect_server_ip)"
  panel_url="$(panel_public_base_url)/"
  if [[ "$health_scheme" == "https" ]] || ssl_is_enabled_on_disk; then
    health_hint="curl -skS https://127.0.0.1:${WEB_PORT}/health"
  else
    health_hint="curl -sS http://127.0.0.1:${WEB_PORT}/health"
  fi
  setup_url=""
  if [[ "$fresh" -eq 1 ]] || [[ ! -f data/setup_complete.flag ]]; then
    setup_url="$(setup_wizard_url "$panel_url" || true)"
    if [[ -z "$setup_url" || "$setup_url" != *"?gate="* ]]; then
      sleep 2
      setup_url="$(setup_wizard_url "$panel_url" || true)"
    fi
    # File fallback if python printed nothing but wrote the hint file.
    if [[ -z "$setup_url" || "$setup_url" != *"?gate="* ]]; then
      if [[ -f data/setup_entry.url ]]; then
        setup_url="$(tr -d '\r\n' < data/setup_entry.url)"
      fi
    fi
  fi

  # ALWAYS print Setup URL when setup is incomplete — even if /health failed.
  if [[ "$fresh" -eq 1 ]] || [[ ! -f data/setup_complete.flag ]]; then
    if [[ -n "$setup_url" && "$setup_url" == *"?gate="* ]]; then
      if [[ "$health_ok" -eq 1 ]]; then
        print_success "Install complete" \
          "${B}Setup URL (one-time, 15 min):${N}" \
          "${B}${setup_url}${N}" \
          "" \
          "Open that exact URL (must include ?gate=…)." \
          "Bare ${panel_url} from the internet is blocked until setup finishes." \
          "Use the full host:port (e.g. https://domain:8443/…), not port 443." \
          "Cloud firewall (Hetzner/AWS/…): allow inbound TCP ${WEB_PORT}." \
          "On-server test: ${health_hint}" \
          "Hint file: ${SCRIPT_DIR}/data/setup_entry.url"
      else
        print_success "Install finished — panel not healthy yet" \
          "${B}Setup URL (one-time, 15 min) — open after service is up:${N}" \
          "${B}${setup_url}${N}" \
          "" \
          "Service did not answer /health yet. Fix, then open the Setup URL above." \
          "journalctl -u ${SERVICE_NAME} -n 80 --no-pager" \
          "Then: bash pgclock.sh status" \
          "On-server test: ${health_hint}" \
          "Hint file: ${SCRIPT_DIR}/data/setup_entry.url"
      fi
    elif [[ "$health_ok" -ne 1 ]]; then
      print_success "Install finished — panel not healthy yet" \
        "Service did not answer /health. Check logs first:" \
        "journalctl -u ${SERVICE_NAME} -n 80 --no-pager" \
        "Then: bash pgclock.sh status" \
        "Or:   cat ${SCRIPT_DIR}/data/setup_entry.url"
    else
      print_success "Install complete" \
        "Setup URL was not generated automatically." \
        "Run:  bash pgclock.sh status" \
        "Or:   cat ${SCRIPT_DIR}/data/setup_entry.url" \
        "Panel base: ${panel_url}" \
        "Cloud firewall: allow inbound TCP ${WEB_PORT}."
    fi
  else
    print_success "Install/refresh complete" \
      "Panel:      ${panel_url}" \
      "Manage:     bash pgclock.sh" \
      "Logs:       journalctl -u ${SERVICE_NAME} -f"
  fi
  return 0
}

cmd_update() {
  banner_small "Update"
  if [[ ! -f .env ]]; then
    err ".env not found. Run Install first."
    return 1
  fi
  ok "Keeping existing .env"
  cp -a .env ".env.bak.$(date +%Y%m%d%H%M%S)"
  ok ".env backup created"

  if [[ -d .git ]]; then
    info "Pulling latest code from GitHub..."
    local branch
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
    if [[ "$branch" == "HEAD" ]]; then
      branch="main"
    fi
    # Targeted fetch (no tags / no --all) — much faster on weak VPS links
    if ! timeout 120 git fetch --no-tags --prune origin "$branch" 2>/dev/null; then
      timeout 120 git fetch --no-tags --prune origin main || true
      branch="main"
    fi
    if git pull --ff-only origin "$branch" 2>/dev/null || git pull --ff-only 2>/dev/null; then
      ok "Code updated (branch: ${branch})"
    else
      warn "Fast-forward failed — syncing hard to origin/main (keeps .env + data)"
      git fetch --no-tags --prune origin main || true
      git checkout -f -B main origin/main
      git reset --hard origin/main
      git clean -fd --exclude=.env --exclude=data --exclude=.venv --exclude='.env.bak.*'
      ok "Code synced to origin/main"
    fi
  else
    warn "Not a git repo — skipped git pull."
  fi

  ensure_python || return 1
  ensure_venv || return 1
  mkdir -p data
  "$PY" - <<'PY' || true
from app.services.web_auth import AUTH_FILE, load_web_admin
creds = load_web_admin()
print(f"web_admin={creds.get('username')} file={AUTH_FILE.exists()}")
PY

  if [[ ! -f .env ]]; then
    local latest_bak
    latest_bak="$(ls -1t .env.bak.* 2>/dev/null | head -n1 || true)"
    if [[ -n "$latest_bak" ]]; then
      cp -a "$latest_bak" .env
      warn "Restored .env from backup"
    else
      err ".env missing after update."
      return 1
    fi
  fi

  ensure_public_web_host

  # Migrate before restart so the new process does not hang with no listener.
  ensure_db_schema || {
    err "Schema migration failed — not restarting a broken panel."
    return 1
  }

  # Backup tools + systemd PATH (pg_dump must be visible to the service).
  ensure_pg_client_tools || true

  # Ensure passwordless restart helper exists for in-panel SSL/updates
  if service_installed; then
    local svc_user
    svc_user="$(systemctl show -p User --value "$SERVICE_NAME" 2>/dev/null || whoami)"
    [[ -z "$svc_user" || "$svc_user" == "-" ]] && svc_user="$(whoami)"
    # Rewrite unit so PATH includes /usr/bin (fixes "pg_dump not found" under systemd).
    install_systemd "$svc_user" || true
    install_restart_helper "$svc_user" || true
  fi

  restart_service_if_any
  print_success "Update complete" \
    "Config:     .env kept (WEB_HOST forced public if it was loopback)" \
    "Schema:     migrated before restart" \
    "Backups:    postgresql-client / systemd PATH refreshed" \
    "Logs:       journalctl -u ${SERVICE_NAME} -f"
  return 0
}

cmd_edit_env() {
  banner_small "Edit .env"
  if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
      cp .env.example .env
      warn "Created .env from .env.example — fill in values."
    else
      err ".env not found. Run Install first."
      return 1
    fi
  fi
  cp -a .env ".env.bak.$(date +%Y%m%d%H%M%S)"
  ok "Backup created"
  local editor="${EDITOR:-nano}"
  if ! command -v "$editor" >/dev/null 2>&1; then
    editor="nano"
  fi
  if ! command -v "$editor" >/dev/null 2>&1; then
    editor="vi"
  fi
  info "Opening with ${editor}..."
  "$editor" .env
  echo ""
  if ask_yn "Restart service to apply changes?" "Y"; then
    restart_service_if_any
  fi
  print_success "Configuration saved" \
    "Edited:     .env"
  return 0
}

cmd_web_panel() {
  while true; do
    banner_small "Web panel"
    local port user ip
    port="$(env_get WEB_PORT 9000)"
    user="$(web_username)"
    ip="$(detect_server_ip)"
    echo -e "  Login URL : ${B}http://${ip}:${port}/login${N}"
    echo -e "  Health    : ${B}http://127.0.0.1:${port}/health${N}"
    echo -e "  Username  : ${B}${user}${N}"
    echo ""
    echo -e "  ${B}1)${N} Reset web password"
    echo -e "  ${B}2)${N} Check /health"
    echo -e "  ${B}3)${N} Allow firewall port ${port}/tcp"
    echo -e "  ${B}0)${N} Back"
    echo ""
    local choice=""
    prompt_read choice "  ${B}Select${N}: "
    case "${choice}" in
      1)
        if [[ ! -f .venv/bin/python ]]; then
          err "venv missing. Run Install first."
        else
          "$PY" scripts/set_web_password.py
          restart_service_if_any
          print_success "Web password updated"
        fi
        pause
        ;;
      2)
        if command -v curl >/dev/null 2>&1; then
          echo ""
          curl -sS "http://127.0.0.1:${port}/health" || err "Health check failed (is the bot running?)"
          echo ""
          echo ""
          ok "Health check finished"
        else
          err "curl not installed."
        fi
        pause
        ;;
      3)
        if command -v ufw >/dev/null 2>&1; then
          sudo_wrap ufw allow "${port}/tcp" >/dev/null 2>&1 || true
          ok "UFW allowed ${port}/tcp"
        else
          echo -e "  Run manually: ${C}sudo ufw allow ${port}/tcp && sudo ufw reload${N}"
        fi
        pause
        ;;
      0) return 0 ;;
      "") ;;
      *) err "Invalid option." ; pause ;;
    esac
  done
}

cmd_service() {
  while true; do
    banner_small "Service"
    if service_installed; then
      local state
      state="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)"
      echo -e "  Unit  : ${B}${SERVICE_NAME}${N}"
      echo -e "  State : ${B}${state}${N}"
    else
      echo -e "  ${D}systemd unit not installed${N}"
    fi
    echo ""
    echo -e "  ${B}1)${N} Status"
    echo -e "  ${B}2)${N} Start / Enable"
    echo -e "  ${B}3)${N} Restart"
    echo -e "  ${B}4)${N} Stop"
    echo -e "  ${B}5)${N} Logs (follow, Ctrl+C to stop)"
    echo -e "  ${B}6)${N} Install systemd unit"
    echo -e "  ${B}0)${N} Back"
    echo ""
    local choice=""
    prompt_read choice "  ${B}Select${N}: "
    case "${choice}" in
      1)
        if service_installed; then
          systemctl status "$SERVICE_NAME" --no-pager || true
          ok "Status shown"
        else
          warn "Service not installed."
        fi
        pause
        ;;
      2)
        if ! service_installed; then
          install_systemd "$(whoami)"
        else
          sudo_wrap systemctl enable --now "$SERVICE_NAME"
          ok "Started"
        fi
        print_success "Service start requested"
        pause
        ;;
      3)
        restart_service_if_any
        print_success "Service restart requested"
        pause
        ;;
      4)
        if service_installed; then
          sudo_wrap systemctl stop "$SERVICE_NAME"
          ok "Stopped"
        else
          warn "Service not installed."
        fi
        pause
        ;;
      5)
        if service_installed; then
          journalctl -u "$SERVICE_NAME" -f
        else
          warn "Service not installed."
          pause
        fi
        ;;
      6)
        if [[ ! -d .venv ]]; then
          err "Run Install first."
        else
          install_systemd "$(ask "System user" "$(whoami)")"
          print_success "systemd unit installed"
        fi
        pause
        ;;
      0) return 0 ;;
      "") ;;
      *) err "Invalid option." ; pause ;;
    esac
  done
}

# Parse DATABASE_URL → host/db/user for uninstall wipe. Prints: host\tdb\tuser
# Empty output when .env / URL missing.
_uninstall_parse_database_url() {
  [[ -f .env ]] || return 0
  DATABASE_URL="$(env_get DATABASE_URL "")" python3 - <<'PY' 2>/dev/null || true
import os, re, urllib.parse
raw = (os.environ.get("DATABASE_URL") or "").strip().strip('"').strip("'")
if not raw:
    raise SystemExit(0)
for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
    if raw.startswith(prefix):
        raw = "postgresql://" + raw[len(prefix):]
        break
else:
    raise SystemExit(0)
u = urllib.parse.urlparse(raw)
qs = urllib.parse.parse_qs(u.query)
host = (u.hostname or "").strip() or (qs.get("host") or [""])[0]
user = urllib.parse.unquote(u.username or "") or "pgclock"
dbname = (u.path or "/pgclock").lstrip("/") or "pgclock"
# Identifiers only — refuse odd names before SQL.
ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
if not ident.match(user) or not ident.match(dbname):
    raise SystemExit(0)
print(f"{host}\t{dbname}\t{user}")
PY
}

# Drop local Postgres DB + role owned by this bot. Never touches remote hosts
# or apt packages. Safe to call when Postgres is down / already gone.
wipe_local_postgres_for_uninstall() {
  local parsed host dbname dbuser sql_file rc
  parsed="$(_uninstall_parse_database_url)"
  if [[ -z "$parsed" ]]; then
    # Fallback defaults used by ensure_postgresql / setup_postgres.sh
    host="127.0.0.1"
    dbname="pgclock"
    dbuser="pgclock"
    info "No DATABASE_URL in .env — will still try to drop local role/db pgclock/pgclock"
  else
    IFS=$'\t' read -r host dbname dbuser <<<"$parsed"
  fi
  case "${host}" in
    127.0.0.1|localhost|::1|""|/var/run/postgresql|/run/postgresql|/tmp)
      ;;
    /*)
      # Unix socket path — still local
      ;;
    *)
      warn "DATABASE_URL points at remote host «${host}» — leaving that database alone."
      warn "Drop it yourself on the remote server if you want a full DB wipe."
      return 0
      ;;
  esac

  if ! command -v psql >/dev/null 2>&1; then
    warn "psql not found — skipped Postgres wipe (install postgresql-client to drop leftovers)."
    return 0
  fi

  info "Dropping local PostgreSQL database «${dbname}» and role «${dbuser}»…"
  # Identifiers already validated as [A-Za-z_][A-Za-z0-9_]*.
  # Use `sudo -u` even as root — sudo_wrap drops -u when already root.
  sql_file="$(mktemp /tmp/pgclock-uninstall-XXXXXX.sql)"
  cat >"$sql_file" <<SQL
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE datname = '${dbname}'
   AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS ${dbname};
DO \$\$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${dbuser}') THEN
    EXECUTE format('REASSIGN OWNED BY %I TO CURRENT_USER', '${dbuser}');
    EXECUTE format('DROP OWNED BY %I', '${dbuser}');
    EXECUTE format('DROP ROLE %I', '${dbuser}');
  END IF;
END
\$\$;
SQL
  rc=1
  if sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres -f "$sql_file" >/dev/null 2>&1; then
    rc=0
  fi
  rm -f "$sql_file"
  if [[ "$rc" -eq 0 ]]; then
    ok "Local PostgreSQL database/role removed (${dbname}/${dbuser})"
  else
    warn "Could not drop Postgres db/role automatically — if reinstall fails, run:"
    warn "  sudo -u postgres psql -c \"DROP DATABASE IF EXISTS ${dbname};\""
    warn "  sudo -u postgres psql -c \"DROP ROLE IF EXISTS ${dbuser};\""
  fi
}

# Remove Let's Encrypt lineage issued for this panel domain (pgclock-<domain>).
wipe_letsencrypt_for_uninstall() {
  local meta domain lineage
  meta="${SCRIPT_DIR}/data/certs/meta.json"
  domain=""
  if [[ -f "$meta" ]]; then
    domain="$(
      PGCLOCK_SSL_META="$meta" python3 - <<'PY' 2>/dev/null || true
import json, os
from pathlib import Path
p = Path(os.environ.get("PGCLOCK_SSL_META") or "")
try:
    m = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
d = (m.get("domain") or m.get("host") or "").strip().lower().rstrip(".")
print(d)
PY
    )"
  fi
  if [[ -z "$domain" ]]; then
    info "No panel SSL domain recorded — skipping Let's Encrypt cleanup."
    return 0
  fi
  if ! command -v certbot >/dev/null 2>&1; then
    warn "certbot missing — LE lineage for ${domain} may remain under /etc/letsencrypt."
    return 0
  fi
  info "Removing Let's Encrypt certificates for ${domain}…"
  for lineage in "pgclock-${domain}" "${domain}"; do
    if [[ -d "/etc/letsencrypt/live/${lineage}" ]] \
      || [[ -d "/etc/letsencrypt/archive/${lineage}" ]]; then
      if sudo_wrap certbot delete --cert-name "$lineage" --non-interactive >/dev/null 2>&1; then
        ok "Removed certbot lineage «${lineage}»"
      else
        warn "certbot delete failed for «${lineage}» — remove manually if needed."
      fi
    fi
  done
}

cmd_uninstall() {
  banner_small "Uninstall"
  warn "FULL uninstall removes EVERYTHING for this bot:"
  warn "  systemd service, global CLI, processes, .venv, data, .env, backups,"
  warn "  Let's Encrypt cert for the panel domain (if any),"
  warn "  local PostgreSQL database + role (pgclock),"
  warn "  and the entire project folder: ${SCRIPT_DIR}"
  warn "PostgreSQL *system packages* stay installed (shared server software)."
  if ! ask_yn "Continue full uninstall?" "N"; then
    info "Cancelled."
    return 0
  fi

  # Capture port / DB / SSL metadata BEFORE deleting .env and data/
  local port
  port="$(env_get WEB_PORT 9000)"

  # Stop & remove systemd first so nothing holds DB connections.
  if service_installed || [[ -f "$SERVICE_PATH" ]]; then
    info "Stopping systemd service..."
    sudo_wrap systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    sudo_wrap rm -f "$SERVICE_PATH"
    sudo_wrap systemctl daemon-reload 2>/dev/null || true
    ok "systemd unit removed"
  else
    info "No systemd unit found."
  fi

  # Remove global CLI marker/binary when it points at this install
  if [[ -f /usr/local/lib/pgclockbot/install_root ]]; then
    local marked
    marked="$(tr -d '\r' </usr/local/lib/pgclockbot/install_root | head -n1 || true)"
    if [[ "$marked" == "$SCRIPT_DIR" ]]; then
      info "Removing global pgclock CLI…"
      sudo_wrap rm -f /usr/local/bin/pgclock /usr/local/lib/pgclockbot/install_root \
        /usr/local/lib/pgclockbot/pgclock-wrapper /usr/local/lib/pgclockbot/ctl || true
      sudo_wrap rm -f /etc/sudoers.d/pgclockbot || true
      ok "Global CLI removed"
    fi
  fi

  # Kill leftover bot processes from this install
  info "Stopping leftover processes..."
  pkill -f "${SCRIPT_DIR}/.venv/bin/python .*run.py" 2>/dev/null || true
  pkill -f "python .*${SCRIPT_DIR}/run.py" 2>/dev/null || true
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" 2>/dev/null || true
  fi

  wipe_letsencrypt_for_uninstall
  wipe_local_postgres_for_uninstall

  local root="$SCRIPT_DIR"
  info "Deleting project folder: ${root}"
  cd / || cd "$HOME" || true
  rm -rf "$root"

  printf '\n' > /dev/tty
  printf '%s==========================================%s\n' "$G" "$N" > /dev/tty
  printf '%s  SUCCESS · Full uninstall complete%s\n' "$G" "$N" > /dev/tty
  printf '%s==========================================%s\n' "$G" "$N" > /dev/tty
  printf '  Removed: %s\n' "$root" > /dev/tty
  printf '  Local Postgres db/role and panel LE cert wiped when present.\n' > /dev/tty
  printf '  Reinstall:\n' > /dev/tty
  printf '    bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/main/get.sh)\n' > /dev/tty
  printf '%s==========================================%s\n\n' "$G" "$N" > /dev/tty
  exit 0
}

cmd_status() {
  banner_small "Status"
  if [[ -f .env ]]; then
    ok ".env present"
    local db_url db_kind web_host
    db_url="$(env_get DATABASE_URL "")"
    case "$db_url" in
      postgresql://*|postgresql+asyncpg://*|postgres://*) db_kind="PostgreSQL" ;;
      sqlite*) db_kind="SQLite (unsupported for new installs)" ;;
      "") db_kind="missing DATABASE_URL" ;;
      *) db_kind="unknown" ;;
    esac
    echo -e "  Database: ${B}${db_kind}${N}"
    web_host="$(env_get WEB_HOST "0.0.0.0")"
    echo -e "  WEB_HOST: ${B}${web_host}${N}"
    case "$web_host" in
      127.0.0.1|localhost|::1)
        warn "WEB_HOST is loopback — public URLs will fail. Fix: set WEB_HOST=0.0.0.0 and restart."
        ;;
    esac
  else
    warn ".env missing"
  fi
  if [[ -d .venv ]]; then
    ok "venv present"
  else
    warn "venv missing"
  fi
  local port panel_base
  port="$(env_get WEB_PORT 9000)"
  echo -e "  WEB_PORT: ${B}${port}${N}"
  panel_base="$(panel_public_base_url)"
  echo -e "  Panel URL: ${B}${panel_base}/${N}"
  if ssl_is_enabled_on_disk; then
    local ssl_mode ssl_host
    ssl_mode="$(python3 -c 'import json;from pathlib import Path;m=json.loads(Path("data/certs/meta.json").read_text());print(m.get("mode") or ("self_signed_ip" if m.get("self_signed") else "letsencrypt"))' 2>/dev/null || echo on)"
    ssl_host="$(python3 -c 'import json;from pathlib import Path;m=json.loads(Path("data/certs/meta.json").read_text());print(m.get("domain") or m.get("host") or "")' 2>/dev/null || true)"
    ok "HTTPS enabled · mode=${ssl_mode}${ssl_host:+ · host=${ssl_host}}"
  else
    echo -e "  HTTPS: ${D}off${N}"
  fi
  if service_installed; then
    echo -e "  Service: ${B}$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)${N}"
  else
    echo -e "  Service: ${D}not installed${N}"
  fi

  # Show what the kernel actually has listening (env can lie if service not restarted).
  local listen_lines=""
  if command -v ss >/dev/null 2>&1; then
    listen_lines="$(ss -ltnp 2>/dev/null | grep -E ":${port}\\b" || true)"
  fi
  if [[ -z "$listen_lines" ]]; then
    listen_lines="$(python3 - <<PY 2>/dev/null || true
port=${port}
try:
    with open("/proc/net/tcp") as f:
        next(f)
        for line in f:
            parts = line.split()
            lip, lport = parts[1].split(":")
            if int(lport, 16) != port:
                continue
            ipn = int(lip, 16)
            a, b, c, d = ipn & 255, (ipn >> 8) & 255, (ipn >> 16) & 255, (ipn >> 24) & 255
            print(f"{a}.{b}.{c}.{d}:{port}")
except Exception:
    pass
PY
)"
  fi
  if [[ -n "$listen_lines" ]]; then
    echo -e "  Listen:  ${B}$(echo "$listen_lines" | tr '\n' ' ' | head -c 200)${N}"
    if echo "$listen_lines" | grep -qE '127\.0\.0\.1|:1:|::1'       && ! echo "$listen_lines" | grep -qE '0\.0\.0\.0|\*:|\[::\]'; then
      warn "Process is listening on loopback only — restart after WEB_HOST=0.0.0.0"
    fi
  else
    warn "Nothing listening on :${port} — start/restart the service"
  fi

  if command -v curl >/dev/null 2>&1; then
    local health
    if ssl_is_enabled_on_disk; then
      health="$(curl -skS --max-time 3 "https://127.0.0.1:${port}/health" 2>/dev/null || true)"
    else
      health="$(curl -sS --max-time 3 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
    fi
    if [[ -n "$health" ]]; then
      ok "Local health: ${health}"
    else
      warn "Local health: unreachable on 127.0.0.1:${port}"
      warn "Logs: journalctl -u ${SERVICE_NAME} -n 80 --no-pager"
    fi
  fi
  if [[ ! -f data/setup_complete.flag ]]; then
    local setup_url
    setup_url="$(setup_wizard_url "$(panel_public_base_url)/")"
    if [[ -n "$setup_url" ]]; then
      echo -e "  Setup URL:  ${B}${setup_url}${N}"
      echo -e "  (valid 15 min — disabled after setup or login)"
    fi
  fi
  info "If browser fails but local health is OK: open Cloud Firewall TCP ${port} (Hetzner/AWS)"
  print_success "Status check"
  return 0
}

cmd_doctor() {
  # Prefer global CLI when present; fall back to venv module.
  if command -v pgclock >/dev/null 2>&1; then
    pgclock doctor "$@" || true
    return 0
  fi
  local py="$PY"
  if [[ ! -x "$py" ]]; then
    py="${SCRIPT_DIR}/.venv/bin/python"
  fi
  if [[ ! -x "$py" ]]; then
    py="${SYSTEM_PY:-python3}"
  fi
  (cd "$SCRIPT_DIR" && PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$py" -m app.cli doctor "$@") || true
}

cmd_help() {
  cat <<EOF

  PGClockBot manager

  Usage:
    bash pgclock.sh                 Interactive menu
    bash pgclock.sh install         Silent install (config via /setup wizard)
    bash pgclock.sh update          Update code + deps
    bash pgclock.sh env             Edit .env
    bash pgclock.sh web             Web panel tools
    bash pgclock.sh service         systemd controls
    bash pgclock.sh status          Quick status
    bash pgclock.sh doctor          Read-only diagnostics (OK/WARN/FAIL)
    bash pgclock.sh uninstall       Full wipe (service, data, local DB, LE cert)
    bash pgclock.sh help            This help

  Global CLI (after install):
    pgclock status|start|stop|restart|logs|health
    pgclock backup|restore|migrate|doctor [--json]
    sudo bash scripts/install_global_cli.sh

  One-liner (clone OR update existing folder, then menu):
    bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/main/get.sh)

  Inside the project:
    bash get.sh
    bash pgclock.sh

EOF
}

# ── UI ──────────────────────────────────────────────────
banner() {
  {
    printf '%s\n' "$C"
    cat <<'ART'
   ==========================================
            P G C l o c k B o t
        PasarGuard Telegram Shop CLI
   ==========================================
ART
    printf '%s\n' "$N"
    printf '  %sRelease v%s%s\n' "$D" "$(read_app_version)" "$N"
    echo ""
  } > /dev/tty
}

banner_small() {
  {
    echo ""
    printf '%s==========================================%s\n' "$C" "$N"
    if [[ -n "${1:-}" ]]; then
      printf '%s  PGClockBot · %s · v%s%s\n' "$B" "$1" "$(read_app_version)" "$N"
    else
      printf '%s  PGClockBot · v%s%s\n' "$B" "$(read_app_version)" "$N"
    fi
    printf '%s==========================================%s\n' "$C" "$N"
    echo ""
  } > /dev/tty
}

show_menu() {
  clear > /dev/tty 2>/dev/null || printf '\033c' > /dev/tty
  banner
  {
    printf '  %s1)%s Install        Silent install → finish in /setup wizard\n' "$B" "$N"
    printf '  %s2)%s Update         Pull latest code (keep .env)\n' "$B" "$N"
    printf '  %s3)%s Edit .env      Change tokens / panel / ports\n' "$B" "$N"
    printf '  %s4)%s Web panel      URL, password reset, health\n' "$B" "$N"
    printf '  %s5)%s Service        Status / restart / logs\n' "$B" "$N"
    printf '  %s6)%s Status         Quick health overview\n' "$B" "$N"
    printf '  %s7)%s Doctor         Read-only diagnostics (OK/WARN/FAIL)\n' "$B" "$N"
    printf '  %s8)%s Uninstall      Remove EVERYTHING (full wipe)\n' "$B" "$N"
    printf '  %s0)%s Exit\n' "$B" "$N"
    echo ""
  } > /dev/tty
}

run_menu() {
  while true; do
    show_menu
    local choice=""
    prompt_read choice "  ${B}Select option${N}: "
    case "${choice}" in
      1|install|i) cmd_install ; pause ;;
      2|update|u)  cmd_update  ; pause ;;
      3|env|edit)  cmd_edit_env ; pause ;;
      4|web)       cmd_web_panel ;;
      5|service)   cmd_service ;;
      6|status)    cmd_status ; pause ;;
      7|doctor)    cmd_doctor ; pause ;;
      8|uninstall) cmd_uninstall ; pause ;;
      0|exit|q|quit)
        echo ""
        ok "Bye."
        exit 0
        ;;
      "")
        ;;
      *)
        err "Invalid option."
        pause
        ;;
    esac
  done
}

dispatch() {
  local cmd="${1:-}"
  case "${cmd}" in
    ""|menu)       run_menu ;;
    install|i)     cmd_install ;;
    update|u)      cmd_update ;;
    env|edit|edit-env|dotenv) cmd_edit_env ;;
    web|panel|web-panel) cmd_web_panel ;;
    service|svc)   cmd_service ;;
    status|s)      cmd_status ;;
    doctor|d)      cmd_doctor ;;
    uninstall|remove) cmd_uninstall ;;
    help|-h|--help) cmd_help ;;
    *)
      err "Unknown command: ${cmd}"
      cmd_help
      exit 1
      ;;
  esac
}

dispatch "${1:-}"
