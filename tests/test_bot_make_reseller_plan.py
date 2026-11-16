"""«ساخت نماینده» in the bot asks for a reseller package before creating the PasarGuard admin."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import make_reseller_plan as flow
from app.db import Base
from app.db.models import BotUser, ResellerPlan, ResellerProfile

ROLES = [
    {"id": 1, "name": "owner", "is_owner": True},
    {"id": 4, "name": "shop", "is_owner": False},
    {"id": 7, "name": "staff", "is_owner": False},
]


class FakeState:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state, self.data = None, {}


class FakePanel:
    def __init__(self) -> None:
        self.create_admin = AsyncMock(return_value={})

    async def get_admin_roles(self) -> list[dict]:
        return list(ROLES)


def _callback(data: str) -> SimpleNamespace:
    message = SimpleNamespace(answer=AsyncMock())
    return SimpleNamespace(
        data=data, answer=AsyncMock(), message=message,
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


class MakeResellerPlanFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'b.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=100, referral_code="r1", role="admin"))
            session.add(BotUser(id=5, telegram_id=500, referral_code="r5", role="user"))
            session.add(ResellerPlan(
                id=3, name="<b>shop</b>", price=0, plan_kind="subscription", billing_mode="payg",
                price_per_gb=1000, pg_role_id=7, is_active=True,
            ))
            session.add(ResellerPlan(id=8, name="off", price=1, plan_kind="subscription", is_active=False))
            await session.commit()
        self.gates = [
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            patch("app.bot.auth.platform_can_manage_representatives", AsyncMock(return_value=True)),
            patch.object(flow, "is_platform_admin", return_value=True),
        ]
        for gate in self.gates:
            gate.start()

    async def asyncTearDown(self) -> None:
        for gate in self.gates:
            gate.stop()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _ask(self) -> tuple[FakeState, SimpleNamespace]:
        state, message = FakeState(), SimpleNamespace(answer=AsyncMock())
        async with self.Session() as session:
            await flow.ask_reseller_plan(message, state, session, user_id=5, can_approve=True)
        return state, message

    async def _choose(self, state: FakeState, data: str, pg: FakePanel) -> SimpleNamespace:
        callback = _callback(data)
        async with self.Session() as session:
            admin = await session.get(BotUser, 1)
            with patch("app.services.pasarguard.get_pg", return_value=pg):
                await flow.make_reseller_with_plan(callback, state=state, session=session, db_user=admin)
        return callback

    async def _profile(self) -> ResellerProfile | None:
        async with self.Session() as session:
            return await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 5))

    async def test_the_menu_lists_only_active_packages_and_a_no_package_choice(self) -> None:
        state, message = await self._ask()
        markup = message.answer.await_args.kwargs["reply_markup"]
        callbacks = [row[0].callback_data for row in markup.inline_keyboard]
        self.assertIn("mkres:p:3", callbacks)
        self.assertNotIn("mkres:p:8", callbacks)
        self.assertIn("mkres:p:0", callbacks)
        self.assertEqual(state.state, flow.MakeResellerPlan.choosing)
        self.assertTrue(all(len(c.encode()) <= 64 for c in callbacks))

    async def test_choosing_a_package_creates_the_admin_with_its_role(self) -> None:
        state, _ = await self._ask()
        pg = FakePanel()
        await self._choose(state, "mkres:p:3", pg)
        self.assertEqual(pg.create_admin.await_args.args[0]["role_id"], 7)
        profile = await self._profile()
        self.assertEqual(profile.plan_id, 3)
        self.assertIsNone(state.state)

    async def test_no_package_on_a_panel_with_several_roles_explains_and_creates_nothing(self) -> None:
        state, _ = await self._ask()
        pg = FakePanel()
        callback = await self._choose(state, "mkres:p:0", pg)
        pg.create_admin.assert_not_awaited()
        self.assertIsNone(await self._profile())
        self.assertIn("پلن", callback.message.answer.await_args.args[0])

    async def test_an_inactive_or_forged_package_id_is_refused(self) -> None:
        for data in ("mkres:p:8", "mkres:p:999"):
            state, _ = await self._ask()
            pg = FakePanel()
            await self._choose(state, data, pg)
            pg.create_admin.assert_not_awaited()
        self.assertIsNone(await self._profile())

    async def test_a_tap_without_a_started_flow_does_nothing(self) -> None:
        pg = FakePanel()
        callback = await self._choose(FakeState(), "mkres:p:3", pg)
        pg.create_admin.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))


if __name__ == "__main__":
    unittest.main()
