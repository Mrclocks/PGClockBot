"""Gift codes need the ``gift_codes`` permission; ``orders`` or ``plans`` alone no longer reach them.

Also covers migration 0077, which gives ``gift_codes`` to every saved permission list that
held ``orders`` or ``plans`` so no shop loses access on upgrade.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.gift_codes import GIFT_CODES_PERMISSION
from app.services.resellers import DEFAULT_FEATURE_PERMS, FEATURE_PERMS, parse_perms

ROOT = Path(__file__).resolve().parents[1]
SHOP_A = 41
SHOP_B = 42


def _reseller(shop_id: int, perms: list[str]) -> dict:
    return {"role": "reseller", "bot_user_id": shop_id, "id": shop_id, "permissions": perms}


class GiftCodesKeyTests(unittest.TestCase):
    def test_key_is_a_shop_permission_and_a_default(self):
        self.assertIn(GIFT_CODES_PERMISSION, {key for key, _ in FEATURE_PERMS})
        self.assertIn(GIFT_CODES_PERMISSION, parse_perms(DEFAULT_FEATURE_PERMS))


class GiftCodeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'gifts.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.staff: dict = {}

        async def get_db():
            async with self.Session() as session:
                yield session

        from app.api.bulk_pages import register_bulk_pages
        from app.api.ux20_pages import register_ux20_pages

        app = FastAPI()
        register_ux20_pages(
            app, render=lambda *a, **k: None, require_staff=lambda: self.staff,
            require_admin=lambda: self.staff, get_db=get_db,
        )
        register_bulk_pages(
            app, require_perm=lambda _perm: (lambda: self.staff),
            require_pg_perm=lambda _perm: (lambda: self.staff), require_admin=lambda: self.staff,
            get_db=get_db, require_staff=lambda: self.staff,
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _code(self, *, shop_id: int | None, code: str, active: bool = True) -> int:
        from app.db.models import ChargeCode

        async with self.Session() as session:
            row = ChargeCode(code=code, amount=10_000, reseller_id=shop_id, is_active=active)
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _get(self, code_id: int):
        from app.db.models import ChargeCode

        async with self.Session() as session:
            return await session.get(ChargeCode, code_id)

    async def _codes(self) -> list:
        from app.db.models import ChargeCode

        async with self.Session() as session:
            return list((await session.scalars(select(ChargeCode))).all())

    async def test_orders_and_plans_alone_cannot_create_edit_or_toggle(self):
        code_id = await self._code(shop_id=SHOP_A, code="SHOPA1")
        self.staff = _reseller(SHOP_A, ["orders", "plans", "payments"])
        create = await self.client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "5000", "code": "NEWONE"})
        self.assertEqual(create.headers["location"], "/home")
        edit = await self.client.post(f"/plans/gift-codes/{code_id}/edit", data={"kind": "wallet", "amount": "999000"})
        self.assertEqual(edit.headers["location"], "/home")
        toggle = await self.client.post(f"/plans/gift-codes/{code_id}/toggle")
        self.assertEqual(toggle.headers["location"], "/home")
        await self.client.post("/plans/gift-codes/bulk-action", data={"action": "toggle", "ids": [str(code_id)]})
        row = await self._get(code_id)
        self.assertTrue(row.is_active)
        self.assertEqual(row.amount, 10_000)
        self.assertEqual([c.code for c in await self._codes()], ["SHOPA1"])

    async def test_gift_codes_key_manages_only_its_own_shop(self):
        own = await self._code(shop_id=SHOP_A, code="SHOPA2")
        other = await self._code(shop_id=SHOP_B, code="SHOPB2")
        platform = await self._code(shop_id=None, code="PLAT2")
        self.staff = _reseller(SHOP_A, [GIFT_CODES_PERMISSION])
        create = await self.client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "5000", "code": "MINTED"})
        self.assertIn("ok=", create.headers["location"])
        minted = [c for c in await self._codes() if c.code == "MINTED"]
        self.assertEqual([c.reseller_id for c in minted], [SHOP_A])
        for code_id in (own, other, platform):
            await self.client.post(f"/plans/gift-codes/{code_id}/toggle")
        await self.client.post("/plans/gift-codes/bulk-action", data={"action": "toggle", "ids": [str(other), str(platform)]})
        self.assertFalse((await self._get(own)).is_active)
        self.assertTrue((await self._get(other)).is_active)
        self.assertTrue((await self._get(platform)).is_active)

    async def test_staff_without_a_shop_never_mint_platform_codes(self):
        self.staff = {"role": "pg_staff", "permissions": [GIFT_CODES_PERMISSION]}
        await self.client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "5000", "code": "NOSHOP"})
        self.staff = _reseller(0, [GIFT_CODES_PERMISSION])
        await self.client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "5000", "code": "ZEROSHOP"})
        self.assertEqual(await self._codes(), [])


def _load_migration():
    spec = importlib.util.spec_from_file_location("gift_codes_permission", ROOT / "alembic/versions/0077_gift_codes_permission.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GiftCodesMigrationTests(unittest.TestCase):
    ROWS = {
        1: "dashboard,orders,plans",
        2: "dashboard,tickets",
        3: None,
        4: "",
        5: "plans,gift_codes",
        6: " orders ",
    }

    def _setup(self, conn) -> None:
        for table in ("reseller_profiles", "reseller_plans"):
            conn.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, web_permissions TEXT, bot_permissions TEXT)"))
            for row_id, perms in self.ROWS.items():
                conn.execute(
                    sa.text(f"INSERT INTO {table} (id, web_permissions, bot_permissions) VALUES (:id, :p, :p)"),
                    {"id": row_id, "p": perms},
                )

    def _rows(self, conn, table: str) -> dict:
        return {r.id: (r.web_permissions, r.bot_permissions) for r in conn.execute(sa.text(f"SELECT * FROM {table}"))}

    def test_upgrade_twice_then_downgrade(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            self._setup(conn)
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                expected = {
                    1: "dashboard,orders,plans,gift_codes",
                    2: "dashboard,tickets",
                    3: None,
                    4: "",
                    5: "plans,gift_codes",
                    6: "orders,gift_codes",
                }
                for table in ("reseller_profiles", "reseller_plans"):
                    self.assertEqual(self._rows(conn, table), {k: (v, v) for k, v in expected.items()})
                migration.downgrade()
                after = self._rows(conn, "reseller_profiles")
                self.assertEqual(after[1], ("dashboard,orders,plans", "dashboard,orders,plans"))
                self.assertEqual(after[5], ("plans", "plans"))
                self.assertEqual(after[3], (None, None))
                self.assertEqual(after[4], ("", ""))
                self.assertEqual(len(after), len(self.ROWS))
        engine.dispose()

    def test_missing_tables_are_skipped(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.downgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
