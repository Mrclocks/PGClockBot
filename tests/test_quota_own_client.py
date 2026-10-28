"""Quota load/enforce must use the actor's own PG client — never Owner get_pg()."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pg_quota import (
    PgQuotaError,
    assert_can_create_user,
    assert_reseller_can_deliver,
    load_staff_limit_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
GB = 1024**3


def _own_client(admin: dict, role: dict | None = None):
    client = AsyncMock()
    client.get_current_admin = AsyncMock(return_value=admin)
    client.get_admin = AsyncMock(return_value=admin)
    client.get_admin_role = AsyncMock(return_value=role or {"limits": {}})
    return client


class QuotaOwnClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_reseller_create_does_not_call_owner_get_pg(self):
        admin = {
            "username": "shop1",
            "status": "active",
            "total_users": 1,
            "permission_overrides": {"max_users": 10},
        }
        client = _own_client(admin)
        with patch("app.services.pasarguard.get_pg") as get_pg:
            await assert_can_create_user(
                {"role": "reseller", "pg_admin_username": "shop1", "bot_user_id": 9},
                from_template=True,
                client=client,
            )
            get_pg.assert_not_called()

    async def test_reseller_without_client_fails_closed_without_owner(self):
        with patch("app.services.pasarguard.get_pg") as get_pg:
            with self.assertRaises(PgQuotaError) as ctx:
                await assert_can_create_user(
                    {
                        "role": "reseller",
                        "pg_admin_username": "shop1",
                        "bot_user_id": 9,
                    },
                    from_template=True,
                )
            get_pg.assert_not_called()
        self.assertTrue(ctx.exception.message)

    async def test_rejects_client_authenticated_as_someone_else(self):
        client = AsyncMock()
        client.get_current_admin = AsyncMock(
            return_value={"username": "owner", "status": "active"}
        )
        with patch("app.services.pasarguard.get_pg") as get_pg:
            with self.assertRaises(PgQuotaError) as ctx:
                await assert_can_create_user(
                    {"role": "reseller", "pg_admin_username": "shop1"},
                    from_template=True,
                    client=client,
                )
            get_pg.assert_not_called()
        self.assertIn("shop1", ctx.exception.message)
        self.assertIn("owner", ctx.exception.message)

    async def test_deliver_uses_shop_client_not_owner(self):
        admin = {
            "username": "shop1",
            "status": "active",
            "total_users": 0,
            "permission_overrides": {"max_users": 5},
        }
        shop_client = _own_client(admin)
        session = MagicMock()
        session.scalar = AsyncMock(return_value=0)
        with patch("app.services.pasarguard.get_pg") as get_pg:
            with patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=shop_client),
            ) as for_shop:
                await assert_reseller_can_deliver(
                    pg_admin_username="shop1",
                    from_template=True,
                    session=session,
                    reseller_user_id=44,
                )
            get_pg.assert_not_called()
            for_shop.assert_awaited_once_with(session, 44)

    async def test_snapshot_uses_injected_client(self):
        admin = {
            "username": "shop1",
            "status": "active",
            "total_users": 2,
            "data_limit": 10 * GB,
            "used_traffic": 1 * GB,
        }
        client = _own_client(admin, {"limits": {"max_users": 8}})
        with patch("app.services.pasarguard.get_pg") as get_pg:
            snap = await load_staff_limit_snapshot(
                {"role": "reseller", "pg_admin_username": "shop1"},
                client=client,
            )
            get_pg.assert_not_called()
        self.assertTrue(snap["restricted"])
        self.assertEqual(snap["current_users"], 2)


class QuotaOwnClientSourceTests(unittest.TestCase):
    def test_load_admin_does_not_call_get_pg_directly(self):
        src = (ROOT / "app/services/pg_quota.py").read_text(encoding="utf-8")
        fn = src.split("async def _load_admin_and_role", 1)[1].split(
            "async def load_staff_limit_snapshot", 1
        )[0]
        self.assertNotIn("get_pg()", fn)
        self.assertIn("_quota_pg_client", fn)

    def test_quota_client_owner_token_only_for_env_actor(self):
        src = (ROOT / "app/services/pg_quota.py").read_text(encoding="utf-8")
        fn = src.split("async def _quota_pg_client", 1)[1].split(
            "async def _client_for_named_pg_admin", 1
        )[0]
        self.assertIn("_staff_uses_env_pg_client", fn)
        self.assertIn("staff_pg_read_client", fn)
        self.assertIn("get_pg()", fn)

    def test_named_admin_helper_never_get_pg(self):
        src = (ROOT / "app/services/pg_quota.py").read_text(encoding="utf-8")
        fn = src.split("async def _client_for_named_pg_admin", 1)[1].split(
            "def _admin_username", 1
        )[0]
        self.assertNotIn("get_pg()", fn)
        self.assertIn("get_pg_for_reseller", fn)
        self.assertIn("get_pg_for_staff", fn)

    def test_provision_gate_threads_session(self):
        src = (ROOT / "app/services/provision_gate.py").read_text(encoding="utf-8")
        self.assertIn("session=session", src)
        self.assertIn("reseller_user_id=rid", src)


if __name__ == "__main__":
    unittest.main()
