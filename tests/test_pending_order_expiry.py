"""Optional expiry of unpaid orders: off by default, per shop, only orders with nothing paid, with a notice."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import CustomerNotice, Order, OrderStatus, Payment
from app.services.cleanup_settings import save_cleanup_tab
from app.services.customer_action_notices import notice_markup_for, notice_text
from app.services.order_expiry_texts import EXPIRED_ACTION
from app.services.pending_order_expiry import (
    KEY_ENABLED,
    KEY_HOURS,
    ExpiryRules,
    expire_all_shops,
    parse_hours,
    rules_from_form,
    save_expiry_rules,
)
from app.services.users import clear_settings_cache, get_all_settings
from tests.postgres_support import scoped_async_engine

SHOP_A, SHOP_B = 81, 82
CUSTOMER = 500
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
OWNER = {"role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0, "org_parent_id": None,
         "org_status": "active"}


class ExpiryCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        from app.db.models import BotUser

        clear_settings_cache()
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"s{shop}", role="reseller"))
            session.add(BotUser(id=CUSTOMER, telegram_id=5000, referral_code="c500"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()

    async def order(self, *, shop: int | None = SHOP_A, hours_old: int = 50, status: str = "pending",
                    payment: dict | None = None) -> int:
        async with self.Session() as session:
            order = Order(user_id=CUSTOMER, amount=1000, status=status, reseller_id=shop,
                          created_at=NOW - timedelta(hours=hours_old))
            session.add(order)
            await session.flush()
            if payment is not None:
                session.add(Payment(user_id=CUSTOMER, amount=1000, order_id=order.id, **payment))
            await session.commit()
            return int(order.id)

    async def switch_on(self, shop: int | None, hours: int = 48) -> None:
        async with self.Session() as session:
            await save_expiry_rules(session, shop_id=shop, rules=ExpiryRules(enabled=True, hours=hours))
        clear_settings_cache()

    async def run_expiry(self) -> int:
        async with self.Session() as session:
            return await expire_all_shops(session, now=NOW)

    async def status(self, order_id: int) -> str:
        async with self.Session() as session:
            return await session.scalar(select(Order.status).where(Order.id == order_id))

    async def notices(self) -> list[CustomerNotice]:
        async with self.Session() as session:
            return list(await session.scalars(select(CustomerNotice)))


class ExpiryTests(ExpiryCase):
    async def test_nothing_is_cancelled_while_expiry_is_off(self) -> None:
        order_id = await self.order(hours_old=1000)
        async with self.Session() as session:
            settings = await get_all_settings(session, reseller_id=SHOP_A)
        self.assertEqual((settings[KEY_ENABLED], settings[KEY_HOURS]), ("0", "48"))
        self.assertEqual(await self.run_expiry(), 0)
        self.assertEqual(await self.status(order_id), OrderStatus.PENDING.value)

    async def test_an_old_unpaid_order_is_cancelled_with_a_notice_and_a_recent_one_waits(self) -> None:
        await self.switch_on(SHOP_A, hours=48)
        old = await self.order(hours_old=49)
        recent = await self.order(hours_old=47)
        self.assertEqual(await self.run_expiry(), 1)
        self.assertEqual(await self.status(old), OrderStatus.CANCELLED.value)
        self.assertEqual(await self.status(recent), OrderStatus.PENDING.value)
        [notice] = await self.notices()
        self.assertEqual((notice.action, notice.shop_id, notice.telegram_id, notice.service_name),
                         (EXPIRED_ACTION, SHOP_A, 5000, f"#{old}"))

    async def test_orders_with_money_moving_are_never_cancelled(self) -> None:
        await self.switch_on(SHOP_A)
        kept = [
            await self.order(status="awaiting_approval", payment={"receipt_file_id": "photo"}),
            await self.order(status="awaiting_receipt", payment={"method": "gateway"}),
            await self.order(status="paid", payment={"status": "approved"}),
            await self.order(status="delivered"),
        ]
        waiting_receipt = await self.order(status="awaiting_receipt", payment={"method": "card"})
        self.assertEqual(await self.run_expiry(), 1)
        for order_id in kept:
            self.assertNotEqual(await self.status(order_id), OrderStatus.CANCELLED.value, order_id)
        self.assertEqual(await self.status(waiting_receipt), OrderStatus.CANCELLED.value)

    async def test_each_shop_uses_its_own_switch_and_hours(self) -> None:
        await self.switch_on(SHOP_A, hours=10)
        shop_a = await self.order(shop=SHOP_A, hours_old=11)
        shop_b = await self.order(shop=SHOP_B, hours_old=1000)
        platform = await self.order(shop=None, hours_old=1000)
        await self.run_expiry()
        self.assertEqual(await self.status(shop_a), OrderStatus.CANCELLED.value)
        self.assertEqual(await self.status(shop_b), OrderStatus.PENDING.value)
        self.assertEqual(await self.status(platform), OrderStatus.PENDING.value)
        await self.switch_on(None, hours=100)
        await self.run_expiry()
        self.assertEqual(await self.status(platform), OrderStatus.CANCELLED.value)
        self.assertEqual(await self.status(shop_b), OrderStatus.PENDING.value)

    async def test_a_second_run_changes_nothing(self) -> None:
        await self.switch_on(SHOP_A)
        await self.order()
        self.assertEqual(await self.run_expiry(), 1)
        self.assertEqual(await self.run_expiry(), 0)
        self.assertEqual(len(await self.notices()), 1)


class RaceTests(ExpiryCase):
    async def test_a_receipt_arriving_after_selection_stops_the_cancel_and_its_notice(self) -> None:
        from app.services.pending_order_expiry import _expire_one

        order_id = await self.order(status="awaiting_receipt", payment={"method": "card"})
        async with self.Session() as session:
            order = await session.get(Order, order_id)
            # The receipt lands between the expiry query and the status claim.
            async with self.Session() as other:
                payment = await other.scalar(select(Payment).where(Payment.order_id == order_id))
                payment.receipt_file_id = "photo"
                await other.commit()
            self.assertFalse(await _expire_one(session, order, 5000, NOW))
        self.assertEqual(await self.status(order_id), OrderStatus.AWAITING_RECEIPT.value)
        self.assertEqual(await self.notices(), [])


class SettingsTests(ExpiryCase):
    def test_hours_are_validated(self) -> None:
        self.assertEqual(parse_hours("24"), 24)
        for bad in ("0", "721", "-1", "1.5", "", "۲۴", "abc", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_hours(bad)
        self.assertIsNone(rules_from_form({"days": "30"}))
        self.assertEqual(rules_from_form({"expiry": "1", "expiry_hours": "6"}), ExpiryRules(True, 6))

    async def test_the_cleanup_form_saves_expiry_and_refuses_bad_hours_before_writing(self) -> None:
        async with self.Session() as session:
            await save_cleanup_tab(session, staff=OWNER, form={"days": "30", "expiry": "1", "expiry_hours": "12"})
            clear_settings_cache()
            settings = await get_all_settings(session, reseller_id=0)
            self.assertEqual((settings[KEY_ENABLED], settings[KEY_HOURS]), ("1", "12"))
            with self.assertRaises(ValueError):
                await save_cleanup_tab(session, staff=OWNER, form={"days": "45", "expiry": "0", "expiry_hours": "0"})
            clear_settings_cache()
            settings = await get_all_settings(session, reseller_id=0)
            self.assertEqual((settings[KEY_ENABLED], settings[KEY_HOURS], settings["cleanup_purge_after_days"]), ("1", "12", "30"))

    async def test_staff_without_the_purge_permission_cannot_change_it(self) -> None:
        staff = {"role": "reseller", "permissions": ["shop_settings"], "bot_user_id": SHOP_A}
        async with self.Session() as session:
            with self.assertRaises(PermissionError):
                await save_cleanup_tab(session, staff=staff, form={"days": "30", "expiry": "1", "expiry_hours": "1"})

    def test_the_notice_names_the_order_and_offers_a_new_purchase(self) -> None:
        text = notice_text(EXPIRED_ACTION, service_name="#42<b>", reason="")
        self.assertIn("#42&lt;b&gt;", text)
        markup = notice_markup_for(CustomerNotice(action=EXPIRED_ACTION, service_name="#42"))
        self.assertTrue(markup.inline_keyboard)


if __name__ == "__main__":
    unittest.main()
