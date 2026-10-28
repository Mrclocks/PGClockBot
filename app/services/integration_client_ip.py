"""Resolve integration transport identity through explicitly trusted proxy networks."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.integration_policy import IntegrationError
from app.services.security_policy import is_placeholder_secret

MAX_FORWARDED_BYTES = 2048
MAX_PROXY_CHAIN = 16
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class IntegrationTransport:
    peer: str | None
    scheme: str


@dataclass(frozen=True, slots=True)
class IntegrationClient:
    address: str
    scheme: str


def parse_address(value: str) -> IPAddress:
    if not value or "%" in value or len(value) > 45:
        raise ValueError("Invalid transport address")
    address = ipaddress.ip_address(value)
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


def proxy_networks(raw: str) -> tuple[IPNetwork, ...]:
    parts = raw.split(",") if raw else []
    if len(parts) > MAX_PROXY_CHAIN:
        raise ValueError("Too many trusted proxy networks")
    networks = tuple(ipaddress.ip_network(part.strip(), strict=True) for part in parts)
    if any(network.prefixlen == 0 or (network.version == 6 and network.network_address.ipv4_mapped) for network in networks):
        raise ValueError("Proxy networks must be explicit native IPv4 or IPv6 ranges")
    return networks


def _trusted(address: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    return any(address in network for network in networks)


def _forwarded_values(headers: list[tuple[bytes, bytes]]) -> tuple[list[str], str | None]:
    selected = [(name.lower(), value) for name, value in headers if name.lower() in {
        b"x-forwarded-for", b"x-forwarded-proto", b"forwarded",
    }]
    if sum(len(value) for _, value in selected) > MAX_FORWARDED_BYTES:
        raise ValueError("Oversized forwarding information")
    if any(name == b"forwarded" for name, _ in selected):
        raise ValueError("Unsupported forwarding format")
    chains = [value for name, value in selected if name == b"x-forwarded-for"]
    schemes = [value for name, value in selected if name == b"x-forwarded-proto"]
    if len(chains) != 1 or len(schemes) > 1:
        raise ValueError("Ambiguous forwarding information")
    parts = chains[0].decode("ascii").split(",")
    if not 1 <= len(parts) <= MAX_PROXY_CHAIN:
        raise ValueError("Invalid proxy chain length")
    scheme = schemes[0].decode("ascii").strip() if schemes else None
    if scheme is not None and scheme not in {"http", "https"}:
        raise ValueError("Invalid forwarded scheme")
    return parts, scheme


def resolve_client(
    transport: IntegrationTransport | None, headers: list[tuple[bytes, bytes]], networks: tuple[IPNetwork, ...],
) -> IntegrationClient:
    if not isinstance(transport, IntegrationTransport) or transport.peer is None:
        raise IntegrationError("security_unavailable")
    try:
        peer = parse_address(transport.peer)
        if not _trusted(peer, networks):
            return IntegrationClient(str(peer), transport.scheme)
        parts, scheme = _forwarded_values(headers)
        addresses = [parse_address(part.strip()) for part in parts]
        current = peer
        for address in reversed(addresses):
            if not _trusted(current, networks):
                break
            current = address
        return IntegrationClient(str(current), scheme or transport.scheme)
    except (ValueError, UnicodeError):
        raise IntegrationError("security_unavailable") from None


def ip_identifier(address: str, secret: str, *, now: datetime | None = None) -> str:
    if is_placeholder_secret(secret) or len(secret) < 32:
        raise IntegrationError("security_unavailable")
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
    message = f"pgclock:integration-ip:v1:{day}:{address}".encode("ascii")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
