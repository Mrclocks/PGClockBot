"""Catalog price quotes, checkout snapshots and shop announcement isolation."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import secrets
import tempfile
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import DictLoader, Environment, FileSystemLoader, ChoiceLoader, select_autoescape
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.api.catalog_discounts import register_catalog_discounts
from app.api.miniapp_feeds import catalog_plans_payload
from app.db.models import (
    Base, BotUser, CampaignRecipient, CatalogDiscount, DiscountCode, MarketingPreference,
    Order, Plan, PlanCategory, ServiceAddonPack, TargetedCampaign, UserService,
)
from app.services.campaigns import campaign_send_delay, process_campaign
from app.services.catalog_discounts import DiscountInput, create_discount, pause_expired_announcements, stop_discount
from app.services.catalog_pricing import PriceTarget, active_discounts, displayed_quote, price_html, quote_price, quote_product
from app.services.formatting import format_expire_short, format_money, format_number
from app.services.gift_codes import expiry_input
from app.services.orders import create_custom_order, create_order, create_wholesale_order, get_catalog_plan, renew_service_with_plan
from app.services.service_addons import create_addon_order
from app.services.service_automation import automation_settings
from app.services.checkout_summary import checkout_summary
from app.services.shop_scope import ShopScopeError
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
SHOP = {"role": "reseller", "bot_user_id": 2, "permissions": ["catalog_discounts"]}
OTHER_SHOP = {**SHOP, "bot_user_id": 3}


class ScriptSources(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        source = dict(attrs).get("src")
        if tag == "script" and source:
            self.sources.append(source)


class CatalogDiscountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        test_url = os.environ.get("PGCLOCKBOT_CATALOG_TEST_URL")
        self.admin_engine = None
        if test_url:
            self.schema = "catalog_test_" + secrets.token_hex(8)
            self.admin_engine = create_async_engine(test_url)
            async with self.admin_engine.begin() as connection:
                await connection.execute(CreateSchema(self.schema))
            self.engine = create_async_engine(test_url, connect_args={"server_settings": {"search_path": self.schema}})
        else:
            self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.tmp.name}/discounts.db")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if not test_url:
                await connection.execute(text("PRAGMA foreign_keys=ON"))
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.token = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        self.now = datetime.now(timezone.utc)
        self.session.add_all([
            BotUser(id=2, telegram_id=102, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=103, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.user = BotUser(id=1, telegram_id=101, referral_code="buyer", wallet_balance=80)
        self.session.add_all([
            self.user,
            BotUser(id=4, telegram_id=104, referral_code="buyer2", reseller_id=2),
            BotUser(id=5, telegram_id=105, referral_code="buyer3", reseller_id=3),
            BotUser(id=6, telegram_id=106, referral_code="optout", reseller_id=2),
            PlanCategory(id=1, name="VIP", audience="users"),
            PlanCategory(id=2, name="Shop VIP", owner_reseller_id=2, audience="users"),
        ])
        await self.session.flush()
        self.plan = Plan(id=1, name="Fixed", price=100, duration_days=30, data_limit_gb=10, category_id=1)
        self.pack = ServiceAddonPack(id=1, name="More", kind="volume", amount=2, price=100, category_id=1)
        self.session.add_all([
            self.plan, self.pack,
            Plan(id=2, name="Second", price=100, duration_days=30, data_limit_gb=10),
            Plan(id=3, name="Other shop", price=100, owner_reseller_id=2, category_id=2),
            Plan(id=4, name="Third shop", price=100, owner_reseller_id=3),
            Plan(id=5, name="Trial", price=0, is_trial=True),
            MarketingPreference(user_id=6, shop_key=2, enabled=False),
        ])
        await self.session.flush()
        self.service = UserService(id=1, bot_user_id=1, pg_user_id=10, pg_username="buyer", plan_id=1)
        self.session.add(self.service)
        if test_url:
            await self.session.execute(text("SELECT setval(pg_get_serial_sequence('plans', 'id'), (SELECT max(id) FROM plans))"))
        await self.session.commit()
        self.contact = patch("app.services.orders.require_purchase_contact", AsyncMock())
        self.contact.start()

    async def asyncTearDown(self) -> None:
        self.contact.stop()
        await self.session.close()
        await self.engine.dispose()
        if self.admin_engine:
            async with self.admin_engine.begin() as connection:
                await connection.execute(DropSchema(self.schema, cascade=True))
            await self.admin_engine.dispose()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()
        self.tmp.cleanup()

    def data(self, **kwargs: object) -> DiscountInput:
        return replace(DiscountInput(
            title="پیشنهاد ویژه", scope="all", mode="temporary", reduction_kind="percent",
            reduction_value=20, duration_hours=24, request_key=secrets.token_hex(16),
        ), **kwargs)

    async def create(self, staff: dict | None = None, **kwargs: object) -> CatalogDiscount:
        return await create_discount(self.session, staff or OWNER, self.data(**kwargs))

    async def test_each_scope_matches_only_its_products(self) -> None:
        targets = [PriceTarget(100, plan_id=1, category_id=1), PriceTarget(100, plan_id=2),
                   PriceTarget(100, kind="custom"), PriceTarget(100, kind="addon", category_id=1)]
        cases = [("plan", {"plan_id": 1}, [80, 100, 100, 100]),
                 ("category", {"category_id": 1}, [80, 100, 100, 80]),
                 ("fixed", {}, [80, 80, 100, 100]), ("all", {}, [80, 80, 80, 80])]
        for scope, kwargs, expected in cases:
            row = await self.create(scope=scope, notify=False, **kwargs)
            self.assertEqual([quote_price(target, [row]).price for target in targets], expected)
            await stop_discount(self.session, OWNER, row.id)

    async def test_permanent_price_has_no_strike_and_temporary_uses_new_base(self) -> None:
        permanent = await self.create(mode="permanent", duration_hours=None, reduction_value=10)
        quote = quote_price(PriceTarget(100), [permanent])
        self.assertEqual(quote.price, 90)
        self.assertIsNone(quote.original_price)
        self.assertNotIn("<s>", price_html(quote))
        temporary = await self.create(reduction_value=20)
        quote = quote_price(PriceTarget(100), [permanent, temporary])
        self.assertEqual((quote.price, quote.original_price), (72, 90))
        self.assertIn("<s>", price_html(quote))
        self.assertIn("تخفیف تا", price_html(quote))
        self.assertIn(expiry_input(temporary.ends_at), price_html(quote))
        text_body = await self.session.scalar(select(TargetedCampaign.text).where(TargetedCampaign.id == permanent.campaign_id))
        self.assertIn("دائمی", text_body)
        self.assertNotIn("تخفیف جدید", text_body)

    async def test_best_offer_does_not_stack_and_expiry_restores_base(self) -> None:
        one = await self.create(reduction_value=20)
        two = await self.create(reduction_kind="amount", reduction_value=30)
        rules = [one, two]
        self.assertEqual(quote_price(PriceTarget(100), rules).price, 70)
        expired = quote_price(PriceTarget(100), rules, now=self.now + timedelta(days=2))
        self.assertEqual((expired.price, expired.original_price), (100, None))
        self.assertEqual(quote_price(PriceTarget(0, is_trial=True), rules).price, 0)
        self.assertEqual(quote_price(PriceTarget(5), rules).price, 0)

    async def test_shop_quotes_targets_and_stop_are_isolated(self) -> None:
        row = await self.create(SHOP, scope="plan", plan_id=3)
        self.assertEqual(await active_discounts(self.session, shop_id=3), [])
        self.assertEqual(await active_discounts(self.session, shop_id=None), [])
        with self.assertRaises(ValueError):
            await self.create(OTHER_SHOP, scope="plan", plan_id=3)
        with self.assertRaises(ValueError):
            await self.create(OTHER_SHOP, scope="category", category_id=2)
        with self.assertRaises(ValueError):
            await stop_discount(self.session, OTHER_SHOP, row.id)
        await self.session.refresh(row)
        self.assertTrue(row.is_active)
        with self.assertRaises(ValueError):
            await quote_product(self.session, self.plan, shop_id=2)

    async def test_missing_permission_and_missing_scope_are_denied(self) -> None:
        for staff in [{}, {"role": "reseller", "bot_user_id": 2, "permissions": ["plans"]}]:
            with self.assertRaises(PermissionError):
                await create_discount(self.session, staff, self.data())
        with self.assertRaises(ShopScopeError):
            await create_discount(self.session, {"role": "admin"}, self.data())

    async def test_invalid_numeric_fields_and_target_combinations_are_rejected(self) -> None:
        for value in [True, -1, 0, 101, float("nan"), 1.2, "20"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.create(reduction_value=value)
        for hours in [True, -1, 0, 8761, float("nan")]:
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                await self.create(duration_hours=hours)
        for kwargs in [{"scope": "plan"}, {"scope": "all", "plan_id": 1}, {"scope": "plan", "plan_id": True},
                       {"mode": "permanent"}, {"reduction_kind": "amount", "reduction_value": 2_000_000_001}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.create(**kwargs)

    async def test_repeated_creation_and_worker_do_not_send_twice(self) -> None:
        data = self.data(title="<img src=x onerror=alert(1)>")
        self.session.add_all([
            Order(user_id=5, reseller_id=2, plan_id=3, amount=100, status="delivered"),
            BotUser(id=8, telegram_id=108, referral_code="unrelated", reseller_id=3),
        ])
        await self.session.commit()
        first = await create_discount(self.session, SHOP, data)
        repeated = await create_discount(self.session, SHOP, data)
        self.assertEqual(first.id, repeated.id)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(TargetedCampaign)), 1)
        ids = list((await self.session.scalars(select(CampaignRecipient.user_id).where(CampaignRecipient.campaign_id == first.campaign_id))).all())
        self.assertEqual(sorted(ids), [4, 5])
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
        await process_campaign(self.session, first.campaign_id, bot, delay=0)
        await process_campaign(self.session, first.campaign_id, bot, delay=0)
        self.assertEqual(bot.send_message.await_count, 2)
        args = bot.send_message.call_args_list[0]
        self.assertEqual({call.args[0] for call in bot.send_message.call_args_list}, {104, 105})
        self.assertIsNone(args.kwargs["parse_mode"])
        self.assertIn(f"{format_number(24)} ساعت", args.args[1])
        self.assertIn(expiry_input(first.ends_at), args.args[1])

    async def test_concurrent_creation_enqueues_one_campaign(self) -> None:
        data = self.data(notify=True)
        async def submit() -> int:
            async with self.Session() as session:
                return (await create_discount(session, OWNER, data)).id
        ids = await asyncio.gather(submit(), submit())
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(TargetedCampaign)), 1)

    async def test_unknown_notification_outcome_is_reviewed_without_resend(self) -> None:
        row = await self.create(SHOP)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError("unknown")))
        await process_campaign(self.session, row.campaign_id, bot, delay=0)
        await process_campaign(self.session, row.campaign_id, bot, delay=0)
        self.assertEqual(bot.send_message.await_count, 1)
        status = await self.session.scalar(select(CampaignRecipient.status).where(CampaignRecipient.campaign_id == row.campaign_id))
        self.assertEqual(status, "review")

    async def test_stopped_and_expired_offers_pause_pending_announcements(self) -> None:
        row = await self.create()
        await stop_discount(self.session, OWNER, row.id)
        await stop_discount(self.session, OWNER, row.id)
        self.assertEqual(await active_discounts(self.session, shop_id=None), [])
        self.assertEqual(await self.session.scalar(select(TargetedCampaign.status).where(TargetedCampaign.id == row.campaign_id)), "paused")
        expired = await self.create()
        expired.ends_at = self.now - timedelta(seconds=1)
        await self.session.commit()
        await pause_expired_announcements(self.session)
        self.assertEqual(await self.session.scalar(select(TargetedCampaign.status).where(TargetedCampaign.id == expired.campaign_id)), "paused")

    async def test_checkout_and_coupons_use_discounted_price_and_keep_snapshot(self) -> None:
        row = await self.create(notify=False)
        self.session.add(DiscountCode(code="SAVE", percent=10, is_active=True))
        await self.session.commit()
        order = await create_order(self.session, user_id=1, plan_id=1, discount_code="SAVE")
        self.assertEqual((order.amount, order.discount_amount), (72, 8))
        self.assertEqual(self.plan.price, 100)
        await stop_discount(self.session, OWNER, row.id)
        await self.session.refresh(order)
        self.assertEqual(order.amount, 72)
        fresh = await create_order(self.session, user_id=1, plan_id=1)
        self.assertEqual(fresh.amount, 100)

    async def test_renewal_addon_and_custom_wholesale_orders_use_quotes(self) -> None:
        await self.create(notify=False)
        renewal = await renew_service_with_plan(self.session, user_id=1, service=self.service, plan=self.plan)
        self.assertEqual(renewal.amount, 80)
        renewal.service_mutation_pending = False
        renewal.status = "cancelled"
        await self.session.commit()
        addon = await create_addon_order(self.session, user_id=1, service=self.service, pack=self.pack)
        self.assertEqual(addon.amount, 80)
        with patch("app.services.users.get_all_settings", AsyncMock(return_value={
            "custom_plan_enabled": "1", "custom_plan_price_per_gb": "100", "custom_plan_price_per_day": "100",
            "custom_plan_group_ids": "1",
            'wholesale_tiers': '[{"min": 1, "percent": 0}]',
            "wholesale_enabled": "1", "wholesale_min_qty": "1", "wholesale_max_qty": "100",
        })):
            custom = await create_custom_order(self.session, user_id=1, data_limit_gb=1, duration_days=1)
            self.assertEqual(custom.amount, 160)
            wholesale = await create_wholesale_order(self.session, user_id=1, plan_id=1, quantity=2)
            self.assertEqual(wholesale.amount, 160)

    async def test_wrong_customer_cannot_purchase_discounted_renewal_or_addon(self) -> None:
        await self.create(notify=False)
        with self.assertRaises(ValueError):
            await renew_service_with_plan(self.session, user_id=4, service=self.service, plan=self.plan)
        with self.assertRaises(ValueError):
            await create_addon_order(self.session, user_id=4, service=self.service, pack=self.pack)

    async def test_catalog_quote_and_miniapp_show_previous_price_without_changing_plan(self) -> None:
        await self.create(notify=False)
        plan = await get_catalog_plan(self.session, 1)
        self.assertEqual((displayed_quote(plan).price, plan.price), (80, 100))
        payload = await catalog_plans_payload(self.session)
        item = next(item for item in payload["plans"] if item["id"] == 1)
        self.assertEqual((item["price"], item["original_price"]), (80, 100))
        self.assertIsNotNone(item["discount_ends_at"])
        env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"), autoescape=select_autoescape())
        parser = ScriptSources()
        parser.feed(env.get_template("miniapp.html").render(app_version="test"))
        core = next(url for url in parser.sources if urlsplit(url).path == "/static/miniapp/app.js")
        prices = next(url for url in parser.sources if urlsplit(url).path == "/static/catalog_price.js")
        self.assertEqual(parse_qs(urlsplit(core).query).get("discounts"), ["1"])
        self.assertLess(parser.sources.index(prices), parser.sources.index(core))

    async def test_management_page_escapes_titles_and_rejects_other_shop_stop(self) -> None:
        row = await self.create(SHOP, title="<img src=x onerror=alert(1)>", notify=False)
        app = FastAPI()
        staff = SHOP
        async def auth() -> dict:
            return staff
        async def db() -> AsyncIterator[AsyncSession]:
            yield self.session
        env = Environment(loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(ROOT / "app/web/templates"),
        ]), autoescape=select_autoescape())
        env.filters["expire"] = format_expire_short
        env.filters["num"] = format_number
        env.filters["money"] = format_money
        def render(request: Request, name: str, context: dict) -> HTMLResponse:
            return HTMLResponse(env.get_template(name).render(request=request, csrf_token="token", **context))
        register_catalog_discounts(app, render=render, require_staff=auth, get_db=db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/catalog-discounts")
            self.assertEqual(response.status_code, 200)
            self.assertIn("&lt;img", response.text)
            self.assertNotIn("<img", response.text)
            staff = OTHER_SHOP
            response = await client.post(f"/catalog-discounts/{row.id}/stop")
            self.assertEqual(response.status_code, 303)
            await self.session.refresh(row)
            self.assertTrue(row.is_active)
            staff = {"role": "reseller", "bot_user_id": 2, "permissions": []}
            self.assertEqual((await client.get("/catalog-discounts")).status_code, 403)


    async def test_price_changes_require_new_confirmation_before_creating_order(self) -> None:
        for expected in (True, -1, 2_000_000_001, float("nan")):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                await create_order(self.session, user_id=1, plan_id=1, expected_price=expected)
        await self.create(notify=False)
        for call in (
            create_order(self.session, user_id=1, plan_id=1, expected_price=100),
            renew_service_with_plan(self.session, user_id=1, service=self.service, plan=self.plan, expected_price=100),
            create_addon_order(self.session, user_id=1, service=self.service, pack=self.pack, expected_price=100),
        ):
            with self.assertRaisesRegex(ValueError, "قیمت تغییر"):
                await call
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(Order)), 0)

    async def test_bot_payment_screen_shows_crossed_price_and_escapes_catalog_text(self) -> None:
        await self.create(notify=False)
        self.plan.name = "<img src=x>"
        self.plan.description = "<script>unsafe</script>"
        order = Order(id=91, user_id=self.user.id, plan_id=1, amount=80)
        self.session.add(order)
        await self.session.commit()
        message = await checkout_summary(self.session, order, currency="تومان")
        self.assertIn("<s>", message)
        self.assertIn("تخفیف تا", message)
        self.assertIn("&lt;img", message)
        self.assertNotIn("<script>", message)

    async def test_automation_choices_expose_current_discounted_prices(self) -> None:
        await self.create(notify=False)
        payload = await automation_settings(self.session, self.user, self.service.id)
        choice = payload["actions"][0]["choices"][0]
        self.assertEqual((choice["price"], choice["original_price"]), (80, 100))


    async def test_active_adjustment_limit_is_enforced_and_stop_releases_capacity(self) -> None:
        rows = [CatalogDiscount(
            shop_key=0, reseller_id=None, active_slot=slot, title="Active", scope="all",
            mode="permanent", reduction_kind="percent", reduction_value=1, request_key=secrets.token_hex(16),
        ) for slot in range(100)]
        self.session.add_all(rows)
        await self.session.commit()
        with self.assertRaisesRegex(ValueError, "متوقف"):
            await self.create(notify=False)
        await stop_discount(self.session, OWNER, rows[0].id)
        row = await self.create(notify=False)
        self.assertEqual(row.active_slot, 0)
        self.assertEqual(len(await active_discounts(self.session, shop_id=None)), 100)

    async def test_deleted_plan_rule_does_not_match_an_unspecified_product(self) -> None:
        row = await self.create(SHOP, scope="plan", plan_id=3, notify=False)
        plan = await self.session.get(Plan, 3)
        await self.session.delete(plan)
        await self.session.commit()
        await self.session.refresh(row)
        self.assertIsNone(row.plan_id)
        self.assertEqual(quote_price(PriceTarget(100), [row]).price, 100)

    def test_price_announcements_leave_one_second_between_sends(self) -> None:
        self.assertEqual(campaign_send_delay("catalog_discount", requested=0.1), 1.0)
        self.assertEqual(campaign_send_delay("trial", requested=0.1), 0.1)


class CatalogDiscountMigrationTests(unittest.TestCase):
    def test_upgrade_twice_downgrade_and_reupgrade_preserve_existing_rows(self) -> None:
        spec = importlib.util.spec_from_file_location("catalog_discount_migration", ROOT / "alembic/versions/0046_catalog_discounts.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        test_url = os.environ.get("PGCLOCKBOT_CATALOG_TEST_URL")
        engine = create_engine(test_url.replace("+asyncpg", "+psycopg") if test_url else "sqlite://")
        schema = "catalog_migration_" + secrets.token_hex(8)
        with engine.begin() as connection:
            if test_url:
                connection.execute(CreateSchema(schema))
                connection.execute(text("SELECT set_config('search_path', :schema, true)"), {"schema": schema})
            for table in ("bot_users", "plans", "plan_categories", "targeted_campaigns"):
                connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
                connection.execute(text(f"INSERT INTO {table} (id) VALUES (1)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
                connection.execute(text("INSERT INTO catalog_discounts (shop_key,title,scope,mode,reduction_kind,reduction_value,request_key) VALUES (0,'saved','all','permanent','percent',10,'request')"))
                migration.downgrade()
                self.assertEqual(connection.scalar(text("SELECT title FROM catalog_discounts")), "saved")
                self.assertEqual(connection.scalar(text("SELECT id FROM plans")), 1)
                migration.upgrade()
                self.assertIn("ix_catalog_discounts_shop_active", {item["name"] for item in inspect(connection).get_indexes("catalog_discounts")})
            if test_url:
                connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()
