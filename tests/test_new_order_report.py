"""«🛒 سفارش جدید» goes to staff when a receipt waits for review, not when a buyer only picks a plan."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers import shop
from app.db import Base
from app.db.models import BotUser, Order, Payment, Plan
from app.services import receipts
from tests.postgres_support import scoped_async_engine


class ReportFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
            Plan(id=1, name="ماهانه", price=210_000, duration_days=30, data_limit_gb=30, is_active=True),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def order_payment(self, *, quantity: int = 1, topup: bool = False) -> Payment:
        order = Order(user_id=1, plan_id=1, amount=210_000, quantity=quantity, payment_method="card")
        self.session.add(order)
        await self.session.flush()
        payment = Payment(user_id=1, order_id=None if topup else order.id, amount=210_000, status="pending",
                          method="card", is_wallet_topup=topup)
        self.session.add(payment)
        await self.session.commit()
        return payment


class PlanPickTests(ReportFixture):
    async def test_picking_a_plan_reports_nothing_to_staff(self) -> None:
        order = Order(user_id=1, plan_id=1, amount=210_000)
        self.session.add(order)
        await self.session.commit()
        callback = SimpleNamespace(data="shop:buy:1", answer=AsyncMock(), message=SimpleNamespace(), bot=None)
        with patch.multiple(
            shop,
            get_all_settings=AsyncMock(return_value={}),
            _answer_shop_maintenance=AsyncMock(return_value=False),
            _record_shop_funnel=AsyncMock(),
            safe_edit_text=AsyncMock(),
            create_order=AsyncMock(return_value=order),
            _show_order_pay=AsyncMock(),
        ), patch("app.services.notifications.notify_new_order", AsyncMock()) as report:
            await shop._complete_shop_buy(
                callback=callback, message=None, session=self.session,
                db_user=SimpleNamespace(id=1, reseller_id=None), state=AsyncMock(), ui={},
                plan=await self.session.get(Plan, 1), plan_id=1,
            )
        report.assert_not_awaited()


class ReceiptTests(ReportFixture):
    async def process(self, payment: Payment, *, auto: bool = False, report: AsyncMock | None = None):
        report = report or AsyncMock()
        with patch.object(receipts, "notify_new_order", report), \
                patch.object(receipts, "notify_pending_approval", AsyncMock()) as review, \
                patch.object(receipts, "get_setting", AsyncMock(return_value="1" if auto else "0")), \
                patch.object(receipts, "auto_approve_receipt", AsyncMock(return_value=None)):
            await receipts.process_receipt(self.session, payment, bot=SimpleNamespace(), user_tg_id=101)
        return report, review

    async def test_a_receipt_waiting_for_review_reports_the_order_with_its_plan(self) -> None:
        report, review = await self.process(await self.order_payment(quantity=3))
        report.assert_awaited_once()
        self.assertEqual(report.await_args.kwargs["plan_name"], "ماهانه ×3")
        review.assert_awaited_once()

    async def test_an_auto_approved_receipt_and_a_wallet_topup_report_no_order(self) -> None:
        report, _review = await self.process(await self.order_payment(), auto=True)
        report.assert_not_awaited()
        report, review = await self.process(await self.order_payment(topup=True))
        report.assert_not_awaited()
        review.assert_awaited_once()

    async def test_a_failed_report_still_sends_the_receipt_for_review(self) -> None:
        failing = AsyncMock(side_effect=TelegramNetworkError(method=SimpleNamespace(), message="down"))
        _report, review = await self.process(await self.order_payment(), report=failing)
        review.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
