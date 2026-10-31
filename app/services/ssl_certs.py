"""Let's Encrypt SSL for panel + mini-app — issue without killing the panel."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, ROOT_DIR, get_settings
from app.util_which import which

logger = logging.getLogger(__name__)


def _install_progress(message: str) -> None:
    """Visible progress during `pgclock.sh` install (stderr → operator tty)."""
    if not (os.environ.get("PGCLOCK_SSL_INSTALL") or "").strip():
        return
    try:
        import sys

        print(f"  [ssl] {message}", file=sys.stderr, flush=True)
    except Exception:
        logger.debug("_install_progress: ignored failure", exc_info=True)


CERT_DIR = DATA_DIR / "certs"
WEBROOT_DIR = DATA_DIR / "acme-www"
META_PATH = CERT_DIR / "meta.json"
PROGRESS_PATH = CERT_DIR / "progress.json"
LIVE_CERT = CERT_DIR / "fullchain.pem"
LIVE_KEY = CERT_DIR / "privkey.pem"
ACME_HELPER = CERT_DIR / "acme_http80.py"

_PROGRESS_LOCK = threading.Lock()
_JOB_LOCK = threading.Lock()
_JOB_RUNNING = False

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]+(?:\.[a-z0-9-]+)+$",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$"
)


def ensure_dirs() -> None:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    WEBROOT_DIR.mkdir(parents=True, exist_ok=True)
    (WEBROOT_DIR / ".well-known" / "acme-challenge").mkdir(parents=True, exist_ok=True)


def normalize_domain(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return ""
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
    s = s.split("/", 1)[0].split(":", 1)[0].strip(".")
    return s


def is_valid_ipv4(ip: str | None) -> bool:
    return bool(_IPV4_RE.match((ip or "").strip()))


def is_valid_domain(domain: str) -> bool:
    d = normalize_domain(domain)
    if not d or d in {"localhost", "127.0.0.1"}:
        return False
    # Pure IPv4 is handled by the self-signed IP path, not Let's Encrypt domain flow.
    if is_valid_ipv4(d):
        return False
    return bool(_DOMAIN_RE.match(d))


def is_valid_tls_host(host: str | None) -> bool:
    """Host usable in PUBLIC_BASE_URL / cert SAN — FQDN or IPv4."""
    h = normalize_domain(host)
    return is_valid_domain(h) or is_valid_ipv4(h)


def certbot_bin() -> str | None:
    return which("certbot")


def certbot_available() -> bool:
    return certbot_bin() is not None


def _set_progress(
    *,
    pct: int,
    stage: str,
    message: str,
    done: bool = False,
    ok: bool | None = None,
    restarting: bool = False,
    https_url: str = "",
) -> None:
    ensure_dirs()
    payload = {
        "pct": max(0, min(100, int(pct))),
        "stage": stage,
        "message": message,
        "done": bool(done),
        "ok": ok,
        "restarting": bool(restarting),
        "https_url": https_url or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _PROGRESS_LOCK:
        PROGRESS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_progress() -> dict[str, Any]:
    if not PROGRESS_PATH.is_file():
        return {
            "pct": 0,
            "stage": "idle",
            "message": "",
            "done": True,
            "ok": None,
            "restarting": False,
            "https_url": "",
        }
    try:
        data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"pct": 0, "stage": "idle", "message": "", "done": True}
    except Exception:
        return {"pct": 0, "stage": "idle", "message": "", "done": True, "ok": None}


def read_meta() -> dict[str, Any]:
    if not META_PATH.is_file():
        return {}
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_meta(data: dict[str, Any]) -> None:
    ensure_dirs()
    META_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        META_PATH.chmod(0o600)
    except OSError:
        pass


def _clear_pending_issue(
    meta: dict[str, Any] | None = None,
    *,
    write: bool = False,
) -> dict[str, Any]:
    """Drop in-flight domain/email hints without touching the active cert identity."""
    data = meta if meta is not None else read_meta()
    if not isinstance(data, dict):
        data = {}
    had_pending = bool(data.get("pending_domain") or data.get("pending_email") or data.get("pending_miniapp_domain"))
    data.pop("pending_domain", None)
    data.pop("pending_email", None)
    data.pop("pending_miniapp_domain", None)
    # Write when caller already mutated ``meta`` (errors), or pending keys existed.
    if write and (meta is not None or had_pending):
        write_meta(data)
    return data


def cert_files_exist() -> bool:
    return LIVE_CERT.is_file() and LIVE_KEY.is_file()


def resolve_cert_paths() -> tuple[Path, Path]:
    return LIVE_CERT, LIVE_KEY


def job_running() -> bool:
    return _JOB_RUNNING


def https_is_active() -> bool:
    """True only when TLS is enabled and cert files are present on disk."""
    try:
        meta = read_meta()
        return bool(meta.get("ssl_enabled")) and cert_files_exist()
    except Exception:
        logger.exception("https_is_active failed")
        return False


def public_panel_base_url() -> str:
    """Canonical panel base URL — HTTPS when active, else HTTP+IP."""
    try:
        if https_is_active():
            meta = read_meta()
            base = (meta.get("public_https") or "").strip().rstrip("/")
            if base:
                return base
            domain = normalize_domain(meta.get("domain") or "")
            if domain:
                return public_https_url(domain).rstrip("/")
    except Exception:
        logger.exception("public_panel_base_url https branch failed")
    try:
        from app.services.setup_wizard import default_http_panel_url

        return default_http_panel_url().rstrip("/")
    except Exception:
        logger.exception("public_panel_base_url http fallback failed")
        return ""


def separate_miniapp_domain(meta: dict[str, Any] | None = None) -> str:
    """The Mini App's own domain when it differs from the panel's, else ""."""
    data = meta if meta is not None else read_meta()
    panel = normalize_domain(data.get("domain") or data.get("host") or data.get("panel_domain") or "")
    miniapp = normalize_domain(data.get("miniapp_domain") or "")
    return miniapp if miniapp and miniapp != panel and is_valid_domain(miniapp) else ""


def public_miniapp_base_url() -> str:
    """HTTPS origin of the Mini App: its own domain when set (it is on the certificate), else the panel's."""
    try:
        if https_is_active():
            own = separate_miniapp_domain()
            if own:
                return public_https_url(own).rstrip("/")
    except Exception:
        logger.exception("public_miniapp_base_url failed")
    return public_panel_base_url()


