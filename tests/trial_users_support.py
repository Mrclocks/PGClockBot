"""Shared fixtures for the trial users tests: a throw-away database with two shops."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order, Plan, Role
from app.services.trial_users import list_trial_users

OWNER = {
    "role": "admin",
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def shop_staff(shop_id: int, permissions: list[str]) -> dict:
    return {"role": "reseller", "permissions": permissions, "bot_user_id": shop_id}


class TrialUsersTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            self.shop_a = await self._user(session, 9001, role=Role.RESELLER.value)
            self.shop_b = await self._user(session, 9002, role=Role.RESELLER.value)
            self.trial_plan = Plan(name="trial", price=0, is_trial=True)
            self.paid_plan = Plan(name="paid", price=50000)
            session.add_all([self.trial_plan, self.paid_plan])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, telegram_id: int, *, name: str | None = None, shop: int | None = None, role: str = Role.USER.value) -> BotUser:
        user = BotUser(
            telegram_id=telegram_id,
            username=f"u{telegram_id}",
            full_name=name or f"user {telegram_id}",
            role=role,
            referral_code=f"R{telegram_id}",
            reseller_id=shop,
        )
        session.add(user)
        await session.flush()
        return user

    async def _order(self, session, user, plan, *, shop=None, amount=0, status="delivered", note=None, service_id=None) -> Order:
        order = Order(
            user_id=user.id, plan_id=plan.id, reseller_id=shop, amount=amount,
            status=status, note=note, service_id=service_id,
        )
        session.add(order)
        await session.flush()
        return order

    async def _take_trial(self, session, user, *, shop=None, service=None) -> Order:
        order = await self._order(session, user, self.trial_plan, shop=shop, service_id=service.id if service else None)
        return order

    async def _buy(self, session, user, *, shop=None, amount=50000, **kwargs) -> Order:
        return await self._order(session, user, self.paid_plan, shop=shop, amount=amount, **kwargs)

    async def _list(self, session, staff, shop_id, **kwargs):
        return await list_trial_users(session, staff=staff, shop_id=shop_id, now=NOW, **kwargs)
