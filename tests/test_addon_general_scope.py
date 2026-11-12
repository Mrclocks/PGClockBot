"""General add-on packs fit every plan category; category packs stay category-only."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import BotUser, Plan, PlanCategory, ServiceAddonPack, UserService
from app.services.plan_categories import delete_category
from app.services.service_addons import (
    create_addon_order,
    create_pack,
    list_packs_for_service,
    update_pack,
)
from app.services.shop_scope import ShopScopeError
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

ROOT = Path(__file__).resolve().parents[1]
OWNER = {
    "role": "admin",
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}


class AddonCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._shop_token = set_shop_reseller_id(None)
        self.addCleanup(reset_shop_reseller_id, self._shop_token)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
                BotUser(id=2, telegram_id=102, referral_code="u2", role="user"),
                BotUser(id=5, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
                PlanCategory(id=1, name="VIP", audience="users"),
                PlanCategory(id=2, name="Eco", audience="users"),
                Plan(id=1, name="vip", price=1, category_id=1),
                Plan(id=2, name="eco", price=1, category_id=2),
                Plan(id=3, name="bare", price=1),
                UserService(id=10, bot_user_id=1, plan_id=1, pg_user_id=1, pg_username="vip-svc"),
                UserService(id=11, bot_user_id=1, plan_id=3, pg_user_id=2, pg_username="bare-svc"),
                UserService(id=12, bot_user_id=6, plan_id=1, pg_user_id=3, pg_username="shop-svc"),
                ServiceAddonPack(id=1, name="G5", kind="volume", amount=5, price=800, is_general=True),
                ServiceAddonPack(id=2, name="G10", kind="volume", amount=10, price=1000, is_general=True),
                ServiceAddonPack(id=3, name="VIP10", kind="volume", amount=10, price=3000, category_id=1, is_general=False),
                ServiceAddonPack(id=4, name="ECO5", kind="volume", amount=5, price=500, category_id=2, is_general=False),
                ServiceAddonPack(id=5, name="D7", kind="duration", amount=7, price=700, is_general=True),
                ServiceAddonPack(id=6, name="OFF", kind="volume", amount=1, price=1, is_general=True, is_active=False),
                ServiceAddonPack(id=7, name="SHOP10", kind="volume", amount=10, price=9000, owner_reseller_id=5, is_general=True),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def names(self, service_id: int, kind: str | None = None, shop_id: int | None = None):
        token = set_shop_reseller_id(shop_id)
        try:
            async with self.Session() as session:
                service = await session.get(UserService, service_id)
                return [p.name for p in await list_packs_for_service(session, service, kind=kind)]
        finally:
            reset_shop_reseller_id(token)


class VisibilityTests(AddonCase):
    async def test_general_packs_reach_every_category_and_uncategorized_services(self):
        self.assertEqual(await self.names(11, "volume"), ["G5", "G10"])
        self.assertEqual(await self.names(10, "duration"), ["D7"])

    async def test_a_category_pack_beats_a_general_pack_of_the_same_size(self):
        self.assertEqual(await self.names(10, "volume"), ["G5", "VIP10"])

    async def test_other_categories_and_inactive_packs_stay_hidden(self):
        for name in ("ECO5", "OFF"):
            self.assertNotIn(name, await self.names(10, "volume"))
            self.assertNotIn(name, await self.names(11, "volume"))

    async def test_a_shop_sees_only_its_own_packs(self):
        self.assertEqual(await self.names(12, "volume", shop_id=5), ["SHOP10"])
        self.assertNotIn("SHOP10", await self.names(10, "volume"))

    async def test_a_pack_without_a_category_is_offered_everywhere_even_without_the_flag(self):
        async with self.Session() as session:
            session.add(ServiceAddonPack(id=8, name="UNLABELED", kind="volume", amount=3, price=1, is_general=False))
            await session.commit()
        for service_id in (10, 11):
            self.assertIn("UNLABELED", await self.names(service_id, "volume"))


class PurchaseTests(AddonCase):
    async def test_an_unlabeled_pack_can_be_bought_for_a_categorized_service(self):
        async with self.Session() as session:
            session.add(ServiceAddonPack(id=8, name="UNLABELED", kind="volume", amount=3, price=77, is_general=False))
            await session.commit()
            service, pack = await session.get(UserService, 10), await session.get(ServiceAddonPack, 8)
            order = await create_addon_order(session, user_id=1, service=service, pack=pack)
            self.assertEqual(order.amount, 77)

    async def test_a_general_pack_can_be_bought_for_a_categorized_service(self):
        async with self.Session() as session:
            service, pack = await session.get(UserService, 10), await session.get(ServiceAddonPack, 2)
            order = await create_addon_order(session, user_id=1, service=service, pack=pack)
            self.assertEqual(order.amount, 1000)

    async def test_a_pack_of_another_category_is_still_refused(self):
        async with self.Session() as session:
            service, pack = await session.get(UserService, 10), await session.get(ServiceAddonPack, 4)
            with self.assertRaises(ValueError):
                await create_addon_order(session, user_id=1, service=service, pack=pack)

    async def test_a_category_pack_is_refused_for_an_uncategorized_service(self):
        async with self.Session() as session:
            service, pack = await session.get(UserService, 11), await session.get(ServiceAddonPack, 3)
            with self.assertRaises(ValueError):
                await create_addon_order(session, user_id=1, service=service, pack=pack)

    async def test_a_shop_pack_is_refused_for_a_platform_customer(self):
        async with self.Session() as session:
            service, pack = await session.get(UserService, 10), await session.get(ServiceAddonPack, 7)
            with self.assertRaises(ValueError):
                await create_addon_order(session, user_id=1, service=service, pack=pack)


class PackWriteTests(AddonCase):
    async def test_new_packs_default_to_general(self):
        async with self.Session() as session:
            pack = await create_pack(session, OWNER, name="New", kind="volume", amount=2, price=100)
            self.assertTrue(pack.is_general)
            self.assertIsNone(pack.category_id)
            blank = await create_pack(session, OWNER, name="Blank", kind="volume", amount=3, price=100, category_id="")
            self.assertTrue(blank.is_general)

    async def test_choosing_a_category_ties_the_pack_and_clearing_it_makes_it_general_again(self):
        async with self.Session() as session:
            pack = await create_pack(session, OWNER, name="Tied", kind="volume", amount=2, price=100, category_id="1")
            self.assertEqual((pack.is_general, pack.category_id), (False, 1))
            pack = await update_pack(session, OWNER, pack.id, category_id="")
            self.assertEqual((pack.is_general, pack.category_id), (True, None))
            pack = await update_pack(session, OWNER, pack.id, category_id="2")
            self.assertEqual((pack.is_general, pack.category_id), (False, 2))

    async def test_a_category_of_another_shop_cannot_be_chosen(self):
        async with self.Session() as session:
            session.add(PlanCategory(id=9, name="Shop cat", audience="users", owner_reseller_id=5))
            await session.commit()
            with self.assertRaises((ShopScopeError, ValueError)):
                await create_pack(session, OWNER, name="X", kind="volume", amount=2, price=1, category_id="9")


class CategoryDeleteTests(AddonCase):
    async def test_a_category_with_packs_cannot_be_deleted_and_nothing_changes(self):
        async with self.Session() as session:
            with self.assertRaises(ValueError) as caught:
                await delete_category(session, OWNER, 1)
            self.assertIn("بسته", str(caught.exception))
            await session.rollback()
            self.assertIsNotNone(await session.get(PlanCategory, 1))
            pack = await session.get(ServiceAddonPack, 3)
            self.assertEqual((pack.is_general, pack.category_id), (False, 1))
            self.assertEqual((await session.get(Plan, 1)).category_id, 1)

    async def test_the_category_can_be_deleted_once_its_packs_are_general_or_gone(self):
        async with self.Session() as session:
            await update_pack(session, OWNER, 3, category_id="")
            await delete_category(session, OWNER, 1)
            self.assertIsNone(await session.get(PlanCategory, 1))
            self.assertIsNone((await session.get(Plan, 1)).category_id)
            self.assertTrue((await session.get(ServiceAddonPack, 3)).is_general)

    async def test_a_pack_whose_category_vanished_underneath_it_is_shown_as_unlabeled(self):
        # The app refuses to delete a category that still has packs, which is what keeps a
        # category pack scoped. If the row disappears anyway (database-level SET NULL) the pack is
        # an unlabeled pack: shown everywhere rather than hidden from every customer.
        async with self.Session() as session:
            await session.execute(text("UPDATE service_addon_packs SET category_id = NULL WHERE id = 3"))
            await session.commit()
        for service_id in (10, 11):
            self.assertIn("VIP10", await self.names(service_id, "volume"))

    async def test_another_shops_packs_do_not_block_a_category(self):
        async with self.Session() as session:
            session.add(PlanCategory(id=3, name="Empty", audience="users"))
            session.add(ServiceAddonPack(id=9, name="Elsewhere", kind="volume", amount=1, price=1,
                                         owner_reseller_id=5, category_id=3, is_general=False))
            await session.commit()
            await delete_category(session, OWNER, 3)
            self.assertIsNone(await session.get(PlanCategory, 3))


class PlansPageTests(unittest.TestCase):
    """The plans page offers «عمومی», labels packs with it and blocks deleting a category in use."""

    def _html(self):
        from types import SimpleNamespace as NS

        from starlette.requests import Request

        from app.api.app import render

        request = Request({
            "type": "http", "asgi": {"version": "3"}, "http_version": "1.1", "method": "GET", "scheme": "https",
            "path": "/plans", "raw_path": b"/plans", "query_string": b"", "headers": [(b"host", b"x.test")],
            "client": ("127.0.0.1", 1), "server": ("x.test", 443), "state": {},
        })
        vip = NS(id=1, name="VIP", audience="users", is_active=True, sort_order=0, description="", button_style=None)
        eco = NS(id=2, name="Eco", audience="users", is_active=True, sort_order=0, description="", button_style=None)

        def pack(pid, name, category_id, general):
            return NS(id=pid, name=name, kind="volume", amount=5, price=1, description="", category_id=category_id,
                      is_general=general, is_active=True, sort_order=0, sale_price=1)

        ctx = {
            "staff": OWNER, "plans": [], "plan_categories": [vip, eco], "plan_category_map": {1: vip, 2: eco},
            "service_addon_packs": [pack(1, "GeneralPack", None, True), pack(2, "VipPack", 1, False)],
            "category_linked_plans": {}, "values": {}, "templates": [], "groups": [], "reseller_plans": [],
            "gift_codes": [], "is_platform_admin": True,
        }
        return render(request, "plans.html", ctx).body.decode("utf-8")

    def test_both_pack_forms_default_to_general(self):
        html = self._html()
        self.assertEqual(html.count("عمومی (همه‌ی دسته‌ها)"), 2)

    def test_pack_tables_label_the_scope(self):
        html = self._html()
        general_row = html.split("GeneralPack", 1)[1].split("</tr>", 1)[0]
        tied_row = html.split("VipPack", 1)[1].split("</tr>", 1)[0]
        self.assertIn("عمومی", general_row)
        self.assertIn("VIP", tied_row)
        self.assertNotIn("عمومی", tied_row)

    def test_a_category_in_use_cannot_be_deleted_from_the_page(self):
        html = self._html()
        self.assertNotIn('action="/plans/categories/1/delete"', html)
        self.assertIn('action="/plans/categories/2/delete"', html)
        self.assertIn("بسته‌ها: VipPack", html)


class MigrationTests(unittest.TestCase):
    def _migration(self):
        spec = importlib.util.spec_from_file_location("general_scope_migration", ROOT / "alembic/versions/0050_addon_pack_general_scope.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_existing_rows_keep_their_scope_and_the_migration_is_repeatable(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = self._migration()
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE service_addon_packs (id INTEGER PRIMARY KEY, name TEXT, category_id INTEGER)"))
            conn.execute(text("INSERT INTO service_addon_packs VALUES (1, 'plain', NULL), (2, 'vip', 7)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                rows = dict(conn.execute(text("SELECT name, is_general FROM service_addon_packs")).all())
                self.assertEqual(rows, {"plain": 1, "vip": 0})
                conn.execute(text("INSERT INTO service_addon_packs (id, name, category_id) VALUES (3, 'new', NULL)"))
                self.assertEqual(conn.execute(text("SELECT is_general FROM service_addon_packs WHERE id = 3")).scalar(), 1)
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM service_addon_packs")).scalar(), 3)
                self.assertNotIn("is_general", {c["name"] for c in sa.inspect(conn).get_columns("service_addon_packs")})
                migration.upgrade()
        engine.dispose()

    def test_missing_table_is_a_no_op(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = self._migration()
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.downgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
