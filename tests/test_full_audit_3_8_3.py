"""Regression suite for the 3.8.3 full-project security & correctness audit."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class SafeFormatTests(unittest.TestCase):
    def test_simple_placeholders(self):
        from app.services.safe_format import safe_format

        self.assertEqual(safe_format("order #{order_id}", order_id=7), "order #7")
        self.assertEqual(
            safe_format("مبلغ {amount}", amount="۱۰۰ تومان"),
            "مبلغ ۱۰۰ تومان",
        )

    def test_blocks_attribute_traversal(self):
        from app.services.safe_format import looks_like_format_injection, safe_format

        evil = "{order_id.__class__.__mro__}"
        self.assertTrue(looks_like_format_injection(evil))
        # Must NOT evaluate attributes — leave the token intact
        self.assertEqual(safe_format(evil, order_id=1), evil)

    def test_delivery_uses_safe_format(self):
        src = (ROOT / "app/services/delivery.py").read_text(encoding="utf-8")
        self.assertIn("safe_format", src)
        self.assertNotIn('.format(order_id=order.id)', src)
        self.assertNotIn("body.format(", src)


class PgStaffClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_staff_pg_denies_pg_staff_owner_fallback(self):
        """C2/C5: pg_staff must never mutate via owner token."""
        from app.api.pg_pages import _staff_pg
        from app.services.pasarguard import PasarGuardError

        staff = {"role": "pg_staff", "pg_admin_username": "staff1"}
        with (
            patch("app.api.pg_pages.get_pg", return_value=MagicMock(name="owner")) as gp,
            patch(
                "app.services.pasarguard.get_pg_for_staff",
                new=AsyncMock(side_effect=PasarGuardError("رمز ذخیره نشده")),
            ),
        ):
            with self.assertRaises(PasarGuardError):
                await _staff_pg(AsyncMock(), staff)
        gp.assert_not_called()

    async def test_staff_pg_reseller_uses_own_credentials(self):
        from app.api.pg_pages import _staff_pg

        staff = {"role": "reseller", "bot_user_id": 42, "pg_admin_username": "res1"}
        fake = MagicMock(name="reseller_pg")
        with patch(
            "app.api.pg_pages.get_pg_for_reseller",
            new=AsyncMock(return_value=fake),
        ) as gpr:
            client, as_owner = await _staff_pg(AsyncMock(), staff)
        self.assertFalse(as_owner)
        self.assertIs(client, fake)
        gpr.assert_awaited_once()

    async def test_staff_pg_rejects_broken_staff(self):
        from app.api.pg_pages import _staff_pg
        from app.services.pasarguard import PasarGuardError

        with patch(
            "app.services.pasarguard.get_pg_for_staff",
            new=AsyncMock(side_effect=PasarGuardError("دسترسی ادمین پاسارگارد فعال نیست")),
        ):
            with self.assertRaises(PasarGuardError):
                await _staff_pg(AsyncMock(), {"role": "pg_staff", "pg_admin_username": ""})


class AssertOwnedUserCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_reseller_does_not_probe_with_owner_token(self):
        from app.api.pg_pages import _assert_owned_user

        staff = {"role": "reseller", "bot_user_id": 9, "pg_admin_username": "res"}
        reseller_pg = AsyncMock()
        reseller_pg.get_user_by_id = AsyncMock(return_value={"id": 1, "username": "u"})
        owner_pg = AsyncMock()
        owner_pg.get_user_by_id = AsyncMock(return_value={"id": 1})
        with (
            patch("app.api.pg_pages.get_pg", return_value=owner_pg),
            patch(
                "app.api.pg_pages.get_pg_for_reseller",
                new=AsyncMock(return_value=reseller_pg),
            ),
        ):
            info = await _assert_owned_user(staff, 1, session=AsyncMock())
        self.assertEqual(info["username"], "u")
        reseller_pg.get_user_by_id.assert_awaited_once_with(1)
        owner_pg.get_user_by_id.assert_not_awaited()

    async def test_pg_staff_no_owner_token_probe(self):
        """C2/C5: pg_staff ownership checks must not use owner get_user_by_id."""
        from app.api.pg_pages import _assert_owned_user
        from app.services.pasarguard import PasarGuardError

        staff = {"role": "pg_staff", "pg_admin_username": "staff1"}
        owner_pg = AsyncMock()
        owner_pg.get_user_by_id = AsyncMock(
            return_value={"id": 5, "admin": {"username": "staff1"}}
        )
        with (
            patch("app.api.pg_pages.get_pg", return_value=owner_pg),
            patch(
                "app.services.pasarguard.get_pg_for_staff",
                new=AsyncMock(side_effect=PasarGuardError("no creds")),
            ),
        ):
            info = await _assert_owned_user(staff, 5, session=AsyncMock())
        self.assertIsNone(info)
        owner_pg.get_user_by_id.assert_not_awaited()


class SsrfProbeTests(unittest.TestCase):
    def test_ensure_api_base_requires_pasarguard_openapi(self):
        src = (ROOT / "app/services/pasarguard.py").read_text(encoding="utf-8")
        self.assertIn('"PasarGuard" in (r.text or "")', src)
        # Weak /api/system acceptance must be gone
        self.assertNotIn("r2.status_code in (200, 401, 403)", src)
        self.assertIn("follow_redirects=False", src)


class BillingTopupIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_idempotency_key(self):
        from app.services.billing import credit_topup

        with self.assertRaises(ValueError) as ctx:
            await credit_topup(AsyncMock(), 1, 5000, idempotency_key=None)
        self.assertIn("یکتا", str(ctx.exception))

    def test_web_form_rejects_empty_nonce(self):
        src = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("len(nonce) < 8", src)
        self.assertIn("فرم شارژ منقضی شده", src)


class PlaintextPasswordUpgradeTests(unittest.TestCase):
    def test_load_upgrades_plaintext(self):
        from app.services import web_auth as wa

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            auth = data_dir / "web_admin.json"
            auth.write_text(
                '{"username":"admin","password":"Str0ng!Pass9","token":"abc"}',
                encoding="utf-8",
            )
            with (
                patch.object(wa, "AUTH_FILE", auth),
                patch.object(wa, "DATA_DIR", data_dir),
            ):
                creds = wa.load_web_admin()
                self.assertTrue(wa._is_bcrypt_hash(creds["password"]))
                # Re-read file — must be bcrypt, never leftover plaintext
                stored = auth.read_text(encoding="utf-8")
                self.assertIn("$2b$", stored)
                self.assertNotIn("Str0ng!Pass9", stored)
                self.assertTrue(wa.pwd_context.verify("Str0ng!Pass9", creds["password"]))
                self.assertTrue(wa.verify_web_admin("admin", "Str0ng!Pass9"))


class BackupManifestPathTests(unittest.TestCase):
    def test_manifest_uses_relative_db_path(self):
        src = (ROOT / "app/services/backup.py").read_text(encoding="utf-8")
        # Relative archive members only (never absolute live paths).
        self.assertIn('SQLITE_DB_MEMBER = "data/bot.db"', src)
        self.assertIn('POSTGRES_DUMP_MEMBER = "data/postgres.dump"', src)
        self.assertIn('"db_path": db_member', src)
        self.assertNotIn('"db_path": str(db_src)', src)


class SessionCookieHardeningTests(unittest.TestCase):
    def test_login_cookie_keeps_samesite_lax(self):
        """lax preserves gateway/top-level return navigations; CSRF is Origin/Referer."""
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        login_chunk = src[src.find("resp.set_cookie") : src.find('@app.post("/logout")')]
        self.assertIn('samesite="lax"', login_chunk)
        self.assertNotIn('samesite="strict"', login_chunk)

    def test_logout_post_exists(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('@app.post("/logout")', src)
        html = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn('action="/logout"', html)
        self.assertIn('method="post"', html)


class StarsDeliveryAlertTests(unittest.TestCase):
    def test_handler_alerts_admins_on_failure(self):
        src = (ROOT / "app/bot/handlers/payments.py").read_text(encoding="utf-8")
        self.assertIn("stars delivery failed", src)
        self.assertIn("تحویل استارز ناموفق", src)
        self.assertIn("admin_ids", src)


class SyntheticIdCollisionTests(unittest.TestCase):
    def test_salt_disambiguates(self):
        from app.services.resellers import _synthetic_telegram_id

        a = _synthetic_telegram_id("alice")
        b = _synthetic_telegram_id("alice", salt=1)
        self.assertNotEqual(a, b)
        self.assertLess(a, 0)
        self.assertLess(b, 0)


class OwnerBypassGuardsStillHold(unittest.TestCase):
    def test_orders_still_fail_closed_on_set_owner(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("مالکیت قابل تنظیم نیست", src)
        self.assertIn("delete_user_by_id", src)
        self.assertIn("assert_provision_create", src)
        self.assertIn("get_pg_for_reseller", src)

    def test_no_owner_fallback_in_staff_pg(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        # Resellers must still use their own credentials
        self.assertIn("get_pg_for_reseller", src)
        # C2: pg_staff no longer gets owner-token mutations
        self.assertIn("بدون اعتبارنامه اختصاصی ممکن نیست", src)
        self.assertNotIn(
            'if staff.get("role") == "pg_staff":\n        if not _pg_owner(staff):\n'
            '            raise PasarGuardError("ادمین پاسارگارد برای این حساب تنظیم نشده است")\n'
            "        return get_pg(), True",
            src,
        )


class Version383Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.8.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.8.0")


if __name__ == "__main__":
    unittest.main()
