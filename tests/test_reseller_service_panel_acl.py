"""Service changes from the shop bot carry the shop's panel role, like the web panel."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import reseller_customer_services as handler
from app.db.models import Base, BotUser, Plan, ResellerProfile, Role, UserService
from app.services import bot_feedback_texts as feedback
from app.services import bot_principal_identity as identity

SHOP_ID = 2
CUSTOMER_ID = 4
SOLD_SERVICE_ID = 11
IMPORTED_SERVICE_ID = 13


def shop_staff(**user_actions: bool) -> dict:
    return {
        "role": "reseller",
        "bot_user_id": SHOP_ID,
        "permissions": ["dashboard", "customer_services"],
        "pg_user_actions": dict(user_actions),
    }


class ShopBotServicePanelAclTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'acl.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.shop = BotUser(id=SHOP_ID, telegram_id=1002, referral_code="shop2", role=Role.RESELLER.value)
        customer = BotUser(
            id=CUSTOMER_ID, telegram_id=1004, referral_code="c4", role=Role.USER.value, reseller_id=SHOP_ID
        )
        self.session.add_all([self.shop, customer])
        await self.session.flush()
        self.session.add(
            ResellerProfile(
                user_id=SHOP_ID,
                is_active=True,
                web_permissions="dashboard,customer_services",
                bot_permissions="dashboard,customer_services",
            )
        )
        self.session.add(Plan(id=1, name="Gold", price=100, duration_days=30, data_limit_gb=10))
        await self.session.flush()
        self.session.add_all(
            [
                UserService(
                    id=SOLD_SERVICE_ID,
                    bot_user_id=CUSTOMER_ID,
                    plan_id=1,
                    pg_user_id=501,
                    pg_username="sold-svc",
                    shop_id=SHOP_ID,
                ),
                UserService(
                    id=IMPORTED_SERVICE_ID,
                    bot_user_id=CUSTOMER_ID,
                    pg_user_id=503,
                    pg_username="imported-svc",
                    remark="imported",
                    shop_id=SHOP_ID,
                ),
            ]
        )
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    def fsm(self) -> FSMContext:
        return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))

    def callback(self, data: str) -> MagicMock:
        return MagicMock(data=data, answer=AsyncMock(), message=MagicMock())

    def with_panel_staff(self, staff: dict | None):
        return patch.object(handler, "shop_bot_panel_staff", AsyncMock(return_value=staff))

    async def run_delete(self, data: str, staff: dict | None) -> MagicMock:
        callback = self.callback(data)
        with self.with_panel_staff(staff), patch.object(handler, "safe_edit_text", AsyncMock()):
            await handler.res_user_service_delete(
                callback, self.session, self.shop, is_reseller_bot=True, reseller_owner_id=SHOP_ID
            )
        return callback

    async def service_exists(self, service_id: int) -> bool:
        async with async_sessionmaker(self.engine)() as fresh:
            return await fresh.get(UserService, service_id) is not None

    async def test_detach_is_refused_when_the_panel_role_cannot_delete(self) -> None:
        callback = await self.run_delete(f"res:svcdel:{CUSTOMER_ID}:{IMPORTED_SERVICE_ID}", shop_staff(delete=False))

        self.assertTrue(await self.service_exists(IMPORTED_SERVICE_ID))
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
        self.assertIn("پاسارگارد", callback.answer.await_args.args[0])

    async def test_detach_goes_through_when_the_panel_role_may_delete(self) -> None:
        await self.run_delete(f"res:svcdel:{CUSTOMER_ID}:{IMPORTED_SERVICE_ID}", shop_staff(delete=True))

        self.assertFalse(await self.service_exists(IMPORTED_SERVICE_ID))

    async def test_nothing_changes_when_the_shop_panel_role_cannot_be_resolved(self) -> None:
        callback = await self.run_delete(f"res:svcdel:{CUSTOMER_ID}:{IMPORTED_SERVICE_ID}", None)

        self.assertTrue(await self.service_exists(IMPORTED_SERVICE_ID))
        self.assertEqual(callback.answer.await_args.args[0], feedback.NO_ACCESS)

    async def test_renew_passes_the_shop_panel_role_to_the_service_helper(self) -> None:
        staff = shop_staff(update=True)
        renew = AsyncMock()
        callback = self.callback(f"res:svcrenew:{CUSTOMER_ID}:{SOLD_SERVICE_ID}")
        with (
            self.with_panel_staff(staff),
            patch.object(handler, "admin_renew_service", renew),
            patch.object(handler, "service_snapshot", AsyncMock()),
            patch.object(handler, "snapshot_telegram_lines", return_value=""),
            patch.object(handler, "safe_edit_text", AsyncMock()),
        ):
            await handler.res_user_service_renew(
                callback, self.fsm(), self.session, self.shop, is_reseller_bot=True, reseller_owner_id=SHOP_ID
            )

        renew.assert_awaited_once()
        self.assertIs(renew.await_args.kwargs["staff"], staff)

    async def test_renew_changes_nothing_when_the_shop_panel_role_cannot_be_resolved(self) -> None:
        renew = AsyncMock()
        callback = self.callback(f"res:svcrenew:{CUSTOMER_ID}:{SOLD_SERVICE_ID}")
        state = self.fsm()
        with (
            self.with_panel_staff(None),
            patch.object(handler, "admin_renew_service", renew),
            patch.object(handler, "safe_edit_text", AsyncMock()),
        ):
            await handler.res_user_service_renew(
                callback, state, self.session, self.shop, is_reseller_bot=True, reseller_owner_id=SHOP_ID
            )

        renew.assert_not_awaited()
        self.assertEqual(callback.answer.await_args.args[0], feedback.NO_ACCESS)
        busy = {key for key in await state.get_data() if key.startswith("rsvcbusy:")}
        self.assertEqual(busy, set(), "the in-flight flag is released")

    async def test_extend_is_refused_when_the_panel_role_cannot_update(self) -> None:
        panel = MagicMock(
            get_user_by_id=AsyncMock(return_value={"data_limit": 0, "expire": None}),
            modify_user_by_id=AsyncMock(),
        )
        state = self.fsm()
        await state.update_data(**{f"rsvcadj:{CUSTOMER_ID}:{SOLD_SERVICE_ID}": {"days": 5, "gb": 0}})
        callback = self.callback(f"res:svcadj:{CUSTOMER_ID}:{SOLD_SERVICE_ID}:confirm")
        with (
            self.with_panel_staff(shop_staff(update=False)),
            patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock(return_value=panel)),
            patch.object(handler, "safe_edit_text", AsyncMock()),
        ):
            await handler.res_user_service_adjust(
                callback, state, self.session, self.shop, is_reseller_bot=True, reseller_owner_id=SHOP_ID
            )

        panel.modify_user_by_id.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))


class ShopBotPanelStaffTests(unittest.IsolatedAsyncioTestCase):
    async def test_staff_comes_from_this_shops_principal(self) -> None:
        profile = MagicMock(id=7, user_id=SHOP_ID)
        resolution = MagicMock(staff=shop_staff(update=True))
        bridge = AsyncMock(return_value=resolution)
        with patch.object(identity, "resolve_bot_principal_bridge", bridge):
            staff = await identity.shop_bot_panel_staff(MagicMock(), profile=profile, db_user=MagicMock())

        self.assertEqual(staff, resolution.staff)
        self.assertIsNot(staff, resolution.staff)
        kwargs = bridge.await_args.kwargs
        self.assertTrue(kwargs["is_reseller_bot"])
        self.assertEqual(kwargs["reseller_profile_id"], 7)
        self.assertEqual(kwargs["reseller_owner_id"], SHOP_ID)

    async def test_no_staff_without_a_resolved_principal(self) -> None:
        with patch.object(identity, "resolve_bot_principal_bridge", AsyncMock(return_value=None)):
            staff = await identity.shop_bot_panel_staff(
                MagicMock(), profile=MagicMock(id=7, user_id=SHOP_ID), db_user=MagicMock()
            )

        self.assertIsNone(staff)
