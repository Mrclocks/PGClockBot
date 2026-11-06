"""Failed and stuck deliveries in the bot: scope, permission, confirmation and one-time closing."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import stuck_orders as stuck
from app.db.models import Base, BotUser, DeliveryFailure, Order, Payment, ShopWallet
from app.services import bot_feedback_texts as feedback
from tests.test_update_owner_only import OWNER_SESSION

SHOP_STAFF = {"role": "reseller", "bot_user_id": 5}
OLD = datetime.now(timezone.utc) - timedelta(hours=2)
CTX = {"is_reseller_bot": False, "reseller_owner_id": None}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class StuckOrdersBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=5, telegram_id=105, referral_code="s5", role="reseller"),
            BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
            BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5, username="<shopper>"),
        ])
        await self.session.flush()
        self.session.add_all([
            Order(id=10, user_id=1, amount=50000, status="paid", updated_at=OLD),
            Order(id=11, user_id=6, reseller_id=5, amount=30000, status="paid", updated_at=OLD),
            Order(id=12, user_id=6, reseller_id=5, amount=1000, status="delivered", updated_at=OLD),
        ])
        await self.session.flush()
        self.session.add_all([
            Payment(order_id=10, user_id=1, amount=50000, method="card", status="approved"),
            Payment(order_id=11, user_id=6, amount=30000, method="card", status="approved"),
            DeliveryFailure(order_id=11, reseller_id=5, error="panel timeout token=abc123 Bearer secretvalue", attempts=2),
            DeliveryFailure(order_id=12, reseller_id=5, error="telegram blocked"),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def press(self, data: str, *, staff: dict | None, handler=None):
        handler = handler or {
            "l": stuck.show_list, "v": stuck.view_order, "r": stuck.retry_order,
            "d": stuck.dismiss_order, "c": stuck.confirm_closing, "x": stuck.run_closing,
        }[data.split(":")[1]]
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        user = SimpleNamespace(id=9, telegram_id=777)
        with patch.object(stuck, "stuck_staff", AsyncMock(return_value=staff)), \
                patch.object(stuck, "safe_edit_text", AsyncMock()) as edit, \
                patch.object(stuck, "tell_customer_refunded", AsyncMock()) as told:
            await handler(callback, session=self.session, db_user=user, **CTX)
        return callback, edit, told

    def shown(self, edit) -> tuple[str, list[str]]:
        return edit.await_args.args[1], callbacks(edit.await_args.kwargs["reply_markup"])

    async def test_shop_sees_only_its_own_queue(self) -> None:
        _, edit, _ = await self.press("stk:l:0", staff=SHOP_STAFF)
        _, found = self.shown(edit)
        self.assertIn("stk:v:11", found)
        self.assertIn("stk:v:12", found)
        self.assertNotIn("stk:v:10", found)

    async def test_platform_owner_sees_only_platform_orders(self) -> None:
        _, edit, _ = await self.press("stk:l:0", staff=OWNER_SESSION)
        _, found = self.shown(edit)
        self.assertIn("stk:v:10", found)
        self.assertNotIn("stk:v:11", found)

    async def test_no_permission_shows_nothing_and_changes_nothing(self) -> None:
        for data in ("stk:l:0", "stk:v:11", "stk:r:11", "stk:d:11", "stk:c:rf:11", "stk:x:rf:11"):
            callback, edit, _ = await self.press(data, staff=None)
            callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
            edit.assert_not_awaited()
        order = await self.session.get(Order, 11)
        await self.session.refresh(order)
        self.assertEqual(order.status, "paid")

    async def test_another_shops_order_is_not_found_and_not_closed(self) -> None:
        other_shop = {"role": "reseller", "bot_user_id": 99}
        for data in ("stk:v:11", "stk:c:rf:11"):
            callback, edit, _ = await self.press(data, staff=other_shop)
            callback.answer.assert_awaited_once_with(feedback.NOT_FOUND, show_alert=True)
            edit.assert_not_awaited()
        callback, _, told = await self.press("stk:x:rf:11", staff=other_shop)
        self.assertIn("دسترسی", callback.answer.await_args.args[0])
        told.assert_not_awaited()
        order = await self.session.get(Order, 11)
        await self.session.refresh(order)
        self.assertEqual(order.status, "paid")

    async def test_platform_owner_cannot_touch_a_shop_order(self) -> None:
        callback, edit, _ = await self.press("stk:v:11", staff=OWNER_SESSION)
        callback.answer.assert_awaited_once_with(feedback.NOT_FOUND, show_alert=True)
        edit.assert_not_awaited()

    async def test_card_redacts_secrets_and_offers_closing_actions_for_a_stuck_order(self) -> None:
        _, edit, _ = await self.press("stk:v:11", staff=SHOP_STAFF)
        text, found = self.shown(edit)
        self.assertNotIn("secretvalue", text)
        self.assertIn("stk:r:11", found)
        self.assertIn("stk:c:rf:11", found)
        self.assertIn("stk:c:md:11", found)
        self.assertIn("stk:c:cl:11", found)
        self.assertIn("stk:d:11", found)
        self.assertIn("res:user:6", found)

    async def test_delivered_order_card_offers_only_retry_and_dismiss(self) -> None:
        _, edit, _ = await self.press("stk:v:12", staff=SHOP_STAFF)
        found = self.shown(edit)[1]
        self.assertIn("stk:r:12", found)
        self.assertIn("stk:d:12", found)
        self.assertFalse([c for c in found if c.startswith("stk:c:")])

    async def test_closing_needs_a_confirm_step_and_runs_once(self) -> None:
        _, edit, _ = await self.press("stk:c:rf:11", staff=SHOP_STAFF)
        self.assertEqual(self.shown(edit)[1][0], "stk:x:rf:11")
        order = await self.session.get(Order, 11)
        await self.session.refresh(order)
        self.assertEqual(order.status, "paid")
        _, _, told = await self.press("stk:x:rf:11", staff=SHOP_STAFF)
        told.assert_awaited_once()
        await self.session.refresh(order)
        self.assertEqual(order.status, "cancelled")
        wallet = await self.session.scalar(select(ShopWallet.balance).where(ShopWallet.user_id == 6, ShopWallet.reseller_id == 5))
        self.assertEqual(wallet, 30000)
        callback, _, told = await self.press("stk:x:rf:11", staff=SHOP_STAFF)
        told.assert_not_awaited()
        wallet = await self.session.scalar(select(ShopWallet.balance).where(ShopWallet.user_id == 6, ShopWallet.reseller_id == 5))
        self.assertEqual(wallet, 30000)

    async def test_dismiss_removes_the_failure_but_keeps_a_stuck_order_listed(self) -> None:
        _, edit, _ = await self.press("stk:d:11", staff=SHOP_STAFF)
        self.assertIn("stk:c:rf:11", self.shown(edit)[1])
        open_rows = await self.session.scalar(
            select(DeliveryFailure.id).where(DeliveryFailure.order_id == 11, DeliveryFailure.resolved_at.is_(None))
        )
        self.assertIsNone(open_rows)

    async def test_retry_reports_a_refusal_without_closing_the_order(self) -> None:
        with patch.object(stuck, "retry_delivery", AsyncMock(side_effect=ValueError("panel said no"))):
            callback, _, _ = await self.press("stk:r:11", staff=SHOP_STAFF)
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        order = await self.session.get(Order, 11)
        await self.session.refresh(order)
        self.assertEqual(order.status, "paid")


if __name__ == "__main__":
    unittest.main()
