"""Regression tests for X-Forwarded-For spoofing resistance.

Previously ``_client_ip`` trusted the LEFT-most X-Forwarded-For entry when
``TRUST_PROXY`` was enabled. That entry is fully attacker-controlled (a
client can send any value, and most reverse-proxy configs only *append*
their own hop rather than overwrite the header). This let a remote attacker:

  * claim to be ``127.0.0.1`` / a private IP and auto-open the setup wizard
    without the one-time gate token (``is_local_setup_client``), and
  * rotate a fake IP on every request to bypass the login brute-force lock.

The fix reads the client IP from the entry counted from the RIGHT by
``TRUST_PROXY_HOPS`` (default 1 = a single reverse proxy directly in front
of the app), which only your own proxy can append.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.api.login_guard import client_ip as _client_ip

# Patch where get_settings is looked up (login_guard), not the old app.py site.
_SETTINGS = "app.api.login_guard.get_settings"


def _make_request(xff: str | None, client_host: str | None = "8.8.4.4"):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "client": (client_host, 12345) if client_host else None,
        "method": "GET",
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)


class _FakeSettings:
    def __init__(
        self,
        trust_proxy: bool,
        trust_proxy_hops: int = 1,
        trusted_proxies: str = "",
    ):
        self.trust_proxy = trust_proxy
        self.trust_proxy_hops = trust_proxy_hops
        # Phase 3: empty → loopback-only; XFF tests that expect trust use a
        # loopback TCP peer (nginx on same host).
        self.trusted_proxies = trusted_proxies


class ClientIpSpoofingTests(unittest.TestCase):
    def test_trust_proxy_disabled_ignores_xff_entirely(self):
        with patch(_SETTINGS, return_value=_FakeSettings(False)):
            req = _make_request("127.0.0.1", client_host="8.8.4.4")
            self.assertEqual(_client_ip(req), "8.8.4.4")

    def test_single_trusted_hop_uses_rightmost_entry_not_attacker_claim(self):
        # Attacker claims to be loopback; real proxy (loopback peer) appends the
        # true client as the LAST entry. With trust_proxy_hops=1 trust the last.
        with patch(_SETTINGS, return_value=_FakeSettings(True, 1)):
            req = _make_request("127.0.0.1, 8.8.8.8", client_host="127.0.0.1")
            self.assertEqual(_client_ip(req), "8.8.8.8")

    def test_attacker_cannot_spoof_loopback_via_xff_left_entry(self):
        with patch(_SETTINGS, return_value=_FakeSettings(True, 1)):
            req = _make_request("127.0.0.1, 8.8.8.8", client_host="127.0.0.1")
            ip = _client_ip(req)
            self.assertNotEqual(ip, "127.0.0.1")

    def test_two_trusted_hops_uses_second_from_right(self):
        with patch(
            _SETTINGS, return_value=_FakeSettings(True, 2, "10.0.0.0/8")
        ):
            req = _make_request(
                "1.1.1.1, 10.0.0.1, 10.0.0.2", client_host="10.0.0.9"
            )
            self.assertEqual(_client_ip(req), "10.0.0.1")

    def test_insufficient_hops_falls_back_to_transport_peer(self):
        with patch(_SETTINGS, return_value=_FakeSettings(True, 3)):
            # Peer untrusted (public) → XFF ignored entirely
            req = _make_request("8.8.8.8", client_host="8.8.4.4")
            self.assertEqual(_client_ip(req), "8.8.4.4")

    def test_missing_xff_falls_back_to_transport_peer(self):
        with patch(_SETTINGS, return_value=_FakeSettings(True, 1)):
            req = _make_request(None, client_host="127.0.0.1")
            self.assertEqual(_client_ip(req), "127.0.0.1")

    def test_untrusted_peer_cannot_use_xff_even_when_trust_proxy_on(self):
        with patch(_SETTINGS, return_value=_FakeSettings(True, 1)):
            req = _make_request("127.0.0.1, 8.8.8.8", client_host="203.0.113.1")
            self.assertEqual(_client_ip(req), "203.0.113.1")


class SetupGateSpoofingTests(unittest.TestCase):
    def test_local_setup_bypass_requires_correctly_trusted_hop(self):
        from app.services.setup_wizard import is_local_setup_client

        with patch(_SETTINGS, return_value=_FakeSettings(True, 1)):
            spoofed = _make_request(
                "127.0.0.1, 8.8.8.8", client_host="127.0.0.1"
            )
            self.assertFalse(is_local_setup_client(_client_ip(spoofed)))

        with patch(_SETTINGS, return_value=_FakeSettings(True, 1)):
            genuinely_local = _make_request(
                "8.8.8.8, 127.0.0.1", client_host="127.0.0.1"
            )
            self.assertTrue(is_local_setup_client(_client_ip(genuinely_local)))


if __name__ == "__main__":
    unittest.main()
