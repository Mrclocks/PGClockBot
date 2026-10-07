"""Shared security helpers: placeholders, CSRF/origin checks, request limits."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

# Documented / example values that must never complete setup or become live creds.
PLACEHOLDER_BOT_TOKENS = frozenset(
    {
        "123456:ABC-DEF",
        "0000000000:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        "YOUR_BOT_TOKEN",
        "change-me",
    }
)
PLACEHOLDER_PASSWORDS = frozenset(
    {
        "Admin!234",
        "admin",
        "password",
        "Password1!",
        "change-me",
        "changeme",
        "12345678",
        "YourPassword",
    }
)
PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "change-me",
        "change-this-long-random-secret",
        "secret",
        "pgclock-secret",
    }
)

# Max JSON body for Telegram webhook (Telegram updates are small).
WEBHOOK_MAX_BODY_BYTES = 256 * 1024
# Soft ceiling for unauthenticated form posts (login/setup) — DoS guard.
PUBLIC_FORM_MAX_BODY_BYTES = 256 * 1024
# Card-auto settlement webhooks — HMAC JSON, intentionally small.
CARD_WEBHOOK_MAX_BODY_BYTES = 64 * 1024
# Mini App JSON POSTs (Phase 4: documented; enforced when wired).
MINIAPP_MAX_BODY_BYTES = 256 * 1024


def is_placeholder_bot_token(token: str | None) -> bool:
    t = (token or "").strip()
    if not t:
        return True
    if t in PLACEHOLDER_BOT_TOKENS:
        return True
    # Telegram tokens look like <digits>:<secret>
    if ":" not in t:
        return True
    left, _, right = t.partition(":")
    if not left.isdigit() or len(right) < 20:
        return True
    if "XXX" in t.upper() or "YOUR_" in t.upper():
        return True
    return False


def is_placeholder_password(password: str | None) -> bool:
    p = (password or "").strip()
    if not p:
        return True
    return p in PLACEHOLDER_PASSWORDS


def is_placeholder_secret(secret: str | None) -> bool:
    return (secret or "").strip() in PLACEHOLDER_SECRETS


def request_host_allowed(request_host: str | None, origin_or_referer: str | None) -> bool:
    """True when Origin/Referer host matches the request Host (CSRF defense-in-depth)."""
    if not origin_or_referer:
        return False
    try:
        parsed = urlparse(origin_or_referer)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    src = (parsed.netloc or "").strip().lower()
    dst = (request_host or "").strip().lower()
    if not src or not dst:
        return False
    # Strip default ports for comparison
    for port in (":80", ":443"):
        if src.endswith(port) and parsed.scheme == ("http" if port == ":80" else "https"):
            src = src[: -len(port)]
        if dst.endswith(port):
            dst = dst[: -len(port)]
    return src == dst


def content_length_ok(content_length: str | None, limit: int) -> bool:
    if content_length is None or content_length == "":
        return True  # chunked / unknown — route-level caps still apply
    try:
        return int(content_length) <= limit
    except (TypeError, ValueError):
        return False


def is_public_ip(host: str) -> bool:
    """Best-effort: False for loopback/link-local/private/reserved. Hostnames → True."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def pg_url_has_userinfo(url: str) -> bool:
    try:
        return bool(urlparse(url).username or urlparse(url).password)
    except Exception:
        return False

# Hostnames that must never be used as PasarGuard targets (SSRF / metadata).
_BLOCKED_PG_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.google.com",
        "metadata",
        "instance-data",
        "kubernetes.default",
        "kubernetes.default.svc",
    }
)
_BLOCKED_PG_HOSTNAME_SUFFIXES = (
    ".metadata.google.internal",
)


def is_loopback_ip(host: str | None) -> bool:
    """True only for IPv4/IPv6 loopback (127.0.0.0/8, ::1)."""
    raw = (host or "").strip()
    if not raw:
        return False
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return raw.lower() in {"localhost", "localhost.localdomain"}


# Phase 3 — default trusted reverse-proxy peers when TRUSTED_PROXIES is empty.
# Matches the common "nginx on the same host" layout. Docker / remote proxies
# must set TRUSTED_PROXIES explicitly (e.g. 172.16.0.0/12,10.0.0.0/8).
_DEFAULT_TRUSTED_PROXY_CIDRS: tuple[str, ...] = ("127.0.0.0/8", "::1/128")


def parse_trusted_proxy_cidrs(raw: str | None) -> list:
    """Parse comma/space-separated CIDRs or single IPs into network objects.

    Empty / blank → loopback-only defaults (safe for same-host nginx).
    Invalid tokens are skipped (fail closed for that token).
    """
    text = (raw or "").strip()
    tokens: list[str]
    if not text:
        tokens = list(_DEFAULT_TRUSTED_PROXY_CIDRS)
    else:
        tokens = [t.strip() for t in text.replace(";", ",").split(",") if t.strip()]
    out: list = []
    for tok in tokens:
        try:
            if "/" in tok:
                out.append(ipaddress.ip_network(tok, strict=False))
            else:
                ip = ipaddress.ip_address(tok)
                out.append(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}", strict=False))
        except ValueError:
            continue
    return out or [
        ipaddress.ip_network(c, strict=False) for c in _DEFAULT_TRUSTED_PROXY_CIDRS
    ]


