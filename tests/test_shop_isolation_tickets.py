"""Tickets belong to the shop stamped on them, not to the customer's current attribution."""

from __future__ import annotations

import unittest


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Ticket, TicketStatus
from app.services.admin_counters import count_open_tickets
from app.services.table_bulk import bulk_close_bot_tickets
from app.services.ticket_scope import staff_ticket_in_scope
from app.services.tickets import list_open_tickets
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION
from tests.postgres_support import scoped_async_engine

SHOP_ID = 5
PLATFORM_TICKET, SHOP_TICKET, UNATTRIBUTED_SHOP_TICKET = 1, 2, 3


class TicketScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                # Customer who is attributed to the shop but also wrote to the platform.
                BotUser(id=7, telegram_id=107, referral_code="both", role="user", reseller_id=SHOP_ID),
                BotUser(id=8, telegram_id=108, referral_code="plain", role="user"),
            ])
            await session.flush()
            session.add_all([
                Ticket(id=PLATFORM_TICKET, user_id=7, subject="platform", status=TicketStatus.OPEN.value, reseller_id=None),
                Ticket(id=SHOP_TICKET, user_id=7, subject="shop", status=TicketStatus.OPEN.value, reseller_id=SHOP_ID),
                Ticket(id=UNATTRIBUTED_SHOP_TICKET, user_id=8, subject="shop ticket", status=TicketStatus.OPEN.value, reseller_id=SHOP_ID),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def ids(self, **scope) -> set[int]:
        async with self.Session() as session:
            return {int(t.id) for t in await list_open_tickets(session, **scope)}

    async def status_of(self, ticket_id: int) -> str:
        async with self.Session() as session:
            return (await session.get(Ticket, ticket_id)).status

    async def test_a_shop_sees_only_tickets_stamped_with_its_shop(self):
        self.assertEqual(await self.ids(reseller_id=SHOP_ID), {SHOP_TICKET, UNATTRIBUTED_SHOP_TICKET})

    async def test_the_platform_keeps_its_ticket_even_when_the_customer_is_attributed_to_a_shop(self):
        self.assertEqual(await self.ids(platform_only=True), {PLATFORM_TICKET})

    async def test_badge_counters_follow_the_ticket_shop(self):
        async with self.Session() as session:
            self.assertEqual(await count_open_tickets(session, shop_id=SHOP_ID), 2)
            self.assertEqual(await count_open_tickets(session, shop_id=None), 1)

    async def test_staff_scope_checks_the_ticket_not_the_customer(self):
        async with self.Session() as session:
            platform_ticket = await session.get(Ticket, PLATFORM_TICKET)
            shop_ticket = await session.get(Ticket, SHOP_TICKET)
        shop_staff = {**SHOP_SESSION, "permissions": ["tickets"]}
        self.assertFalse(staff_ticket_in_scope(shop_staff, platform_ticket))
        self.assertTrue(staff_ticket_in_scope(shop_staff, shop_ticket))
        self.assertTrue(staff_ticket_in_scope(OWNER_SESSION, platform_ticket))
        self.assertFalse(staff_ticket_in_scope(OWNER_SESSION, shop_ticket))

    async def test_shop_bulk_close_leaves_the_platform_ticket_open(self):
        shop_staff = {**SHOP_SESSION, "permissions": ["tickets"]}
        async with self.Session() as session:
            ok, fail = await bulk_close_bot_tickets(session, shop_staff, [PLATFORM_TICKET, SHOP_TICKET])
        self.assertEqual((ok, fail), (1, 1))
        self.assertEqual(await self.status_of(SHOP_TICKET), TicketStatus.CLOSED.value)
        self.assertEqual(await self.status_of(PLATFORM_TICKET), TicketStatus.OPEN.value)

    async def test_owner_bulk_close_does_not_touch_a_shop_ticket_of_an_unattributed_user(self):
        async with self.Session() as session:
            ok, fail = await bulk_close_bot_tickets(
                session, OWNER_SESSION, [UNATTRIBUTED_SHOP_TICKET, SHOP_TICKET, PLATFORM_TICKET]
            )
        self.assertEqual((ok, fail), (1, 2))
        self.assertEqual(await self.status_of(UNATTRIBUTED_SHOP_TICKET), TicketStatus.OPEN.value)
        self.assertEqual(await self.status_of(SHOP_TICKET), TicketStatus.OPEN.value)
        self.assertEqual(await self.status_of(PLATFORM_TICKET), TicketStatus.CLOSED.value)
