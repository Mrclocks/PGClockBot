"""Settings «لینک‌های سریع»: a shop's bot username is looked up with its plain token, not the sealed one."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, ResellerProfile
from app.services.secret_box import seal_bot_token
from app.services.ux20 import build_magic_links_context

SHOP_ID = 10
PLAIN_TOKEN = "123456:SHOP-TOKEN"


class MagicLinksBotTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'm.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=SHOP_ID, telegram_id=500, referral_code="shop", role="reseller"))
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_ID, is_active=True, bot_token=seal_bot_token(PLAIN_TOKEN)))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def test_shop_bot_username_is_looked_up_with_the_plain_token(self) -> None:
        probe = AsyncMock(return_value={"ok": True, "username": "shop_bot"})
        staff = {"role": "reseller", "bot_user_id": SHOP_ID}
        with patch("app.services.home_overview.check_bot_connection", probe):
            async with self.Session() as session:
                ctx = await build_magic_links_context(session, staff)
        probe.assert_awaited_once_with(PLAIN_TOKEN)
        self.assertEqual(ctx["bot_username"], "shop_bot")
        self.assertEqual(ctx["links"]["wallet"], "https://t.me/shop_bot?start=wallet")


if __name__ == "__main__":
    unittest.main()
