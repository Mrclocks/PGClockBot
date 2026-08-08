"""v4.2.5 — root-cause fixes for bot plan create + group selection."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
ADMIN_PLANS = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")


class PlanDetailKeyboardTests(unittest.TestCase):
    def test_plan_detail_keyboard_append_is_valid(self):
        """Bug: rows.append(row1, row2) raised TypeError after create → generic error."""
        block = ADMIN[
            ADMIN.find("def _plan_detail_keyboard") : ADMIN.find("router = Router")
        ]
        # Must not call append with two list args
        self.assertNotIn(
            'callback_data=f"adm:plan:delask:{p.id}",\n            )\n        ],\n        [InlineKeyboardButton',
            block,
        )
        self.assertIn('callback_data=f"adm:plan:delask:{p.id}"', block)
        self.assertIn("adm:plans:aud:users", block)
        # Two separate appends
        self.assertGreaterEqual(block.count("rows.append("), 2)


class ResellerAddRoutingTests(unittest.TestCase):
    def test_resplan_add_start_exact_modes_only(self):
        """Bug: startswith(adm:resplan:add:) stole toggrp/grpdone → نامعتبر."""
        self.assertIn(
            'F.data.in_({"adm:resplan:add:fixed", "adm:resplan:add:payg"})',
            ADMIN_PLANS,
        )
        self.assertNotIn(
            'F.data.startswith("adm:resplan:add:")',
            ADMIN_PLANS,
        )
        self.assertIn("adm:resplan:add:toggrp:", ADMIN_PLANS)
        self.assertIn("adm:resplan:add:grpdone", ADMIN_PLANS)

    def test_add_wizard_keeps_cancel_keyboard(self):
        block = ADMIN_PLANS[
            ADMIN_PLANS.find("async def open_add_kind_action") : ADMIN_PLANS.find(
                "async def _rerender_plans_screen"
            )
        ]
        # Must not restore list KB mid-wizard (overwrites انصراف)
        self.assertNotIn("await message.answer(\"⬇️\", reply_markup=list_markup)", block.split("Settings-based")[0])
        self.assertIn("kb.cancel_reply()", block)


class UserGroupConfirmTests(unittest.TestCase):
    def test_grpdone_uses_safe_edit_and_defensive_finish(self):
        block = ADMIN[ADMIN.find("async def adm_plan_grp_done") : ADMIN.find("async def adm_plan_clear_link")]
        self.assertIn("safe_edit_text", block)
        self.assertIn("ساخت پلن ناموفق بود", block)
        self.assertIn("await _finish_new_plan", block)

    def test_finish_new_plan_does_not_clear_before_read(self):
        block = ADMIN[ADMIN.find("async def _finish_new_plan") : ADMIN.find("@router.callback_query(F.data == \"adm:plan:new:mode:tpl\")")]
        # Read name before mutating state; no early state.clear()
        name_idx = block.find('name = str(data.get("name")')
        clear_idx = block.find("await state.clear()")
        self.assertGreater(name_idx, 0)
        self.assertEqual(clear_idx, -1)
        self.assertIn("await session.commit()", block)


class ParityGapTests(unittest.TestCase):
    def test_sort_and_naming_edit_fields(self):
        self.assertIn("adm:plan:edit:sort:", ADMIN)
        self.assertIn("adm:plan:edit:prefix:", ADMIN)
        self.assertIn("adm:plan:edit:suffix:", ADMIN)

    def test_reseller_pg_flags(self):
        self.assertIn("adm:resplan:flag:pgadmin:", ADMIN_PLANS)
        self.assertIn("adm:resplan:flag:sharepg:", ADMIN_PLANS)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")


if __name__ == "__main__":
    unittest.main()
