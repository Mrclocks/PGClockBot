"""Phase 3 — TRUSTED_PROXIES gate + real-loopback setup wizard."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.api.login_guard import client_ip as _client_ip
from app.api.login_guard import forwarded_proto_is_https, transport_peer_ip

_SETTINGS = "app.api.login_guard.get_settings"


def _make_request(
    xff: str | None,
    client_host: str | None = "8.8.4.4",
    *,
    proto: str | None = None,
):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if proto is not None:
        headers.append((b"x-forwarded-proto", proto.encode()))
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
        self.trusted_proxies = trusted_proxies


class TrustedProxyPeerTests(unittest.TestCase):
    def test_trust_proxy_ignores_xff_from_untrusted_peer(self):
        # Remote attacker connects directly with TRUST_PROXY=1 — must NOT
        # honor XFF (would claim loopback and open the wizard).
        with patch(_SETTINGS, return_value=_FakeSettings(True, 1, "")):
            req = _make_request("127.0.0.1", client_host="8.8.4.4")
            self.assertEqual(_client_ip(req), "8.8.4.4")
            self.assertEqual(transport_peer_ip(req), "8.8.4.4")

    def test_trust_proxy_honors_xff_from_loopback_peer(self):
        with patch(_SETTINGS, return_value=_FakeSettings(True, 1, "")):
            req = _make_request("203.0.113.9, 127.0.0.1", client_host="127.0.0.1")
            # hops=1 → rightmost before last? parts[-1] with hops=1 is last entry
            # Actually parts = [203.0.113.9, 127.0.0.1], parts[-1] = 127.0.0.1
            # Wait — typically nginx appends the client it saw. Chain from
            # internet client through local nginx: "203.0.113.9" only, peer=127.0.0.1.
            # For hops=1, parts[-1] is the client nginx saw.
            req2 = _make_request("203.0.113.9", client_host="127.0.0.1")
            self.assertEqual(_client_ip(req2), "203.0.113.9")

    def test_custom_trusted_proxies_docker_cidr(self):
        with patch(
            _SETTINGS, return_value=_FakeSettings(True, 1, "172.16.0.0/12")
        ):
            req = _make_request("198.51.100.7", client_host="172.18.0.5")
            self.assertEqual(_client_ip(req), "198.51.100.7")
            # Outside CIDR — ignored
            req2 = _make_request("198.51.100.7", client_host="10.0.0.5")
            self.assertEqual(_client_ip(req2), "10.0.0.5")

    def test_forwarded_proto_requires_trusted_peer(self):
        with patch(_SETTINGS, return_value=_FakeSettings(True, 1, "")):
            bad = _make_request(None, client_host="8.8.4.4", proto="https")
            self.assertFalse(forwarded_proto_is_https(bad))
            good = _make_request(None, client_host="127.0.0.1", proto="https")
            self.assertTrue(forwarded_proto_is_https(good))

    def test_trust_proxy_off_still_ignores_xff(self):
        with patch(_SETTINGS, return_value=_FakeSettings(False)):
            req = _make_request("127.0.0.1", client_host="8.8.4.4")
            self.assertEqual(_client_ip(req), "8.8.4.4")


class SetupLocalRequestTests(unittest.TestCase):
    def test_remote_peer_cannot_open_wizard_even_with_spoofed_xff(self):
        from app.services.setup_wizard import is_local_setup_request

        with patch(_SETTINGS, return_value=_FakeSettings(True, 1, "")):
            # Direct remote connection claiming loopback via XFF
            req = _make_request("127.0.0.1", client_host="8.8.4.4")
            self.assertFalse(is_local_setup_request(req))

    def test_local_nginx_public_client_does_not_auto_open_wizard(self):
        """Everyone behind local nginx must use ?gate= — not auto-open."""
        from app.services.setup_wizard import is_local_setup_request

        with patch(_SETTINGS, return_value=_FakeSettings(True, 1, "")):
            req = _make_request("203.0.113.50", client_host="127.0.0.1")
            self.assertFalse(is_local_setup_request(req))

    def test_true_loopback_peer_and_client_opens_wizard(self):
        from app.services.setup_wizard import is_local_setup_request

        with patch(_SETTINGS, return_value=_FakeSettings(False)):
            req = _make_request(None, client_host="127.0.0.1")
            self.assertTrue(is_local_setup_request(req))

        with patch(_SETTINGS, return_value=_FakeSettings(True, 1, "")):
            req = _make_request("127.0.0.1", client_host="127.0.0.1")
            self.assertTrue(is_local_setup_request(req))


class PeerCidrHelperTests(unittest.TestCase):
    def test_default_cidrs_are_loopback(self):
        from app.services.security_policy import (
            parse_trusted_proxy_cidrs,
            peer_is_trusted_proxy,
        )

        nets = parse_trusted_proxy_cidrs("")
        self.assertTrue(any("127.0.0.0" in str(n) for n in nets))
        self.assertTrue(peer_is_trusted_proxy("127.0.0.1", trusted_proxies_raw=""))
        self.assertFalse(peer_is_trusted_proxy("10.0.0.1", trusted_proxies_raw=""))
        self.assertTrue(
            peer_is_trusted_proxy("10.0.0.1", trusted_proxies_raw="10.0.0.0/8")
        )


class DocsAndConfigPhase3Tests(unittest.TestCase):
    def test_env_example_documents_trusted_proxies(self):
        from pathlib import Path

        text = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn("TRUSTED_PROXIES", text)
        self.assertIn("TRUST_PROXY_HOPS", text)

    def test_config_has_trusted_proxies_field(self):
        from app.config import Settings

        self.assertTrue(hasattr(Settings, "model_fields") or hasattr(Settings, "__fields__"))
        fields = getattr(Settings, "model_fields", None) or getattr(Settings, "__fields__")
        self.assertIn("trusted_proxies", fields)

    def test_app_uses_local_setup_request(self):
        from pathlib import Path

        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("is_local_setup_request(request)", src)
        self.assertNotIn("is_local_setup_client(_client_ip(request))", src)


if __name__ == "__main__":
    unittest.main()
