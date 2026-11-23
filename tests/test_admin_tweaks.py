"""Admin and shop homes show failed deliveries; plans open in one screen; no submenu keyboards return."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers import admin_plans, nav_hubs, purchase_contact, reseller_plans
from app.bot.nav_inline import (
    admin_groups_hub_keyboard,
    admin_plans_audience_keyboard,
    reseller_group_hub_keyboard,
    reseller_manage_hub_keyboard,
)
from app.db import Base
from app.db.models import BotUser, Order, OrderStatus
from app.services.admin_counters import PendingCounts, format_queue_summary, pending_counts
from tests.postgres_support import scoped_async_engine


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class DeliveryQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=105, referral_code="shop", role="reseller"),
                BotUser(id=7, telegram_id=107, referral_code="me", role="user"),
            ])
            await session.flush()
            for oid, shop, status in (
                (1, None, OrderStatus.PAID.value), (2, None, OrderStatus.DELIVERING.value),
                (3, None, OrderStatus.DELIVERED.value), (4, 5, OrderStatus.PAID.value),
            ):
                session.add(Order(id=oid, user_id=7, reseller_id=shop, amount=1000, status=status))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_each_shop_counts_only_its_own_stuck_orders(self) -> None:
        async with self.Session() as session:
            platform = await pending_counts(session, shop_id=None)
            shop = await pending_counts(session, shop_id=5)
            other = await pending_counts(session, shop_id=6)
        self.assertEqual((platform.deliveries, shop.deliveries, other.deliveries), (2, 1, 0))
        self.assertEqual(platform.total, 2)

    def test_the_summary_names_failed_deliveries_only_when_there_are_some(self) -> None:
        self.assertIn("تحویل ناموفق ۲", format_queue_summary(PendingCounts(deliveries=2)))
        self.assertNotIn("تحویل", format_queue_summary(PendingCounts(payments=1)))


class HomeShortcutTests(unittest.TestCase):
    def test_the_admin_home_links_failed_deliveries_and_marks_daily_operations(self) -> None:
        markup = admin_groups_hub_keyboard({"btn_adm_hub_ops": "🗓 عملیات روزانه"}, pending_payments=1, pending_deliveries=2)
        self.assertIn("adm:orders", _callbacks(markup))
        self.assertIn("🗓 عملیات روزانه (۳)", _texts(markup))

    def test_the_shop_home_links_its_own_orders(self) -> None:
        entries = [("res_orders", "🛒 سفارش‌ها")]
        with patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=entries):
            home = reseller_manage_hub_keyboard(MagicMock(), {"btn_res_grp_daily": "📥 کارهای روزانه"}, pending_deliveries=1)
            daily = reseller_group_hub_keyboard("daily", MagicMock(), {}, pending_deliveries=1)
        self.assertIn("res:orders", _callbacks(home))
        self.assertNotIn("adm:orders", _callbacks(home))
        self.assertIn("📥 کارهای روزانه (۱)", _texts(home))
        self.assertIn("🛒 سفارش‌ها (۱)", _texts(daily))


class PlansOneScreenTests(unittest.IsolatedAsyncioTestCase):
    def test_an_audience_screen_holds_its_actions_and_backs_up_to_the_plans_hub(self) -> None:
        data = _callbacks(admin_plans_audience_keyboard("users", {}))
        self.assertIn("nv:adm:ra:adm_plans_add", data)
        self.assertNotIn("nv:adm:plans:list:users", data)
        self.assertEqual(data[-1], "nv:adm:plans")

    async def test_user_plans_open_page_by_page_with_the_other_kinds(self) -> None:
        plans = [SimpleNamespace(id=i, name=f"P{i}", is_active=True, is_trial=False, pg_template_id=1) for i in range(1, 12)]
        session = MagicMock(execute=AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: plans))))
        with patch.object(admin_plans, "get_all_settings", AsyncMock(return_value={})):
            text, markup = await admin_plans.users_plans_overview_view(session, 1)
        data = _callbacks(markup)
        self.assertEqual([c for c in data if c.startswith("adm:plan:view:")], ["adm:plan:view:11"])
        self.assertIn("adm:plans:kind:users:trial", data)
        self.assertIn("nv:adm:plans:aud:users", data)
        self.assertIn("صفحه ۲ از ۲", text)

    async def test_a_page_callback_keeps_the_audience(self) -> None:
        callback = MagicMock(data="nv:adm:plans:aud:resellers:p:3", answer=AsyncMock(), message=MagicMock())
        with (
            patch("app.bot.handlers.reply_nav._deny_unless_owner", AsyncMock(return_value=True)),
            patch("app.bot.handlers.reply_nav.present_admin_plans_audience", AsyncMock()) as shown,
        ):
            await nav_hubs.nv_adm_plans_aud(callback, MagicMock(), MagicMock(), AsyncMock())
        self.assertEqual((shown.await_args.kwargs["audience"], shown.await_args.kwargs["page_index"]), ("resellers", 3))

    async def test_a_forged_audience_is_refused(self) -> None:
        callback = MagicMock(data="nv:adm:plans:aud:admins", answer=AsyncMock(), message=MagicMock())
        with patch("app.bot.handlers.reply_nav.present_admin_plans_audience", AsyncMock()) as shown:
            await nav_hubs.nv_adm_plans_aud(callback, MagicMock(), MagicMock(), AsyncMock())
        shown.assert_not_awaited()


class NoSubmenuKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_in_the_new_plan_form_reopens_the_plans_screen(self) -> None:
        message = MagicMock(text="انصراف", answer=AsyncMock())
        state = AsyncMock()
        with patch("app.bot.handlers.reply_nav.open_reseller_plans_hub", AsyncMock()) as plans:
            await reseller_plans.res_plan_mode_cancel(message, state, MagicMock(), MagicMock(), is_reseller_bot=True)
        plans.assert_awaited_once()
        state.clear.assert_awaited_once()
        message.answer.assert_not_awaited()

    async def test_a_hint_keeps_the_current_keyboard(self) -> None:
        message = MagicMock(text="سلام", answer=AsyncMock())
        await reseller_plans.res_plan_category_cancel(message, AsyncMock(), MagicMock(), MagicMock())
        self.assertNotIn("reply_markup", message.answer.await_args.kwargs)

    async def test_a_verified_contact_brings_back_the_main_menu(self) -> None:
        main_kb = ReplyKeyboardMarkup(keyboard=[])
        message = MagicMock(contact=SimpleNamespace(user_id=1, phone_number="+98"), answer=AsyncMock())
        state = AsyncMock(get_data=AsyncMock(return_value={"purchase_contact_shop": "k"}))
        with (
            patch.object(purchase_contact, "purchase_shop_key", return_value="k"),
            patch.object(purchase_contact, "verify_purchase_contact", AsyncMock()),
            patch.object(purchase_contact, "build_main_reply_keyboard", AsyncMock(return_value=(main_kb, {}, "user"))),
        ):
            await purchase_contact.receive_purchase_contact(message, MagicMock(), MagicMock(), state, is_reseller_bot=True)
        self.assertIs(message.answer.await_args_list[0].kwargs["reply_markup"], main_kb)


if __name__ == "__main__":
    unittest.main()
