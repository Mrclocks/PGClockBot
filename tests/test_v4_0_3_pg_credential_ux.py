"""v4.0.3 — PasarGuard-aligned username/password validation UX."""

from __future__ import annotations

import unittest

from app.services.credential_policy import (
    humanize_pg_validation_error,
    password_policy_hint_fa,
    username_policy_hint_fa,
    validate_credentials,
    validate_password_strength,
    validate_pg_username,
)
from app.services.web_auth import validate_web_username


_OK_PWD = "AaBb12!secret"


class PgUsernameTests(unittest.TestCase):
    def test_accepts_pg_charset(self):
        for name in ("admin", "shop_01", "a-b", "u@host", "a.b_c-1"):
            cleaned, err = validate_pg_username(name)
            self.assertIsNone(err, name)
            self.assertEqual(cleaned, name)

    def test_rejects_short(self):
        _, err = validate_pg_username("ab")
        self.assertIsNotNone(err)
        self.assertIn("۳", err)
        self.assertIn("هماهنگ نیست", err)

    def test_rejects_persian_and_space(self):
        _, err = validate_pg_username("ادمین")
        self.assertIsNotNone(err)
        self.assertIn("انگلیسی", err)
        _, err2 = validate_pg_username("bad name")
        self.assertIsNotNone(err2)

    def test_rejects_consecutive_specials(self):
        _, err = validate_pg_username("a__b")
        self.assertIsNotNone(err)
        self.assertIn("پشت‌سرهم", err)
        _, err2 = validate_pg_username("user..1")
        self.assertIsNotNone(err2)

    def test_max_128(self):
        ok = "a" * 128
        _, err = validate_pg_username(ok)
        self.assertIsNone(err)
        _, err2 = validate_pg_username("a" * 129)
        self.assertIsNotNone(err2)
        self.assertIn("۱۲۸", err2)

    def test_web_auth_delegates_to_pg(self):
        cleaned, err = validate_web_username("shop-01")
        self.assertIsNone(err)
        self.assertEqual(cleaned, "shop-01")
        _, err2 = validate_web_username("x__y")
        self.assertIsNotNone(err2)


class PasswordExplainTests(unittest.TestCase):
    def test_lists_multiple_failures(self):
        ok, err = validate_password_strength("short")
        self.assertFalse(ok)
        self.assertIn("هماهنگ نیست", err)
        self.assertIn("۱۲", err)
        # short also lacks digits/upper/special — should mention more than one issue
        self.assertIn("(2)", err)

    def test_counts_current_digits(self):
        ok, err = validate_password_strength("AaBbCc!xxxxx")  # one? zero digits
        self.assertFalse(ok)
        self.assertIn("رقم", err)

    def test_credentials_username_first(self):
        cleaned, err = validate_credentials("ab", _OK_PWD)
        self.assertEqual(cleaned, "ab")
        self.assertIsNotNone(err)
        self.assertIn("نام کاربری", err)

    def test_credentials_ok(self):
        cleaned, err = validate_credentials("good_admin", _OK_PWD)
        self.assertEqual(cleaned, "good_admin")
        self.assertIsNone(err)

    def test_hints(self):
        self.assertIn("۱۲", password_policy_hint_fa())
        self.assertIn("۱۲۸", username_policy_hint_fa())


class HumanizeApiErrorTests(unittest.TestCase):
    def test_translates_username_length(self):
        msg = humanize_pg_validation_error(
            "body.username: Value error, Username only can be 3 to 128 characters."
        )
        self.assertIn("پاسارگارد", msg)
        self.assertIn("۳", msg)
        self.assertIn("۱۲۸", msg)

    def test_translates_password_special(self):
        msg = humanize_pg_validation_error(
            "Password must contain at least one special character"
        )
        self.assertIn("کاراکتر خاص", msg)

    def test_passthrough_unknown(self):
        raw = "connection refused"
        self.assertEqual(humanize_pg_validation_error(raw), raw)


class SourceGuardTests(unittest.TestCase):
    def test_create_admin_uses_validate_credentials(self):
        from pathlib import Path

        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        fn = src[src.find("async def pg_admins_create") : src.find("async def pg_admins_web_access")]
        self.assertIn("validate_credentials", fn)
        self.assertIn("_pg_err", fn)

    def test_version_file(self):
        from pathlib import Path

        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual(Path("VERSION").read_text(encoding="utf-8").strip(), "4.9.0")


if __name__ == "__main__":
    unittest.main()
