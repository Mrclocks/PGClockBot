"""A shop's customer card in the bot lists the customer's services, active ones first."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.db.models import Plan, UserService
from app.services.customer_service_lines import SERVICE_LINE_LIMIT, service_lines


def _service(service_id: int, name: str, status: str | None, *, cancelled: bool = False, plan: str | None = None) -> UserService:
    service = UserService(
        id=service_id, bot_user_id=1, pg_username=name, is_cancelled=cancelled, quota_status=status,
        quota_synced_at=datetime.now(timezone.utc), quota_expire_at=datetime.now(timezone.utc) + timedelta(days=10),
        quota_data_limit_bytes=10 * 1024**3,
    )
    if plan:
        service.plan = Plan(name=plan, price=1, duration_days=30, data_limit_gb=10)
    return service


class ServiceLinesTests(unittest.TestCase):
    def test_active_services_come_first_with_plan_status_expiry_and_volume(self) -> None:
        lines = service_lines([_service(1, "old", "expired"), _service(2, "live", "active", plan="۳۰ روزه")])
        self.assertIn("live", lines[0])
        self.assertIn("۳۰ روزه", lines[0])
        self.assertIn("فعال", lines[0])
        self.assertIn("حجم", lines[0])
        self.assertIn("old", lines[1])
        self.assertIn("منقضی", lines[1])

    def test_a_cancelled_service_is_never_shown_as_active(self) -> None:
        lines = service_lines([_service(1, "gone", "active", cancelled=True), _service(2, "live", "active")])
        self.assertIn("live", lines[0])
        self.assertIn("لغو شده", lines[1])

    def test_a_long_list_is_cut_with_a_count_of_the_rest(self) -> None:
        lines = service_lines([_service(i, f"svc{i}", "active") for i in range(1, SERVICE_LINE_LIMIT + 4)])
        self.assertEqual(len(lines), SERVICE_LINE_LIMIT + 1)
        self.assertIn("3 سرویس دیگر", lines[-1])

    def test_hostile_names_are_escaped_and_no_services_means_no_lines(self) -> None:
        self.assertIn("&lt;b&gt;", service_lines([_service(1, "<b>x", "active", plan="<i>p</i>")])[0])
        self.assertNotIn("<i>", service_lines([_service(1, "x", "active", plan="<i>p</i>")])[0])
        self.assertEqual(service_lines([]), [])

    def test_a_service_never_synced_shows_no_made_up_quota(self) -> None:
        service = _service(1, "fresh", None)
        service.quota_synced_at = None
        self.assertNotIn("انقضا", service_lines([service])[0])


class CustomerCardHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_card_lists_only_this_shops_services_of_the_customer(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.bot.handlers import reseller as handler
        from app.db import Base
        from app.db.models import BotUser, ResellerProfile, Role

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            shop = BotUser(telegram_id=1, full_name="shop", role=Role.RESELLER.value, referral_code="R1")
            customer = BotUser(telegram_id=2, full_name="Sara", role=Role.USER.value, referral_code="R2")
            session.add_all([shop, customer])
            await session.flush()
            session.add(ResellerProfile(user_id=shop.id, is_active=True, web_permissions="dashboard", bot_permissions="dashboard"))
            customer.reseller_id = shop.id
            mine = _service(1, "mine-svc", "active", plan="پلن من")
            mine.bot_user_id, mine.shop_id = customer.id, shop.id
            other = _service(2, "platform-svc", "active")
            other.bot_user_id, other.shop_id = customer.id, None
            session.add_all([mine, other])
            await session.commit()
            callback = MagicMock(data=f"res:user:{customer.id}", answer=AsyncMock(), message=MagicMock())
            edit = AsyncMock()
            with patch.object(handler, "safe_edit_text", edit):
                await handler.res_user_view(
                    callback, session, shop, is_reseller_bot=True, reseller_owner_id=shop.id
                )
        await engine.dispose()
        text = edit.await_args.args[1]
        self.assertIn("mine-svc", text)
        self.assertIn("پلن من", text)
        self.assertNotIn("platform-svc", text)
