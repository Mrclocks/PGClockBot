"""Shop staff can open live service info/edit for their own customers only."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.bot.handlers import reseller_customer_services as handler
from app.db.models import Base, BotUser, Plan, ResellerProfile, Role, UserService
from app.services import bot_feedback_texts as feedback
from app.services.bot_user_admin import ServiceSnapshot
from app.services.staff_cards import format_staff_customer_card


class StaffCardFormatTests(unittest.TestCase):
    def test_customer_card_is_structured_without_a_web_panel_footer(self) -> None:
        text = format_staff_customer_card(
            flags="🟢",
            display_name="Tami",
            telegram_id=7200987409,
            username="tamishb",
            wallet_text="۰ تومان",
            service_count=1,
            is_blocked=False,
            wallet_label="کیف پول فروشگاه",
            service_summary=["📦 <code>clk</code>\n📶 فعال"],
        )
        self.assertIn("Tami", text)
        self.assertIn("7200987409", text)
        self.assertIn("@tamishb", text)
        self.assertIn("کیف پول فروشگاه", text)
        self.assertIn("━━━━━━━━━━━━", text)
        self.assertNotIn("وب‌پنل", text)
        self.assertNotIn("/users?uid=", text)

    def test_hostile_display_name_is_escaped(self) -> None:
        text = format_staff_customer_card(
            flags="",
            display_name="<b>x</b>",
            telegram_id=1,
            username=None,
            wallet_text="0",
            service_count=0,
            is_blocked=False,
        )
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertNotIn("<b>x</b>", text)


class ResellerServiceScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'svc.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        shop = BotUser(id=2, telegram_id=1002, referral_code="shop2", role=Role.RESELLER.value)
        other = BotUser(id=3, telegram_id=1003, referral_code="shop3", role=Role.RESELLER.value)
        limited = BotUser(id=9, telegram_id=1009, referral_code="lim", role=Role.USER.value)
        customer = BotUser(
            id=4, telegram_id=1004, referral_code="c4", role=Role.USER.value, reseller_id=2, full_name="Sara"
        )
        foreign = BotUser(
            id=5, telegram_id=1005, referral_code="c5", role=Role.USER.value, reseller_id=3, full_name="Other"
        )
        self.session.add_all([shop, other, limited, customer, foreign])
        await self.session.flush()
        self.session.add(
            ResellerProfile(
                user_id=2,
                is_active=True,
                web_permissions="dashboard,customer_services",
                bot_permissions="dashboard,customer_services",
            )
        )
        self.session.add(
            ResellerProfile(
                user_id=3,
                is_active=True,
                # dashboard without customer_services → can search, not manage services.
                web_permissions="dashboard",
                bot_permissions="dashboard",
            )
        )
        self.session.add(Plan(id=1, name="Gold", price=100, duration_days=30, data_limit_gb=10))
        await self.session.flush()
        self.session.add(
            UserService(
                id=11,
                bot_user_id=4,
                plan_id=1,
                pg_username="shop-svc",
                shop_id=2,
                quota_status="active",
            )
        )
        self.session.add(
            UserService(
                id=12,
                bot_user_id=4,
                plan_id=1,
                pg_username="other-shop-svc",
                shop_id=3,
                quota_status="active",
            )
        )
        await self.session.commit()
        self.shop = shop
        self.other_shop = other
        self.customer = customer
        self.foreign = foreign

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    def _callback(self, data: str) -> MagicMock:
        return MagicMock(data=data, answer=AsyncMock(), message=MagicMock())

    async def _snap_for(self, service_id: int) -> ServiceSnapshot:
        svc = (
            await self.session.execute(
                select(UserService)
                .where(UserService.id == service_id)
                .options(selectinload(UserService.plan))
            )
        ).scalar_one()
        return ServiceSnapshot(
            service=svc,
            pg=None,
            status_fa="فعال",
            used_text="0",
            limit_text="10 گیگ",
            volume_text="0 از 10 گیگ",
            remain_gb_text="10 گیگ",
            days_left=10,
            expire_text="—",
            subscription_url=None,
        )

    async def test_services_list_is_shop_scoped_and_offers_per_service_buttons(self) -> None:
        snap = await self._snap_for(11)
        callback = self._callback(f"res:svcs:{self.customer.id}")
        edit = AsyncMock()
        with patch.object(handler, "safe_edit_text", edit), patch.object(
            handler, "list_service_snapshots", AsyncMock(return_value=[snap])
        ) as listed:
            await handler.res_user_services(
                callback,
                self.session,
                self.shop,
                is_reseller_bot=True,
                reseller_owner_id=self.shop.id,
            )
        listed.assert_awaited_once()
        self.assertEqual(listed.await_args.kwargs.get("shop_id"), 2)
        markup = edit.await_args.kwargs["reply_markup"]
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("res:svc:4:11", buttons)
        self.assertIn("res:user:4", buttons)
        self.assertNotIn("وب‌پنل", edit.await_args.args[1])

    async def test_another_shops_service_cannot_be_opened(self) -> None:
        callback = self._callback(f"res:svc:{self.customer.id}:12")
        edit = AsyncMock()
        with patch.object(handler, "safe_edit_text", edit):
            await handler.res_user_service_one(
                callback,
                self.session,
                self.shop,
                is_reseller_bot=True,
                reseller_owner_id=self.shop.id,
            )
        edit.assert_not_awaited()
        callback.answer.assert_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
        alert_text = callback.answer.await_args.args[0] if callback.answer.await_args.args else ""
        self.assertTrue(alert_text)  # safe error, not empty

    async def test_foreign_customer_card_services_are_denied(self) -> None:
        callback = self._callback(f"res:svcs:{self.foreign.id}")
        edit = AsyncMock()
        with patch.object(handler, "safe_edit_text", edit):
            await handler.res_user_services(
                callback,
                self.session,
                self.shop,
                is_reseller_bot=True,
                reseller_owner_id=self.shop.id,
            )
        edit.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
        self.assertEqual(callback.answer.await_args.args[0], feedback.NO_ACCESS)

    async def test_staff_without_customer_services_cannot_open_services(self) -> None:
        """``dashboard`` alone is not enough — ``customer_services`` is required."""
        callback = self._callback(f"res:svcs:{self.foreign.id}")
        edit = AsyncMock()
        with patch.object(handler, "safe_edit_text", edit):
            await handler.res_user_services(
                callback,
                self.session,
                self.other_shop,
                is_reseller_bot=True,
                reseller_owner_id=self.other_shop.id,
            )
        edit.assert_not_awaited()
        self.assertEqual(callback.answer.await_args.args[0], feedback.NO_ACCESS)
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_renew_loads_the_service_with_this_shop_id(self) -> None:
        callback = self._callback(f"res:svcrenew:{self.customer.id}:11")
        edit = AsyncMock()
        owned = AsyncMock(return_value=await self.session.get(UserService, 11))
        with patch.object(handler, "safe_edit_text", edit), patch.object(
            handler, "get_owned_service", owned
        ), patch.object(
            handler, "admin_renew_service", AsyncMock()
        ), patch.object(
            handler,
            "service_snapshot",
            AsyncMock(return_value=await self._snap_for(11)),
        ):
            await handler.res_user_service_renew(
                callback,
                self.session,
                self.shop,
                is_reseller_bot=True,
                reseller_owner_id=self.shop.id,
            )
        owned.assert_awaited()
        self.assertEqual(owned.await_args.kwargs.get("shop_id"), 2)
        self.assertEqual(owned.await_args.kwargs.get("bot_user_id"), 4)
        self.assertEqual(owned.await_args.kwargs.get("service_id"), 11)

    async def test_customer_card_shows_service_manage_button(self) -> None:
        from app.bot.handlers import reseller as res_handler

        callback = self._callback(f"res:user:{self.customer.id}")
        edit = AsyncMock()
        with patch.object(res_handler, "safe_edit_text", edit):
            await res_handler.res_user_view(
                callback,
                self.session,
                self.shop,
                is_reseller_bot=True,
                reseller_owner_id=self.shop.id,
            )
        text = edit.await_args.args[1]
        markup = edit.await_args.kwargs["reply_markup"]
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("res:svcs:4", buttons)
        self.assertIn("shop-svc", text)
        self.assertNotIn("other-shop-svc", text)
        self.assertNotIn("/users?uid=", text)
        self.assertNotIn("وب‌پنل", text)
