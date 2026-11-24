"""Gift-code and campaign panels have their own nav level, so every Back lands one step up."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import menu_nav as nav
from app.bot.handlers import marketing_tools, nav_marketing, reply_nav
from app.db.models import BotUser
from tests.postgres_support import scoped_async_engine


def _message():
    message = AsyncMock()
    message.from_user = SimpleNamespace(is_bot=True)
    return message


def _callback(data: str):
    return SimpleNamespace(data=data, message=_message(), answer=AsyncMock())


class NavLevelCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
        await nav.set_nav_level(self.state, nav.NAV_ADMIN, push=False)
        self.user = BotUser(telegram_id=1)
        patches = [
            patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=True)),
            patch.object(marketing_tools, "panel_page_url", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _stack(self) -> list[str]:
        return list((await self.state.get_data()).get(nav.NAV_STACK) or [])

    async def _open_hub(self, session):
        await nav_marketing.open_admin_marketing_hub(_message(), session, self.user, self.state, push=True)

    async def _tap(self, session, handler, data):
        await handler(_callback(data), session=session, db_user=self.user, state=self.state)


class PanelLevelTests(NavLevelCase):
    async def test_opening_a_panel_records_its_level_above_the_hub(self):
        async with self.Session() as session:
            await self._open_hub(session)
            await self._tap(session, marketing_tools.open_gift_panel, marketing_tools.GIFT_PANEL_CALLBACK)
            self.assertEqual(await nav.get_nav_level(self.state), nav.NAV_ADMIN_GIFTS)
            self.assertEqual(await self._stack(), [nav.NAV_ADMIN, nav.NAV_ADMIN_MARKETING])
            await self._tap(session, marketing_tools.open_campaign_panel, marketing_tools.CAMPAIGN_PANEL_CALLBACK)
            self.assertEqual(await nav.get_nav_level(self.state), nav.NAV_ADMIN_CAMPAIGNS)

    async def test_actions_inside_a_panel_keep_level_and_stack(self):
        async with self.Session() as session:
            await self._open_hub(session)
            await self._tap(session, marketing_tools.open_gift_panel, marketing_tools.GIFT_PANEL_CALLBACK)
            before = await self._stack()
            await self._tap(session, marketing_tools.set_gift_state, "adm:gc:set:999:0")
            await self._tap(session, marketing_tools.open_gift_panel, marketing_tools.GIFT_PANEL_CALLBACK)
            self.assertEqual(await nav.get_nav_level(self.state), nav.NAV_ADMIN_GIFTS)
            self.assertEqual(await self._stack(), before)

    async def test_reply_back_from_a_panel_reopens_the_marketing_hub(self):
        for opener, data in (
            (marketing_tools.open_gift_panel, marketing_tools.GIFT_PANEL_CALLBACK),
            (marketing_tools.open_campaign_panel, marketing_tools.CAMPAIGN_PANEL_CALLBACK),
        ):
            with self.subTest(panel=data):
                async with self.Session() as session:
                    await nav.set_nav_level(self.state, nav.NAV_ADMIN, push=False)
                    await self.state.update_data(**{nav.NAV_STACK: []})
                    await self._open_hub(session)
                    await self._tap(session, opener, data)
                    message = _message()
                    await reply_nav.handle_back(message, session, self.user, self.state)
                    self.assertEqual(await nav.get_nav_level(self.state), nav.NAV_ADMIN_MARKETING)
                    markup = message.edit_text.await_args.kwargs["reply_markup"]
                    self.assertIn("nv:adm:gc", [b.callback_data for row in markup.inline_keyboard for b in row])
                    await reply_nav.handle_back(_message(), session, self.user, self.state)
                    self.assertEqual(await nav.get_nav_level(self.state), nav.NAV_ADMIN)

    async def test_inline_back_to_the_hub_leaves_no_stale_stack_entry(self):
        async with self.Session() as session:
            await self._open_hub(session)
            await self._tap(session, marketing_tools.open_gift_panel, marketing_tools.GIFT_PANEL_CALLBACK)
            await nav_marketing.nv_adm_marketing(_callback("nv:adm:marketing"), session, self.user, self.state)
            self.assertEqual(await nav.get_nav_level(self.state), nav.NAV_ADMIN_MARKETING)
            self.assertEqual(await self._stack(), [nav.NAV_ADMIN])
            self.assertEqual(await nav.pop_nav_level(self.state), nav.NAV_ADMIN)

    async def test_reopening_the_hub_from_the_hub_does_not_pop_anything(self):
        async with self.Session() as session:
            await self._open_hub(session)
            stack = await self._stack()
            await nav_marketing.nv_adm_marketing(_callback("nv:adm:marketing"), session, self.user, self.state)
            self.assertEqual(await self._stack(), stack)


class RestoreGateTests(NavLevelCase):
    async def test_restoring_a_panel_level_opens_that_panel_for_the_owner(self):
        async with self.Session() as session:
            for level, marker in ((nav.NAV_ADMIN_GIFTS, "کدهای هدیه"), (nav.NAV_ADMIN_CAMPAIGNS, "کمپین")):
                message = _message()
                await marketing_tools.open_admin_marketing_panel(
                    level, message, session, self.user, self.state, push=False
                )
                self.assertEqual(await nav.get_nav_level(self.state), level)
                self.assertIn(marker, message.edit_text.await_args.args[0])

    async def test_non_owner_and_shop_bot_cannot_restore_a_panel_level(self):
        for owner, reseller_bot in ((False, False), (True, True)):
            with self.subTest(owner=owner, reseller_bot=reseller_bot):
                async with self.Session() as session:
                    await nav.set_nav_level(self.state, nav.NAV_ADMIN, push=False)
                    message = _message()
                    with patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=owner)):
                        await marketing_tools.open_admin_marketing_panel(
                            nav.NAV_ADMIN_GIFTS, message, session, self.user, self.state,
                            push=False, is_reseller_bot=reseller_bot,
                        )
                    message.edit_text.assert_not_awaited()
                    self.assertEqual(await nav.get_nav_level(self.state), nav.NAV_ADMIN)


if __name__ == "__main__":
    unittest.main()
