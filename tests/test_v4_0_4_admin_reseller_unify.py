"""v4.0.4 — login flash, delete FK cascade, admin↔reseller unify."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class LoginFlashFixTests(unittest.TestCase):
    def test_css_hides_flash_when_hidden(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".flash[hidden]", css)
        self.assertIn("display: none !important", css)

    def test_login_restart_gate_strict(self):
        html = (ROOT / "app/web/templates/login.html").read_text(encoding="utf-8")
        self.assertIn("restarting') != '1'", html)
        self.assertIn("restarting') == '1'", html)


class DeleteCascadeSourceTests(unittest.TestCase):
    def test_delete_bot_user_clears_reseller_fks(self):
        src = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        fn = src[src.find("async def delete_bot_user") : src.find("def friendly_user_delete_error")]
        for needle in (
            "ResellerSetting",
            "ResellerBillingTransaction",
            "ResellerBillingRate",
            "owner_reseller_id",
            "PanelTicket",
            "TrialClaim",
        ):
            self.assertIn(needle, fn, needle)

    def test_friendly_fk_message(self):
        from app.services.users import friendly_user_delete_error

        msg = friendly_user_delete_error(
            Exception("FOREIGN KEY constraint failed (sqlite3.IntegrityError)")
        )
        self.assertIn("کلید خارجی", msg)
        self.assertNotIn("SQL:", msg)


class AdminResellerUnifySourceTests(unittest.TestCase):
    def test_create_admin_as_reseller_path(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        fn = src[
            src.find("async def pg_admins_create") : src.find(
                "async def pg_admins_web_access_legacy"
            )
        ]
        self.assertIn("as_reseller", fn)
        self.assertIn("provision_existing_pg_admin", fn)
        self.assertIn("grant_web_access", fn)

    def test_convert_route_and_service(self):
        pages = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("async def pg_admins_convert_to_reseller", pages)
        self.assertIn("convert_staff_to_reseller", pages)
        res = (ROOT / "app/services/resellers.py").read_text(encoding="utf-8")
        self.assertIn("async def convert_staff_to_reseller", res)

    def test_delete_admin_cascades_reseller(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        fn = src[src.find("async def pg_admins_delete") :]
        self.assertIn("reseller_by_pg_username", fn)
        self.assertIn("revoke_reseller", fn)

    def test_template_has_switch_and_convert(self):
        html = (ROOT / "app/web/templates/pg_admins.html").read_text(encoding="utf-8")
        self.assertIn("as_reseller", html)
        self.assertIn("تبدیل به نماینده", html)
        self.assertIn("convert-to-reseller", html)
        self.assertIn("ادمین + نماینده", html)

    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")


class ConvertStaffTests(unittest.IsolatedAsyncioTestCase):
    async def test_convert_requires_staff(self):
        from app.services import resellers as res_mod

        session = MagicMock()
        with patch(
            "app.services.pg_staff_access.access_by_pg_username",
            new=AsyncMock(return_value=None),
        ):
            profile, hint, err = await res_mod.convert_staff_to_reseller(
                session, pg_username="shop1", plan_id=1, password="AaBb12!secret"
            )
        self.assertIsNone(profile)
        self.assertIsNone(hint)
        self.assertIn("ادمین فرعی", err or "")

    async def test_convert_revokes_then_provisions(self):
        from app.services import resellers as res_mod

        session = AsyncMock()
        session.rollback = AsyncMock()
        staff = MagicMock()
        staff.web_username = "shop1"
        staff.pg_admin_password_enc = "enc"
        fake_profile = MagicMock()

        with (
            patch(
                "app.services.pg_staff_access.access_by_pg_username",
                new=AsyncMock(return_value=staff),
            ),
            patch(
                "app.services.pg_staff_access.revoke_web_access",
                new=AsyncMock(return_value=True),
            ) as rev,
            patch(
                "app.services.secret_box.decrypt_secret",
                return_value="AaBb12!secret",
            ),
            patch.object(
                res_mod,
                "provision_existing_pg_admin",
                new=AsyncMock(return_value=(fake_profile, "shop-settings?tab=bot", None)),
            ) as prov,
        ):
            profile, hint, err = await res_mod.convert_staff_to_reseller(
                session, pg_username="Shop1", plan_id=7, password=""
            )
        self.assertIsNone(err)
        self.assertIs(profile, fake_profile)
        self.assertEqual(hint, "shop-settings?tab=bot")
        rev.assert_awaited()
        self.assertEqual(rev.await_args.kwargs.get("commit"), False)
        prov.assert_awaited()
        self.assertEqual(prov.await_args.kwargs["plan_id"], 7)


if __name__ == "__main__":
    unittest.main()
