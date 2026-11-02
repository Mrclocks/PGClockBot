"""«سرویس‌های من» is reachable for a customer without services while «اتصال اشتراک قبلی» is on."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import keyboards as kb
from app.bot.handlers.reply_nav import _nav_context
from app.bot.menu_nav import build_main_reply_keyboard, user_sees_my_services
from app.bot.nav_inline import support_hub_keyboard
from app.bot.service_import_ui import ADD_CALLBACK, empty_services_keyboard
from app.db import Base
from app.db.models import BotUser, ResellerProfile, UserService
from app.services.users import clear_settings_cache, reset_shop_reseller_id, set_settings_bulk, set_shop_reseller_id

SERVICES_ACTION = kb.REPLY_ACTION_SERVICES


def _button_texts(markup) -> list[str]:
    return [b.text for row in markup.keyboard for b in row]


class MenuCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'm.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
                BotUser(id=2, telegram_id=102, referral_code="u2", role="user"),
                BotUser(id=5, telegram_id=105, referral_code="s5", role="reseller"),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
            ])
            await session.flush()
            session.add(ResellerProfile(user_id=5, is_active=True))
            session.add(UserService(
                bot_user_id=2, pg_user_id=9, pg_username="have", subscription_url="u", subscription_token="t",
            ))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()

    async def switch(self, on: bool, *, shop: int | None = None):
        async with self.Session() as session:
            await set_settings_bulk(session, {"service_import_enabled": "1" if on else "0"}, reseller_id=shop)
        clear_settings_cache()

    async def sees(self, user_id: int, *, shop: int | None = None) -> bool:
        token = set_shop_reseller_id(shop)
        try:
            async with self.Session() as session:
                user = await session.get(BotUser, user_id)
                return await user_sees_my_services(
                    session, user, is_reseller_bot=shop is not None, reseller_owner_id=shop
                )
        finally:
            reset_shop_reseller_id(token)


class VisibilityTests(MenuCase):
    async def test_a_customer_without_services_does_not_see_the_button_while_the_feature_is_off(self):
        await self.switch(False)
        self.assertFalse(await self.sees(1))

    async def test_a_customer_without_services_sees_the_button_while_the_feature_is_on(self):
        await self.switch(True)
        self.assertTrue(await self.sees(1))

    async def test_a_customer_with_services_always_sees_it(self):
        await self.switch(False)
        self.assertTrue(await self.sees(2))

    async def test_the_switch_of_one_shop_does_not_reach_the_platform_or_another_shop(self):
        await self.switch(True, shop=5)
        self.assertTrue(await self.sees(6, shop=5))
        self.assertFalse(await self.sees(1))

    async def test_the_platform_switch_does_not_show_the_button_in_a_shop_bot_that_has_it_off(self):
        await self.switch(True)
        await self.switch(False, shop=5)
        self.assertTrue(await self.sees(1))
        self.assertFalse(await self.sees(6, shop=5))


class KeyboardTests(MenuCase):
    async def keyboard_texts(self, user_id: int, *, shop: int | None = None) -> list[str]:
        token = set_shop_reseller_id(shop)
        try:
            async with self.Session() as session:
                user = await session.get(BotUser, user_id)
                markup, _ui, _role = await build_main_reply_keyboard(
                    session, user, is_reseller_bot=shop is not None, reseller_owner_id=shop
                )
        finally:
            reset_shop_reseller_id(token)
        return _button_texts(markup)

    async def test_the_main_keyboard_gets_my_services_next_to_buy_without_other_changes(self):
        await self.switch(False)
        before = await self.keyboard_texts(1)
        await self.switch(True)
        after = await self.keyboard_texts(1)
        label = kb._t(None, "btn_services")
        self.assertNotIn(label, before)
        self.assertIn(label, after)
        self.assertIn(kb._t(None, "btn_shop"), after)
        self.assertEqual(sorted(set(after) - {label}), sorted(before))

    async def test_tapping_my_services_is_understood_for_that_customer(self):
        await self.switch(True)
        token = set_shop_reseller_id(None)
        try:
            async with self.Session() as session:
                user = await session.get(BotUser, 1)
                ui, role, has, show_creds = await _nav_context(session, user, is_reseller_bot=False, reseller_owner_id=None)
        finally:
            reset_shop_reseller_id(token)
        mapping = kb.reply_action_map(role, has_services=has, ui=ui, show_reseller_creds=show_creds)
        self.assertEqual(mapping.get(kb._t(ui, "btn_services")), SERVICES_ACTION)

    async def test_a_shop_bot_keyboard_follows_the_shops_own_switch(self):
        await self.switch(True, shop=5)
        self.assertIn(kb._t(None, "btn_services"), await self.keyboard_texts(6, shop=5))
        await self.switch(False, shop=5)
        self.assertNotIn(kb._t(None, "btn_services"), await self.keyboard_texts(6, shop=5))


class ScreenTests(unittest.TestCase):
    def test_the_empty_list_offers_buying_and_connecting(self):
        markup = empty_services_keyboard({})
        flat = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(flat, ["shop:list", ADD_CALLBACK, "menu:home"])

    def test_the_support_hub_has_no_reseller_apply_entry(self):
        data = [b.callback_data for row in support_hub_keyboard({}).inline_keyboard for b in row]
        self.assertIn("nv:s:new", data)
        self.assertNotIn("nv:resapply", data)
