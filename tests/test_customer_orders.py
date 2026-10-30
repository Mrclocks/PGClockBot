"""«سفارش‌های من»: a customer sees only their own orders of the shop whose bot is open."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import customer_orders as bot
from app.bot.handlers import wallet
from app.bot.nav_inline import wallet_hub_keyboard
from app.db.models import Base, BotUser, Order, Payment, Plan
from app.services import bot_feedback_texts as feedback
from app.services.customer_orders import get_customer_order, list_customer_orders
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id


def buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class OrdersCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'orders.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.token = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=1, telegram_id=1001, referral_code="me"),
            BotUser(id=4, telegram_id=1004, referral_code="other"),
            Plan(id=1, name="Gold <i>", price=100, duration_days=30, data_limit_gb=10),
        ])
        await self.session.flush()
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        self.session.add_all([
            Order(id=10, user_id=1, plan_id=1, amount=100, status="delivered"),
            Order(id=11, user_id=1, plan_id=1, amount=200, status="awaiting_receipt"),
            Order(id=12, user_id=1, plan_id=1, amount=300, status="rejected"),
            Order(id=13, user_id=1, plan_id=1, amount=400, status="paid", updated_at=old),
            Order(id=14, user_id=1, plan_id=1, amount=500, status="delivered", note="renew:5"),
            Order(id=20, user_id=1, plan_id=1, amount=900, status="delivered", reseller_id=2),
            Order(id=30, user_id=4, plan_id=1, amount=700, status="delivered"),
        ])
        await self.session.flush()
        self.session.add_all([
            Payment(id=1, order_id=11, user_id=1, amount=200, status="pending"),
            Payment(id=2, order_id=12, user_id=1, amount=300, status="rejected", receipt_file_id="f"),
        ])
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()
        self.tmp.cleanup()


class CustomerOrdersServiceTests(OrdersCase):
    async def test_a_customer_sees_only_their_orders_of_this_shop(self):
        platform, _ = await list_customer_orders(self.session, 1, shop_id=None, limit=20)
        self.assertEqual([item.order_id for item in platform], [14, 13, 12, 11, 10])
        shop, _ = await list_customer_orders(self.session, 1, shop_id=2)
        self.assertEqual([item.order_id for item in shop], [20])
        other, _ = await list_customer_orders(self.session, 4, shop_id=None)
        self.assertEqual([item.order_id for item in other], [30])

    async def test_someone_elses_order_and_another_shops_order_read_as_missing(self):
        self.assertIsNone(await get_customer_order(self.session, 1, 30, shop_id=None))
        self.assertIsNone(await get_customer_order(self.session, 1, 20, shop_id=None))
        self.assertIsNone(await get_customer_order(self.session, 1, 10, shop_id=2))
        self.assertEqual((await get_customer_order(self.session, 1, 10, shop_id=None)).order_id, 10)

    async def test_paging_walks_older_orders_without_repeating(self):
        first, cursor = await list_customer_orders(self.session, 1, shop_id=None, limit=2)
        second, cursor = await list_customer_orders(self.session, 1, shop_id=None, before_id=cursor, limit=2)
        third, cursor = await list_customer_orders(self.session, 1, shop_id=None, before_id=cursor, limit=2)
        self.assertEqual([[i.order_id for i in page] for page in (first, second, third)], [[14, 13], [12, 11], [10]])
        self.assertIsNone(cursor)

    async def test_each_item_says_what_is_going_on(self):
        items = {item.order_id: item for item in (await list_customer_orders(self.session, 1, shop_id=None, limit=20))[0]}
        self.assertTrue(items[11].waiting_receipt)
        self.assertFalse(items[12].waiting_receipt)
        self.assertTrue(items[13].delayed)
        self.assertFalse(items[10].delayed)
        self.assertEqual(items[14].kind, "تمدید")
        self.assertEqual(items[10].kind, "خرید")
        self.assertEqual(items[12].payment_status, "rejected")

    async def test_a_page_costs_a_fixed_number_of_queries(self):
        statements = []
        event.listen(self.engine.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        await list_customer_orders(self.session, 1, shop_id=None, limit=20)
        self.assertLessEqual(len(statements), 2)


class CustomerOrdersBotTests(OrdersCase):
    async def press(self, data, *, shop_id=None, user_id=1):
        handler = bot.order_card if data.startswith("myord:v") else bot.orders_list
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        token = set_shop_reseller_id(shop_id)
        try:
            with patch.object(bot, "safe_edit_text", AsyncMock()) as edit:
                await handler(callback, self.session, SimpleNamespace(id=user_id))
        finally:
            reset_shop_reseller_id(token)
        return callback, edit

    def shown(self, edit):
        return edit.await_args.args[1], buttons(edit.await_args.kwargs["reply_markup"])

    async def test_list_shows_only_this_customers_orders_with_back_to_the_wallet_history(self):
        _, edit = await self.press("myord:l")
        _, found = self.shown(edit)
        self.assertEqual(found, [f"myord:v:{i}" for i in (14, 13, 12, 11, 10)] + [bot.BACK_CALLBACK])
        _, edit = await self.press("myord:l", shop_id=2)
        self.assertEqual(self.shown(edit)[1], ["myord:v:20", bot.BACK_CALLBACK])

    async def test_card_of_another_customer_or_shop_is_refused(self):
        for data, shop_id, user_id in (("myord:v:30", None, 1), ("myord:v:20", None, 1), ("myord:v:10", 2, 1), ("myord:v:10", None, 4)):
            callback, edit = await self.press(data, shop_id=shop_id, user_id=user_id)
            callback.answer.assert_awaited_once_with(feedback.NOT_FOUND, show_alert=True)
            edit.assert_not_awaited()

    async def test_cards_guide_the_customer_for_each_state(self):
        text, found = self.shown((await self.press("myord:v:11"))[1])
        self.assertIn("عکس رسید", text)
        self.assertEqual(found, [bot.SUPPORT_CALLBACK, bot.LIST_CALLBACK])
        text, _ = self.shown((await self.press("myord:v:13"))[1])
        self.assertIn("شناسه سفارش", text)
        text, _ = self.shown((await self.press("myord:v:12"))[1])
        self.assertIn("اشتباهی", text)
        text, found = self.shown((await self.press("myord:v:10"))[1])
        self.assertIn("سرویس‌های من", text)
        self.assertEqual(found, [bot.LIST_CALLBACK])

    async def test_plan_names_are_escaped(self):
        text, _ = self.shown((await self.press("myord:v:10"))[1])
        self.assertIn("Gold &lt;i&gt;", text)
        self.assertNotIn("Gold <i>", text)

    async def test_older_page_goes_back_to_the_first_page(self):
        with patch.object(bot, "list_customer_orders", AsyncMock(return_value=([], None))):
            _, edit = await self.press("myord:m:11")
        self.assertEqual(self.shown(edit)[1], [bot.LIST_CALLBACK])


class WalletHistoryButtonTests(OrdersCase):
    def test_the_button_is_added_above_back_and_the_hub_itself_is_unchanged(self):
        hub = wallet_hub_keyboard({})
        self.assertNotIn(bot.LIST_CALLBACK, buttons(hub))
        with_button = buttons(bot.with_orders_button(hub))
        self.assertEqual(with_button[-2:], [bot.LIST_CALLBACK, buttons(hub)[-1]])
        self.assertEqual([b for b in with_button if b != bot.LIST_CALLBACK], buttons(hub))

    async def test_wallet_history_screen_offers_the_orders(self):
        callback = AsyncMock()
        callback.message = AsyncMock()
        with patch.object(wallet, "safe_edit_text", AsyncMock()) as edit:
            await wallet.wallet_tx(callback, self.session, SimpleNamespace(id=1))
        self.assertIn(bot.LIST_CALLBACK, buttons(edit.await_args.kwargs["reply_markup"]))


if __name__ == "__main__":
    unittest.main()
