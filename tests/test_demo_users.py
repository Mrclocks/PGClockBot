"""Demo customer reporting, staff authorization and reversible metadata."""
from __future__ import annotations

import importlib.util
import unittest
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.user_pages import register_user_pages
from app.db.models import (
    Base, BotUser, DeliveryFailure, FunnelEvent, LoyaltyReward, Order, Payment, Plan,
    PointsTransaction, ReferralEvent, RewardRedemption, ShopWallet, Ticket, UserService, WalletTransaction,
)
from app.services.daily_report import _scoped_totals, _today_activity
from app.services.demo_users import set_demo_user
from app.services.finance_reports import build_finance_report
from app.services.formatting import format_money, format_number
from app.services.home_overview import admin_customer_counts, bot_dashboard_summary, bot_panel_summary, reseller_shop_summary
from app.services.loyalty import overview_metrics
from app.services.subordinate_report import SubordinateShop, collect_subordinate_stats
from app.services.ux20 import funnel_summary
from app.services.shop_scope import ShopScopeError

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}


def shop_staff(shop_id: int, *, has_permission: bool = True) -> dict[str, Any]:
    return {
        "role": "reseller", "bot_user_id": shop_id,
        "permissions": ["dashboard", "demo_users"] if has_permission else ["dashboard"],
    }


class DemoUserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.now = datetime.now(timezone.utc)
        self.session.add_all([
            BotUser(id=10, telegram_id=1010, referral_code="shop10", role="reseller", created_at=self.now - timedelta(days=90)),
            BotUser(id=20, telegram_id=1020, referral_code="shop20", role="reseller", created_at=self.now - timedelta(days=90)),
            Plan(id=1, name="Test plan", price=100, duration_days=1, data_limit_gb=10),
        ])
        await self.session.flush()
        self.customers = {1: None, 2: None, 11: 10, 12: 10, 21: 20, 22: 20}
        self.demo_ids = {2, 12, 22}
        for user_id, shop_id in self.customers.items():
            self.session.add(BotUser(
                id=user_id, telegram_id=1000 + user_id, referral_code=f"customer{user_id}",
                full_name='<script>alert("test")</script>', reseller_id=shop_id,
                is_demo=user_id in self.demo_ids, wallet_balance=50, created_at=self.now,
            ))
        await self.session.flush()
        for user_id, shop_id in self.customers.items():
            self.session.add_all([
                Order(id=user_id * 10, user_id=user_id, reseller_id=shop_id, plan_id=1, amount=900 if user_id in self.demo_ids else 100, status="delivered", payment_method="wallet", created_at=self.now),
                Order(id=user_id * 10 + 1, user_id=user_id, reseller_id=shop_id, plan_id=1, amount=50, status="awaiting_approval", created_at=self.now),
                UserService(id=user_id, bot_user_id=user_id, plan_id=1, pg_username=f"customer{user_id}", notified_traffic=True, created_at=self.now),
                Ticket(user_id=user_id, reseller_id=shop_id, subject="Support", status="open"),
                FunnelEvent(user_id=user_id, reseller_id=shop_id, step="delivered", idempotency_key=f"demo{user_id}", created_at=self.now),
                WalletTransaction(user_id=user_id, reseller_id=shop_id, amount=70, balance_after=50, reason="test"),
            ])
            if shop_id is not None:
                self.session.add(ShopWallet(user_id=user_id, reseller_id=shop_id, balance=80))
        await self.session.flush()
        for user_id, shop_id in self.customers.items():
            self.session.add_all([
                Payment(user_id=user_id, order_id=user_id * 10 + 1, amount=50, status="pending", receipt_file_id="fake-receipt"),
                Payment(user_id=user_id, amount=30, status="pending", receipt_file_id="fake-topup", is_wallet_topup=True, wallet_shop_id=shop_id),
                Payment(user_id=user_id, amount=70, status="approved", is_wallet_topup=True, wallet_shop_id=shop_id),
                DeliveryFailure(order_id=user_id * 10 + 1, reseller_id=shop_id, error="test"),
            ])
        await self.session.commit()
        self.staff = OWNER.copy()
        self.app = FastAPI()

        async def get_db() -> AsyncIterator[AsyncSession]:
            yield self.session

        async def require_staff() -> dict[str, Any]:
            return self.staff

        self.env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"), autoescape=select_autoescape())
        self.env.filters["num"] = format_number
        self.env.filters["money"] = format_money

        def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
            return HTMLResponse(self.env.get_template(name).render(**context, csrf_token="fake-csrf"))

        register_user_pages(self.app, render=render, require_admin=require_staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.session.close()
        await self.engine.dispose()

    async def test_finance_reports_exclude_demo_history_in_each_shop_and_period(self) -> None:
        for shop_id in (None, 10, 20):
            with self.subTest(shop_id=shop_id):
                report = await build_finance_report(self.session, reseller_id=shop_id)
                for period in ("day", "week", "month"):
                    self.assertEqual(report["periods"][period], {"orders": 2, "delivered": 1, "revenue": 100, "new_users": 1, "avg_order": 100})
                self.assertEqual(report["payment_methods"], [{"method": "wallet", "label": "کیف پول", "count": 1, "amount": 100}])
                for key in ("total_users", "open_tickets", "delivery_failures", "expiring_services", "low_volume_services"):
                    self.assertEqual(report["ops"][key], 3 if shop_id is None and key == "total_users" else 1)
                self.assertEqual(report["ops"]["pending_receipts"], 2)

    async def test_daily_reports_and_dashboards_use_same_non_demo_revenue(self) -> None:
        for shop_id in (None, 10, 20):
            summary = await _scoped_totals(self.session, reseller_id=shop_id)
            self.assertEqual(summary["orders_total"], 2)
            self.assertEqual(summary["revenue_total"], 100)
            activity = await _today_activity(self.session, reseller_id=shop_id)
            self.assertEqual(activity["revenue_today"], 100)
            self.assertEqual(activity["services_new"], 1)
            if shop_id is not None:
                dashboard = await reseller_shop_summary(self.session, shop_id)
                self.assertEqual(dashboard["users"], 1)
                self.assertEqual(dashboard["services"], 1)
                self.assertEqual(dashboard["pending"], 2)
        platform = await bot_panel_summary(self.session)
        self.assertEqual(platform["services"], 3)
        self.assertEqual(platform["pending"], 2)
        self.assertEqual((await bot_dashboard_summary(self.session))["pending_orders"], 1)
        self.assertEqual((await admin_customer_counts(self.session))["orders"], 6)

    async def test_records_older_than_month_are_removed_from_all_time_totals(self) -> None:
        self.session.add(Order(user_id=11, reseller_id=10, amount=600, status="delivered", created_at=self.now - timedelta(days=90)))
        await self.session.commit()
        self.assertEqual((await _scoped_totals(self.session, reseller_id=10))["revenue_total"], 700)
        await set_demo_user(self.session, shop_staff(10), 11, is_demo=True)
        totals = await _scoped_totals(self.session, reseller_id=10)
        self.assertEqual(totals["revenue_total"], 0)
        self.assertEqual(totals["orders_total"], 0)
        await set_demo_user(self.session, shop_staff(10), 11, is_demo=False)
        self.assertEqual((await _scoped_totals(self.session, reseller_id=10))["revenue_total"], 700)

    async def test_subordinate_report_excludes_demo_customers_orders_and_services(self) -> None:
        shops = [SubordinateShop(principal_id=10, reseller_profile_id=10, reseller_user_id=10, display_name="Shop", is_active=True)]
        row = (await collect_subordinate_stats(self.session, shops))[0]
        self.assertEqual(row.users_total, 1)
        self.assertEqual(row.services_total, 1)
        self.assertEqual(row.orders_new, 2)
        self.assertEqual(row.revenue_today, 100)

    async def test_payment_reports_follow_transaction_shop_for_shared_customers(self) -> None:
        self.session.add(Order(id=500, user_id=11, reseller_id=20, amount=50, status="awaiting_approval"))
        await self.session.flush()
        self.session.add_all([
            Payment(user_id=1, amount=30, status="pending", receipt_file_id="other-shop-topup", is_wallet_topup=True, wallet_shop_id=10),
            Payment(user_id=11, order_id=500, amount=50, status="pending", receipt_file_id="other-shop-order"),
        ])
        await self.session.commit()
        for shop_id, pending in ((None, 2), (10, 3), (20, 3)):
            self.assertEqual((await build_finance_report(self.session, reseller_id=shop_id))["ops"]["pending_receipts"], pending)
            self.assertEqual((await _scoped_totals(self.session, reseller_id=shop_id))["pending"], pending)
        await set_demo_user(self.session, shop_staff(10), 11, is_demo=True)
        self.assertEqual((await build_finance_report(self.session, reseller_id=20))["ops"]["pending_receipts"], 2)

    async def test_reports_see_demo_change_even_with_customer_cached_in_session(self) -> None:
        other_session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with other_session() as other:
            cached_user = await other.get(BotUser, 11)
            await set_demo_user(self.session, shop_staff(10), 11, is_demo=True)
            self.assertFalse(cached_user.is_demo)
            self.assertEqual((await build_finance_report(other, reseller_id=10))["active"]["revenue"], 0)

    async def test_funnel_excludes_demo_events_and_fallback_orders(self) -> None:
        for shop_id in (None, 10, 20):
            self.assertEqual((await funnel_summary(self.session, reseller_id=shop_id))["delivered"], 1)
        await self.session.execute(sa.delete(FunnelEvent))
        await self.session.commit()
        for shop_id in (None, 10, 20):
            funnel = await funnel_summary(self.session, reseller_id=shop_id)
            self.assertEqual(funnel["delivered"], 1)
            self.assertEqual(funnel["pay_start"], 2)

    async def test_loyalty_credits_and_points_history_excludes_demo_customers(self) -> None:
        self.session.add(LoyaltyReward(id=1, name="Wallet", reward_type="wallet_credit", reward_value=100, points_cost=10))
        await self.session.flush()
        for user_id in self.customers:
            self.session.add_all([
                PointsTransaction(user_id=user_id, amount=100, balance_after=100, tx_type="earn", idempotency_key=f"earn{user_id}"),
                PointsTransaction(user_id=user_id, amount=-10, balance_after=90, tx_type="redeem", idempotency_key=f"spend{user_id}"),
                RewardRedemption(user_id=user_id, reward_id=1, reward_type="wallet_credit", reward_value=100, points_spent=10, status="completed", idempotency_key=f"reward{user_id}"),
            ])
        for user_id in (11, 12):
            await self.session.execute(sa.update(BotUser).where(BotUser.id == user_id).values(referred_by_id=10))
            self.session.add(ReferralEvent(referrer_id=10, referred_id=user_id, qualification_state="qualified", event_key="purchase", idempotency_key=f"ref{user_id}"))
        await self.session.commit()
        summary = await overview_metrics(self.session, reseller_id=10)
        self.assertEqual(summary["points_issued"], 100)
        self.assertEqual(summary["points_redeemed"], 10)
        self.assertEqual(summary["wallet_credits_issued"], 100)
        self.assertEqual(summary["total_referrals"], 1)
        for shop_id, amount in ((None, 300), (20, 100)):
            self.assertEqual((await overview_metrics(self.session, reseller_id=shop_id))["wallet_credits_issued"], amount)
        await set_demo_user(self.session, shop_staff(10), 11, is_demo=True)
        summary = await overview_metrics(self.session, reseller_id=10)
        self.assertEqual(summary["points_issued"], 0)
        self.assertEqual(summary["points_redeemed"], 0)
        self.assertEqual(summary["wallet_credits_issued"], 0)
        self.assertEqual(summary["qualified_referrals"], 0)
        self.assertEqual(summary["top_referrers"], [])
        await set_demo_user(self.session, shop_staff(10), 11, is_demo=False)
        self.assertEqual((await overview_metrics(self.session, reseller_id=10))["wallet_credits_issued"], 100)

    async def test_marking_existing_customer_is_reversible_and_repeat_safe(self) -> None:
        for _ in range(2):
            await set_demo_user(self.session, shop_staff(10), 11, is_demo=True)
        report = await build_finance_report(self.session, reseller_id=10)
        self.assertEqual(report["active"]["revenue"], 0)
        self.assertEqual(report["active"]["orders"], 0)
        self.assertEqual(report["ops"]["pending_receipts"], 0)
        self.assertEqual((await _scoped_totals(self.session, reseller_id=10))["revenue_total"], 0)
        self.assertEqual((await funnel_summary(self.session, reseller_id=10))["delivered"], 0)
        for _ in range(2):
            await set_demo_user(self.session, shop_staff(10), 11, is_demo=False)
        self.assertEqual((await build_finance_report(self.session, reseller_id=10))["active"]["revenue"], 100)
        self.assertEqual((await build_finance_report(self.session, reseller_id=20))["active"]["revenue"], 100)
        self.assertEqual((await self.session.get(BotUser, 11)).wallet_balance, 50)
        wallet = await self.session.scalar(sa.select(ShopWallet).where(ShopWallet.user_id == 11, ShopWallet.reseller_id == 10))
        self.assertEqual(wallet.balance, 80)
        for model, count in ((Order, 12), (Payment, 18), (WalletTransaction, 6), (UserService, 6)):
            self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(model)), count)

    async def test_new_activity_for_demo_customer_remains_excluded(self) -> None:
        self.session.add(Order(user_id=12, reseller_id=10, amount=10000, status="delivered", created_at=self.now))
        await self.session.commit()
        report = await build_finance_report(self.session, reseller_id=10)
        self.assertEqual(report["active"]["orders"], 2)
        self.assertEqual(report["active"]["revenue"], 100)

    async def test_missing_permission_customer_and_scopeless_staff_are_denied(self) -> None:
        for staff in (shop_staff(10, has_permission=False), {"role": "user", "bot_user_id": 11}, {"role": "admin"}, {"role": "reseller", "permissions": ["demo_users"]}):
            with self.subTest(staff=staff), self.assertRaises((PermissionError, ShopScopeError)):
                await set_demo_user(self.session, staff, 11, is_demo=True)
            self.assertFalse((await self.session.get(BotUser, 11)).is_demo)

    async def test_shop_cannot_change_other_shop_or_platform_customer(self) -> None:
        for user_id in (1, 21):
            with self.assertRaises(ValueError):
                await set_demo_user(self.session, shop_staff(10), user_id, is_demo=True)
            self.assertFalse((await self.session.get(BotUser, user_id)).is_demo)
        with self.assertRaises(ValueError):
            await set_demo_user(self.session, OWNER, 11, is_demo=True)
        self.assertFalse((await self.session.get(BotUser, 11)).is_demo)

    async def test_invalid_identifiers_and_states_are_rejected(self) -> None:
        for user_id in (True, -1, 0, 2**31, "11"):
            with self.assertRaises(ValueError):
                await set_demo_user(self.session, shop_staff(10), user_id, is_demo=True)
        for state in (1, "true", None):
            with self.assertRaises(ValueError):
                await set_demo_user(self.session, shop_staff(10), 11, is_demo=state)

    async def test_post_saves_and_redirects_back_to_user_modal(self) -> None:
        response = await self.client.post("/users/1/demo", data={"is_demo": "1", "csrf_token": "fake-csrf"})
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/users?edit=1&ok="))
        self.assertTrue((await self.session.get(BotUser, 1)).is_demo)

    async def test_post_rejects_wrong_shop_missing_permission_and_malformed_state(self) -> None:
        for staff, user_id, state in ((shop_staff(10), 21, "1"), (shop_staff(10, has_permission=False), 11, "1"), (shop_staff(10), 11, "true")):
            self.staff = staff
            response = await self.client.post(f"/users/{user_id}/demo", data={"is_demo": state})
            self.assertEqual(response.status_code, 303)
            self.assertIn("&err=", response.headers["location"])
            self.assertFalse((await self.session.get(BotUser, user_id)).is_demo)

    async def test_edit_form_shows_state_csrf_and_escapes_customer_text(self) -> None:
        with patch("app.services.bot_user_admin.list_service_snapshots", AsyncMock(return_value=[])), patch("app.services.bot_user_admin.list_wallet_txs", AsyncMock(return_value=[])), patch("app.services.plans_catalog.list_catalog_plans", AsyncMock(return_value=[])):
            response = await self.client.get("/users/2/edit?fragment=1")
            self.assertEqual(response.status_code, 200)
            self.assertIn('action="/users/2/demo"', response.text)
            self.assertIn('name="csrf_token" value="fake-csrf"', response.text)
            self.assertIn('value="1" selected', response.text)
            self.assertIn('&lt;script&gt;', response.text)
            self.assertNotIn('<script>alert', response.text)
            self.staff = shop_staff(10, has_permission=False)
            response = await self.client.get("/users/11/edit?fragment=1")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('action="/users/11/demo"', response.text)
            response = await self.client.get("/users/21/edit?fragment=1")
            self.assertEqual(response.status_code, 403)


class DemoUserMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_preserve_existing_customers(self) -> None:
        spec = importlib.util.spec_from_file_location("demo_user_migration", ROOT / "alembic/versions/0041_demo_users.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY, full_name TEXT)"))
            connection.execute(sa.text("INSERT INTO bot_users VALUES (1, 'existing')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(connection.execute(sa.text("SELECT is_demo FROM bot_users")).scalar(), 0)
                connection.execute(sa.text("UPDATE bot_users SET is_demo = true"))
                self.assertIn("ix_bot_users_is_demo", {index["name"] for index in sa.inspect(connection).get_indexes("bot_users")})
                migration.downgrade()
                migration.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT full_name FROM bot_users")).scalar(), "existing")
                migration.upgrade()
                self.assertEqual(connection.execute(sa.text("SELECT is_demo FROM bot_users")).scalar(), 0)
        engine.dispose()
