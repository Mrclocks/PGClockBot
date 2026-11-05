"""Per-plan reseller extra-pack stairs: isolation, validation, purchase whitelist, migration."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, ResellerPlan, ResellerPlanExtraPack, ResellerProfile
from app.services.reseller_capacity import unit_offer, unit_offer_for_plan
from app.services.reseller_plan_extra_packs import (
    DEFAULT_EXTRA_GB_AMOUNTS,
    DEFAULT_EXTRA_USER_AMOUNTS,
    KIND_GB,
    KIND_USERS,
    active_amounts_for_plan,
    ensure_default_packs,
    get_pack_for_plan,
    normalize_amount_list,
    parse_pack_amount,
    replace_kind_packs,
    resolve_extra_pack_button_style,
)

ROOT = Path(__file__).resolve().parents[1]


class ParseHelpersTests(unittest.TestCase):
    def test_amount_rejects_bool_and_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_pack_amount(KIND_GB, True)
        with self.assertRaises(ValueError):
            parse_pack_amount(KIND_GB, 0)
        with self.assertRaises(ValueError):
            parse_pack_amount(KIND_GB, 100_001)
        self.assertEqual(parse_pack_amount(KIND_USERS, "20"), 20)

    def test_normalize_dedupes_and_caps(self) -> None:
        self.assertEqual(normalize_amount_list(KIND_GB, [10, 5, 10, 1]), [10, 5, 1])
        with self.assertRaises(ValueError):
            normalize_amount_list(KIND_GB, list(range(1, 25)))

    def test_button_style_inherit_and_colors(self) -> None:
        from app.services.reseller_plan_extra_packs import parse_pack_button_style

        self.assertIsNone(parse_pack_button_style("inherit"))
        self.assertEqual(parse_pack_button_style(""), "")
        self.assertEqual(parse_pack_button_style("primary"), "primary")
        plan = MagicMock(button_style="success", category=None)
        self.assertEqual(resolve_extra_pack_button_style("danger", plan), "danger")
        self.assertEqual(resolve_extra_pack_button_style(None, plan), "success")


class ExtraPackServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _two_plans(self, session):
        a = ResellerPlan(
            name="A",
            price=1000,
            allow_buy_extra=True,
            extra_gb_price=1000,
            extra_user_price=2000,
            plan_kind="subscription",
            billing_mode="fixed",
        )
        b = ResellerPlan(
            name="B",
            price=1000,
            allow_buy_extra=True,
            extra_gb_price=1000,
            extra_user_price=2000,
            plan_kind="subscription",
            billing_mode="fixed",
        )
        session.add_all([a, b])
        await session.flush()
        return a, b

    async def test_packs_do_not_leak_across_plans(self) -> None:
        async with self.Session() as session:
            a, b = await self._two_plans(session)
            await replace_kind_packs(session, a, KIND_GB, [7, 14])
            await replace_kind_packs(session, b, KIND_GB, [3])
            await session.commit()
            self.assertEqual(
                await active_amounts_for_plan(session, int(a.id), KIND_GB), frozenset({7, 14})
            )
            self.assertEqual(
                await active_amounts_for_plan(session, int(b.id), KIND_GB), frozenset({3})
            )
            pack_a = (await session.scalars(
                sa.select(ResellerPlanExtraPack).where(ResellerPlanExtraPack.plan_id == a.id)
            )).first()
            assert pack_a is not None
            self.assertIsNone(
                await get_pack_for_plan(session, plan_id=int(b.id), pack_id=int(pack_a.id))
            )
            self.assertIsNotNone(
                await get_pack_for_plan(session, plan_id=int(a.id), pack_id=int(pack_a.id))
            )

    async def test_legacy_fallback_then_explicit_empty(self) -> None:
        async with self.Session() as session:
            a, _b = await self._two_plans(session)
            await session.commit()
            self.assertEqual(
                await active_amounts_for_plan(session, int(a.id), KIND_GB),
                frozenset(DEFAULT_EXTRA_GB_AMOUNTS),
            )
            await ensure_default_packs(session, a)
            await session.commit()
            self.assertEqual(
                await active_amounts_for_plan(session, int(a.id), KIND_USERS),
                frozenset(DEFAULT_EXTRA_USER_AMOUNTS),
            )
            await replace_kind_packs(session, a, KIND_GB, [])
            await session.commit()
            self.assertEqual(await active_amounts_for_plan(session, int(a.id), KIND_GB), frozenset())

    async def test_unit_offer_uses_plan_whitelist(self) -> None:
        async with self.Session() as session:
            a, _b = await self._two_plans(session)
            await replace_kind_packs(session, a, KIND_GB, [25])
            await session.commit()
            profile = ResellerProfile(
                user_id=1,
                pg_admin_username="shop_a",
                plan_id=int(a.id),
                billing_mode="fixed",
            )
            offer = await unit_offer_for_plan(session, profile, a, KIND_GB, 25)
            self.assertEqual(offer.gb, 25)
            self.assertEqual(offer.price, 25_000)
            with self.assertRaises(ValueError):
                await unit_offer_for_plan(session, profile, a, KIND_GB, 10)
            # Sync path without allowed_amounts still rejects non-defaults.
            with self.assertRaises(ValueError):
                unit_offer(profile, a, KIND_GB, 25)


class ExtraPackFormApplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_apply_form_replaces_packs_with_styles(self) -> None:
        from app.services.reseller_plan_extra_packs import apply_extra_packs_from_form, list_packs

        class Form(dict):
            def getlist(self, key):
                val = self.get(key, [])
                return list(val) if isinstance(val, (list, tuple)) else [val]

        async with self.Session() as session:
            plan = ResellerPlan(
                name="F",
                price=1,
                allow_buy_extra=True,
                extra_gb_price=100,
                plan_kind="subscription",
                billing_mode="fixed",
            )
            session.add(plan)
            await session.flush()
            form = Form(
                {
                    "extra_gb_amount": ["2", "8"],
                    "extra_gb_style": ["primary", "danger"],
                    "extra_user_amount": ["4"],
                    "extra_user_style": ["inherit"],
                }
            )
            await apply_extra_packs_from_form(session, plan, form)
            await session.commit()
            gb = await list_packs(session, int(plan.id), kind=KIND_GB, active_only=True)
            users = await list_packs(session, int(plan.id), kind=KIND_USERS, active_only=True)
            self.assertEqual([(p.amount, p.button_style) for p in gb], [(2, "primary"), (8, "danger")])
            self.assertEqual([(p.amount, p.button_style) for p in users], [(4, None)])


class ExtraPackMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "extra_packs_mig", ROOT / "alembic/versions/0084_reseller_plan_extra_packs.py"
        )
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)

        with tempfile.TemporaryDirectory() as tmp:
            engine = sa.create_engine(f"sqlite:///{Path(tmp) / 'm.db'}")
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "CREATE TABLE reseller_plans ("
                        "id INTEGER PRIMARY KEY, name VARCHAR(128), allow_buy_extra BOOLEAN NOT NULL DEFAULT 0)"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO reseller_plans (id, name, allow_buy_extra) VALUES "
                        "(1, 'with', 1), (2, 'without', 0)"
                    )
                )
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
                    migration.upgrade()
                    names = set(sa.inspect(conn).get_table_names())
                    self.assertIn("reseller_plan_extra_packs", names)
                    rows = conn.execute(
                        sa.text(
                            "SELECT plan_id, kind, amount FROM reseller_plan_extra_packs "
                            "ORDER BY plan_id, kind, amount"
                        )
                    ).fetchall()
                    gb = {(r[0], r[2]) for r in rows if r[1] == "gb"}
                    users = {(r[0], r[2]) for r in rows if r[1] == "users"}
                    self.assertEqual(gb, {(1, n) for n in DEFAULT_EXTRA_GB_AMOUNTS})
                    self.assertEqual(users, {(1, n) for n in DEFAULT_EXTRA_USER_AMOUNTS})
                    self.assertFalse(any(r[0] == 2 for r in rows))
                    migration.downgrade()
                    self.assertNotIn(
                        "reseller_plan_extra_packs", set(sa.inspect(conn).get_table_names())
                    )
                    self.assertEqual(
                        conn.execute(sa.text("SELECT COUNT(*) FROM reseller_plans")).scalar(), 2
                    )
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
