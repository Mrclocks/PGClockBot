"""https_public_base keeps the live panel listen port (e.g. :9000)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import public_https as pub


class HttpsPublicBasePanelPortTests(unittest.TestCase):
    def test_live_panel_with_port_wins_over_http_env(self) -> None:
        with (
            patch.object(pub, "https_is_active", return_value=True),
            patch.object(
                pub, "public_panel_base_url", return_value="https://bot.example.com:9000"
            ),
            patch.object(pub, "env_public_base", return_value="http://203.0.113.5:9000"),
        ):
            self.assertEqual(pub.https_public_base(), "https://bot.example.com:9000")

    def test_does_not_strip_live_panel_port(self) -> None:
        with (
            patch.object(pub, "https_is_active", return_value=True),
            patch.object(
                pub, "public_panel_base_url", return_value="https://bot.example.com:9000"
            ),
            patch.object(pub, "env_public_base", return_value="https://bot.example.com"),
        ):
            # Live panel TLS address is authoritative, even when env is "cleaner".
            self.assertEqual(pub.https_public_base(), "https://bot.example.com:9000")

    def test_env_https_when_panel_tls_off(self) -> None:
        with (
            patch.object(pub, "https_is_active", return_value=False),
            patch.object(pub, "env_public_base", return_value="https://bot.example.com:9000"),
        ):
            self.assertEqual(pub.https_public_base(), "https://bot.example.com:9000")

    def test_empty_when_no_https(self) -> None:
        with (
            patch.object(pub, "https_is_active", return_value=False),
            patch.object(pub, "env_public_base", return_value="http://203.0.113.5:9000"),
        ):
            self.assertEqual(pub.https_public_base(), "")


if __name__ == "__main__":
    unittest.main()
