"""Delivered staff notices open the customer card (platform or shop) under the message."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order, OrderStatus, Payment, PaymentStatus, Plan
from app.services.notifications import notify_new_subscription, notify_wallet_topup_ok
from app.services.trial_notify import notify_trial_claimed


def _callbacks(markup) -> list[str]:
    if markup is None:
        return []
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class StaffNotifyViewUserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all(
            [
                BotUser(
                    id=1,
                    telegram_id=6495996073,
                    referral_code="c1",
                    role="user",
                    username="sid_official",
                    full_name="SID",
                ),
                BotUser(id=5, telegram_id=5005, referral_code="s5", role="reseller"),
                Plan(id=1, name="پلن ترکیبی", price=150_000, is_active=True),
                Plan(id=2, name="تست", price=0, is_active=True, is_trial=True),
                Order(
                    id=38,
                    user_id=1,
                    plan_id=1,
                    amount=150_000,
                    payment_method="wallet",
                    status=OrderStatus.DELIVERED.value,
                    service_id=11,
                ),
                Order(
                    id=39,
                    user_id=1,
                    plan_id=1,
                    reseller_id=5,
                    amount=150_000,
                    payment_method="wallet",
                    status=OrderStatus.DELIVERED.value,
                    service_id=12,
                    note="renew:12",
                ),
                Order(
                    id=40,
                    user_id=1,
                    plan_id=1,
                    amount=50_000,
                    status=OrderStatus.DELIVERED.value,
                    note="reseller_app:9",
                ),
                Order(
                    id=41,
                    user_id=1,
                    plan_id=2,
                    amount=0,
                    status=OrderStatus.DELIVERED.value,
                    service_id=13,
                ),
                Order(
                    id=42,
                    user_id=1,
                    plan_id=2,
                    reseller_id=5,
                    amount=0,
                    status=OrderStatus.DELIVERED.value,
                    service_id=14,
                ),
                Payment(
                    id=7,
                    user_id=1,
                    amount=20_000,
                    method="card",
                    status=PaymentStatus.APPROVED.value,
                    is_wallet_topup=True,
                ),
            ]
        )
        await self.session.commit()
        self.dispatch = AsyncMock(return_value=1)
        self.patches = [
            patch("app.services.notifications._dispatch_dual_notify", self.dispatch),
            patch(
                "app.services.notifications.get_settings",
                return_value=SimpleNamespace(currency="تومان", admin_ids=[]),
            ),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.session.close()
        await self.engine.dispose()

    async def test_delivered_platform_subscription_opens_user_and_service(self) -> None:
        order = await self.session.get(Order, 38)
        await notify_new_subscription(
            None, self.session, order=order, user_tg_id=None, plan_name="پلن ترکیبی"
        )
        self.assertEqual(
            _callbacks(self.dispatch.await_args.kwargs["markup"]),
            ["adm:users:view:1", "adm:users:svc:1:11"],
        )

    async def test_delivered_shop_renewal_opens_shop_customer_only(self) -> None:
        order = await self.session.get(Order, 39)
        await notify_new_subscription(
            None, self.session, order=order, user_tg_id=None, plan_name="پلن ترکیبی"
        )
        callbacks = _callbacks(self.dispatch.await_args.kwargs["markup"])
        self.assertEqual(callbacks, ["res:user:1"])
        self.assertFalse(any(c.startswith("adm:users:svc:") for c in callbacks))
        self.assertFalse(any(c.startswith("adm:users:view:") for c in callbacks))
        self.assertIn("🔄 تمدید اشتراک", self.dispatch.await_args.args[3])

    async def test_agency_fee_subscription_has_user_without_service(self) -> None:
        order = await self.session.get(Order, 40)
        await notify_new_subscription(None, self.session, order=order, user_tg_id=None)
        callbacks = _callbacks(self.dispatch.await_args.kwargs["markup"])
        self.assertEqual(callbacks, ["adm:users:view:1"])
        self.assertFalse(any(c.startswith("adm:users:svc:") for c in callbacks))

    async def test_needs_approval_keeps_order_review_buttons(self) -> None:
        order = await self.session.get(Order, 38)
        await notify_new_subscription(
            None, self.session, order=order, user_tg_id=None, needs_approval=True
        )
        callbacks = _callbacks(self.dispatch.await_args.kwargs["markup"])
        self.assertTrue(any(c.startswith("ordrev:ok:") for c in callbacks))
        self.assertTrue(any(c.startswith("ordrev:no:") for c in callbacks))
        self.assertNotIn("adm:users:view:1", callbacks)
        self.assertFalse(any(c.startswith("adm:users:svc:") for c in callbacks))

    async def test_platform_wallet_topup_opens_user(self) -> None:
        payment = await self.session.get(Payment, 7)
        await notify_wallet_topup_ok(None, self.session, payment, None)
        self.assertEqual(
            _callbacks(self.dispatch.await_args.kwargs["markup"]),
            ["adm:users:view:1"],
        )

    async def test_platform_trial_opens_user_and_service(self) -> None:
        with patch("app.services.trial_notify._dispatch_dual_notify", self.dispatch):
            await notify_trial_claimed(
                MagicMock(),
                self.session,
                order=await self.session.get(Order, 41),
                user=await self.session.get(BotUser, 1),
            )
        self.assertEqual(
            _callbacks(self.dispatch.await_args.kwargs["markup"]),
            ["adm:users:view:1", "adm:users:svc:1:13"],
        )

    async def test_shop_trial_opens_shop_customer_only(self) -> None:
        with patch("app.services.trial_notify._dispatch_dual_notify", self.dispatch):
            await notify_trial_claimed(
                MagicMock(),
                self.session,
                order=await self.session.get(Order, 42),
                user=await self.session.get(BotUser, 1),
            )
        callbacks = _callbacks(self.dispatch.await_args.kwargs["markup"])
        self.assertEqual(callbacks, ["res:user:1"])
        self.assertFalse(any(c.startswith("adm:users:svc:") for c in callbacks))


if __name__ == "__main__":
    unittest.main()