def peer_is_trusted_proxy(
    peer_host: str | None,
    *,
    trusted_proxies_raw: str | None = None,
) -> bool:
    """True when the TCP peer may set X-Forwarded-* for this app."""
    raw = (peer_host or "").strip()
    if not raw or raw == "unknown":
        return False
    if raw.lower() in {"localhost", "localhost.localdomain"}:
        raw = "127.0.0.1"
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    # Normalize IPv4-mapped IPv6 (::ffff:x.x.x.x) for CIDR matching.
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped  # type: ignore[assignment]
    for net in parse_trusted_proxy_cidrs(trusted_proxies_raw):
        try:
            if ip in net:
                return True
        except TypeError:
            continue
    return False


def _hostname_blocked_for_pg(hostname: str) -> bool:
    h = (hostname or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h in _BLOCKED_PG_HOSTNAMES:
        return True
    return any(h.endswith(suf) for suf in _BLOCKED_PG_HOSTNAME_SUFFIXES)


def _ip_unsafe_for_pg(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject link-local / metadata / multicast / unspecified. Allow loopback + RFC1918."""
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return True
    # IPv4 link-local already covered; explicitly block AWS/GCP/Azure metadata IP.
    if ip.version == 4 and str(ip) == "169.254.169.254":
        return True
    return False


class UnsafePgUrlError(ValueError):
    """Raised when a PasarGuard base URL is not safe to dial (SSRF guard)."""


def assert_safe_pg_base_url(
    raw: str,
    *,
    resolve_dns: bool = True,
    allow_private: bool = True,
) -> str:
    """Normalize and reject SSRF-prone PasarGuard URLs.

    Allows loopback and RFC1918 by default (common same-host / LAN panels).
    Always rejects link-local/metadata hosts, userinfo, and non-http(s) schemes.
    Set ``allow_private=False`` (or env ``PG_URL_PUBLIC_ONLY=1``) to require a
    public IP / hostname that resolves only to public addresses.
    """
    import os
    import socket
    from app.config import normalize_pg_base_url

    if os.environ.get("PG_URL_PUBLIC_ONLY", "").strip() in {"1", "true", "yes"}:
        allow_private = False

    url = normalize_pg_base_url((raw or "").strip())
    if not url:
        raise UnsafePgUrlError("آدرس پاسارگارد خالی است")
    if pg_url_has_userinfo(raw) or pg_url_has_userinfo(url):
        raise UnsafePgUrlError("آدرس پاسارگارد نباید شامل نام کاربری/رمز در URL باشد")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafePgUrlError("آدرس پاسارگارد باید http یا https باشد")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise UnsafePgUrlError("آدرس پاسارگارد نامعتبر است")
    if _hostname_blocked_for_pg(host):
        # localhost is blocked as hostname label; loopback IPs still allowed below
        if host not in {"localhost", "localhost.localdomain"}:
            raise UnsafePgUrlError("هدف پاسارگارد مجاز نیست (metadata/blocked host)")
        if not allow_private:
            raise UnsafePgUrlError("آدرس localhost در حالت فقط-عمومی مجاز نیست")
        # Map localhost → loopback for dialing policy
        host = "127.0.0.1"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _ip_unsafe_for_pg(ip):
            raise UnsafePgUrlError("آدرس IP پاسارگارد برای اتصال مجاز نیست")
        if not allow_private and (ip.is_private or ip.is_loopback):
            raise UnsafePgUrlError("در حالت فقط-عمومی، IP خصوصی/لوکال مجاز نیست")
        return url

    if not resolve_dns:
        return url

    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafePgUrlError("نام میزبان پاسارگارد قابل resolve نیست") from exc

    if not infos:
        raise UnsafePgUrlError("نام میزبان پاسارگارد قابل resolve نیست")

    saw_public = False
    for info in infos:
        addr = info[4][0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_unsafe_for_pg(resolved):
            raise UnsafePgUrlError("میزبان پاسارگارد به IP ناامن resolve می‌شود")
        if resolved.is_private or resolved.is_loopback:
            if not allow_private:
                raise UnsafePgUrlError("میزبان پاسارگارد به IP خصوصی resolve می‌شود")
        else:
            saw_public = True
    if not allow_private and not saw_public:
        raise UnsafePgUrlError("میزبان پاسارگارد باید به IP عمومی resolve شود")
    return url


def safe_pg_api_base_candidates(
    raw: str,
    *,
    resolve_dns: bool = True,
    allow_private: bool = True,
) -> list[str]:
    """Phase 4: candidate API roots that each pass ``assert_safe_pg_base_url``.

    Never returns an unsanitized URL. Empty input or fully-blocked seeds → [].
    Keeps ``allow_private`` default so LAN / same-host PasarGuard still works.
    """
    from app.config import pg_api_base_candidates

    out: list[str] = []
    for cand in pg_api_base_candidates(raw):
        try:
            safe = assert_safe_pg_base_url(
                cand, resolve_dns=resolve_dns, allow_private=allow_private
            )
        except UnsafePgUrlError:
            continue
        if safe and safe not in out:
            out.append(safe)
    return out

