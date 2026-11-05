"""Admin renew stays a fresh start; plan carry only affects customer renewals."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Plan, UserService
from app.services.bot_user_admin import admin_renew_service

GB = 1024**3
DAY = 86400


class FakePG:
    def __init__(self):
        now = int(time.time())
        self.info = {
            "status": "active",
            "data_limit": 10 * GB,
            "used_traffic": 3 * GB,
            "expire": now + 5 * DAY,
        }
        self.writes: list[dict] = []
        self.resets = 0

    async def get_user_by_id(self, user_id):
        return dict(self.info)

    async def modify_user_by_id(self, user_id, payload):
        self.writes.append(dict(payload))
        self.info.update(payload)
        return dict(self.info)

    async def reset_user_by_id(self, user_id):
        self.resets += 1
        self.info["used_traffic"] = 0


class AdminRenewFreshStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add(BotUser(id=1, telegram_id=101, referral_code="u1"))
        self.carry_plan = Plan(
            id=1,
            name="با انتقال برای مشتری",
            price=100_000,
            duration_days=30,
            data_limit_gb=10,
            carry_remaining_quota=True,
            is_active=True,
        )
        self.other_plan = Plan(
            id=2,
            name="پلن دیگر",
            price=120_000,
            duration_days=30,
            data_limit_gb=20,
            carry_remaining_quota=True,
            is_active=True,
        )
        self.service = UserService(
            id=7,
            bot_user_id=1,
            pg_user_id=77,
            pg_username="u77",
            plan_id=1,
            remark="ok",
            shop_id=None,
        )
        self.session.add_all([self.carry_plan, self.other_plan, self.service])
        await self.session.commit()
        self.pg = FakePG()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_admin_renew_ignores_customer_carry_and_resets(self):
        before = int(time.time())
        with (
            patch("app.services.bot_user_admin.assert_staff_pg_user_action"),
            patch(
                "app.services.bot_user_admin._pg_client_for_bot_service",
                AsyncMock(return_value=self.pg),
            ),
            patch(
                "app.services.bot_user_admin.service_snapshot",
                AsyncMock(return_value=SimpleNamespace(service=self.service)),
            ),
            patch("app.services.bot_user_admin.release_volume_hold", AsyncMock()),
            patch("app.services.bot_user_admin._hold_quota_change", AsyncMock(return_value=None)),
            patch(
                "app.services.pg_access.staff_user_actions",
                return_value={"reset_usage": True, "update": True},
            ),
        ):
            await admin_renew_service(
                self.session,
                self.service,
                plan=self.carry_plan,
                reset_traffic=True,
                staff={"role": "admin"},
            )
        self.assertEqual(self.pg.resets, 1)
        self.assertEqual(self.pg.writes[0]["data_limit"], 10 * GB)
        raw_expire = self.pg.writes[0]["expire"]
        if isinstance(raw_expire, str):
            expire = int(
                datetime.fromisoformat(raw_expire.replace("Z", "+00:00")).timestamp()
            )
        else:
            expire = int(raw_expire)
        self.assertGreaterEqual(expire, before + 30 * DAY - 2)
        self.assertLessEqual(expire, before + 30 * DAY + 5)

    async def test_failed_panel_renew_restores_previous_plan_id(self):
        self.pg.modify_user_by_id = AsyncMock(side_effect=RuntimeError("panel down"))
        with (
            patch("app.services.bot_user_admin.assert_staff_pg_user_action"),
            patch(
                "app.services.bot_user_admin._pg_client_for_bot_service",
                AsyncMock(return_value=self.pg),
            ),
            patch("app.services.bot_user_admin.release_volume_hold", AsyncMock()),
            patch("app.services.bot_user_admin._hold_quota_change", AsyncMock(return_value=None)),
            patch(
                "app.services.pg_access.staff_user_actions",
                return_value={"reset_usage": True, "update": True},
            ),
        ):
            with self.assertRaises(RuntimeError):
                await admin_renew_service(
                    self.session,
                    self.service,
                    plan=self.other_plan,
                    reset_traffic=True,
                    staff={"role": "admin"},
                )
        self.assertEqual(self.service.plan_id, 1)


if __name__ == "__main__":
    unittest.main()