def public_https_url(domain: str | None = None) -> str:
    settings = get_settings()
    port = int(settings.web_port or 9000)
    meta = read_meta()
    d = normalize_domain(
        domain or meta.get("domain") or meta.get("host") or meta.get("panel_domain") or ""
    )
    if not d:
        return ""
    return f"https://{d}" if port == 443 else f"https://{d}:{port}"


def _run(cmd: list[str], *, timeout: int = 300) -> tuple[int, str]:
    env = {
        **os.environ,
        "DEBIAN_FRONTEND": "noninteractive",
        "PATH": (os.environ.get("PATH") or "")
        + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT_DIR),
            env=env,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as exc:
        return 1, str(exc)


def _with_sudo(cmd: list[str]) -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    sudo = which("sudo")
    if sudo:
        return [sudo, "-n", *cmd]
    return cmd


def _port_in_use(port: int = 80) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.8)
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            logger.debug("_port_in_use: ignored failure", exc_info=True)


def install_certbot() -> dict[str, Any]:
    if certbot_available():
        return {"ok": True, "path": certbot_bin()}
    apt = which("apt-get") or which("apt")
    if not apt:
        return {"ok": False, "error": "apt پیدا نشد — دستی: sudo apt install certbot"}
    _set_progress(pct=12, stage="install", message="در حال نصب certbot…", done=False)
    code, out = _run(_with_sudo([apt, "install", "-y", "certbot"]), timeout=360)
    path = certbot_bin()
    if code != 0 or not path:
        return {"ok": False, "error": (out or "نصب certbot ناموفق بود")[-1200:]}
    return {"ok": True, "path": path}


def _parse_expiry(cert_path: Path) -> tuple[str | None, bool]:
    if not cert_path.is_file():
        return None, True
    openssl = which("openssl")
    if not openssl:
        return None, False
    code, out = _run([openssl, "x509", "-in", str(cert_path), "-noout", "-enddate"], timeout=20)
    if code != 0 or "notAfter=" not in out:
        return None, False
    raw = out.split("notAfter=", 1)[-1].strip().splitlines()[0].strip()
    exp_dt = None
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y GMT"):
        try:
            exp_dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if not exp_dt:
        return raw, False
    return exp_dt.isoformat(), exp_dt <= datetime.now(timezone.utc)


