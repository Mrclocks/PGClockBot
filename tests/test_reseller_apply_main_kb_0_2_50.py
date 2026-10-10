"""Reseller-apply must appear on the customer main ReplyKeyboard (/start home)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class DirectMainReplyKeyboardInfersApply(unittest.TestCase):
    def test_none_flag_infers_from_settings(self):
        from app.bot import keyboards as kb
        from app.services.users import DEFAULT_SETTINGS

        ui = dict(DEFAULT_SETTINGS)
        markup = kb.main_reply_keyboard("user", has_services=False, ui=ui)
        labels = [b.text for row in markup.keyboard for b in row]
        self.assertIn(ui["btn_reseller_apply"], labels)

    def test_explicit_false_hides(self):
        from app.bot import keyboards as kb
        from app.services.users import DEFAULT_SETTINGS

        ui = dict(DEFAULT_SETTINGS)
        markup = kb.main_reply_keyboard(
            "user", has_services=False, ui=ui, show_reseller_apply=False
        )
        labels = [b.text for row in markup.keyboard for b in row]
        self.assertNotIn(ui["btn_reseller_apply"], labels)


class RenderHomeUsesBuildMain(unittest.IsolatedAsyncioTestCase):
    async def test_user_home_calls_build_main_reply_keyboard(self):
        from app.bot.handlers.start import render_home

        message = AsyncMock()
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.full_name = "Ali"
        db_user.telegram_id = 1
        db_user.username = "ali"
        db_user.id = 9
        db_user.role = "user"
        ui = {
            "welcome_text": "سلام",
            "shop_title": "فروشگاه",
            "menu_order": "shop,support,reseller_apply,miniapp",
            "show_reseller_apply": "1",
            "btn_reseller_apply": "🤝 درخواست نمایندگی",
        }

        with (
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(MagicMock(), ui, "user")),
            ) as build,
            patch(
                "app.services.rich_text.outbound_setting_text",
                return_value=("سلام", {}),
            ),
        ):
            await render_home(
                message,
                session,
                db_user,
                ui=ui,
                is_reseller_bot=False,
                reseller_owner_id=None,
                effective_role="user",
            )

        build.assert_awaited()
        message.answer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
