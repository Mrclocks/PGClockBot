"""The customer hears what staff did and why; delivery problems never undo the operation."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, CustomerNotice
from app.services.customer_action_notices import (
    ACTION_NOTICE_TITLES,
    NoticeTarget,
    deliver_notice,
    notice_text,
    notify_customers,
    pg_user_targets,
    pg_users_targets_for_shop,
)
from app.services.customer_notice_queue import OUTCOME_FAILED, OUTCOME_RETRY, OUTCOME_SENT, OUTCOME_SKIPPED
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
            # Cleanup warnings/deletes use CLEANUP_NOTICE_TEXTS; exempt/hold are staff-only.
            if action.startswith(("service_cleanup_", "cleanup_")):
                continue
            if action.startswith(("service_", "pg_user_")) and action != "service_auto_delete":
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
    def bot(self, error: Exception | None = None) -> MagicMock:
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=error)
        return bot

    async def queued(self) -> list[CustomerNotice]:
        async with AsyncSession(bind=self.engine) as session:
            return list((await session.scalars(select(CustomerNotice).order_by(CustomerNotice.id))).all())

    async def deliver(self, notice: CustomerNotice, bot: MagicMock | None):
        async def bot_for(_shop_id):
            return bot

        async with AsyncSession(bind=self.engine) as session:
            return await deliver_notice(session, bot_for, notice)

    def notice(self, **kw) -> CustomerNotice:
        values = dict(id=1, shop_id=SHOP_ID, bot_user_id=CUSTOMER_ID, telegram_id=107, action="service_delete",
                      service_name="svc12", reason="درخواست مشتری")
        return CustomerNotice(**{**values, **kw})

    async def test_notifying_only_queues_a_pending_row_per_customer(self) -> None:
        targets = [NoticeTarget(CUSTOMER_ID, 107, SHOP_ID, "svc12")]
        async with AsyncSession(bind=self.engine) as session:
            self.assertEqual(await notify_customers(session, targets, action="service_delete", reason="درخواست مشتری"), 1)
        rows = await self.queued()
        self.assertEqual([(r.status, r.shop_id, r.telegram_id, r.reason) for r in rows], [("pending", SHOP_ID, 107, "درخواست مشتری")])

    async def test_nothing_to_notify_queues_nothing(self) -> None:
        async with AsyncSession(bind=self.engine) as session:
            self.assertEqual(await notify_customers(session, [], action="service_delete", reason="درخواست مشتری"), 0)
        self.assertEqual(await self.queued(), [])

    async def test_a_message_with_the_reason_is_sent_through_the_shop_bot(self) -> None:
        bot = self.bot()
        outcome = await self.deliver(self.notice(), bot)
        self.assertEqual(outcome.kind, OUTCOME_SENT)
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args[0], 107)
        self.assertIn("درخواست مشتری", args[1])
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_each_failure_kind_maps_to_its_outcome(self) -> None:
        method = MagicMock()
        cases = (
            (TelegramRetryAfter(method=method, message="slow", retry_after=7), OUTCOME_RETRY),
            (TelegramForbiddenError(method=method, message="blocked"), OUTCOME_SKIPPED),
            (TelegramBadRequest(method=method, message="bad"), OUTCOME_FAILED),
            (TelegramNetworkError(method=method, message="down"), OUTCOME_RETRY),
        )
        for error, expected in cases:
            outcome = await self.deliver(self.notice(), self.bot(error))
            self.assertEqual(outcome.kind, expected, type(error).__name__)
        retry = await self.deliver(self.notice(), self.bot(cases[0][0]))
        self.assertEqual(retry.retry_after.total_seconds(), 8)

    async def test_a_shop_without_a_bot_is_skipped(self) -> None:
        self.assertEqual((await self.deliver(self.notice(), None)).kind, OUTCOME_SKIPPED)

    async def test_a_blocked_customer_is_not_messaged(self) -> None:
        async with AsyncSession(bind=self.engine) as session:
            (await session.get(BotUser, CUSTOMER_ID)).is_blocked = True
            await session.commit()
        bot = self.bot()
        self.assertEqual((await self.deliver(self.notice(), bot)).kind, OUTCOME_SKIPPED)
        bot.send_message.assert_not_awaited()

    async def test_a_shop_only_finds_its_own_services_for_a_panel_user(self) -> None:
        async with AsyncSession(bind=self.engine) as session:
            mine = await pg_user_targets(session, {**SHOP_STAFF}, pg_user_id=SERVICE_ID)
            other = await pg_user_targets(session, {**SHOP_STAFF, "bot_user_id": SHOP_ID + 1, "reseller_id": SHOP_ID + 1}, pg_user_id=SERVICE_ID)
            batch = await pg_users_targets_for_shop(session, SHOP_ID, pg_user_ids=[SERVICE_ID, 999])
            foreign = await pg_users_targets_for_shop(session, SHOP_ID + 1, pg_user_ids=[SERVICE_ID])
        self.assertEqual([t.user_id for t in mine], [CUSTOMER_ID])
        self.assertEqual(other, [])
        self.assertEqual(list(batch), [SERVICE_ID])
        self.assertEqual(foreign, {})

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