def cert_status() -> dict[str, Any]:
    ensure_dirs()
    meta = read_meta()
    cert_path, key_path = resolve_cert_paths()
    has = cert_path.is_file() and key_path.is_file()
    expires_at, expired = (None, True)
    if has:
        expires_at, expired = _parse_expiry(cert_path)
    port = int(get_settings().web_port or 9000)
    domain = normalize_domain(
        meta.get("domain") or meta.get("host") or meta.get("panel_domain") or ""
    )
    enabled = bool(meta.get("ssl_enabled")) and has
    mode = (meta.get("mode") or "").strip().lower()
    if not mode:
        if meta.get("self_signed"):
            mode = "self_signed_ip"
        elif domain and enabled:
            mode = "letsencrypt"
        else:
            mode = "none"
    return {
        "has_cert": has,
        "certbot": certbot_available(),
        "certbot_path": certbot_bin(),
        "cert_path": str(cert_path.resolve()) if cert_path.is_file() else str(LIVE_CERT),
        "key_path": str(key_path.resolve()) if key_path.is_file() else str(LIVE_KEY),
        "letsencrypt_live": meta.get("letsencrypt_live") or "",
        "domain": domain,
        "email": meta.get("email") or "",
        "issued_at": meta.get("issued_at"),
        "expires_at": expires_at or meta.get("expires_at"),
        "expired": bool(expired) if has else True,
        "ssl_enabled": enabled,
        "self_signed": bool(meta.get("self_signed")),
        "mode": mode,
        "last_error": meta.get("last_error"),
        "pending_domain": normalize_domain(meta.get("pending_domain") or ""),
        "pending_email": (meta.get("pending_email") or "").strip(),
        "public_https": public_https_url(domain) if domain else "",
        "miniapp_domain": separate_miniapp_domain(meta),
        "pending_miniapp_domain": normalize_domain(meta.get("pending_miniapp_domain") or ""),
        "public_miniapp": (public_https_url(separate_miniapp_domain(meta) or domain) + "/miniapp/") if domain else "",
        "web_port": port,
        "progress": read_progress(),
        "running": job_running(),
        "port80_busy": _port_in_use(80),
    }


def _sudo_read_file(path: Path) -> bytes | None:
    cmd = _with_sudo(["cat", str(path)])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30,
            env={
                **os.environ,
                "PATH": (os.environ.get("PATH") or "")
                + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except Exception:
        return None
    return None


def _copy_live_from_letsencrypt(name: str) -> str | None:
    live = Path("/etc/letsencrypt/live") / name
    fullchain = live / "fullchain.pem"
    privkey = live / "privkey.pem"

    def _exists(p: Path) -> bool:
        try:
            return p.exists()
        except PermissionError:
            return True

    if not _exists(fullchain) or not _exists(privkey):
        return None
    ensure_dirs()
    try:
        shutil.copy2(fullchain, LIVE_CERT)
        shutil.copy2(privkey, LIVE_KEY)
    except OSError:
        cert_bytes = _sudo_read_file(fullchain)
        key_bytes = _sudo_read_file(privkey)
        if not cert_bytes or not key_bytes:
            return None
        LIVE_CERT.write_bytes(cert_bytes)
        LIVE_KEY.write_bytes(key_bytes)
    try:
        os.chmod(LIVE_KEY, 0o600)
        os.chmod(LIVE_CERT, 0o644)
    except OSError:
        pass
    if not LIVE_CERT.is_file() or not LIVE_KEY.is_file():
        return None
    return str(live)


def _lineage_names_for_domain(primary: str) -> tuple[str, ...]:
    """Exact Let's Encrypt lineage names that belong to this hostname only."""
    d = normalize_domain(primary)
    if not d:
        return ()
    return (f"pgclock-{d}", d)


def _find_and_copy_cert(primary: str) -> str | None:
    """Copy live cert for ``primary`` only — never fall back to an unrelated lineage.

    A previous "newest live dir" fallback could copy the *old* domain's cert after
    a domain switch, leaving HTTPS enabled under the new hostname with the wrong
    SAN (browser domain mismatch even when DNS was fine).
    """
    wanted = set(_lineage_names_for_domain(primary))
    if not wanted:
        return None
    for name in _lineage_names_for_domain(primary):
        found = _copy_live_from_letsencrypt(name)
        if found:
            return found
    # Permission-denied listing: still only accept exact name matches.
    live_root = Path("/etc/letsencrypt/live")
    try:
        is_dir = live_root.is_dir()
    except PermissionError:
        is_dir = True
    if not is_dir:
        return None
    try:
        children = [
            p for p in live_root.iterdir() if p.is_dir() and not p.name.startswith(".")
        ]
    except PermissionError:
        _code, out = _run(_with_sudo(["ls", "-1", str(live_root)]), timeout=20)
        children = [
            live_root / line.strip()
            for line in (out or "").splitlines()
            if line.strip()
        ]
    for child in children:
        cname = str(getattr(child, "name", "") or Path(str(child)).name)
        if cname not in wanted:
            continue
        found = _copy_live_from_letsencrypt(cname)
        if found:
            return found
    return None


def _write_acme_helper() -> Path:
    ensure_dirs()
    ACME_HELPER.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1]).resolve()\n"
        "class H(SimpleHTTPRequestHandler):\n"
        "    def __init__(self, *a, **k):\n"
        "        super().__init__(*a, directory=str(root), **k)\n"
        "    def log_message(self, *a):\n"
        "        return\n"
        "ThreadingHTTPServer(('0.0.0.0', 80), H).serve_forever()\n",
        encoding="utf-8",
    )
    try:
        ACME_HELPER.chmod(0o755)
    except OSError:
        pass
    return ACME_HELPER


