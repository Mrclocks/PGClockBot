"""Proxy provenance, canonical addresses and bounded forwarding chains."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request

from app.api.integration_transport import IntegrationPeerCapture, TRANSPORT_KEY
from app.services.integration_client_ip import (
    IntegrationTransport, ip_identifier, proxy_networks, resolve_client,
)
from app.services.integration_policy import IntegrationError

NETWORKS = proxy_networks("10.0.0.0/24,2001:db8:1::/48")


def headers(chain: str, scheme: str = "https") -> list[tuple[bytes, bytes]]:
    return [(b"x-forwarded-for", chain.encode()), (b"x-forwarded-proto", scheme.encode())]


@pytest.mark.parametrize("peer,chain,expected", [
    ("10.0.0.1", "198.51.100.3", "198.51.100.3"),
    ("::ffff:10.0.0.1", "::ffff:198.51.100.3", "198.51.100.3"),
    ("10.0.0.1", "192.0.2.99, 198.51.100.3, 10.0.0.2", "198.51.100.3"),
    ("2001:db8:1::1", "2001:db8:2::abcd, 10.0.0.2", "2001:db8:2::abcd"),
    ("198.51.100.8", "127.0.0.1, 10.0.0.2", "198.51.100.8"),
])
def test_only_verified_proxy_peers_can_forward(peer: str, chain: str, expected: str) -> None:
    client = resolve_client(IntegrationTransport(peer, "http"), headers(chain), NETWORKS)
    assert client.address == expected
    assert client.scheme == ("http" if peer == "198.51.100.8" else "https")


@pytest.mark.parametrize("values", [
    [], headers(""), headers("unknown"), headers("[2001:db8::1]:80"), headers("fe80::1%eth0"),
    headers("198.51.100.3", "https,http"), headers("198.51.100.3") + [(b"x-forwarded-for", b"1.2.3.4")],
    headers("198.51.100.3") + [(b"forwarded", b"for=1.2.3.4")], headers(",".join(["1.2.3.4"]*17)),
    headers("1"*2049), [(b"x-forwarded-for", b"\xff")],
])
def test_malformed_or_unverifiable_forwarding_fails_closed(values: list[tuple[bytes, bytes]]) -> None:
    with pytest.raises(IntegrationError) as caught:
        resolve_client(IntegrationTransport("10.0.0.1", "https"), values, NETWORKS)
    assert caught.value.code == "security_unavailable"


@pytest.mark.parametrize("peer", [None, "not-an-ip", "fe80::1%eth0"])
def test_missing_or_invalid_transport_peer_fails_closed(peer: str | None) -> None:
    with pytest.raises(IntegrationError):
        resolve_client(IntegrationTransport(peer, "https"), [], NETWORKS)
    with pytest.raises(IntegrationError):
        resolve_client(None, headers("198.51.100.3"), NETWORKS)


def test_default_ignores_all_forwarded_headers_and_generic_panel_trust() -> None:
    with patch.dict("os.environ", {"TRUST_PROXY": "1", "TRUST_PROXY_HOPS": "5"}):
        client = resolve_client(IntegrationTransport("::ffff:198.51.100.9", "http"), headers("1.2.3.4"), ())
    assert client.address == "198.51.100.9" and client.scheme == "http"


def test_server_normalization_cannot_be_reinterpreted_by_integration() -> None:
    async def run() -> None:
        app = FastAPI()
        @app.get("/")
        async def show(request: Request) -> dict:
            raw = request.scope[TRANSPORT_KEY]
            integration = resolve_client(raw, request.scope["headers"], proxy_networks("10.0.0.1/32"))
            return {"panel": request.client.host, "raw": raw.peer, "integration": integration.address}
        wrapped = IntegrationPeerCapture(app, trusted_hosts="*")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped, client=("10.0.0.1", 123)),
                                     base_url="http://test") as client:
            response = await client.get("/", headers={"X-Forwarded-For": "192.0.2.99, 198.51.100.3", "X-Forwarded-Proto": "https"})
        # Even an overly permissive panel/server configuration cannot enlarge the integration allowlist.
        assert response.json() == {"panel": "192.0.2.99", "raw": "10.0.0.1", "integration": "198.51.100.3"}
    asyncio.run(run())


@pytest.mark.parametrize("networks", ["*", "0.0.0.0/0", "::/0", "::ffff:10.0.0.0/120", "bad", ",".join(["10.0.0.1/32"]*17)])
def test_proxy_configuration_requires_explicit_bounded_native_networks(networks: str) -> None:
    with pytest.raises(ValueError):
        proxy_networks(networks)


def test_ip_pseudonyms_are_day_bounded_and_secret_keyed() -> None:
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    first = ip_identifier("198.51.100.3", "synthetic-secret-key"*3, now=now)
    assert len(first) == 64
    assert first != ip_identifier("198.51.100.3", "other-synthetic-key"*3, now=now)
    assert first != ip_identifier("198.51.100.3", "synthetic-secret-key"*3, now=now+timedelta(days=1))
