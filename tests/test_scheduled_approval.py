"""Scheduled approval of card receipts nobody reviewed: who qualifies, per shop, and what never does."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order, Payment, PaymentReceiptFingerprint, Role
from app.services import scheduled_approval as sa_mod
from app.services.orders import attach_receipt
from app.services.scheduled_approval import (
    MAX_MINUTES,
    ApprovalRule,
    approve_due_receipts,
    due_payments,
    rule_from_settings,
    shops_with_waiting_receipts,
)
from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
RULE = ApprovalRule(minutes=5, max_amount=0, returning_only=False)


class RuleTests(unittest.TestCase):
    def test_the_rule_is_off_by_default_and_for_anything_malformed(self) -> None:
        self.assertEqual(DEFAULT_SETTINGS["auto_approve_after_minutes"], "0")
        for raw in (None, "", "0", "abc", "-5", "1.5", "٥", True):
            with self.subTest(raw=raw):
                self.assertIsNone(rule_from_settings({"auto_approve_after_minutes": raw}))
        self.assertIsNone(rule_from_settings({}))

    def test_the_rule_reads_the_minutes_cap_and_the_returning_only_switch(self) -> None:
        rule = rule_from_settings({"auto_approve_after_minutes": "7", "auto_approve_after_max_amount": "250000"})
        self.assertEqual((rule.minutes, rule.max_amount, rule.returning_only), (7, 250000, True))
        off = rule_from_settings({"auto_approve_after_minutes": "7", "auto_approve_after_returning_only": "0"})
        self.assertFalse(off.returning_only)
        self.assertEqual(rule_from_settings({"auto_approve_after_minutes": "999999"}).minutes, MAX_MINUTES)
        self.assertIn("7", rule.reason)

    def test_the_settings_are_offered_in_the_payment_settings_group(self) -> None:
        keys = {item[0] for items in SETTING_GROUPS.values() for item in items}
        self.assertTrue({"auto_approve_after_minutes", "auto_approve_after_max_amount", "auto_approve_after_returning_only"} <= keys)


class ApprovalTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.counter = 0
        async with self.Session() as session:
            self.shop_a = await self._user(session, role=Role.RESELLER.value)
            self.shop_b = await self._user(session, role=Role.RESELLER.value)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, *, role: str = Role.USER.value, blocked: bool = False) -> BotUser:
        self.counter += 1
        user = BotUser(
            telegram_id=7000 + self.counter, username=f"u{self.counter}", full_name=f"user {self.counter}",
            role=role, referral_code=f"R{self.counter}", is_blocked=blocked,
        )
        session.add(user)
        await session.flush()
        return user

    async def _receipt(
        self, session, *, shop: int | None = None, minutes_ago: float = 10, amount: int = 90000, method: str = "card",
        topup: bool = False, order_status: str = "awaiting_approval", note: str | None = None, user: BotUser | None = None,
        status: str = "pending", fingerprint: str | None = "unique", file_id: str = "AgACfileid", with_receipt_at: bool = True,
    ) -> Payment:
        user = user or await self._user(session)
        order = Order(user_id=user.id, reseller_id=shop, amount=amount, status=order_status, note=note)
        session.add(order)
        await session.flush()
        self.counter += 1
        payment = Payment(
            order_id=None if topup else order.id, user_id=user.id, amount=amount, method=method, status=status,
            receipt_file_id=file_id, is_wallet_topup=topup, wallet_shop_id=shop if topup else None,
            receipt_at=NOW - timedelta(minutes=minutes_ago) if with_receipt_at else None,
        )
        session.add(payment)
        await session.flush()
        if fingerprint is not None:
            session.add(PaymentReceiptFingerprint(payment_id=payment.id, file_unique_id=f"{fingerprint}-{self.counter}", sha256=None))
        return payment

    async def _due(self, session, shop: int | None = None, rule: ApprovalRule = RULE) -> list[int]:
        return [p.id for p in await due_payments(session, shop_id=shop, rule=rule, now=NOW)]


class SelectionTests(ApprovalTestCase):
    async def test_a_receipt_is_due_only_after_the_chosen_time_and_never_without_a_timestamp(self) -> None:
        async with self.Session() as session:
            old = await self._receipt(session, minutes_ago=6)
            await self._receipt(session, minutes_ago=4)
            await self._receipt(session, minutes_ago=600, with_receipt_at=False)
            await session.commit()
            self.assertEqual(await self._due(session), [old.id])
            self.assertEqual(await self._due(session, rule=ApprovalRule(30, 0, False)), [])

    async def test_nothing_unsafe_ever_qualifies(self) -> None:
        async with self.Session() as session:
            good = await self._receipt(session)
            await self._receipt(session, topup=True)
            await self._receipt(session, method="crypto")
            await self._receipt(session, method="wallet")
            for note in ("renew:3", "svc_addon:1:2:volume:5", "reseller_app:9"):
                await self._receipt(session, note=note)
            await self._receipt(session, order_status="pending")
            await self._receipt(session, order_status="delivered")
            await self._receipt(session, status="approved")
            await self._receipt(session, status="rejected")
            await self._receipt(session, user=await self._user(session, blocked=True))
            await self._receipt(session, file_id=None)
            await session.commit()
            self.assertEqual(await self._due(session), [good.id])

    async def test_a_duplicate_or_unfingerprinted_receipt_stays_for_a_person(self) -> None:
        async with self.Session() as session:
            first = await self._receipt(session, fingerprint=None)
            second = await self._receipt(session, fingerprint=None)
            lonely = await self._receipt(session, fingerprint=None)
            session.add_all([
                PaymentReceiptFingerprint(payment_id=first.id, file_unique_id="same-file", sha256=None),
                PaymentReceiptFingerprint(payment_id=second.id, file_unique_id="same-file", sha256=None),
            ])
            third = await self._receipt(session, fingerprint=None)
            fourth = await self._receipt(session, fingerprint=None)
            session.add_all([
                PaymentReceiptFingerprint(payment_id=third.id, file_unique_id="other-1", sha256="ab" * 32),
                PaymentReceiptFingerprint(payment_id=fourth.id, file_unique_id="other-2", sha256="ab" * 32),
            ])
            await session.commit()
            self.assertEqual(await self._due(session), [], "duplicates by file or by image, and a missing fingerprint, are manual")
            self.assertNotIn(lonely.id, await self._due(session))

    async def test_the_amount_cap_and_the_returning_customer_rule(self) -> None:
        async with self.Session() as session:
            returning = await self._user(session)
            session.add(Order(user_id=returning.id, reseller_id=None, amount=50000, status="delivered"))
            other_shop_buyer = await self._user(session)
            session.add(Order(user_id=other_shop_buyer.id, reseller_id=self.shop_a.id, amount=50000, status="delivered"))
            free_only = await self._user(session)
            session.add(Order(user_id=free_only.id, reseller_id=None, amount=0, status="delivered"))
            cheap = await self._receipt(session, amount=40000, user=returning)
            pricey = await self._receipt(session, amount=900000, user=returning)
            new_customer = await self._receipt(session, amount=40000)
            elsewhere = await self._receipt(session, amount=40000, user=other_shop_buyer)
            trial_only = await self._receipt(session, amount=40000, user=free_only)
            await session.commit()
            self.assertEqual(sorted(await self._due(session, rule=ApprovalRule(5, 100000, False))), sorted([cheap.id, new_customer.id, elsewhere.id, trial_only.id]))
            self.assertEqual(await self._due(session, rule=ApprovalRule(5, 0, True)), [cheap.id, pricey.id])
            self.assertEqual(await self._due(session, rule=ApprovalRule(5, 100000, True)), [cheap.id])

    async def test_each_shop_only_ever_sees_its_own_receipts(self) -> None:
        async with self.Session() as session:
            platform = await self._receipt(session, shop=None)
            a = await self._receipt(session, shop=self.shop_a.id)
            b = await self._receipt(session, shop=self.shop_b.id)
            await session.commit()
            self.assertEqual(await self._due(session, None), [platform.id])
            self.assertEqual(await self._due(session, self.shop_a.id), [a.id])
            self.assertEqual(await self._due(session, self.shop_b.id), [b.id])
            self.assertEqual(set(await shops_with_waiting_receipts(session)), {None, self.shop_a.id, self.shop_b.id})

    async def test_the_oldest_receipts_come_first_and_a_batch_is_bounded(self) -> None:
        async with self.Session() as session:
            newest = await self._receipt(session, minutes_ago=6)
            oldest = await self._receipt(session, minutes_ago=90)
            middle = await self._receipt(session, minutes_ago=30)
            await session.commit()
            self.assertEqual(await self._due(session), [oldest.id, middle.id, newest.id])
            self.assertEqual(len(await due_payments(session, shop_id=None, rule=RULE, now=NOW, limit=2)), 2)


class AttachReceiptTests(ApprovalTestCase):
    async def test_attaching_a_receipt_records_when_and_a_resend_restarts_the_clock(self) -> None:
        async with self.Session() as session:
            payment = await self._receipt(session, with_receipt_at=False, file_id=None)
            await session.commit()
            await attach_receipt(session, payment, "FILE-1")
            first = (await session.get(Payment, payment.id, populate_existing=True)).receipt_at
            self.assertIsNotNone(first)
            await attach_receipt(session, payment, "FILE-2")
            second = (await session.get(Payment, payment.id, populate_existing=True)).receipt_at
        self.assertGreaterEqual(second, first)


class RunnerTests(ApprovalTestCase):
    def _bots(self, *, available: bool = True):
        opened: list[int | None] = []
        closed: list[object] = []
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock(side_effect=lambda: closed.append(1))))

        async def open_bot(shop_id):
            opened.append(shop_id)
            return (bot, True) if available else (None, False)

        return bot, opened, closed, open_bot

    @staticmethod
    def _settings(per_shop: dict):
        async def settings_for(shop_id):
            return per_shop.get(shop_id, {})

        return settings_for

    async def test_only_shops_that_turned_it_on_get_their_due_receipts_approved(self) -> None:
        async with self.Session() as session:
            platform = await self._receipt(session, shop=None)
            a = await self._receipt(session, shop=self.shop_a.id)
            await self._receipt(session, shop=self.shop_b.id)
            await session.commit()
            bot, opened, closed, open_bot = self._bots()
            approve = AsyncMock(side_effect=self._decide(session))
            settings = {self.shop_a.id: {"auto_approve_after_minutes": "5", "auto_approve_after_returning_only": "0"}}
            with patch.object(sa_mod, "auto_approve_receipt", approve), patch.object(sa_mod, "datetime", wraps=datetime) as clock:
                clock.now.return_value = NOW
                total = await approve_due_receipts(session, settings_for=self._settings(settings), open_bot=open_bot, now=NOW)
            self.assertEqual(total, 1)
            self.assertEqual(opened, [self.shop_a.id])
            self.assertEqual(len(closed), 1)
            self.assertEqual([c.args[1].id for c in approve.await_args_list], [a.id])
            self.assertIn("5", approve.await_args.kwargs["reason"])
            self.assertEqual((await session.get(Payment, platform.id, populate_existing=True)).status, "pending")

    def _decide(self, session):
        async def decide(sess, payment, **kwargs):
            payment.status = "approved"
            await sess.commit()

        return decide

    async def test_a_receipt_a_person_decided_meanwhile_is_left_alone(self) -> None:
        async with self.Session() as session:
            payment = await self._receipt(session)
            await session.commit()
            _bot, _opened, _closed, open_bot = self._bots()
            async with self.Session() as admin:
                (await admin.get(Payment, payment.id)).status = "approved"
                await admin.commit()
            approve = AsyncMock()
            with patch.object(sa_mod, "auto_approve_receipt", approve), patch.object(sa_mod, "due_payments", AsyncMock(return_value=[payment])):
                total = await approve_due_receipts(
                    session, settings_for=self._settings({None: {"auto_approve_after_minutes": "5"}}), open_bot=open_bot, now=NOW
                )
        self.assertEqual(total, 0)
        approve.assert_not_awaited()

    async def test_a_receipt_that_could_not_be_approved_is_not_retried_every_minute(self) -> None:
        async with self.Session() as session:
            broken = await self._receipt(session)
            raising = await self._receipt(session)
            fine = await self._receipt(session)
            await session.commit()
            broken_id, raising_id, fine_id = broken.id, raising.id, fine.id  # plain ints: a rollback expires the objects
            _bot, _opened, _closed, open_bot = self._bots()

            async def decide(sess, payment, **kwargs):
                if payment.id == broken_id:
                    return "manual review"  # the approval did not complete: payment stays pending
                if payment.id == raising_id:
                    raise RuntimeError("panel down")
                payment.status = "approved"
                await sess.commit()

            settings = {None: {"auto_approve_after_minutes": "5", "auto_approve_after_returning_only": "0"}}
            with patch.object(sa_mod, "auto_approve_receipt", AsyncMock(side_effect=decide)):
                first = await approve_due_receipts(session, settings_for=self._settings(settings), open_bot=open_bot, now=NOW)
                second = await approve_due_receipts(session, settings_for=self._settings(settings), open_bot=open_bot, now=NOW)
        async with self.Session() as check:
            rows = {p.id: (p.status, p.receipt_at) for p in (await check.scalars(select(Payment))).all()}
        self.assertEqual((first, second), (1, 0))
        self.assertEqual(rows[fine_id][0], "approved")
        for payment_id in (broken_id, raising_id):
            self.assertEqual(rows[payment_id], ("pending", None))

    async def test_a_shop_without_a_bot_is_skipped_and_its_receipts_stay_due(self) -> None:
        async with self.Session() as session:
            payment = await self._receipt(session)
            await session.commit()
            _bot, opened, _closed, open_bot = self._bots(available=False)
            approve = AsyncMock()
            with patch.object(sa_mod, "auto_approve_receipt", approve):
                total = await approve_due_receipts(
                    session, settings_for=self._settings({None: {"auto_approve_after_minutes": "5", "auto_approve_after_returning_only": "0"}}),
                    open_bot=open_bot, now=NOW,
                )
            self.assertEqual((total, opened), (0, [None]))
            approve.assert_not_awaited()
            self.assertEqual(await self._due(session, rule=ApprovalRule(5, 0, False)), [payment.id])

    async def test_a_failing_shop_does_not_stop_the_others(self) -> None:
        async with self.Session() as session:
            await self._receipt(session, shop=self.shop_a.id)
            good = await self._receipt(session, shop=self.shop_b.id)
            await session.commit()
            _bot, _opened, _closed, open_bot = self._bots()

            async def settings_for(shop_id):
                if shop_id == self.shop_a.id:
                    raise RuntimeError("settings unreadable")
                return {"auto_approve_after_minutes": "5", "auto_approve_after_returning_only": "0"}

            approve = AsyncMock(side_effect=self._decide(session))
            with patch.object(sa_mod, "auto_approve_receipt", approve):
                total = await approve_due_receipts(session, settings_for=settings_for, open_bot=open_bot, now=NOW)
            self.assertEqual(total, 1)
            self.assertEqual((await session.get(Payment, good.id, populate_existing=True)).status, "approved")


class InstantPathStaysTests(ApprovalTestCase):
    async def test_the_immediate_auto_approval_still_runs_the_shared_flow(self) -> None:
        from app.services import receipts

        async with self.Session() as session:
            payment = await self._receipt(session, shop=None)
            await session.commit()
            shared = AsyncMock(return_value=None)
            with (
                patch.object(receipts, "get_setting", AsyncMock(return_value="1")),
                patch.object(receipts, "auto_approve_receipt", shared),
            ):
                result = await receipts.process_receipt(session, payment, bot=AsyncMock(), user_tg_id=11)
        self.assertIsNone(result)
        shared.assert_awaited_once()

    async def test_the_staff_notice_says_why_it_was_automatic(self) -> None:
        from app.services import notifications

        async with self.Session() as session:
            payment = await self._receipt(session)
            await session.commit()
            dispatch = AsyncMock(return_value=1)
            with patch.object(notifications, "_dispatch_dual_notify", dispatch):
                await notifications.notify_auto_approve(AsyncMock(), session, payment, reason="بدون بررسی ادمین تا ۵ دقیقه")
        self.assertIn("بدون بررسی ادمین تا ۵ دقیقه", dispatch.await_args.args[3])


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_payments(self) -> None:
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("receipt_at_migration", ROOT / "alembic/versions/0075_payment_receipt_at.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE payments (id INTEGER PRIMARY KEY, status TEXT, amount INTEGER)"))
            conn.execute(text("INSERT INTO payments VALUES (1, 'pending', 90000)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIsNone(conn.execute(text("SELECT receipt_at FROM payments")).scalar())
                self.assertIn(migration.INDEX, {i["name"] for i in sa.inspect(conn).get_indexes("payments")})
                migration.downgrade()
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT amount FROM payments")).scalar(), 90000)
                self.assertNotIn(migration.INDEX, {i["name"] for i in sa.inspect(conn).get_indexes("payments")})
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