def _start_acme_http() -> subprocess.Popen | None:
    """Serve WEBROOT on :80 without touching the panel process on WEB_PORT."""
    if _port_in_use(80):
        return None
    helper = _write_acme_helper()
    py = which("python3") or sys_executable()
    cmd = _with_sudo([py, str(helper), str(WEBROOT_DIR)])
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(ROOT_DIR),
        )
    except Exception as exc:
        logger.warning("ACME http80 start failed: %s", exc)
        return None
    # Wait until port 80 answers
    for _ in range(25):
        if proc.poll() is not None:
            return None
        if _port_in_use(80):
            return proc
        time.sleep(0.2)
    _stop_acme_http(proc)
    return None


def sys_executable() -> str:
    import sys

    return sys.executable


def _stop_acme_http(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        if proc.poll() is None:
            # Prefer graceful terminate; escalate if needed
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.kill(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
    except Exception:
        logger.debug("_stop_acme_http: ignored failure", exc_info=True)
    # Also kill helper by pattern if sudo spawned a child
    _run(_with_sudo(["pkill", "-f", str(ACME_HELPER)]), timeout=10)


HELPER_TOO_OLD = (
    "برنامهٔ کمکی صدور گواهی روی سرور قدیمی است و دامنهٔ جدای مینی‌اپ را نمی‌شناسد. "
    "دسترسی sudo بدون رمز را برای به‌روزرسانی آن فعال کنید، یا فعلاً دامنهٔ مینی‌اپ را همان دامنهٔ پنل بگذارید."
)


def _certbot_issue(domain: str, email: str, *, force: bool = False, also: str = "") -> tuple[bool, str]:
    """One certificate for ``domain`` (lineage ``pgclock-<domain>``), plus ``also`` when set."""
    bin_path = certbot_bin()
    if not bin_path:
        return False, "certbot پیدا نشد"

    # ECDSA P-256 — lighter/faster than RSA-2048 for ACME + TLS handshake.
    base = [
        bin_path,
        "certonly",
        "--agree-tos",
        "--non-interactive",
        "--email",
        email,
        "--cert-name",
        f"pgclock-{domain}",
        "-d",
        domain,
        *(["-d", also] if also else []),
        "--key-type",
        "ecdsa",
        "--elliptic-curve",
        "secp256r1",
    ]
    if force:
        base.append("--force-renewal")

    ensure_dirs()
    helper: subprocess.Popen | None = None
    try:
        if _port_in_use(80):
            return (
                False,
                "پورت ۸۰ اشغال است. سرویس/پروکسی روی ۸۰ را موقتاً متوقف کنید، "
                "بعد دوباره «دریافت گواهی» را بزنید. پنل روی پورت فعلی دست نخورده می‌ماند.",
            )

        _set_progress(pct=35, stage="acme_http", message="راه‌اندازی موقت ACME روی پورت ۸۰…", done=False)
        _install_progress("opening temporary ACME helper on :80…")
        helper = _start_acme_http()
        if not helper:
            _install_progress("FAILED · could not bind :80 for ACME challenge")
            return (
                False,
                "نتوانستیم پورت ۸۰ را برای تأیید دامنه باز کنیم. "
                "با کاربر root/sudo و پورت ۸۰ آزاد دوباره تلاش کنید.",
            )

        _set_progress(pct=50, stage="challenge", message="درخواست گواهی از Let's Encrypt…", done=False)
        _install_progress("ACME challenge running — waiting for Let's Encrypt…")
        # Prefer constrained ctl helper (no raw certbot sudo). Fall back only if helper missing.
        from app.services.service_control import HELPER_INSTALL_PATH, ensure_restart_helper, helper_capabilities

        ensure_restart_helper()
        ctl = HELPER_INSTALL_PATH if HELPER_INSTALL_PATH.is_file() else None
        if ctl is not None:
            # An older helper would drop the extra name silently; refuse instead.
            if also and "san" not in helper_capabilities():
                return False, HELPER_TOO_OLD
            cmd = _with_sudo(
                [
                    str(ctl),
                    "certbot-certonly",
                    domain,
                    email,
                    str(WEBROOT_DIR),
                    *(["--force"] if force else []),
                    *(["--also", also] if also else []),
                ]
            )
        else:
            cmd = _with_sudo([*base, "--webroot", "-w", str(WEBROOT_DIR)])
        code, out = _run(cmd, timeout=240)
        if code == 0:
            return True, out
        hint = _dns_failure_hint(out or "")
        return False, ((out or "certbot failed").strip()[-1800:] + hint).strip()
    finally:
        _stop_acme_http(helper)


def _dns_failure_hint(log: str) -> str:
    """Append a DNS hint only for real DNS failures — not certbot boilerplate.

    Certbot's generic failure text often mentions "DNS A/AAAA record(s)" even when
    the actual problem was HTTP-01 / port 80. Matching bare ``dns`` caused false
    "دامنه مشکل دارد" reports after domain switches.
    """
    low = (log or "").lower()
    if not low:
        return ""
    specific = (
        "nxdomain",
        "dns problem:",
        "no valid ip addresses found",
        "servfail looking up",
        "dnssec",
    )
    if any(s in low for s in specific):
        return "\nDNS دامنه باید به IP همین سرور اشاره کند."
    return ""


def issue_or_renew(
    *,
    domain: str,
    email: str,
    force: bool = False,
    enable: bool = True,
    restart: bool = True,
    miniapp_domain: str = "",
) -> dict[str, Any]:
    """Issue/renew Let's Encrypt certificate, then optionally enable HTTPS.

    ``miniapp_domain`` (optional) is put on the same certificate so the Mini App can be
    served from its own address; empty or equal to ``domain`` means the panel's domain.
    """
    ensure_dirs()
    domain = normalize_domain(domain)
    miniapp_domain = normalize_domain(miniapp_domain)
    if miniapp_domain == domain:
        miniapp_domain = ""
    email = (email or "").strip()
    prev = read_meta()
    was_enabled = bool(prev.get("ssl_enabled"))

    if not is_valid_domain(domain):
        _set_progress(pct=100, stage="error", message="دامنه نامعتبر است", done=True, ok=False)
        _clear_pending_issue(read_meta(), write=True)
        return {"ok": False, "error": "دامنه نامعتبر است (مثال: panel.example.com)"}
    if miniapp_domain and not is_valid_domain(miniapp_domain):
        _set_progress(pct=100, stage="error", message="دامنهٔ مینی‌اپ نامعتبر است", done=True, ok=False)
        _clear_pending_issue(read_meta(), write=True)
        return {"ok": False, "error": "دامنهٔ مینی‌اپ نامعتبر است (مثال: app.example.com)"}
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        _set_progress(pct=100, stage="error", message="ایمیل نامعتبر است", done=True, ok=False)
        _clear_pending_issue(read_meta(), write=True)
        return {"ok": False, "error": "ایمیل معتبر برای Let's Encrypt لازم است"}

    _set_progress(
        pct=5,
        stage="start",
        message=f"شروع دریافت گواهی برای {domain}…",
        done=False,
    )
    _install_progress(f"start issue/renew · domain={domain}")

    if not certbot_available():
        _install_progress("certbot missing — installing via apt…")
        inst = install_certbot()
        if not inst.get("ok"):
            err = str(inst.get("error") or "نصب certbot ناموفق")
            _set_progress(pct=100, stage="error", message=err[:200], done=True, ok=False)
            meta = read_meta()
            # Keep active domain/cert identity — only record the error + clear pending.
            meta["last_error"] = err
            meta["ssl_enabled"] = was_enabled
            _clear_pending_issue(meta, write=True)
            _install_progress(f"certbot install failed: {err[:200]}")
            return {"ok": False, "error": err}

    _set_progress(pct=22, stage="ready", message="certbot آماده است", done=False)
    _install_progress("certbot ready · requesting certificate (HTTP-01 on :80)…")
    ok, log = _certbot_issue(domain, email, force=force, also=miniapp_domain)
    if not ok:
        meta = read_meta()
        meta["last_error"] = log[-2000:]
        # keep previous ssl_enabled + domain/public_https as-is on failure
        meta["ssl_enabled"] = was_enabled
        _clear_pending_issue(meta, write=True)
        _set_progress(pct=100, stage="error", message=(log[-280:] or "ناموفق"), done=True, ok=False)
        return {"ok": False, "error": log[-1500:] or "صدور گواهی ناموفق بود"}

    _set_progress(pct=82, stage="copy", message="کپی گواهی به data/certs…", done=False)
    live = _find_and_copy_cert(domain)
    if not live or not cert_files_exist():
        msg = (
            "گواهی برای این دامنه صادر شد ولی فایل live متناظر پیدا/کپی نشد "
            f"(انتظار: pgclock-{domain}). دسترسی /etc/letsencrypt را بررسی کنید — "
            "گواهی دامنه قبلی دست‌نخورده ماند."
        )
        meta = read_meta()
        meta["last_error"] = msg
        meta["ssl_enabled"] = was_enabled
        _clear_pending_issue(meta, write=True)
        _set_progress(pct=100, stage="error", message=msg, done=True, ok=False)
        return {"ok": False, "error": msg}

    expires_at, _expired = _parse_expiry(LIVE_CERT)
    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "domain": domain,
        "host": domain,
        "panel_domain": domain,
        # On the certificate together with the panel domain (or the panel domain itself).
        "miniapp_domain": miniapp_domain or domain,
        "email": email,
        "issued_at": now,
        "expires_at": expires_at,
        # Critical: do NOT flip HTTPS on here — that used to restart mid-HTTP and "crash" Safari
        "ssl_enabled": was_enabled,
        "self_signed": False,
        "mode": "letsencrypt",
        "last_error": None,
        "letsencrypt_live": live,
        "log_tail": (log or "")[-800:],
        "public_https": public_https_url(domain),
        "pending_domain": None,
        "pending_email": None,
    }
    write_meta(meta)
    if not enable:
        _set_progress(pct=100, stage="done", message="گواهی آماده است", done=True, ok=True)
        return {
            "ok": True,
            "domain": domain,
            "expires_at": expires_at,
            "cert_path": str(LIVE_CERT),
            "key_path": str(LIVE_KEY),
            "public_https": public_https_url(domain),
            "auto_enabled": False,
            "mode": "letsencrypt",
        }
    _set_progress(pct=90, stage="enable", message="فعال‌سازی HTTPS…", done=False)
    en = enable_https(restart=restart)
    if not en.get("ok"):
        msg = (
            "گواهی آماده شد ولی فعال‌سازی HTTPS ناموفق بود — "
            + str(en.get("error") or "خطا")[:400]
        )
        meta = read_meta()
        meta["last_error"] = msg
        write_meta(meta)
        _set_progress(pct=100, stage="error", message=msg[:280], done=True, ok=False)
        return {
            "ok": False,
            "error": msg,
            "domain": domain,
            "cert_path": str(LIVE_CERT),
            "key_path": str(LIVE_KEY),
            "needs_manual_enable": True,
        }
    check = verify_tls_material()
    if not check.get("ok"):
        msg = str(check.get("error") or "گواهی قابل بارگذاری نیست")
        meta = read_meta()
        meta["last_error"] = msg
        write_meta(meta)
        return {
            "ok": False,
            "error": msg,
            "domain": domain,
            "cert_path": str(LIVE_CERT),
            "key_path": str(LIVE_KEY),
        }
    return {
        "ok": True,
        "domain": domain,
        "expires_at": expires_at,
        "cert_path": str(LIVE_CERT),
        "key_path": str(LIVE_KEY),
        "public_https": en.get("public_https"),
        "auto_enabled": True,
        "mode": "letsencrypt",
    }


def issue_self_signed_ip(
    ip: str | None = None,
    *,
    days: int = 90,
    enable: bool = True,
    restart: bool = False,
) -> dict[str, Any]:
    """Issue a temporary ECDSA self-signed cert with IP SAN (no ACME)."""
    ensure_dirs()
    host = normalize_domain(ip or "")
    if not is_valid_ipv4(host):
        try:
            from app.services.setup_wizard import detect_server_ip

            host = normalize_domain(detect_server_ip() or "")
        except Exception:
            host = ""
    if not is_valid_ipv4(host):
        return {"ok": False, "error": "IP سرور تشخیص داده نشد — یک IPv4 معتبر بدهید"}

    openssl = which("openssl")
    if not openssl:
        return {"ok": False, "error": "openssl پیدا نشد"}

    _set_progress(pct=20, stage="self_signed", message="صدور گواهی موقت برای IP…", done=False)
    # ECDSA P-256 — lighter/faster than RSA for self-signed install certs.
    code, out = _run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(LIVE_KEY),
            "-out",
            str(LIVE_CERT),
            "-days",
            str(max(1, int(days))),
            "-subj",
            f"/CN={host}",
            "-addext",
            f"subjectAltName=IP:{host}",
        ],
        timeout=60,
    )
    if code != 0 or not cert_files_exist():
        msg = (out or "openssl self-signed failed")[-1200:]
        meta = read_meta()
        meta.update({"last_error": msg, "domain": host, "host": host, "mode": "self_signed_ip"})
        write_meta(meta)
        _set_progress(pct=100, stage="error", message=msg[:200], done=True, ok=False)
        return {"ok": False, "error": msg}

    try:
        os.chmod(LIVE_KEY, 0o600)
        os.chmod(LIVE_CERT, 0o644)
    except OSError:
        pass

    expires_at, _expired = _parse_expiry(LIVE_CERT)
    now = datetime.now(timezone.utc).isoformat()
    url = public_https_url(host)
    meta = {
        "domain": host,
        "host": host,
        "panel_domain": host,
        "miniapp_domain": host,
        "email": "",
        "issued_at": now,
        "expires_at": expires_at,
        "ssl_enabled": False,
        "self_signed": True,
        "mode": "self_signed_ip",
        "last_error": None,
        "letsencrypt_live": "",
        "public_https": url,
    }
    write_meta(meta)
    if not enable:
        _set_progress(pct=100, stage="done", message="گواهی موقت IP آماده است", done=True, ok=True)
        return {
            "ok": True,
            "domain": host,
            "expires_at": expires_at,
            "cert_path": str(LIVE_CERT),
            "key_path": str(LIVE_KEY),
            "public_https": url,
            "self_signed": True,
            "mode": "self_signed_ip",
            "auto_enabled": False,
        }
    en = enable_https(restart=restart)
    if not en.get("ok"):
        return {
            "ok": False,
            "error": str(en.get("error") or "فعال‌سازی HTTPS ناموفق"),
            "domain": host,
            "needs_manual_enable": True,
        }
    check = verify_tls_material()
    if not check.get("ok"):
        return {
            "ok": False,
            "error": str(check.get("error") or "گواهی قابل بارگذاری نیست"),
            "domain": host,
        }
    _set_progress(
        pct=100,
        stage="done",
        message="گواهی موقت IP فعال شد",
        done=True,
        ok=True,
        https_url=str(en.get("public_https") or url),
    )
    return {
        "ok": True,
        "domain": host,
        "expires_at": expires_at,
        "cert_path": str(LIVE_CERT),
        "key_path": str(LIVE_KEY),
        "public_https": en.get("public_https") or url,
        "self_signed": True,
        "mode": "self_signed_ip",
        "auto_enabled": True,
    }


