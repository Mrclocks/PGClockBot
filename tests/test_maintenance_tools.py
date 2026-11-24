"""«نگهداری سرور»: owner only; clearing caches empties the data caches and is recorded; reconnecting restarts every
active shop bot once in the background and refuses a second run while the first one is still going."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BotUser, ResellerProfile, ShopStaffAction
from app.services import maintenance
from app.services import users as users_service
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness


class MaintenanceHarness(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        maintenance._reconnect_task = None
        maintenance._last_run = None

    async def call(self, session_payload: dict, method: str, path: str, *, csrf: bool = True):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "origin": "http://panel.test"}
        if csrf:
            headers["x-csrf-token"] = "token"
        return await self.client.request(method, path, headers=headers)

    async def seed_shops(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([BotUser(id=uid, telegram_id=900 + uid, referral_code=f"s{uid}", role="reseller")
                             for uid in (21, 22, 23)])
            await session.flush()
            session.add_all([
                ResellerProfile(id=71, user_id=21, is_active=True, setup_completed_at=now, bot_token="t1"),
                ResellerProfile(id=72, user_id=22, is_active=True, setup_completed_at=now, bot_token="t2"),
                ResellerProfile(id=73, user_id=23, is_active=False, setup_completed_at=now, bot_token="t3"),
            ])
            await session.commit()

    async def actions(self) -> list[str]:
        async with self.Session() as session:
            return [row.reason for row in await session.scalars(select(ShopStaffAction).order_by(ShopStaffAction.id))]





class MaintenancePageTests(MaintenanceHarness):
    async def test_the_owner_sees_both_tools_and_the_list_offers_the_page(self) -> None:
        await self.seed_shops()
        with patch.object(maintenance, "get_reseller_bot_manager", return_value=SimpleNamespace(running_count=lambda: 1)):
            page = (await self.call(OWNER_SESSION, "GET", "/tools/maintenance")).text
        self.assertIn("۱ از ۲ ربات در حال اجرا", page)
        self.assertIn('action="/tools/maintenance/reconnect-bots"', page)
        self.assertIn('action="/tools/maintenance/clear-caches"', page)
        self.assertIn('data-confirm-title="پاک کردن کش‌ها"', page)
        self.assertIn('href="/tools/maintenance"', (await self.call(OWNER_SESSION, "GET", "/tools")).text)

    async def test_with_no_active_shop_there_is_nothing_to_reconnect(self) -> None:
        with patch.object(maintenance, "get_reseller_bot_manager", return_value=SimpleNamespace(running_count=lambda: 0)):
            page = (await self.call(OWNER_SESSION, "GET", "/tools/maintenance")).text
        self.assertNotIn('action="/tools/maintenance/reconnect-bots"', page)

    async def test_a_shop_cannot_open_or_use_the_tools(self) -> None:
        for method, path in (("GET", "/tools/maintenance"), ("POST", "/tools/maintenance/clear-caches"),
                             ("POST", "/tools/maintenance/reconnect-bots")):
            with self.subTest(path=path):
                response = await self.call(SHOP_SESSION, method, path)
                self.assertEqual((response.status_code, response.headers["location"]), (303, "/home"))
        self.assertEqual(await self.actions(), [])


class ClearCachesTests(MaintenanceHarness):
    async def test_clearing_empties_the_settings_cache_and_is_recorded(self) -> None:
        users_service._SETTINGS_CACHE[0] = (0.0, {"k": "v"})
        response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/clear-caches")
        self.assertIn("/tools/maintenance?ok=", response.headers["location"])
        self.assertEqual(users_service._SETTINGS_CACHE, {})
        self.assertEqual(await self.actions(), ["پاک کردن کش‌های پنل"])

    async def test_a_post_without_the_csrf_token_clears_nothing(self) -> None:
        users_service._SETTINGS_CACHE[0] = (0.0, {"k": "v"})
        self.addCleanup(users_service._SETTINGS_CACHE.clear)
        response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/clear-caches", csrf=False)
        self.assertEqual(response.status_code, 403)
        self.assertIn(0, users_service._SETTINGS_CACHE)

    def test_one_cache_that_fails_does_not_stop_the_others(self) -> None:
        cleared = MagicMock()
        broken = MagicMock(side_effect=RuntimeError("boom"), __name__="broken")
        with patch.object(maintenance, "CACHE_CLEARERS", (broken, cleared)):
            self.assertEqual(maintenance.clear_caches(), 1)
        cleared.assert_called_once_with()


class ReconnectTests(MaintenanceHarness):
    async def test_every_active_shop_bot_is_restarted_once_in_the_background(self) -> None:
        await self.seed_shops()
        manager = SimpleNamespace(restart_reseller_profile=AsyncMock(side_effect=[True, False]), running_count=lambda: 1)
        with patch.object(maintenance, "get_reseller_bot_manager", return_value=manager), \
                patch.object(maintenance, "SessionLocal", self.Session):
            response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/reconnect-bots")
            self.assertIn("/tools/maintenance?ok=", response.headers["location"])
            await maintenance._reconnect_task
        restarted = sorted(call.args[0] for call in manager.restart_reseller_profile.await_args_list)
        self.assertEqual(restarted, [71, 72])
        self.assertEqual((maintenance._last_run.total, maintenance._last_run.failed), (2, 1))
        self.assertEqual(await self.actions(), ["اتصال دوبارهٔ ربات فروشگاه‌ها"])

    async def test_a_second_press_while_running_is_refused(self) -> None:
        await self.seed_shops()
        release = asyncio.Event()

        async def slow_restart(_profile_id: int) -> bool:
            await release.wait()
            return True

        manager = SimpleNamespace(restart_reseller_profile=slow_restart, running_count=lambda: 0)
        with patch.object(maintenance, "get_reseller_bot_manager", return_value=manager), \
                patch.object(maintenance, "SessionLocal", self.Session):
            await self.call(OWNER_SESSION, "POST", "/tools/maintenance/reconnect-bots")
            second = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/reconnect-bots")
            release.set()
            await maintenance._reconnect_task
        self.assertIn("/tools/maintenance?err=", second.headers["location"])
        self.assertEqual(await self.actions(), ["اتصال دوبارهٔ ربات فروشگاه‌ها"])

    async def test_without_a_bot_manager_nothing_starts(self) -> None:
        with patch.object(maintenance, "get_reseller_bot_manager", return_value=None):
            response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/reconnect-bots")
        self.assertIn("/tools/maintenance?err=", response.headers["location"])
        self.assertIsNone(maintenance._reconnect_task)
