"""Discount codes limited to some of the shop's plans: rules, shop scope, page and routes."""

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

from app.db.models import Base, BotUser, ChargeCode, Order, Plan
from app.services.gift_codes import MAX_CODE_PLANS, describe_code, validate_rules
from app.services.orders import apply_discount, apply_discount_to_order
from app.services.ux20 import create_charge_code
from tests.test_gift_code_page import GiftCodeHarness, _reseller

SHOP, OTHER_SHOP = 3, 2
PLAN_50, PLAN_100, PLAN_OTHER, TRIAL, OTHER_SHOP_PLAN, PLATFORM_PLAN = 10, 11, 12, 13, 20, 30


class PlanLimitedCodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.tmp.name}/test.db")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=1, referral_code="one"),
                BotUser(id=OTHER_SHOP, telegram_id=2, referral_code="two"),
                BotUser(id=SHOP, telegram_id=3, referral_code="shop"),
                Plan(id=PLAN_50, name="50 گیگ", price=100_000, owner_reseller_id=SHOP),
                Plan(id=PLAN_100, name="100 گیگ", price=200_000, owner_reseller_id=SHOP),
                Plan(id=PLAN_OTHER, name="10 گیگ", price=50_000, owner_reseller_id=SHOP),
                Plan(id=TRIAL, name="تست", price=0, owner_reseller_id=SHOP, is_trial=True),
                Plan(id=OTHER_SHOP_PLAN, name="other", price=100_000, owner_reseller_id=OTHER_SHOP),
                Plan(id=PLATFORM_PLAN, name="platform", price=100_000),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.tmp.cleanup()

    async def order(self, session, *, plan_id: int | None, note: str | None = None) -> Order:
        row = Order(user_id=1, amount=100_000, reseller_id=SHOP, status="pending", plan_id=plan_id, note=note)
        session.add(row)
        await session.commit()
        return row

    async def code(self, session, **rules) -> ChargeCode:
        return await create_charge_code(
            session, kind="discount", percent=20, max_uses=None, reseller_id=SHOP, **rules
        )

    async def test_a_plan_limited_code_discounts_only_those_plans(self) -> None:
        async with self.Session() as session:
            code = await self.code(session, plan_ids=[str(PLAN_100), str(PLAN_50)])
            self.assertEqual(code.plan_ids, f"{PLAN_50},{PLAN_100}")
            for plan_id in (PLAN_50, PLAN_100):
                order = await apply_discount_to_order(session, await self.order(session, plan_id=plan_id), code.code)
                self.assertEqual(order.discount_amount, 20_000)
            other = await self.order(session, plan_id=PLAN_OTHER)
            with self.assertRaisesRegex(ValueError, "پلن"):
                await apply_discount_to_order(session, other, code.code)
            await session.commit()
            await session.refresh(code)
            self.assertEqual(code.used_count, 2, "a refused order burns no use")

    async def test_renewals_of_those_plans_count_but_add_ons_do_not(self) -> None:
        async with self.Session() as session:
            key = (await self.code(session, plan_ids=[str(PLAN_50)])).code
            renewal = await self.order(session, plan_id=PLAN_50, note="renew:1")
            self.assertEqual((await apply_discount_to_order(session, renewal, key)).discount_amount, 20_000)
            for plan_id, note in ((PLAN_OTHER, "renew:1"), (None, "svc_addon:1:1:volume:10")):
                order = await self.order(session, plan_id=plan_id, note=note)
                with self.subTest(note=note), self.assertRaises(ValueError):
                    await apply_discount_to_order(session, order, key)
                await session.commit()

    async def test_a_preview_without_an_order_promises_no_discount(self) -> None:
        async with self.Session() as session:
            code = await self.code(session, plan_ids=[str(PLAN_50)])
            self.assertEqual(await apply_discount(session, code.code, 100_000), (0, None))
            order = await self.order(session, plan_id=PLAN_50)
            self.assertEqual(await apply_discount(session, code.code, 100_000, order=order), (20_000, code.code))

    async def test_only_the_shops_own_sellable_plans_can_be_chosen(self) -> None:
        async with self.Session() as session:
            for plan_id in (OTHER_SHOP_PLAN, PLATFORM_PLAN, TRIAL, 999):
                with self.subTest(plan_id=plan_id), self.assertRaisesRegex(ValueError, "فروشگاه"):
                    await self.code(session, plan_ids=[str(PLAN_50), str(plan_id)])
            self.assertEqual(await session.scalar(sa.select(sa.func.count(ChargeCode.id))), 0)

    async def test_the_list_names_the_codes_plans(self) -> None:
        async with self.Session() as session:
            limited = await self.code(session, plan_ids=[str(PLAN_50), str(PLAN_100)])
            every = await self.code(session)
        names = {PLAN_50: "50 گیگ", PLAN_100: "100 گیگ"}
        self.assertEqual(describe_code(limited, names)["plans_label"], "50 گیگ، 100 گیگ")
        self.assertEqual(describe_code(every, names)["plans_label"], "همه پلن‌ها")
        self.assertEqual(describe_code(limited, {})["plans_label"], "پلن حذف‌شده")


