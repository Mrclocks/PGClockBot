"""Phase 1E — resource owner_principal_id foundation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, Order, OrgPrincipal, Role, Ticket, UserService
from app.services import org_principals as op
from app.services.org_scope import scope_ids_for_principal
from app.services.resource_principal import (
    ResourcePrincipalResolution,
    build_unique_reseller_bot_user_principal_map,
    resolve_resource_principal,
    resource_in_principal_scope,
    safe_backfill_owner_principal_ids,
)
from tests.postgres_support import scoped_async_engine


class ResourcePrincipalPureTests(unittest.TestCase):
    def test_a_mapped_visible_to_a(self) -> None:
        res = resolve_resource_principal(
            SimpleNamespace(owner_principal_id=5, reseller_id=10)
        )
        self.assertEqual(res.principal_id, 5)
        self.assertEqual(res.source, "explicit")
        self.assertTrue(
            resource_in_principal_scope(
                actor_visible_principal_ids=frozenset({5, 6}),
                resolution=res,
            )
        )

    def test_b_visible_to_owner_via_scope(self) -> None:
        # Owner visible set includes all active principals (org_scope).
        owner = SimpleNamespace(id=1, depth=0, parent_id=None, status="active")
        a = SimpleNamespace(id=10, depth=1, parent_id=1, status="active")
        b = SimpleNamespace(id=20, depth=1, parent_id=1, status="active")
        visible = scope_ids_for_principal(owner, all_principals=[owner, a, b])
        res = ResourcePrincipalResolution(10, "explicit")
        self.assertTrue(
            resource_in_principal_scope(
                actor_visible_principal_ids=visible, resolution=res
            )
        )

    def test_c_not_visible_to_sibling(self) -> None:
        owner = SimpleNamespace(id=1, depth=0, parent_id=None, status="active")
        a = SimpleNamespace(id=10, depth=1, parent_id=1, status="active")
        b = SimpleNamespace(id=20, depth=1, parent_id=1, status="active")
        visible_b = scope_ids_for_principal(b, all_principals=[owner, a, b])
        res = ResourcePrincipalResolution(10, "explicit")
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=visible_b, resolution=res
            )
        )

    def test_d_a1_not_visible_to_a2(self) -> None:
        owner = SimpleNamespace(id=1, depth=0, parent_id=None, status="active")
        a = SimpleNamespace(id=10, depth=1, parent_id=1, status="active")
        a1 = SimpleNamespace(id=11, depth=2, parent_id=10, status="active")
        a2 = SimpleNamespace(id=12, depth=2, parent_id=10, status="active")
        visible_a2 = scope_ids_for_principal(
            a2, all_principals=[owner, a, a1, a2]
        )
        res = ResourcePrincipalResolution(11, "explicit")
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=visible_a2, resolution=res
            )
        )
        # Parent A sees A1 (descendant) — allowed by org_scope; child does not see parent.
        visible_a = scope_ids_for_principal(a, all_principals=[owner, a, a1, a2])
        self.assertTrue(
            resource_in_principal_scope(
                actor_visible_principal_ids=visible_a, resolution=res
            )
        )
        parent_res = ResourcePrincipalResolution(10, "explicit")
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=scope_ids_for_principal(
                    a1, all_principals=[owner, a, a1, a2]
                ),
                resolution=parent_res,
            )
        )

    def test_e_null_unknown_deny(self) -> None:
        res = resolve_resource_principal(
            SimpleNamespace(owner_principal_id=None, reseller_id=None)
        )
        self.assertEqual(res.source, "unknown")
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=frozenset({1, 2, 3}),
                resolution=res,
            )
        )
        # Unmapped reseller_id also unknown
        res2 = resolve_resource_principal(
            SimpleNamespace(owner_principal_id=None, reseller_id=99),
            reseller_bot_user_to_principal={10: 5},
        )
        self.assertEqual(res2.source, "unknown")
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=frozenset({5}),
                resolution=res2,
            )
        )

    def test_f_legacy_reseller_mapping(self) -> None:
        res = resolve_resource_principal(
            SimpleNamespace(owner_principal_id=None, reseller_id=10),
            reseller_bot_user_to_principal={10: 55},
        )
        self.assertEqual(res, ResourcePrincipalResolution(55, "legacy_reseller"))
        self.assertTrue(
            resource_in_principal_scope(
                actor_visible_principal_ids=frozenset({55}),
                resolution=res,
            )
        )

    def test_h_same_reseller_different_principal_isolated(self) -> None:
        # Same shop tenant id cannot isolate children — explicit principal does.
        map_legacy = {10: 100}  # reseller shop → parent principal only
        r_a1 = SimpleNamespace(owner_principal_id=111, reseller_id=10)
        r_a2 = SimpleNamespace(owner_principal_id=112, reseller_id=10)
        ra1 = resolve_resource_principal(
            r_a1, reseller_bot_user_to_principal=map_legacy
        )
        ra2 = resolve_resource_principal(
            r_a2, reseller_bot_user_to_principal=map_legacy
        )
        self.assertEqual(ra1.principal_id, 111)
        self.assertEqual(ra2.principal_id, 112)
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=frozenset({111}),
                resolution=ra2,
            )
        )

    def test_i_explicit_precedes_legacy(self) -> None:
        res = resolve_resource_principal(
            SimpleNamespace(owner_principal_id=7, reseller_id=10),
            reseller_bot_user_to_principal={10: 99},
        )
        self.assertEqual(res.principal_id, 7)
        self.assertEqual(res.source, "explicit")

    def test_j_pg_role_names_irrelevant(self) -> None:
        for name in ("admin", "operator", "RoleX", "whatever"):
            resource = SimpleNamespace(
                owner_principal_id=3,
                reseller_id=10,
                pg_role_name=name,
            )
            staffish = SimpleNamespace(role=name, owner_principal_id=3)
            res = resolve_resource_principal(resource)
            self.assertEqual(res.principal_id, 3)
            # Role string on a decoy object is ignored by resolver
            self.assertEqual(
                resolve_resource_principal(staffish).principal_id, 3
            )

    def test_user_service_legacy_via_reseller_arg(self) -> None:
        svc = SimpleNamespace(owner_principal_id=None)  # no reseller_id attr
        res = resolve_resource_principal(
            svc,
            legacy_reseller_id=10,
            reseller_bot_user_to_principal={10: 44},
        )
        self.assertEqual(res.source, "legacy_reseller")
        self.assertEqual(res.principal_id, 44)

    def test_ambiguous_bot_user_map_omitted(self) -> None:
        principals = [
            SimpleNamespace(id=1, bot_user_id=10, status="active"),
            SimpleNamespace(id=2, bot_user_id=10, status="active"),
            SimpleNamespace(id=3, bot_user_id=20, status="active"),
            SimpleNamespace(id=4, bot_user_id=30, status="disabled"),
        ]
        m = build_unique_reseller_bot_user_principal_map(principals)  # type: ignore[arg-type]
        self.assertNotIn(10, m)
        self.assertEqual(m.get(20), 3)
        self.assertNotIn(30, m)


class ResourcePrincipalBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _user(self, session, *, tg: int, reseller_id: int | None = None):
        u = BotUser(
            telegram_id=tg,
            role=Role.USER.value,
            referral_code=f"r{tg}",
            reseller_id=reseller_id,
            wallet_balance=0,
        )
        session.add(u)
        await session.flush()
        return u

    async def test_g_no_invented_ownership_without_map(self) -> None:
        async with self.Session() as session:
            await op.ensure_owner_principal(session)
            shop = await self._user(session, tg=1000)
            customer = await self._user(session, tg=1001, reseller_id=shop.id)
            await session.commit()
            stats = await safe_backfill_owner_principal_ids(session)
            await session.commit()
            await session.refresh(customer)
            # No OrgPrincipal.bot_user_id=shop.id → must stay NULL (not invented)
            self.assertIsNone(customer.owner_principal_id)
            self.assertEqual(stats["bot_users"], 0)

    async def test_f_backfill_when_reseller_principal_exists(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            shop = await self._user(session, tg=2000)
            principal_a = await op.create_principal(
                session,
                parent_id=owner.id,
                depth=1,
                bot_user_id=shop.id,
            )
            customer = await self._user(session, tg=2001, reseller_id=shop.id)
            order = Order(
                user_id=customer.id,
                amount=1000,
                reseller_id=shop.id,
            )
            ticket = Ticket(
                user_id=customer.id,
                reseller_id=shop.id,
                subject="hi",
            )
            svc = UserService(
                bot_user_id=customer.id,
                pg_username="u1",
            )
            session.add_all([order, ticket, svc])
            await session.commit()

            stats = await safe_backfill_owner_principal_ids(session)
            await session.commit()
            await session.refresh(customer)
            await session.refresh(order)
            await session.refresh(ticket)
            await session.refresh(svc)

            self.assertEqual(customer.owner_principal_id, principal_a.id)
            self.assertEqual(order.owner_principal_id, principal_a.id)
            self.assertEqual(ticket.owner_principal_id, principal_a.id)
            self.assertEqual(svc.owner_principal_id, principal_a.id)
            self.assertGreaterEqual(stats["bot_users"], 1)
            self.assertGreaterEqual(stats["orders"], 1)
            self.assertGreaterEqual(stats["tickets"], 1)
            self.assertGreaterEqual(stats["user_services"], 1)

            # Idempotent — no overwrite / no invent on second pass
            stats2 = await safe_backfill_owner_principal_ids(session)
            self.assertEqual(stats2["bot_users"], 0)
            self.assertEqual(stats2["orders"], 0)

    async def test_h_child_principals_same_reseller_stay_isolated(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            shop = await self._user(session, tg=3000)
            a = await op.create_principal(
                session, parent_id=owner.id, depth=1, bot_user_id=shop.id
            )
            a1 = await op.create_principal(session, parent_id=a.id, depth=2)
            a2 = await op.create_principal(session, parent_id=a.id, depth=2)
            # Explicit child ownership (provisioning later); same reseller_id
            u1 = await self._user(session, tg=3001, reseller_id=shop.id)
            u2 = await self._user(session, tg=3002, reseller_id=shop.id)
            u1.owner_principal_id = a1.id
            u2.owner_principal_id = a2.id
            await session.commit()

            r1 = resolve_resource_principal(u1)
            r2 = resolve_resource_principal(u2)
            self.assertEqual(r1.principal_id, a1.id)
            self.assertEqual(r2.principal_id, a2.id)

            all_p = [owner, a, a1, a2]
            vis_a1 = scope_ids_for_principal(a1, all_principals=all_p)
            vis_a2 = scope_ids_for_principal(a2, all_principals=all_p)
            self.assertTrue(
                resource_in_principal_scope(
                    actor_visible_principal_ids=vis_a1, resolution=r1
                )
            )
            self.assertFalse(
                resource_in_principal_scope(
                    actor_visible_principal_ids=vis_a1, resolution=r2
                )
            )
            self.assertFalse(
                resource_in_principal_scope(
                    actor_visible_principal_ids=vis_a2, resolution=r1
                )
            )

    async def test_models_have_owner_principal_column(self) -> None:
        for model in (BotUser, Order, Ticket, UserService):
            self.assertTrue(hasattr(model, "owner_principal_id"))
        # Intentionally unchanged
        from app.db.models import Payment, PanelTicket, Plan, WalletTransaction

        for model in (Payment, PanelTicket, Plan, WalletTransaction):
            self.assertFalse(hasattr(model, "owner_principal_id"))


if __name__ == "__main__":
    unittest.main()
