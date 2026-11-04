"""Customer service addon packs scoped to a single sales plan."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Plan, ServiceAddonPack, UserService
from app.services.service_addons import (
    list_packs_for_service,
    pack_matches_service,
)

ROOT = Path(__file__).resolve().parents[1]


class PackMatchTests(unittest.TestCase):
    def test_plan_pack_only_matches_same_plan(self) -> None:
        pack = ServiceAddonPack(
            name="P", kind="volume", amount=5, price=1, plan_id=10, is_general=False
        )
        self.assertTrue(
            pack_matches_service(pack, service_plan_id=10, service_category_id=None)
        )
        self.assertFalse(
            pack_matches_service(pack, service_plan_id=11, service_category_id=None)
        )
        self.assertFalse(
            pack_matches_service(pack, service_plan_id=None, service_category_id=1)
        )

    def test_general_pack_still_matches_category_path(self) -> None:
        pack = ServiceAddonPack(
            name="G", kind="volume", amount=5, price=1, plan_id=None, is_general=True
        )
        self.assertTrue(
            pack_matches_service(pack, service_plan_id=1, service_category_id=None)
        )


class PlanScopedListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_plan_packs_hide_general_for_that_plan_only(self) -> None:
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=1, referral_code="a"),
                    Plan(id=1, name="A", price=1000, duration_days=30),
                    Plan(id=2, name="B", price=1000, duration_days=30),
                    ServiceAddonPack(
                        id=1,
                        name="general",
                        kind="volume",
                        amount=5,
                        price=800,
                        is_general=True,
                    ),
                    ServiceAddonPack(
                        id=2,
                        name="only-a",
                        kind="volume",
                        amount=10,
                        price=900,
                        plan_id=1,
                        is_general=False,
                    ),
                    UserService(
                        id=10,
                        bot_user_id=1,
                        plan_id=1,
                        pg_username="u1",
                        pg_user_id=1,
                    ),
                    UserService(
                        id=11,
                        bot_user_id=1,
                        plan_id=2,
                        pg_username="u2",
                        pg_user_id=2,
                    ),
                ]
            )
            await session.commit()
            svc_a = await session.get(UserService, 10)
            svc_b = await session.get(UserService, 11)
            packs_a = await list_packs_for_service(session, svc_a)
            packs_b = await list_packs_for_service(session, svc_b)
            self.assertEqual([p.name for p in packs_a], ["only-a"])
            self.assertEqual([p.name for p in packs_b], ["general"])


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "addon_plan_scope",
            ROOT / "alembic/versions/0085_service_addon_plan_scope.py",
        )
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)

        with tempfile.TemporaryDirectory() as tmp:
            engine = sa.create_engine(f"sqlite:///{Path(tmp) / 'm.db'}")
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "CREATE TABLE plans (id INTEGER PRIMARY KEY, name VARCHAR(128))"
                    )
                )
                conn.execute(
                    sa.text(
                        "CREATE TABLE service_addon_packs ("
                        "id INTEGER PRIMARY KEY, name VARCHAR(128), kind VARCHAR(16), "
                        "amount FLOAT, price INTEGER, is_general BOOLEAN, is_active BOOLEAN)"
                    )
                )
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
                    migration.upgrade()
                    cols = {c["name"] for c in sa.inspect(conn).get_columns("service_addon_packs")}
                    self.assertIn("plan_id", cols)
                    self.assertIn("button_style", cols)
                    migration.downgrade()
                    cols = {c["name"] for c in sa.inspect(conn).get_columns("service_addon_packs")}
                    self.assertNotIn("plan_id", cols)
                    self.assertNotIn("button_style", cols)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
