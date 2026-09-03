"""Login form must accept autofill and never return raw FastAPI 422 JSON."""

from __future__ import annotations

import unittest
from pathlib import Path


class LoginAutofillTests(unittest.TestCase):
    def test_login_fields_allow_password_managers(self):
        src = Path("app/web/templates/login.html").read_text(encoding="utf-8")
        self.assertIn('autocomplete="on"', src)
        self.assertIn('autocomplete="username"', src)
        self.assertIn('autocomplete="current-password"', src)
        # readonly-until-focus blocks many autofill/password managers
        login_form = src.split("action=\"/login\"", 1)[1].split("</form>", 1)[0]
        self.assertNotIn("readonly", login_form)

    def test_login_submit_accepts_missing_form_fields(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        start = src.find("async def login_submit(")
        end = src.find("\n    @app.", start + 1)
        chunk = src[start:end]
        self.assertIn('username: str = Form("")', chunk)
        self.assertIn('password: str = Form("")', chunk)
        self.assertNotIn("Form(...)", chunk)
        self.assertIn("نام کاربری و رمز دسترسی الزامی است", chunk)

    def test_login_autofill_auto_submits(self):
        src = Path("app/web/templates/login.html").read_text(encoding="utf-8")
        self.assertIn("autoSubmitAutofill", src)
        self.assertIn("requestSubmit", src)
        self.assertIn("auth-autofill-mark", Path("app/web/static/panel.css").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