def configure_for_install(
    *,
    mode: str,
    domain: str = "",
    email: str = "",
    ip: str = "",
    web_port: int | str | None = None,
) -> dict[str, Any]:
    """Install-time SSL wiring: issue cert + set PUBLIC_BASE_URL (no panel restart)."""
    mode_n = (mode or "none").strip().lower().replace("-", "_")
    _install_progress(f"mode={mode_n}")
    if mode_n in ("", "none", "off", "http", "3"):
        ensure_dirs()
        meta = read_meta()
        meta.update(
            {
                "ssl_enabled": False,
                "self_signed": False,
                "mode": "none",
                "last_error": None,
            }
        )
        write_meta(meta)
        _install_progress("skipped (HTTP only)")
        return {"ok": True, "mode": "none", "ssl_enabled": False, "public_https": ""}

    if web_port is not None:
        try:
            port_i = int(web_port)
        except (TypeError, ValueError):
            port_i = 9000
        if 1 <= port_i <= 65535:
            try:
                from app.services.setup_wizard import update_env_keys

                update_env_keys({"WEB_PORT": port_i})
                get_settings.cache_clear()
            except Exception as exc:
                logger.warning("install WEB_PORT update failed: %s", exc)

    if mode_n in ("ip", "self_signed", "self_signed_ip", "temp_ip", "2"):
        _install_progress(f"issuing temporary self-signed cert for IP {ip or '(auto)'}…")
        result = issue_self_signed_ip(ip or None, enable=True, restart=False)
        if result.get("ok"):
            _install_progress(
                f"OK · self-signed · {result.get('public_https') or ''} · expires {result.get('expires_at') or '?'}"
            )
        else:
            _install_progress(f"FAILED · {str(result.get('error') or 'unknown')[:300]}")
        return result

    if mode_n in ("domain", "letsencrypt", "le", "1"):
        _install_progress(f"Let's Encrypt for {domain} (email={email}) — needs DNS→this server + port 80…")
        result = issue_or_renew(
            domain=domain,
            email=email,
            force=False,
            enable=True,
            restart=False,
        )
        if result.get("ok"):
            _install_progress(
                f"OK · Let's Encrypt · {result.get('public_https') or domain} · expires {result.get('expires_at') or '?'}"
            )
        else:
            _install_progress(f"FAILED · {str(result.get('error') or 'unknown')[:400]}")
        return result

    _install_progress(f"invalid mode: {mode}")
    return {"ok": False, "error": f"حالت SSL نامعتبر: {mode}"}


