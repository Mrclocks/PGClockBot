"""Phase 1F — PasarGuard object scope + credential/cache isolation."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.pg_object_scope import (
    assert_owned_host,
    assert_owned_node,
    inbound_tag_allowed,
    pg_object_in_staff_scope,
)
from app.services.plans_catalog import (
    groups_allowed_for_staff,
    template_allowed_for_staff,
)
import app.services.pasarguard as pasarguard_mod
from tests.postgres_support import scoped_async_engine


class PgObjectScopePureTests(unittest.TestCase):
    def test_a_principal_a_cannot_see_b_host(self) -> None:
        staff_a = {
            "pg_admin_username": "admin_a",
            "pg_is_owner": False,
            "role": "reseller",
            "bot_user_id": 10,
        }
        foreign = {"id": 99, "admin": {"username": "admin_b"}}
        self.assertFalse(pg_object_in_staff_scope(foreign, staff_a))

    def test_b_cannot_modify_foreign_node_by_id(self) -> None:
        staff = {
            "pg_admin_username": "admin_a",
            "pg_is_owner": False,
            "role": "reseller",
            "bot_user_id": 10,
        }
        foreign = {"id": 5, "owner_username": "admin_b"}
        self.assertFalse(pg_object_in_staff_scope(foreign, staff))

    def test_c_missing_allow_list_deny_non_owner(self) -> None:
        # Hybrid limited / untrusted: missing allow-list ≠ unrestricted
        hybrid = {"role": "admin", "pg_is_owner": False, "pg_access": {}}
        self.assertFalse(template_allowed_for_staff(hybrid, 1))
        self.assertFalse(groups_allowed_for_staff(hybrid, [1]))
        # pg_staff without credentials
        staff = {"role": "pg_staff", "pg_access": {}, "pg_credentials_ready": False}
        self.assertFalse(template_allowed_for_staff(staff, 1))
        # Invalid allow-list payload → empty deny
        bad = {
            "role": "reseller",
            "bot_user_id": 3,
            "pg_access": {"allowed_template_ids": "not-a-list"},
        }
        self.assertFalse(template_allowed_for_staff(bad, 1))

    def test_d_unknown_ownership_deny(self) -> None:
        staff = {
            "pg_admin_username": "admin_a",
            "pg_is_owner": False,
            "role": "reseller",
            "bot_user_id": 10,
        }
        opaque = {"id": 7}  # no owner field, no list proof
        self.assertFalse(pg_object_in_staff_scope(opaque, staff))
        self.assertFalse(pg_object_in_staff_scope(opaque, staff, listed_ids=set()))
        # Proven via own-client list
        self.assertTrue(pg_object_in_staff_scope(opaque, staff, listed_ids={7}))

    def test_j_role_names_irrelevant(self) -> None:
        for name in ("Operator", "admin", "RoleX", "whatever"):
            staff = {
                "role": "reseller",
                "bot_user_id": 10,
                "pg_admin_username": "alice",
                "pg_is_owner": False,
                "pg_role_name": name,
            }
            own = {"id": 1, "admin": {"username": "alice"}}
            other = {"id": 2, "admin": {"username": "bob"}}
            self.assertTrue(pg_object_in_staff_scope(own, staff))
            self.assertFalse(pg_object_in_staff_scope(other, staff))

    def test_k_same_pg_role_isolated(self) -> None:
        a = {
            "role": "reseller",
            "bot_user_id": 10,
            "pg_admin_username": "alice",
            "pg_is_owner": False,
            "pg_role_name": "RoleX",
        }
        b = {
            "role": "reseller",
            "bot_user_id": 20,
            "pg_admin_username": "bob",
            "pg_is_owner": False,
            "pg_role_name": "RoleX",
        }
        ha = {"id": 1, "admin": {"username": "alice"}}
        hb = {"id": 2, "admin": {"username": "bob"}}
        self.assertTrue(pg_object_in_staff_scope(ha, a))
        self.assertFalse(pg_object_in_staff_scope(hb, a))
        self.assertTrue(pg_object_in_staff_scope(hb, b))

    def test_l_owner_platform_compatible(self) -> None:
        owner = {"role": "admin", "pg_is_owner": True, "pg_admin_username": "root"}
        self.assertTrue(pg_object_in_staff_scope({"id": 1}, owner))
        self.assertTrue(template_allowed_for_staff(owner, 999))
        self.assertTrue(groups_allowed_for_staff(owner, [1, 2, 3]))

    def test_m_unknown_not_platform(self) -> None:
        # Opaque object must not be treated as Owner/platform for limited staff
        limited = {"role": "admin", "pg_is_owner": False, "pg_admin_username": "lim"}
        self.assertFalse(pg_object_in_staff_scope({"id": 1}, limited))

    def test_inbound_tag_allowlist(self) -> None:
        self.assertTrue(inbound_tag_allowed("vless", {"vless", "vmess"}))
        self.assertFalse(inbound_tag_allowed("spoof", {"vless"}))
        self.assertFalse(inbound_tag_allowed("vless", None))


class AssertOwnedHostNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_id_tamper_host_denied(self) -> None:
        staff = {
            "role": "reseller",
            "bot_user_id": 10,
            "pg_admin_username": "admin_a",
            "pg_is_owner": False,
        }
        pg = MagicMock()
        pg.get_host = AsyncMock(
            return_value={"id": 99, "admin": {"username": "admin_b"}}
        )
        pg.get_hosts = AsyncMock(return_value=[{"id": 1, "admin": {"username": "admin_a"}}])
        got = await assert_owned_host(pg, staff, 99)
        self.assertIsNone(got)

    async def test_b_id_tamper_node_denied(self) -> None:
        staff = {
            "role": "reseller",
            "bot_user_id": 10,
            "pg_admin_username": "admin_a",
            "pg_is_owner": False,
        }
        pg = MagicMock()
        pg.get_node = AsyncMock(return_value={"id": 5, "owner_username": "admin_b"})
        pg.get_nodes = AsyncMock(return_value=[])
        got = await assert_owned_node(pg, staff, 5)
        self.assertIsNone(got)

    async def test_own_host_via_list_proof(self) -> None:
        staff = {
            "role": "reseller",
            "bot_user_id": 10,
            "pg_admin_username": "admin_a",
            "pg_is_owner": False,
        }
        pg = MagicMock()
        pg.get_host = AsyncMock(return_value={"id": 3})  # no owner field
        pg.get_hosts = AsyncMock(return_value=[{"id": 3}, {"id": 4}])
        got = await assert_owned_host(pg, staff, 3)
        self.assertEqual(got["id"], 3)

    async def test_l_owner_gets_host(self) -> None:
        staff = {"role": "admin", "pg_is_owner": True}
        pg = MagicMock()
        pg.get_host = AsyncMock(return_value={"id": 8})
        got = await assert_owned_host(pg, staff, 8)
        self.assertEqual(got["id"], 8)
        pg.get_hosts.assert_not_called()


class CredentialIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_e_missing_credentials_deny(self) -> None:
        from app.services.pasarguard import PasarGuardError, get_pg_for_staff

        session = AsyncMock()
        session.get = AsyncMock(
            return_value=MagicMock(
                id=1,
                is_active=True,
                pg_username="s1",
                pg_admin_password_enc=None,
            )
        )
        with patch("app.services.secret_box.decrypt_secret", return_value=""):
            with self.assertRaises(PasarGuardError):
                await get_pg_for_staff(session, staff_id=1)

    async def test_f_staff_pg_no_owner_fallback(self) -> None:
        from app.api.pg_pages import _staff_pg
        from app.services.pasarguard import PasarGuardError

        staff = {"role": "pg_staff", "pg_admin_username": "s1", "pg_staff_id": 1}
        owner = MagicMock()
        with (
            patch("app.api.pg_pages.get_pg", return_value=owner),
            patch(
                "app.services.pasarguard.get_pg_for_staff",
                new=AsyncMock(side_effect=PasarGuardError("no creds")),
            ),
        ):
            with self.assertRaises(PasarGuardError):
                await _staff_pg(AsyncMock(), staff)
        owner.get_user_by_id = AsyncMock()  # ensure we didn't use owner for staff path


class StaffCacheIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        pasarguard_mod.reset_pg()

    async def asyncTearDown(self):
        pasarguard_mod.reset_pg()
        await self.engine.dispose()

    async def test_g_h_cache_key_isolates_siblings(self) -> None:
        from app.db.models import PgStaffAccess
        from app.services.secret_box import encrypt_secret

        async with self.Session() as session:
            a = PgStaffAccess(
                pg_username="same_name",
                web_username="a",
                web_password_hash="x",
                pg_admin_password_enc=encrypt_secret("pass_a"),
                is_active=True,
            )
            b = PgStaffAccess(
                pg_username="same_name_b",
                web_username="b",
                web_password_hash="y",
                pg_admin_password_enc=encrypt_secret("pass_b"),
                is_active=True,
            )
            session.add_all([a, b])
            await session.commit()
            await session.refresh(a)
            await session.refresh(b)

            clients = []

            class _FakeClient:
                def __init__(self, username, password):
                    self.username = username
                    self.password = password
                    self._token = "tok"

                async def ensure_token(self):
                    return None

            def _factory(*, username=None, password=None, **kw):
                c = _FakeClient(username, password)
                clients.append(c)
                return c

            with patch.object(pasarguard_mod, "PasarGuardClient", side_effect=_factory):
                c1 = await pasarguard_mod.get_pg_for_staff(session, staff_id=a.id)
                c2 = await pasarguard_mod.get_pg_for_staff(session, staff_id=b.id)
                c1b = await pasarguard_mod.get_pg_for_staff(session, staff_id=a.id)

            self.assertIsNot(c1, c2)
            self.assertIs(c1, c1b)
            keys = list(pasarguard_mod._pg_staff_cache.keys())
            self.assertIn((a.id, "same_name"), keys)
            self.assertIn((b.id, "same_name_b"), keys)
            # Must not key by username alone
            self.assertTrue(all(isinstance(k, tuple) and len(k) == 2 for k in keys))


class PgUnavailableGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_i_unreachable_denies(self) -> None:
        from app.services.pg_staff_access import (
            PG_UNAVAILABLE_MSG,
            enforce_pg_admin_web_gate,
        )

        with patch(
            "app.services.pg_staff_access.fetch_pg_admin_gate",
            new=AsyncMock(return_value=("unreachable", None)),
        ), patch(
            "app.services.pg_staff_access.revoke_web_access", new=AsyncMock()
        ) as revoke:
            ok, msg = await enforce_pg_admin_web_gate(
                AsyncMock(), f"1f_unreachable_{id(self)}"
            )
        self.assertFalse(ok)
        self.assertEqual(msg, PG_UNAVAILABLE_MSG)
        revoke.assert_not_called()


class ResellerAllowListCompatTests(unittest.TestCase):
    def test_reseller_with_credentials_none_allow_list_ok(self) -> None:
        staff = {
            "role": "reseller",
            "bot_user_id": 3,
            "pg_access": {},
            "pg_credentials_ready": True,
        }
        self.assertTrue(template_allowed_for_staff(staff, 9))
        self.assertTrue(groups_allowed_for_staff(staff, [1]))


if __name__ == "__main__":
    unittest.main()
