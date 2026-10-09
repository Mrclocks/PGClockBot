"""Purchase contact enforcement and isolated, repeat-safe cancellation notices."""
from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import sqlalchemy as sa
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.miniapp_pages import register_miniapp_pages
from app.api.customer_features import register_customer_features
from app.bot.handlers.purchase_contact import PurchaseContactStates, prompt_purchase_contact_if_needed, receive_purchase_contact
from app.bot.handlers.shop import shop_buy
from app.db.models import Base, BotUser, Order, Payment, Plan, PurchaseContact, ResellerProfile, ServiceCancellation, UserService
from app.jobs.cancellation_notifications import _send_notice
from app.services.cancellation_notifications import CancellationNotice, NoticeOutcome, process_cancellation_notifications
from app.services.orders import create_custom_order, create_order, create_wholesale_order, mark_order_free_paid, pay_with_wallet, start_card_payment
from app.services.purchase_contact import purchase_contact_needed, verify_purchase_contact
from app.services.service_cancellations import request_cancellation
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_setting, set_shop_reseller_id


class ContactAndNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'contacts.db'}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.scope = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        self.user = BotUser(id=1, telegram_id=1001, referral_code="customer", reseller_id=2, wallet_balance=1000)
        self.other = BotUser(id=4, telegram_id=1004, referral_code="other", reseller_id=3)
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            self.user, self.other,
            Plan(id=1, name="Paid", price=100, duration_days=30, data_limit_gb=10),
            Plan(id=2, name="Trial", price=0, duration_days=1, data_limit_gb=1, is_trial=True),
            ResellerProfile(user_id=2, web_username="shop2", web_permissions="orders,shop_settings", is_active=True),
            ResellerProfile(user_id=3, web_username="shop3", web_permissions="orders,shop_settings", is_active=True),
        ])
        await self.session.flush()
        self.session.add_all([
            Order(id=10, user_id=1, plan_id=1, amount=100, status="delivered"),
            Order(id=20, user_id=1, plan_id=1, amount=100, status="delivered", reseller_id=2),
            UserService(id=11, bot_user_id=1, pg_user_id=101, pg_username="<mine>", remark="order:10"),
            UserService(id=12, bot_user_id=1, pg_user_id=102, pg_username="shop-mine", remark="order:20"),
        ])
        await self.session.commit()
        self.config = SimpleNamespace(web_secret="purchase-contact-test-secret", admin_ids=[9001], public_base_url="https://example.test")
        self.settings_patch = patch("app.services.purchase_contact.get_settings", return_value=self.config)
        self.settings_patch.start()
        self.main_bot = SimpleNamespace(send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
        self.shop_bot = SimpleNamespace(send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
        self.sleep = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        await self.session.close()
        await self.engine.dispose()
        reset_shop_reseller_id(self.scope)
        _SETTINGS_CACHE.clear()
        self.tmp.cleanup()

    async def verify(self, user: BotUser | None = None) -> None:
        user = user or self.user
        await verify_purchase_contact(self.session, user, contact_user_id=user.telegram_id, phone_number="+989121234567")

    async def test_contact_requirement_is_off_by_default(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        self.assertEqual(order.amount, 100)
        self.assertFalse(await purchase_contact_needed(self.session, 1))

    async def test_contact_gate_prevents_all_new_subscription_orders(self) -> None:
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        for create in (
            lambda: create_order(self.session, user_id=1, plan_id=1),
            lambda: create_custom_order(self.session, user_id=1, data_limit_gb=10, duration_days=30),
            lambda: create_wholesale_order(self.session, user_id=1, plan_id=1, quantity=3),
        ):
            with self.subTest(create=create), self.assertRaisesRegex(ValueError, "verify_phone"):
                await create()
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(Order)), 2)
        self.assertEqual(self.user.wallet_balance, 1000)

    async def test_verifying_own_contact_allows_purchase_once_without_raw_phone_storage(self) -> None:
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        await self.verify()
        await self.verify()
        self.assertFalse(await purchase_contact_needed(self.session, 1))
        row = await self.session.get(PurchaseContact, (1, 0))
        self.assertEqual(len(row.phone_hash), 64)
        self.assertNotIn("9121234567", row.phone_hash)
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(PurchaseContact)), 1)
        self.assertEqual((await create_order(self.session, user_id=1, plan_id=1)).amount, 100)

    async def test_another_customer_contact_and_invalid_phone_are_rejected(self) -> None:
        for contact_id, phone in [(1004, "+989121234567"), (None, "+989121234567"), (1001, "bad"), (1001, "9" * 16)]:
            with self.subTest(contact_id=contact_id, phone=phone), self.assertRaises(ValueError):
                await verify_purchase_contact(self.session, self.user, contact_user_id=contact_id, phone_number=phone)
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(PurchaseContact)), 0)

    async def test_concurrent_contact_verification_keeps_one_record(self) -> None:
        async def verify(session: AsyncSession) -> None:
            user = await session.get(BotUser, 1)
            await verify_purchase_contact(session, user, contact_user_id=1001, phone_number="+989121234567")

        async with self.Session() as first, self.Session() as second:
            await asyncio.gather(verify(first), verify(second))
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(PurchaseContact)), 1)

    async def test_phone_verification_does_not_cross_shops_or_customers(self) -> None:
        for shop_id in (0, 2, 3):
            await set_setting(self.session, "purchase_require_contact", "1", reseller_id=shop_id)
        await self.verify()
        self.assertTrue(await purchase_contact_needed(self.session, 4))
        for shop_id in (2, 3):
            token = set_shop_reseller_id(shop_id)
            try:
                self.assertTrue(await purchase_contact_needed(self.session, 1))
                await self.verify()
                self.assertFalse(await purchase_contact_needed(self.session, 1))
                self.assertTrue(await purchase_contact_needed(self.session, 4))
            finally:
                reset_shop_reseller_id(token)
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(PurchaseContact)), 3)

    async def test_platform_contact_toggle_does_not_enable_it_for_shop_customers(self) -> None:
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        token = set_shop_reseller_id(2)
        try:
            self.assertFalse(await purchase_contact_needed(self.session, 1))
        finally:
            reset_shop_reseller_id(token)

    async def test_trial_keeps_its_separate_contact_requirement(self) -> None:
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        self.assertEqual((await create_order(self.session, user_id=1, plan_id=2)).amount, 0)
        await set_setting(self.session, "trial_require_contact", "1", reseller_id=0)
        with self.assertRaisesRegex(ValueError, "تست"):
            await create_order(self.session, user_id=4, plan_id=2)

    async def test_existing_unpaid_order_cannot_bypass_new_contact_toggle(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        for checkout in (lambda: pay_with_wallet(self.session, order, self.user), lambda: start_card_payment(self.session, order, 1)):
            with self.assertRaisesRegex(ValueError, "verify_phone"):
                await checkout()
        self.assertEqual(self.user.wallet_balance, 1000)
        self.assertEqual(order.status, "pending")
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(Payment)), 0)

    async def test_free_nontrial_order_requires_contact_before_payment(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        order.amount = 0
        await self.session.commit()
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        with self.assertRaisesRegex(ValueError, "verify_phone"):
            await mark_order_free_paid(self.session, order, 1)

    async def test_delivered_free_order_remains_idempotent_when_contact_toggle_changes(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        order.amount, order.status = 0, "delivered"
        await self.session.commit()
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        self.assertEqual((await mark_order_free_paid(self.session, order, 1)).id, order.id)
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(Payment)), 0)
    async def test_contact_flow_preserves_custom_selection_and_resumes_purchase(self) -> None:
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=123, chat_id=1001, user_id=1001))
        await state.update_data(custom_gb=25, custom_days=60)
        message = SimpleNamespace(answer=AsyncMock(), contact=SimpleNamespace(user_id=1001, phone_number="+989121234567"))
        callback = SimpleNamespace(data="shop:custom:buy", message=message, answer=AsyncMock())
        self.assertTrue(await prompt_purchase_contact_if_needed(callback, self.session, self.user, state, {"purchase_require_contact": "1"}))
        self.assertEqual(await state.get_state(), PurchaseContactStates.contact.state)
        await receive_purchase_contact(message, self.session, self.user, state)
        self.assertIsNone(await state.get_state())
        self.assertEqual((await state.get_data())["custom_gb"], 25)
        markup = message.answer.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "shop:custom:buy")
        self.assertFalse(await prompt_purchase_contact_if_needed(callback, self.session, self.user, state, {"purchase_require_contact": "1"}))
        await storage.close()

    async def test_shop_buy_prompts_before_creating_order(self) -> None:
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=123, chat_id=1001, user_id=1001))
        callback = SimpleNamespace(data="shop:buy:1", message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
        with patch("app.bot.handlers.shop._complete_shop_buy", new_callable=AsyncMock) as complete:
            await shop_buy(callback, self.session, self.user, state)
            complete.assert_not_awaited()
            self.assertEqual(await state.get_state(), PurchaseContactStates.contact.state)
        await storage.close()

    async def test_miniapp_purchase_cannot_bypass_contact_gate(self) -> None:
        await set_setting(self.session, "purchase_require_contact", "1", reseller_id=0)
        app = FastAPI()

        async def db() -> AsyncSession:
            return self.session

        register_miniapp_pages(app, render=None, get_db=db)
        with patch("app.api.miniapp_pages.load_mini_user", AsyncMock(return_value=self.user)), patch("app.api.miniapp_pages._require_commerce_ready", AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test") as client:
                response = await client.post("/api/mini/buy", json={"plan_id": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("verify_phone", response.json()["detail"])
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(Payment)), 0)
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 1000)

    async def request(self, service_id: int = 11, shop_id: int | None = None) -> int:
        row = await request_cancellation(self.session, self.user, service_id, shop_id=shop_id, reason="<b>دیگر نیاز ندارم</b>")
        return row["id"]

    async def test_repeated_cancellation_notifies_once_after_committed_claim(self) -> None:
        request_id = await self.request()
        self.assertEqual(await self.request(), request_id)
        sent = []

        async def send(notice: CancellationNotice) -> NoticeOutcome:
            self.assertFalse(self.session.in_transaction())
            async with self.Session() as other:
                self.assertEqual((await other.get(ServiceCancellation, request_id)).notification_status, "sending")
            sent.append(notice)
            return "sent"

        await process_cancellation_notifications(self.session, send)
        await process_cancellation_notifications(self.session, send)
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0].shop_id)
        self.assertIn("&lt;mine&gt;", sent[0].text)
        self.assertIn("&lt;b&gt;", sent[0].text)
        self.assertNotIn("<b>دیگر", sent[0].text)

    async def test_concurrent_workers_do_not_send_the_same_notice_twice(self) -> None:
        await self.request()
        await self.session.commit()
        send = AsyncMock(return_value="sent")
        async with self.Session() as first, self.Session() as second:
            await asyncio.gather(process_cancellation_notifications(first, send), process_cancellation_notifications(second, send))
        self.assertEqual(send.await_count, 1)

    async def test_unknown_notice_outcome_is_not_retried_and_other_items_continue(self) -> None:
        first = await self.request()
        second = await self.request(12, 2)
        send = AsyncMock(side_effect=[TimeoutError("lost response"), "sent"])
        await process_cancellation_notifications(self.session, send)
        await process_cancellation_notifications(self.session, send)
        self.assertEqual(send.await_count, 2)
        await self.session.refresh(await self.session.get(ServiceCancellation, first))
        self.assertEqual((await self.session.get(ServiceCancellation, first)).notification_status, "review")
        self.assertEqual((await self.session.get(ServiceCancellation, second)).notification_status, "sent")

    async def test_stale_sending_notice_moves_to_review_without_resend(self) -> None:
        request_id = await self.request()
        row = await self.session.get(ServiceCancellation, request_id)
        row.notification_status = "sending"
        row.notification_attempted_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        await self.session.commit()
        send = AsyncMock(return_value="sent")
        await process_cancellation_notifications(self.session, send)
        send.assert_not_awaited()
        await self.session.refresh(row)
        self.assertEqual(row.notification_status, "review")

    async def test_platform_notice_uses_platform_bot_despite_sticky_reseller(self) -> None:
        request_id = await self.request()
        await self.dispatch_notices()
        self.main_bot.send_message.assert_awaited_once()
        self.assertEqual(self.main_bot.send_message.call_args.args[0], 9001)
        self.shop_bot.send_message.assert_not_awaited()
        await self.session.refresh(await self.session.get(ServiceCancellation, request_id))
        self.assertEqual((await self.session.get(ServiceCancellation, request_id)).notification_status, "sent")

    async def test_shop_notice_uses_own_bot_and_does_not_inherit_platform_toggle(self) -> None:
        await set_setting(self.session, "notify_service_cancellation", "0", reseller_id=0)
        await self.request(12, 2)
        await self.dispatch_notices()
        self.main_bot.send_message.assert_not_awaited()
        self.shop_bot.send_message.assert_awaited_once()
        self.assertEqual(self.shop_bot.send_message.call_args.args[0], 1002)

    async def test_shop_notice_toggle_and_orders_permission_are_enforced(self) -> None:
        request_id = await self.request(12, 2)
        await set_setting(self.session, "notify_service_cancellation", "0", reseller_id=2)
        await self.dispatch_notices()
        self.shop_bot.send_message.assert_not_awaited()
        row = await self.session.get(ServiceCancellation, request_id)
        await self.session.refresh(row)
        self.assertEqual(row.notification_status, "skipped")
        await set_setting(self.session, "notify_service_cancellation", "1", reseller_id=2)
        profile = await self.session.scalar(sa.select(ResellerProfile).where(ResellerProfile.user_id == 2))
        profile.web_permissions = "tickets"
        await self.session.commit()
        notice = CancellationNotice(request_id, 2, "درخواست لغو")
        outcome = await self.dispatch_notice(notice)
        self.assertEqual(outcome, "pending")
        self.shop_bot.send_message.assert_not_awaited()

    async def test_telegram_timeout_does_not_resend_notice(self) -> None:
        request_id = await self.request()
        self.main_bot.send_message.side_effect = TimeoutError("lost response")
        await self.dispatch_notices()
        await self.dispatch_notices()
        self.assertEqual(self.main_bot.send_message.await_count, 1)
        row = await self.session.get(ServiceCancellation, request_id)
        await self.session.refresh(row)
        self.assertEqual(row.notification_status, "review")

    async def test_rate_limit_retries_only_the_rejected_recipient_after_delay(self) -> None:
        await self.request()
        self.config.admin_ids = [9001, 9002]
        self.main_bot.send_message.side_effect = [None, TelegramRetryAfter(
            method=SendMessage(chat_id=9002, text="درخواست لغو"), message="rate limited", retry_after=2,
        ), None]
        await self.dispatch_notices()
        await self.dispatch_notices()
        self.assertEqual([call.args[0] for call in self.main_bot.send_message.await_args_list], [9001, 9002, 9002])
        self.assertIn((2,), [call.args for call in self.sleep.await_args_list])

    async def test_unavailable_bot_does_not_block_next_new_notice(self) -> None:
        await self.request()
        send = AsyncMock(return_value="pending")
        await process_cancellation_notifications(self.session, send)
        second = await self.request(12, 2)
        send.return_value = "sent"
        await process_cancellation_notifications(self.session, send)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(send.call_args.args[0].request_id, second)

    async def test_wrong_shop_or_customer_cannot_queue_cancellation_notice(self) -> None:
        for user, service_id, shop_id in [(self.other, 11, None), (self.user, 12, 3)]:
            with self.assertRaises(ValueError):
                await request_cancellation(self.session, user, service_id, shop_id=shop_id, reason="درخواست")
        self.assertEqual(await self.session.scalar(sa.select(sa.func.count()).select_from(ServiceCancellation)), 0)

    async def test_notice_review_status_is_visible_in_the_panel(self) -> None:
        request_id = await self.request()
        row = await self.session.get(ServiceCancellation, request_id)
        row.notification_status = "review"
        row.reason = "<b>xss</b>"
        await self.session.commit()
        app = FastAPI()

        async def db() -> AsyncSession:
            yield self.session

        async def auth() -> dict[str, object]:
            return {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}

        register_customer_features(
            app, render=lambda *a, **k: HTMLResponse(""), require_staff=auth, get_db=db
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test") as client:
            redirected = await client.get("/service-cancellations", follow_redirects=False)
        self.assertEqual(redirected.status_code, 303)
        self.assertIn("/finance?tab=cancellations", redirected.headers.get("location", ""))

        from app.services.service_cancellations import STATUS_LABELS, list_cancellation_requests

        rows, _ = await list_cancellation_requests(self.session, None, limit=50)
        templates = Environment(
            loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app/web/templates")),
            autoescape=select_autoescape(),
        )
        html = templates.get_template("_finance_cancellations.html").render(
            cancel_requests=rows,
            cancel_labels=STATUS_LABELS,
            can_approve_cancel=True,
            cancel_next_before=None,
            cancel_flash_err=None,
            csrf_token="test",
        )
        self.assertIn("اعلان ربات: نیازمند بررسی", html)
        self.assertIn("&lt;b&gt;", html)

    async def dispatch_notice(self, notice: CancellationNotice) -> NoticeOutcome:
        with patch("app.jobs.cancellation_notifications.SessionLocal", self.Session), patch("app.jobs.cancellation_notifications.get_settings", return_value=self.config), patch("app.jobs.cancellation_notifications.open_notify_bot_for_reseller", AsyncMock(return_value=(self.shop_bot, False))), patch("app.jobs.cancellation_notifications.asyncio.sleep", self.sleep):
            return await _send_notice(self.main_bot, notice)

    async def dispatch_notices(self) -> None:
        await process_cancellation_notifications(self.session, self.dispatch_notice)


class ContactNoticeMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_preserve_existing_requests_and_contacts(self) -> None:
        path = Path(__file__).resolve().parents[1] / "alembic/versions/0040_purchase_contacts_cancellation_notices.py"
        spec = importlib.util.spec_from_file_location("contact_notice_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            connection.execute(sa.text("CREATE TABLE service_cancellations (id INTEGER PRIMARY KEY, reason TEXT)"))
            connection.execute(sa.text("INSERT INTO bot_users VALUES (1)"))
            connection.execute(sa.text("INSERT INTO service_cancellations VALUES (1, 'existing')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(connection.execute(sa.text("SELECT notification_status FROM service_cancellations")).scalar(), "skipped")
                connection.execute(sa.text("INSERT INTO purchase_contacts (user_id, shop_key, telegram_id, phone_hash) VALUES (1, 0, 1001, :hash)"), {"hash": "a" * 64})
                connection.execute(sa.text("INSERT INTO service_cancellations (id, reason) VALUES (2, 'new')"))
                self.assertEqual(connection.execute(sa.text("SELECT notification_status FROM service_cancellations WHERE id = 2")).scalar(), "pending")
                migration.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT count(*) FROM service_cancellations")).scalar(), 2)
                self.assertEqual(connection.execute(sa.text("SELECT count(*) FROM purchase_contacts")).scalar(), 1)
                migration.upgrade()
                self.assertEqual(connection.execute(sa.text("SELECT phone_hash FROM purchase_contacts")).scalar(), "a" * 64)
        engine.dispose()
