"""Staff reports about a buyer show their name, @username and plain numeric Telegram id."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order, Payment, PaymentStatus
from app.services.notifications import (
    notify_new_order,
    notify_new_subscription,
    notify_pending_approval,
    notify_wallet_topup_ok,
)

BUYER_TG = 123456789


class NumericIdInReportsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=BUYER_TG, referral_code="b1", role="user", username="buyer"),
            BotUser(id=2, telegram_id=-5, referral_code="b2", role="user", username="synthetic"),
            BotUser(id=3, telegram_id=987654321, referral_code="b3", role="user", username="farid_g",
                    full_name="Farid <b>Ghaderi</b>"),
            Order(id=1, user_id=1, amount=5_000),
            Payment(id=1, user_id=1, amount=1_700_000, method="card", status=PaymentStatus.APPROVED.value,
                    is_wallet_topup=True),
            Payment(id=2, user_id=1, amount=5_000, method="card", status=PaymentStatus.PENDING.value, order_id=1),
            Payment(id=3, user_id=2, amount=1_000, method="card", status=PaymentStatus.APPROVED.value,
                    is_wallet_topup=True),
            Order(id=2, user_id=3, amount=210_000),
        ])
        await self.session.commit()
        self.dispatch = AsyncMock(return_value=1)
        self.patches = [
            patch("app.services.notifications._dispatch_dual_notify", self.dispatch),
            patch("app.services.notifications.notify_enabled", AsyncMock(return_value=True)),
            patch("app.services.notifications.get_settings",
                  return_value=SimpleNamespace(currency="تومان", admin_ids=[])),
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

    async def test_each_buyer_report_shows_the_numeric_id(self) -> None:
        order = await self.session.get(Order, 1)
        reports = {
            "wallet top-up": notify_wallet_topup_ok(None, self.session, await self.session.get(Payment, 1), None),
            "pending approval": notify_pending_approval(None, self.session, await self.session.get(Payment, 2), None),
            "new subscription": notify_new_subscription(None, self.session, order=order, user_tg_id=None,
                                                        user_name="@buyer"),
            "new order": notify_new_order(None, self.session, order=order, user_tg_id=None),
        }
        for name, report in reports.items():
            with self.subTest(report=name):
                await report
                text = self.last_text()
                self.assertIn("<b>کاربر:</b> @buyer", text)
                self.assertIn(f"<b>آیدی:</b> {BUYER_TG}", text, "plain text, so Telegram offers it for copying")
                self.assertNotIn(f"<code>{BUYER_TG}</code>", text)
                self.assertNotIn("یوزرنیم", text, "the username is already the buyer's label")

    async def test_name_username_and_id_each_have_a_line_and_the_name_is_escaped(self) -> None:
        await notify_new_subscription(None, self.session, order=await self.session.get(Order, 2), user_tg_id=None,
                                      user_name="Farid <b>Ghaderi</b>")
        text = self.last_text()
        self.assertIn("<b>کاربر:</b> Farid &lt;b&gt;Ghaderi&lt;/b&gt;", text)
        self.assertIn("<b>یوزرنیم:</b> @farid_g", text)
        self.assertIn("<b>آیدی:</b> 987654321", text)
        self.assertLess(text.index("کاربر"), text.index("یوزرنیم"))
        self.assertLess(text.index("یوزرنیم"), text.index("آیدی"))

    async def test_a_synthetic_id_is_not_shown(self) -> None:
        await notify_wallet_topup_ok(None, self.session, await self.session.get(Payment, 3), None)
        self.assertNotIn("آیدی", self.last_text())


if __name__ == "__main__":
    unittest.main()
