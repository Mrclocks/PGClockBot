"""Wave F: polish remaining chrome leaks after Wave E (no new features)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class FillerChromeWaveFTests(unittest.TestCase):
    def test_extra_filler_texts(self):
        from app.bot.nav_inline import is_filler_chrome_text

        for t in ("⬇️", "⌨️ بکاپ", "⌨️ نمایندگان", "پشتیبانی:", "تیکت:", "تیکت‌ها:"):
            self.assertTrue(is_filler_chrome_text(t), t)


class FilteredPgKeyboardInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_heals_to_main_kb(self):
        from app.bot.auth import filtered_pg_reply_keyboard

        session = AsyncMock()
        db_user = MagicMock()
        main_kb = MagicMock(name="MAIN")

        with (
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(main_kb, {}, "admin")),
            ),
            patch(
                "app.bot.keyboards.pg_reply_keyboard",
                return_value="PG_CHROME",
            ) as chrome,
        ):
            out = await filtered_pg_reply_keyboard(
                db_user, session=session, ui={"nav_mode": "inline"}
            )

        self.assertIs(out, main_kb)
        chrome.assert_not_called()


class SyncPlansInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_plans_inline_no_down_arrow(self):
        from app.bot.handlers.admin_plans import sync_plans_reply_keyboard

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"_adm_plans_aud": "users"})
        state.update_data = AsyncMock()

        with (
            patch(
                "app.bot.handlers.admin_plans.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.handlers.admin_plans.nav.set_nav_level",
                new_callable=AsyncMock,
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(MagicMock(name="MAIN"), {}, "admin")),
            ),
        ):
            await sync_plans_reply_keyboard(
                message, session, db_user, state, audience="users"
            )

        texts = [c.args[0] for c in message.answer.await_args_list if c.args]
        self.assertFalse(any(t == "⬇️" for t in texts))
        first_markup = message.answer.await_args_list[0].kwargs.get("reply_markup")
        data = [b.callback_data for row in first_markup.inline_keyboard for b in row]
        self.assertTrue(
            any(
                d.startswith("nv:adm:ra:adm_plans_") or d.startswith("nv:adm:plans:")
                for d in data
            )
        )


class SupportLegacyInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_support_list_empty_uses_hub(self):
        from app.bot.handlers.support import support_list

        callback = AsyncMock()
        callback.message = AsyncMock()
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1

        with (
            patch(
                "app.bot.handlers.support.list_user_tickets",
                AsyncMock(return_value=[]),
            ),
            patch(
                "app.bot.handlers.support.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.handlers.support.safe_edit_text",
                new_callable=AsyncMock,
            ) as edit,
            patch(
                "app.bot.handlers.support.kb.support_reply_keyboard",
                return_value="SUPPORT_CHROME",
            ) as chrome,
        ):
            await support_list(callback, session, db_user)

        edit.assert_awaited()
        markup = edit.await_args.kwargs.get("reply_markup")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nv:s:new", data)
        callback.message.answer.assert_not_awaited()
        chrome.assert_not_called()


class WholesaleQtyInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_qty_heals_main_not_shop_chrome(self):
        from app.bot.handlers.shop import wholesale_qty_entered

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        message.text = "3"
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"wholesale_plan_id": 9})
        state.update_data = AsyncMock()
        state.set_state = AsyncMock()
        state.clear = AsyncMock()
        plan = MagicMock()
        plan.id = 9
        plan.name = "P"
        plan.price = 1000
        qty_kb = MagicMock()
        qty_kb.inline_keyboard = []

        with (
            patch(
                "app.bot.handlers.shop.get_all_settings",
                AsyncMock(
                    return_value={
                        "nav_mode": "inline",
                        "custom_plan_enabled": "0",
                        "wholesale_min": "1",
                        "wholesale_max": "100",
                    }
                ),
            ),
            patch(
                "app.bot.handlers.shop.wholesale_bounds",
                return_value=(1, 100),
            ),
            patch(
                "app.bot.handlers.shop.get_catalog_plan",
                AsyncMock(return_value=plan),
            ),
            patch(
                "app.bot.handlers.shop.parse_wholesale_tiers",
                return_value=[],
            ),
            patch(
                "app.bot.handlers.shop.wholesale_tier_percent",
                return_value=0,
            ),
            patch(
                "app.bot.handlers.shop.calc_wholesale_price",
                return_value=(3000, 0),
            ),
            patch(
                "app.bot.handlers.shop.get_settings",
                return_value=MagicMock(currency="IRT"),
            ),
            patch(
                "app.bot.handlers.shop.format_toman",
                return_value="3,000",
            ),
            patch(
                "app.bot.handlers.shop.kb.is_cancel_text",
                return_value=False,
            ),
            patch(
                "app.bot.handlers.shop.kb.wholesale_qty_keyboard",
                return_value=qty_kb,
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(MagicMock(name="MAIN"), {}, "user")),
            ),
            patch(
                "app.bot.menu_nav.set_nav_level",
                new_callable=AsyncMock,
            ),
            patch(
                "app.bot.tg_utils.attach_reply_keyboard",
                new_callable=AsyncMock,
            ) as attach,
            patch(
                "app.bot.handlers.shop.kb.shop_reply_keyboard",
                return_value="SHOP_CHROME",
            ) as shop_chrome,
        ):
            await wholesale_qty_entered(
                message, state, session, db_user, is_reseller_bot=False
            )

        shop_chrome.assert_not_called()
        attach.assert_awaited()
        first_markup = message.answer.await_args_list[0].kwargs.get("reply_markup")
        self.assertIs(first_markup, qty_kb)


class StaffLoyaltyAnswerInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_staff_loyalty_content_inline(self):
        from app.bot.handlers.loyalty import _staff_loyalty_answer
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        content = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="x", callback_data="loyadm:rules")]
            ]
        )
        main_kb = MagicMock(name="MAIN")

        with (
            patch(
                "app.services.users.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(main_kb, {}, "admin")),
            ),
            patch(
                "app.bot.keyboards.admin_loyalty_reply_keyboard",
                return_value="LOY_CHROME",
            ) as chrome,
        ):
            await _staff_loyalty_answer(
                message,
                session,
                db_user,
                "rules body",
                can_tiers=True,
                content_inline=content,
            )

        chrome.assert_not_called()
        first = message.answer.await_args_list[0]
        self.assertEqual(first.args[0], "rules body")
        self.assertIs(first.kwargs.get("reply_markup"), content)
        second = message.answer.await_args_list[1]
        self.assertIs(second.kwargs.get("reply_markup"), main_kb)


class AdmPgCallbackInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_adm_pg_opens_pg_home(self):
        from app.bot.handlers.admin_pg_users import adm_pg

        callback = AsyncMock()
        callback.message = AsyncMock()
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()

        with (
            patch(
                "app.bot.auth.bot_may_open_pg_hub",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.services.users.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.handlers.reply_nav.open_pg_home",
                new_callable=AsyncMock,
            ) as open_home,
        ):
            await adm_pg(
                callback,
                db_user,
                session,
                is_reseller_bot=False,
                reseller_profile_id=None,
                reseller_owner_id=None,
            )

        open_home.assert_awaited()
        callback.message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
