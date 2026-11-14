"""Staff notices that need action expose safe approve/user/panel buttons under the message."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import keyboards as kb
from app.db.models import Base, BotUser, Order, Payment, PaymentReviewMessage, PaymentStatus
from app.services import notifications
from app.services.notifications import (
    notify_new_order,
    notify_pending_approval,
    notify_settlement_review,
    notify_stars_delivery_failed,
    notify_wireguard_missing,
)
from app.services.payment_review_actions import execute_payment_legacy_ok
from app.services.payment_review_diag import diagnose_payment_review


def _callbacks(markup) -> list[str]:
    if markup is None:
        return []
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def _urls(markup) -> list[str]:
    if markup is None:
        return []
    return [button.url for row in markup.inline_keyboard for button in row if button.url]


class StaffNotifyActionButtonsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all(
            [
                BotUser(id=1, telegram_id=1010, referral_code="c1", role="user", full_name="Ali"),
                BotUser(id=5, telegram_id=5005, referral_code="s5", role="reseller"),
                Order(id=10, user_id=1, amount=50_000, reseller_id=5),
                Order(id=11, user_id=1, amount=40_000),
                Order(id=12, user_id=1, amount=30_000, reseller_id=5),
                Payment(
                    id=20,
                    user_id=1,
                    order_id=10,
                    amount=50_000,
                    method="card",
                    status=PaymentStatus.PENDING.value,
                ),
                Payment(
                    id=21,
                    user_id=1,
                    amount=10_000,
                    method="card",
                    status=PaymentStatus.PENDING.value,
                    is_wallet_topup=True,
                    wallet_shop_id=5,
                ),
                Payment(
                    id=22,
                    user_id=1,
                    order_id=11,
                    amount=40_000,
                    method="stars",
                    status=PaymentStatus.APPROVED.value,
                ),
                Payment(
                    id=23,
                    user_id=1,
                    order_id=12,
                    amount=30_000,
                    method="stars",
                    status=PaymentStatus.APPROVED.value,
                ),
            ]
        )
        await self.session.commit()
        self.dispatch = AsyncMock(return_value=1)
        self.settings = SimpleNamespace(
            currency="تومان",
            admin_ids=[],
            public_base_url="https://panel.example",
        )
        create_bot = MagicMock()
        bot = MagicMock()
        bot.session = MagicMock(close=AsyncMock())
        create_bot.return_value = bot
        self.patches = [
            patch.object(notifications, "_dispatch_dual_notify", self.dispatch),
            patch.object(notifications, "get_settings", return_value=self.settings),
            patch("app.bot.create_bot", create_bot),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.session.close()
        await self.engine.dispose()

    async def test_pending_approval_keeps_review_and_opens_shop_user(self) -> None:
        payment = await self.session.get(Payment, 20)
        await notify_pending_approval(None, self.session, payment, None)
        self.assertEqual(
            _callbacks(self.dispatch.await_args.kwargs["markup"]),
            ["payrev:ok:20", "payrev:no:20", "res:user:1"],
        )

    async def test_shop_wallet_pending_opens_shop_user(self) -> None:
        payment = await self.session.get(Payment, 21)
        await notify_pending_approval(None, self.session, payment, None)
        self.assertEqual(
            _callbacks(self.dispatch.await_args.kwargs["markup"]),
            ["payrev:ok:21", "payrev:no:21", "res:user:1"],
        )

    async def test_new_order_opens_customer_card(self) -> None:
        order = await self.session.get(Order, 11)
        await notify_new_order(None, self.session, order=order, user_tg_id=None)
        self.assertEqual(
            _callbacks(self.dispatch.await_args.kwargs["markup"]),
            ["adm:users:view:1"],
        )

    async def test_settlement_review_has_user_but_no_payrev(self) -> None:
        """payrev:ok would approve a still-pending mismatched settlement; do not offer it."""
        payment = await self.session.get(Payment, 20)
        diag = await diagnose_payment_review(self.session, payment)
        self.assertTrue(diag.can_claim_approve)
        await notify_settlement_review(
            self.session, payment, shop_id=5, reason="مبلغ نامطابق — نیاز به بررسی دستی"
        )
        callbacks = _callbacks(self.dispatch.await_args.kwargs["markup"])
        self.assertEqual(callbacks, ["res:user:1"])
        self.assertFalse(any(c.startswith("payrev:") for c in callbacks))
        self.assertNotIn("payment", self.dispatch.await_args.kwargs)

    async def test_payrev_ok_would_still_approve_pending_settlement_payment(self) -> None:
        """Documents why settlement notices omit payrev: legacy ok claims PENDING approve."""
        payment = await self.session.get(Payment, 20)
        bot = AsyncMock()
        with patch(
            "app.services.payment_review_actions.approve_payment",
            AsyncMock(return_value=None),
        ) as approve, patch(
            "app.services.payment_review_actions._deliver_and_notify",
            AsyncMock(return_value=None),
        ):
            result = await execute_payment_legacy_ok(
                self.session, payment, reviewer_tg=5005, bot=bot
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "approve")
        approve.assert_awaited_once()

    async def test_platform_stars_failure_links_delivery_tab(self) -> None:
        payment = await self.session.get(Payment, 22)
        await notify_stars_delivery_failed(
            None,
            self.session,
            payment,
            telegram_id=1010,
            charge_id="ch1",
            refunded=False,
            error="boom",
        )
        markup = self.dispatch.await_args.kwargs["markup"]
        self.assertEqual(_callbacks(markup), ["adm:users:view:1"])
        self.assertEqual(_urls(markup), ["https://panel.example/finance?tab=delivery"])

    async def test_shop_stars_failure_has_user_but_no_panel_url(self) -> None:
        payment = await self.session.get(Payment, 23)
        await notify_stars_delivery_failed(
            None,
            self.session,
            payment,
            telegram_id=1010,
            charge_id="ch2",
            refunded=False,
            error="boom",
        )
        markup = self.dispatch.await_args.kwargs["markup"]
        self.assertEqual(_callbacks(markup), ["res:user:1"])
        self.assertEqual(_urls(markup), [])

    async def test_wireguard_missing_links_delivery_tab(self) -> None:
        await notify_wireguard_missing(None, self.session, "no conf")
        markup = self.dispatch.await_args.kwargs["markup"]
        self.assertEqual(_callbacks(markup), [])
        self.assertEqual(_urls(markup), ["https://panel.example/finance?tab=delivery"])

    async def test_panel_url_omitted_without_https(self) -> None:
        self.settings.public_base_url = "http://panel.example"
        await notify_wireguard_missing(None, self.session, "no conf")
        self.assertIsNone(self.dispatch.await_args.kwargs["markup"])
        payment = await self.session.get(Payment, 22)
        await notify_stars_delivery_failed(
            None, self.session, payment, telegram_id=1010, charge_id="ch", refunded=True, error="x"
        )
        self.assertEqual(
            _urls(self.dispatch.await_args.kwargs["markup"]),
            [],
        )

    def test_reseller_app_review_can_add_user_button(self) -> None:
        self.assertEqual(
            _callbacks(kb.reseller_app_review(9)),
            ["adm:resapp:ok:9", "adm:resapp:no:9"],
        )
        self.assertEqual(
            _callbacks(kb.reseller_app_review(9, user_id=3)),
            ["adm:resapp:ok:9", "adm:resapp:no:9", "adm:users:view:3"],
        )


class PendingApprovalNoticeCommitTests(unittest.IsolatedAsyncioTestCase):
    """notify_pending_approval may commit review-message rows; payment status must stay put."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=1010, referral_code="c1", role="user"),
                    BotUser(id=9, telegram_id=9001, referral_code="a9", role="admin", full_name="Admin"),
                    Payment(
                        id=30,
                        user_id=1,
                        amount=12_000,
                        method="card",
                        status=PaymentStatus.PENDING.value,
                        is_wallet_topup=True,
                    ),
                ]
            )
            await session.commit()
        self.bot = MagicMock()
        self.bot.id = 500
        self.bot.token = "500:test"
        # Same Telegram message id on a repeated fan-out: conflict key must not insert twice.
        self.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(
                message_id=77,
                chat=SimpleNamespace(id=9001),
                photo=None,
                html_text="card",
                html_caption=None,
            )
        )
        self.bot.send_photo = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_pending_notice_commit_does_not_change_payment_or_duplicate_rows(self) -> None:
        settings = SimpleNamespace(admin_ids=[9001], currency="تومان", public_base_url="")
        async with self.Session() as session:
            payment = await session.get(Payment, 30)
            with patch.object(notifications, "get_settings", return_value=settings), patch.object(
                notifications, "notify_enabled", AsyncMock(return_value=True)
            ):
                await notify_pending_approval(self.bot, session, payment, 1010)
                status_after_first = payment.status
                await notify_pending_approval(self.bot, session, payment, 1010)
            await session.refresh(payment)
            self.assertEqual(status_after_first, PaymentStatus.PENDING.value)
            self.assertEqual(payment.status, PaymentStatus.PENDING.value)
            count = await session.scalar(select(func.count()).select_from(PaymentReviewMessage))
            self.assertEqual(int(count or 0), 1)
            self.assertEqual(self.bot.send_message.await_count, 2)


if __name__ == "__main__":
    unittest.main()
