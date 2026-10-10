"""Wave B: service card inline actions (no service_actions reply chrome)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class ServiceCardKeyboardTests(unittest.TestCase):
    def test_callbacks_reuse_svc_prefix(self):
        from app.bot.nav_inline import service_card_keyboard

        kb = service_card_keyboard(42, {"btn_renew": "تمدید", "btn_sub_link": "لینک"})
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("svc:renew:42", data)
        self.assertIn("svc:addon:42", data)
        self.assertIn("svc:link:42", data)
        self.assertIn("svc:auto:42", data)
        self.assertIn("svc:delask:42", data)
        self.assertIn("svc:view:42", data)  # refresh
        self.assertIn("nv:svc:guide:42", data)
        self.assertIn("svc:list", data)
        # No new money/mutation prefixes
        self.assertTrue(all(d.startswith(("svc:", "nv:svc:")) for d in data))

    def test_list_keyboard_adds_home_back(self):
        from app.bot.nav_inline import services_list_keyboard

        svc = MagicMock()
        svc.id = 7
        svc.pg_username = "u1"
        kb = services_list_keyboard([svc], {})
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("svc:view:7", data)
        self.assertIn("menu:home", data)


class SvcViewInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_nav_edits_with_card_keyboard(self):
        from app.bot.handlers.services import svc_view

        callback = AsyncMock()
        callback.data = "svc:view:9"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        svc = MagicMock()
        svc.id = 9
        svc.bot_user_id = 1
        svc.pg_username = "demo"
        session.get = AsyncMock(return_value=svc)
        db_user = MagicMock()
        db_user.id = 1
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()

        with (
            patch(
                "app.bot.handlers.services.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.handlers.services.fetch_live_service_info",
                AsyncMock(return_value={"username": "demo"}),
            ),
            patch(
                "app.bot.handlers.services.service_card",
                return_value="CARD",
            ),
            patch(
                "app.bot.handlers.services.safe_edit_text",
                new_callable=AsyncMock,
            ) as edit,
            patch(
                "app.bot.handlers.services.format_message",
                return_value="TITLE\nCARD",
            ),
        ):
            await svc_view(callback, session, db_user, state)

        edit.assert_awaited()
        markup = edit.await_args.kwargs.get("reply_markup")
        self.assertIsNotNone(markup)
        self.assertTrue(hasattr(markup, "inline_keyboard"))
        callback.message.answer.assert_not_awaited()

    async def test_stale_classic_still_edits_inline_card(self):
        """Option B: nav_mode=classic does not restore reply-keyboard actions."""
        from app.bot.handlers.services import svc_view

        callback = AsyncMock()
        callback.data = "svc:view:9"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        svc = MagicMock()
        svc.id = 9
        svc.bot_user_id = 1
        svc.pg_username = "demo"
        session.get = AsyncMock(return_value=svc)
        db_user = MagicMock()
        db_user.id = 1

        with (
            patch(
                "app.bot.handlers.services.get_all_settings",
                AsyncMock(return_value={"nav_mode": "classic"}),
            ),
            patch(
                "app.bot.handlers.services.fetch_live_service_info",
                AsyncMock(return_value={"username": "demo"}),
            ),
            patch(
                "app.bot.handlers.services.service_card",
                return_value="CARD",
            ),
            patch(
                "app.bot.handlers.services.safe_edit_text",
                new_callable=AsyncMock,
            ) as edit,
            patch(
                "app.bot.handlers.services.format_message",
                return_value="TITLE\nCARD",
            ),
        ):
            await svc_view(callback, session, db_user, state=None)

        edit.assert_awaited()
        markup = edit.await_args.kwargs.get("reply_markup")
        self.assertTrue(hasattr(markup, "inline_keyboard"))
        callback.message.answer.assert_not_awaited()


class LegacyServiceLabelsTests(unittest.TestCase):
    def test_reply_action_map_keeps_service_labels(self):
        from app.bot.reply_keyboards import reply_action_map
        from app.db.models import Role

        ui = {
            "nav_mode": "inline",
            "menu_order": "shop,services",
            "btn_sub_link": "لینک و QR",
            "btn_renew": "تمدید",
            "btn_svc_addon": "حجم",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        mapping = reply_action_map(
            Role.USER.value, has_services=True, ui=ui, include_submenus=True
        )
        self.assertEqual(mapping.get("لینک و QR"), "svc_link")
        self.assertEqual(mapping.get("تمدید"), "svc_renew")


class OpenServicesSingleShortcutTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_service_opens_card(self):
        from app.bot.handlers.reply_nav import open_services_list

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        svc = MagicMock()
        svc.id = 3
        result = MagicMock()
        result.scalars.return_value.all.return_value = [svc]
        session.execute = AsyncMock(return_value=result)

        with (
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=("KB", {}, "user")),
            ),
            patch(
                "app.bot.handlers.services.svc_view",
                new_callable=AsyncMock,
            ) as view,
        ):
            await open_services_list(message, session, db_user, state=None)

        view.assert_awaited()
        # Only the placeholder bubble — not a list + chrome
        self.assertEqual(message.answer.await_count, 1)


if __name__ == "__main__":
    unittest.main()
