"""3.6.6 — fix نمایندگان label collision; shop chrome on reply; no static inline hubs."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class ResellersLabelCollisionTests(unittest.TestCase):
    def test_admin_hub_maps_to_adm_resellers(self):
        from app.bot.keyboards import reply_action_map

        ui = {
            "btn_back": "⬅️ بازگشت",
            "btn_menu_home": "🏠 منوی اصلی",
            "menu_layout": "compact",
        }
        mapping = reply_action_map("admin", ui=ui, include_submenus=True, is_reseller_bot=False)
        self.assertEqual(mapping["🤝 نمایندگان"], "adm_resellers")
        self.assertEqual(mapping["🤝 فقط نمایندگان"], "bc_aud_resellers")
        self.assertNotEqual(mapping["🤝 نمایندگان"], "bc_aud_resellers")

    def test_broadcast_keyboard_unique_label(self):
        from app.bot.keyboards import (
            admin_broadcast_reply_keyboard,
            admin_people_reply_keyboard,
            admin_reply_keyboard,
        )

        ui = {"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی", "menu_layout": "compact"}
        admin_flat = [b.text for row in admin_reply_keyboard(ui).keyboard for b in row]
        people_flat = [b.text for row in admin_people_reply_keyboard(ui).keyboard for b in row]
        bc_flat = [b.text for row in admin_broadcast_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("👤 افراد", admin_flat)
        self.assertNotIn("🤝 نمایندگان", admin_flat)
        self.assertIn("🤝 نمایندگان", people_flat)
        self.assertIn("🤝 فقط نمایندگان", bc_flat)
        self.assertNotIn("🤝 نمایندگان", bc_flat)

    def test_classic_and_compact_both_reply(self):
        from app.bot.keyboards import admin_people_reply_keyboard, admin_reply_keyboard, admin_resellers_reply_keyboard

        for layout in ("classic", "compact"):
            ui = {
                "menu_layout": layout,
                "btn_back": "⬅️ بازگشت",
                "btn_menu_home": "🏠 منوی اصلی",
            }
            admin = admin_reply_keyboard(ui)
            people = admin_people_reply_keyboard(ui)
            res = admin_resellers_reply_keyboard(ui)
            self.assertTrue(admin.keyboard)
            self.assertTrue(people.keyboard)
            self.assertTrue(res.keyboard)
            flat = [b.text for row in people.keyboard for b in row]
            self.assertIn("🤝 نمایندگان", flat)
            if layout == "compact":
                # paired rows (except possible odd last + footer)
                self.assertGreaterEqual(len(admin.keyboard[0]), 1)
            else:
                self.assertEqual(len(admin.keyboard[0]), 1)


class ShopChromeReplyTests(unittest.TestCase):
    def test_plans_keyboard_names_only(self):
        from app.bot.keyboards import plans_keyboard, shop_reply_keyboard

        class P:
            id = 1
            name = "Demo"
            price = 10000
            is_trial = False

        kb = plans_keyboard([P()], custom_enabled=True, wholesale_enabled=True)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Demo" in t for t in texts))
        self.assertFalse(any("دلخواه" in t for t in texts))
        self.assertFalse(any("عمده" in t for t in texts))
        # Inline back to kind picker is intentional chrome
        self.assertTrue(any("بازگشت" in t for t in texts))

        shop = shop_reply_keyboard(
            {"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"},
        )
        sflat = [b.text for row in shop.keyboard for b in row]
        self.assertNotIn("✨ پلن دلخواه", sflat)
        self.assertNotIn("عمده", sflat)
        self.assertIn("⬅️ بازگشت", sflat)

    def test_support_contacts_url_only(self):
        from app.bot.keyboards import support_contacts_keyboard

        kb = support_contacts_keyboard(
            [{"title": "Support", "telegram": "@support_bot"}],
            {"btn_back": "⬅️ بازگشت"},
        )
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Support" in t for t in texts))
        self.assertFalse(any("تیکت" in t for t in texts))
        self.assertFalse(any("بازگشت" in t for t in texts))


class CancelBroadcastSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_cancel_skips_fsm_to_state_handlers(self):
        from aiogram.dispatcher.event.bases import SkipHandler

        from app.bot.handlers.reply_nav import global_cancel_restore

        state = AsyncMock()
        state.get_state = AsyncMock(return_value="AdminStates:broadcast_text")
        with patch(
            "app.bot.handlers.reply_nav.restore_main_reply", AsyncMock()
        ) as restore:
            with self.assertRaises(SkipHandler):
                await global_cancel_restore(
                    AsyncMock(), AsyncMock(), MagicMock(), state
                )
        restore.assert_not_awaited()

    async def test_global_cancel_outside_fsm_restores_main_menu(self):
        from app.bot.handlers.reply_nav import global_cancel_restore

        state = AsyncMock()
        state.get_state = AsyncMock(return_value=None)
        with patch(
            "app.bot.handlers.reply_nav.restore_main_reply", AsyncMock()
        ) as restore:
            await global_cancel_restore(AsyncMock(), AsyncMock(), MagicMock(), state)
        restore.assert_awaited_once()

    async def test_broadcast_cancel_reopens_hub_without_sending(self):
        from app.bot.handlers.admin import adm_broadcast_send
        from app.bot.handlers.reply_nav import open_admin_broadcast_hub

        message = MagicMock()
        message.text = "انصراف"
        message.answer = AsyncMock()
        state = AsyncMock()
        db_user = MagicMock()
        with (
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            patch("app.bot.handlers.admin._is_admin", MagicMock(return_value=True)),
            patch("app.bot.nav_input.try_legacy_cancel", AsyncMock(return_value=False)),
            patch("app.bot.nav_chrome.answer_staff_nav", AsyncMock()) as staff_nav,
            patch("app.services.broadcast.send_broadcast", AsyncMock()) as send,
        ):
            await adm_broadcast_send(
                message, state, session=AsyncMock(), db_user=db_user
            )

        send.assert_not_awaited()
        staff_nav.assert_awaited_once()
        kwargs = staff_nav.await_args.kwargs
        self.assertIs(kwargs["reopen_panel"], open_admin_broadcast_hub)
        self.assertTrue(kwargs["clear_state"])


if __name__ == "__main__":
    unittest.main()
