"""Reply keyboard + 4-square menu icon must never be cleared by chrome tip-delete."""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReplyKeyboardChromeDeleteGuards(unittest.TestCase):
    def test_shop_picker_never_deletes_reply_chrome(self):
        from app.bot.handlers import shop

        src = inspect.getsource(shop.present_shop_kind_picker)
        self.assertIn("present_inline_only", src)
        self.assertNotIn("chrome.delete", src)
        self.assertNotIn("\\u2060", src)
        self.assertNotIn("\u2060", src)
        # No try/delete tip pattern in the helper
        self.assertIsNone(re.search(r"\.delete\(", src))

    def test_reseller_apply_never_deletes_reply_chrome(self):
        from app.bot.handlers import reply_nav

        src = inspect.getsource(reply_nav.open_reseller_apply)
        self.assertIn("present_inline_only", src)
        self.assertNotIn("chrome.delete", src)
        self.assertIsNone(re.search(r"await chrome\.delete", src))

    def test_handlers_have_no_zwsp_chrome_delete(self):
        """Regression: \\u2060 tip + delete was the v0.2.8 iOS keyboard wipe."""
        for rel in (
            "app/bot/handlers/shop.py",
            "app/bot/handlers/reply_nav.py",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn('"\\u2060"', text)
            self.assertNotIn("'\\u2060'", text)
            self.assertNotIn('"\u2060"', text)
            self.assertNotIn("'\u2060'", text)

    def test_attach_reply_keyboard_documents_ios_menu_icon(self):
        from app.bot import tg_utils

        doc = inspect.getdoc(tg_utils.attach_reply_keyboard) or ""
        self.assertIn("Never delete", doc)
        self.assertIn("4-square", doc)


if __name__ == "__main__":
    unittest.main()
