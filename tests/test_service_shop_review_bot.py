"""Owner-only service shop assignment in the bot: denial, one-time claim, unknown shop."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import service_shop_review_view as view
from app.db.models import Base, BotUser, ResellerProfile, UserService
from app.services import bot_feedback_texts as feedback


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class ServiceShopReviewBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=5, telegram_id=105, referral_code="s5", role="reseller", full_name="<Shop>"),
            BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
        ])
        await self.session.flush()
        self.session.add(ResellerProfile(user_id=5, is_active=True))
        self.session.add_all([
            UserService(id=11, bot_user_id=1, pg_user_id=101, pg_username="needs-review", shop_needs_review=True),
            UserService(id=12, bot_user_id=1, pg_user_id=102, pg_username="settled", shop_needs_review=False),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def press(self, data: str, *, owner: bool):
        handler = {"l": view.show_list, "v": view.show_service, "c": view.confirm_assign, "x": view.run_assign}[data.split(":")[1]]
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        with patch.object(view, "require_bot_owner", AsyncMock(return_value=owner)), \
                patch.object(view, "safe_edit_text", AsyncMock()) as edit:
            await handler(callback, session=self.session, db_user=SimpleNamespace(id=9))
        return callback, edit

    async def test_non_owner_sees_and_changes_nothing(self) -> None:
        for data in ("ssr:l:0", "ssr:v:11", "ssr:c:11:5", "ssr:x:11:5"):
            callback, edit = await self.press(data, owner=False)
            callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
            edit.assert_not_awaited()
        service = await self.session.get(UserService, 11)
        await self.session.refresh(service)
        self.assertTrue(service.shop_needs_review)

    async def test_list_shows_only_services_under_review(self) -> None:
        _, edit = await self.press("ssr:l:0", owner=True)
        found = callbacks(edit.await_args.kwargs["reply_markup"])
        self.assertIn("ssr:v:11", found)
        self.assertNotIn("ssr:v:12", found)

    async def test_card_escapes_shop_names_and_offers_platform_and_shops(self) -> None:
        _, edit = await self.press("ssr:v:11", owner=True)
        found = callbacks(edit.await_args.kwargs["reply_markup"])
        self.assertIn("ssr:c:11:p", found)
        self.assertIn("ssr:c:11:5", found)

    async def test_assign_runs_once_and_second_tap_is_refused(self) -> None:
        callback, _ = await self.press("ssr:x:11:5", owner=True)
        self.assertIn("تعیین شد", callback.answer.await_args.args[0])
        service = await self.session.get(UserService, 11)
        await self.session.refresh(service)
        self.assertEqual((service.shop_id, service.shop_needs_review), (5, False))
        callback, _ = await self.press("ssr:x:11:5", owner=True)
        self.assertIn("در انتظار", callback.answer.await_args.args[0])

    async def test_unknown_shop_is_rejected_before_any_change(self) -> None:
        callback, edit = await self.press("ssr:c:11:77", owner=True)
        callback.answer.assert_awaited_once_with("فروشگاه نامعتبر است", show_alert=True)
        edit.assert_not_awaited()
        callback, _ = await self.press("ssr:x:11:77", owner=True)
        self.assertIn("فروشگاه یافت نشد", callback.answer.await_args.args[0])
        service = await self.session.get(UserService, 11)
        await self.session.refresh(service)
        self.assertTrue(service.shop_needs_review)

    async def test_missing_service_is_not_found(self) -> None:
        callback, edit = await self.press("ssr:v:12", owner=True)
        callback.answer.assert_awaited_once_with(feedback.NOT_FOUND, show_alert=True)
        edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
