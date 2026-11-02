"""Renewal previews, quota carryover and durable paid-order recovery."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_pages import register_miniapp_pages
from app.db.models import Base, BotUser, Order, Payment, Plan, ServiceAddonPack, UserService, WalletTransaction
from app.services.orders import apply_renewal, pay_with_wallet, renew_service_with_plan
from app.services.pasarguard import PasarGuardError
from app.services.service_renewals import (
    RenewalPendingReview, calculate_renewal, preview_renewal, renewal_terms,
)
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

GB = 1024**3
DAY = 86400
ROOT = Path(__file__).resolve().parents[1]


class RenewalArithmeticTests(unittest.TestCase):
    def result(self, *, limit=10 * GB, used=3 * GB, expire=1_700_000_000 + 5 * DAY, status="active", gb=10, days=30, **extra):
        return calculate_renewal(
            {"status": status, "data_limit": limit, "used_traffic": used, "expire": expire, **extra},
            renewal_terms(SimpleNamespace(data_limit_gb=gb, duration_days=days)), now=1_700_000_000,
        )

    def test_adds_live_remaining_volume_and_time_without_reset(self):
        result = self.result()
        self.assertEqual(result["remaining_bytes"], 7 * GB)
        self.assertEqual(result["result_remaining_bytes"], 17 * GB)
        self.assertEqual(result["payload"]["data_limit"], 20 * GB)
        self.assertEqual(result["payload"]["expire"], 1_700_000_000 + 35 * DAY)

    def test_overused_and_expired_service_gets_full_purchased_quota(self):
        result = self.result(used=12 * GB, expire=1_700_000_000 - DAY, status="limited")
        self.assertEqual(result["payload"]["data_limit"], 22 * GB)
        self.assertEqual(result["result_seconds"], 30 * DAY)

    def test_unlimited_new_plan_explicitly_clears_panel_limits(self):
        result = self.result(gb=None, days=0)
        self.assertEqual(result["payload"], {"status": "active", "data_limit": 0, "expire": 0})
        self.assertIsNone(result["result_remaining_bytes"])
        self.assertIsNone(result["result_seconds"])

    def test_unlimited_existing_quota_does_not_make_finite_plan_unlimited(self):
        result = self.result(limit=0, expire=0)
        self.assertEqual(result["payload"]["data_limit"], 13 * GB)
        self.assertEqual(result["result_seconds"], 30 * DAY)
        self.assertEqual(len(result["warnings"]), 2)

    def test_on_hold_extends_duration_without_starting_timer(self):
        result = self.result(status="on_hold", expire=0, on_hold_expire_duration=5 * DAY)
        self.assertEqual(result["payload"]["status"], "on_hold")
        self.assertEqual(result["payload"]["expire"], 0)
        self.assertEqual(result["payload"]["on_hold_expire_duration"], 35 * DAY)

    def test_on_hold_unlimited_time_activates_with_explicit_warning(self):
        result = self.result(status="on_hold", expire=0, expire_duration=5 * DAY, days=0)
        self.assertEqual(result["payload"]["status"], "active")
        self.assertEqual(result["payload"]["expire"], 0)
        self.assertIn("انتظار", result["warnings"][0])

    def test_naive_iso_expiration_is_interpreted_as_utc(self):
        result = self.result(expire="2023-11-19T22:13:20")
        self.assertEqual(result["result_seconds"], 35 * DAY)

    def test_missing_or_invalid_live_fields_fail_closed(self):
        baseline = {"status": "active", "data_limit": 10 * GB, "used_traffic": 0, "expire": 0}
        terms = renewal_terms(SimpleNamespace(data_limit_gb=10, duration_days=30))
        for key in baseline:
            info = dict(baseline)
            info.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                calculate_renewal(info, terms)
        for key, value in [("data_limit", -1), ("used_traffic", True), ("expire", "bad"), ("status", "unknown")]:
            with self.subTest(field=key), self.assertRaises(ValueError):
                calculate_renewal({**baseline, key: value}, terms)

    def test_on_hold_missing_duration_is_unknown_not_unlimited(self):
        with self.assertRaises(ValueError):
            self.result(status="on_hold", expire=0)

    def test_fractional_gigabytes_are_preserved(self):
        result = self.result(gb=2.5)
        self.assertEqual(result["payload"]["data_limit"], int(12.5 * GB))


class RenewalPG:
    def __init__(self):
        self.info = {"status": "active", "data_limit": 10 * GB, "used_traffic": 3 * GB, "expire": int(time.time()) + 5 * DAY}
        self.writes = []
        self.fail_after_write = False
        self.reject = False
        self.entered = None
        self.release = None
        self.reset_user_by_id = AsyncMock()
        self.modify_user_with_template = AsyncMock()

    async def get_user_by_id(self, user_id):
        return dict(self.info)

    async def modify_user_by_id(self, user_id, payload):
        if self.reject:
            raise PasarGuardError("denied", status_code=403)
        self.writes.append(dict(payload))
        self.info.update(payload)
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.fail_after_write:
            raise TimeoutError("lost panel response")
        return dict(self.info)


class RenewalFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'renewal.db'}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.user = BotUser(id=1, telegram_id=1001, referral_code="renew-buyer", wallet_balance=1000)
        self.other = BotUser(id=2, telegram_id=1002, referral_code="renew-other")
        self.plan = Plan(id=1, name="Monthly", price=200, data_limit_gb=10, duration_days=30, pg_template_id=7)
        self.service = UserService(id=1, bot_user_id=1, pg_user_id=10, pg_username="renew-service", plan_id=1)
        self.session.add_all([self.user, self.other, self.plan, self.service])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        self.pg = RenewalPG()
        self.patches = [
            patch("app.services.orders.get_pg", return_value=self.pg),
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.loyalty.consume_loyalty_discount_for_order", AsyncMock()),
            patch("app.services.loyalty.on_order_delivered", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def create(self):
        return await renew_service_with_plan(self.session, user_id=1, service=self.service, plan=self.plan)


class RenewalDeliveryTests(RenewalFixture):
    async def test_preview_has_no_financial_or_panel_writes(self):
        preview = await preview_renewal(self.session, user_id=1, service=self.service, plan=self.plan)
        self.assertEqual(preview["result_remaining_bytes"], 17 * GB)
        self.assertEqual(preview["price"], 200)
        self.assertNotIn("payload", preview)
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 0)
        self.assertEqual(self.user.wallet_balance, 1000)
        self.assertEqual(self.pg.writes, [])

    async def test_payment_applies_carryover_once_and_keeps_connection_groups(self):
        self.pg.info["group_ids"] = [3, 4]
        preview = await preview_renewal(self.session, user_id=1, service=self.service, plan=self.plan)
        order = await self.create()
        await pay_with_wallet(self.session, order, self.user)
        await pay_with_wallet(self.session, order, self.user)
        self.assertEqual(order.status, "delivered")
        self.assertFalse(order.service_mutation_pending)
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 800)
        self.assertEqual(len(self.pg.writes), 1)
        self.assertEqual(self.pg.info["data_limit"] - self.pg.info["used_traffic"], preview["result_remaining_bytes"])
        self.assertEqual(self.pg.info["group_ids"], [3, 4])
        self.pg.modify_user_with_template.assert_not_awaited()
        self.pg.reset_user_by_id.assert_not_awaited()

    async def test_plan_edit_after_purchase_does_not_change_entitlement(self):
        order = await self.create()
        self.plan.data_limit_gb, self.plan.duration_days = 1, 1
        await self.session.commit()
        await pay_with_wallet(self.session, order, self.user)
        self.assertEqual(self.pg.info["data_limit"], 20 * GB)
        self.assertGreater(self.pg.info["expire"], int(time.time()) + 34 * DAY)

    async def test_unknown_result_keeps_payment_and_retry_reuses_absolute_target(self):
        order = await self.create()
        self.pg.fail_after_write = True
        with self.assertRaises(RenewalPendingReview):
            await pay_with_wallet(self.session, order, self.user)
        await self.session.refresh(order)
        await self.session.refresh(self.user)
        self.assertEqual((order.status, order.service_mutation_pending, self.user.wallet_balance), ("paid", True, 800))
        payment = await self.session.scalar(select(Payment).where(Payment.order_id == order.id))
        self.assertEqual(payment.status, "approved")
        with self.assertRaisesRegex(ValueError, "تعیین تکلیف"):
            await self.create()
        self.pg.info["used_traffic"] += GB
        self.pg.fail_after_write = False
        await pay_with_wallet(self.session, order, self.user)
        self.assertEqual(self.pg.writes[0], self.pg.writes[1])
        self.assertEqual(self.pg.info["data_limit"], 20 * GB)
        self.assertEqual(await self.session.scalar(select(func.count(WalletTransaction.id))), 1)
        self.assertEqual(order.status, "delivered")
        self.assertFalse(order.service_mutation_pending)

    async def test_known_panel_rejection_refunds_and_releases_service(self):
        order = await self.create()
        self.pg.reject = True
        with self.assertRaises(PasarGuardError):
            await pay_with_wallet(self.session, order, self.user)
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 1000)
        self.assertEqual(order.status, "pending")
        self.assertFalse(order.service_mutation_pending)
        self.assertNotIn("target", json.loads(order.renewal_snapshot))
        self.pg.reject = False
        await pay_with_wallet(self.session, order, self.user)
        self.assertEqual(self.pg.info["data_limit"], 20 * GB)

    async def test_rejected_retry_does_not_clear_original_uncertain_result(self):
        order = await self.create()
        self.pg.fail_after_write = True
        with self.assertRaises(RenewalPendingReview):
            await pay_with_wallet(self.session, order, self.user)
        original = order.renewal_snapshot
        self.pg.fail_after_write, self.pg.reject = False, True
        with self.assertRaises(RenewalPendingReview):
            await pay_with_wallet(self.session, order, self.user)
        self.assertTrue(order.service_mutation_pending)
        self.assertEqual(order.renewal_snapshot, original)

    async def test_on_hold_retry_after_first_connection_does_not_restart_timer(self):
        self.pg.info.update(status="on_hold", expire=0, on_hold_expire_duration=5 * DAY)
        order = await self.create()
        self.pg.fail_after_write = True
        with self.assertRaises(RenewalPendingReview):
            await pay_with_wallet(self.session, order, self.user)
        self.pg.info.update(status="active", expire=int(time.time()) + 35 * DAY)
        self.pg.fail_after_write = False
        await pay_with_wallet(self.session, order, self.user)
        self.assertEqual(order.status, "delivered")
        self.assertEqual(len(self.pg.writes), 1)
        self.assertEqual(self.pg.info["status"], "active")

    async def test_incomplete_panel_response_refunds_before_any_mutation(self):
        order = await self.create()
        self.pg.info.pop("used_traffic")
        with self.assertRaises(ValueError):
            await pay_with_wallet(self.session, order, self.user)
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 1000)
        self.assertFalse(order.service_mutation_pending)
        self.assertEqual(self.pg.writes, [])

    async def test_cross_user_and_cross_shop_are_rejected_before_purchase(self):
        self.service.bot_user_id = 2
        with self.assertRaisesRegex(ValueError, "متعلق"):
            await self.create()
        self.service.bot_user_id = 1
        self.user.reseller_id = 2
        self.service.shop_id = 2
        await self.session.commit()
        with self.assertRaisesRegex(ValueError, "فروشگاه"):
            await self.create()
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 0)

    async def test_linked_service_and_trial_plan_cannot_be_renewed(self):
        self.service.remark = "linked"
        with self.assertRaises(ValueError):
            await self.create()
        self.service.remark = None
        self.plan.is_trial = True
        with self.assertRaises(ValueError):
            await self.create()

    async def test_distinct_paid_orders_cannot_write_same_service_concurrently(self):
        first, second = await self.create(), await self.create()
        first.status = second.status = "paid"
        await self.session.commit()
        first_id, second_id = first.id, second.id
        self.pg.entered, self.pg.release = asyncio.Event(), asyncio.Event()

        async def deliver(order_id):
            async with self.Session() as session:
                order = await session.get(Order, order_id)
                await apply_renewal(session, order, await session.get(UserService, 1), await session.get(Plan, 1))

        running = asyncio.create_task(deliver(first_id))
        try:
            await asyncio.wait_for(self.pg.entered.wait(), 3)
            with self.assertRaisesRegex(ValueError, "تغییر دیگری"):
                await deliver(second_id)
        finally:
            self.pg.release.set()
            await running
        self.assertEqual(len(self.pg.writes), 1)
        await deliver(second_id)
        self.assertEqual(self.pg.info["data_limit"], 30 * GB)

    async def _serialize_addon_and_renewal(self, *, first_renewal):
        from app.services.service_addons import apply_service_addon, create_addon_order

        pack = ServiceAddonPack(name="extra", kind="volume", amount=5, price=50)
        self.session.add(pack)
        await self.session.commit()
        addon = await create_addon_order(self.session, user_id=1, service=self.service, pack=pack)
        renewal = await self.create()
        addon.status = renewal.status = "paid"
        await self.session.commit()
        first_id, second_id = (renewal.id, addon.id) if first_renewal else (addon.id, renewal.id)
        self.pg.entered, self.pg.release = asyncio.Event(), asyncio.Event()

        async def deliver(order_id):
            async with self.Session() as session:
                order = await session.get(Order, order_id)
                if order.note.startswith("renew:"):
                    await apply_renewal(session, order, await session.get(UserService, 1), await session.get(Plan, 1))
                else:
                    await apply_service_addon(session, order)

        running = asyncio.create_task(deliver(first_id))
        try:
            await asyncio.wait_for(self.pg.entered.wait(), 3)
            with self.assertRaisesRegex(ValueError, "تغییر دیگری"):
                await deliver(second_id)
        finally:
            self.pg.release.set()
            await running
        await deliver(second_id)
        self.assertEqual(self.pg.info["data_limit"], 25 * GB)

    async def test_addon_cannot_overwrite_a_renewal_in_progress(self):
        await self._serialize_addon_and_renewal(first_renewal=True)

    async def test_renewal_waits_for_an_addon_in_progress(self):
        await self._serialize_addon_and_renewal(first_renewal=False)

    async def test_legacy_order_preserves_previous_reset_contract(self):
        self.plan.pg_template_id = None
        order = Order(user_id=1, plan_id=1, service_id=1, amount=200, status="paid", note="renew:1:auto")
        self.session.add(order)
        await self.session.commit()
        await apply_renewal(self.session, order, self.service, self.plan, reset_traffic=True)
        self.assertEqual(self.pg.info["data_limit"], 10 * GB)
        self.pg.reset_user_by_id.assert_awaited_once()


class MiniAppRenewalTests(RenewalFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.api_patches = [
            patch("app.api.miniapp_pages.load_mini_user", AsyncMock(return_value=self.user)),
            patch("app.api.miniapp_pages.assert_mini_force_join", AsyncMock()),
            patch("app.api.miniapp_pages.get_all_settings", AsyncMock(return_value={"pay_wallet_enabled": "1"})),
        ]
        for p in self.api_patches:
            p.start()
        app = FastAPI()

        async def get_db():
            yield self.session

        register_miniapp_pages(app, render=lambda *args: "", get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.api_patches):
            p.stop()
        await super().asyncTearDown()

    async def quote(self):
        response = await self.client.get("/api/mini/service/1/renewal-preview?plan_id=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        preview = response.json()
        return {"terms": preview["terms"], "price": preview["price"], "request_key": preview["request_key"]}

    async def test_api_preview_then_wallet_delivery(self):
        quote = await self.quote()
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 0)
        response = await self.client.post("/api/mini/renew", json={"service_id": 1, "plan_id": 1, "preview": quote})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["wallet"], 800)
        self.assertEqual(self.pg.info["data_limit"], 20 * GB)
        retry = await self.client.post("/api/mini/renew", json={"service_id": 1, "plan_id": 1, "preview": quote})
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["order_id"], response.json()["order_id"])
        self.assertEqual(retry.json()["wallet"], 800)
        self.assertEqual(len(self.pg.writes), 1)

    async def test_api_retry_after_lost_response_succeeds_when_wallet_is_now_empty(self):
        self.user.wallet_balance = 200
        await self.session.commit()
        quote = await self.quote()
        body = {"service_id": 1, "plan_id": 1, "preview": quote}
        first = await self.client.post("/api/mini/renew", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["wallet"], 0)
        retry = await self.client.post("/api/mini/renew", json=body)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["order_id"], first.json()["order_id"])
        self.assertEqual(retry.json()["wallet"], 0)
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 1)
        self.assertEqual(len(self.pg.writes), 1)

    async def test_api_price_change_requires_new_preview_before_debit(self):
        quote = await self.quote()
        self.plan.price = 300
        await self.session.commit()
        response = await self.client.post("/api/mini/renew", json={"service_id": 1, "plan_id": 1, "preview": quote})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 0)
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 1000)

    async def test_api_rejects_foreign_service_and_shop_plan_without_panel_read(self):
        self.service.bot_user_id = 2
        await self.session.commit()
        response = await self.client.get("/api/mini/service/1/renewal-preview?plan_id=1")
        self.assertEqual(response.status_code, 404)
        self.service.bot_user_id = 1
        self.plan.owner_reseller_id = 2
        await self.session.commit()
        response = await self.client.get("/api/mini/service/1/renewal-preview?plan_id=1")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pg.writes, [])

    async def test_api_admin_commerce_and_force_join_are_enforced(self):
        self.user.role = "admin"
        response = await self.client.get("/api/mini/service/1/renewal-preview?plan_id=1")
        self.assertEqual(response.status_code, 403)
        self.user.role = "user"
        from fastapi import HTTPException
        with patch("app.api.miniapp_pages.assert_mini_force_join", AsyncMock(side_effect=HTTPException(403, "join required"))):
            response = await self.client.get("/api/mini/service/1/renewal-preview?plan_id=1")
        self.assertEqual(response.status_code, 403)

    async def test_api_unknown_result_keeps_debit_and_prevents_new_purchase(self):
        quote = await self.quote()
        self.pg.fail_after_write = True
        response = await self.client.post("/api/mini/renew", json={"service_id": 1, "plan_id": 1, "preview": quote})
        self.assertEqual(response.status_code, 400)
        self.assertIn("پشتیبانی", response.json()["detail"])
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 800)
        response = await self.client.get("/api/mini/service/1/renewal-preview?plan_id=1")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.pg.writes), 1)


class RenewalBotTests(RenewalFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.data = {}

        async def update_data(**kwargs):
            self.data.update(kwargs)

        self.state = SimpleNamespace(get_data=AsyncMock(side_effect=lambda: dict(self.data)), update_data=AsyncMock(side_effect=update_data))
        self.callback = SimpleNamespace(data="svc:renewpay:1:1", answer=AsyncMock(), message=AsyncMock())

    async def test_preview_does_not_purchase_even_when_plan_is_free(self):
        from app.bot.handlers.services import svc_renew_preview, svc_renew_pay

        self.plan.price = 0
        await self.session.commit()
        with (
            patch("app.bot.handlers.services.safe_edit_text", AsyncMock()) as edit,
            patch("app.bot.handlers.services.get_all_settings", AsyncMock(return_value={})),
        ):
            await svc_renew_preview(self.callback, self.session, self.user, self.state)
            self.assertIn("پیش‌نمایش", edit.call_args.args[1])
            self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 0)
            self.assertEqual(self.pg.writes, [])
            self.callback.data = "svc:renewconfirm:1:1"
            await svc_renew_pay(self.callback, self.session, self.user, self.state)
            await svc_renew_pay(self.callback, self.session, self.user, self.state)
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 1)
        self.assertEqual(len(self.pg.writes), 1)
        order = await self.session.scalar(select(Order))
        self.assertEqual(order.status, "delivered")

    async def test_confirmation_rejects_changed_terms_before_order_creation(self):
        from app.bot.handlers.services import svc_renew_preview, svc_renew_pay

        with (
            patch("app.bot.handlers.services.safe_edit_text", AsyncMock()),
            patch("app.bot.handlers.services.get_all_settings", AsyncMock(return_value={"pay_wallet_enabled": "1"})),
        ):
            await svc_renew_preview(self.callback, self.session, self.user, self.state)
            self.plan.data_limit_gb = 1
            await self.session.commit()
            self.callback.data = "svc:renewconfirm:1:1"
            await svc_renew_pay(self.callback, self.session, self.user, self.state)
        self.assertIn("تغییر", self.callback.answer.call_args.args[0])
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 0)


class RenewalMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_existing_orders_and_downgrade_is_reversible(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = ROOT / "alembic/versions/0038_renewal_preview.py"
        spec = importlib.util.spec_from_file_location("renewal_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id INTEGER, amount INTEGER, status TEXT, note TEXT)"))
            conn.execute(text("INSERT INTO orders VALUES (1, 1, 200, 'paid', 'renew:1')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(conn.execute(text("SELECT amount, renewal_snapshot, service_mutation_pending FROM orders")).one(), (200, None, 0))
                migration.downgrade()
            self.assertEqual({c["name"] for c in sa.inspect(conn).get_columns("orders")}, {"id", "service_id", "amount", "status", "note"})
        engine.dispose()
