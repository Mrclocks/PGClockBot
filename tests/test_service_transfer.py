"""Service transfer: a one-time link, one owner at a time, inside the shop, without the sender's automation."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, CustomerNotice, Order, Plan, ServiceAutomation, ServiceTransfer, UserService
from app.services import service_transfer as transfer
from app.services.customer_action_notices import notice_text
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
UI = {
    "service_transfer_enabled": "1", "service_transfer_plans": "all",
    "service_transfer_link_hours": "24", "service_transfer_per_period": "1",
}
OWNER, FRIEND, OTHER, SHOP = 1, 2, 3, 7


class FakePG:
    def __init__(self) -> None:
        self.revoked: list[int] = []
        self.fail = False

    async def revoke_sub_by_id(self, user_id: int) -> dict:
        if self.fail:
            raise TimeoutError("lost")
        self.revoked.append(user_id)
        return {"id": user_id, "subscription_url": "https://sub.example/sub/NEWTOKEN123"}


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.pg = FakePG()
        self.patch = patch("app.services.service_transfer.get_pg", return_value=self.pg)
        self.patch.start()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=OWNER, telegram_id=1001, referral_code="c1"),
                BotUser(id=FRIEND, telegram_id=1002, referral_code="c2"),
                BotUser(id=OTHER, telegram_id=1003, referral_code="c3"),
                BotUser(id=SHOP, telegram_id=1007, referral_code="s", role="reseller"),
            ])
            await session.flush()
            session.add_all([Plan(id=1, name="month", price=100), Plan(id=2, name="other", price=100)])
            await session.flush()
            session.add_all([
                UserService(id=10, bot_user_id=OWNER, plan_id=1, pg_user_id=500, pg_username="svc<b>", subscription_url="https://sub.example/sub/OLD"),
                UserService(id=11, bot_user_id=OWNER, pg_user_id=501, pg_username="shop", shop_id=SHOP),
            ])
            await session.flush()
            session.add(ServiceAutomation(service_id=10, renew_enabled=True, volume_enabled=True))
            await session.commit()

    async def asyncTearDown(self) -> None:
        self.patch.stop()
        await self.engine.dispose()

    async def make(self, *, user_id: int = OWNER, service_id: int = 10, shop_id: int | None = None, ui: dict | None = None) -> str:
        async with self.Session() as session:
            _row, token = await transfer.create_transfer(
                session, await session.get(BotUser, user_id), service_id, shop_id=shop_id, ui=ui or UI
            )
            return token

    async def accept(self, token: str, *, user_id: int = FRIEND, shop_id: int | None = None, ui: dict | None = None) -> UserService:
        async with self.Session() as session:
            return await transfer.accept_transfer(
                session, await session.get(BotUser, user_id), token, shop_id=shop_id, ui=ui or UI
            )

    async def service(self, service_id: int = 10) -> UserService:
        async with self.Session() as session:
            return await session.get(UserService, service_id)

    async def test_accepting_moves_the_service_stops_automation_and_rotates_the_link(self) -> None:
        token = await self.make()
        async with self.Session() as session:
            stored = (await session.scalars(select(ServiceTransfer))).one()
            self.assertNotEqual(stored.token_hash, token)
            self.assertEqual(stored.token_hash, transfer.token_hash(token))
        await self.accept(token)
        service = await self.service()
        self.assertEqual((service.bot_user_id, service.subscription_token), (FRIEND, "NEWTOKEN123"))
        self.assertEqual(self.pg.revoked, [500])
        async with self.Session() as session:
            automation = await session.get(ServiceAutomation, 10)
            self.assertEqual((automation.renew_enabled, automation.volume_enabled), (False, False))
            notices = list(await session.scalars(select(CustomerNotice)))
        self.assertEqual([(n.action, n.telegram_id) for n in notices], [("service_transfer_done", 1001)])
        self.assertIn("svc&lt;b&gt;", notice_text("service_transfer_done", service_name="svc<b>", reason=""))

    async def test_a_link_works_once_even_for_two_people_at_once(self) -> None:
        token = await self.make()
        results = await asyncio.gather(self.accept(token), self.accept(token, user_id=OTHER), return_exceptions=True)
        self.assertEqual(sorted(type(r).__name__ for r in results), ["UserService", "ValueError"])
        with self.assertRaises(ValueError):
            await self.accept(token, user_id=OTHER)
        self.assertIn((await self.service()).bot_user_id, (FRIEND, OTHER))

    async def test_expired_cancelled_foreign_or_own_links_are_refused(self) -> None:
        token = await self.make()
        with self.assertRaises(ValueError):
            await self.accept(token, user_id=OWNER)
        with self.assertRaises(ValueError):
            await self.accept(token, shop_id=SHOP)
        with self.assertRaises(ValueError):
            await self.accept("not-a-real-token-at-all")
        async with self.Session() as session:
            await session.execute(sa.update(ServiceTransfer).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await session.commit()
        with self.assertRaises(ValueError):
            await self.accept(token)
        fresh = await self.make()
        async with self.Session() as session:
            self.assertTrue(await transfer.cancel_transfer(session, await session.get(BotUser, OWNER), 10, shop_id=None))
        with self.assertRaises(ValueError):
            await self.accept(fresh)
        self.assertEqual((await self.service()).bot_user_id, OWNER)

    async def test_only_the_owner_in_its_shop_makes_one_open_link(self) -> None:
        for kwargs in ({"user_id": FRIEND}, {"service_id": 11}, {"ui": {"service_transfer_enabled": "0"}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.make(**kwargs)
        await self.make()
        with self.assertRaises(ValueError):
            await self.make()

    async def test_the_period_limit_counts_accepted_transfers(self) -> None:
        await self.accept(await self.make())
        with self.assertRaises(ValueError):
            await self.make(user_id=FRIEND)

    async def test_a_service_being_cancelled_cannot_move(self) -> None:
        token = await self.make()
        async with self.Session() as session:
            (await session.get(UserService, 10)).cancellation_pending = True
            await session.commit()
        with self.assertRaises(ValueError):
            await self.accept(token)
        self.assertEqual((await self.service()).bot_user_id, OWNER)

    async def test_a_frozen_service_gets_no_link(self) -> None:
        from app.db.models import ServiceFreeze

        async with self.Session() as session:
            session.add(ServiceFreeze(service_id=10, user_id=OWNER, status="frozen", remaining_seconds=60,
                                      resume_by=datetime.now(timezone.utc) + timedelta(days=1)))
            await session.commit()
        with self.assertRaisesRegex(ValueError, "متوقف"):
            await self.make()

    async def test_a_failed_link_rotation_still_gives_the_service(self) -> None:
        self.pg.fail = True
        await self.accept(await self.make())
        service = await self.service()
        self.assertEqual((service.bot_user_id, service.subscription_url), (FRIEND, "https://sub.example/sub/OLD"))

    async def test_only_services_on_chosen_plans_move_and_the_plan_is_checked_again_on_accept(self) -> None:
        with self.assertRaisesRegex(ValueError, "پلن"):
            await self.make(ui={**UI, "service_transfer_plans": "2"})
        async with self.Session() as session:
            service = await session.get(UserService, 10)
            self.assertIsNone(await transfer.transfer_button(session, service, {**UI, "service_transfer_plans": "2"}))
        token = await self.make(ui={**UI, "service_transfer_plans": "1"})
        for ui in ({**UI, "service_transfer_plans": "2"}, {**UI, "service_transfer_enabled": "0"}):
            with self.subTest(ui=ui), self.assertRaises(ValueError):
                await self.accept(token, ui=ui)
        self.assertEqual((await self.service()).bot_user_id, OWNER)
        await self.accept(token)
        self.assertEqual((await self.service()).bot_user_id, FRIEND)

    async def test_an_unpaid_order_on_the_service_blocks_the_link(self) -> None:
        async with self.Session() as session:
            session.add(Order(user_id=OWNER, plan_id=1, amount=100, status="awaiting_receipt", service_id=10))
            await session.commit()
        with self.assertRaisesRegex(ValueError, "سفارش"):
            await self.make()

    async def test_a_failed_link_rotation_is_retried_by_the_job(self) -> None:
        self.pg.fail = True
        await self.accept(await self.make())
        async with self.Session() as session:
            self.assertTrue((await session.scalars(select(ServiceTransfer))).one().link_rotation_pending)
            self.assertEqual(await transfer.retry_link_rotations(session), 0)
        self.pg.fail = False
        async with self.Session() as session:
            self.assertEqual(await transfer.retry_link_rotations(session), 1)
            self.assertEqual(await transfer.retry_link_rotations(session), 0)
            self.assertFalse((await session.scalars(select(ServiceTransfer))).one().link_rotation_pending)
        self.assertEqual(((await self.service()).subscription_token, self.pg.revoked), ("NEWTOKEN123", [500]))

    async def test_the_new_owner_cannot_cancel_for_a_refund_of_the_senders_payment(self) -> None:
        from app.services.service_cancellations import request_cancellation

        await self.accept(await self.make())
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, "منتقل"):
                await request_cancellation(session, await session.get(BotUser, FRIEND), 10, shop_id=None, reason="x")

    async def test_a_trial_service_never_moves_even_on_all_plans(self) -> None:
        token = await self.make()
        async with self.Session() as session:
            await session.execute(sa.update(Plan).where(Plan.id == 1).values(is_trial=True))
            await session.commit()
        with self.assertRaises(ValueError):
            await self.accept(token)
        with self.assertRaises(ValueError):
            await self.make()
        async with self.Session() as session:
            owner = await session.get(BotUser, OWNER)
            await transfer.cancel_transfer(session, owner, 10, shop_id=None)
            # No new link is offered; the open one could still be cancelled above.
            self.assertIsNone(await transfer.transfer_button(session, await session.get(UserService, 10), UI))
        self.assertEqual((await self.service()).bot_user_id, OWNER)

    async def test_voiding_the_senders_payment_is_left_to_staff(self) -> None:
        from types import SimpleNamespace

        from app.services.payment_voids import _resolve_plan

        async with self.Session() as session:
            session.add(Order(id=90, user_id=OWNER, plan_id=1, amount=100, status="delivered", service_id=10))
            await session.commit()
        await self.accept(await self.make())
        async with self.Session() as session:
            payment = SimpleNamespace(method="card", is_wallet_topup=False, amount=100)
            with self.assertRaisesRegex(ValueError, "منتقل"):
                await _resolve_plan(session, payment, await session.get(Order, 90), False)

    async def test_the_more_menu_offers_start_then_cancel(self) -> None:
        async with self.Session() as session:
            service = await session.get(UserService, 10)
            self.assertEqual(await transfer.transfer_button(session, service, UI), "start")
            self.assertIsNone(await transfer.transfer_button(session, service, {}))
            self.assertIsNone(await transfer.transfer_button(session, service, {"service_transfer_enabled": "1"}))
        await self.make()
        async with self.Session() as session:
            self.assertEqual(await transfer.transfer_button(session, await session.get(UserService, 10), {}), "cancel")


class TransferMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        spec = importlib.util.spec_from_file_location("transfer", ROOT / "alembic/versions/0126_service_transfers.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE user_services (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO bot_users VALUES (1)"))
            conn.execute(text("INSERT INTO user_services VALUES (10)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                insert = text("INSERT INTO service_transfers (service_id, from_user_id, token_hash, status, expires_at) "
                              "VALUES (10, 1, :h, :status, now())")
                conn.execute(insert, {"h": "a", "status": "open"})
                conn.execute(insert, {"h": "b", "status": "accepted"})
                with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                    conn.execute(insert, {"h": "c", "status": "open"})
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("service_transfers"))
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
