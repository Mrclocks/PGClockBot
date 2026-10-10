"""Option B nav contract: inline-only content; lasting main ReplyKeyboard only."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


ROOT = Path(__file__).resolve().parents[1]


class DualKeyboardContractSourceGuards(unittest.TestCase):
    def test_present_inline_only_documents_no_chrome(self):
        from app.bot.nav_inline import present_inline_only

        doc = inspect.getdoc(present_inline_only) or ""
        self.assertIn("no reply-chrome", doc.lower())

    def test_reseller_apply_inline_only(self):
        from app.bot.handlers import reply_nav

        src = inspect.getsource(reply_nav.open_reseller_apply)
        self.assertIn("present_inline_only", src)
        self.assertNotIn("present_inline_with_reply_chrome", src)
        self.assertNotIn("reseller_apply_reply_keyboard", src)
        self.assertNotIn('chrome_text="⌨️ منوی اصلی"', src)
        self.assertNotIn('text="⌨️ منوی اصلی"', src)

    def test_shop_uses_inline_only_present(self):
        from app.bot.handlers import shop

        src = inspect.getsource(shop.present_shop_kind_picker)
        self.assertIn("present_inline_only", src)
        self.assertIn("shop_kind_keyboard", src)
        self.assertNotIn("present_inline_with_reply_chrome", src)
        self.assertNotIn("shop_reply_keyboard", src)

    def test_services_list_inline_only(self):
        from app.bot.handlers import reply_nav

        src = inspect.getsource(reply_nav.open_services_list)
        self.assertIn("present_inline_only", src)
        self.assertNotIn("present_inline_with_reply_chrome", src)

    def test_support_list_inline_only(self):
        from app.bot.handlers import reply_nav

        src = inspect.getsource(reply_nav.open_support_list)
        self.assertIn("present_inline_only", src)
        self.assertNotIn("present_inline_with_reply_chrome", src)

    def test_support_home_inline_only(self):
        from app.bot.handlers import reply_nav

        src = inspect.getsource(reply_nav.open_support_home)
        self.assertIn("present_inline_only", src)
        self.assertIn("support_hub_keyboard", src)
        self.assertNotIn("attach_reply_keyboard", src)
        self.assertNotIn("support_reply_keyboard", src)

    def test_show_nav_keyboard_no_classic_submenu_builders(self):
        from app.bot import menu_nav as nav

        src = inspect.getsource(nav.show_nav_keyboard)
        for name in (
            "shop_reply_keyboard",
            "support_reply_keyboard",
            "wallet_reply_keyboard",
            "loyalty_reply_keyboard",
            "reseller_apply_reply_keyboard",
            "admin_ops_reply_keyboard",
            "admin_people_reply_keyboard",
            "admin_product_reply_keyboard",
            "admin_system_reply_keyboard",
            "reseller_reply_keyboard",
        ):
            self.assertNotIn(name, src)

    def test_nav_reseller_apply_constant(self):
        from app.bot import menu_nav as nav

        self.assertEqual(nav.NAV_RESELLER_APPLY, "reseller_apply")
        src = Path("app/bot/menu_nav.py").read_text(encoding="utf-8")
        self.assertIn("NAV_RESELLER_APPLY", src)
        self.assertNotIn("reseller_apply_reply_keyboard", src)


class PresentInlineOnlyHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_present_sends_single_inline_message(self):
        from app.bot.nav_inline import present_inline_only

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        await present_inline_only(
            message,
            text="BODY",
            inline="INLINE",
        )
        self.assertEqual(message.answer.await_count, 1)
        self.assertEqual(
            message.answer.await_args.kwargs.get("reply_markup"), "INLINE"
        )
        self.assertEqual(message.answer.await_args.args[0], "BODY")


if __name__ == "__main__":
    unittest.main()
