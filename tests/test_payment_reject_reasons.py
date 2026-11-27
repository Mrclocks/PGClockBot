"""Ready-made reasons for rejecting a receipt: parsing, the bot picker and the payer's notice."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers import payments
from app.db.models import Base, BotUser, Payment, PaymentStatus
from app.services.reject_reasons import (
    MAX_REASON_LENGTH,
    MAX_REASONS,
    NO_REASON_NOTE,
    button_label,
    cancellation_reasons,
    parse_reasons,
    reason_at,
    with_reason,
)
from tests.postgres_support import scoped_async_engine

REVIEWER_TG = 555
BUYER_TG = 777
HOSTILE = "<b>رسید</b> & جعلی"


def callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class ReasonParsingTests(unittest.TestCase):
    def test_lines_are_trimmed_deduplicated_and_bounded(self) -> None:
        raw = "  یک  \n\nیک\nدو   سه\n" + "\n".join(f"r{i}" for i in range(20))
        reasons = parse_reasons(raw)
        self.assertEqual(reasons[:2], ["یک", "دو سه"])
        self.assertEqual(len(reasons), MAX_REASONS)
        self.assertEqual(len(parse_reasons("x" * 500)[0]), MAX_REASON_LENGTH)
        for bad in (None, 5, ["a"]):
            self.assertEqual(parse_reasons(bad), [])

    def test_a_number_outside_the_list_is_refused(self) -> None:
        for number in (0, 3, -1, True, "1"):
            with self.subTest(number=number), self.assertRaises(ValueError):
                reason_at(["a", "b"], number)
        self.assertEqual(reason_at(["a", "b"], 2), "b")

    def test_an_emptied_cancellation_list_keeps_the_defaults(self) -> None:
        self.assertTrue(cancellation_reasons({"service_cancel_reject_reasons": " \n "}))

    def test_the_reason_is_escaped_for_html_and_plain_for_entities(self) -> None:
        self.assertIn("&lt;b&gt;رسید&lt;/b&gt; &amp; جعلی", with_reason("body", {}, HOSTILE))
        self.assertIn(HOSTILE, with_reason("body", {"parse_mode": None, "entities": []}, HOSTILE))
        self.assertEqual(with_reason("body", {}, None), "body")
        self.assertLessEqual(len(button_label("ب" * 100)), 40)


class BotRejectReasonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add(BotUser(id=7, telegram_id=BUYER_TG, referral_code="b", role="user"))
        await self.session.flush()
        self.session.add(Payment(id=9, user_id=7, amount=10, method="card", status=PaymentStatus.PENDING.value))
        await self.session.commit()
        self.settings = {"payment_reject_reasons": f"مبلغ اشتباه است\n{HOSTILE}"}
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.patches = [
            patch.object(payments, "get_all_settings", AsyncMock(side_effect=lambda *_a, **_k: dict(self.settings))),
            patch.object(payments, "reseller_can_review_payment", AsyncMock(return_value=True)),
            patch("app.services.payment_review_messages.remember_review_message", AsyncMock()),
            patch("app.services.orders._sync_payment_review_messages", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.session.close()
        await self.engine.dispose()

    def callback(self, data: str) -> AsyncMock:
        callback = AsyncMock()
        callback.data = data
        callback.bot = self.bot
        callback.message = AsyncMock()
        return callback

    async def payment(self) -> Payment:
        row = await self.session.get(Payment, 9)
        await self.session.refresh(row)
        return row

    def reviewer(self) -> SimpleNamespace:
        return SimpleNamespace(telegram_id=REVIEWER_TG)

    async def test_reject_offers_the_shops_reasons_without_rejecting_yet(self) -> None:
        callback = self.callback("payrev:no:9")
        await payments.pay_reject(callback, session=self.session, db_user=self.reviewer())
        markup = callback.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(callbacks(markup), ["payrev:nr:9:1", "payrev:nr:9:2", "payrev:nr:9:0", "payrev:nx:9"])
        self.assertEqual((await self.payment()).status, PaymentStatus.PENDING.value)
        self.bot.send_message.assert_not_awaited()

    async def test_the_chosen_reason_is_saved_and_sent_escaped_once(self) -> None:
        first = self.callback("payrev:nr:9:2")
        await payments.pay_reject_with_reason(first, session=self.session, db_user=self.reviewer())
        payment = await self.payment()
        self.assertEqual((payment.status, payment.review_note), (PaymentStatus.REJECTED.value, HOSTILE))
        self.bot.send_message.assert_awaited_once()
        sent = self.bot.send_message.await_args
        self.assertEqual(sent.args[0], BUYER_TG)
        self.assertIn("&lt;b&gt;رسید&lt;/b&gt;", sent.args[1])
        self.assertNotIn(HOSTILE, sent.args[1])
        first.message.delete.assert_awaited_once()

        again = self.callback("payrev:nr:9:1")
        await payments.pay_reject_with_reason(again, session=self.session, db_user=self.reviewer())
        self.assertTrue(again.answer.await_args.kwargs["show_alert"])
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.payment()).review_note, HOSTILE, "a second tap does not overwrite the reason")

    async def test_reject_without_a_reason_keeps_the_old_note(self) -> None:
        await payments.pay_reject_with_reason(self.callback("payrev:nr:9:0"), session=self.session, db_user=self.reviewer())
        self.assertEqual((await self.payment()).review_note, NO_REASON_NOTE)
        self.assertNotIn("دلیل", self.bot.send_message.await_args.args[1])

    async def test_a_reason_removed_since_the_picker_was_shown_is_refused(self) -> None:
        self.settings = {"payment_reject_reasons": "تنها دلیل"}
        callback = self.callback("payrev:nr:9:2")
        await payments.pay_reject_with_reason(callback, session=self.session, db_user=self.reviewer())
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertEqual((await self.payment()).status, PaymentStatus.PENDING.value)

    async def test_a_reviewer_without_access_cannot_reject(self) -> None:
        callback = self.callback("payrev:nr:9:1")
        with patch.object(payments, "reseller_can_review_payment", AsyncMock(return_value=False)):
            await payments.pay_reject_with_reason(callback, session=self.session, db_user=self.reviewer())
        self.assertEqual((await self.payment()).status, PaymentStatus.PENDING.value)
        self.bot.send_message.assert_not_awaited()

    async def test_without_reasons_reject_happens_at_once(self) -> None:
        self.settings = {"payment_reject_reasons": ""}
        callback = self.callback("payrev:no:9")
        await payments.pay_reject(callback, session=self.session, db_user=self.reviewer())
        callback.message.answer.assert_not_awaited()
        self.assertEqual((await self.payment()).status, PaymentStatus.REJECTED.value)

    async def test_dismiss_removes_the_picker_only(self) -> None:
        callback = self.callback("payrev:nx:9")
        await payments.pay_reject_dismiss(callback)
        callback.message.delete.assert_awaited_once()
        self.assertEqual((await self.payment()).status, PaymentStatus.PENDING.value)


if __name__ == "__main__":
    unittest.main()
