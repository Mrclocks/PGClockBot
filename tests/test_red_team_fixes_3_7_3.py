"""3.7.3 — Red Team confirmed findings: regressions after security merge.

Priority fixes:
1. Owner credential leakage / pg_staff Owner-token fallback
2. Privilege escalation (host delete widening)
3. Cross-tenant list/mutate via Owner dump
4. Template quota bypass
5. Bot token privilege escalation (sub-admin)
6. Cookie PG ACL trust after role removal
7. Role cache invalidation
8. Billing negative balance race
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class Version373Tests(unittest.TestCase):
    def test_version_notes_retained(self):
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.7.3"', notes)
        from app.version import __version__

        self.assertGreaterEqual(tuple(int(x) for x in __version__.split(".")), (3, 7, 3))


class OwnerTokenPgStaffTests(unittest.IsolatedAsyncioTestCase):
    async def test_pg_staff_never_returns_owner_client(self):
        from app.services.pasarguard import PasarGuardError, get_pg_for_staff

        with (
            patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("Owner token must not be used"),
            ),
            patch(
                "app.services.pasarguard.get_pg_for_staff_admin",
                new=AsyncMock(side_effect=PasarGuardError("no stored password")),
            ),
        ):
            with self.assertRaises(PasarGuardError):
                await get_pg_for_staff(
                    AsyncMock(),
                    {"role": "pg_staff", "pg_admin_username": "staff1"},
                )

    async def test_pg_staff_admin_requires_stored_password(self):
        from app.services.pasarguard import PasarGuardError, get_pg_for_staff_admin

        row = SimpleNamespace(
            pg_username="staff1",
            is_active=True,
            pg_password_enc=None,
        )
        with patch(
            "app.services.pg_staff_access.access_by_pg_username",
            new=AsyncMock(return_value=row),
        ):
            with self.assertRaises(PasarGuardError) as ctx:
                await get_pg_for_staff_admin(AsyncMock(), "staff1")
        self.assertIn("ذخیره نشده", str(ctx.exception))

    def test_model_has_pg_password_enc(self):
        from app.db.models import PgStaffAccess

        self.assertTrue(hasattr(PgStaffAccess, "pg_password_enc"))
        mig = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("pg_password_enc", mig)


class CrossTenantListClientTests(unittest.TestCase):
    def test_list_endpoints_use_staff_scoped_client(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("async def _list_pg", src)
        for marker in (
            "await _list_pg(session, staff)",
            # Old Owner dump patterns on non-admin lists must be gone for these routes
        ):
            self.assertIn(marker, src)
        # hosts/nodes GET must not hardcode get_pg() for inventory
        hosts_start = src.index("async def pg_hosts")
        hosts_body = src[hosts_start : src.index("async def pg_hosts_create")]
        self.assertIn("_list_pg", hosts_body)
        self.assertNotIn("pg = get_pg()", hosts_body)

        nodes_start = src.index("async def pg_nodes")
        nodes_body = src[nodes_start : src.index("async def pg_node_reconnect")]
        self.assertIn("_list_pg", nodes_body)
        self.assertNotIn("get_pg().get_nodes()", nodes_body)


class HostDeletePermissionTests(unittest.TestCase):
    def test_delete_requires_hosts_delete_only(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        start = src.index("async def pg_hosts_delete")
        body = src[start : start + 500]
        self.assertIn('staff_pg_action(staff, "hosts", "delete")', body)
        self.assertNotIn(
            'staff_pg_action(staff, "hosts", "update")',
            body,
            msg="update must not widen to delete",
        )


class TemplateQuotaBypassTests(unittest.IsolatedAsyncioTestCase):
    async def test_from_template_without_limits_fails_when_max_set(self):
        from app.services.pg_quota import PgQuotaError, assert_can_create_user

        admin = {
            "username": "r1",
            "status": "active",
            "total_users": 0,
            "permission_overrides": {"data_limit_max": 1024**3},
        }
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value={"limits": {}})
            get_pg.return_value = client
            with self.assertRaises(PgQuotaError):
                await assert_can_create_user(
                    {"role": "pg_staff", "pg_admin_username": "r1"},
                    from_template=True,
                )

    def test_template_create_calls_assert_can_create_user(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        start = src.index("async def pg_templates_create")
        body = src[start : src.index("async def pg_templates_delete")]
        self.assertIn("assert_can_create_user", body)

    def test_user_create_from_template_loads_template_limits(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("get_user_template(tid)", src)
        self.assertIn("tpl_data_limit", src)


class BotTokenOwnerOnlyTests(unittest.TestCase):
    def test_token_handlers_require_shop_owner(self):
        src = (ROOT / "app/bot/handlers/reseller_settings.py").read_text(encoding="utf-8")
        self.assertIn("def _is_shop_owner", src)
        ask = src[src.index("async def bot_token_ask") : src.index("async def bot_token_save")]
        save = src[src.index("async def bot_token_save") : src.index("async def bot_token_save") + 900]
        self.assertIn("_is_shop_owner(db_user, profile)", ask)
        self.assertIn("_is_shop_owner(db_user, profile)", save)

    def test_sub_admin_rejected(self):
        from app.bot.handlers.reseller_settings import _is_shop_owner

        owner = SimpleNamespace(id=10)
        sub = SimpleNamespace(id=99)
        profile = SimpleNamespace(user_id=10)
        self.assertTrue(_is_shop_owner(owner, profile))
        self.assertFalse(_is_shop_owner(sub, profile))


class CookieAclTrustTests(unittest.TestCase):
    def test_require_staff_clears_stale_pg_acl(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('user.pop(stale_key, None)', src)
        self.assertIn('enrich_staff_pg_from_role(user, [], None)', src)

    def test_enrich_always_overwrites_acl_fields(self):
        from app.services.pg_access import enrich_staff_pg_from_role

        stale = {
            "role": "reseller",
            "pg_permissions": ["pg_hosts", "pg_nodes"],
            "pg_writes": {"hosts": True, "nodes": True},
            "pg_actions": {"hosts": {"delete": True}},
            "pg_user_actions": {"create": True},
            "pg_access": {"allowed_template_ids": [1]},
        }
        out = enrich_staff_pg_from_role(stale, [], None)
        self.assertEqual(out["pg_permissions"], [])
        self.assertFalse(out["pg_writes"].get("hosts"))
        self.assertFalse(out["pg_actions"]["hosts"].get("delete"))
        self.assertFalse(out["pg_user_actions"].get("create"))
        self.assertIsNone(out["pg_access"].get("allowed_template_ids"))

    def test_empty_web_permissions_lockdown_aligned(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("with_shop_settings(parsed) if parsed else parsed", src)


class RoleCacheTests(unittest.TestCase):
    def test_invalidate_role_cache_exists_and_works(self):
        from app.services import pg_access as pga

        self.assertTrue(hasattr(pga, "invalidate_role_cache"))
        pga._ROLE_CACHE[7] = (0.0, ["pg_users"], {"id": 7})
        pga.invalidate_role_cache(7)
        self.assertNotIn(7, pga._ROLE_CACHE)
        pga._ROLE_CACHE[8] = (0.0, [], {})
        pga.invalidate_role_cache()
        self.assertEqual(pga._ROLE_CACHE, {})

    def test_reseller_edit_invalidates_cache(self):
        src = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("invalidate_role_cache", src)


class BillingFloorTests(unittest.IsolatedAsyncioTestCase):
    async def test_debit_refuses_negative_balance(self):
        from app.services.billing import BillingError, debit_usage

        profile = SimpleNamespace(user_id=5, billing_balance=100)
        session = AsyncMock()
        session.no_autoflush = MagicMock()
        session.no_autoflush.__enter__ = MagicMock(return_value=None)
        session.no_autoflush.__exit__ = MagicMock(return_value=False)
        result = MagicMock(rowcount=0)
        session.execute = AsyncMock(return_value=result)
        session.refresh = AsyncMock()

        with (
            patch("app.services.billing._find_by_idempotency", new=AsyncMock(return_value=None)),
            patch("app.services.billing.bytes_cost_proportional", return_value=500),
        ):
            with self.assertRaises(BillingError) as ctx:
                await debit_usage(
                    session,
                    profile,
                    bytes_delta=1024**3,
                    rate_per_gb=1000,
                    watermark_after=1024**3,
                    idempotency_key="t1",
                )
        self.assertIn("موجودی", ctx.exception.message)

    def test_sql_has_balance_floor_predicate(self):
        src = (ROOT / "app/services/billing.py").read_text(encoding="utf-8")
        start = src.index("async def debit_usage")
        body = src[start : start + 1200]
        self.assertIn("billing_balance >= int(amount)", body)


class GrantSyncsPgPasswordTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_stores_encrypted_pg_password(self):
        from app.services.pg_staff_access import grant_web_access

        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        with (
            patch(
                "app.services.pg_staff_access.conflict_message_for_new_grant",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.pg_staff_access._username_taken",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.pg_staff_access._sync_pg_admin_password",
                new=AsyncMock(return_value=("enc-secret", None)),
            ),
            patch("app.services.pg_staff_access.hash_password", return_value="hash"),
        ):
            row, err = await grant_web_access(
                session,
                pg_username="staff1",
                web_username="webstaff",
                password="Aa1!aaaa",
            )
        self.assertIsNone(err)
        self.assertIsNotNone(row)
        self.assertEqual(row.pg_password_enc, "enc-secret")


if __name__ == "__main__":
    unittest.main()
