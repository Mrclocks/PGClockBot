"""The customer hears what staff did and why; delivery problems never undo the operation."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.exceptions import TelegramNetworkError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.customer_action_notices import (
    ACTION_NOTICE_TITLES,
    NoticeTarget,
    notice_text,
    notify_customers,
    pg_user_targets,
)
from app.services.staff_audit import ACTION_LABELS
from tests.test_customer_services_web_permission import (
    CUSTOMER_ID,
    SERVICE_ID,
    SHOP_ID,
    CustomerServicesWebPermissionTests,
)

SHOP_STAFF = {"role": "reseller", "bot_user_id": SHOP_ID, "reseller_id": SHOP_ID}


class NoticeTextTests(unittest.TestCase):
    def test_every_logged_service_action_has_a_customer_message(self) -> None:
        for action in ACTION_LABELS:
            if action.startswith(("service_", "pg_user_")):
                self.assertIn(action, ACTION_NOTICE_TITLES)

    def test_reason_and_service_name_are_escaped_for_telegram_html(self) -> None:
        text = notice_text("service_delete", service_name="<b>x</b>", reason="<script>alert(1)</script>")
        self.assertNotIn("<script>", text)
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_detaching_an_external_service_does_not_claim_it_was_deleted(self) -> None:
        text = notice_text("service_detach", service_name="a", reason="انتقال به حساب دیگر")
        self.assertNotIn("حذف شد", text)
        self.assertIn("همچنان فعال", text)

    def test_revoking_the_link_tells_the_customer_to_fetch_the_new_one(self) -> None:
        self.assertIn("لینک جدید", notice_text("pg_user_revoke_sub", service_name="a", reason="ریسک لو رفتن"))


class NotifyCustomersTests(CustomerServicesWebPermissionTests):
    def bot(self, *, fail: bool = False) -> MagicMock:
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=TelegramNetworkError(method=MagicMock(), message="down") if fail else None)
        bot.session.close = AsyncMock()
        return bot

    async def test_a_message_with_the_reason_reaches_the_customer(self) -> None:
        bot = self.bot()
        target = NoticeTarget(CUSTOMER_ID, 107, SHOP_ID, "svc12")
        with patch("app.services.customer_action_notices.open_notify_bot_for_user", AsyncMock(return_value=(bot, True))):
            async with AsyncSession(bind=self.engine) as session:
                sent = await notify_customers(session, [target], action="service_delete", reason="درخواست مشتری")
        self.assertEqual(sent, 1)
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args[0], 107)
        self.assertIn("درخواست مشتری", args[1])
        bot.session.close.assert_awaited_once()

    async def test_a_delivery_failure_is_swallowed_and_counted_as_not_sent(self) -> None:
        bot = self.bot(fail=True)
        target = NoticeTarget(CUSTOMER_ID, 107, SHOP_ID, "svc12")
        with patch("app.services.customer_action_notices.open_notify_bot_for_user", AsyncMock(return_value=(bot, True))):
            async with AsyncSession(bind=self.engine) as session:
                self.assertEqual(await notify_customers(session, [target], action="service_renew", reason="تمدید دستی"), 0)

    async def test_one_bot_is_opened_per_shop_for_several_customers(self) -> None:
        bot = self.bot()
        targets = [NoticeTarget(CUSTOMER_ID, 107, SHOP_ID, "a"), NoticeTarget(CUSTOMER_ID, 107, SHOP_ID, "b")]
        opener = AsyncMock(return_value=(bot, True))
        with patch("app.services.customer_action_notices.open_notify_bot_for_user", opener):
            async with AsyncSession(bind=self.engine) as session:
                self.assertEqual(await notify_customers(session, targets, action="service_renew", reason="تمدید دستی"), 2)
        opener.assert_awaited_once()
        bot.session.close.assert_awaited_once()

    async def test_an_unusable_bot_token_does_not_break_the_operation(self) -> None:
        from aiogram.utils.token import TokenValidationError

        target = NoticeTarget(CUSTOMER_ID, 107, SHOP_ID, "svc12")
        broken = AsyncMock(side_effect=TokenValidationError("Token is invalid!"))
        with patch("app.services.customer_action_notices.open_notify_bot_for_user", broken):
            async with AsyncSession(bind=self.engine) as session:
                self.assertEqual(await notify_customers(session, [target], action="service_renew", reason="تمدید دستی"), 0)

    async def test_a_blocked_customer_is_not_messaged(self) -> None:
        async with AsyncSession(bind=self.engine) as session:
            (await session.get(BotUser, CUSTOMER_ID)).is_blocked = True
            await session.commit()
        bot = self.bot()
        target = NoticeTarget(CUSTOMER_ID, 107, SHOP_ID, "svc12")
        with patch("app.services.customer_action_notices.open_notify_bot_for_user", AsyncMock(return_value=(bot, True))):
            async with AsyncSession(bind=self.engine) as session:
                self.assertEqual(await notify_customers(session, [target], action="service_renew", reason="تمدید دستی"), 0)
        bot.send_message.assert_not_awaited()

    async def test_a_shop_only_finds_its_own_services_for_a_panel_user(self) -> None:
        async with AsyncSession(bind=self.engine) as session:
            mine = await pg_user_targets(session, {**SHOP_STAFF}, pg_user_id=SERVICE_ID)
            other = await pg_user_targets(session, {**SHOP_STAFF, "bot_user_id": SHOP_ID + 1, "reseller_id": SHOP_ID + 1}, pg_user_id=SERVICE_ID)
        self.assertEqual([t.user_id for t in mine], [CUSTOMER_ID])
        self.assertEqual(other, [])

    async def test_the_web_operation_notifies_the_customer_with_the_reason(self) -> None:
        await self.set_shop_permissions("dashboard,customer_services")
        self.patched_service_ops()
        with patch("app.api.user_pages.notify_customers", AsyncMock(return_value=1)) as notify:
            response = await self.post(
                f"/users/{CUSTOMER_ID}/services/{SERVICE_ID}/extend",
                {"extra_days": "5", "extra_gb": "0", "reason": "جبران قطعی سرویس"},
            )
        self.assertEqual(response.status_code, 303)
        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.kwargs["reason"], "جبران قطعی سرویس")
        self.assertEqual(notify.await_args.kwargs["action"], "service_extend")


del CustomerServicesWebPermissionTests

if __name__ == "__main__":
    unittest.main()
