"""Regression test: POST /setup/bot must reject placeholder/example bot tokens.

Before the fix, ``setup_bot`` only checked the token for emptiness, so a
first-run operator could paste the documented example token
(``123456:ABC-DEF``) straight from the README and "complete" setup with a
non-functional bot — a subtle foot-gun that also weakens the setup gate's
guarantee that a real bot is wired up. ``is_placeholder_bot_token`` already
existed elsewhere but wasn't consulted here.
"""

from __future__ import annotations

import unittest

from app.services.security_policy import is_placeholder_bot_token


class SetupBotPlaceholderRejectionTests(unittest.TestCase):
    """The rule itself; tests/test_setup_wizard_routes.py drives POST /setup/bot with it."""

    def test_known_example_tokens_are_flagged(self):
        # Same values documented in README / .env.example — must never pass.
        for example in ("123456:ABC-DEF", "YOUR_BOT_TOKEN", "", "0000000000:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"):
            self.assertTrue(is_placeholder_bot_token(example), f"expected {example!r} to be flagged as placeholder")

    def test_realistic_token_is_not_flagged(self):
        self.assertFalse(is_placeholder_bot_token("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"))


if __name__ == "__main__":
    unittest.main()
