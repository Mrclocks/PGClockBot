"""One-time automatic-renewal offer after the second manual renewal of a service."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, CustomerNotice, Order, Plan, ServiceAutomation, UserService
from app.services.auto_renew_offer import ACTION, queue_auto_renew_offer
from app.services.customer_action_notices import notice_markup_for, notice_text
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = "app.services.auto_renew_offer.get_all_settings"


class AutoRenewOfferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.settings = patch(SETTINGS, new=AsyncMock(return_value={}))
        self.settings.start()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=1001, referral_code="c1"),
                BotUser(id=2, telegram_id=1002, referral_code="c2"),
                Plan(id=1, name="monthly", price=200, duration_days=30, data_limit_gb=10),
            ])
            await session.flush()
            session.add(UserService(id=10, bot_user_id=1, plan_id=1, pg_user_id=500, pg_username="svc<b>"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        self.settings.stop()
        await self.engine.dispose()

    async def renewal(self, *, note: str = "renew:10", status: str = "delivered", user_id: int = 1) -> Order:
        async with self.Session() as session:
            order = Order(user_id=user_id, plan_id=1, amount=200, status=status, note=note, service_id=10)
            session.add(order)
            await session.commit()
            return order

    async def offer(self, order: Order) -> bool:
        async with self.Session() as session:
            return await queue_auto_renew_offer(session, await session.get(Order, order.id))

    async def notices(self) -> list[CustomerNotice]:
        async with self.Session() as session:
            return list(await session.scalars(select(CustomerNotice)))

    async def test_offer_follows_the_second_manual_renewal_once(self) -> None:
        self.assertFalse(await self.offer(await self.renewal()))
        second = await self.renewal()
        self.assertTrue(await self.offer(second))
        self.assertFalse(await self.offer(second))
        self.assertFalse(await self.offer(await self.renewal()))
        notices = await self.notices()
        self.assertEqual([(n.action, n.service_id, n.telegram_id) for n in notices], [(ACTION, 10, 1001)])

    async def test_two_delivery_hooks_at_once_queue_one_offer(self) -> None:
        import asyncio

        await self.renewal()
        second, third = await self.renewal(), await self.renewal()
        results = await asyncio.gather(self.offer(second), self.offer(third))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(await self.notices()), 1)

    async def test_automatic_and_undelivered_renewals_do_not_count(self) -> None:
        await self.renewal(note="renew:10:auto")
        await self.renewal(status="paid")
        self.assertFalse(await self.offer(await self.renewal()))
        self.assertEqual(await self.notices(), [])

    async def test_no_offer_when_automatic_renewal_is_already_on(self) -> None:
        async with self.Session() as session:
            session.add(ServiceAutomation(service_id=10, renew_enabled=True))
            await session.commit()
        await self.renewal()
        self.assertFalse(await self.offer(await self.renewal()))

    async def test_switched_off_or_wallet_off_sends_nothing(self) -> None:
        await self.renewal()
        second = await self.renewal()
        for ui in ({"auto_renew_offer_enabled": "0"}, {"pay_wallet_enabled": "0"}):
            with patch(SETTINGS, new=AsyncMock(return_value=ui)):
                self.assertFalse(await self.offer(second))
        self.assertEqual(await self.notices(), [])

    async def test_an_order_of_another_customer_is_never_offered(self) -> None:
        await self.renewal(user_id=2)
        self.assertFalse(await self.offer(await self.renewal(user_id=2)))
        self.assertEqual(await self.notices(), [])

    async def test_a_linked_service_is_not_offered(self) -> None:
        async with self.Session() as session:
            (await session.get(UserService, 10)).remark = "linked"
            await session.commit()
        await self.renewal()
        self.assertFalse(await self.offer(await self.renewal()))

    def test_the_notice_escapes_the_name_and_opens_the_automation_settings(self) -> None:
        self.assertIn("svc&lt;b&gt;", notice_text(ACTION, service_name="svc<b>", reason=""))
        notice = CustomerNotice(action=ACTION, service_id=10, bot_user_id=1, telegram_id=1001, reason="")
        data = [b.callback_data for row in notice_markup_for(notice).inline_keyboard for b in row]
        self.assertEqual(data[0], "nto:svc:auto:10")


class AutoRenewOfferMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_the_rows(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("offer", ROOT / "alembic/versions/0123_auto_renew_offer.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE service_automations (service_id INTEGER PRIMARY KEY, renew_enabled BOOLEAN)"))
            conn.execute(text("INSERT INTO service_automations VALUES (1, false), (2, true)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("service_automations")}
                self.assertIn("renew_offered_at", columns)
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT count(*) FROM service_automations")).scalar(), 2)
                columns = {c["name"] for c in sa.inspect(conn).get_columns("service_automations")}
                self.assertNotIn("renew_offered_at", columns)
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
