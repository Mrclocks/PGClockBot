"""Setup finish: HTTP login + readable .env flash copy."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class SetupFinishHttpTests(unittest.TestCase):
    def test_finish_login_url_is_http_with_restarting(self):
        from app.services.setup_wizard import setup_finish_login_url

        with patch("app.services.ssl_certs.https_is_active", return_value=False), patch(
            "app.services.setup_wizard.detect_server_ip", return_value="91.107.149.20"
        ), patch("app.config.get_settings") as gs:
            gs.return_value.web_port = 9000
            url = setup_finish_login_url()
        self.assertTrue(url.startswith("http://"), url)
        self.assertNotIn("https://", url)
        self.assertIn("/login", url)
        self.assertIn("restarting=1", url)

    def test_force_http_strips_https(self):
        from app.services.setup_wizard import force_http_login_url

        with patch("app.services.ssl_certs.https_is_active", return_value=False), patch(
            "app.services.setup_wizard.detect_server_ip", return_value="10.0.0.2"
        ), patch("app.config.get_settings") as gs:
            gs.return_value.web_port = 9000
            out = force_http_login_url("https://evil.example/login")
        self.assertTrue(out.startswith("http://"), out)
        self.assertNotIn("https://", out)
        self.assertIn("evil.example", out)

    def test_force_http_keeps_https_when_tls_live(self):
        from app.services.setup_wizard import force_http_login_url

        with patch("app.services.ssl_certs.https_is_active", return_value=True), patch(
            "app.services.ssl_certs.public_panel_base_url",
            return_value="https://panel.example.com:9443",
        ):
            out = force_http_login_url("https://evil.example/login")
        self.assertEqual(out, "https://panel.example.com:9443/login?restarting=1")

    def test_template_wraps_flash_and_forces_http_nav(self):
        html = (ROOT / "app/web/templates/setup.html").read_text(encoding="utf-8")
        self.assertIn('class="flash-copy"', html)
        self.assertIn("فایل env را امن نگه دارید", html)
        self.assertNotIn(".env", html.split("setup-finish-form")[0])
        self.assertIn("setup-finish-form", html)
        self.assertIn("window.location.replace", html)
        self.assertIn("base.protocol = 'http:'", html)
        self.assertIn("httpsReady", html)
        self.assertIn("finish_login_url", html)
        self.assertIn("progress.hidden = (n >= 4)", html)
        self.assertIn(".setup-probe[hidden]", (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8"))

    def test_css_isolates_env_fname(self):
        html = (ROOT / "app/web/templates/setup.html").read_text(encoding="utf-8")
        self.assertIn("فایل env را امن نگه دارید", html)
        self.assertNotIn("code.env-fname", html)


if __name__ == "__main__":
    unittest.main()
