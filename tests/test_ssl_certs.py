"""SSL helpers regression tests."""

from __future__ import annotations

import unittest
from pathlib import Path


class SslDomainTests(unittest.TestCase):
    def test_normalize_and_validate(self):
        from app.services.ssl_certs import is_valid_domain, normalize_domain

        self.assertEqual(normalize_domain("https://Bot.Example.com/x"), "bot.example.com")
        self.assertTrue(is_valid_domain("bot.example.com"))
        self.assertFalse(is_valid_domain("localhost"))
        self.assertFalse(is_valid_domain(""))


class SslUiWiredTests(unittest.TestCase):
    def test_settings_tab_and_template(self):
        from app.services.users import PANEL_SETTINGS_TABS, SETTINGS_TABS, TAB_SETTING_GROUPS

        self.assertNotIn(("ssl", "SSL"), SETTINGS_TABS)
        self.assertIn(("ssl", "SSL"), PANEL_SETTINGS_TABS)
        self.assertEqual(TAB_SETTING_GROUPS.get("ssl"), [])
        self.assertTrue(Path("app/web/templates/_settings_ssl.html").is_file())
        self.assertTrue(Path("app/services/ssl_certs.py").is_file())
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("/settings/ssl/progress", src)
        self.assertIn("PANEL_SETTINGS", src)
        self.assertIn("start_issue_job", src)
        self.assertIn("enable_https", src)
        main = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("uvicorn_ssl_kwargs", main)
        ssl_src = Path("app/services/ssl_certs.py").read_text(encoding="utf-8")
        self.assertIn("never enables HTTPS", ssl_src)
        self.assertIn("_start_acme_http", ssl_src)


class UpdateCopyTests(unittest.TestCase):
    def test_update_template_has_progress_ui(self):
        src = Path("app/web/templates/_settings_update.html").read_text(encoding="utf-8")
        self.assertIn("upd-start", src)
        self.assertIn("/update/status", src)
        self.assertIn("get.sh", src)
        self.assertIn("گزینه ۲", src)


if __name__ == "__main__":
    unittest.main()
