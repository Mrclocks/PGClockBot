"""A free trial whose panel create had an unknown outcome is not handed out again."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import shop
from app.db.models import Base, BotUser, DeliveryFailure, Order, OrderStatus, Plan, TrialClaim
from app.services.orders import (
    DeliveryAttempt,
    create_order,
    deliver_order,
    mark_order_free_paid,
    settle_failed_free_delivery,
)
from app.services.pasarguard import PasarGuardError

BUYER = 1
TRIAL_PLAN = 1


class TrialDeliveryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=BUYER, telegram_id=11, referral_code="buyer", role="user"),
            Plan(id=TRIAL_PLAN, name="trial", price=0, is_active=True, is_trial=True, pg_template_id=1),
        ])
        await self.session.commit()
        self.panel_accounts: list[str] = []
        self.pg = SimpleNamespace(
            _login_username=None,
            create_user_from_template=AsyncMock(),
            delete_user_by_id=AsyncMock(),
        )
        self.patches = [
            patch("app.services.users.get_all_settings", AsyncMock(return_value={})),
            patch("app.services.orders.get_pg", return_value=self.pg),
            patch("app.services.orders._hold_delivery_volume", AsyncMock(return_value=None)),
            patch("app.services.orders._assert_plan_volume_available", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.session.close()
        await self.engine.dispose()

    def panel_times_out_after_creating(self) -> None:
        async def create(payload: dict) -> dict:
            self.panel_accounts.append(payload["username"])
            raise httpx.ReadTimeout("slow panel")

        self.pg.create_user_from_template.side_effect = create

    def panel_refuses(self) -> None:
        self.pg.create_user_from_template.side_effect = PasarGuardError("bad template", status_code=422)

    async def failed_trial(self) -> tuple[Order, DeliveryAttempt]:
        order = await create_order(self.session, user_id=BUYER, plan_id=TRIAL_PLAN)
        await mark_order_free_paid(self.session, order, BUYER)
        attempt = DeliveryAttempt()
        with self.assertRaises(Exception):
            await deliver_order(self.session, order, attempt=attempt)
        return order, attempt

    async def count(self, model) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(model)) or 0)


class UnknownOutcomeTests(TrialDeliveryTestCase):
    async def test_panel_timeout_after_create_keeps_trial_spent(self) -> None:
        self.panel_times_out_after_creating()
        order, attempt = await self.failed_trial()

        self.assertTrue(attempt.panel_may_hold_account)
        payment = await settle_failed_free_delivery(self.session, order, attempt=attempt, error="timeout")

        self.assertIsNotNone(payment)
        with self.assertRaisesRegex(ValueError, "قبلاً دریافت"):
            await create_order(self.session, user_id=BUYER, plan_id=TRIAL_PLAN)
        self.assertEqual(len(self.panel_accounts), 1)

    async def test_unknown_outcome_order_is_listed_for_staff(self) -> None:
        self.panel_times_out_after_creating()
        order, attempt = await self.failed_trial()
        await settle_failed_free_delivery(self.session, order, attempt=attempt, error="timeout")
        order_id = int(order.id)

        self.session.expire_all()
        kept = await self.session.get(Order, order_id)
        self.assertEqual(kept.status, OrderStatus.PAID.value)
        self.assertEqual(await self.count(DeliveryFailure), 1)
        self.assertEqual(await self.count(TrialClaim), 1)

    async def test_repeated_timeouts_cannot_mint_more_trials(self) -> None:
        self.panel_times_out_after_creating()
        order, attempt = await self.failed_trial()
        await settle_failed_free_delivery(self.session, order, attempt=attempt, error="timeout")
        for _ in range(5):
            with self.assertRaises(ValueError):
                await create_order(self.session, user_id=BUYER, plan_id=TRIAL_PLAN)
        self.assertEqual(len(self.panel_accounts), 1)


class DefiniteFailureTests(TrialDeliveryTestCase):
    async def test_panel_refusal_releases_trial_for_retry(self) -> None:
        self.panel_refuses()
        order, attempt = await self.failed_trial()

        self.assertFalse(attempt.panel_may_hold_account)
        self.assertIsNone(
            await settle_failed_free_delivery(self.session, order, attempt=attempt, error="refused")
        )
        retry = await create_order(self.session, user_id=BUYER, plan_id=TRIAL_PLAN)
        self.assertNotEqual(retry.id, order.id)
        self.assertEqual(await self.count(DeliveryFailure), 0)

    async def test_failure_before_panel_call_releases_trial_for_retry(self) -> None:
        with patch("app.services.orders.get_pg", side_effect=RuntimeError("panel not configured")):
            order, attempt = await self.failed_trial()

        self.assertFalse(attempt.panel_may_hold_account)
        self.pg.create_user_from_template.assert_not_awaited()
        await settle_failed_free_delivery(self.session, order, attempt=attempt, error="config")
        await create_order(self.session, user_id=BUYER, plan_id=TRIAL_PLAN)


class ShopFreeDeliveryHandlerTests(TrialDeliveryTestCase):
    async def deliver_via_bot(self) -> tuple[Order | None, str, AsyncMock]:
        order = await create_order(self.session, user_id=BUYER, plan_id=TRIAL_PLAN)
        buyer = await self.session.get(BotUser, BUYER)
        notice = AsyncMock()
        with patch("app.services.notifications.notify_approved_delivery_stuck", notice):
            delivered, text = await shop._deliver_free_order(MagicMock(), self.session, buyer, order)
        return delivered, text, notice

    async def test_buyer_told_review_and_staff_alerted_on_unknown_outcome(self) -> None:
        self.panel_times_out_after_creating()
        delivered, text, notice = await self.deliver_via_bot()

        self.assertIsNone(delivered)
        self.assertIn("در حال بررسی", text)
        notice.assert_awaited_once()
        self.assertNotIn("slow panel", text)

    async def test_buyer_may_retry_after_definite_refusal(self) -> None:
        self.panel_refuses()
        delivered, text, notice = await self.deliver_via_bot()

        self.assertIsNone(delivered)
        self.assertIn("خطا در تحویل", text)
        notice.assert_not_awaited()
        await create_order(self.session, user_id=BUYER, plan_id=TRIAL_PLAN)


if __name__ == "__main__":
    unittest.main()
