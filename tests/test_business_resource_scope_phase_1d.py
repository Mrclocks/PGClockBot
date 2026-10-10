"""Phase 1D — business resource object scope (H2 + H3).

Ownership uses BotUser.reseller_id / Order.reseller_id only — never PG role names
and never invented owner_principal_id.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.shop_scope import (
    ShopScopeError,
    assert_bot_user_in_scope,
    assert_order_retry_in_scope,
    bot_user_in_scope,
    resolve_shop_scope_id,
)


def _find_route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method.upper() in (
            getattr(route, "methods", None) or set()
        ):
            return route
    raise AssertionError(f"route not found: {method} {path}")


class BusinessScopePureTests(unittest.TestCase):
    def _owner(self, **extra):
        staff = {
            "role": "admin",
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
        }
        staff.update(extra)
        return staff

    def test_a_principal_sees_own_user(self) -> None:
        staff = {"role": "reseller", "bot_user_id": 10}
        user = SimpleNamespace(id=1, reseller_id=10)
        self.assertTrue(bot_user_in_scope(staff, user))
        assert_bot_user_in_scope(staff, user)

    def test_b_cannot_access_sibling_user(self) -> None:
        staff_a = {"role": "reseller", "bot_user_id": 10}
        user_b = SimpleNamespace(id=2, reseller_id=20)
        self.assertFalse(bot_user_in_scope(staff_a, user_b))
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff_a, user_b)

    def test_c_modify_denied_same_as_read(self) -> None:
        # Mutation uses the same assert — ID-only change cannot cross shops.
        staff = {"role": "reseller", "bot_user_id": 10}
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff, SimpleNamespace(reseller_id=99))

    def test_d_loyalty_target_other_branch_denied(self) -> None:
        owner = self._owner()
        tenant_user = SimpleNamespace(id=5, reseller_id=42)
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(owner, tenant_user)

    def test_e_wallet_other_branch_denied(self) -> None:
        # Wallet credit targets BotUser — same ownership gate.
        staff = {"role": "reseller", "bot_user_id": 10}
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff, SimpleNamespace(reseller_id=11))

    def test_f_unknown_ownership_deny(self) -> None:
        staff = {"role": "reseller", "bot_user_id": 10}
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff, SimpleNamespace())  # no reseller_id
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff, None)
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff, SimpleNamespace(reseller_id="bad"))

    def test_g_order_accessible_to_owner_shop(self) -> None:
        staff = {"role": "reseller", "bot_user_id": 10}
        assert_order_retry_in_scope(staff, SimpleNamespace(reseller_id=10))

    def test_h_order_b_not_retryable_by_a(self) -> None:
        staff_a = {"role": "reseller", "bot_user_id": 10}
        with self.assertRaises(ShopScopeError):
            assert_order_retry_in_scope(staff_a, SimpleNamespace(reseller_id=20))

    def test_i_order_id_tamper_sibling(self) -> None:
        staff = {"role": "reseller", "bot_user_id": 10}
        with self.assertRaises(ShopScopeError):
            assert_order_retry_in_scope(staff, SimpleNamespace(reseller_id=999))

    def test_j_owner_visibility_platform_only(self) -> None:
        owner = self._owner()
        assert_bot_user_in_scope(owner, SimpleNamespace(reseller_id=None))
        assert_order_retry_in_scope(owner, SimpleNamespace(reseller_id=None))

    def test_k_owner_mutation_local_safety_blocks_tenant(self) -> None:
        owner = self._owner()
        with self.assertRaises(ShopScopeError):
            assert_order_retry_in_scope(owner, SimpleNamespace(reseller_id=5))
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(owner, SimpleNamespace(reseller_id=5))

    def test_m_pg_role_names_irrelevant(self) -> None:
        for role_name in ("Operator", "admin", "whatever", "RoleX"):
            staff = {
                "role": "reseller",
                "bot_user_id": 10,
                "pg_role_name": role_name,
            }
            self.assertTrue(
                bot_user_in_scope(staff, SimpleNamespace(reseller_id=10))
            )
            self.assertFalse(
                bot_user_in_scope(staff, SimpleNamespace(reseller_id=11))
            )

    def test_n_same_pg_role_isolated_by_shop(self) -> None:
        a = {"role": "reseller", "bot_user_id": 10, "pg_role_name": "RoleX"}
        b = {"role": "reseller", "bot_user_id": 20, "pg_role_name": "RoleX"}
        ua = SimpleNamespace(reseller_id=10)
        ub = SimpleNamespace(reseller_id=20)
        self.assertTrue(bot_user_in_scope(a, ua))
        self.assertFalse(bot_user_in_scope(a, ub))
        self.assertTrue(bot_user_in_scope(b, ub))
        self.assertFalse(bot_user_in_scope(b, ua))

    def test_missing_ownership_never_global(self) -> None:
        # pg_staff must not resolve to platform shop scope.
        with self.assertRaises(ShopScopeError):
            resolve_shop_scope_id({"role": "pg_staff", "bot_user_id": 1})
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(
                {"role": "pg_staff"}, SimpleNamespace(reseller_id=None)
            )


class LoyaltyShopScopeTests(unittest.TestCase):
    def test_pg_staff_cannot_fall_through_to_platform(self) -> None:
        from app.api.loyalty_pages import _shop_scope

        owner = {
            "role": "admin",
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
        }
        self.assertIsNone(_shop_scope(owner))
        with self.assertRaises(ValueError):
            _shop_scope({"role": "admin"})  # bare role ≠ platform
        self.assertEqual(_shop_scope({"role": "reseller", "bot_user_id": 42}), 42)
        with self.assertRaises(ValueError):
            _shop_scope({"role": "pg_staff"})
        with self.assertRaises(ValueError):
            _shop_scope({"role": "reseller"})


class RetryDeliveryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "1d.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        from app.api.ux20_pages import register_ux20_pages

        self.app = FastAPI()
        register_ux20_pages(
            self.app,
            render=lambda *a, **k: None,
            require_staff=lambda: None,
            require_admin=lambda: None,
            get_db=lambda: None,
        )
        self.retry_route = _find_route(
            self.app, "/orders/{order_id}/retry-delivery", "POST"
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _order(self, session, *, reseller_id: int | None):
        from app.db.models import Order, OrderStatus

        row = Order(
            user_id=1,
            plan_id=1,
            amount=1000,
            status=OrderStatus.PAID.value,
            reseller_id=reseller_id,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    async def test_reseller_retries_own_order(self) -> None:
        async with self.Session() as session:
            order = await self._order(session, reseller_id=10)
            staff = {
                "role": "reseller",
                "bot_user_id": 10,
                "permissions": ["orders"],
            }
            with patch(
                "app.api.ux20_pages.retry_delivery", new=AsyncMock()
            ) as retry:
                resp = await self.retry_route.endpoint(
                    order_id=order.id, staff=staff, session=session
                )
            self.assertEqual(resp.status_code, 303)
            self.assertIn("ok=", resp.headers.get("location", ""))
            retry.assert_awaited_once()

    async def test_reseller_cannot_retry_sibling_order(self) -> None:
        async with self.Session() as session:
            order = await self._order(session, reseller_id=20)
            staff = {
                "role": "reseller",
                "bot_user_id": 10,
                "permissions": ["orders"],
            }
            with patch(
                "app.api.ux20_pages.retry_delivery", new=AsyncMock()
            ) as retry:
                resp = await self.retry_route.endpoint(
                    order_id=order.id, staff=staff, session=session
                )
            self.assertEqual(resp.status_code, 303)
            loc = resp.headers.get("location", "")
            self.assertIn("err=", loc)
            retry.assert_not_awaited()

    async def test_owner_cannot_retry_tenant_order(self) -> None:
        async with self.Session() as session:
            order = await self._order(session, reseller_id=10)
            staff = {
                "role": "admin",
                "permissions": ["orders"],
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
            }
            with patch(
                "app.api.ux20_pages.retry_delivery", new=AsyncMock()
            ) as retry:
                resp = await self.retry_route.endpoint(
                    order_id=order.id, staff=staff, session=session
                )
            self.assertEqual(resp.status_code, 303)
            self.assertIn("err=", resp.headers.get("location", ""))
            retry.assert_not_awaited()

    async def test_owner_can_retry_platform_order(self) -> None:
        async with self.Session() as session:
            order = await self._order(session, reseller_id=None)
            staff = {
                "role": "admin",
                "permissions": ["orders"],
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
            }
            with patch(
                "app.api.ux20_pages.retry_delivery", new=AsyncMock()
            ) as retry:
                resp = await self.retry_route.endpoint(
                    order_id=order.id, staff=staff, session=session
                )
            self.assertEqual(resp.status_code, 303)
            self.assertIn("ok=", resp.headers.get("location", ""))
            retry.assert_awaited_once()


class UserWalletRouteScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "1d-users.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        from app.api.user_pages import register_user_pages

        self.app = FastAPI()
        register_user_pages(
            self.app,
            render=lambda *a, **k: None,
            require_admin=lambda: None,
            get_db=lambda: None,
        )
        self.credit_route = _find_route(
            self.app, "/users/{user_id}/wallet-credit", "POST"
        )
        self.edit_route = _find_route(self.app, "/users/{user_id}/edit", "GET")

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, *, reseller_id: int | None, telegram_id: int):
        from app.db.models import BotUser, Role

        row = BotUser(
            telegram_id=telegram_id,
            role=Role.USER.value,
            reseller_id=reseller_id,
            wallet_balance=0,
            referral_code=f"ref{telegram_id}",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    async def test_owner_can_read_platform_user(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, reseller_id=None, telegram_id=101)
            staff = {
                "role": "admin",
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
            }
            with (
                patch(
                    "app.services.bot_user_admin.list_service_snapshots",
                    new=AsyncMock(return_value=[]),
                ),
                patch(
                    "app.services.bot_user_admin.list_wallet_txs",
                    new=AsyncMock(return_value=[]),
                ),
            ):
                # render returns None — endpoint still returns that after scope pass
                from starlette.requests import Request

                scope = {
                    "type": "http",
                    "method": "GET",
                    "path": f"/users/{user.id}/edit",
                    "headers": [],
                    "query_string": b"",
                }
                request = Request(scope)
                resp = await self.edit_route.endpoint(
                    user_id=user.id,
                    request=request,
                    staff=staff,
                    session=session,
                )
            # Redirect would mean deny; None means render() ran (in scope).
            self.assertIsNone(resp)

    async def test_owner_cannot_credit_tenant_wallet(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, reseller_id=55, telegram_id=202)
            staff = {
                "role": "admin",
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
            }

            class _Form(dict):
                def get(self, k, default=None):
                    return dict.get(self, k, default)

            request = AsyncMock()
            request.form = AsyncMock(return_value=_Form(amount="1000", note="x"))
            with patch(
                "app.services.bot_user_admin.admin_adjust_user_wallet",
                new=AsyncMock(),
            ) as adjust:
                resp = await self.credit_route.endpoint(
                    user_id=user.id,
                    request=request,
                    staff=staff,
                    session=session,
                )
            self.assertEqual(resp.status_code, 303)
            self.assertIn("err=", resp.headers.get("location", ""))
            adjust.assert_not_awaited()


class LoyaltyAdjustRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "1d-loyalty.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        from app.api.loyalty_pages import register_loyalty_pages

        self.app = FastAPI()
        register_loyalty_pages(
            self.app,
            render=lambda *a, **k: None,
            require_perm=lambda _p: (lambda: None),
            require_admin=lambda: None,
            get_db=lambda: None,
        )
        self.adjust_route = _find_route(self.app, "/loyalty/adjust", "POST")

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_adjust_other_branch_denied(self) -> None:
        from app.db.models import BotUser, Role

        async with self.Session() as session:
            user = BotUser(
                telegram_id=303,
                role=Role.USER.value,
                reseller_id=77,
                wallet_balance=0,
                referral_code="ref303",
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            staff = {
                "role": "admin",
                "username": "owner",
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
            }
            with patch(
                "app.api.loyalty_pages.admin_adjust_points", new=AsyncMock()
            ) as adj:
                resp = await self.adjust_route.endpoint(
                    staff=staff,
                    session=session,
                    user_id=user.id,
                    delta=10,
                    reason="test",
                )
            self.assertEqual(resp.status_code, 303)
            self.assertIn("err=", resp.headers.get("location", ""))
            adj.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
