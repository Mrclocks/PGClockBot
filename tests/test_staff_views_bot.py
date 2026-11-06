"""Trial users and staff action log in the bot: shop scope, permission and owner-only reading."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import staff_audit_view as audit
from app.bot.handlers import trial_users_view as trial
from app.db.models import Base, BotUser, Order, Plan, ShopStaffAction
from app.services import bot_feedback_texts as feedback
from tests.test_update_owner_only import OWNER_SESSION

SHOP_STAFF = {"role": "reseller", "bot_user_id": 5}
CTX = {"is_reseller_bot": False, "reseller_owner_id": None}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class StaffViewsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=5, telegram_id=105, referral_code="s5", role="reseller"),
            BotUser(id=7, telegram_id=107, referral_code="s7", role="reseller"),
            BotUser(id=1, telegram_id=101, referral_code="u1", role="user", full_name="<Platform Guest>"),
            BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5, full_name="Shopper"),
        ])
        self.session.add(Plan(id=1, name="Trial", price=0, is_trial=True, is_active=True))
        await self.session.flush()
        self.session.add_all([
            Order(id=10, user_id=1, plan_id=1, amount=0, status="delivered"),
            Order(id=11, user_id=6, plan_id=1, reseller_id=5, amount=0, status="delivered"),
            ShopStaffAction(shop_id=5, actor_kind="shop_owner", actor_label="<owner>", action="wallet_credit", target_type="user", target_id=6, reason="<b>gift</b>"),
            ShopStaffAction(shop_id=None, actor_kind="platform_owner", actor_label="root", action="wallet_credit", target_type="user", target_id=1),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def trial_view(self, data: str, staff: dict | None):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        with patch.object(trial, "resolve_feature_staff", AsyncMock(return_value=staff)), \
                patch.object(trial, "safe_edit_text", AsyncMock()) as edit:
            await trial.show_trial_users(callback, session=self.session, db_user=SimpleNamespace(id=9), **CTX)
        return callback, edit

    async def audit_view(self, staff: dict | None, db_user_id: int):
        callback = AsyncMock()
        callback.data = "saud:l:0"
        callback.message = AsyncMock()
        with patch.object(audit, "resolve_feature_staff", AsyncMock(return_value=staff)), \
                patch.object(audit, "safe_edit_text", AsyncMock()) as edit:
            await audit.show_staff_actions(callback, session=self.session, db_user=SimpleNamespace(id=db_user_id), **CTX)
        return callback, edit

    async def test_trial_list_is_limited_to_the_acting_shop(self) -> None:
        _, edit = await self.trial_view("tru:l:all:0", SHOP_STAFF)
        text = edit.await_args.args[1]
        found = callbacks(edit.await_args.kwargs["reply_markup"])
        self.assertIn("Shopper", text)
        self.assertNotIn("Platform Guest", text)
        self.assertIn("res:user:6", found)
        self.assertNotIn("adm:users:view:1", found)

    async def test_platform_owner_sees_platform_trials_with_escaped_names(self) -> None:
        _, edit = await self.trial_view("tru:l:all:0", OWNER_SESSION)
        text = edit.await_args.args[1]
        self.assertIn("&lt;Platform Guest&gt;", text)
        self.assertNotIn("<Platform Guest>", text)
        self.assertNotIn("Shopper", text)

    async def test_trial_list_denied_without_permission(self) -> None:
        callback, edit = await self.trial_view("tru:l:all:0", None)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        edit.assert_not_awaited()

    async def test_unknown_filter_falls_back_to_all(self) -> None:
        self.assertEqual(trial.normalize_filter("../x"), "all")

    async def test_shop_owner_reads_only_the_shops_log_and_text_is_escaped(self) -> None:
        _, edit = await self.audit_view(SHOP_STAFF, 5)
        text = edit.await_args.args[1]
        self.assertIn("&lt;owner&gt;", text)
        self.assertIn("&lt;b&gt;gift&lt;/b&gt;", text)
        self.assertNotIn("root", text)

    async def test_a_shop_bot_admin_who_is_not_the_owner_is_denied(self) -> None:
        callback, edit = await self.audit_view(SHOP_STAFF, 7)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        edit.assert_not_awaited()

    async def test_platform_owner_reads_the_platform_log_only(self) -> None:
        _, edit = await self.audit_view(OWNER_SESSION, 99)
        text = edit.await_args.args[1]
        self.assertIn("root", text)
        self.assertNotIn("&lt;owner&gt;", text)

    async def test_log_is_denied_without_staff(self) -> None:
        callback, edit = await self.audit_view(None, 5)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
