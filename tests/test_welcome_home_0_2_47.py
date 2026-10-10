"""Welcome = one message; menu:home edit fallback; trial first; support apply scope."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest

import app.bot.keyboards  # noqa: F401


class ShopKindTrialFirstTests(unittest.TestCase):
    def test_trial_is_first_button_when_enabled(self):
        from app.bot.keyboards import shop_kind_keyboard

        kb = shop_kind_keyboard(
            {},
            fixed_on=True,
            trial_on=True,
            custom_on=True,
            wholesale_on=True,
        )
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertEqual(flat[0], "shop:kind:trial")
        self.assertIn("shop:kind:fixed", flat)


class SupportResellerApplyScopeTests(unittest.TestCase):
    def test_hub_includes_apply_only_when_flag_true(self):
        from app.bot.nav_inline import support_hub_keyboard

        with_apply = support_hub_keyboard({}, include_reseller_apply=True)
        without = support_hub_keyboard({}, include_reseller_apply=False)
        data_with = [b.callback_data for row in with_apply.inline_keyboard for b in row]
        data_without = [b.callback_data for row in without.inline_keyboard for b in row]
        self.assertIn("nv:resapply", data_with)
        self.assertNotIn("nv:resapply", data_without)


class OpenSupportHomeShopBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_bot_never_shows_reseller_apply(self):
        from app.bot.handlers.reply_nav import open_support_home

        message = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.role = "user"
        state = AsyncMock()
        captured = {}

        def _capture_kb(*_a, **kw):
            captured["include"] = kw.get("include_reseller_apply")
            return MagicMock()

        with (
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                new=AsyncMock(
                    return_value={
                        "menu_order": "shop,support,reseller_apply",
                        "show_reseller_apply": "1",
                        "support_text": "help",
                        "support_contacts": "[]",
                    }
                ),
            ),
            patch(
                "app.bot.nav_inline.present_inline_only",
                new=AsyncMock(),
            ),
            patch(
                "app.bot.nav_inline.support_hub_keyboard",
                side_effect=_capture_kb,
            ),
            patch(
                "app.services.support_contacts.parse_support_contacts",
                return_value=[],
            ),
            patch(
                "app.services.support_contacts.active_support_contacts",
                return_value=[],
            ),
            patch(
                "app.services.rich_text.outbound_setting_text",
                return_value=("help", {}),
            ),
            patch(
                "app.bot.handlers.reply_nav.nav.set_nav_level",
                new=AsyncMock(),
            ),
        ):
            await open_support_home(
                message,
                session,
                db_user,
                state,
                is_reseller_bot=True,
                reseller_owner_id=5,
            )

        self.assertFalse(captured.get("include"))


class RenderHomeEditFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_failure_falls_back_to_answer(self):
        from app.bot.handlers.start import render_home

        message = AsyncMock()
        message.photo = None
        message.edit_text = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(), message="message can't be edited"
            )
        )
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        db_user.role = "user"
        db_user.full_name = "U"
        db_user.telegram_id = 10
        db_user.username = None

        with (
            patch(
                "app.bot.handlers.start.get_all_settings",
                new=AsyncMock(return_value={"welcome_text": "سلام", "shop_title": ""}),
            ),
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value="user"),
            ),
            patch(
                "app.services.reseller_access.is_shop_owner_on_main_bot",
                return_value=False,
            ),
            patch(
                "app.bot.handlers.start._has_services",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.services.rich_text.outbound_setting_text",
                return_value=("سلام", {}),
            ),
            patch(
                "app.bot.handlers.start.kb.main_reply_keyboard",
                return_value=MagicMock(name="KB"),
            ),
        ):
            await render_home(
                message,
                session,
                db_user,
                edit=True,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        message.answer.assert_awaited()
        self.assertIsNotNone(message.answer.await_args.kwargs.get("reply_markup"))

    async def test_not_modified_stays_silent(self):
        from app.bot.handlers.start import render_home

        message = AsyncMock()
        message.photo = None
        message.edit_text = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(), message="Bad Request: message is not modified"
            )
        )
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        db_user.role = "user"
        db_user.full_name = "U"
        db_user.telegram_id = 10
        db_user.username = None

        with (
            patch(
                "app.bot.handlers.start.get_all_settings",
                new=AsyncMock(return_value={"welcome_text": "سلام", "shop_title": ""}),
            ),
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value="user"),
            ),
            patch(
                "app.services.reseller_access.is_shop_owner_on_main_bot",
                return_value=False,
            ),
            patch(
                "app.bot.handlers.start._has_services",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.services.rich_text.outbound_setting_text",
                return_value=("سلام", {}),
            ),
            patch(
                "app.bot.handlers.start.kb.main_reply_keyboard",
                return_value=MagicMock(name="KB"),
            ),
        ):
            await render_home(
                message,
                session,
                db_user,
                edit=True,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