class PlanLimitValidationTests(unittest.TestCase):
    def test_invalid_plan_limits_are_refused(self) -> None:
        for rules in (
            dict(kind="wallet", amount=100, plan_ids=["10"]),
            dict(kind="discount", percent=20, plan_ids=["abc"]),
            dict(kind="discount", percent=20, plan_ids=["0"]),
            dict(kind="discount", percent=20, plan_ids=["-5"]),
            dict(kind="discount", percent=20, plan_ids=[True]),
            dict(kind="discount", percent=20, plan_ids=[str(2**31)]),
            dict(kind="discount", percent=20, plan_ids=[str(i) for i in range(1, MAX_CODE_PLANS + 2)]),
            dict(kind="discount", percent=20, plan_ids=["10"], purchase_types=["volume", "duration"]),
        ):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                validate_rules(**rules)

    def test_plan_ids_are_stored_sorted_and_once(self) -> None:
        rules = validate_rules(kind="discount", percent=20, plan_ids=["11", " 10", "11", ""])
        self.assertEqual(rules["plan_ids"], "10,11")
        self.assertIsNone(validate_rules(kind="discount", percent=20)["plan_ids"])
        mixed = validate_rules(kind="discount", percent=20, plan_ids=["10"], purchase_types=["renew", "volume"])
        self.assertEqual(mixed["plan_ids"], "10")


class PlanLimitPageTests(GiftCodeHarness):
    """The page lists only the shop's own plans; the routes keep the limit inside the shop."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.app import render
        from app.api.ux20_pages import register_ux20_pages

        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=5, referral_code="s5"),
                BotUser(id=6, telegram_id=6, referral_code="s6"),
                Plan(id=PLAN_50, name="پلن <b>پنجاه</b>", price=100_000, owner_reseller_id=5),
                Plan(id=PLAN_100, name="پلن صد", price=200_000, owner_reseller_id=5, is_active=False),
                Plan(id=TRIAL, name="تست پنج", price=0, owner_reseller_id=5, is_trial=True),
                Plan(id=OTHER_SHOP_PLAN, name="پلن فروشگاه شش", price=100_000, owner_reseller_id=6),
            ])
            await session.commit()

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_ux20_pages(
            app, render=render, require_staff=lambda: self.staff, require_admin=lambda: self.staff, get_db=get_db
        )
        self.post_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.post_client.aclose()
        await super().asyncTearDown()

    async def post(self, path: str, data: dict) -> httpx.Response:
        self.staff.clear()
        self.staff.update(_reseller(["gift_codes"], shop_id=5))
        return await self.post_client.post(path, data=data)

    async def test_the_form_offers_the_shops_own_plans_only(self) -> None:
        page = (await self._get(_reseller(["gift_codes"], shop_id=5))).text
        self.assertIn('name="plan_ids" value="10"', page)
        self.assertIn("پلن صد (غیرفعال)", page)
        self.assertIn("&lt;b&gt;پنجاه&lt;/b&gt;", page, "plan names are escaped")
        for absent in ("پلن فروشگاه شش", "تست پنج"):
            self.assertNotIn(absent, page)

    async def test_create_and_edit_keep_the_limit_within_the_shop(self) -> None:
        created = await self.post("/plans/gift-codes", {
            "kind": "discount", "percent": "20", "code": "FIFTY20", "plan_ids": [str(PLAN_50)],
        })
        self.assertIn("ok=", created.headers["location"])
        async with self.Session() as session:
            code = (await session.execute(select(ChargeCode).where(ChargeCode.code == "FIFTY20"))).scalar_one()
        self.assertEqual(code.plan_ids, str(PLAN_50))
        refused = await self.post(f"/plans/gift-codes/{code.id}/edit", {
            "kind": "discount", "percent": "20", "plan_ids": [str(PLAN_50), str(OTHER_SHOP_PLAN)],
        })
        self.assertIn("err=", refused.headers["location"])
        cleared = await self.post(f"/plans/gift-codes/{code.id}/edit", {"kind": "discount", "percent": "20"})
        self.assertIn("ok=", cleared.headers["location"])
        async with self.Session() as session:
            self.assertIsNone((await session.get(ChargeCode, code.id)).plan_ids, "no plan chosen = every plan")
        other = await self.post("/plans/gift-codes", {
            "kind": "discount", "percent": "20", "code": "STEAL20", "plan_ids": [str(OTHER_SHOP_PLAN)],
        })
        self.assertIn("err=", other.headers["location"])


class PlanIdsMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_existing_codes(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0110_charge_code_plan_ids.py"
        spec = importlib.util.spec_from_file_location("charge_code_plan_ids_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE charge_codes (id INTEGER PRIMARY KEY, code VARCHAR(64))"))
            conn.execute(sa.text("INSERT INTO charge_codes (id, code) VALUES (1, 'KEEP')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIsNone(conn.execute(sa.text("SELECT plan_ids FROM charge_codes")).scalar())
                migration.downgrade()
                columns = {column["name"] for column in sa.inspect(conn).get_columns("charge_codes")}
                self.assertNotIn("plan_ids", columns)
                self.assertEqual(conn.execute(sa.text("SELECT code FROM charge_codes")).scalar(), "KEEP")
                migration.downgrade()
        engine.dispose()
