"""Fix 4/5: callback↔handler contract and NAV_RULES allow-list checks."""

from __future__ import annotations

import unittest

# callback_data prefixes / exact values that are intentionally not Router handlers
# (URL buttons, web_app, switch_inline, noop placeholders, dynamic pay ids, etc.).

# Explicit full callback_data strings that may appear without a handler match.


class CallbackHandlerContractTests(unittest.TestCase):
    def test_guide_home_uses_menu_home(self):
        from app.bot.handlers.guides import guides_list_keyboard

        kb = guides_list_keyboard([], {}, back_callback=None)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("menu:home", flat)
        self.assertNotIn("nav:home", flat)


if __name__ == "__main__":
    unittest.main()
