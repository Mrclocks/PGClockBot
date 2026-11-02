"""Screens that used to end without any navigation now offer «بازگشت»."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from app.bot import keyboards as kb
from app.bot.handlers import admin_pg_users, reseller


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class DeadEndScreenTests(unittest.IsolatedAsyncioTestCase):
    def test_custom_plan_days_can_go_back(self):
        self.assertIn("shop:custom", _callbacks(kb.custom_days_keyboard(30, {})))

    def test_reseller_info_screens_return_to_their_group(self):
        self.assertEqual(_callbacks(reseller._group_back("res_stats")), ["nv:res:grp:account"])
        self.assertEqual(_callbacks(reseller._group_back("res_billing")), ["nv:res:grp:account"])

    async def test_pg_user_card_error_offers_back_to_users(self):
        target = MagicMock()
        target.edit_text = AsyncMock()
        target.answer = AsyncMock()
        await admin_pg_users._show_user_card(target, 5, edit=False, pg=None)
        markup = target.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(_callbacks(markup), ["adm:pg:users"])


if __name__ == "__main__":
    unittest.main()
