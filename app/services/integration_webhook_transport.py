"""Resolve every address, reject internal targets, and pin TLS to the validated IP."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit, urlunsplit

from app.services.integration_policy import IntegrationError

HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
MAX_RESPONSE_BYTES = 16*1024


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    url: str
    hostname: str
    path: str


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status: int
    retry_after: int = 0


def webhook_target(raw: str) -> WebhookTarget:
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 2048 or any(ord(char) < 33 or ord(char) > 126 for char in raw):
            raise ValueError
        url = urlsplit(raw)
        host = (url.hostname or "").lower()
        labels = host.split(".")
        if (url.scheme != "https" or url.port not in {None, 443} or url.username or url.password or url.query or url.fragment
                or len(host) > 253 or len(labels) < 2 or any(not HOST_LABEL.fullmatch(label) for label in labels)
                or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")) or labels[-1].isdigit()):
            raise ValueError
        path = url.path or "/"
        if "\\" in path or any(ord(char) < 33 or ord(char) == 127 for char in unquote(path)):
            raise ValueError
        normalized = urlunsplit(("https", host, path, "", ""))
        return WebhookTarget(normalized, host, path)
    except (ValueError, UnicodeError):
        raise IntegrationError("invalid_request") from None


def validated_addresses(addresses: list[str]) -> tuple[str, ...]:
    if not addresses or len(addresses) > 32:
        raise IntegrationError("invalid_request")
    result = set()
    try:
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            comparable = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
            if not comparable.is_global or comparable.is_multicast or comparable.is_reserved or "%" in raw:
                raise ValueError
            result.add(str(address))
    except ValueError:
        raise IntegrationError("invalid_request") from None
    return tuple(sorted(result))


async def resolve_webhook(target: WebhookTarget) -> tuple[str, ...]:
    async with asyncio.timeout(2):
        rows = await asyncio.get_running_loop().getaddrinfo(target.hostname, 443, type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP)
    return validated_addresses([row[4][0] for row in rows])


def retry_after_seconds(value: str, *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    try:
        if value.isascii() and value.isdecimal() and len(value) <= 10:
            return min(86400, int(value))
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return 0
        return max(0, min(86400, int((parsed-now).total_seconds())))
    except (ValueError, TypeError, OverflowError):
        return 0


async def send_pinned_webhook(target: WebhookTarget, addresses: tuple[str, ...], body: bytes,
                              headers: dict[str, str]) -> WebhookResponse:
    addresses = validated_addresses(list(addresses))
    # No redirect handling, proxy support, alternate hostname, or second DNS lookup.
    context = await asyncio.to_thread(ssl.create_default_context)
    writer = None
    try:
        async with asyncio.timeout(5):
            reader, writer = await asyncio.open_connection(addresses[0], 443, ssl=context,
                server_hostname=target.hostname, limit=MAX_RESPONSE_BYTES+1)
            request_headers = {"Host": target.hostname, "Content-Type": "application/json", "Content-Length": str(len(body)),
                "Connection": "close", **headers}
            if any("\r" in value or "\n" in value for value in request_headers.values()):
                raise IntegrationError("invalid_request")
            request = f"POST {target.path} HTTP/1.1\r\n"+"".join(f"{key}: {value}\r\n" for key, value in request_headers.items())+"\r\n"
            writer.write(request.encode("ascii")+body)
            await writer.drain()
            raw = await reader.readuntil(b"\r\n\r\n")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("response_limit")
            lines = raw.decode("iso-8859-1").split("\r\n")
            if not re.fullmatch(r"HTTP/1\.[01] [1-5][0-9]{2}(?: .*)?", lines[0]):
                raise ValueError("response_format")
            status = int(lines[0].split(" ")[1])
            response_headers = {}
            for line in lines[1:]:
                if line:
                    key, value = line.split(":", 1)
                    response_headers[key.lower()] = value.strip()
            size = len(raw)
            while chunk := await reader.read(min(4096, MAX_RESPONSE_BYTES+1-size)):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("response_limit")
            retry = retry_after_seconds(response_headers.get("retry-after", "")) if status in {429, 503} else 0
            return WebhookResponse(status, retry)
    finally:
        if writer is not None:
            writer.close()
            # Closing is best effort and must not extend the total delivery budget.
