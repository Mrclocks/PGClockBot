"""Deleting a shop, wheel prizes, the points ledger and "has a service" follow the shop of each record."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.menu_nav import user_has_services
from app.db import Base
from app.db.models import (
    BotUser, BotUserShop, LuckyWheelUserState, PointsTransaction, ResellerProfile, Ticket, UserService,
)
from app.services.home_overview import admin_customer_counts, bot_panel_summary
from app.services.loyalty import month_earned_points
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user
from app.services.lucky_wheel import _resolve_service
from app.services.tickets import list_open_tickets
from app.services.users import delete_bot_user, reset_shop_reseller_id, set_shop_reseller_id

SHOP_ID, OTHER_SHOP, CUSTOMER = 5, 6, 7
PLATFORM_SVC, SHOP_SVC, OTHER_SHOP_SVC = 31, 32, 33


class AuditFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="s5", role="reseller"),
                BotUser(id=OTHER_SHOP, telegram_id=106, referral_code="s6", role="reseller"),
                BotUser(id=CUSTOMER, telegram_id=107, referral_code="c", role="user", reseller_id=SHOP_ID),
            ])
            await session.flush()
            session.add_all([
                ResellerProfile(user_id=SHOP_ID, is_active=True),
                ResellerProfile(user_id=OTHER_SHOP, is_active=True),
                UserService(id=PLATFORM_SVC, bot_user_id=CUSTOMER, pg_user_id=1, pg_username="p", shop_id=None),
                UserService(id=SHOP_SVC, bot_user_id=CUSTOMER, pg_user_id=2, pg_username="s", shop_id=SHOP_ID),
                UserService(id=OTHER_SHOP_SVC, bot_user_id=CUSTOMER, pg_user_id=3, pg_username="o", shop_id=OTHER_SHOP),
                BotUserShop(bot_user_id=CUSTOMER, shop_id=SHOP_ID),
                BotUserShop(bot_user_id=CUSTOMER, shop_id=None),
                LuckyWheelUserState(user_id=CUSTOMER, reseller_id=None),
                LuckyWheelUserState(user_id=CUSTOMER, reseller_id=SHOP_ID),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmp.cleanup()

    async def test_deleting_a_shop_removes_its_wheel_state_and_releases_its_services(self):
        async with self.Session() as session:
            await delete_bot_user(session, SHOP_ID, delete_pg_services=False, delete_pg_admin=False)
        async with self.Session() as session:
            states = [(s.user_id, s.reseller_id) for s in await session.scalars(select(LuckyWheelUserState))]
            services = {s.id: s.shop_id for s in await session.scalars(select(UserService))}
            members = {(m.bot_user_id, m.shop_id) for m in await session.scalars(select(BotUserShop))}
        self.assertEqual(states, [(CUSTOMER, None)])
        self.assertEqual(services, {PLATFORM_SVC: None, SHOP_SVC: None, OTHER_SHOP_SVC: OTHER_SHOP})
        self.assertEqual(members, {(CUSTOMER, None)})

    async def resolve(self, bot_shop: int | None, service_id: int | None):
        token = set_shop_reseller_id(bot_shop)
        try:
            async with self.Session() as session:
                customer = await session.get(BotUser, CUSTOMER)
                return await _resolve_service(session, customer, service_id)
        finally:
            reset_shop_reseller_id(token)

    async def test_a_wheel_prize_targets_only_a_service_the_wheels_shop_sold(self):
        self.assertEqual((await self.resolve(SHOP_ID, None)).id, SHOP_SVC)
        self.assertEqual((await self.resolve(None, None)).id, PLATFORM_SVC)
        self.assertEqual((await self.resolve(SHOP_ID, SHOP_SVC)).id, SHOP_SVC)
        for bot_shop, service_id in ((SHOP_ID, OTHER_SHOP_SVC), (SHOP_ID, PLATFORM_SVC), (None, SHOP_SVC)):
            with self.assertRaisesRegex(ValueError, "سرویس معتبر نیست"):
                await self.resolve(bot_shop, service_id)

    async def test_has_services_looks_at_the_bot_in_use(self):
        async with self.Session() as session:
            await session.execute(UserService.__table__.delete().where(UserService.id.in_([PLATFORM_SVC, SHOP_SVC])))
            await session.commit()
        for bot_shop, expected in ((None, False), (SHOP_ID, False), (OTHER_SHOP, True)):
            token = set_shop_reseller_id(bot_shop)
            try:
                async with self.Session() as session:
                    self.assertEqual(await user_has_services(session, CUSTOMER), expected, bot_shop)
            finally:
                reset_shop_reseller_id(token)

    async def test_month_earned_points_counts_one_purse(self):
        async with self.Session() as session:
            now = datetime.now(timezone.utc)
            session.add_all([
                PointsTransaction(user_id=CUSTOMER, amount=10, balance_after=10, tx_type="earn", idempotency_key="a", reseller_id=None, created_at=now),
                PointsTransaction(user_id=CUSTOMER, amount=25, balance_after=25, tx_type="earn", idempotency_key="b", reseller_id=SHOP_ID, created_at=now),
            ])
            await session.commit()
            self.assertEqual(await month_earned_points(session, CUSTOMER, shop_id=None), 10)
            self.assertEqual(await month_earned_points(session, CUSTOMER, shop_id=SHOP_ID), 25)

    async def test_listing_tickets_without_a_scope_never_returns_every_shop(self):
        async with self.Session() as session:
            session.add_all([
                Ticket(user_id=CUSTOMER, subject="platform", status="open", reseller_id=None),
                Ticket(user_id=CUSTOMER, subject="shop", status="open", reseller_id=SHOP_ID),
            ])
            await session.commit()
            subjects = {t.subject for t in await list_open_tickets(session)}
        self.assertEqual(subjects, {"platform"})

    async def test_shop_message_without_shop_bot_is_refused_instead_of_using_platform_bot(self):
        async with self.Session() as session:
            customer = await session.get(BotUser, CUSTOMER)
            with self.assertRaises(ShopBotUnavailable):
                await open_notify_bot_for_user(session, customer, shop_id=SHOP_ID)

    async def test_platform_dashboard_counts_only_platform_services(self):
        async with self.Session() as session:
            summary = await bot_panel_summary(session)
            counts = await admin_customer_counts(session)
        self.assertEqual(summary["services"], 1)
        self.assertEqual(counts["orders"], 0)
