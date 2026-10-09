from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, ROOT_DIR, get_settings, normalize_pg_base_url
from app.services.web_auth import load_web_admin

SETUP_FLAG = DATA_DIR / "setup_complete.flag"
SETUP_IN_PROGRESS = DATA_DIR / "setup_in_progress.flag"
SETUP_GATE_FILE = DATA_DIR / "setup_gate.token"
SETUP_GATE_META_FILE = DATA_DIR / "setup_gate.json"
SETUP_ENTRY_FILE = DATA_DIR / "setup_entry.url"
ENV_PATH = ROOT_DIR / ".env"

SETUP_GATE_TTL_SEC = 15 * 60

# Keys the wizard may write; unknown keys in .env are preserved on merge.
WIZARD_ENV_KEYS = (
    "BOT_TOKEN",
    "BOT_USERNAME",
    "ADMIN_IDS",
    "PG_BASE_URL",
    "PG_SUBSCRIPTION_PATH",
    "PG_USERNAME",
    "PG_PASSWORD",
    "PG_API_KEY",
    "WEB_HOST",
    "WEB_PORT",
    "WEB_SECRET",
    "PUBLIC_BASE_URL",
    "CURRENCY",
    "WEBHOOK_URL",
    "WEBHOOK_PATH",
)


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def mark_setup_complete() -> Path:
    _ensure_data_dir()
    SETUP_FLAG.write_text("ok\n", encoding="utf-8")
    try:
        SETUP_FLAG.chmod(0o600)
    except OSError:
        pass
    try:
        if SETUP_IN_PROGRESS.exists():
            SETUP_IN_PROGRESS.unlink()
    except OSError:
        pass
    revoke_setup_gate()
    return SETUP_FLAG


