"""3.7.4 — PG credential repair + UI gating (no Owner fallback)."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class Version374Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertGreaterEqual(tuple(int(x) for x in __version__.split(".")), (3, 7, 4))
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.7.4"', notes)


class CredentialReadinessTests(unittest.TestCase):
    def test_enc_has_secret(self):
        from app.services.pg_credentials import enc_has_secret
        from app.services.secret_box import encrypt_secret

        self.assertFalse(enc_has_secret(None))
        self.assertFalse(enc_has_secret(""))
        self.assertTrue(enc_has_secret(encrypt_secret("Aa1!bbbb")))

    def test_staff_pg_client_ready_flag(self):
        from app.services.pg_credentials import staff_pg_client_ready

        self.assertTrue(staff_pg_client_ready({"role": "admin"}))
        self.assertFalse(staff_pg_client_ready({"role": "reseller"}))
        self.assertFalse(staff_pg_client_ready({"role": "reseller", "pg_client_ready": False}))
        self.assertTrue(staff_pg_client_ready({"role": "reseller", "pg_client_ready": True}))

    def test_credential_status_needs_repair(self):
        from app.services.pg_credentials import (
            credential_status_for_reseller,
            credential_status_for_staff,
        )

        missing = SimpleNamespace(
            pg_admin_username="shop1",
            pg_admin_password_enc=None,
            pg_role_id=3,
        )
        st = credential_status_for_reseller(missing)
        self.assertTrue(st["needs_repair"])
        self.assertFalse(st["ready"])

        staff_missing = SimpleNamespace(pg_username="staff1", pg_password_enc=None)
        self.assertTrue(credential_status_for_staff(staff_missing)["needs_repair"])


class OldResellerWithoutPasswordTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_pg_for_reseller_fails_without_enc(self):
        from app.services.pasarguard import PasarGuardError, get_pg_for_reseller

        profile = SimpleNamespace(
            pg_admin_username="shop1",
            pg_admin_password_enc=None,
        )
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=profile))
        )
        with self.assertRaises(PasarGuardError):
            await get_pg_for_reseller(session, 42)

    async def test_overview_without_session_fails_closed(self):
        from app.services.pg_overview import build_reseller_pg_overview

        out = await build_reseller_pg_overview(
            {"role": "reseller", "pg_admin_username": "shop1"}
        )
        self.assertFalse(out["ready"])
        self.assertIsNotNone(out["error"])

    async def test_overview_never_calls_owner_for_reseller(self):
        from app.services.pasarguard import PasarGuardError
        from app.services.pg_overview import build_reseller_pg_overview

        with (
            patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("Owner must not be used"),
            ),
            patch(
                "app.services.pasarguard.get_pg_for_staff",
                new=AsyncMock(
                    side_effect=PasarGuardError("رمز پاسارگارد نماینده ذخیره نشده")
                ),
            ),
        ):
            out = await build_reseller_pg_overview(
                {"role": "reseller", "pg_admin_username": "shop1", "bot_user_id": 42},
                session=AsyncMock(),
            )
        self.assertFalse(out["ready"])
        self.assertIsNotNone(out["error"])


class ResellerAfterMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_stores_encrypted_password(self):
        from app.services.pg_credentials import repair_reseller_pg_credentials
        from app.services.secret_box import decrypt_secret

        profile = SimpleNamespace(
            user_id=9,
            pg_admin_username="shop1",
            pg_admin_password_enc=None,
            web_password_hash=None,
        )
        session = AsyncMock()
        session.commit = AsyncMock()
        with (
            patch("app.services.pasarguard.get_pg") as get_pg,
            patch("app.services.pasarguard.invalidate_reseller_pg_client"),
        ):
            get_pg.return_value.get_admin = AsyncMock(
                return_value={"username": "shop1"}
            )
            get_pg.return_value.modify_admin = AsyncMock()
            pwd, err = await repair_reseller_pg_credentials(
                session, profile, password="Aa1!repairOK"
            )
        self.assertIsNone(err)
        self.assertEqual(pwd, "Aa1!repairOK")
        self.assertEqual(decrypt_secret(profile.pg_admin_password_enc), "Aa1!repairOK")
        get_pg.return_value.modify_admin.assert_awaited_once()

    async def test_list_pg_works_after_credentials(self):
        from app.api.pg_pages import _list_pg

        client = object()
        with patch(
            "app.api.pg_pages.get_pg_for_staff",
            new=AsyncMock(return_value=(client, False)),
        ):
            out = await _list_pg(
                AsyncMock(),
                {"role": "reseller", "bot_user_id": 1, "pg_client_ready": True},
            )
        self.assertIs(out, client)


class RestrictedAdminNodePermissionTests(unittest.TestCase):
    def test_map_role_without_nodes_omits_pg_nodes(self):
        from app.services.pg_access import map_pg_role_to_features

        role = {
            "permissions": {
                "users": {"read": True},
                "system": {"read": True},
            }
        }
        feats = map_pg_role_to_features(role)
        self.assertIn("pg_users", feats)
        self.assertNotIn("pg_nodes", feats)

    def test_map_role_with_nodes_includes_pg_nodes(self):
        from app.services.pg_access import map_pg_role_to_features

        role = {
            "permissions": {
                "nodes": {"read": True},
                "users": {"read": True},
            }
        }
        feats = map_pg_role_to_features(role)
        self.assertIn("pg_nodes", feats)
        self.assertIn("pg_users", feats)

    def test_sidebar_gates_on_pg_client_ready(self):
        src = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("pg_client_ready", src)
        self.assertIn("pg_needs_credentials", src)
        self.assertIn("/pg/credentials-required", src)

    def test_require_pg_perm_blocks_without_ready(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        block = src[src.index("def require_pg_perm") : src.index("def require_pg_perm") + 1200]
        self.assertIn("pg_client_ready", block)
        self.assertIn("credentials-required", block)

    def test_no_owner_fallback_in_get_pg_for_staff(self):
        src = (ROOT / "app/services/pasarguard.py").read_text(encoding="utf-8")
        fn = src[src.index("async def get_pg_for_staff") : src.index("def public_pg_api_base")]
        staff_branch = fn[fn.index('if staff.get("role") == "pg_staff"') :]
        self.assertNotIn("get_pg()", staff_branch)
        self.assertIn("get_pg_for_staff_admin", staff_branch)


class RepairEndpointsPresentTests(unittest.TestCase):
    def test_admin_repair_routes(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("/repair-pg-credentials", src)
        self.assertIn("repair_pg_staff_credentials", src)
        self.assertIn("repair_reseller_pg_credentials", src)

    def test_reseller_edit_repair(self):
        src = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("repair-pg-credentials", src)
        html = (ROOT / "app/web/templates/reseller_edit.html").read_text(encoding="utf-8")
        self.assertIn("همگام‌سازی رمز پاسارگارد", html)


if __name__ == "__main__":
    unittest.main()
