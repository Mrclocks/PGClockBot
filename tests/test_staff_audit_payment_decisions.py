"""Payment decisions and free checkouts in the staff log, and the staff lines of the daily report."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order, OrderStatus, Payment, PaymentStatus, ShopStaffAction
from app.services.daily_report import staff_summary
from app.services.orders import approve_payment, reject_payment
from app.services.payment_decision_audit import record_free_checkout
from app.services.staff_audit import ACTOR_SHOP_OWNER, ACTOR_STAFF, actor_from_staff, list_staff_actions

SHOP = 10
OTHER_SHOP = 20
CUSTOMER = 30
STAFF_MEMBER = 40
SHOP_OWNER_TG = 1010
STAFF_TG = 4040


class DecisionCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=SHOP_OWNER_TG, referral_code="shop", role="reseller", full_name="Shop Owner"),
                BotUser(id=OTHER_SHOP, telegram_id=2020, referral_code="other", role="reseller"),
                BotUser(id=CUSTOMER, telegram_id=3030, referral_code="cust", reseller_id=SHOP),
                BotUser(id=STAFF_MEMBER, telegram_id=STAFF_TG, referral_code="staff", full_name="<b>Ali</b>"),
            ])
            await session.commit()
        self.settings = patch("app.services.payment_decision_audit.get_settings", return_value=SimpleNamespace(admin_ids=[]))
        self.settings.start()
        self.addCleanup(self.settings.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.tmp.cleanup()

    async def add_order_payment(self, session, *, amount: int = 5000) -> Payment:
        order = Order(user_id=CUSTOMER, reseller_id=SHOP, amount=amount, status=OrderStatus.AWAITING_APPROVAL.value)
        session.add(order)
        await session.flush()
        payment = Payment(user_id=CUSTOMER, order_id=order.id, amount=amount, status=PaymentStatus.PENDING.value, receipt_file_id="r")
        session.add(payment)
        await session.commit()
        return payment

    async def log(self, session, shop_id: int | None) -> list[ShopStaffAction]:
        return await list_staff_actions(session, shop_id=shop_id)


class PaymentDecisionLogTests(DecisionCase):
    async def test_bot_rejection_is_logged_in_the_payment_shop_with_the_reviewer(self) -> None:
        async with self.Session() as session:
            payment = await self.add_order_payment(session)
            await reject_payment(session, payment, SHOP_OWNER_TG, "rejected")
            rows = await self.log(session, SHOP)
        self.assertEqual([(r.action, r.actor_kind, r.actor_user_id, r.amount) for r in rows],
                         [("payment_reject", ACTOR_SHOP_OWNER, SHOP, 5000)])

    async def test_web_approval_uses_the_signed_in_staff_member(self) -> None:
        staff = {"role": "staff", "bot_user_id": STAFF_MEMBER, "username": "ali"}
        async with self.Session() as session:
            payment = await self.add_order_payment(session)
            with patch("app.services.orders.fulfill_paid_order", AsyncMock(return_value=None)):
                await approve_payment(session, payment, reviewer_tg=0, actor=actor_from_staff(staff, shop_id=None))
            rows = await self.log(session, SHOP)
        self.assertEqual([(r.action, r.actor_kind, r.actor_user_id) for r in rows], [("payment_approve", ACTOR_STAFF, STAFF_MEMBER)])

    async def test_repeated_decision_adds_no_second_record(self) -> None:
        async with self.Session() as session:
            payment = await self.add_order_payment(session)
            await reject_payment(session, payment, STAFF_TG, "rejected")
            with self.assertRaises(ValueError):
                await reject_payment(session, payment, STAFF_TG, "rejected")
            self.assertEqual(len(await self.log(session, SHOP)), 1)

    async def test_automatic_approval_is_not_a_staff_action(self) -> None:
        async with self.Session() as session:
            payment = await self.add_order_payment(session)
            with patch("app.services.orders.fulfill_paid_order", AsyncMock(return_value=None)):
                await approve_payment(session, payment, reviewer_tg=0)
            self.assertEqual(await self.log(session, SHOP), [])

    async def test_another_shop_and_the_platform_do_not_see_the_record(self) -> None:
        async with self.Session() as session:
            payment = await self.add_order_payment(session)
            await reject_payment(session, payment, STAFF_TG, "rejected")
            self.assertEqual(await self.log(session, OTHER_SHOP), [])
            self.assertEqual(await self.log(session, None), [])

    async def test_free_checkout_is_logged_against_the_order(self) -> None:
        async with self.Session() as session:
            order = Order(id=77, user_id=CUSTOMER, reseller_id=SHOP, amount=0, status=OrderStatus.PAID.value)
            session.add(order)
            await session.flush()
            await record_free_checkout(session, order, actor_id=STAFF_MEMBER)
            await session.commit()
            rows = await self.log(session, SHOP)
        self.assertEqual([(r.action, r.target_type, r.target_id, r.actor_label) for r in rows],
                         [("free_checkout", "order", 77, "<b>Ali</b>")])


class StaffSummaryTests(DecisionCase):
    async def test_summary_counts_each_member_escapes_names_and_keeps_shops_apart(self) -> None:
        start = datetime(2026, 11, 9, 20, 30, tzinfo=timezone.utc)
        inside = start + timedelta(hours=2)
        def row(shop, label, action, when):
            return ShopStaffAction(shop_id=shop, actor_kind="staff", actor_label=label, action=action,
                                   target_type="payment", target_id=1, created_at=when)
        async with self.Session() as session:
            session.add_all([
                row(SHOP, "<b>Ali</b>", "payment_approve", inside),
                row(SHOP, "<b>Ali</b>", "payment_approve", inside),
                row(SHOP, "<b>Ali</b>", "payment_reject", inside),
                row(SHOP, "Sara", "wallet_credit", inside),
                row(SHOP, "Old", "payment_approve", start - timedelta(hours=1)),
                row(OTHER_SHOP, "Other", "payment_approve", inside),
            ])
            await session.commit()
            text = await staff_summary(session, reseller_id=SHOP, start=start, end=start + timedelta(days=1))
            empty = await staff_summary(session, reseller_id=None, start=start, end=start + timedelta(days=1))
        self.assertEqual(text.splitlines(), ["• &lt;b&gt;Ali&lt;/b&gt;: 3 اقدام (2 تأیید رسید، 1 رد رسید)", "• Sara: 1 اقدام"])
        self.assertEqual(empty, "—")


if __name__ == "__main__":
    unittest.main()
