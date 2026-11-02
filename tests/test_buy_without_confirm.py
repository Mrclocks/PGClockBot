"""Picking a paid plan opens its payment screen directly, with the service details on it."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import shop
from app.db.models import Base, BotUser, Order, Plan
from app.services.checkout_summary import checkout_summary
from app.services.orders import CUSTOM_ORDER_NOTE


class _Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
            Plan(id=1, name="ماهانه <b>VIP</b>", description="برای <i>همه</i>", price=210_000,
                 duration_days=30, data_limit_gb=30, is_active=True),
            Plan(id=2, name="تست", price=0, duration_days=1, data_limit_gb=1, is_active=True, is_trial=True),
            Plan(id=3, name="پلن دلخواه", description="سفارشی 20 گیگابایت / 15 روز", price=90_000,
                 duration_days=15, data_limit_gb=20, is_active=False),
            Plan(id=4, name="نامحدود", price=500_000, duration_days=0, data_limit_gb=None, is_active=True),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()


class CheckoutSummaryTests(_Fixture):
    async def summary(self, **order) -> str:
        row = Order(user_id=1, **order)
        self.session.add(row)
        await self.session.commit()
        return await checkout_summary(self.session, row, currency="تومان")

    async def test_a_plan_purchase_shows_what_is_being_bought_escaped(self) -> None:
        text = await self.summary(plan_id=1, amount=210_000)
        for part in ("ماهانه &lt;b&gt;VIP&lt;/b&gt;", "برای &lt;i&gt;همه&lt;/i&gt;", "30 روز", "30 گیگابایت",
                     "مبلغ قابل پرداخت", "210٬000"):
            with self.subTest(part=part):
                self.assertIn(part, text)
        self.assertNotIn("<b>VIP</b>", text)

    async def test_unlimited_plans_say_so(self) -> None:
        text = await self.summary(plan_id=4, amount=500_000)
        self.assertEqual(text.count("نامحدود"), 3, "name, duration and volume")

    async def test_wholesale_shows_quantity_and_per_service_details(self) -> None:
        text = await self.summary(plan_id=1, amount=600_000, quantity=3, discount_amount=30_000)
        for part in ("سفارش عمده", "مدت هر سرویس", "حجم هر سرویس", "تعداد", "قیمت هر سرویس", "تخفیف"):
            with self.subTest(part=part):
                self.assertIn(part, text)

    async def test_a_custom_plan_hides_its_internal_description(self) -> None:
        text = await self.summary(plan_id=3, amount=90_000, note=CUSTOM_ORDER_NOTE)
        self.assertIn("پلن دلخواه", text)
        self.assertIn("15 روز", text)
        self.assertNotIn("سفارشی", text)

    async def test_renewals_keep_only_the_amount(self) -> None:
        text = await self.summary(plan_id=1, amount=210_000, note="renew:5")
        self.assertNotIn("پلن", text)
        self.assertIn("مبلغ قابل پرداخت", text)

    async def test_a_notice_is_shown_above_the_amount(self) -> None:
        row = Order(user_id=1, plan_id=1, amount=150_000, discount_amount=60_000)
        self.session.add(row)
        await self.session.commit()
        text = await checkout_summary(self.session, row, currency="تومان", notice=shop.DISCOUNT_APPLIED)
        self.assertLess(text.index(shop.DISCOUNT_APPLIED), text.index("مبلغ قابل پرداخت"))


class PlanPickTests(_Fixture):
    def callback(self, data: str) -> SimpleNamespace:
        return SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(), bot=None)

    def patches(self, **extra):
        base = {
            "get_all_settings": AsyncMock(return_value={}),
            "_answer_shop_maintenance": AsyncMock(return_value=False),
            "_record_shop_funnel": AsyncMock(),
            "safe_edit_text": AsyncMock(),
        }
        base.update(extra)
        return patch.multiple(shop, **base)

    async def test_a_paid_plan_goes_straight_to_its_payment_screen(self) -> None:
        begin = AsyncMock()
        with self.patches(_begin_plan_purchase=begin):
            await shop.shop_plan(self.callback("shop:plan:1"), self.session, SimpleNamespace(id=1), AsyncMock())
        begin.assert_awaited_once()
        self.assertEqual(int(begin.await_args.args[5].id), 1)

    async def test_a_free_plan_keeps_its_details_screen(self) -> None:
        begin, edit = AsyncMock(), AsyncMock()
        with self.patches(_begin_plan_purchase=begin, safe_edit_text=edit):
            await shop.shop_plan(self.callback("shop:plan:2"), self.session, SimpleNamespace(id=1), AsyncMock())
        begin.assert_not_awaited()
        buttons = [b.callback_data for row in edit.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("shop:buy:2", buttons, "a free plan is delivered only after «ادامه»")

    async def test_accepting_the_terms_resumes_the_purchase(self) -> None:
        state = AsyncMock()
        plan = await self.session.get(Plan, 1)
        with patch("app.bot.handlers.terms.prompt_terms_if_needed", AsyncMock(return_value=True)):
            await shop._begin_plan_purchase(
                self.callback("shop:plan:1"), self.session, SimpleNamespace(id=1), state, {}, plan,
                reseller_owner_id=None,
            )
        state.update_data.assert_awaited_with(terms_resume="shop:buy:1")

    async def test_the_payment_screen_carries_the_service_details(self) -> None:
        order = Order(user_id=1, plan_id=1, amount=210_000)
        self.session.add(order)
        await self.session.commit()
        show = AsyncMock()
        with self.patches(create_order=AsyncMock(return_value=order), _show_order_pay=show,
                          _notify_new_order=AsyncMock()):
            await shop._complete_shop_buy(
                callback=self.callback("shop:buy:1"), message=None, session=self.session,
                db_user=SimpleNamespace(id=1, reseller_id=None), state=AsyncMock(), ui={},
                plan=await self.session.get(Plan, 1), plan_id=1,
            )
        summary = show.await_args.args[5]
        self.assertIn("ماهانه", summary)
        self.assertIn("30 گیگابایت", summary)

    async def test_custom_and_wholesale_continue_buys_without_a_confirm_screen(self) -> None:
        for confirm, buy in ((shop.custom_confirm, "custom_buy"), (shop.wholesale_confirm, "wholesale_buy")):
            with self.subTest(buy), patch.object(shop, buy, AsyncMock()) as purchase:
                await confirm(self.callback("x"), self.session, SimpleNamespace(id=1), AsyncMock())
                purchase.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
