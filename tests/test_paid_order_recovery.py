"""Paid orders the panel did not apply: truthful notices, automatic retry once the panel answers,
and one wallet refund when the retries end without delivery."""

from __future__ import annotations

import importlib.util
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import sqlalchemy as sa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    Base,
    BotUser,
    DeliveryFailure,
    Order,
    Payment,
    PaymentSettlement,
    Plan,
    SettlementStatus,
    ShopWallet,
    UserService,
    WalletTransaction,
)
from app.services import delivery_recovery_policy as policy
from app.services import orders as orders_module
from app.services import paid_order_recovery as recovery
from app.services.delivery import build_delivery_content
from app.services.notifications import notify_new_subscription
from app.services.orders import DeliveryAttempt, deliver_order, pay_with_wallet, renew_service_with_plan
from app.services.payment_settlement import settle_and_approve
from app.services.pasarguard import PasarGuardError
from app.services.receipts import auto_approve_receipt
from app.services.service_renewals import RenewalPendingReview
from app.services.stuck_order_resolution import refund_undelivered_order
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from app.services.ux20 import count_stuck_paid_orders, list_stuck_paid_orders, retry_delivery
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

GB = 1024**3
DAY = 86400
SHOP = 7
ROOT = Path(__file__).resolve().parents[1]


def connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused")


class FakePanel:
    """One panel user; ``down`` refuses connections, ``lose_reply`` applies then drops the answer."""

    def __init__(self) -> None:
        self.info = {"status": "active", "data_limit": 10 * GB, "used_traffic": 3 * GB,
                     "expire": int(time.time()) + 5 * DAY}
        self.writes: list[dict] = []
        self.down = False
        self.lose_reply = False
        self.reset_user_by_id = AsyncMock()
        self.modify_user_with_template = AsyncMock()

    async def get_user_by_id(self, user_id: int) -> dict:
        if self.down:
            raise connect_error()
        return dict(self.info)

    async def modify_user_by_id(self, user_id: int, payload: dict) -> dict:
        if self.down:
            raise connect_error()
        self.writes.append(dict(payload))
        self.info.update(payload)
        if self.lose_reply:
            raise TimeoutError("lost panel response")
        return dict(self.info)


class RecoveryFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add_all([
            BotUser(id=1, telegram_id=1001, referral_code="buyer", wallet_balance=1000),
            BotUser(id=2, telegram_id=1002, referral_code="other", wallet_balance=0),
            Plan(id=1, name="Monthly", price=200, data_limit_gb=10, duration_days=30, pg_template_id=7),
            UserService(id=1, bot_user_id=1, pg_user_id=10, pg_username="svc", plan_id=1),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        self.pg = FakePanel()
        self.stuck_notice = AsyncMock()
        self.patches = [
            patch("app.services.orders.get_pg", return_value=self.pg),
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.loyalty.consume_loyalty_discount_for_order", AsyncMock()),
            patch("app.services.loyalty.on_order_delivered", AsyncMock()),
            patch("app.services.notifications.notify_approved_delivery_stuck", self.stuck_notice),
            patch("app.bot.create_bot", return_value=SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    async def gateway_renewal(self) -> SimpleNamespace:
        """A renewal paid through a gateway whose settle ran while the panel refused connections."""
        service = await self.session.get(UserService, 1)
        plan = await self.session.get(Plan, 1)
        order = await renew_service_with_plan(self.session, user_id=1, service=service, plan=plan)
        payment = Payment(user_id=1, order_id=order.id, amount=order.amount, status="pending", method="psp")
        self.session.add(payment)
        await self.session.flush()
        settlement = PaymentSettlement(
            payment_id=payment.id, shop_owner_id=None, tenant_key=0, channel="psp", provider="mock",
            status=SettlementStatus.AWAITING.value, amount=order.amount,
            idempotency_key=f"psp:{payment.id}", external_ref="ref",
        )
        self.session.add(settlement)
        await self.session.commit()
        self.pg.down = True
        await settle_and_approve(self.session, settlement, expected_amount=order.amount, external_ref="ref")
        return SimpleNamespace(order_id=int(order.id), payment_id=int(payment.id), amount=int(order.amount))

    async def fresh(self, model, row_id):
        self.session.expire_all()
        return await self.session.get(model, row_id)

    async def failure_of(self, order_id: int) -> DeliveryFailure:
        self.session.expire_all()
        return await self.session.scalar(select(DeliveryFailure).where(DeliveryFailure.order_id == order_id))

    async def run_due(self, *, panel_up: bool, now: datetime) -> list[recovery.RecoveryResult]:
        results = []
        async with self.Session() as session:
            ids = await recovery.due_failure_ids(session, now=now)
        for failure_id in ids:
            async with self.Session() as session:
                result = await recovery.recover_one(session, failure_id, now=now, panel_up=panel_up)
                if result is not None:
                    results.append(result)
        return results

    async def wallet_balance(self) -> int:
        return int((await self.fresh(BotUser, 1)).wallet_balance)


def later(minutes: float = 0, hours: float = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes, hours=hours)


class TruthfulNoticeTests(RecoveryFixture):
    async def test_unapplied_renewal_is_not_reported_as_renewed(self) -> None:
        paid = await self.gateway_renewal()
        order = await self.fresh(Order, paid.order_id)
        self.assertEqual(order.status, "paid")
        dispatch = AsyncMock()
        with patch("app.services.notifications._dispatch_dual_notify", dispatch):
            await notify_new_subscription(SimpleNamespace(), self.session, order=order, user_tg_id=1001)
        dispatch.assert_not_awaited()
        payment = await self.session.get(Payment, paid.payment_id)
        text = (await build_delivery_content(self.session, payment, order))["text"]
        self.assertIn(policy.PENDING_RENEWAL_NOTE, text)
        self.assertNotIn("آماده است", text)

    async def test_staff_get_the_stuck_notice_and_the_order_is_scheduled(self) -> None:
        paid = await self.gateway_renewal()
        self.stuck_notice.assert_awaited_once()
        failure = await self.failure_of(paid.order_id)
        self.assertIsNotNone(failure.next_auto_retry_at)
        self.assertIsNone(failure.auto_retry_stopped_at)
        payment = await self.fresh(Payment, paid.payment_id)
        self.assertEqual(payment.status, "approved")
        self.assertNotEqual(payment.review_note, policy.UNKNOWN_DELIVERY_NOTE)

    async def test_delivered_renewal_still_gets_the_staff_notice(self) -> None:
        paid = await self.gateway_renewal()
        self.pg.down = False
        await self.run_due(panel_up=True, now=later(minutes=3))
        dispatch = AsyncMock()
        with patch("app.services.notifications._dispatch_dual_notify", dispatch):
            await notify_new_subscription(
                SimpleNamespace(), self.session, order=await self.fresh(Order, paid.order_id), user_tg_id=1001,
            )
        dispatch.assert_awaited_once()
        self.assertIn("تحویل‌شده", dispatch.await_args.args[3])


class RetryTests(RecoveryFixture):
    async def test_panel_down_spends_no_attempt_and_moves_no_money(self) -> None:
        paid = await self.gateway_renewal()
        self.assertEqual(await self.run_due(panel_up=False, now=later(minutes=3)), [])
        failure = await self.failure_of(paid.order_id)
        self.assertEqual(failure.auto_attempts, 0)
        self.assertIsNone(failure.resolved_at)
        self.assertEqual(self.pg.writes, [])
        self.assertEqual(await self.wallet_balance(), 1000)

    async def test_renewal_is_applied_once_when_the_panel_returns(self) -> None:
        paid = await self.gateway_renewal()
        self.pg.down = False
        results = await self.run_due(panel_up=True, now=later(minutes=3))
        self.assertEqual([(r.order_id, r.outcome) for r in results], [(paid.order_id, recovery.Outcome.DELIVERED)])
        order = await self.fresh(Order, paid.order_id)
        self.assertEqual((order.status, order.service_mutation_pending), ("delivered", False))
        self.assertEqual(self.pg.info["data_limit"], 20 * GB)
        self.assertIsNotNone((await self.failure_of(paid.order_id)).resolved_at)
        # Later runs find nothing: no second write, no second notice.
        self.assertEqual(await self.run_due(panel_up=True, now=later(hours=2)), [])
        self.assertEqual(len(self.pg.writes), 1)

    async def test_a_second_run_cannot_take_a_leased_row(self) -> None:
        paid = await self.gateway_renewal()
        failure = await self.failure_of(paid.order_id)
        now = later(minutes=3)
        async with self.Session() as first, self.Session() as second:
            self.assertTrue(await recovery._lease(first, failure.id, now))
            self.assertFalse(await recovery._lease(second, failure.id, now))

    async def test_lost_reply_is_finished_with_the_saved_target(self) -> None:
        paid = await self.gateway_renewal()
        self.pg.down, self.pg.lose_reply = False, True
        self.assertEqual(await self.run_due(panel_up=True, now=later(minutes=3)), [])
        order = await self.fresh(Order, paid.order_id)
        self.assertTrue(order.service_mutation_pending)
        self.pg.lose_reply = False
        self.pg.info["used_traffic"] += GB
        results = await self.run_due(panel_up=True, now=later(minutes=20))
        self.assertEqual(results[0].outcome, recovery.Outcome.DELIVERED)
        self.assertEqual(self.pg.writes[0], self.pg.writes[1], "the retry re-sends the absolute target")
        self.assertEqual(await self.wallet_balance(), 1000)


class GiveUpTests(RecoveryFixture):
    async def exhaust(self, paid: SimpleNamespace) -> list[recovery.RecoveryResult]:
        """Run every due attempt while the panel answers but refuses the renewal."""
        self.pg.down = False
        results: list[recovery.RecoveryResult] = []
        with patch.object(self.pg, "get_user_by_id", AsyncMock(side_effect=PasarGuardError("bad", status_code=500))):
            for step in range(policy.MAX_AUTO_ATTEMPTS + 2):
                results += await self.run_due(panel_up=True, now=later(hours=step + 1))
        return results

    async def test_refunds_to_the_wallet_once_after_the_last_attempt(self) -> None:
        paid = await self.gateway_renewal()
        results = await self.exhaust(paid)
        self.assertEqual([r.outcome for r in results], [recovery.Outcome.REFUNDED])
        self.assertEqual(results[0].resolution.refunded, paid.amount)
        self.assertEqual(await self.wallet_balance(), 1000 + paid.amount)
        order = await self.fresh(Order, paid.order_id)
        self.assertEqual((order.status, order.service_mutation_pending), ("cancelled", False))
        self.assertEqual(self.pg.writes, [])
        credits = await self.session.scalar(select(func.count(WalletTransaction.id)).where(WalletTransaction.amount > 0))
        self.assertEqual(credits, 1)
        self.assertEqual(await self.run_due(panel_up=True, now=later(hours=30)), [])
        self.assertEqual(await self.wallet_balance(), 1000 + paid.amount)

    async def test_a_long_outage_refunds_at_the_end_of_the_window(self) -> None:
        paid = await self.gateway_renewal()
        window_end = later(hours=policy.AUTO_RETRY_WINDOW_HOURS, minutes=1)
        results = await self.run_due(panel_up=False, now=window_end)
        self.assertEqual([r.outcome for r in results], [recovery.Outcome.REFUNDED])
        self.assertEqual(await self.wallet_balance(), 1000 + paid.amount)

    async def test_a_write_in_doubt_is_handed_to_staff_without_refund(self) -> None:
        paid = await self.gateway_renewal()
        self.pg.down, self.pg.lose_reply = False, True
        await self.run_due(panel_up=True, now=later(minutes=3))
        self.pg.down = True
        results = await self.run_due(panel_up=False, now=later(hours=policy.AUTO_RETRY_WINDOW_HOURS, minutes=1))
        self.assertEqual([r.outcome for r in results], [recovery.Outcome.STAFF])
        self.assertEqual(results[0].reason, recovery.REASON_MAY_BE_APPLIED)
        self.assertEqual(await self.wallet_balance(), 1000)
        failure = await self.failure_of(paid.order_id)
        self.assertIsNone(failure.resolved_at, "it stays in «تحویل ناموفق» for staff")
        self.assertIsNotNone(failure.auto_retry_stopped_at)


class NewPurchaseTests(RecoveryFixture):
    async def paid_purchase(self, *, review_note: str | None = None) -> Order:
        order = Order(user_id=1, plan_id=1, amount=300, status="paid", payment_method="psp")
        self.session.add(order)
        await self.session.flush()
        self.session.add(Payment(user_id=1, order_id=order.id, amount=300, status="approved",
                                 method="psp", review_note=review_note))
        self.session.add(DeliveryFailure(order_id=order.id, error="x", next_auto_retry_at=later(minutes=-1)))
        await self.session.commit()
        return order

    async def test_unknown_create_is_never_repeated(self) -> None:
        order = await self.paid_purchase(review_note=policy.UNKNOWN_DELIVERY_NOTE)
        create = AsyncMock()
        with patch.object(orders_module, "deliver_order", create):
            self.assertEqual(await self.run_due(panel_up=True, now=later()), [])
        create.assert_not_awaited()
        failure = await self.failure_of(order.id)
        self.assertIsNotNone(failure.auto_retry_stopped_at)
        self.assertEqual(await self.wallet_balance(), 1000)

    async def test_refused_connection_is_a_known_failure(self) -> None:
        order = await self.paid_purchase()
        self.pg.create_user_from_template = AsyncMock(side_effect=connect_error())
        attempt = DeliveryAttempt()
        with patch("app.services.orders._hold_and_charge_delivery", AsyncMock(return_value=(None, None))), \
                self.assertRaises(httpx.ConnectError):
            await deliver_order(self.session, order, attempt=attempt)
        self.assertFalse(attempt.panel_may_hold_account)

    async def test_timeout_is_an_unknown_outcome(self) -> None:
        order = await self.paid_purchase()
        self.pg.create_user_from_template = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        attempt = DeliveryAttempt()
        with patch("app.services.orders._hold_and_charge_delivery", AsyncMock(return_value=(None, None))), \
                self.assertRaises(httpx.ReadTimeout):
            await deliver_order(self.session, order, attempt=attempt)
        self.assertTrue(attempt.panel_may_hold_account)


class ShopRefundTests(RecoveryFixture):
    async def test_shop_order_refund_goes_to_that_shops_wallet_only(self) -> None:
        self.session.add(BotUser(id=SHOP, telegram_id=7007, referral_code="shop", role="reseller"))
        order = Order(user_id=1, plan_id=1, amount=500, status="paid", reseller_id=SHOP)
        self.session.add(order)
        await self.session.flush()
        self.session.add(Payment(user_id=1, order_id=order.id, amount=500, status="approved", method="psp"))
        await self.session.commit()
        order_id = int(order.id)
        result = await refund_undelivered_order(self.session, order, now=later())
        self.assertEqual((result.refunded, result.shop_id), (500, SHOP))
        purse = await self.session.scalar(select(ShopWallet).where(ShopWallet.user_id == 1, ShopWallet.reseller_id == SHOP))
        self.assertEqual(purse.balance, 500)
        self.assertEqual(await self.wallet_balance(), 1000, "the platform purse is untouched")
        with self.assertRaises(ValueError):
            await refund_undelivered_order(self.session, await self.fresh(Order, order_id), now=later())
        purse = await self.session.scalar(select(ShopWallet).where(ShopWallet.user_id == 1, ShopWallet.reseller_id == SHOP))
        self.assertEqual(purse.balance, 500)

    async def test_refund_is_refused_while_a_renewal_write_is_in_doubt(self) -> None:
        order = Order(user_id=1, plan_id=1, amount=200, status="paid", note="renew:1", service_id=1,
                      service_mutation_pending=True, renewal_snapshot=json.dumps({"version": 1, "target": {}}))
        self.session.add(order)
        await self.session.flush()
        self.session.add(Payment(user_id=1, order_id=order.id, amount=200, status="approved", method="psp"))
        await self.session.commit()
        with self.assertRaises(ValueError):
            await refund_undelivered_order(self.session, order, now=later())
        self.assertEqual(await self.wallet_balance(), 1000)


class WalletRenewalTests(RecoveryFixture):
    async def test_lost_reply_is_queued_for_retry_and_the_buyer_may_retap(self) -> None:
        service = await self.session.get(UserService, 1)
        order = await renew_service_with_plan(self.session, user_id=1, service=service,
                                              plan=await self.session.get(Plan, 1))
        buyer = await self.session.get(BotUser, 1)
        order_id = int(order.id)
        self.pg.lose_reply = True
        with self.assertRaises(RenewalPendingReview):
            await pay_with_wallet(self.session, order, buyer)
        failure = await self.failure_of(order_id)
        self.assertIsNotNone(failure.next_auto_retry_at)
        payment = await self.session.scalar(select(Payment).where(Payment.order_id == order_id))
        self.assertNotEqual(payment.review_note, policy.UNKNOWN_DELIVERY_NOTE)
        self.pg.lose_reply = False
        fresh_order = await self.fresh(Order, order_id)
        delivered = await pay_with_wallet(self.session, fresh_order, await self.session.get(BotUser, 1))
        self.assertEqual(delivered.status, "delivered")
        self.assertIsNotNone((await self.failure_of(order_id)).resolved_at)
        self.assertEqual(await self.wallet_balance(), 800)


class StuckListTests(RecoveryFixture):
    async def test_unapplied_renewal_is_listed_as_stuck(self) -> None:
        paid = await self.gateway_renewal()
        listed = await list_stuck_paid_orders(self.session, reseller_id=None)
        self.assertEqual([o.id for o in listed], [paid.order_id])
        self.assertEqual(await count_stuck_paid_orders(self.session, reseller_id=None), 1)
        self.assertEqual(await count_stuck_paid_orders(self.session, reseller_id=SHOP), 0)

    async def test_retry_button_does_not_take_a_fresh_delivering_order(self) -> None:
        paid = await self.gateway_renewal()
        await self.session.execute(sa.update(Order).where(Order.id == paid.order_id).values(status="delivering"))
        await self.session.commit()
        with self.assertRaisesRegex(ValueError, "در جریان"):
            await retry_delivery(self.session, paid.order_id)
        self.assertEqual((await self.fresh(Order, paid.order_id)).status, "delivering")

    async def test_retry_button_does_not_deliver_an_unpaid_order(self) -> None:
        order = Order(user_id=1, plan_id=1, amount=300, status="awaiting_approval")
        self.session.add(order)
        await self.session.commit()
        with self.assertRaisesRegex(ValueError, "پرداخت تأییدشده"):
            await retry_delivery(self.session, order.id)
        self.assertEqual((await self.fresh(Order, order.id)).status, "awaiting_approval")


class AutoApprovedReceiptTests(RecoveryFixture):
    async def test_buyer_is_told_the_truth_and_staff_are_alerted(self) -> None:
        service = await self.session.get(UserService, 1)
        order = await renew_service_with_plan(self.session, user_id=1, service=service,
                                              plan=await self.session.get(Plan, 1))
        payment = Payment(user_id=1, order_id=order.id, amount=order.amount, status="pending",
                          method="card", receipt_file_id="photo")
        self.session.add(payment)
        await self.session.commit()
        order_id, payment_id = int(order.id), int(payment.id)
        self.pg.down = True
        stuck, pending_review = AsyncMock(), AsyncMock()
        with patch("app.services.receipts.notify_approved_delivery_stuck", stuck), \
                patch("app.services.receipts.notify_pending_approval", pending_review):
            text = await auto_approve_receipt(self.session, payment, bot=SimpleNamespace(), user_tg_id=1001)
        self.assertIn(policy.PENDING_RENEWAL_NOTE, text)
        self.assertNotIn("ادمین", text, "the buyer is not told about staff buttons")
        stuck.assert_awaited_once()
        pending_review.assert_not_awaited()
        self.assertEqual((await self.fresh(Payment, payment_id)).status, "approved")
        self.assertIsNotNone((await self.failure_of(order_id)).next_auto_retry_at)


class PolicyTests(unittest.TestCase):
    def test_unknown_note_matches_the_orders_module(self) -> None:
        self.assertEqual(policy.UNKNOWN_DELIVERY_NOTE, orders_module.UNKNOWN_DELIVERY_NOTE)

    def test_additive_orders_are_never_retried_automatically(self) -> None:
        for note in ("svc_addon:1:2", "res_cap:1", "res_pkg:1", "reseller_app:1"):
            self.assertFalse(policy.is_auto_recoverable_kind(Order(note=note)), note)
        self.assertTrue(policy.is_auto_recoverable_kind(Order(note="renew:1")))
        self.assertTrue(policy.is_auto_recoverable_kind(Order(note=None)))

    def test_backoff_grows_and_is_capped(self) -> None:
        delays = [recovery.retry_delay(n) for n in range(1, 9)]
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[-1], recovery.RETRY_BACKOFF[-1])


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_stops_old_rows_and_downgrade_keeps_them(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = ROOT / "alembic/versions/0119_delivery_failure_auto_retry.py"
        spec = importlib.util.spec_from_file_location("auto_retry_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE delivery_failures (id INTEGER PRIMARY KEY, order_id INTEGER, error TEXT, "
                "attempts INTEGER, resolved_at TIMESTAMPTZ)"
            ))
            conn.execute(text("INSERT INTO delivery_failures VALUES (1, 5, 'old', 1, NULL), (2, 6, 'done', 1, '2026-01-01')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                rows = conn.execute(text(
                    "SELECT id, auto_attempts, auto_retry_stopped_at IS NOT NULL FROM delivery_failures ORDER BY id"
                )).all()
                self.assertEqual(rows, [(1, 0, 1), (2, 0, 0)])
                self.assertIn(migration.INDEX, {i["name"] for i in sa.inspect(conn).get_indexes("delivery_failures")})
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT error FROM delivery_failures ORDER BY id")).scalars().all(),
                                 ["old", "done"])
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