def enable_https(*, restart: bool = True) -> dict[str, Any]:
    """Turn on uvicorn TLS + PUBLIC_BASE_URL, then optionally restart."""
    if not cert_files_exist():
        return {"ok": False, "error": "ابتدا گواهی را دریافت کنید"}
    meta = read_meta()
    domain = normalize_domain(
        meta.get("domain") or meta.get("host") or meta.get("panel_domain") or ""
    )
    if not domain:
        return {"ok": False, "error": "دامنه/میزبان گواهی مشخص نیست"}
    if not is_valid_tls_host(domain):
        return {"ok": False, "error": "میزبان گواهی نامعتبر است"}
    url = public_https_url(domain)
    try:
        from app.services.setup_wizard import update_env_keys

        update_env_keys({"PUBLIC_BASE_URL": url})
        get_settings.cache_clear()
    except Exception as exc:
        logger.warning("PUBLIC_BASE_URL update failed: %s", exc)
    meta["ssl_enabled"] = True
    meta["domain"] = domain
    meta["host"] = domain
    meta["public_https"] = url
    meta["last_error"] = None
    if not meta.get("mode"):
        meta["mode"] = "self_signed_ip" if meta.get("self_signed") else "letsencrypt"
    write_meta(meta)
    if restart:
        _set_progress(
            pct=100,
            stage="restart",
            message="HTTPS فعال شد — ری‌استارت پنل… بعداً با آدرس HTTPS وارد شوید",
            done=True,
            ok=True,
            restarting=True,
            https_url=url,
        )
        try:
            from app.services.service_control import schedule_panel_restart

            ok = schedule_panel_restart(delay_sec=3.5, reason="ssl https enable")
            if not ok:
                return {
                    "ok": False,
                    "error": (
                        "HTTPS در تنظیمات فعال شد ولی ری‌استارت خودکار ممکن نشد. "
                        "روی سرور اجرا کنید: sudo systemctl restart pgclockbot "
                        "سپس با آدرس HTTPS وارد شوید."
                    ),
                    "public_https": url,
                    "needs_manual_restart": True,
                }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"فعال شد ولی ری‌استارت ممکن نشد: {exc}",
                "public_https": url,
                "needs_manual_restart": True,
            }
    else:
        _set_progress(
            pct=100,
            stage="done",
            message="HTTPS فعال شد",
            done=True,
            ok=True,
            restarting=False,
            https_url=url,
        )
    return {"ok": True, "public_https": url}


