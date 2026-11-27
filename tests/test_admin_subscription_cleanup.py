"""A removed shop takes its subscription row with it; leftovers go only once the panel says 404."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, PgAdminSubscription, PgStaffAccess, ResellerProfile, Role
from app.services.admin_subscription_cleanup import delete_unheld_subscription, prune_leftover_subscriptions
from app.services.pasarguard import PasarGuardError
from app.services.resellers import revoke_reseller
from tests.postgres_support import scoped_async_engine

ALLOW_CHECK = "app.services.resellers.assert_platform_may_delete_pg_admins"


class GatePanel:
    """``get_admin_gate`` answers from a fixed table; unknown names raise like an outage."""

    def __init__(self, gates: dict[str, str]) -> None:
        self.gates = gates
        self.asked: list[str] = []

    async def get_admin_gate(self, username: str) -> tuple[str, dict | None]:
        self.asked.append(username)
        if username not in self.gates:
            raise PasarGuardError("down", 502)
        state = self.gates[username]
        return state, ({"username": username} if state == "ok" else None)


class CleanupHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _add_subscription(self, pg_username: str) -> None:
        async with self.Session() as session:
            session.add(PgAdminSubscription(pg_username=pg_username, base_gb=100, extra_gb_purchased=0))
            await session.commit()

    async def _add_shop(self, user_id: int, pg_username: str) -> None:
        async with self.Session() as session:
            session.add(BotUser(id=user_id, telegram_id=user_id, role=Role.RESELLER.value, referral_code=f"c{user_id}"))
            session.add(ResellerProfile(user_id=user_id, pg_admin_username=pg_username, is_active=True))
            await session.commit()
        await self._add_subscription(pg_username)

    async def _add_staff(self, pg_username: str) -> None:
        async with self.Session() as session:
            session.add(PgStaffAccess(pg_username=pg_username, web_username=f"w-{pg_username}", web_password_hash="x"))
            await session.commit()

    async def _subscriptions(self) -> set[str]:
        async with self.Session() as session:
            return set(await session.scalars(select(PgAdminSubscription.pg_username)))


class RevokeRemovesSubscriptionTests(CleanupHarness):
    async def _revoke(self, user_id: int, *, delete_pg_admin: bool, delete_admin: AsyncMock | None = None) -> None:
        pg = AsyncMock()
        pg.delete_admin = delete_admin or AsyncMock(return_value=None)
        with patch("app.services.pasarguard.get_pg", return_value=pg), patch(ALLOW_CHECK, new=AsyncMock()):
            async with self.Session() as session:
                await revoke_reseller(session, user_id, delete_pg_admin=delete_pg_admin, reason="test")

    async def test_revoke_with_panel_admin_deletes_the_subscription(self) -> None:
        await self._add_shop(501, "shop501")
        await self._add_shop(502, "shop502")
        await self._revoke(501, delete_pg_admin=True)
        self.assertEqual(await self._subscriptions(), {"shop502"})

    async def test_revoke_of_an_admin_already_gone_deletes_the_subscription(self) -> None:
        await self._add_shop(501, "shop501")
        await self._revoke(501, delete_pg_admin=True, delete_admin=AsyncMock(side_effect=PasarGuardError("gone", 404)))
        self.assertEqual(await self._subscriptions(), set())

    async def test_refused_panel_delete_keeps_the_subscription(self) -> None:
        await self._add_shop(501, "shop501")
        with self.assertRaises(ValueError):
            await self._revoke(501, delete_pg_admin=True, delete_admin=AsyncMock(side_effect=PasarGuardError("no", 403)))
        self.assertEqual(await self._subscriptions(), {"shop501"})

    async def test_revoke_that_keeps_the_panel_admin_keeps_its_subscription(self) -> None:
        await self._add_shop(501, "shop501")
        await self._revoke(501, delete_pg_admin=False)
        self.assertEqual(await self._subscriptions(), {"shop501"})

    async def test_subscription_held_by_a_staff_login_is_kept(self) -> None:
        await self._add_shop(501, "shop501")
        await self._add_staff("shop501")
        await self._revoke(501, delete_pg_admin=True)
        self.assertEqual(await self._subscriptions(), {"shop501"})


class LeftoverCleanupTests(CleanupHarness):
    async def test_only_confirmed_missing_unheld_admins_are_removed(self) -> None:
        await self._add_subscription("gone")
        await self._add_subscription("plain-admin")
        await self._add_subscription("unreachable")
        await self._add_shop(601, "live-shop")
        await self._add_subscription("staff-admin")
        await self._add_staff("staff-admin")
        panel = GatePanel({"gone": "missing", "plain-admin": "ok"})

        removed = await prune_leftover_subscriptions(self.Session, panel)

        self.assertEqual(removed, ["gone"])
        self.assertEqual(await self._subscriptions(), {"plain-admin", "unreachable", "live-shop", "staff-admin"})
        self.assertEqual(sorted(panel.asked), ["gone", "plain-admin", "unreachable"])

    async def test_running_twice_is_harmless(self) -> None:
        await self._add_subscription("gone")
        panel = GatePanel({"gone": "missing"})
        self.assertEqual(await prune_leftover_subscriptions(self.Session, panel), ["gone"])
        self.assertEqual(await prune_leftover_subscriptions(self.Session, panel), [])

    async def test_a_shop_linked_after_the_probe_keeps_its_row(self) -> None:
        await self._add_subscription("relinked")
        async with self.Session() as session:
            session.add(BotUser(id=701, telegram_id=701, role=Role.RESELLER.value, referral_code="c701"))
            session.add(ResellerProfile(user_id=701, pg_admin_username="relinked", is_active=True))
            await session.commit()
            self.assertFalse(await delete_unheld_subscription(session, "relinked"))
            await session.commit()
        self.assertEqual(await self._subscriptions(), {"relinked"})

    async def test_slow_panel_keeps_the_row(self) -> None:
        await self._add_subscription("slow")

        class SlowPanel:
            async def get_admin_gate(self, username: str) -> tuple[str, None]:
                raise asyncio.TimeoutError

        self.assertEqual(await prune_leftover_subscriptions(self.Session, SlowPanel()), [])
        self.assertEqual(await self._subscriptions(), {"slow"})


if __name__ == "__main__":
    unittest.main()
