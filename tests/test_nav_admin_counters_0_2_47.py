"""0.2.47 PR6 — admin/reseller pending counters with hard shop isolation."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    Order,
    Payment,
    Plan,
    ServiceCancellation,
    Ticket,
    UserService,
)
from app.services.admin_counters import (
    PendingCounts,
    format_queue_summary,
    pending_counts,
    with_badge,
)
from app.services.numbers import to_fa_digits


class ToFaDigitsTests(unittest.TestCase):
    def test_maps_ascii_to_persian(self):
        self.assertEqual(to_fa_digits(12), "۱۲")
        self.assertEqual(to_fa_digits("03"), "۰۳")

    def test_badge_and_summary(self):
        self.assertEqual(with_badge("رسیدها", 0), "رسیدها")
        self.assertEqual(with_badge("رسیدها", 3), "رسیدها (۳)")
        self.assertIsNone(format_queue_summary(PendingCounts()))
        line = format_queue_summary(
            PendingCounts(payments=2, tickets=1, cancellations=4)
        )
        self.assertIn("رسید ۲", line)
        self.assertIn("تیکت ۱", line)
        self.assertIn("لغو ۴", line)


class KeyboardBadgeTests(unittest.TestCase):
    def test_admin_ops_badges_payments_and_tickets(self):
        from app.bot.nav_inline import admin_ops_hub_keyboard

        texts = [
            b.text
            for row in admin_ops_hub_keyboard(
                {}, pending_payments=5, pending_tickets=2
            ).inline_keyboard
            for b in row
        ]
        self.assertTrue(any("(۵)" in t for t in texts))
        self.assertTrue(any("(۲)" in t for t in texts))

    def test_reseller_manage_badges(self):
        from app.bot.nav_inline import reseller_manage_hub_keyboard

        profile = MagicMock()
        with patch(
            "app.bot.reply_keyboards._reseller_submenu_entries",
            return_value=[
                ("res_payments", "🧾 رسیدهای در انتظار"),
                ("res_tickets", "🎫 تیکت‌های مشتریان"),
                ("res_dash", "خانه"),
            ],
        ):
            texts = [
                b.text
                for row in reseller_manage_hub_keyboard(
                    profile,
                    {},
                    pending_payments=7,
                    pending_tickets=1,
                ).inline_keyboard
                for b in row
            ]
        self.assertTrue(any("رسید" in t and "(۷)" in t for t in texts))
        self.assertTrue(any("تیکت" in t and "(۱)" in t for t in texts))
        self.assertTrue(any(t == "خانه" for t in texts))


class PendingCountsIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session: AsyncSession = async_sessionmaker(
            self.engine, expire_on_commit=False
        )()
        now = datetime.now(timezone.utc)
        self.session.add_all(
            [
                BotUser(
                    id=1,
                    telegram_id=101,
                    referral_code="plat_cust",
                    role="user",
                    created_at=now,
                ),
                BotUser(
                    id=10,
                    telegram_id=110,
                    referral_code="shop10",
                    role="reseller",
                    created_at=now,
                ),
                BotUser(
                    id=20,
                    telegram_id=120,
                    referral_code="shop20",
                    role="reseller",
                    created_at=now,
                ),
                BotUser(
                    id=11,
                    telegram_id=111,
                    referral_code="c11",
                    role="user",
                    reseller_id=10,
                    created_at=now,
                ),
                BotUser(
                    id=21,
                    telegram_id=121,
                    referral_code="c21",
                    role="user",
                    reseller_id=20,
                    created_at=now,
                ),
                BotUser(
                    id=12,
                    telegram_id=112,
                    referral_code="demo12",
                    role="user",
                    reseller_id=10,
                    is_demo=True,
                    created_at=now,
                ),
                Plan(id=1, name="P", price=100, duration_days=30, data_limit_gb=10),
            ]
        )
        await self.session.flush()
        # Platform: 1 order receipt + 1 platform topup
        self.session.add_all(
            [
                Order(
                    id=100,
                    user_id=1,
                    plan_id=1,
                    amount=100,
                    status="awaiting_approval",
                    reseller_id=None,
                ),
                Payment(
                    user_id=1,
                    order_id=100,
                    amount=100,
                    status="pending",
                    receipt_file_id="p-order",
                ),
                Payment(
                    user_id=1,
                    amount=50,
                    status="pending",
                    receipt_file_id="p-topup",
                    is_wallet_topup=True,
                    wallet_shop_id=None,
                ),
                Ticket(
                    user_id=1,
                    reseller_id=None,
                    subject="plat",
                    status="open",
                ),
                UserService(
                    id=1,
                    bot_user_id=1,
                    plan_id=1,
                    pg_username="plat1",
                    pg_user_id=1,
                ),
                ServiceCancellation(
                    id=1,
                    service_id=1,
                    user_id=1,
                    reseller_id=None,
                    reason="plat cancel",
                    pg_user_id=1,
                    status="pending",
                ),
            ]
        )
        # Shop 10: order receipt + shop topup + ticket + cancel
        self.session.add_all(
            [
                Order(
                    id=200,
                    user_id=11,
                    plan_id=1,
                    amount=200,
                    status="awaiting_approval",
                    reseller_id=10,
                ),
                Payment(
                    user_id=11,
                    order_id=200,
                    amount=200,
                    status="pending",
                    receipt_file_id="s10-order",
                ),
                Payment(
                    user_id=11,
                    amount=30,
                    status="pending",
                    receipt_file_id="s10-topup",
                    is_wallet_topup=True,
                    wallet_shop_id=10,
                ),
                # Demo customer traffic must not inflate shop/platform badges
                Payment(
                    user_id=12,
                    amount=30,
                    status="pending",
                    receipt_file_id="demo-topup",
                    is_wallet_topup=True,
                    wallet_shop_id=10,
                ),
                Ticket(
                    user_id=11,
                    reseller_id=10,
                    subject="shop10",
                    status="answered",
                ),
                UserService(
                    id=11,
                    bot_user_id=11,
                    plan_id=1,
                    pg_username="s11",
                    pg_user_id=11,
                ),
                ServiceCancellation(
                    id=11,
                    service_id=11,
                    user_id=11,
                    reseller_id=10,
                    reason="shop10 cancel",
                    pg_user_id=11,
                    status="review",
                ),
            ]
        )
        # Shop 20: separate queue — must not leak into shop 10 or platform
        self.session.add_all(
            [
                Order(
                    id=300,
                    user_id=21,
                    plan_id=1,
                    amount=300,
                    status="awaiting_approval",
                    reseller_id=20,
                ),
                Payment(
                    user_id=21,
                    order_id=300,
                    amount=300,
                    status="pending",
                    receipt_file_id="s20-order",
                ),
                Payment(
                    user_id=21,
                    amount=40,
                    status="pending",
                    receipt_file_id="s20-topup",
                    is_wallet_topup=True,
                    wallet_shop_id=20,
                ),
                Ticket(
                    user_id=21,
                    reseller_id=20,
                    subject="shop20",
                    status="open",
                ),
                UserService(
                    id=21,
                    bot_user_id=21,
                    plan_id=1,
                    pg_username="s21",
                    pg_user_id=21,
                ),
                ServiceCancellation(
                    id=21,
                    service_id=21,
                    user_id=21,
                    reseller_id=20,
                    reason="shop20 cancel",
                    pg_user_id=21,
                    status="pending",
                ),
            ]
        )
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def test_platform_excludes_all_shop_queues(self):
        counts = await pending_counts(self.session, shop_id=None)
        self.assertEqual(counts.payments, 2)
        self.assertEqual(counts.tickets, 1)
        self.assertEqual(counts.cancellations, 1)

    async def test_shop_a_excludes_platform_and_shop_b(self):
        counts = await pending_counts(self.session, shop_id=10)
        self.assertEqual(counts.payments, 2)  # order + topup; demo excluded
        self.assertEqual(counts.tickets, 1)
        self.assertEqual(counts.cancellations, 1)

    async def test_shop_b_isolated_from_shop_a(self):
        counts = await pending_counts(self.session, shop_id=20)
        self.assertEqual(counts.payments, 2)
        self.assertEqual(counts.tickets, 1)
        self.assertEqual(counts.cancellations, 1)

    async def test_shop_topup_not_counted_as_platform(self):
        """wallet_shop_id tenancy — shop topups never appear on platform."""
        # Extra shop-10 topup from a platform-affiliated user id must stay shop-scoped
        self.session.add(
            Payment(
                user_id=1,
                amount=99,
                status="pending",
                receipt_file_id="cross-topup",
                is_wallet_topup=True,
                wallet_shop_id=10,
            )
        )
        await self.session.commit()
        plat = await pending_counts(self.session, shop_id=None)
        shop = await pending_counts(self.session, shop_id=10)
        self.assertEqual(plat.payments, 2)
        self.assertEqual(shop.payments, 3)


if __name__ == "__main__":
    unittest.main()
