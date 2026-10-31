"""pgclock doctor — read-only install / service / security diagnostics.

Exit code: 0 when no FAIL checks; non-zero when any FAIL is present.
Supports ``--json`` via CliContext.json_mode.
"""

from __future__ import annotations

import json
import shutil
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.cli.context import (
    CliContext,
    SERVICE_NAME,
    database_url,
    env_get,
    service_unit_installed,
    systemctl,
    web_port,
)
from app.cli.install_root import MARKER_PATH
from app.cli.output import header, ok, warn
from app.cli.service_cmds import _probe_health
import logging

logger = logging.getLogger(__name__)

Check = dict[str, Any]


def _loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in {"", "127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _port_listening(port: int) -> bool:
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.4)
                if s.connect_ex((address, int(port))) == 0:
                    return True
        except OSError:
            continue
    return False


def _probe_scheme(scheme: str, port: int, path: str = "/health") -> dict[str, Any]:
    url = f"{scheme}://127.0.0.1:{int(port)}{path}"
    try:
        ctx = ssl._create_unverified_context() if scheme == "https" else None
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2.5, context=ctx) as resp:
            return {"ok": True, "status": int(getattr(resp, "status", 200) or 200), "url": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "url": url}


def _cert_expiry_days(port: int) -> int | None:
    try:
        pem = ssl.get_server_certificate(("127.0.0.1", int(port)), timeout=2.5)
        cert = ssl.PEM_cert_to_DER_cert(pem)  # type: ignore[attr-defined]
        # Prefer cryptography-free parse via ssl._ssl if available — fall back to openssl file.
        _ = cert
    except Exception:
        logger.debug("_cert_expiry_days: ignored failure", exc_info=True)
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", int(port)), timeout=2.5) as sock:
            with context.wrap_socket(sock, server_hostname="127.0.0.1") as ssock:
                der = ssock.getpeercert(binary_form=True)
                if not der:
                    return None
                # Parse notAfter via ssl cert dict when verify would populate — binary fallback:
                from datetime import datetime as dt

                try:
                    import OpenSSL  # type: ignore

                    x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_ASN1, der)
                    na = x509.get_notAfter().decode("ascii")
                    exp = dt.strptime(na, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc)
                    return int((exp - datetime.now(timezone.utc)).total_seconds() // 86400)
                except Exception:
                    return None
    except Exception:
        return None


def _owner_identity_status() -> tuple[str, str, str]:
    """Compare the panel Web Owner login with the Telegram ADMIN_IDS owner list.

    The two are independent identity stores; either one missing locks an owner
    out of one surface, so the doctor reports which side is unset.
    """
    from app.config import get_settings
    from app.services.web_auth import load_web_admin

    has_web_password = bool((load_web_admin().get("password") or "").strip())
    has_admin_ids = bool(get_settings().admin_ids)
    if has_web_password and has_admin_ids:
        return "OK", "Web Owner and ADMIN_IDS are both configured", ""
    if has_web_password:
        return "WARN", "Web Owner is configured but ADMIN_IDS is empty", "set ADMIN_IDS so Telegram owner tools work"
    if has_admin_ids:
        return "WARN", "ADMIN_IDS is set but the Web Owner password is empty", "finish the setup wizard to set the panel login"
    return "WARN", "neither the Web Owner password nor ADMIN_IDS is configured yet", "run the setup wizard"


def cmd_doctor(ctx: CliContext) -> int:
    checks: list[Check] = []

    def add(name: str, status: str, detail: str, *, fix: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})
        if ctx.json_mode:
            return
        if status == "OK":
            ok(f"{name}: {detail}")
        elif status == "WARN":
            warn(f"{name}: {detail}" + (f" — fix: {fix}" if fix else ""))
        else:
            warn(f"FAIL {name}: {detail}" + (f" — fix: {fix}" if fix else ""))

    if not ctx.json_mode:
        header("doctor")

    # .env keys
    env_path = ctx.env_path
    if env_path.is_file():
        try:
            mode = oct(env_path.stat().st_mode)[-3:]
            if mode not in {"600", "640"}:
                add(".env", "WARN", f"present mode={mode}", fix="chmod 600 .env")
            else:
                add(".env", "OK", f"present mode={mode}")
        except OSError:
            add(".env", "OK", "present")
    else:
        add(".env", "FAIL", "missing", fix="run install / setup wizard")

    required_keys = ("DATABASE_URL", "WEB_SECRET", "WEB_PORT")
    for key in required_keys:
        val = env_get(ctx.root, key, "")
        if not val:
            add(f"env:{key}", "FAIL", "missing", fix=f"set {key} in .env")
        elif key == "WEB_SECRET" and val in {"change-me", "changeme"}:
            add(f"env:{key}", "FAIL", "placeholder", fix="set a strong WEB_SECRET")
        else:
            add(f"env:{key}", "OK", "set")

    try:
        status, detail, fix = _owner_identity_status()
        add("owner_identity", status, detail, fix=fix)
    except Exception as exc:  # diagnostics must not abort the rest of the doctor run
        add("owner_identity", "WARN", f"check skipped: {exc}"[:160])

    bot_token = env_get(ctx.root, "BOT_TOKEN", "")
    if not bot_token:
        add("env:BOT_TOKEN", "WARN", "missing", fix="set BOT_TOKEN (never printed)")
    else:
        add("env:BOT_TOKEN", "OK", "set (hidden)")

    web_host = env_get(ctx.root, "WEB_HOST", "0.0.0.0")
    if _loopback_host(web_host) and web_host.strip() not in {"0.0.0.0", ""}:
        add(
            "WEB_HOST",
            "WARN",
            f"loopback ({web_host})",
            fix="use 0.0.0.0 or a public bind address for remote panel access",
        )
    else:
        add("WEB_HOST", "OK", web_host or "0.0.0.0")

    port = web_port(ctx.root)
    if _port_listening(port):
        add("port", "OK", f"listening on {port}")
    else:
        add("port", "FAIL", f"nothing listening on {port}", fix="pgclock start / check firewall")

    http_p = _probe_scheme("http", port)
    https_p = _probe_scheme("https", port)
    public_base = (env_get(ctx.root, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    setup_scheme = "https" if public_base.lower().startswith("https://") else "http"
    if http_p.get("ok"):
        add("probe:http", "OK", f"127.0.0.1:{port}/health")
    else:
        add("probe:http", "WARN", str(http_p.get("error") or "unreachable"))
    if https_p.get("ok"):
        add("probe:https", "OK", f"127.0.0.1:{port}/health")
    else:
        add("probe:https", "WARN", str(https_p.get("error") or "unreachable"))

    if setup_scheme == "https" and not https_p.get("ok") and http_p.get("ok"):
        add(
            "setup_scheme",
            "WARN",
            "PUBLIC_BASE_URL is https but local https probe failed",
            fix="enable SSL or align PUBLIC_BASE_URL with listening scheme",
        )
    elif setup_scheme == "http" and https_p.get("ok") and not http_p.get("ok"):
        add(
            "setup_scheme",
            "WARN",
            "only https responds but PUBLIC_BASE_URL is http",
            fix="set PUBLIC_BASE_URL to https://…",
        )
    else:
        add("setup_scheme", "OK", f"expected {setup_scheme}")

    if https_p.get("ok"):
        days = _cert_expiry_days(port)
        if days is None:
            add("cert", "WARN", "could not read expiry", fix="check data/certs")
        elif days < 0:
            add("cert", "FAIL", "expired", fix="renew Let's Encrypt / re-issue cert")
        elif days < 14:
            add("cert", "WARN", f"expires in {days}d", fix="renew certificate soon")
        else:
            add("cert", "OK", f"expires in {days}d")
    else:
        add("cert", "OK", "n/a (https not serving)")

    # Database + alembic
    url = database_url(ctx.root)
    try:
        from app.db.engine_url import parse_engine, to_sync_url
        from sqlalchemy import create_engine, text

        info_eng = parse_engine(url)
        eng = create_engine(to_sync_url(url))
        try:
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            add("database", "OK", info_eng.dialect)
        finally:
            eng.dispose()
        from app.db.alembic_runner import current_revision, heads

        rev = current_revision(url)
        head_list: list[str] = []
        try:
            head_list = list(heads() or [])
        except Exception:
            head_list = []
        if not rev:
            add("alembic", "FAIL", "empty revision", fix="pgclock migrate")
        elif head_list and str(rev) not in {str(h) for h in head_list}:
            add(
                "alembic",
                "WARN",
                f"at {rev}, heads={','.join(str(h) for h in head_list)}",
                fix="pgclock migrate",
            )
        else:
            add("alembic", "OK", f"revision {rev}")
    except Exception as e:
        add("database", "FAIL", str(e)[:200], fix="check DATABASE_URL / Postgres")

    # Bot getMe — never print token
    if bot_token:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/getMe", method="GET"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            if body.get("ok"):
                uname = ((body.get("result") or {}).get("username")) or "?"
                add("bot_getMe", "OK", f"@{uname}")
            else:
                add("bot_getMe", "FAIL", "telegram rejected token", fix="rotate BOT_TOKEN")
        except Exception as e:
            add("bot_getMe", "FAIL", str(e)[:160], fix="check BOT_TOKEN / network")
    else:
        add("bot_getMe", "WARN", "skipped — no BOT_TOKEN")

    # PasarGuard
    pg_url = env_get(ctx.root, "PASARGUARD_BASE_URL", "") or env_get(
        ctx.root, "PG_BASE_URL", ""
    )
    if pg_url:
        try:
            req = urllib.request.Request(pg_url.rstrip("/") + "/", method="GET")
            with urllib.request.urlopen(req, timeout=4) as resp:
                add("pasarguard", "OK", f"reachable status={getattr(resp, 'status', '?')}")
        except Exception as e:
            add("pasarguard", "WARN", str(e)[:160], fix="check PASARGUARD_BASE_URL")
    else:
        add("pasarguard", "WARN", "PASARGUARD_BASE_URL unset")

    # Disk
    try:
        usage = shutil.disk_usage(ctx.root)
        free_gb = usage.free / (1024**3)
        if free_gb < 1:
            add("disk", "FAIL", f"{free_gb:.1f} GiB free", fix="free disk space")
        elif free_gb < 3:
            add("disk", "WARN", f"{free_gb:.1f} GiB free")
        else:
            add("disk", "OK", f"{free_gb:.1f} GiB free")
    except OSError as e:
        add("disk", "WARN", str(e)[:120])

    if MARKER_PATH.is_file():
        add("global_marker", "OK", str(MARKER_PATH))
    else:
        add("global_marker", "WARN", "missing", fix="scripts/install_global_cli.sh")

    if service_unit_installed():
        st = systemctl("is-active", SERVICE_NAME)
        state = (st.stdout or "").strip() or "unknown"
        if state == "active":
            add("systemd", "OK", f"{SERVICE_NAME} active")
        else:
            add("systemd", "WARN", f"state={state}", fix="pgclock start")
    else:
        add("systemd", "WARN", "unit not installed")

    health = _probe_health(port)
    if health.get("ok"):
        add("health", "OK", f"/health on :{port}")
    else:
        add("health", "WARN", str(health.get("error") or "unreachable"))

    fails = sum(1 for c in checks if c["status"] == "FAIL")
    warns = sum(1 for c in checks if c["status"] == "WARN")

    if ctx.json_mode:
        print(
            json.dumps(
                {
                    "ok": fails == 0,
                    "fail": fails,
                    "warn": warns,
                    "checks": checks,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        header("doctor summary")
        if fails == 0:
            ok(f"no FAIL ({warns} warn)")
        else:
            warn(f"{fails} FAIL, {warns} WARN")

    return 1 if fails else 0
