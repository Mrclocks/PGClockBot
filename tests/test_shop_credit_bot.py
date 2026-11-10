"""Owner credit panel on the reseller card: summary text and freeze target."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.handlers.shop_credit_bot import (
    HELP_LINE,
    TEMP_LIMIT_MAX_DAYS,
    STATUS_LABELS,
    ShopCreditStates,
    _credit_keyboard,
    credit_summary_lines,
)
from app.bot.keyboards import admin_reseller_actions
from app.services.shop_credit import CreditStatus, CreditUsage


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class ShopCreditBotUiTests(unittest.TestCase):
    def test_reseller_card_offers_credit_panel(self):
        self.assertIn("adm:resellers:credit:9", _callbacks(admin_reseller_actions(9)))

    def test_credit_summary_includes_status_amounts_and_optional_help(self):
        overview = SimpleNamespace(
            usage=CreditUsage(limit=100_000, used=25_000),
            balance=10_000,
            status=CreditStatus.ACTIVE,
            account=None,
        )
        text = "\n".join(credit_summary_lines(overview, currency="IRT", with_help=True))
        self.assertIn(STATUS_LABELS[CreditStatus.ACTIVE], text)
        self.assertIn("100٬000", text)
        self.assertIn("25٬000", text)
        self.assertIn(HELP_LINE, text)

    def test_freeze_button_targets_the_opposite_state(self):
        frozen = _callbacks(_credit_keyboard(7, frozen=True))
        open_ = _callbacks(_credit_keyboard(7, frozen=False))
        self.assertIn("adm:resellers:credit:freeze:7:0", frozen)
        self.assertIn("adm:resellers:credit:freeze:7:1", open_)
        self.assertIn("adm:resellers:credit:limit:7", open_)
        self.assertIn("adm:resellers:credit:temp:7", open_)

    def test_limit_prompt_uses_named_fsm_state(self):
        self.assertTrue(str(ShopCreditStates.waiting_limit.state).endswith("waiting_limit"))
        self.assertTrue(str(ShopCreditStates.waiting_temp_amount.state).endswith("waiting_temp_amount"))
        self.assertTrue(str(ShopCreditStates.waiting_temp_days.state).endswith("waiting_temp_days"))
        self.assertEqual(TEMP_LIMIT_MAX_DAYS, 365)


if __name__ == "__main__":
    unittest.main()
