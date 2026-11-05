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
    DAYS_PER_EXTRA_MONTH,
    apply_plan_extra_rates_from_form,
    create_addon_order,
    list_packs_for_plan,
    list_packs_for_service,
    pack_matches_service,
    pack_purchase_button_text,
    plan_duration_months_for_form,
    replace_plan_scoped_packs_from_form,
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
        self.assertFalse(
            pack_matches_service(
                pack,
                service_plan_id=1,
                service_category_id=None,
                plan_catalog_exclusive=True,
            )
        )

    def test_purchase_button_skips_redundant_name(self) -> None:
        pack = ServiceAddonPack(
            name="۱۰ گیگ", kind="volume", amount=10, price=1, is_general=True
        )
        text = pack_purchase_button_text(pack, price_text="۱٬۰۰۰ تومان")
        self.assertEqual(text, "📦 10 گیگ — ۱٬۰۰۰ تومان")
        self.assertEqual(text.count("گیگ"), 1)
        named = ServiceAddonPack(
            name="بسته ویژه", kind="volume", amount=10, price=1, is_general=True
        )
        named_text = pack_purchase_button_text(named, price_text="۱٬۰۰۰ تومان")
        self.assertIn("بسته ویژه", named_text)
        self.assertIn("10 گیگ", named_text)


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

    async def test_volume_scoped_plan_still_offers_general_duration(self) -> None:
        """Exclusivity is per kind: volume-only scoped rows must not hide duration."""
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=1, referral_code="a", wallet_balance=10_000),
                    Plan(id=1, name="A", price=1000, duration_days=30),
                    ServiceAddonPack(
                        id=1,
                        name="general-vol",
                        kind="volume",
                        amount=5,
                        price=800,
                        is_general=True,
                    ),
                    ServiceAddonPack(
                        id=2,
                        name="general-time",
                        kind="duration",
                        amount=7,
                        price=700,
                        is_general=True,
                    ),
                    ServiceAddonPack(
                        id=3,
                        name="plan-vol",
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
                ]
            )
            await session.commit()
            service = await session.get(UserService, 10)
            vol = await list_packs_for_service(session, service, kind="volume")
            dur = await list_packs_for_service(session, service, kind="duration")
            self.assertEqual([p.name for p in vol], ["plan-vol"])
            self.assertEqual([p.name for p in dur], ["general-time"])
            all_packs = await list_packs_for_service(session, service)
            self.assertEqual([p.name for p in all_packs], ["plan-vol", "general-time"])

            general_vol = await session.get(ServiceAddonPack, 1)
            general_time = await session.get(ServiceAddonPack, 2)
            plan_vol = await session.get(ServiceAddonPack, 3)
            with self.assertRaises(ValueError):
                await create_addon_order(
                    session, user_id=1, service=service, pack=general_vol, commit=False
                )
            time_order = await create_addon_order(
                session, user_id=1, service=service, pack=general_time, commit=False
            )
            self.assertEqual(time_order.amount, 700)
            vol_order = await create_addon_order(
                session, user_id=1, service=service, pack=plan_vol, commit=False
            )
            self.assertEqual(vol_order.amount, 900)

    async def test_direct_general_pack_id_refused_when_kind_is_scoped(self) -> None:
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=1, referral_code="a", wallet_balance=10_000),
                    Plan(id=1, name="A", price=1000, duration_days=30),
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
                ]
            )
            await session.commit()
            service = await session.get(UserService, 10)
            general = await session.get(ServiceAddonPack, 1)
            plan_pack = await session.get(ServiceAddonPack, 2)
            with self.assertRaises(ValueError):
                await create_addon_order(
                    session, user_id=1, service=service, pack=general, commit=False
                )
            order = await create_addon_order(
                session, user_id=1, service=service, pack=plan_pack, commit=False
            )
            self.assertEqual(order.amount, 900)

    async def test_inactive_plan_packs_do_not_fall_back_to_general(self) -> None:
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=1, referral_code="a"),
                    Plan(id=1, name="A", price=1000, duration_days=30),
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
                        name="off",
                        kind="volume",
                        amount=10,
                        price=900,
                        plan_id=1,
                        is_general=False,
                        is_active=False,
                    ),
                    UserService(
                        id=10,
                        bot_user_id=1,
                        plan_id=1,
                        pg_username="u1",
                        pg_user_id=1,
                    ),
                ]
            )
            await session.commit()
            svc = await session.get(UserService, 10)
            packs = await list_packs_for_service(session, svc, kind="volume")
            self.assertEqual(packs, [])


class ReplacePlanScopedPacksFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.staff = {
            "role": "admin",
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
            "pg_is_owner": True,
        }

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_form_replace_prices_from_unit_rates(self) -> None:
        async with self.Session() as session:
            plan = Plan(
                id=7,
                name="P",
                price=1000,
                duration_days=30,
                allow_buy_extra=True,
                extra_gb_price=200,
                extra_month_price=1500,
            )
            session.add(plan)
            await session.commit()
            form = {
                "plan_vol_amount": ["5", "10"],
                "plan_vol_style": ["primary", "inherit"],
                "plan_dur_amount": ["1", "3"],
                "plan_dur_style": ["success", "danger"],
            }
            await replace_plan_scoped_packs_from_form(
                session, self.staff, plan_id=7, form=form
            )
            await session.commit()
            packs = await list_packs_for_plan(
                session, plan_id=7, reseller_id=None, active_only=False
            )
            vols = [p for p in packs if p.kind == "volume"]
            durs = [p for p in packs if p.kind == "duration"]
            self.assertEqual([(p.amount, p.price, p.button_style) for p in vols], [
                (5.0, 1000, "primary"),
                (10.0, 2000, None),
            ])
            self.assertEqual([(p.amount, p.price, p.button_style, p.name) for p in durs], [
                (float(DAYS_PER_EXTRA_MONTH), 1500, "success", "1 ماه"),
                (float(3 * DAYS_PER_EXTRA_MONTH), 4500, "danger", "3 ماه"),
            ])
            self.assertEqual(plan_duration_months_for_form(durs[1]), 3)
            self.assertTrue(all(p.plan_id == 7 and not p.is_general for p in packs))

    async def test_form_replace_clears_when_switch_off_and_rejects_other_shop(self) -> None:
        async with self.Session() as session:
            session.add_all(
                [
                    Plan(
                        id=1,
                        name="Mine",
                        price=1000,
                        duration_days=30,
                        allow_buy_extra=False,
                    ),
                    Plan(
                        id=2,
                        name="Other",
                        price=1000,
                        duration_days=30,
                        owner_reseller_id=99,
                        allow_buy_extra=True,
                        extra_gb_price=100,
                    ),
                    ServiceAddonPack(
                        id=1,
                        name="old",
                        kind="volume",
                        amount=1,
                        price=100,
                        plan_id=1,
                        is_general=False,
                    ),
                ]
            )
            await session.commit()
            await replace_plan_scoped_packs_from_form(
                session,
                self.staff,
                plan_id=1,
                form={"plan_vol_amount": ["5"], "plan_vol_style": ["inherit"]},
            )
            await session.flush()
            left = await list_packs_for_plan(
                session, plan_id=1, reseller_id=None, active_only=False
            )
            self.assertEqual(left, [])
            with self.assertRaises(ValueError):
                await replace_plan_scoped_packs_from_form(
                    session,
                    self.staff,
                    plan_id=2,
                    form={"plan_vol_amount": ["1"], "plan_vol_style": ["inherit"]},
                )

    def test_apply_plan_extra_rates_from_form(self) -> None:
        plan = Plan(name="P", price=1, duration_days=30)
        apply_plan_extra_rates_from_form(
            plan,
            {
                "allow_buy_extra": "on",
                "extra_gb_price": "1200",
                "extra_month_price": "45000",
            },
        )
        self.assertTrue(plan.allow_buy_extra)
        self.assertEqual(plan.extra_gb_price, 1200)
        self.assertEqual(plan.extra_month_price, 45000)
        apply_plan_extra_rates_from_form(plan, {})
        self.assertFalse(plan.allow_buy_extra)
        self.assertEqual(plan.extra_gb_price, 0)
        self.assertEqual(plan.extra_month_price, 0)


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

    def test_customer_extra_rates_upgrade_twice_and_downgrade(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "plan_customer_extra_rates",
            ROOT / "alembic/versions/0090_plan_customer_extra_rates.py",
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
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
                    migration.upgrade()
                    cols = {c["name"] for c in sa.inspect(conn).get_columns("plans")}
                    self.assertIn("allow_buy_extra", cols)
                    self.assertIn("extra_gb_price", cols)
                    self.assertIn("extra_month_price", cols)
                    migration.downgrade()
                    cols = {c["name"] for c in sa.inspect(conn).get_columns("plans")}
                    self.assertNotIn("allow_buy_extra", cols)
                    self.assertNotIn("extra_gb_price", cols)
                    self.assertNotIn("extra_month_price", cols)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
