"""Phase D1 — PasarGuard-aligned credential / password policy."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.credential_policy import (
    generate_compliant_password,
    password_policy_hint_fa,
    validate_password_strength,
)


# Meets PG: ≥12, ≥2 digit, ≥2 upper, ≥2 lower, special, no quote
_OK = "AaBb12!secret"


class PasarGuardParityTests(unittest.TestCase):
    def test_accepts_compliant(self):
        ok, err = validate_password_strength(_OK)
        self.assertTrue(ok, err)
        self.assertEqual(err, "")

    def test_rejects_short(self):
        ok, err = validate_password_strength("Aa1!Bb2@x")  # 9 chars
        self.assertFalse(ok)
        self.assertIn("۱۲", err)

    def test_rejects_one_digit(self):
        ok, err = validate_password_strength("AaBbCc!xxxxx")
        self.assertFalse(ok)
        self.assertIn("رقم", err)

    def test_rejects_one_upper(self):
        ok, err = validate_password_strength("Aabbcc12!xxx")
        self.assertFalse(ok)
        self.assertIn("بزرگ", err)

    def test_rejects_one_lower(self):
        ok, err = validate_password_strength("AABBCC12!XXX")
        self.assertFalse(ok)
        self.assertIn("کوچک", err)

    def test_rejects_no_special(self):
        ok, err = validate_password_strength("AaBbCc123456")
        self.assertFalse(ok)
        self.assertIn("خاص", err)

    def test_rejects_double_quote(self):
        ok, err = validate_password_strength('AaBb12!sec"et')
        self.assertFalse(ok)
        self.assertIn('"', err)

    def test_rejects_username_embedded(self):
        ok, err = validate_password_strength("XxStaff1!ab12", username="staff1")
        self.assertFalse(ok)
        self.assertIn("نام کاربری", err)

    def test_rejects_over_72_bytes(self):
        # Build a long password that still has classes but exceeds 72 bytes
        body = "AaBb12!" + ("x" * 70)
        ok, err = validate_password_strength(body)
        self.assertFalse(ok)
        self.assertIn("۷۲", err)

    def test_web_auth_reexport(self):
        from app.services.web_auth import validate_password_strength as v2

        ok, _ = v2(_OK)
        self.assertTrue(ok)

    def test_hint_mentions_twelve(self):
        self.assertIn("۱۲", password_policy_hint_fa())


class GeneratorTests(unittest.TestCase):
    def test_rand_samples_pass(self):
        for _ in range(20):
            pwd = generate_compliant_password(14)
            ok, err = validate_password_strength(pwd)
            self.assertTrue(ok, err)

    def test_reseller_rand_password_delegates(self):
        from app.services.resellers import _rand_password

        for _ in range(10):
            ok, err = validate_password_strength(_rand_password())
            self.assertTrue(ok, err)


class SourceGuardTests(unittest.TestCase):
    def test_pg_admins_create_validates(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        fn = src[src.find("async def pg_admins_create") : src.find("async def pg_admins_web_access")]
        self.assertIn("validate_credentials", fn)

    def test_cli_validates(self):
        src = Path("scripts/set_web_password.py").read_text(encoding="utf-8")
        self.assertIn("validate_password_strength", src)

    def test_apply_reseller_validates(self):
        src = Path("app/services/resellers.py").read_text(encoding="utf-8")
        fn = src[
            src.find("async def apply_reseller_panel_password") : src.find(
                "def _rand_username"
            )
        ]
        self.assertIn("validate_password_strength", fn)

    def test_d1_did_not_require_d2_yet_marker_removed(self):
        """D2 supersedes D1 grant-route freeze; staff path must use grant_web_access."""
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("async def pg_admins_web_access_staff", src)
        self.assertIn("grant_web_access", src)


class EncryptFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_fails_when_encrypt_none(self):
        from app.services import pg_staff_access as psa

        session = AsyncMock()
        session.add = MagicMock()
        with (
            patch.object(psa, "conflict_message_for_new_grant", new=AsyncMock(return_value=None)),
            patch.object(psa, "_username_taken", new=AsyncMock(return_value=None)),
            patch.object(psa, "resolve_pg_role_id_for_admin", new=AsyncMock(return_value=1)),
            patch.object(psa, "hash_password", return_value="h"),
            patch("app.services.pasarguard.get_pg") as gp,
            patch("app.services.pasarguard.reset_pg"),
            patch("app.services.secret_box.encrypt_secret", return_value=None),
        ):
            gp.return_value.modify_admin = AsyncMock()
            row, err = await psa.grant_web_access(
                session,
                pg_username="staffx",
                web_username="staffx",
                password=_OK,
            )
        self.assertIsNone(row)
        self.assertIn("رمز‌گذاری", err or "")
        gp.return_value.modify_admin.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class PanelUsernameCaseTests(unittest.TestCase):
    def test_uppercase_and_panel_symbols_are_accepted_and_case_is_kept(self) -> None:
        from app.services.credential_policy import validate_pg_username

        cleaned, error = validate_pg_username("Ali.Reza-01@Shop")
        self.assertIsNone(error)
        self.assertEqual(cleaned, "Ali.Reza-01@Shop")

    def test_username_up_to_128_chars_is_accepted_and_longer_is_rejected(self) -> None:
        from app.services.credential_policy import validate_pg_username

        self.assertIsNone(validate_pg_username("A" * 128)[1])
        self.assertIsNotNone(validate_pg_username("A" * 129)[1])
