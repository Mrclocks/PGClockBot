"""Panel in-app update button: CSRF JSON + UI wired from panel.js (not inline)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.services.csrf import (
    CSRF_FORM_FIELD,
    CSRF_HEADER,
    csrf_tokens_match,
    extract_csrf_from_request,
)

ROOT = Path(__file__).resolve().parents[1]


class UpdateCsrfJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_csrf_from_json_body(self):
        req = MagicMock()
        req.headers = {"content-type": "application/json"}
        req.json = AsyncMock(
            return_value={CSRF_FORM_FIELD: "tok-from-json", "target": "10.1.7"}
        )
        tok = await extract_csrf_from_request(req)
        self.assertEqual(tok, "tok-from-json")

    async def test_header_wins_over_json_body(self):
        req = MagicMock()
        req.headers = {
            "content-type": "application/json",
            CSRF_HEADER: "tok-header",
        }
        req.json = AsyncMock(return_value={CSRF_FORM_FIELD: "tok-json"})
        tok = await extract_csrf_from_request(req)
        self.assertEqual(tok, "tok-header")
        req.json.assert_not_awaited()

    def test_tokens_match(self):
        self.assertTrue(csrf_tokens_match("abc", "abc"))
        self.assertFalse(csrf_tokens_match("abc", "xyz"))
        self.assertFalse(csrf_tokens_match("", "abc"))


class UpdateUiTemplateTests(unittest.TestCase):
    def test_update_logic_lives_in_panel_js(self):
        html = (ROOT / "app/web/templates/_settings_update.html").read_text(
            encoding="utf-8"
        )
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertNotIn("<script", html.lower())
        self.assertIn("function csrfToken()", js)
        self.assertIn("X-CSRF-Token", js)
        self.assertIn("csrf_token", js)
        self.assertIn("readUpdateResponse", js)
        self.assertIn("/update/start", js)
        self.assertIn("نشست امنیتی منقضی شده", js)

    def test_app_returns_json_csrf_error_for_update(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('path_now.startswith("/update/")', src)
        self.assertIn('path_now.startswith("/backup/")', src)
        self.assertIn("نشست امنیتی منقضی شده", src)
        self.assertIn("درخواست امنیتی رد شد", src)
        self.assertIn("def _csrf_reject", src)
        self.assertIn("JSONResponse", src)


if __name__ == "__main__":
    unittest.main()
