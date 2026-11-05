"""Staff notices that need action expose approve/user/panel buttons under the message."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import keyboards as kb
from app.db.models import Base, BotUser, Order, Payment, PaymentStatus
from app.services import notifications
from app.services.notifications import (
    notify_new_order,
    notify_pending_approval,
    notify_settlement_review,
    notify_stars_delivery_failed,
    notify_wireguard_missing,
)


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
            ]
        )
        await self.session.commit()
        self.dispatch = AsyncMock(return_value=1)
        self.settings = SimpleNamespace(
            currency="تومان",
            admin_ids=[],
            public_base_url="https://panel.example",
        )
        # create_bot is imported inside notify_settlement_review from app.bot
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

    async def test_settlement_review_has_payrev_and_user(self) -> None:
        payment = await self.session.get(Payment, 20)
        await notify_settlement_review(
            self.session, payment, shop_id=5, reason="نیاز به بررسی دستی"
        )
        self.assertEqual(
            _callbacks(self.dispatch.await_args.kwargs["markup"]),
            ["payrev:ok:20", "payrev:no:20", "res:user:1"],
        )
        self.assertIs(self.dispatch.await_args.kwargs["payment"], payment)

    async def test_stars_failure_links_delivery_tab_and_user(self) -> None:
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

    async def test_wireguard_missing_links_delivery_tab(self) -> None:
        await notify_wireguard_missing(None, self.session, "no conf")
        markup = self.dispatch.await_args.kwargs["markup"]
        self.assertEqual(_callbacks(markup), [])
        self.assertEqual(_urls(markup), ["https://panel.example/finance?tab=delivery"])

    async def test_panel_url_omitted_without_https(self) -> None:
        self.settings.public_base_url = "http://panel.example"
        await notify_wireguard_missing(None, self.session, "no conf")
        self.assertIsNone(self.dispatch.await_args.kwargs["markup"])

    def test_reseller_app_review_can_add_user_button(self) -> None:
        self.assertEqual(
            _callbacks(kb.reseller_app_review(9)),
            ["adm:resapp:ok:9", "adm:resapp:no:9"],
        )
        self.assertEqual(
            _callbacks(kb.reseller_app_review(9, user_id=3)),
            ["adm:resapp:ok:9", "adm:resapp:no:9", "adm:users:view:3"],
        )


if __name__ == "__main__":
    unittest.main()
