"""Checks for a URL an operator typed that the server will fetch on their behalf.

Only a public HTTPS address on the default port is accepted. The host is checked as a literal and,
before every fetch, against what its name resolves to now, so a name that points at a private
address is refused. The caller must also refuse redirects. A name that changes its answer between
this check and the connection is not covered; that residual risk is small because the response is
never shown to anyone, only parsed for one number.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

MAX_URL_LENGTH = 500
HTTPS_PORT = 443
_BLOCKED_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "metadata.google.internal"})


class UnsafeUrlError(ValueError):
    """The address is refused; the message is safe to show to the operator."""


def _is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def validate_public_https_url(raw: object) -> tuple[str, str]:
    """(normalized url, host) for a public https address, or UnsafeUrlError."""
    text = str(raw or "").strip()
    if not text or len(text) > MAX_URL_LENGTH or any(ord(ch) < 33 for ch in text):
        raise UnsafeUrlError("آدرس API نامعتبر است")
    try:
        parts = urlparse(text)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("آدرس API نامعتبر است") from exc
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeUrlError("آدرس API باید با https شروع شود")
    if parts.username or parts.password:
        raise UnsafeUrlError("آدرس API نباید نام کاربری یا رمز داشته باشد")
    if port not in (None, HTTPS_PORT):
        raise UnsafeUrlError("آدرس API باید روی پورت پیش‌فرض https باشد")
    host = parts.hostname.strip().lower().rstrip(".")
    if host in _BLOCKED_NAMES or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrlError("آدرس API به میزبان داخلی مجاز نیست")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise UnsafeUrlError("آدرس API نامعتبر است") from None
    else:
        if not _is_public(host):
            raise UnsafeUrlError("آدرس API به میزبان داخلی مجاز نیست")
    return text, host


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def ensure_host_resolves_public(host: str) -> None:
    """Refuse a name whose current answer includes any non-public address."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not literal.is_global:
            raise UnsafeUrlError("آدرس API به میزبان داخلی مجاز نیست")
        return
    try:
        addresses = await asyncio.to_thread(_resolve, host)
    except OSError as exc:
        raise UnsafeUrlError("آدرس API قابل resolve نیست") from exc
    if not addresses or not all(_is_public(address) for address in addresses):
        raise UnsafeUrlError("آدرس API به میزبان داخلی مجاز نیست")
