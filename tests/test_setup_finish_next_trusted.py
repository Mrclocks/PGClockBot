"""After setup the browser is only sent to this panel's own addresses."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.setup_wizard import force_http_login_url, trusted_finish_next

SERVER_IP = "10.0.0.2"


class TrustedFinishNextTests(unittest.TestCase):
    def _trusted(self, next_url: str, *, request_host: str = "panel.example.com") -> str:
        with patch("app.services.ssl_certs.https_is_active", return_value=False), patch(
            "app.services.setup_wizard.detect_server_ip", return_value=SERVER_IP
        ):
            return trusted_finish_next(next_url, request_host=request_host)

    def test_a_foreign_host_is_dropped(self):
        for foreign in ("https://evil.example/login", "//evil.example/login", "http://evil.example:9000/login?restarting=1"):
            self.assertEqual(self._trusted(foreign), "", foreign)

    def test_the_detected_server_address_is_accepted(self):
        url = f"http://{SERVER_IP}:9000/login?restarting=1"
        self.assertEqual(self._trusted(url), url)

    def test_the_host_the_operator_used_is_accepted(self):
        url = "http://panel.example.com:9000/login?restarting=1"
        self.assertEqual(self._trusted(url, request_host="panel.example.com"), url)

    def test_a_dropped_next_falls_back_to_the_panels_own_login_url(self):
        with patch("app.services.ssl_certs.https_is_active", return_value=False), patch(
            "app.services.setup_wizard.detect_server_ip", return_value=SERVER_IP
        ):
            target = force_http_login_url(trusted_finish_next("https://evil.example/login", request_host="panel.example.com"))
        self.assertIn(SERVER_IP, target)
        self.assertNotIn("evil.example", target)

    def test_a_malformed_next_is_dropped(self):
        self.assertEqual(self._trusted("http://[bad"), "")


if __name__ == "__main__":
    unittest.main()
