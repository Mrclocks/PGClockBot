"""v4.2.3 — bot plans dashboard rebuild: inline list + add on reply keyboard."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PLANS = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")
KEYBOARDS = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
REPLY_KB = (ROOT / "app/bot/reply_keyboards.py").read_text(encoding="utf-8")


class PlansDashboardRebuildTests(unittest.TestCase):
    def test_add_plan_on_reply_keyboard_not_kind_picker(self):
        self.assertIn("REPLY_ACTION_ADM_PLANS_ADD", REPLY_KB)
        from app.bot.keyboards import REPLY_ACTION_ADM_PLANS_ADD  # loads reply_keyboards in order
        from app.bot.reply_keyboards import _admin_plans_list_entries

        self.assertEqual(dict(_admin_plans_list_entries())[REPLY_ACTION_ADM_PLANS_ADD], "➕ افزودن پلن")
        list_block = REPLY_KB[
            REPLY_KB.find("def _admin_plans_list_entries")
            : REPLY_KB.find("def _admin_plans_add_type_entries")
        ]
        self.assertIn("REPLY_ACTION_ADM_PLANS_ADD", list_block)
        # Catalog extras (web parity) are allowed on the list reply keyboard
        self.assertIn("REPLY_ACTION_ADM_PLANS_CATEGORIES", list_block)
        self.assertNotIn("REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED", list_block)

    def test_type_picker_after_add(self):
        self.assertIn("def admin_plans_add_type_keyboard", KEYBOARDS)
        self.assertIn("adm:plans:add:users:fixed", KEYBOARDS)
        self.assertIn("adm:plans:add:resellers:payg", KEYBOARDS)
        self.assertIn("open_add_kind_action", ADMIN_PLANS)

    def test_overview_lists_plans_inline(self):
        ov_block = KEYBOARDS[
            KEYBOARDS.find("def admin_users_plans_overview_keyboard")
            : KEYBOARDS.find("def admin_resellers_plans_overview_keyboard")
        ]
        self.assertIn("adm:plan:view:", ov_block)
        self.assertIn("adm:plans:kind:users:trial", ov_block)
        self.assertNotIn("adm:plan:add", ov_block)
        res_block = KEYBOARDS[
            KEYBOARDS.find("def admin_resellers_plans_overview_keyboard")
            : KEYBOARDS.find("def admin_plans_add_type_keyboard")
        ]
        self.assertIn("adm:resplan:view:", res_block)
        self.assertNotIn("adm:resplan:add:", res_block)


class PlansAddFlowNavTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_nav_wires_add_flow(self):
        from app.bot import keyboards as kb
        from app.bot import menu_nav as nav
        from app.bot.handlers import reply_nav

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"_adm_plans_aud": "resellers"})
        with (
            patch.object(reply_nav, "_deny_unless_owner", AsyncMock(return_value=True)),
            patch.object(reply_nav, "get_all_settings", AsyncMock(return_value={})),
            patch.object(nav, "set_nav_level", AsyncMock()) as set_level,
            patch(
                "app.bot.nav_inline.present_nav_panel", AsyncMock()
            ) as present,
        ):
            await reply_nav.dispatch_admin_inline_reply_action(
                AsyncMock(),
                AsyncMock(),
                MagicMock(),
                state,
                kb.REPLY_ACTION_ADM_PLANS_ADD,
            )

        set_level.assert_awaited_once_with(
            state, nav.NAV_ADMIN_PLANS_ADD_TYPE, push=True
        )
        callbacks = [
            b.callback_data
            for row in present.await_args.kwargs["inline"].inline_keyboard
            for b in row
        ]
        self.assertTrue(any(c.startswith("nv:adm:ra:") for c in callbacks))

    async def test_nav_level_for_add_type(self):
        """Back from the add-type step returns to the audience's plan panel."""
        from app.bot import menu_nav as nav
        from app.bot.handlers import reply_nav

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"_adm_plans_aud": "resellers"})
        with (
            patch.object(nav, "get_nav_level", AsyncMock(return_value=nav.NAV_ADMIN_PLANS_ADD_TYPE)),
            patch.object(nav, "pop_nav_level", AsyncMock(return_value=nav.NAV_ADMIN_PLANS_ADD_TYPE)),
            patch.object(reply_nav, "_deny_unless_owner", AsyncMock(return_value=True)),
            patch.object(reply_nav, "present_admin_plans_audience", AsyncMock()) as panel,
        ):
            await reply_nav.handle_back(AsyncMock(), AsyncMock(), MagicMock(), state)

        panel.assert_awaited_once()
        self.assertEqual(panel.await_args.kwargs["audience"], "resellers")

class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__
        from app.services.updates import is_same_or_newer

        self.assertTrue(is_same_or_newer(__version__, "0.1.0"))
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), __version__)


if __name__ == "__main__":
    unittest.main()