def _gate_meta_expired(meta: dict[str, Any]) -> bool:
    try:
        return time.time() >= float(meta.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True


def _write_gate_meta(
    token: str,
    *,
    expires_at: float,
    created_at: float | None = None,
) -> dict[str, Any]:
    _ensure_data_dir()
    created = float(created_at if created_at is not None else time.time())
    payload: dict[str, Any] = {
        "token": token,
        "created_at": created,
        "expires_at": float(expires_at),
    }
    SETUP_GATE_META_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    SETUP_GATE_FILE.write_text(token + "\n", encoding="utf-8")
    try:
        SETUP_GATE_META_FILE.chmod(0o600)
        SETUP_GATE_FILE.chmod(0o600)
    except OSError:
        pass
    return payload


def _read_gate_meta() -> dict[str, Any] | None:
    if SETUP_GATE_META_FILE.is_file():
        try:
            data = json.loads(SETUP_GATE_META_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and (data.get("token") or "").strip():
                return data
        except Exception:
            pass
    if SETUP_GATE_FILE.is_file():
        token = SETUP_GATE_FILE.read_text(encoding="utf-8").strip()
        if token:
            return _write_gate_meta(
                token,
                expires_at=time.time() + SETUP_GATE_TTL_SEC,
            )
    return None


def revoke_setup_gate() -> None:
    """Invalidate one-time setup link, cookie gate, and persisted URL hint."""
    for path in (SETUP_GATE_META_FILE, SETUP_GATE_FILE, SETUP_ENTRY_FILE):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def create_setup_gate_session() -> str:
    """Fresh gate session — valid for SETUP_GATE_TTL_SEC from now."""
    token = secrets.token_urlsafe(24)
    _write_gate_meta(token, expires_at=time.time() + SETUP_GATE_TTL_SEC)
    return token


def ensure_setup_gate_token() -> str:
    """Return the active gate token, creating one only when missing or expired."""
    if is_setup_complete():
        revoke_setup_gate()
        return ""
    meta = _read_gate_meta()
    if meta and not _gate_meta_expired(meta):
        return str(meta["token"]).strip()
    revoke_setup_gate()
    return create_setup_gate_session()


def setup_gate_cookie_max_age() -> int:
    """Seconds left for setup_gate cookie (0 when expired or missing)."""
    meta = _read_gate_meta()
    if not meta or _gate_meta_expired(meta):
        return 0
    try:
        rem = int(float(meta["expires_at"]) - time.time())
    except (TypeError, ValueError):
        return 0
    return max(0, min(SETUP_GATE_TTL_SEC, rem))


def rotate_setup_gate_token() -> str:
    """Invalidate URL token after first open; cookie keeps same expiry window."""
    if is_setup_complete():
        revoke_setup_gate()
        return ""
    meta = _read_gate_meta()
    if not meta or _gate_meta_expired(meta):
        revoke_setup_gate()
        return create_setup_gate_session()
    try:
        expires_at = float(meta["expires_at"])
        created_at = float(meta.get("created_at") or time.time())
    except (TypeError, ValueError):
        revoke_setup_gate()
        return create_setup_gate_session()
    new_token = secrets.token_urlsafe(24)
    _write_gate_meta(new_token, expires_at=expires_at, created_at=created_at)
    return new_token


def setup_gate_ok(provided: str | None) -> bool:
    if is_setup_complete():
        return False
    meta = _read_gate_meta()
    if not meta or _gate_meta_expired(meta):
        revoke_setup_gate()
        return False
    expected = str(meta.get("token") or "").strip()
    got = (provided or "").strip()
    if not got or not expected:
        return False
    try:
        return secrets.compare_digest(got, expected)
    except (TypeError, ValueError):
        return False


def is_local_setup_client(host: str | None) -> bool:
    """True only for loopback — LAN/VPC peers must use the one-time gate URL.

    Private RFC1918 addresses are *not* treated as local: on cloud VPCs any
    peer could otherwise open ``/setup`` and seize the panel before finish.
    """
    from app.services.security_policy import is_loopback_ip

    ip = (host or "").strip()
    if not ip or ip == "unknown":
        return False
    return is_loopback_ip(ip)


def build_setup_entry_url(
    base_url: str | None = None,
    *,
    token: str | None = None,
) -> str:
    """One-time first-run URL (includes gate query param)."""
    base = (base_url or default_panel_base_url()).strip().rstrip("/")
    tok = (token or ensure_setup_gate_token()).strip()
    return f"{base}/?gate={tok}"


def persist_setup_entry_url(base_url: str | None = None) -> str:
    """Write chmod-0600 hint file for install scripts / operators on the server."""
    if is_setup_complete():
        revoke_setup_gate()
        return ""
    meta = _read_gate_meta()
    if meta and not _gate_meta_expired(meta):
        token = str(meta["token"]).strip()
    else:
        revoke_setup_gate()
        token = create_setup_gate_session()
    url = build_setup_entry_url(base_url, token=token)
    _ensure_data_dir()
    SETUP_ENTRY_FILE.write_text(url + "\n", encoding="utf-8")
    try:
        SETUP_ENTRY_FILE.chmod(0o600)
    except OSError:
        pass
    return url


def read_setup_entry_url() -> str | None:
    if is_setup_complete():
        return None
    meta = _read_gate_meta()
    if not meta or _gate_meta_expired(meta):
        return None
    if SETUP_ENTRY_FILE.exists():
        raw = SETUP_ENTRY_FILE.read_text(encoding="utf-8").strip()
        if raw:
            return raw
    try:
        return build_setup_entry_url(token=str(meta["token"]).strip())
    except Exception:
        return None


def begin_setup() -> None:
    """Mark wizard in progress so partial saves do not auto-complete setup."""
    _ensure_data_dir()
    if SETUP_FLAG.exists():
        return
    SETUP_IN_PROGRESS.write_text("1\n", encoding="utf-8")


def _read_env_file() -> dict[str, str]:
    """Parse KEY=VALUE pairs from .env (best-effort, preserves simple quoted values)."""
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        val = val.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        out[key] = val.replace("\r", "").strip()
    return out


def _env_get(key: str) -> str:
    data = _read_env_file()
    if key in data and data[key]:
        return data[key]
    try:
        get_settings.cache_clear()
        settings = get_settings()
        mapping = {
            "BOT_TOKEN": settings.bot_token,
            "BOT_USERNAME": settings.bot_username,
            "ADMIN_IDS": ",".join(str(i) for i in settings.admin_ids),
            "PG_BASE_URL": settings.pg_base_url,
            "PG_SUBSCRIPTION_PATH": settings.pg_subscription_path,
            "PG_USERNAME": settings.pg_username,
            "PG_PASSWORD": settings.pg_password,
            "PG_API_KEY": settings.pg_api_key,
            "WEB_HOST": settings.web_host,
            "WEB_PORT": str(settings.web_port),
            "WEB_SECRET": settings.web_secret,
            "PUBLIC_BASE_URL": settings.public_base_url,
            "CURRENCY": settings.currency,
        }
        return str(mapping.get(key, "") or "").strip()
    except Exception:
        return ""


def _has_web_password() -> bool:
    """True when a real (non-placeholder) panel password is configured."""
    from app.services.security_policy import is_placeholder_password
    from app.services.web_auth import _is_bcrypt_hash

    creds = load_web_admin()
    stored = (creds.get("password") or "").strip()
    if not stored:
        return False
    # Hashed passwords are always treated as real (placeholders are never hashed in).
    if _is_bcrypt_hash(stored):
        return True
    return not is_placeholder_password(stored)


def _has_bot_token() -> bool:
    from app.services.security_policy import is_placeholder_bot_token

    token = _env_get("BOT_TOKEN")
    return bool(token) and not is_placeholder_bot_token(token)


def _has_admin_ids() -> bool:
    raw = _env_get("ADMIN_IDS")
    if not raw:
        return False
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    for p in parts:
        try:
            int(p)
            return True
        except ValueError:
            continue
    return False


# Cache only the True outcome — once setup is done it stays done for this process.
_SETUP_COMPLETE_CACHED = False


def is_setup_complete() -> bool:
    """True when first-run wizard is done and required config exists.

    Existing installs that already have a bot token and web password are
    auto-flagged on first check so the wizard never traps them — unless a
    setup session is in progress (partial wizard saves).
    """
    global _SETUP_COMPLETE_CACHED
    if _SETUP_COMPLETE_CACHED:
        return True

    # Wizard mid-flight: do not treat partial credentials as "done"
    if SETUP_IN_PROGRESS.exists():
        return False

    has_pw = _has_web_password()
    has_token = _has_bot_token()
    has_admins = _has_admin_ids()
    ready = has_pw and has_token

    if SETUP_FLAG.exists():
        # Stale flag after wipe / incomplete scaffold → force wizard again
        if ready:
            _SETUP_COMPLETE_CACHED = True
            return True
        try:
            SETUP_FLAG.unlink()
        except OSError:
            pass
        return False

    if ready:
        # Prefer also having admin ids, but don't trap old installs without them.
        # Never auto-complete from empty/placeholder example credentials.
        if has_admins or has_pw:
            mark_setup_complete()
            _SETUP_COMPLETE_CACHED = True
            return True

    return False


def _escape_env_value(val: str) -> str:
    return (
        (val or "")
        .replace("\r", "")
        .strip()
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def update_env_keys(updates: dict[str, str | int | None]) -> Path:
    """Merge keys into .env without wiping unknown keys or comments when possible.

    Known keys are upserted; if the file is missing, a minimal file is created.
    Ensures WEB_SECRET exists (generates one if blank/missing).
    """
    cleaned: dict[str, str] = {}
    for k, v in updates.items():
        if v is None:
            continue
        cleaned[str(k)] = str(v).replace("\r", "").strip()

    if ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding="utf-8")
    else:
        text = ""

    def upsert(src: str, key: str, value: str) -> str:
        line = f'{key}="{_escape_env_value(value)}"'
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
        if pattern.search(src):
            return pattern.sub(line, src)
        if src and not src.endswith("\n"):
            src += "\n"
        return src + line + "\n"

    for key, value in cleaned.items():
        text = upsert(text, key, value)

    # Ensure WEB_SECRET is present and not a known placeholder
    placeholders = {"", "change-me", "change-this-long-random-secret"}
    merged_secret = (cleaned.get("WEB_SECRET") or _read_env_file().get("WEB_SECRET") or "").strip()
    if merged_secret in placeholders:
        text = upsert(text, "WEB_SECRET", secrets.token_hex(32))

    ENV_PATH.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass
    get_settings.cache_clear()
    return ENV_PATH


class WebSecretPersistenceError(RuntimeError):
    """Generated WEB_SECRET could not be stored; do not use a process-only key."""


def _is_usable_web_secret(raw: str | None) -> bool:
    from app.services.security_policy import PLACEHOLDER_SECRETS, is_placeholder_secret

    s = (raw or "").strip()
    if not s:
        return False
    if is_placeholder_secret(s) or s in PLACEHOLDER_SECRETS:
        return False
    return True


def _existing_valid_web_secret() -> str:
    """Return an already-configured WEB_SECRET (env, .env, or settings).

    Container/environment secrets win. A persisted .env value is also valid.
    Placeholders are ignored so they cannot become the live encryption key.
    """
    import os

    candidates: list[str] = []
    try:
        candidates.append(os.environ.get("WEB_SECRET") or "")
    except Exception:
        pass
    try:
        candidates.append(_read_env_file().get("WEB_SECRET") or "")
    except Exception:
        pass
    try:
        get_settings.cache_clear()
        candidates.append(get_settings().web_secret or "")
    except Exception:
        pass
    for raw in candidates:
        if _is_usable_web_secret(raw):
            return raw.strip()
    return ""


def ensure_web_secret() -> str:
    """Return a usable WEB_SECRET, generating and persisting one if missing.

    Externally supplied valid secrets (process environment / settings) are
    accepted even when ``.env`` is not writable. A newly generated secret must
    be persisted to ``.env``; otherwise raise rather than keeping a process-only
    encryption key. Never falls back to a known placeholder.
    """
    import logging

    log = logging.getLogger(__name__)
    existing = _existing_valid_web_secret()
    if existing:
        return existing
    secret = secrets.token_hex(32)
    try:
        update_env_keys({"WEB_SECRET": secret})
    except Exception:
        log.exception("persisting WEB_SECRET failed")
        raise WebSecretPersistenceError(
            "WEB_SECRET is missing and could not be persisted. "
            "Set WEB_SECRET in the environment or make .env writable."
        ) from None
    persisted = _existing_valid_web_secret()
    if persisted:
        return persisted
    raise WebSecretPersistenceError(
        "WEB_SECRET is missing and could not be persisted. "
        "Set WEB_SECRET in the environment or make .env writable."
    )


def normalize_webhook_path(raw: str | None) -> str:
    """Ensure webhook path starts with / and has no trailing slash (except root)."""
    path = (raw or "").strip() or "/telegram/webhook"
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/telegram/webhook"


def normalize_webhook_base_url(raw: str | None) -> str:
    """Strip whitespace and trailing slash from a public webhook base URL."""
    return (raw or "").strip().rstrip("/")


def telegram_webhook_endpoint(
    webhook_url: str | None,
    webhook_path: str | None = None,
) -> tuple[str, str, str]:
    """Return ``(origin, path, full_url)`` for Telegram ``setWebhook``.

    ``WEBHOOK_URL`` must be an https origin. If a path sneaks into the base
    (common when pasting the full endpoint), it is stripped so Telegram does
    not POST to ``…/telegram/webhook/telegram/webhook`` while the app listens
    on ``WEBHOOK_PATH`` only.
    """
    from urllib.parse import urlparse

    path = normalize_webhook_path(webhook_path)
    base = normalize_webhook_base_url(webhook_url)
    if not base:
        return "", path, ""
    parsed = urlparse(base)
    if parsed.scheme and parsed.netloc and parsed.path not in {"", "/"}:
        base = f"{parsed.scheme}://{parsed.netloc}"
    return base, path, f"{base}{path}"


def resolve_bot_update_mode(
    *,
    mode: str | None,
    webhook_url: str | None,
    webhook_path: str | None = None,
    public_base_url: str | None = None,
) -> tuple[str, str, str]:
    """Return (mode, webhook_base_url, webhook_path).

    mode is ``polling`` or ``webhook``. For polling, webhook_base_url is empty.
    """
    path = normalize_webhook_path(webhook_path)
    requested = (mode or "").strip().lower()
    base = normalize_webhook_base_url(webhook_url)
    if not base:
        base = normalize_webhook_base_url(public_base_url)

    if requested == "webhook" or (not requested and base):
        if not base:
            raise ValueError("برای حالت Webhook آدرس HTTPS عمومی لازم است")
        if not base.lower().startswith("https://"):
            raise ValueError("آدرس Webhook باید با https:// شروع شود")
        return "webhook", base, path
    return "polling", "", path


def current_setup_values() -> dict[str, str]:
    """Values for pre-filling the wizard form."""
    creds = load_web_admin()
    # Only prefill username when credentials already exist (re-running wizard).
    # Never force-write "admin" into an empty first-time form.
    has_creds = bool(creds.get("password"))
    webhook_url = normalize_webhook_base_url(_env_get("WEBHOOK_URL"))
    return {
        "username": (creds.get("username") or "") if has_creds else "",
        "BOT_TOKEN": _env_get("BOT_TOKEN"),
        "BOT_USERNAME": _env_get("BOT_USERNAME"),
        "ADMIN_IDS": _env_get("ADMIN_IDS"),
        "PG_BASE_URL": _env_get("PG_BASE_URL"),
        "PG_SUBSCRIPTION_PATH": _env_get("PG_SUBSCRIPTION_PATH") or "/sub",
        "PG_USERNAME": _env_get("PG_USERNAME"),
        "PG_PASSWORD": _env_get("PG_PASSWORD"),
        "PG_API_KEY": _env_get("PG_API_KEY"),
        "WEB_PORT": _env_get("WEB_PORT") or "9000",
        "PUBLIC_BASE_URL": _env_get("PUBLIC_BASE_URL"),
        "CURRENCY": _env_get("CURRENCY") or "تومان",
        "WEBHOOK_URL": webhook_url,
        "WEBHOOK_PATH": normalize_webhook_path(_env_get("WEBHOOK_PATH") or "/telegram/webhook"),
        "BOT_UPDATE_MODE": "webhook" if webhook_url else "polling",
    }


def parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def detect_server_ip() -> str:
    """Best-effort public/LAN IP for panel links shown to resellers."""
    import socket
    import urllib.request

    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                ip = (resp.read() or b"").decode("utf-8", errors="ignore").strip()
            if ip and " " not in ip and len(ip) < 64 and not ip.lower().startswith("<"):
                return ip
        except Exception:
            continue
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        # Outbound UDP trick — no packets sent; reveals preferred local IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip:
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def default_panel_base_url(*, public_base: str | None = None, web_port: int | str | None = None) -> str:
    """Panel base for links — HTTPS only when SSL is active; otherwise HTTP+IP."""
    try:
        from app.services.ssl_certs import https_is_active, public_panel_base_url

        if https_is_active():
            if public_base is not None and str(public_base).strip():
                return str(public_base).strip().rstrip("/")
            return public_panel_base_url()
    except Exception:
        pass
    return default_http_panel_url(web_port=web_port)


def default_http_panel_url(*, web_port: int | str | None = None) -> str:
    """Panel base URL over HTTP using detected server IP (ignores PUBLIC_BASE_URL)."""
    from app.config import get_settings

    settings = get_settings()
    port = web_port if web_port is not None else settings.web_port
    try:
        port_s = str(int(port or 9000))
    except (TypeError, ValueError):
        port_s = "9000"
    try:
        ip = (detect_server_ip() or "").strip() or "127.0.0.1"
    except Exception:
        ip = "127.0.0.1"
    return f"http://{ip}:{port_s}"


def setup_finish_login_url() -> str:
    """Login URL after wizard — HTTPS when TLS is already live, else HTTP+IP."""
    try:
        from app.services.ssl_certs import https_is_active, public_panel_base_url

        if https_is_active():
            base = (public_panel_base_url() or "").strip().rstrip("/")
            if base.lower().startswith("https://"):
                return base + "/login?restarting=1"
    except Exception:
        pass
    return default_http_panel_url().rstrip("/") + "/login?restarting=1"


def force_http_login_url(url: str) -> str:
    """Normalize wizard finish target to a login URL.

    When HTTPS is already active (install-time SSL), keep/use https.
    Otherwise coerce to http so we never send the operator to a dead HTTPS
    PUBLIC_BASE_URL before a certificate exists.
    """
    try:
        from app.services.ssl_certs import https_is_active

        tls_live = bool(https_is_active())
    except Exception:
        tls_live = False

    if tls_live:
        return setup_finish_login_url()

    raw = (url or "").strip()
    if not raw:
        return setup_finish_login_url()
    try:
        from urllib.parse import urlparse, urlunparse

        p = urlparse(raw)
        if p.scheme.lower() == "https":
            p = p._replace(scheme="http")
        elif not p.scheme:
            return setup_finish_login_url()
        if p.scheme.lower() != "http":
            return setup_finish_login_url()
        path = p.path or "/login"
        if not path.rstrip("/").endswith("/login"):
            path = path.rstrip("/") + "/login"
        q = p.query
        if "restarting=" not in q:
            q = (q + "&" if q else "") + "restarting=1"
        return urlunparse((p.scheme, p.netloc, path, "", q, ""))
    except Exception:
        return setup_finish_login_url()


def panel_url_hint(public_base: str = "", web_port: str = "9000") -> str:
    base = default_panel_base_url(public_base=public_base, web_port=web_port)
    return base.rstrip("/") + "/"


def wizard_panel_url_hint(web_port: str = "9000") -> str:
    """Panel URL shown during setup — HTTPS when TLS is live, else HTTP+IP."""
    try:
        from app.services.ssl_certs import https_is_active, public_panel_base_url

        if https_is_active():
            base = (public_panel_base_url() or "").strip().rstrip("/")
            if base.lower().startswith("https://"):
                return base + "/"
    except Exception:
        pass
    return default_http_panel_url(web_port=web_port).rstrip("/") + "/"


async def setup_pg_access_audit() -> dict[str, Any]:
    """Live PG ACL report for wizard step 4. Display only — never an authz source."""
    from app.services.pg_access import public_pg_access_audit, resolve_platform_pg_capabilities

    values = current_setup_values()
    uname = (values.get("PG_USERNAME") or "").strip()
    pwd = (values.get("PG_PASSWORD") or "").strip()
    api_key = (values.get("PG_API_KEY") or "").strip()
    base = normalize_pg_base_url(values.get("PG_BASE_URL") or "")
    if not uname or not (pwd or api_key) or not base:
        return public_pg_access_audit(
            {
                "ok": False,
                "error": "اعتبارنامه پاسارگارد ناقص است (رمز یا کلید API)",
                "username": uname or None,
            }
        )
    try:
        caps = await resolve_platform_pg_capabilities(
            username=uname,
            password=pwd or None,
            api_key=api_key or None,
            base_url=base,
            use_cache=False,
        )
    except Exception:
        return public_pg_access_audit(
            {"ok": False, "error": "بررسی دسترسی پاسارگارد ناموفق بود", "username": uname}
        )
    return public_pg_access_audit(caps)
