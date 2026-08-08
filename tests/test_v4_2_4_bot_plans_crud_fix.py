"""v4.2.5 — bot plans CRUD: commit, handler routing, PAYG group picker."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PLANS = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")
ADMIN = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
TG_UTILS = (ROOT / "app/bot/tg_utils.py").read_text(encoding="utf-8")


class PlansCrudFixTests(unittest.TestCase):
    def test_reseller_plan_save_commits(self):
        self.assertIn("async def _persist", ADMIN_PLANS)
        save_block = ADMIN_PLANS[ADMIN_PLANS.find("async def _save_reseller_plan") :]
        self.assertIn("await _persist(session)", save_block)

    def test_reseller_delete_commits_and_cleans_billing(self):
        self.assertIn("delete_plan_billing_rate", ADMIN_PLANS)
        idx = ADMIN_PLANS.find("async def resplan_del(")
        self.assertGreater(idx, 0)
        block = ADMIN_PLANS[idx : idx + 900]
        self.assertIn("delete_plan_billing_rate", block)
        self.assertIn("await _persist(session)", block)

    def test_reseller_edit_routing_excludes_toggrp(self):
        self.assertIn(
            'F.data.regexp(r"^adm:resplan:edit:(name|price|comm|rate|desc|grp|role):\\d+$")',
            ADMIN_PLANS,
        )
        self.assertIn("adm:resplan:edit:toggrp:", ADMIN_PLANS)
        self.assertIn("adm:resplan:edit:grpdone:", ADMIN_PLANS)

    def test_payg_create_has_group_picker(self):
        self.assertIn("res_plan_link", ADMIN_PLANS)
        self.assertIn("adm:resplan:add:toggrp:", ADMIN_PLANS)
        self.assertIn("adm:resplan:add:grpdone", ADMIN_PLANS)
        # Skip removed in 4.2.6 — groups are required
        self.assertNotIn("adm:resplan:add:grpskip", ADMIN_PLANS)
        self.assertIn("adm:resplan:add:setrole:", ADMIN_PLANS)

    def test_user_plan_detail_escapes_html(self):
        detail = ADMIN[ADMIN.find("async def _plan_detail_text") : ADMIN.find("def _plan_detail_keyboard")]
        self.assertIn("html.escape(p.name)", detail)

    def test_persian_digit_parser(self):
        self.assertIn("def parse_bot_int", TG_UTILS)
        self.assertIn("from app.services.numbers import", TG_UTILS)
        from app.bot.tg_utils import parse_bot_float, parse_bot_int

        self.assertEqual(parse_bot_int("۱۲۰۰۰"), 12000)
        self.assertEqual(parse_bot_int("۳۰"), 30)
        self.assertEqual(parse_bot_int("١٢٣"), 123)
        self.assertAlmostEqual(parse_bot_float("۱٫۵"), 1.5)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.8.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.8.0")


if __name__ == "__main__":
    unittest.main()
