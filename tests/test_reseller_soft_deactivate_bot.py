"""Soft-deactivate stops the dedicated shop bot and middleware refuses inactive shops."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.middlewares import UserMiddleware
from app.db.models import Base, BotUser, ResellerProfile
from app.services.reseller_bots import (
    clear_reseller_token_cache,
    lookup_reseller_by_bot_token,
    sync_reseller_bot_runtime,
)


class SoftDeactivateBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller"),
                ResellerProfile(
                    id=7, user_id=3, is_active=True, bot_token="123:ABC",
                    bot_token_hash=None, setup_completed_at=__import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                ),
            ])
            await session.commit()
        clear_reseller_token_cache()

    async def asyncTearDown(self) -> None:
        clear_reseller_token_cache()
        await self.engine.dispose()

    async def test_sync_stops_bot_when_inactive(self) -> None:
        mgr = SimpleNamespace(stop_reseller=AsyncMock(), restart_reseller_profile=AsyncMock())
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 7)
            profile.is_active = False
            await session.commit()
            with patch("app.services.reseller_bots.get_reseller_bot_manager", return_value=mgr):
                await sync_reseller_bot_runtime(profile)
        mgr.stop_reseller.assert_awaited_once_with(7)
        mgr.restart_reseller_profile.assert_not_awaited()

    async def test_sync_restarts_bot_when_reactivated(self) -> None:
        mgr = SimpleNamespace(stop_reseller=AsyncMock(), restart_reseller_profile=AsyncMock(return_value=True))
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 7)
            with patch("app.services.reseller_bots.get_reseller_bot_manager", return_value=mgr):
                await sync_reseller_bot_runtime(profile)
        mgr.restart_reseller_profile.assert_awaited_once_with(7)
        mgr.stop_reseller.assert_not_awaited()

    async def test_lookup_ignores_inactive_shop(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 7)
            self.assertIsNotNone(await lookup_reseller_by_bot_token(session, "123:ABC"))
            profile.is_active = False
            await session.commit()
            clear_reseller_token_cache()
            self.assertIsNone(await lookup_reseller_by_bot_token(session, "123:ABC"))

    async def test_middleware_rejects_inactive_even_with_cached_attr(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 7)
            profile.is_active = False
            await session.commit()
        clear_reseller_token_cache()

        bot = MagicMock()
        bot.token = "123:ABC"
        bot._pgclock_reseller = {"profile_id": 7, "user_id": 3}
        event = MagicMock()
        event.message = None
        handler = AsyncMock(return_value="ok")
        data = {"session": await self.Session().__aenter__(), "bot": bot}

        with (
            patch("app.bot.middlewares.get_settings", return_value=SimpleNamespace(bot_token="MAIN:TOKEN")),
            patch("app.bot.middlewares._reply_message", return_value=None),
            patch("app.bot.middlewares._extract_from_user", return_value=None),
        ):
            result = await UserMiddleware()(handler, event, data)
        self.assertIsNone(result)
        handler.assert_not_awaited()
        await data["session"].close()


if __name__ == "__main__":
    unittest.main()
