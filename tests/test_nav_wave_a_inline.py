"""Wave A: inline-first navigation — stable main KB, no chrome carriers."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class NavModeTests(unittest.TestCase):
    def test_default_inline(self):
        from app.bot.nav_mode import is_inline_nav, nav_mode

        self.assertEqual(nav_mode({}), "inline")
        self.assertTrue(is_inline_nav(None))
        self.assertTrue(is_inline_nav({"nav_mode": "inline"}))
        self.assertFalse(is_inline_nav({"nav_mode": "classic"}))

    def test_default_settings_has_nav_mode(self):
        from app.services.users import DEFAULT_SETTINGS

        self.assertEqual(DEFAULT_SETTINGS.get("nav_mode"), "inline")


class MainKeyboardInlineTests(unittest.TestCase):
    def test_no_home_footer_and_no_reseller_apply(self):
        # Import keyboards first to avoid keyboards↔reply_keyboards circular init.
        import app.bot.keyboards  # noqa: F401
        from app.bot.reply_keyboards import main_reply_keyboard, reply_action_map
        from app.db.models import Role

        ui = {
            "nav_mode": "inline",
            "menu_layout": "compact",
            "menu_order": "shop,services,wallet,support,loyalty,reseller_apply,miniapp",
            "loyalty_enabled": "1",
            "btn_shop": "خرید",
            "btn_services": "سرویس‌ها",
            "btn_wallet": "کیف",
            "btn_support": "پشتیبانی",
            "btn_loyalty": "باشگاه",
            "btn_reseller_apply": "درخواست نمایندگی",
            "btn_miniapp": "مینی‌اپ",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        with patch("app.bot.nav_inline.miniapp_reply_button", return_value=None):
            kb = main_reply_keyboard(
                Role.USER.value, has_services=True, ui=ui
            )
        labels = [b.text for row in kb.keyboard for b in row]
        self.assertNotIn("🏠 منوی اصلی", labels)
        self.assertNotIn("درخواست نمایندگی", labels)
        self.assertIn("خرید", labels)
        self.assertIn("کیف", labels)
        # Legacy map still knows apply label
        mapping = reply_action_map(
            Role.USER.value, has_services=True, ui=ui, include_submenus=True
        )
        self.assertEqual(mapping.get("درخواست نمایندگی"), "reseller_apply")

    def test_classic_keeps_home_footer(self):
        import app.bot.keyboards  # noqa: F401
        from app.bot.reply_keyboards import main_reply_keyboard
        from app.db.models import Role

        ui = {
            "nav_mode": "classic",
            "menu_layout": "compact",
            "menu_order": "shop,wallet",
            "btn_shop": "خرید",
            "btn_wallet": "کیف",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        kb = main_reply_keyboard(Role.USER.value, has_services=False, ui=ui)
        labels = [b.text for row in kb.keyboard for b in row]
        self.assertIn("🏠 منوی اصلی", labels)


class HubKeyboardTests(unittest.TestCase):
    def test_wallet_hub_callbacks(self):
        from app.bot.nav_inline import wallet_hub_keyboard

        kb = wallet_hub_keyboard({})
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("nv:w:topup", data)
        self.assertIn("nv:w:tx", data)
        self.assertIn("menu:home", data)

    def test_support_hub_optional_apply(self):
        from app.bot.nav_inline import support_hub_keyboard

        kb = support_hub_keyboard({}, include_reseller_apply=True)
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("nv:s:new", data)
        self.assertIn("nv:resapply", data)


class PresentInlineOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_message_no_chrome(self):
        from app.bot.nav_inline import present_inline_only

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        await present_inline_only(
            message, text="BODY", inline="INLINE"
        )
        self.assertEqual(message.answer.await_count, 1)
        self.assertEqual(
            message.answer.await_args.kwargs.get("reply_markup"), "INLINE"
        )


class OpenWalletInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_wallet_home_one_message(self):
        from app.bot.handlers.reply_nav import open_wallet_home

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        ui = {"nav_mode": "inline"}

        with (
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                AsyncMock(return_value=ui),
            ),
            patch(
                "app.services.wallet.wallet_balance_for_context",
                AsyncMock(return_value=0),
            ),
            patch("app.config.get_settings") as gs,
        ):
            gs.return_value.currency = "IRT"
            await open_wallet_home(message, session, db_user, state=None)

        self.assertEqual(message.answer.await_count, 1)
        markup = message.answer.await_args.kwargs.get("reply_markup")
        self.assertIsNotNone(markup)
        # Must be inline hub, not ReplyKeyboard
        self.assertTrue(hasattr(markup, "inline_keyboard"))


class FillerGuardTests(unittest.TestCase):
    def test_filler_detection(self):
        from app.bot.nav_inline import is_filler_chrome_text

        self.assertTrue(is_filler_chrome_text("⌨️ منوی اصلی"))
        self.assertTrue(is_filler_chrome_text("📱"))
        self.assertFalse(is_filler_chrome_text("👛 کیف پول"))


if __name__ == "__main__":
    unittest.main()
