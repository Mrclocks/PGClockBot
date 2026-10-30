"""Staff search by subscription link: shop scope, no secret leak, result buttons and the typed step."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import admin
from app.bot.handlers import subscription_lookup as bot
from app.db.models import Base, BotUser, Plan, ShopWallet, UserService
from app.services import bot_feedback_texts as feedback
from app.services.subscription_lookup import find_service_by_link, link_token

PLATFORM_TOKEN = "PlatformTokenAbcdef0123456789ABCDEF-_xyz"
SHOP_TOKEN = "ShopTwoTokenAbcdef0123456789ABCDEF-_xyzq"
OTHER_TOKEN = "ShopThreeTokenAbcdef0123456789ABCDEF-_xy"
REVIEW_TOKEN = "NeedsReviewTokenAbcdef0123456789ABCDEF-_"
URL = "https://sub.example.com/sub/{}"


class LookupCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'lookup.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=1, telegram_id=1001, referral_code="c1", full_name="Platform Customer", username="platformer", wallet_balance=700),
            BotUser(id=4, telegram_id=1004, referral_code="c4", reseller_id=2, full_name="<b>Shop</b> Customer", wallet_balance=900),
            BotUser(id=5, telegram_id=1005, referral_code="c5", reseller_id=3, full_name="Other Shop Customer"),
            Plan(id=1, name="Gold <i>", price=100, duration_days=30, data_limit_gb=10),
        ])
        await self.session.flush()
        self.session.add_all([
            UserService(id=10, bot_user_id=1, plan_id=1, pg_user_id=500, pg_username="plat-svc", subscription_token=PLATFORM_TOKEN, quota_status="active"),
            UserService(id=11, bot_user_id=4, plan_id=1, pg_user_id=501, pg_username="shop-svc", shop_id=2, subscription_token=SHOP_TOKEN, quota_status="disabled"),
            UserService(id=12, bot_user_id=5, plan_id=1, pg_user_id=502, pg_username="other-svc", shop_id=3, subscription_token=OTHER_TOKEN),
            UserService(id=13, bot_user_id=1, plan_id=1, pg_user_id=503, pg_username="review-svc", subscription_token=REVIEW_TOKEN, shop_needs_review=True),
            UserService(id=14, bot_user_id=4, plan_id=1, pg_user_id=504, pg_username="platform-of-shop-customer", subscription_token="CustomerOfShopBoughtOnPlatformToken0123456", shop_id=None),
        ])
        self.session.add(ShopWallet(user_id=4, reseller_id=2, balance=300))
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()


class LookupServiceTests(LookupCase):
    async def test_each_shop_finds_only_its_own_services(self):
        self.assertEqual((await find_service_by_link(self.session, URL.format(PLATFORM_TOKEN), shop_id=None)).service.id, 10)
        self.assertEqual((await find_service_by_link(self.session, URL.format(SHOP_TOKEN), shop_id=2)).service.id, 11)
        for raw, shop_id in ((SHOP_TOKEN, None), (SHOP_TOKEN, 3), (PLATFORM_TOKEN, 2), (OTHER_TOKEN, 2), (OTHER_TOKEN, None)):
            self.assertIsNone(await find_service_by_link(self.session, raw, shop_id=shop_id))

    async def test_bare_long_token_and_url_with_text_around_it_match(self):
        self.assertEqual((await find_service_by_link(self.session, SHOP_TOKEN, shop_id=2)).owner.id, 4)
        self.assertEqual((await find_service_by_link(self.session, f"لینک من: {URL.format(SHOP_TOKEN)} ممنون", shop_id=2)).service.id, 11)

    async def test_services_waiting_for_shop_review_are_hidden(self):
        self.assertIsNone(await find_service_by_link(self.session, URL.format(REVIEW_TOKEN), shop_id=None))

    async def test_ids_handles_and_short_text_are_not_links(self):
        for raw in ("123456789", "@someone", "someone", "https://t.me/someone", "", None, "plat-svc"):
            self.assertIsNone(link_token(raw))
            self.assertIsNone(await find_service_by_link(self.session, raw, shop_id=None))


class ResultCardTests(LookupCase):
    async def card(self, raw, shop_id):
        return await bot.staff_lookup(self.session, raw, shop_id=shop_id)

    def buttons(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_shop_card_shows_the_shop_purse_escapes_names_and_never_echoes_the_token(self):
        text, markup = await self.card(URL.format(SHOP_TOKEN), 2)
        self.assertIn("&lt;b&gt;Shop&lt;/b&gt; Customer", text)
        self.assertNotIn("<b>Shop</b>", text)
        self.assertIn("Gold &lt;i&gt;", text)
        self.assertNotIn(SHOP_TOKEN, text)
        self.assertIn("300", text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).replace(",", "").replace("٬", ""))
        self.assertNotIn("900", text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).replace(",", "").replace("٬", ""))
        self.assertEqual(self.buttons(markup), ["res:user:4", bot.ASK_CALLBACK, bot.SHOP_BACK])

    async def test_platform_card_offers_service_management_only_for_platform_customers(self):
        _, markup = await self.card(URL.format(PLATFORM_TOKEN), None)
        self.assertEqual(self.buttons(markup), ["adm:users:view:1", "adm:users:svc:1:10", bot.PLATFORM_ASK, bot.PLATFORM_BACK])
        _, markup = await self.card("CustomerOfShopBoughtOnPlatformToken0123456", None)
        self.assertEqual(self.buttons(markup), ["adm:users:view:4", bot.PLATFORM_ASK, bot.PLATFORM_BACK])

    async def test_another_shops_link_answers_exactly_like_an_unknown_one(self):
        other = await self.card(URL.format(OTHER_TOKEN), 2)
        unknown = await self.card(URL.format("UnknownTokenAbcdef0123456789ABCDEF-_xyzqrs"), 2)
        self.assertEqual(other[0], unknown[0])
        self.assertEqual(self.buttons(other[1]), self.buttons(unknown[1]))
        self.assertEqual(other[0], bot.NOT_FOUND_TEXT)


class TypedStepTests(LookupCase):
    async def receive(self, text, *, scope=2):
        message = AsyncMock()
        message.text = text
        state = AsyncMock()
        user = SimpleNamespace(id=9, telegram_id=777)
        with patch.object(bot, "shop_scope", AsyncMock(return_value=scope)), \
                patch.object(bot, "finish_text_step", AsyncMock()) as finish:
            await bot.receive_link(message, state, self.session, user, is_reseller_bot=False, reseller_owner_id=None)
        return message, state, finish

    async def test_result_is_shown_and_the_typed_link_is_deleted(self):
        message, state, finish = await self.receive(URL.format(SHOP_TOKEN))
        self.assertIn("shop-svc", finish.await_args.kwargs["text"])
        message.delete.assert_awaited_once()
        state.set_state.assert_awaited_with(None)

    async def test_lost_permission_ends_the_step_without_a_lookup(self):
        message, state, finish = await self.receive(URL.format(SHOP_TOKEN), scope=None)
        finish.assert_not_awaited()
        state.clear.assert_awaited()
        message.answer.assert_awaited_with(feedback.NO_ACCESS_REPLY)

    async def test_a_message_that_cannot_be_deleted_does_not_break_the_answer(self):
        message = AsyncMock()
        message.delete.side_effect = TelegramBadRequest(method=MagicMock(), message="message can't be deleted")
        await bot.delete_typed_link(message)

    async def test_entry_button_needs_the_dashboard_permission(self):
        callback = AsyncMock()
        callback.message = AsyncMock()
        with patch.object(bot, "shop_scope", AsyncMock(return_value=None)), patch.object(bot, "ask_text", AsyncMock()) as ask:
            await bot.ask_link(callback, AsyncMock(), self.session, SimpleNamespace(), is_reseller_bot=False, reseller_owner_id=None)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        ask.assert_not_awaited()
        with patch.object(bot, "shop_scope", AsyncMock(return_value=2)), patch.object(bot, "ask_text", AsyncMock()) as ask:
            await bot.ask_link(callback, AsyncMock(), self.session, SimpleNamespace(), is_reseller_bot=False, reseller_owner_id=None)
        self.assertEqual(ask.await_args.kwargs["return_to"], bot.SHOP_BACK)

    async def test_shop_scope_needs_the_permission_and_a_shop(self):
        async def scope(actor, perm):
            with patch.object(bot, "load_reseller_actor", AsyncMock(return_value=actor)), \
                    patch.object(bot, "has_bot_perm", MagicMock(return_value=perm)):
                return await bot.shop_scope(MagicMock(), MagicMock(), is_reseller_bot=True, reseller_owner_id=None)

        self.assertEqual(await scope((2, object()), True), 2)
        self.assertIsNone(await scope((2, object()), False))
        self.assertIsNone(await scope((None, None), True))


class PlatformSearchTests(LookupCase):
    async def test_platform_link_search_answers_with_the_platform_card_and_deletes_the_link(self):
        message = AsyncMock()
        message.text = URL.format(PLATFORM_TOKEN)
        state = AsyncMock()
        await admin._answer_link_search(message, state, self.session, SimpleNamespace(id=9))
        state.clear.assert_awaited()
        self.assertIn("plat-svc", message.answer.await_args_list[-1].args[0])
        message.delete.assert_awaited_once()

    async def test_platform_search_cannot_see_a_shop_link(self):
        message = AsyncMock()
        message.text = URL.format(SHOP_TOKEN)
        await admin._answer_link_search(message, AsyncMock(), self.session, SimpleNamespace(id=9))
        self.assertEqual(message.answer.await_args_list[-1].args[0], bot.NOT_FOUND_TEXT)


if __name__ == "__main__":
    unittest.main()
