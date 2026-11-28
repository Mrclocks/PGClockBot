"""Several receipt photos for one purchase: one review, later photos join it, staff see them under the card."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Base, BotUser, Order, Payment, PaymentReceipt, PaymentReviewMessage, PaymentStatus, Plan,
)
from app.services import payment_receipts as pr
from app.services.receipts import submit_receipt
from tests.postgres_support import scoped_async_engine, scoped_sync_engine
from tests.test_update_owner_only import SHOP_SESSION, RoutesHarness

ROOT = Path(__file__).resolve().parents[1]
BOT_ID = 4242


class FakeBot:
    def __init__(self) -> None:
        self.id = BOT_ID
        self.sent: list[dict] = []

    async def send_photo(self, **kwargs) -> None:
        self.sent.append(kwargs)


class ReceiptCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([BotUser(id=1, telegram_id=1001, referral_code="c1"), Plan(id=1, name="p", price=100_000)])
            await session.flush()
            session.add(Order(id=1, user_id=1, plan_id=1, amount=100_000, status="awaiting_receipt"))
            await session.flush()
            session.add(Payment(id=1, user_id=1, order_id=1, amount=100_000, method="card",
                                status=PaymentStatus.PENDING.value))
            await session.commit()
        self.bot = FakeBot()
        self.reviews = 0
        self.dup_policy = "warn"
        self.duplicates: list = []

        async def review(session, payment, **_kwargs):
            # Stands in for the staff review card: one card per review, stored like the real fan-out.
            self.reviews += 1
            session.add(PaymentReviewMessage(bot_id=BOT_ID, chat_id=77, message_id=500 + self.reviews,
                                             payment_id=int(payment.id), text="card"))
            await session.commit()
            return "first answer"

        patches = [
            patch("app.services.receipts.download_receipt_sha256", AsyncMock(return_value=None)),
            patch("app.services.receipts.process_receipt", review),
            patch("app.services.receipts.find_duplicate_receipts", AsyncMock(side_effect=lambda *a, **k: self.duplicates)),
            patch("app.services.receipts.get_receipt_dup_policy", AsyncMock(side_effect=lambda *a, **k: self.dup_policy)),
            patch("app.services.receipts._assess_risk", AsyncMock(return_value=None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def send(self, file_id: str, unique: str, **kwargs) -> str | None:
        async with self.Session() as session:
            payment = await session.get(Payment, 1)
            return await submit_receipt(session, payment, bot=self.bot, file_id=file_id, file_unique_id=unique, **kwargs)

    async def extras(self) -> list[PaymentReceipt]:
        async with self.Session() as session:
            return list((await session.scalars(select(PaymentReceipt).order_by(PaymentReceipt.id))).all())

    async def payment(self) -> Payment:
        async with self.Session() as session:
            return await session.get(Payment, 1)


class SecondPhotoTests(ReceiptCase):
    async def test_a_second_photo_joins_the_review_instead_of_opening_another(self) -> None:
        self.assertEqual(await self.send("F1", "u1"), "first answer")
        self.assertEqual(await self.send("F2", "u2"), pr.EXTRA_ADDED)
        self.assertEqual(self.reviews, 1, "staff get one review card")
        self.assertEqual((await self.payment()).receipt_file_id, "F1", "the first receipt is not replaced")
        self.assertEqual([r.file_id for r in await self.extras()], ["F2"])

    async def test_the_photo_is_sent_under_the_review_card_once(self) -> None:
        await self.send("F1", "u1")
        await self.send("F2", "u2")
        self.assertEqual(len(self.bot.sent), 1)
        sent = self.bot.sent[0]
        self.assertEqual((sent["chat_id"], sent["photo"], sent["reply_to_message_id"]), (77, "F2", 501))
        self.assertIn("#1", sent["caption"])
        self.assertNotIn("reply_markup", sent, "no second set of review buttons")
        async with self.Session() as session:
            self.assertEqual(await pr.forward_extra_receipts(session, 1, bot=self.bot), 0)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_the_same_photo_twice_is_kept_once(self) -> None:
        await self.send("F1", "u1")
        self.assertEqual(await self.send("F1-again", "u1"), pr.EXTRA_ALREADY_SENT)
        await self.send("F2", "u2")
        self.assertEqual(await self.send("F2-again", "u2"), pr.EXTRA_ALREADY_SENT)
        self.assertEqual(len(await self.extras()), 1)

    async def test_more_photos_than_the_limit_are_refused(self) -> None:
        await self.send("F0", "u0")
        for i in range(1, pr.MAX_EXTRA_RECEIPTS + 1):
            self.assertEqual(await self.send(f"F{i}", f"u{i}"), pr.EXTRA_ADDED)
        self.assertEqual(await self.send("Fx", "ux"), pr.EXTRA_LIMIT)
        self.assertEqual(len(await self.extras()), pr.MAX_EXTRA_RECEIPTS)

    async def test_a_duplicate_of_another_payment_is_refused_or_flagged_like_the_first(self) -> None:
        await self.send("F1", "u1")
        self.duplicates = [SimpleNamespace(payment_id=9)]
        self.dup_policy = "block"
        self.assertIn("رسید تکراری", await self.send("F2", "u2"))
        self.assertEqual(await self.extras(), [])
        self.dup_policy = "warn"
        self.assertEqual(await self.send("F3", "u3"), pr.EXTRA_ADDED)
        self.assertIn("#9", self.bot.sent[-1]["caption"])

    async def test_the_mini_app_and_web_upload_still_take_one_receipt(self) -> None:
        await self.send("F1", "u1")
        with self.assertRaises(ValueError):
            await self.send("F2", "u2", first_only=True)
        self.assertEqual(await self.extras(), [])

    async def test_a_photo_a_day_later_replaces_the_receipt_and_is_reviewed_again(self) -> None:
        await self.send("F1", "u1")
        async with self.Session() as session:
            (await session.get(Payment, 1)).receipt_at = datetime.now(timezone.utc) - timedelta(days=2)
            await session.commit()
        self.assertEqual(await self.send("F2", "u2"), "first answer")
        self.assertEqual(((await self.payment()).receipt_file_id, self.reviews), ("F2", 2))

    async def test_a_decided_payment_takes_no_more_photos(self) -> None:
        await self.send("F1", "u1")
        async with self.Session() as session:
            (await session.get(Payment, 1)).status = PaymentStatus.APPROVED.value
            await session.commit()
        with self.assertRaises(ValueError):
            await self.send("F2", "u2")
        self.assertEqual(await self.extras(), [])


class ForwardRaceTests(ReceiptCase):
    async def test_a_photo_stored_before_the_card_exists_is_forwarded_once_the_card_is_sent(self) -> None:
        async with self.Session() as session:
            payment = await session.get(Payment, 1)
            payment.receipt_file_id, payment.receipt_at = "F1", datetime.now(timezone.utc)
            await session.commit()
            self.assertEqual(await pr.add_extra_receipt(session, payment, file_id="F2", file_unique_id="u2"),
                             pr.EXTRA_ADDED)
            self.assertEqual(await pr.forward_extra_receipts(session, 1, bot=self.bot), 0, "no card yet")
            self.assertIsNone((await self.extras())[0].forwarded_at, "not claimed without a card")
            session.add(PaymentReviewMessage(bot_id=BOT_ID, chat_id=77, message_id=600, payment_id=1, text="c"))
            await session.commit()
        async with self.Session() as one, self.Session() as two:
            sent = await asyncio.gather(pr.forward_extra_receipts(one, 1, bot=self.bot),
                                        pr.forward_extra_receipts(two, 1, bot=self.bot))
        self.assertEqual(sorted(sent), [0, 1])
        self.assertEqual(len(self.bot.sent), 1)

    async def test_another_bot_s_cards_do_not_get_the_photo(self) -> None:
        await self.send("F1", "u1")
        other = FakeBot()
        other.id = 999
        async with self.Session() as session:
            payment = await session.get(Payment, 1)
            await pr.add_extra_receipt(session, payment, file_id="F2", file_unique_id="u2")
            self.assertEqual(await pr.forward_extra_receipts(session, 1, bot=other), 0)
        self.assertEqual(other.sent, [])

    async def test_photo_order_and_counts_for_the_web_panel(self) -> None:
        await self.send("F1", "u1")
        await self.send("F2", "u2")
        await self.send("F3", "u3")
        async with self.Session() as session:
            payment = await session.get(Payment, 1)
            files = [await pr.receipt_file_id(session, payment, i) for i in range(4)]
            self.assertEqual(files, ["F1", "F2", "F3", None])
            self.assertIsNone(await pr.receipt_file_id(session, payment, -1))
            self.assertEqual(await pr.extra_receipt_counts(session, [1, 2]), {1: 2})
            total = await session.scalar(select(func.count(PaymentReceipt.id)))
            self.assertEqual(total, 2)


def _load_migration():
    path = ROOT / "alembic/versions/0144_payment_receipts.py"
    spec = importlib.util.spec_from_file_location("payment_receipts_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PaymentReceiptsMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = _load_migration()
        self.engine = scoped_sync_engine()
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE payments (id INTEGER PRIMARY KEY, receipt_file_id VARCHAR(255))"))
            conn.execute(sa.text("INSERT INTO payments (id, receipt_file_id) VALUES (1, 'F1')"))

    def _run(self, conn, *, down: bool = False) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def test_upgrade_twice_then_downgrade_keeps_the_first_receipt(self) -> None:
        with self.engine.begin() as conn:
            self._run(conn)
            self._run(conn)
            conn.execute(sa.text("INSERT INTO payment_receipts (payment_id, file_id, file_unique_id) VALUES (1, 'F2', 'u')"))
            with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                conn.execute(sa.text(
                    "INSERT INTO payment_receipts (payment_id, file_id, file_unique_id) VALUES (1, 'F3', 'u')"))
        with self.engine.begin() as conn:
            self._run(conn, down=True)
            self._run(conn, down=True)
            self.assertFalse(sa.inspect(conn).has_table("payment_receipts"))
            self.assertEqual(conn.scalar(sa.text("SELECT receipt_file_id FROM payments WHERE id = 1")), "F1")

    def test_revision_follows_the_current_head(self) -> None:
        self.assertEqual(self.migration.down_revision, "0143_rich_descriptions")


if __name__ == "__main__":
    unittest.main()


class ReceiptRouteTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            session.add_all([
                BotUser(id=6, telegram_id=106, referral_code="shop6", role="reseller"),
                BotUser(id=7, telegram_id=107, referral_code="buyer"),
                Plan(id=1, name="p", price=100),
            ])
            await session.flush()
            session.add_all([
                Order(id=1, user_id=7, plan_id=1, amount=100, status="awaiting_approval", reseller_id=5),
                Order(id=2, user_id=7, plan_id=1, amount=100, status="awaiting_approval", reseller_id=6),
            ])
            await session.flush()
            session.add_all([
                Payment(id=1, user_id=7, order_id=1, amount=100, method="card", status="pending", receipt_file_id="F1"),
                Payment(id=2, user_id=7, order_id=2, amount=100, method="card", status="pending", receipt_file_id="G1"),
            ])
            await session.flush()
            session.add_all([PaymentReceipt(payment_id=1, file_id="F2"), PaymentReceipt(payment_id=2, file_id="G2")])
            await session.commit()

        async def fetch(_token, file_id):
            return file_id.encode(), "image/jpeg"

        for target, value in (
            ("app.api.finance_pages.payment_bot_token", AsyncMock(return_value="token")),
            ("app.api.finance_pages.fetch_telegram_file", fetch),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_each_photo_of_the_shop_s_payment_is_served_by_index(self) -> None:
        first = await self.call(SHOP_SESSION, "GET", "/payments/1/receipt")
        second = await self.call(SHOP_SESSION, "GET", "/payments/1/receipt?n=1")
        self.assertEqual((first.status_code, first.content), (200, b"F1"))
        self.assertEqual((second.status_code, second.content), (200, b"F2"))
        self.assertIn("receipt-1-2", second.headers["content-disposition"])

    async def test_a_missing_or_out_of_range_photo_is_refused(self) -> None:
        self.assertEqual((await self.call(SHOP_SESSION, "GET", "/payments/1/receipt?n=2")).status_code, 404)
        for bad in ("-1", "99", "x"):
            self.assertEqual((await self.call(SHOP_SESSION, "GET", f"/payments/1/receipt?n={bad}")).status_code, 422)

    async def test_another_shop_s_extra_photo_is_not_served(self) -> None:
        response = await self.call(SHOP_SESSION, "GET", "/payments/2/receipt?n=1")
        self.assertNotEqual(response.status_code, 200)
        self.assertNotIn(b"G2", response.content)
