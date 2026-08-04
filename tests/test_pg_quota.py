"""Tests for PasarGuard admin quota enforcement (web panel / shop)."""

from __future__ import annotations

import time
import unittest
from unittest.mock import AsyncMock, patch

from app.services.pg_quota import (
    PgQuotaError,
    assert_admin_can_write,
    assert_can_create_user,
    assert_can_modify_user,
    merge_role_limits,
    staff_needs_quota_check,
)


GB = 1024**3


class MergeLimitsTests(unittest.TestCase):
    def test_overrides_win(self):
        admin = {"permission_overrides": {"max_users": 2, "data_limit_max": 10 * GB}}
        role = {"limits": {"max_users": 100, "data_limit_max": 50 * GB, "expire_max": 86400}}
        limits = merge_role_limits(admin, role)
        self.assertEqual(limits["max_users"], 2)
        self.assertEqual(limits["data_limit_max"], 10 * GB)
        self.assertEqual(limits["expire_max"], 86400)

    def test_role_only(self):
        limits = merge_role_limits({}, {"limits": {"max_users": 5}})
        self.assertEqual(limits["max_users"], 5)


class StaffGateTests(unittest.TestCase):
    def test_owner_admin_skips(self):
        self.assertFalse(staff_needs_quota_check({"role": "admin", "pg_admin_username": "x"}))

    def test_reseller_needs_check(self):
        self.assertTrue(staff_needs_quota_check({"role": "reseller", "pg_admin_username": "r1"}))

    def test_pg_staff_needs_check(self):
        self.assertTrue(staff_needs_quota_check({"role": "pg_staff", "pg_admin_username": "a1"}))

    def test_reseller_without_pg_link_still_needs_check(self):
        """Missing PG link must not skip the gate (fail closed later)."""
        self.assertTrue(staff_needs_quota_check({"role": "reseller"}))
        self.assertTrue(staff_needs_quota_check({"role": "pg_staff", "pg_admin_username": ""}))


class WriteGateTests(unittest.TestCase):
    def test_limited_blocks_write(self):
        with self.assertRaises(PgQuotaError) as ctx:
            assert_admin_can_write({"username": "a", "status": "limited"}, None)
        self.assertIn("محدود", ctx.exception.message)

    def test_traffic_exhausted_blocks(self):
        with self.assertRaises(PgQuotaError):
            assert_admin_can_write(
                {"username": "a", "status": "active", "data_limit": 10 * GB, "used_traffic": 10 * GB},
                None,
            )

    def test_disabled_blocks(self):
        with self.assertRaises(PgQuotaError):
            assert_admin_can_write({"username": "a", "status": "disabled"}, None)

    def test_active_ok(self):
        assert_admin_can_write({"username": "a", "status": "active"}, None)


class CreateQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_bypass(self):
        await assert_can_create_user({"role": "admin"}, data_limit=None, expire_ts=None)

    async def test_max_users_reached(self):
        admin = {
            "username": "r1",
            "status": "active",
            "total_users": 2,
            "permission_overrides": {"max_users": 2},
        }
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value={"limits": {}})
            get_pg.return_value = client
            with self.assertRaises(PgQuotaError) as ctx:
                await assert_can_create_user(
                    {"role": "reseller", "pg_admin_username": "r1", "pg_role_id": 1},
                    data_limit=1 * GB,
                    expire_ts=int(time.time()) + 86400,
                )
            self.assertIn("سقف تعداد کاربران", ctx.exception.message)

    async def test_unlimited_volume_blocked_when_max_set(self):
        admin = {
            "username": "r1",
            "status": "active",
            "total_users": 0,
            "permission_overrides": {"max_users": 2, "data_limit_max": 10 * GB},
        }
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value={"limits": {}})
            get_pg.return_value = client
            with self.assertRaises(PgQuotaError) as ctx:
                await assert_can_create_user(
                    {"role": "reseller", "pg_admin_username": "r1"},
                    data_limit=None,  # unlimited
                    expire_ts=int(time.time()) + 86400,
                )
            self.assertIn("نامحدود", ctx.exception.message)

    async def test_volume_above_max(self):
        admin = {
            "username": "r1",
            "status": "active",
            "total_users": 0,
        }
        role = {"limits": {"data_limit_max": 10 * GB, "expire_max": 30 * 86400}}
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value=role)
            get_pg.return_value = client
            with self.assertRaises(PgQuotaError) as ctx:
                await assert_can_create_user(
                    {"role": "pg_staff", "pg_admin_username": "r1", "pg_role_id": 9},
                    data_limit=20 * GB,
                    expire_ts=int(time.time()) + 7 * 86400,
                )
            self.assertIn("بیشتر از", ctx.exception.message)

    async def test_expire_above_max(self):
        admin = {"username": "r1", "status": "active", "total_users": 0}
        role = {"limits": {"expire_max": 7 * 86400, "data_limit_max": 10 * GB}}
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value=role)
            get_pg.return_value = client
            with self.assertRaises(PgQuotaError) as ctx:
                await assert_can_create_user(
                    {"role": "reseller", "pg_admin_username": "r1", "pg_role_id": 1},
                    data_limit=5 * GB,
                    expire_ts=int(time.time()) + 30 * 86400,
                )
            self.assertIn("مدت", ctx.exception.message)

    async def test_template_create_enforces_volume_bounds(self):
        admin = {
            "username": "r1",
            "status": "active",
            "total_users": 1,
            "permission_overrides": {"max_users": 2, "data_limit_max": 1 * GB},
        }
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value={"limits": {}})
            get_pg.return_value = client
            # from_template must NOT skip volume/expire — unlimited omission fails closed
            with self.assertRaises(PgQuotaError) as ctx:
                await assert_can_create_user(
                    {"role": "reseller", "pg_admin_username": "r1"},
                    from_template=True,
                )
            self.assertIn("حجم", ctx.exception.message)

    async def test_template_create_ok_with_resolved_limits(self):
        admin = {
            "username": "r1",
            "status": "active",
            "total_users": 1,
            "permission_overrides": {"max_users": 2, "data_limit_max": 5 * GB},
        }
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value={"limits": {}})
            get_pg.return_value = client
            await assert_can_create_user(
                {"role": "reseller", "pg_admin_username": "r1"},
                data_limit=1 * GB,
                expire_ts=None,
                from_template=True,
            )

    async def test_valid_custom_create(self):
        admin = {"username": "r1", "status": "active", "total_users": 0}
        role = {
            "limits": {
                "max_users": 2,
                "data_limit_min": 1 * GB,
                "data_limit_max": 10 * GB,
                "expire_min": 86400,
                "expire_max": 30 * 86400,
            }
        }
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value=role)
            get_pg.return_value = client
            await assert_can_create_user(
                {"role": "reseller", "pg_admin_username": "r1", "pg_role_id": 1},
                data_limit=5 * GB,
                expire_ts=int(time.time()) + 14 * 86400,
            )


class ModifyQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_modify_volume_above_max(self):
        admin = {"username": "r1", "status": "active"}
        role = {"limits": {"data_limit_max": 10 * GB}}
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value=role)
            get_pg.return_value = client
            with self.assertRaises(PgQuotaError):
                await assert_can_modify_user(
                    {"role": "reseller", "pg_admin_username": "r1", "pg_role_id": 1},
                    data_limit=50 * GB,
                    expire_ts=int(time.time()) + 86400,
                )


class WiringTests(unittest.TestCase):
    def test_pg_pages_imports_quota(self):
        from pathlib import Path

        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("assert_provision_create", src)
        self.assertIn("assert_provision_modify", src)
        self.assertIn("assert_can_mutate_owned_users", src)
        self.assertIn("PgQuotaError", src)

    def test_orders_imports_quota(self):
        from pathlib import Path

        src = Path("app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("assert_provision_create", src)
        self.assertIn("assert_provision_renew", src)


if __name__ == "__main__":
    unittest.main()
