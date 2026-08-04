"""3.7.2 — Owner-token isolation, mutation authz, and privilege-escalation regressions.

Critical regression target: resellers/pg_staff must never mutate PasarGuard
resources under the platform owner token (previous unrestricted-create bug class).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class Version372Tests(unittest.TestCase):
    def test_version_notes_retained(self):
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.7.2"', notes)
        self.assertIn('"3.7.3"', notes)


class OwnerTokenMutationWiringTests(unittest.TestCase):
    """Static guards: web panel user mutations must route through staff PG client."""

    def test_pg_user_mutations_use_staff_pg(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        for marker in (
            "await pg.modify_user_by_id",
            "await pg.set_disabled_by_id",
            "await pg.reset_user_by_id",
            "await pg.revoke_sub_by_id",
            "await pg.delete_user_by_id",
        ):
            self.assertIn(marker, src, msg=f"missing staff-scoped call: {marker}")

        # Old owner-token mutation pattern must stay gone for disable/enable/etc.
        self.assertNotIn("await get_pg().set_disabled_by_id(user_id, True)", src)
        self.assertNotIn("await get_pg().set_disabled_by_id(user_id, False)", src)
        self.assertNotIn("await get_pg().reset_user_by_id(user_id)", src)
        self.assertNotIn("await get_pg().revoke_sub_by_id(user_id)", src)
        self.assertNotIn("await get_pg().delete_user_by_id(user_id)", src)
        self.assertNotIn("await get_pg().modify_user_by_id(user_id, payload)", src)

    def test_staff_pg_helper_uses_shared_get_pg_for_staff(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("get_pg_for_staff", src)
        self.assertIn("return await get_pg_for_staff(session, staff)", src)

    def test_assert_owned_user_prefers_staff_client(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        start = src.index("async def _assert_owned_user")
        body = src[start : start + 900]
        self.assertIn("_staff_pg(session, staff)", body)
        self.assertIn("session: AsyncSession | None = None", body)

    def test_plans_create_template_uses_staff_client(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        start = src.index("async def plans_create")
        end = src.index("async def plans_trial_save", start)
        body = src[start:end]
        self.assertIn("get_pg_for_staff(session, staff)", body)
        self.assertNotIn("await get_pg().create_user_template", body)

    def test_delete_bot_user_prefers_reseller_client(self):
        src = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        start = src.index("async def delete_bot_user")
        body = src[start : start + 4500]
        self.assertIn("get_pg_for_reseller", body)
        self.assertIn("owner_reseller_id", body)


class GetPgForStaffTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_gets_owner_client(self):
        from app.services.pasarguard import get_pg_for_staff

        owner = object()
        with patch("app.services.pasarguard.get_pg", return_value=owner):
            client, as_owner = await get_pg_for_staff(AsyncMock(), {"role": "admin"})
        self.assertIs(client, owner)
        self.assertTrue(as_owner)

    async def test_reseller_never_falls_back_to_owner(self):
        from app.services.pasarguard import get_pg_for_staff

        shop = object()
        session = AsyncMock()
        with (
            patch("app.services.pasarguard.get_pg", side_effect=AssertionError("owner used")),
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=shop),
            ) as mock_res,
        ):
            client, as_owner = await get_pg_for_staff(
                session, {"role": "reseller", "bot_user_id": 42}
            )
        self.assertIs(client, shop)
        self.assertFalse(as_owner)
        mock_res.assert_awaited_once_with(session, 42)

    async def test_reseller_without_shop_id_fails_closed(self):
        from app.services.pasarguard import PasarGuardError, get_pg_for_staff

        with self.assertRaises(PasarGuardError):
            await get_pg_for_staff(AsyncMock(), {"role": "reseller"})

    async def test_pg_staff_never_falls_back_to_owner(self):
        from app.services.pasarguard import PasarGuardError, get_pg_for_staff

        with (
            patch("app.services.pasarguard.get_pg", side_effect=AssertionError("owner used")),
            patch(
                "app.services.pasarguard.get_pg_for_staff_admin",
                new=AsyncMock(side_effect=PasarGuardError("no creds")),
            ),
        ):
            with self.assertRaises(PasarGuardError):
                await get_pg_for_staff(
                    AsyncMock(),
                    {"role": "pg_staff", "pg_admin_username": "staff1"},
                )

    async def test_pg_staff_uses_staff_admin_client(self):
        from app.services.pasarguard import get_pg_for_staff

        staff_client = object()
        with (
            patch("app.services.pasarguard.get_pg", side_effect=AssertionError("owner used")),
            patch(
                "app.services.pasarguard.get_pg_for_staff_admin",
                new=AsyncMock(return_value=staff_client),
            ) as mock_staff,
        ):
            client, as_owner = await get_pg_for_staff(
                AsyncMock(),
                {"role": "pg_staff", "pg_admin_username": "staff1"},
            )
        self.assertIs(client, staff_client)
        self.assertFalse(as_owner)
        mock_staff.assert_awaited_once()

    async def test_pg_staff_without_username_fails_closed(self):
        from app.services.pasarguard import PasarGuardError, get_pg_for_staff

        with self.assertRaises(PasarGuardError):
            await get_pg_for_staff(AsyncMock(), {"role": "pg_staff"})


class OwnershipFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_admin_field_denies_non_owner(self):
        from app.api.pg_pages import _assert_owned_user

        pg = AsyncMock()
        pg.get_user_by_id = AsyncMock(return_value={"id": 1, "admin": None, "username": "u"})
        with patch("app.api.pg_pages.get_pg", return_value=pg):
            out = await _assert_owned_user(
                {"role": "reseller", "pg_admin_username": "shop_a", "bot_user_id": 9},
                1,
                session=None,
            )
        self.assertIsNone(out)

    async def test_foreign_owner_denied(self):
        from app.api.pg_pages import _assert_owned_user

        pg = AsyncMock()
        pg.get_user_by_id = AsyncMock(
            return_value={"id": 1, "admin": "other_shop", "username": "u"}
        )
        with patch("app.api.pg_pages.get_pg", return_value=pg):
            out = await _assert_owned_user(
                {"role": "reseller", "pg_admin_username": "shop_a"},
                1,
                session=None,
            )
        self.assertIsNone(out)

    async def test_matching_owner_allowed(self):
        from app.api.pg_pages import _assert_owned_user

        pg = AsyncMock()
        info = {"id": 1, "admin": "shop_a", "username": "u"}
        pg.get_user_by_id = AsyncMock(return_value=info)
        with patch("app.api.pg_pages.get_pg", return_value=pg):
            out = await _assert_owned_user(
                {"role": "reseller", "pg_admin_username": "shop_a"},
                1,
                session=None,
            )
        self.assertEqual(out, info)


class GateCacheInvalidationTests(unittest.TestCase):
    def test_invalidate_clears_entry(self):
        from app.services import pg_staff_access as psa

        psa._PG_GATE_CACHE.clear()
        psa._PG_GATE_CACHE["alice"] = (0.0, (True, None))
        psa.invalidate_pg_gate_cache("alice")
        self.assertNotIn("alice", psa._PG_GATE_CACHE)

    def test_revoke_web_access_invalidates_cache(self):
        src = (ROOT / "app/services/pg_staff_access.py").read_text(encoding="utf-8")
        revoke = src[src.index("async def revoke_web_access") : src.index("async def set_active")]
        self.assertIn("invalidate_pg_gate_cache", revoke)
        active = src[src.index("async def set_active") : src.index("async def change_staff_credentials")]
        self.assertIn("invalidate_pg_gate_cache", active)


class ResellerPromotionCleanupTests(unittest.TestCase):
    def test_promote_to_admin_calls_revoke_reseller(self):
        src = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        start = src.index("if role == Role.ADMIN.value:")
        body = src[start : start + 900]
        self.assertIn("revoke_reseller(", body)
        self.assertIn("delete_pg_admin=True", body)
        # Old soft-disable pattern must not remain
        self.assertNotIn("profile.is_active = False", body)


class ResellerClientCacheTests(unittest.TestCase):
    def test_ttl_and_invalidate_helpers_exist(self):
        from app.services import pasarguard as pg

        self.assertTrue(hasattr(pg, "invalidate_reseller_pg_client"))
        self.assertTrue(hasattr(pg, "_RESELLER_CLIENT_TTL_SEC"))
        pg._pg_reseller_cache[1] = object()
        pg._pg_reseller_cache_ts[1] = 0.0
        pg.invalidate_reseller_pg_client(1)
        self.assertNotIn(1, pg._pg_reseller_cache)
        self.assertNotIn(1, pg._pg_reseller_cache_ts)


class BotAclRegressionTests(unittest.TestCase):
    def test_adm_ticket_reply_rechecks_admin(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        start = src.index("async def adm_ticket_reply")
        body = src[start : start + 600]
        self.assertIn("if not _is_admin(db_user):", body)

    def test_settings_edit_save_revalidates_fields(self):
        src = (ROOT / "app/bot/handlers/reseller_settings.py").read_text(encoding="utf-8")
        start = src.index("async def settings_edit_save")
        body = src[start : start + 1200]
        self.assertIn("if key not in FIELDS:", body)


class PasarGuardClientNoSilentOwnerFallbackTests(unittest.TestCase):
    def test_explicit_username_does_not_load_owner_token(self):
        from app.services.pasarguard import PasarGuardClient

        with patch("app.services.pasarguard.get_settings") as gs:
            gs.return_value = SimpleNamespace(
                pg_base_url="https://pg.example",
                pg_access_token="OWNER_TOKEN",
                pg_username="owner",
                pg_password="secret",
            )
            client = PasarGuardClient(username="shop_admin", password="shop_pw")
        self.assertEqual(client._login_username, "shop_admin")
        self.assertIsNone(client._token)

    def test_owner_client_uses_env_token(self):
        from app.services.pasarguard import PasarGuardClient

        with patch("app.services.pasarguard.get_settings") as gs:
            gs.return_value = SimpleNamespace(
                pg_base_url="https://pg.example",
                pg_access_token="OWNER_TOKEN",
                pg_username="owner",
                pg_password="secret",
            )
            client = PasarGuardClient()
        self.assertEqual(client._token, "OWNER_TOKEN")
        self.assertIsNone(client._login_username)


if __name__ == "__main__":
    unittest.main()
