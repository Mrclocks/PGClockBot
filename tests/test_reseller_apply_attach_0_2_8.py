"""Reseller-apply: inline modes + lasting apply chrome (never main menu)."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class ResellerApplyAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_reseller_apply_inline_only(self):
        """Option B: apply flow is one inline message (no lasting apply chrome)."""
        from app.bot.handlers.reply_nav import open_reseller_apply
        from app.db.models import Role

        message = AsyncMock()
        message.answer = AsyncMock(return_value=AsyncMock())

        session = AsyncMock()
        db_user = MagicMock()
        db_user.role = Role.USER.value
        state = AsyncMock()

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📦 ثابت — 1 پلن", callback_data="resapply:mode:fixed")],
                [InlineKeyboardButton(text="⚡ PAYG — 0 پلن", callback_data="resapply:mode:payg")],
            ]
        )

        with (
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                new=AsyncMock(return_value=("MAIN", None, None)),
            ),
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                new=AsyncMock(
                    return_value={
                        "nav_mode": "classic",
                        "menu_order": "shop,reseller_apply",
                    }
                ),
            ),
            patch(
                "app.services.resellers.list_active_reseller_plans",
                new=AsyncMock(side_effect=[[MagicMock()], []]),
            ),
            patch(
                "app.bot.handlers.reseller._resapply_mode_keyboard",
                new=AsyncMock(return_value=markup),
            ),
            patch(
                "app.bot.handlers.reply_nav.nav.set_nav_level",
                new=AsyncMock(),
            ) as set_nav,
        ):
            await open_reseller_apply(message, session, db_user, state)

        set_nav.assert_awaited()
        self.assertEqual(message.answer.await_count, 1)
        call = message.answer.await_args
        self.assertIs(call.kwargs.get("reply_markup"), markup)
        self.assertIn("درخواست نمایندگی", call.args[0])
        self.assertIsNot(call.kwargs.get("reply_markup"), "MAIN")


class ResellerApplySourceGuards(unittest.TestCase):
    def test_no_orphan_plan_type_caption(self):
        fn = inspect.getsource(
            __import__(
                "app.bot.handlers.reply_nav", fromlist=["open_reseller_apply"]
            ).open_reseller_apply
        )
        self.assertNotIn('"نوع پلن:"', fn)
        self.assertNotIn("'نوع پلن:'", fn)
        self.assertIn("_resapply_mode_keyboard", fn)
        self.assertIn("present_inline_only", fn)
        self.assertNotIn('text="⌨️ منوی اصلی"', fn)
        self.assertIn("NAV_RESELLER_APPLY", fn)


if __name__ == "__main__":
    unittest.main()