def disable_https(*, restart: bool = True) -> dict[str, Any]:
    meta = read_meta()
    meta["ssl_enabled"] = False
    write_meta(meta)
    try:
        from app.services.setup_wizard import default_http_panel_url, update_env_keys

        update_env_keys({"PUBLIC_BASE_URL": default_http_panel_url().rstrip("/")})
        get_settings.cache_clear()
    except Exception as exc:
        logger.warning("PUBLIC_BASE_URL http revert failed: %s", exc)
    if restart:
        try:
            from app.services.service_control import schedule_panel_restart

            ok = schedule_panel_restart(delay_sec=2.5, reason="ssl https disable")
            if not ok:
                return {
                    "ok": False,
                    "error": (
                        "HTTPS در تنظیمات خاموش شد ولی ری‌استارت خودکار ممکن نشد. "
                        "دستی: sudo systemctl restart pgclockbot"
                    ),
                    "needs_manual_restart": True,
                }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": True}


def start_issue_job(*, domain: str, email: str, force: bool = False, miniapp_domain: str = "") -> dict[str, Any]:
    """Background issue/renew — enables HTTPS and restarts when cert succeeds."""
    global _JOB_RUNNING
    with _JOB_LOCK:
        if _JOB_RUNNING:
            return {"ok": False, "error": "یک عملیات SSL در حال اجراست"}
        _JOB_RUNNING = True

    def _worker() -> None:
        global _JOB_RUNNING
        try:
            issue_or_renew(domain=domain, email=email, force=force, miniapp_domain=miniapp_domain)
        except Exception as exc:
            logger.exception("SSL job failed")
            _set_progress(pct=100, stage="error", message=str(exc)[:240], done=True, ok=False)
            meta = read_meta()
            meta["last_error"] = str(exc)
            # Keep active domain/cert; only clear the in-flight switch hint.
            _clear_pending_issue(meta, write=True)
        finally:
            with _JOB_LOCK:
                _JOB_RUNNING = False

    _set_progress(pct=3, stage="queued", message="در صف…", done=False)
    threading.Thread(target=_worker, name="pgclock-ssl", daemon=True).start()
    return {"ok": True, "started": True}


def uvicorn_ssl_kwargs() -> dict[str, str] | None:
    meta = read_meta()
    if not meta.get("ssl_enabled"):
        return None
    cert, key = resolve_cert_paths()
    if not cert.is_file() or not key.is_file():
        return None
    return {"ssl_certfile": str(cert.resolve()), "ssl_keyfile": str(key.resolve())}


def verify_tls_material() -> dict[str, Any]:
    """Confirm cert+key exist and can be loaded by the stdlib SSL stack (uvicorn)."""
    import ssl as _ssl

    if not cert_files_exist():
        return {"ok": False, "error": "cert files missing (data/certs/fullchain.pem + privkey.pem)"}
    cert, key = resolve_cert_paths()
    try:
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert.resolve()), keyfile=str(key.resolve()))
    except Exception as exc:
        return {"ok": False, "error": f"SSL load failed: {exc}"[:800]}
    meta = read_meta()
    return {
        "ok": True,
        "cert_path": str(cert.resolve()),
        "key_path": str(key.resolve()),
        "ssl_enabled": bool(meta.get("ssl_enabled")),
        "domain": normalize_domain(meta.get("domain") or meta.get("host") or ""),
        "mode": meta.get("mode") or "",
    }
