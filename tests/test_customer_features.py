"""Customer support, refund accounting and campaign delivery boundaries."""
import asyncio
import importlib.util
import os
import secrets
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.customer_features import register_customer_features
from app.api.miniapp_pages import register_miniapp_pages
from app.db.models import Base, BotUser, CampaignRecipient, MarketingPreference, Order, Plan, ServiceAutomation, ServiceCancellation, ShopWallet, TargetedCampaign, UserService, WalletTransaction
from app.services.campaigns import audience_query, campaign_progress, change_campaign_state, create_campaign, process_campaign, set_marketing_preference
from app.services.service_cancellations import approve_cancellation, cancellation_status, reject_cancellation, request_cancellation
from app.services.service_renewals import assert_service_quota_available
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}


class FakePanel:
    def __init__(self):
        self.status = "active"
        self.fail = False
        self.writes = 0
        self.entered = None
        self.release = None

    async def get_user_by_id(self, user_id):
        return {"id": user_id, "status": self.status}

    async def set_disabled_by_id(self, user_id, disabled):
        self.writes += 1
        self.status = "disabled"
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.fail:
            raise TimeoutError("lost response")
        return {"id": user_id, "status": self.status}


class CustomerFeaturesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pg_url = os.environ.get("PGCLOCKBOT_CUSTOMER_TEST_URL")
        self.admin_engine = None
        if pg_url:
            self.schema = "pgclock_customer_test_" + secrets.token_hex(8)
            self.admin_engine = create_async_engine(pg_url)
            async with self.admin_engine.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            self.engine = create_async_engine(pg_url, connect_args={"server_settings": {"search_path": self.schema}})
        else:
            self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'features.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.token = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        self.now = datetime.now(timezone.utc)
        self.user = BotUser(id=1, telegram_id=1001, referral_code="customer", reseller_id=2, wallet_balance=50, created_at=self.now - timedelta(days=90))
        self.other = BotUser(id=4, telegram_id=1004, referral_code="other", reseller_id=3, wallet_balance=0, created_at=self.now - timedelta(days=90))
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([self.user, self.other,
            Plan(id=1, name="Paid", price=100, duration_days=30, data_limit_gb=10),
            Plan(id=2, name="Trial", price=0, duration_days=1, data_limit_gb=1, is_trial=True),
        ])
        await self.session.flush()
        self.session.add_all([
            Order(id=10, user_id=1, plan_id=2, amount=0, status="delivered", reseller_id=None, created_at=self.now - timedelta(days=60)),
            Order(id=20, user_id=1, plan_id=2, amount=0, status="delivered", reseller_id=2, created_at=self.now - timedelta(days=60)),
            Order(id=30, user_id=4, plan_id=2, amount=0, status="delivered", reseller_id=3, created_at=self.now - timedelta(days=60)),
        ])
        await self.session.flush()
        self.service = UserService(id=11, bot_user_id=1, pg_user_id=101, pg_username="mine", remark="order:10", subscription_url="https://vpn.example/sub/mine", quota_status="active", quota_synced_at=self.now, quota_expire_at=self.now + timedelta(days=2))
        self.shop_service = UserService(id=12, bot_user_id=1, pg_user_id=102, pg_username="shop-mine", remark="order:20", subscription_url="https://vpn.example/sub/shop-mine", quota_status="active", quota_synced_at=self.now, quota_expire_at=self.now + timedelta(days=2))
        self.session.add_all([self.service, self.shop_service])
        await self.session.commit()
        self.panel = FakePanel()
        self.patches = [patch("app.services.service_cancellations.get_pg", return_value=self.panel),
                        patch("app.services.service_cancellations.get_pg_for_reseller", AsyncMock(return_value=self.panel))]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.session.close()
        await self.engine.dispose()
        if self.admin_engine:
            async with self.admin_engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
            await self.admin_engine.dispose()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()
        self.tmp.cleanup()

    async def request(self, service_id=11, shop_id=None):
        return await request_cancellation(self.session, self.user, service_id, shop_id=shop_id, reason="دیگر نیاز ندارم")

    async def approve(self, request_id, amount=123, shop_id=None):
        return await approve_cancellation(self.session, request_id, shop_id=shop_id, amount=amount, actor="operator", note="تأیید شد")

    async def campaign(self, shop_id=None, audience="trial"):
        return await create_campaign(self.session, shop_id=shop_id, audience=audience, days=7, text="پیشنهاد فروشگاه", actor="operator")

    async def test_request_duplicate_keeps_single_open_request_and_service_active(self):
        first, second = await self.request(), await self.request()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(ServiceCancellation)), 1)
        self.assertEqual(self.panel.writes, 0)
        self.assertIsNone(first["refund_amount"])

    async def test_platform_refund_uses_origin_shop_not_sticky_user_shop(self):
        row = await self.request()
        await self.approve(row["id"])
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 173)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(ShopWallet)), 0)
        self.assertTrue(self.service.is_cancelled)
        self.assertFalse(self.service.cancellation_pending)

    async def test_shop_refund_stays_in_same_shop_and_is_idempotent(self):
        row = await self.request(12, 2)
        await self.approve(row["id"], shop_id=2)
        await self.approve(row["id"], shop_id=2)
        wallet = await self.session.scalar(select(ShopWallet).where(ShopWallet.user_id == 1, ShopWallet.reseller_id == 2))
        self.assertEqual(wallet.balance, 123)
        self.assertEqual(self.user.wallet_balance, 50)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(WalletTransaction)), 1)
        self.assertEqual(self.panel.writes, 1)

    async def test_zero_refund_approves_without_wallet_entry(self):
        row = await self.request()
        approved = await self.approve(row["id"], amount=0)
        self.assertEqual(approved.status, "approved")
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(WalletTransaction)), 0)

    async def test_lost_panel_response_retains_amount_blocks_quota_and_retry_does_not_disable_twice(self):
        row = await self.request()
        self.panel.fail = True
        with self.assertRaises(ValueError):
            await self.approve(row["id"])
        async with self.Session() as other:
            request = await other.get(ServiceCancellation, row["id"])
            self.assertEqual(request.status, "review")
            self.assertEqual(request.refund_amount, 123)
            self.assertEqual(await other.scalar(select(func.count()).select_from(WalletTransaction)), 0)
            with self.assertRaises(ValueError):
                await assert_service_quota_available(other, 11)
        with self.assertRaises(ValueError):
            await self.approve(row["id"], amount=124)
        self.panel.fail = False
        await self.approve(row["id"])
        self.assertEqual(self.panel.writes, 1)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(WalletTransaction)), 1)

    async def test_open_paid_order_prevents_refund_and_disabling(self):
        row = await self.request()
        self.session.add(Order(user_id=1, plan_id=1, amount=100, status="paid", note="renew:11", service_id=11))
        await self.session.commit()
        with self.assertRaises(ValueError):
            await self.approve(row["id"])
        request = await self.session.get(ServiceCancellation, row["id"])
        self.assertEqual(request.status, "pending")
        self.assertEqual(self.panel.writes, 0)

    async def test_parallel_approval_is_exclusive_and_quota_creation_is_blocked(self):
        row = await self.request()
        self.panel.entered, self.panel.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.approve(row["id"]))
        await self.panel.entered.wait()
        try:
            async with self.Session() as other:
                with self.assertRaises(ValueError):
                    await approve_cancellation(other, row["id"], shop_id=None, amount=123, actor="other")
                with self.assertRaises(ValueError):
                    await assert_service_quota_available(other, 11)
        finally:
            self.panel.release.set()
            await task
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(WalletTransaction)), 1)

    async def test_approval_transaction_failure_keeps_refund_retryable(self):
        row = await self.request()
        with patch("app.services.service_cancellations.credit_wallet", AsyncMock(side_effect=RuntimeError("database error"))):
            with self.assertRaises(ValueError):
                await self.approve(row["id"])
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(WalletTransaction)), 0)
        await self.approve(row["id"])
        self.assertEqual(self.panel.writes, 1)

    async def test_rejection_allows_new_request_and_does_not_change_wallet(self):
        row = await self.request()
        await reject_cancellation(self.session, row["id"], shop_id=None, actor="operator", note="دلیل رد")
        new = await self.request()
        self.assertNotEqual(row["id"], new["id"])
        self.assertEqual(self.panel.writes, 0)

    async def test_customer_and_staff_shop_boundaries_fail_closed(self):
        for uid, sid, shop in [(4, 11, None), (1, 11, 2), (1, 12, None)]:
            user = await self.session.get(BotUser, uid)
            with self.subTest(uid=uid, sid=sid), self.assertRaises(ValueError):
                await request_cancellation(self.session, user, sid, shop_id=shop, reason="test")
        row = await self.request(12, 2)
        for shop in [None, 3]:
            with self.subTest(shop=shop), self.assertRaises(ValueError):
                await self.approve(row["id"], shop_id=shop)
        self.assertEqual(self.panel.writes, 0)

    async def test_changed_panel_identity_is_not_disabled_or_refunded(self):
        row = await self.request()
        self.service.pg_user_id = 999
        await self.session.commit()
        with self.assertRaises(ValueError):
            await self.approve(row["id"])
        self.assertEqual(self.panel.writes, 0)

    async def test_transferred_service_does_not_expose_previous_customer_request(self):
        await self.request()
        self.service.bot_user_id = 4
        self.service.remark = "order:30"
        await self.session.commit()
        self.assertIsNone(await cancellation_status(self.session, self.other, 11, shop_id=3))
        with self.assertRaises(ValueError):
            await request_cancellation(self.session, self.other, 11, shop_id=3, reason="درخواست جدید")
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(ServiceCancellation)), 1)

    async def test_approved_cancellation_disables_automation_and_blocks_rewards(self):
        self.session.add(ServiceAutomation(service_id=11, renew_enabled=True, volume_enabled=True))
        await self.session.commit()
        row = await self.request()
        await self.approve(row["id"])
        auto = await self.session.get(ServiceAutomation, 11)
        self.assertFalse(auto.renew_enabled or auto.volume_enabled)
        from app.services.loyalty import apply_service_reward
        with self.assertRaises(ValueError):
            await apply_service_reward(self.session, self.user, self.service, "time_days", 1)

    async def test_invalid_refund_amounts_are_rejected(self):
        row = await self.request()
        for amount in [-1, True, 2.5, 2**31]:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                await self.approve(row["id"], amount=amount)

    async def test_wallet_capacity_is_checked_before_panel_mutation(self):
        row = await self.request()
        self.user.wallet_balance = 2**31 - 10
        await self.session.commit()
        with self.assertRaises(ValueError):
            await self.approve(row["id"], amount=20)
        self.assertEqual(self.panel.writes, 0)

    async def test_campaign_audience_is_scoped_to_purchase_origin(self):
        for scope, expected in [(None, [1]), (2, [1]), (3, [4])]:
            ids = list((await self.session.scalars(audience_query(shop_id=scope, audience="trial", days=7))).all())
            self.assertEqual(ids, expected)

    async def test_paid_conversion_excludes_trial_only_in_its_own_shop(self):
        self.session.add(Order(user_id=1, plan_id=1, amount=100, status="delivered", reseller_id=None))
        await self.session.commit()
        self.assertEqual(list((await self.session.scalars(audience_query(shop_id=None, audience="trial", days=7))).all()), [])
        self.assertEqual(list((await self.session.scalars(audience_query(shop_id=2, audience="trial", days=7))).all()), [1])

    async def test_expiring_requires_fresh_quota_and_ignores_foreign_services(self):
        self.service.quota_synced_at = self.now - timedelta(days=2)
        await self.session.commit()
        self.assertEqual(list((await self.session.scalars(audience_query(shop_id=None, audience="expiring", days=7))).all()), [])
        self.assertEqual(list((await self.session.scalars(audience_query(shop_id=2, audience="expiring", days=7))).all()), [1])

    async def test_on_hold_service_is_not_targeted_as_expiring(self):
        self.service.quota_status = "on_hold"
        await self.session.commit()
        self.assertEqual(list((await self.session.scalars(audience_query(shop_id=None, audience="expiring", days=7))).all()), [])

    async def test_inactive_uses_local_purchase_history(self):
        self.session.add(Order(user_id=1, plan_id=1, amount=100, status="delivered", reseller_id=2))
        await self.session.commit()
        self.assertEqual(list((await self.session.scalars(audience_query(shop_id=None, audience="inactive", days=30))).all()), [1])
        self.assertEqual(list((await self.session.scalars(audience_query(shop_id=2, audience="inactive", days=30))).all()), [])

    async def test_campaign_stays_draft_until_started_and_sends_only_once(self):
        row = await self.campaign()
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=50)))
        await process_campaign(self.session, row.id, bot, delay=0)
        bot.send_message.assert_not_awaited()
        await change_campaign_state(self.session, row.id, shop_id=None, action="start")
        await process_campaign(self.session, row.id, bot, delay=0)
        await process_campaign(self.session, row.id, bot, delay=0)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.args[0], 1001)
        self.assertEqual(await campaign_progress(self.session, row.id), {"sent": 1})

    async def test_optout_is_shop_scoped_and_checked_again_before_send(self):
        row = await self.campaign()
        await set_marketing_preference(self.session, 1, shop_id=None, enabled=False)
        bot = SimpleNamespace(send_message=AsyncMock())
        await change_campaign_state(self.session, row.id, shop_id=None, action="start")
        await process_campaign(self.session, row.id, bot, delay=0)
        bot.send_message.assert_not_awaited()
        self.assertEqual(await campaign_progress(self.session, row.id), {"skipped": 1})
        self.assertEqual(list((await self.session.scalars(audience_query(shop_id=2, audience="trial", days=7))).all()), [1])
        await set_marketing_preference(self.session, 1, shop_id=None, enabled=True)
        self.assertTrue((await self.session.get(MarketingPreference, (1, 0))).enabled)

    async def test_lost_telegram_response_is_not_sent_twice(self):
        row = await self.campaign()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError("unknown delivery")))
        await change_campaign_state(self.session, row.id, shop_id=None, action="start")
        await process_campaign(self.session, row.id, bot, delay=0)
        await process_campaign(self.session, row.id, bot, delay=0)
        self.assertEqual(await campaign_progress(self.session, row.id), {"review": 1})
        bot.send_message.assert_awaited_once()

    async def test_restart_recovers_interrupted_send_without_resending(self):
        row = await self.campaign()
        recipient = await self.session.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == row.id))
        recipient.status = "sending"
        row.status = "running"
        await self.session.commit()
        bot = SimpleNamespace(send_message=AsyncMock())
        await process_campaign(self.session, row.id, bot, delay=0)
        bot.send_message.assert_not_awaited()
        self.assertEqual(await campaign_progress(self.session, row.id), {"review": 1})

    async def test_foreign_shop_cannot_start_or_pause_campaign(self):
        row = await self.campaign(shop_id=2)
        campaign_id = row.id
        for shop in [None, 3]:
            with self.subTest(shop=shop), self.assertRaises(ValueError):
                await change_campaign_state(self.session, campaign_id, shop_id=shop, action="start")
            await self.session.rollback()

    async def test_parallel_workers_do_not_duplicate_send(self):
        row = await self.campaign()
        await change_campaign_state(self.session, row.id, shop_id=None, action="start")
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=3)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        task = asyncio.create_task(process_campaign(self.session, row.id, bot, delay=0))
        await entered.wait()
        try:
            async with self.Session() as other:
                await process_campaign(other, row.id, bot, delay=0)
        finally:
            release.set()
            await task
        bot.send_message.assert_awaited_once()

    async def test_pause_resume_only_sends_remaining_snapshot_members(self):
        row = await self.campaign()
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
        await change_campaign_state(self.session, row.id, shop_id=None, action="start")
        await change_campaign_state(self.session, row.id, shop_id=None, action="pause")
        await process_campaign(self.session, row.id, bot, delay=0)
        bot.send_message.assert_not_awaited()
        await change_campaign_state(self.session, row.id, shop_id=None, action="start")
        await process_campaign(self.session, row.id, bot, delay=0)
        self.assertEqual(await campaign_progress(self.session, row.id), {"sent": 1})

    async def test_telegram_rate_limit_preserves_queue_and_delays_retry(self):
        from aiogram.exceptions import TelegramRetryAfter
        from aiogram.methods import SendMessage
        row = await self.campaign()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramRetryAfter(method=SendMessage(chat_id=1001, text="message"), message="retry", retry_after=30)))
        await change_campaign_state(self.session, row.id, shop_id=None, action="start")
        await process_campaign(self.session, row.id, bot, delay=0)
        await process_campaign(self.session, row.id, bot, delay=0)
        self.assertEqual(await campaign_progress(self.session, row.id), {"pending": 1})
        bot.send_message.assert_awaited_once()
        await self.session.execute(update(TargetedCampaign).where(TargetedCampaign.id == row.id).values(locked_until=self.now - timedelta(seconds=1)))
        await self.session.commit()
        bot.send_message.side_effect = None
        bot.send_message.return_value = SimpleNamespace(message_id=42)
        await process_campaign(self.session, row.id, bot, delay=0)
        self.assertEqual(await campaign_progress(self.session, row.id), {"sent": 1})

    async def test_lease_transfer_never_repeats_an_inflight_send(self):
        row = await self.campaign()
        campaign_id = row.id
        await change_campaign_state(self.session, campaign_id, shop_id=None, action="start")
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=3)
        first = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        second = SimpleNamespace(send_message=AsyncMock())
        task = asyncio.create_task(process_campaign(self.session, campaign_id, first, delay=0))
        await entered.wait()
        try:
            async with self.Session() as other:
                await other.execute(update(TargetedCampaign).where(TargetedCampaign.id == campaign_id).values(locked_until=self.now - timedelta(seconds=1)))
                await other.commit()
                await process_campaign(other, campaign_id, second, delay=0)
        finally:
            release.set()
            await task
        second.send_message.assert_not_awaited()
        first.send_message.assert_awaited_once()

    def panel_app(self, staff):
        app = FastAPI()
        async def db():
            yield self.session
        async def auth():
            return staff
        env = Environment(loader=FileSystemLoader(str(ROOT / "app/web/templates")), autoescape=select_autoescape())
        def render(request, template, data):
            # The standalone route harness verifies content and escaping without panel globals.
            source = (ROOT / "app/web/templates/customer_features.html").read_text().replace('{% extends "base.html" %}', '')
            return HTMLResponse(env.from_string(source).render(request=request, csrf_token="test", **data))
        register_customer_features(app, render=render, require_staff=auth, get_db=db)
        return app

    async def test_panel_pages_deny_bare_admin_and_missing_scope(self):
        for staff in [{"role": "admin"}, {"role": "reseller", "web_permissions": "orders,shop_settings"}, {"role": "pg_staff"}]:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.panel_app(staff)), base_url="http://test") as client:
                for url in ["/service-cancellations", "/campaigns"]:
                    self.assertEqual((await client.get(url)).status_code, 403)

    async def test_panel_shop_filter_and_preview_escape_customer_content(self):
        await self.request()
        row = await self.request(12, 2)
        obj = await self.session.get(ServiceCancellation, row["id"])
        obj.reason = '<script>alert("x")</script>'
        await self.session.commit()
        staff = {"role": "reseller", "bot_user_id": 2, "permissions": ["orders", "shop_settings", "campaigns"]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.panel_app(staff)), base_url="http://test") as client:
            redirect = await client.get("/service-cancellations", follow_redirects=False)
            self.assertEqual(redirect.status_code, 303)
            self.assertIn("/finance?tab=cancellations", redirect.headers.get("location", ""))
            self.assertEqual((await client.get("/campaigns")).status_code, 200)
        from app.services.service_cancellations import list_cancellation_requests
        rows, _ = await list_cancellation_requests(self.session, 2, limit=50)
        self.assertTrue(any(r.id == row["id"] for r, _ in rows))
        self.assertTrue(all(uname != "mine" for _, uname in rows) or any(uname == "shop-mine" for _, uname in rows))
        env = Environment(loader=FileSystemLoader(str(ROOT / "app/web/templates")), autoescape=select_autoescape())
        html = env.get_template("_finance_cancellations.html").render(
            cancel_requests=rows,
            cancel_labels={"pending": "در انتظار بررسی", "processing": "…", "review": "…", "approved": "…", "rejected": "…", "withdrawn": "…"},
            can_approve_cancel=False,
            cancel_next_before=None,
            cancel_flash_err=None,
            csrf_token="test",
        )
        self.assertIn("shop-mine", html)
        self.assertNotIn(">mine<", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert", html)

    async def test_campaign_and_refund_require_their_own_permissions(self):
        row = await self.request(12, 2)
        staff = {"role": "reseller", "bot_user_id": 2, "permissions": ["orders", "shop_settings"]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.panel_app(staff)), base_url="http://test") as client:
            self.assertEqual((await client.get("/campaigns")).status_code, 403)
            self.assertEqual((await client.post("/campaigns", data={"audience": "trial", "days": "7", "text": "test"})).status_code, 403)
            self.assertEqual((await client.post(f"/service-cancellations/{row['id']}/approve", data={"amount": "123"})).status_code, 403)
        self.assertEqual(self.panel.writes, 0)

    async def test_mini_api_requires_customer_ownership_and_platform_cancellation_scope(self):
        app = FastAPI()
        async def db():
            yield self.session
        register_miniapp_pages(app, render=lambda *args: None, get_db=db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("app.api.miniapp_pages.load_mini_user", AsyncMock(return_value=self.other)), patch("app.api.miniapp_pages.assert_mini_force_join", AsyncMock()):
                self.assertEqual((await client.post("/api/mini/service/11/cancellation", json={"reason": "test"})).status_code, 404)
            async def load_customer(*args):
                return await self.session.get(BotUser, 1)
            with patch("app.api.miniapp_pages.load_mini_user", AsyncMock(side_effect=load_customer)), patch("app.api.miniapp_pages.assert_mini_force_join", AsyncMock()):
                self.assertEqual((await client.post("/api/mini/service/12/cancellation", json={"reason": "test"})).status_code, 400)
                self.assertEqual((await client.post("/api/mini/service/11/cancellation", json={"reason": "test"})).status_code, 200)
                self.assertEqual((await client.post("/api/mini/service/11/cancellation", json=[])).status_code, 400)


class CustomerFeatureMigrationTests(unittest.TestCase):
    def test_sqlite_upgrade_repeat_and_downgrade_preserve_existing_service(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        spec = importlib.util.spec_from_file_location("customer_migration", ROOT / "alembic/versions/0039_customer_features.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, pg_username TEXT)"))
            conn.execute(text("INSERT INTO user_services VALUES (1, 'old-service')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(conn.execute(text("SELECT cancellation_pending, is_cancelled FROM user_services")).one(), (0, 0))
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT pg_username FROM user_services")).scalar(), "old-service")
                migration.upgrade()
                self.assertIn("marketing_preferences", sa.inspect(conn).get_table_names())
        engine.dispose()

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_miniapp_cancellation_interactions(self):
        result = subprocess.run([shutil.which("node"), str(ROOT / "tests/miniapp_customer_features.test.cjs")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
