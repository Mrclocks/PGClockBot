"""Child-pricing light panel: ACL gate and hub callback wiring."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.handlers import child_pricing_bot as cp
from app.bot.nav_inline import _reseller_entry_buttons
from app.bot.reseller_hub_groups import back_callback_for
from app.services.users import DEFAULT_SETTINGS


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class ChildPricingHubWiringTests(unittest.TestCase):
    def test_customers_hub_uses_dedicated_callback(self):
        buttons = _reseller_entry_buttons(
            [("res_users", "مشتریان"), ("res_child_pricing", "قیمت")],
            dict(DEFAULT_SETTINGS),
            {},
        )
        data = [b.callback_data for b in buttons]
        self.assertIn(cp.PANEL_CALLBACK, data)
        self.assertNotIn("nv:res:ra:res_child_pricing", data)

    def test_panel_back_returns_to_customers_group(self):
        markup = cp._panel_markup(dict(DEFAULT_SETTINGS), web_url="https://panel.example/x")
        self.assertIn(back_callback_for("res_child_pricing"), _callbacks(markup))
        urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
        self.assertEqual(urls, ["https://panel.example/x"])

    def test_hostile_plan_name_is_escaped(self):
        plan = SimpleNamespace(
            id=3, name="<b>x</b>", price_per_gb=1000, payg_user_price=2000
        )
        line = cp._plan_line(plan, currency="IRT")
        self.assertNotIn("<b>x</b>", line)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", line)


class ChildPricingAclTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_actor_gets_no_access(self):
        message = AsyncMock()
        with patch.object(cp, "_parent_shop_id", AsyncMock(return_value=None)):
            await cp.open_child_pricing_panel(
                message, AsyncMock(), SimpleNamespace(), state=AsyncMock(), is_reseller_bot=True
            )
        message.answer.assert_awaited_with(cp.NO_ACCESS)


if __name__ == "__main__":
    unittest.main()
