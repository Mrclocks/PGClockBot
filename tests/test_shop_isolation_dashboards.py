"""Dashboard and action-center counts follow the shop of each ticket, payment and service."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, BotUserShop, Order, OrderStatus, Payment, PaymentStatus, Ticket, UserService
from app.services.finance_reports import shop_ops_snapshot
from app.services.action_center import build_action_center
from tests.postgres_support import scoped_async_engine

SHOP_ID, CUSTOMER = 5, 7


class DashboardScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                # Attributed to the shop, customer of the platform too.
                BotUser(id=CUSTOMER, telegram_id=107, referral_code="cust", role="user", reseller_id=SHOP_ID),
            ])
            await session.flush()
            session.add_all([
                BotUserShop(bot_user_id=CUSTOMER, shop_id=None),
                BotUserShop(bot_user_id=CUSTOMER, shop_id=SHOP_ID),
                Order(id=1, user_id=CUSTOMER, reseller_id=None, amount=10, status=OrderStatus.PENDING.value),
                Order(id=2, user_id=CUSTOMER, reseller_id=SHOP_ID, amount=10, status=OrderStatus.PENDING.value),
            ])
            await session.flush()
            session.add_all([
                Payment(user_id=CUSTOMER, order_id=1, amount=10, receipt_file_id="a", status=PaymentStatus.PENDING.value),
                Payment(user_id=CUSTOMER, order_id=2, amount=10, receipt_file_id="b", status=PaymentStatus.PENDING.value),
                Ticket(user_id=CUSTOMER, subject="platform", status="open", reseller_id=None),
                Ticket(user_id=CUSTOMER, subject="shop", status="open", reseller_id=SHOP_ID),
                UserService(bot_user_id=CUSTOMER, pg_user_id=1, pg_username="p", remark="order:1", shop_id=None,
                            quota_synced_at=now, quota_expire_at=now + timedelta(days=1)),
                UserService(bot_user_id=CUSTOMER, pg_user_id=2, pg_username="s", remark="order:2", shop_id=SHOP_ID,
                            quota_synced_at=now, quota_expire_at=now + timedelta(days=1)),
                UserService(bot_user_id=CUSTOMER, pg_user_id=3, pg_username="r", remark="imported", shop_needs_review=True,
                            quota_synced_at=now, quota_expire_at=now + timedelta(days=1)),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def center(self, shop: int | None) -> dict[str, str]:
        async with self.Session() as session:
            data = await build_action_center(session, reseller_id=shop)
        return {item["key"]: item["title"] for item in data["entries"]}

    async def test_each_side_sees_only_its_own_receipts_tickets_and_expiring_services(self):
        for shop in (None, SHOP_ID):
            entries = await self.center(shop)
            self.assertTrue(entries["pending"].startswith("1 "), (shop, entries))
            self.assertTrue(entries["tickets"].startswith("1 "), (shop, entries))
            self.assertTrue(entries["expiring"].startswith("1 "), (shop, entries))

    async def test_ops_snapshot_counts_services_users_and_payments_per_shop(self):
        async with self.Session() as session:
            platform = await shop_ops_snapshot(session, reseller_id=None)
            shop = await shop_ops_snapshot(session, reseller_id=SHOP_ID)
        # The platform also counts the shop owner's own account.
        self.assertEqual((platform["total_users"], shop["total_users"]), (2, 1))
        for snapshot in (platform, shop):
            self.assertEqual(snapshot["pending_receipts"], 1)
            self.assertEqual(snapshot["open_tickets"], 1)
