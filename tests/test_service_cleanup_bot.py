"""Service cleanup in the bot: scoped preview, reasoned hold and exemption, escaped names, hub entries."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import func, select

from app.bot.handlers import service_cleanup_bot as view
from app.bot.nav_inline import admin_system_hub_keyboard, reseller_group_hub_keyboard
from app.bot.service_cleanup_entries import SERVICE_CLEANUP_CALLBACK
from app.db.models import ShopStaffAction, UserService
from app.services import bot_feedback_texts as feedback
from app.services.service_cleanup import CleanupPolicy, save_policy
from app.services.users import DEFAULT_SETTINGS
from tests.test_service_cleanup import OTHER_SHOP, SHOP, CleanupCase
from tests.test_update_owner_only import OWNER_SESSION

SHOP_STAFF = {"role": "reseller", "bot_user_id": SHOP, "permissions": ["service_cleanup"]}
SHOP_WITHOUT_PERMISSION = {"role": "reseller", "bot_user_id": SHOP, "permissions": ["orders"]}
REASON = "مشتری تمدید را قول داده است"
HANDLERS = {
    "h": view.show_summary,
    "v": view.show_service,
    "d": view.choose_hold_days,
    "H": view.run_hold,
    "E": view.run_exempt,
    "x": view.show_exempt,
}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class CleanupBotCase(CleanupCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        now = datetime.now(timezone.utc)
        policy = CleanupPolicy(time_on=True, time_days=7)
        async with await self.session() as session:
            for shop in (None, SHOP, OTHER_SHOP):
                await save_policy(session, shop_id=shop, policy=policy, now=now - timedelta(days=60))
        await self.add_service(1, 20, SHOP, quota_expire_at=now - timedelta(days=20), pg_username="<i>svc</i>")
        await self.add_service(2, 21, OTHER_SHOP, quota_expire_at=now - timedelta(days=20))
        await self.add_service(3, 22, None, quota_expire_at=now - timedelta(days=20))

    async def press(self, data: str, staff: dict | None, *, reason: str | None = REASON):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        handler = HANDLERS[data.split(":")[1]]
        async with await self.session() as session:
            with (
                patch.object(view, "resolve_feature_staff", AsyncMock(return_value=staff)),
                patch.object(view, "safe_edit_text", AsyncMock()) as edit,
                patch.object(view, "panel_page_url", return_value=None),
                patch.object(view, "take_reason", AsyncMock(return_value=reason)),
                patch.object(view, "ask_reason", AsyncMock()) as ask,
            ):
                await handler(callback, session=session, db_user=SimpleNamespace(id=9))
        return callback, edit, ask

    async def service(self, service_id: int) -> UserService:
        async with await self.session() as session:
            return await session.get(UserService, service_id)

    async def logged(self) -> int:
        async with await self.session() as session:
            return int(await session.scalar(select(func.count()).select_from(ShopStaffAction)))


class AccessTests(CleanupBotCase):
    async def test_without_the_permission_nothing_is_shown_or_changed(self) -> None:
        for staff in (None, SHOP_WITHOUT_PERMISSION):
            for data in ("scl:h", "scl:v:1", "scl:d:1", "scl:H:1:7", "scl:E:1:1", "scl:x"):
                with self.subTest(staff=staff, data=data):
                    callback, edit, _ = await self.press(data, staff)
                    callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
                    edit.assert_not_awaited()
        row = await self.service(1)
        self.assertIsNone(row.cleanup_hold_until)
        self.assertFalse(row.cleanup_exempt)

    async def test_each_actor_sees_only_its_own_services(self) -> None:
        _, edit, _ = await self.press("scl:h", SHOP_STAFF)
        found = callbacks(edit.await_args.kwargs["reply_markup"])
        self.assertIn("scl:v:1", found)
        self.assertNotIn("scl:v:2", found)
        self.assertNotIn("scl:v:3", found)
        _, edit, _ = await self.press("scl:h", dict(OWNER_SESSION))
        found = callbacks(edit.await_args.kwargs["reply_markup"])
        self.assertIn("scl:v:3", found)
        self.assertNotIn("scl:v:1", found)

    async def test_another_shops_service_has_no_card_and_cannot_be_held_or_exempted(self) -> None:
        callback, edit, _ = await self.press("scl:v:2", SHOP_STAFF)
        callback.answer.assert_awaited_once_with(view.NOT_LISTED_TEXT, show_alert=True)
        edit.assert_not_awaited()
        for data in ("scl:H:2:7", "scl:E:2:1", "scl:H:3:7", "scl:E:3:1"):
            await self.press(data, SHOP_STAFF)
        for service_id in (2, 3):
            row = await self.service(service_id)
            self.assertIsNone(row.cleanup_hold_until)
            self.assertFalse(row.cleanup_exempt)
        self.assertEqual(await self.logged(), 0)


class ActionTests(CleanupBotCase):
    async def test_the_card_escapes_customer_and_service_names(self) -> None:
        _, edit, _ = await self.press("scl:v:1", SHOP_STAFF)
        text = edit.await_args.args[1]
        self.assertIn("&lt;i&gt;svc&lt;/i&gt;", text)
        self.assertIn("&lt;b&gt;Eve&lt;/b&gt;", text)
        self.assertNotIn("<b>Eve</b>", text)
        self.assertIn("res:user:20", callbacks(edit.await_args.kwargs["reply_markup"]))

    async def test_a_hold_asks_for_a_reason_first(self) -> None:
        _, _, ask = await self.press("scl:H:1:7", SHOP_STAFF, reason=None)
        ask.assert_awaited_once()
        self.assertIsNone((await self.service(1)).cleanup_hold_until)

    async def test_a_reasoned_hold_moves_the_deadline_and_is_logged(self) -> None:
        callback, _, _ = await self.press("scl:H:1:7", SHOP_STAFF)
        self.assertIn("تمدید شد", callback.answer.await_args.args[0])
        self.assertIsNotNone((await self.service(1)).cleanup_hold_until)
        self.assertEqual(await self.logged(), 1)

    async def test_out_of_range_days_are_refused(self) -> None:
        callback, _, _ = await self.press("scl:H:1:999", SHOP_STAFF)
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
        self.assertIsNone((await self.service(1)).cleanup_hold_until)

    async def test_exempting_and_returning_a_service(self) -> None:
        _, _, ask = await self.press("scl:E:1:1", SHOP_STAFF, reason=None)
        ask.assert_awaited_once()
        self.assertFalse((await self.service(1)).cleanup_exempt)
        await self.press("scl:E:1:1", SHOP_STAFF)
        self.assertTrue((await self.service(1)).cleanup_exempt)
        _, edit, _ = await self.press("scl:x", SHOP_STAFF)
        self.assertIn("scl:E:1:0", callbacks(edit.await_args.kwargs["reply_markup"]))
        await self.press("scl:E:1:0", SHOP_STAFF)
        self.assertFalse((await self.service(1)).cleanup_exempt)
        self.assertEqual(await self.logged(), 2)


class HubEntryTests(CleanupBotCase):
    async def test_the_admin_system_hub_and_a_permitted_shop_offer_the_cleanup(self) -> None:
        ui = dict(DEFAULT_SETTINGS)
        self.assertIn(SERVICE_CLEANUP_CALLBACK, callbacks(admin_system_hub_keyboard(ui)))
        entries = [("res_users", "👥 مشتریان")]
        with patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=entries):
            with patch("app.bot.nav_inline.has_bot_perm", side_effect=lambda _p, key: key == "service_cleanup"):
                allowed = reseller_group_hub_keyboard("customers", MagicMock(), ui)
            with patch("app.bot.nav_inline.has_bot_perm", return_value=False):
                denied = reseller_group_hub_keyboard("customers", MagicMock(), ui)
        self.assertIn(SERVICE_CLEANUP_CALLBACK, callbacks(allowed))
        self.assertNotIn(SERVICE_CLEANUP_CALLBACK, callbacks(denied))

