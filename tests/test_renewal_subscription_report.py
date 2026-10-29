"""Staff delivery reports tag renewals as تمدید, not اشتراک جدید."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order
from app.services.notifications import notify_new_subscription


class RenewalSubscriptionReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all(
            [
                BotUser(
                    id=1,
                    telegram_id=91087008,
                    referral_code="n1",
                    role="user",
                    username="Niloofar_kj",
                    full_name="Niloofar",
                ),
                Order(
                    id=281,
                    user_id=1,
                    amount=55_000,
                    payment_method="card",
                    note="custom",
                ),
                Order(
                    id=283,
                    user_id=1,
                    amount=160_000,
                    payment_method="gateway",
                    note="renew:11",
                    service_id=11,
                    plan_id=2,
                ),
                Order(
                    id=284,
                    user_id=1,
                    amount=10_000,
                    payment_method="wallet",
                    note="renew:11:auto",
                    service_id=11,
                    plan_id=2,
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

    def last_text(self) -> str:
        return self.dispatch.await_args.args[3]

    async def test_new_purchase_keeps_new_subscription_title(self) -> None:
        order = await self.session.get(Order, 281)
        await notify_new_subscription(
            None,
            self.session,
            order=order,
            user_tg_id=None,
            plan_name="پلن دلخواه",
        )
        text = self.last_text()
        self.assertIn("🆕 اشتراک جدید", text)
        self.assertNotIn("تمدید اشتراک", text)

    async def test_renewal_uses_renewal_title(self) -> None:
        order = await self.session.get(Order, 283)
        await notify_new_subscription(
            None,
            self.session,
            order=order,
            user_tg_id=None,
            plan_name="20 گیگ 30 روزه",
        )
        text = self.last_text()
        self.assertIn("🔄 تمدید اشتراک", text)
        self.assertNotIn("🆕 اشتراک جدید", text)
        self.assertIn("20 گیگ 30 روزه", text)
        self.assertIn("#283", text)

    async def test_auto_renewal_note_also_uses_renewal_title(self) -> None:
        order = await self.session.get(Order, 284)
        await notify_new_subscription(
            None,
            self.session,
            order=order,
            user_tg_id=None,
            plan_name="پلن پایه",
        )
        text = self.last_text()
        self.assertIn("🔄 تمدید اشتراک", text)
        self.assertNotIn("🆕 اشتراک جدید", text)


if __name__ == "__main__":
    unittest.main()
