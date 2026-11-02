"""Pay-step Back button + reseller capacity row under My Services."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class PayMethodsBackKeyboardTests(unittest.TestCase):
    def test_pay_methods_includes_back_above_cancel(self):
        from app.bot.keyboards import pay_methods

        markup = pay_methods(
            38,
            {
                "pay_wallet_enabled": "1",
                "btn_pay_wallet": "پرداخت از کیف پول",
                "btn_back": "⬅️ بازگشت",
                "btn_cancel": "❌ انصراف",
            },
        )
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("pay:back:38", data)
        self.assertIn("menu:home", data)
        self.assertLess(data.index("pay:back:38"), data.index("menu:home"))


class NavigateOrderPayBackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_plan_returns_to_the_plan_list(self):
        from app.bot.handlers.shop import navigate_order_pay_back
        from app.db.models import OrderStatus

        message = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 7
        state = AsyncMock()
        order = SimpleNamespace(
            id=38,
            user_id=7,
            plan_id=12,
            service_id=None,
            note=None,
            status=OrderStatus.PENDING.value,
        )

        with (
            patch(
                "app.bot.menu_nav.clear_checkout_nav",
                new_callable=AsyncMock,
            ),
            patch(
                "app.bot.handlers.shop.shop_kind_fixed",
                new_callable=AsyncMock,
            ) as plan_list,
        ):
            await navigate_order_pay_back(
                message, session, db_user, order, state
            )

        # The plan details live on the payment screen; back goes to the list, not a new order.
        plan_list.assert_awaited()
        cb = plan_list.await_args.args[0]
        self.assertEqual(cb.data, "shop:kind:fixed")

    async def test_renew_reopens_service_card(self):
        from app.bot.handlers.shop import navigate_order_pay_back
        from app.db.models import OrderStatus

        message = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 7
        state = AsyncMock()
        order = SimpleNamespace(
            id=9,
            user_id=7,
            plan_id=1,
            service_id=55,
            note="renew:55",
            status=OrderStatus.PENDING.value,
        )

        with (
            patch(
                "app.bot.menu_nav.clear_checkout_nav",
                new_callable=AsyncMock,
            ),
            patch(
                "app.bot.handlers.services.svc_view",
                new_callable=AsyncMock,
            ) as svc_view,
        ):
            await navigate_order_pay_back(
                message, session, db_user, order, state
            )

        svc_view.assert_awaited()
        self.assertEqual(svc_view.await_args.args[0].data, "svc:view:55")


class ResellerMyServiceCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_format_includes_remain_volume(self):
        from app.services.resellers import format_reseller_my_service_card

        profile = SimpleNamespace(
            id=3,
            user_id=9,
            pg_admin_username="shop_admin",
            pg_role_id=2,
            billing_mode="fixed",
            plan_id=1,
        )
        overview = {
            "ready": True,
            "status_label": "فعال",
            "traffic": {
                "ratio_text": "40 گیگ از 100 گیگ",
                "remain_text": "60 گیگ",
                "has_limit": True,
            },
            "users": {
                "ratio_text": "2/10",
                "remain_text": "8",
                "has_limit": True,
            },
            "lifetime_text": None,
            "role_name": "Reseller",
            "error": None,
        }
        plan = SimpleNamespace(name="پلن طلایی")
        session = AsyncMock()

        with (
            patch(
                "app.services.reseller_capacity.load_reseller_plan",
                AsyncMock(return_value=plan),
            ),
            patch(
                "app.services.pg_overview.build_reseller_pg_overview",
                AsyncMock(return_value=overview),
            ),
            patch(
                "app.services.pg_admin_subscription.get_subscription",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.services.billing.is_payg",
                return_value=False,
            ),
        ):
            text = await format_reseller_my_service_card(session, profile)

        self.assertIn("سرویس نمایندگی", text)
        self.assertIn("باقی‌مانده حجم", text)
        self.assertIn("60 گیگ", text)
        self.assertIn("shop_admin", text)

    def test_list_label(self):
        from app.services.resellers import reseller_my_service_list_label

        label = reseller_my_service_list_label(
            SimpleNamespace(pg_admin_username="alpha")
        )
        self.assertIn("نمایندگی", label)
        self.assertIn("alpha", label)


class ServicesListResellerRowTests(unittest.TestCase):
    def test_keyboard_injects_reseller_row(self):
        from app.bot.nav_inline import services_list_keyboard

        svc = MagicMock()
        svc.id = 7
        svc.pg_username = "u1"
        kb = services_list_keyboard(
            [svc], {}, reseller_capacity_label="🤝 نمایندگی · shop"
        )
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("svc:reseller", data)
        self.assertIn("svc:view:7", data)
        self.assertTrue(any("نمایندگی" in (t or "") for t in texts))
        # Reseller row is first
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "svc:reseller")


class OpenServicesResellerOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reseller_only_opens_capacity_card(self):
        from app.bot.handlers.reply_nav import open_services_list

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        with (
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=("KB", {}, "user")),
            ),
            patch(
                "app.bot.handlers.services._reseller_capacity_list_label",
                AsyncMock(return_value="🤝 نمایندگی · x"),
            ),
            patch(
                "app.bot.handlers.services.svc_reseller_view",
                new_callable=AsyncMock,
            ) as view,
        ):
            await open_services_list(message, session, db_user, state=None)

        view.assert_awaited()


if __name__ == "__main__":
    unittest.main()
