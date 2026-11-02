"""Regression test: gift/charge-code toggle must enforce shop authorization.

``POST /plans/gift-codes/{code_id}/toggle`` only had ``require_staff`` (any
authenticated web session) and used ``rid = None if is_platform_admin(staff)
else shop_owner_id(staff)``. For a scopeless non-admin (e.g. ``pg_staff``
who may only be permitted to see nodes in PasarGuard), ``rid`` is also
``None`` — and the endpoint's own guard only blocked touching a *reseller's*
codes (``row.reseller_id is not None``), not platform-level ones. So any
authenticated pg_staff could flip platform-wide gift/charge codes on or off
with a direct POST, regardless of their actual PasarGuard permissions. This
must never regress.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _find_route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method.upper() in (
            getattr(route, "methods", None) or set()
        ):
            return route
    raise AssertionError(f"route not found: {method} {path}")


class GiftCodeToggleAuthzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "gifts.db"
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
        self.toggle_route = _find_route(
            self.app, "/plans/gift-codes/{code_id}/toggle", "POST"
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _make_platform_code(self, session, *, active: bool = True):
        from app.db.models import ChargeCode

        row = ChargeCode(
            code="PLATFORM50",
            amount=50000,
            reseller_id=None,
            is_active=active,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    async def test_pg_staff_without_permission_cannot_toggle_platform_code(self):
        async with self.Session() as session:
            code = await self._make_platform_code(session, active=True)

            pg_staff = {
                "role": "pg_staff",
                "id": 1,
                "username": "nodes_only_admin",
                "permissions": [],  # no shop-level permission at all
            }
            resp = await self.toggle_route.endpoint(
                code_id=code.id, staff=pg_staff, session=session
            )
            self.assertEqual(resp.status_code, 303)

            await session.refresh(code)
            self.assertTrue(
                code.is_active,
                "a pg_staff with no shop permissions must not be able to toggle "
                "a platform-level gift code",
            )

    async def test_reseller_cannot_toggle_platform_code(self):
        async with self.Session() as session:
            code = await self._make_platform_code(session, active=True)

            reseller = {
                "role": "reseller",
                "bot_user_id": 42,
                "id": 42,
                "username": "shop_owner",
                "permissions": ["plans", "orders"],
            }
            await self.toggle_route.endpoint(
                code_id=code.id, staff=reseller, session=session
            )
            await session.refresh(code)
            self.assertTrue(
                code.is_active,
                "a reseller must not be able to toggle a platform-level gift code",
            )

    async def test_platform_admin_can_toggle_platform_code(self):
        async with self.Session() as session:
            code = await self._make_platform_code(session, active=True)

            admin = {
                "role": "admin",
                "id": 1,
                "username": "root",
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
            }
            await self.toggle_route.endpoint(
                code_id=code.id, staff=admin, session=session
            )
            await session.refresh(code)
            self.assertFalse(code.is_active)

    async def test_staff_with_shop_permission_can_toggle_own_code(self):
        from app.db.models import ChargeCode

        async with self.Session() as session:
            row = ChargeCode(
                code="SHOP50",
                amount=50000,
                reseller_id=42,
                is_active=True,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)

            reseller = {
                "role": "reseller",
                "bot_user_id": 42,
                "id": 42,
                "username": "shop_owner",
                "permissions": ["gift_codes"],
            }
            await self.toggle_route.endpoint(
                code_id=row.id, staff=reseller, session=session
            )
            await session.refresh(row)
            self.assertFalse(row.is_active)


if __name__ == "__main__":
    unittest.main()
