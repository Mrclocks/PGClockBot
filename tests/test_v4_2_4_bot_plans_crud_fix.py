"""v4.2.5 — bot plans CRUD: commit, handler routing, PAYG group picker."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PLANS = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")
ADMIN = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
PLAN_EDITOR = (ROOT / "app/bot/handlers/admin_plan_editor.py").read_text(encoding="utf-8")
TG_UTILS = (ROOT / "app/bot/tg_utils.py").read_text(encoding="utf-8")


class PlansCrudFixTests(unittest.TestCase):
    def test_reseller_plan_save_commits(self):
        self.assertIn("async def _persist", ADMIN_PLANS)
        save_block = ADMIN_PLANS[ADMIN_PLANS.find("async def _save_reseller_plan") :]
        self.assertIn("await _persist(session)", save_block)

    def test_reseller_edit_routing_excludes_toggrp(self):
        self.assertIn(
            'F.data.regexp(\n        r"^adm:resplan:edit:(name|price|comm|rate|desc|grp|role|addon_gb|addon_users|extra_gb|extra_user):\\d+$"',
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
        detail = PLAN_EDITOR[PLAN_EDITOR.find("async def _plan_detail_text") : PLAN_EDITOR.find("def _plan_detail_keyboard")]
        self.assertIn("html.escape(p.name)", detail)

    def test_persian_digit_parser(self):
        self.assertIn("def parse_bot_int", TG_UTILS)
        self.assertIn("from app.services.numbers import", TG_UTILS)
        from app.bot.tg_utils import parse_bot_float, parse_bot_int

        self.assertEqual(parse_bot_int("۱۲۰۰۰"), 12000)
        self.assertEqual(parse_bot_int("۳۰"), 30)
        self.assertEqual(parse_bot_int("١٢٣"), 123)
        self.assertAlmostEqual(parse_bot_float("۱٫۵"), 1.5)


class ResellerPlanDeleteHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Billing-rate cleanup lives in delete_reseller_plan (covered in
    test_v4_0_7_payg_multi_plan_rates); the handler must commit afterwards."""

    async def _delete(self, *, delete_side_effect=None):
        from app.bot.handlers import admin_plans

        calls: list[str] = []
        session = AsyncMock()
        plan = MagicMock(id=4, is_system=False)
        session.get = AsyncMock(return_value=plan)
        session.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        session.rollback = AsyncMock(side_effect=lambda: calls.append("rollback"))

        async def _fake_delete(_session, _plan):
            calls.append("delete")
            if delete_side_effect is not None:
                raise delete_side_effect

        callback = AsyncMock()
        callback.data = "adm:resplan:del:4"
        callback.message = MagicMock()
        with (
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            patch.object(admin_plans, "_is_admin", MagicMock(return_value=True)),
            patch.object(admin_plans, "reseller_plan_mode_of", MagicMock(return_value="payg")),
            patch.object(admin_plans, "_rerender_plans_screen", AsyncMock()),
            patch("app.services.plans_catalog.delete_reseller_plan", _fake_delete),
        ):
            await admin_plans.resplan_del(
                callback, session=session, db_user=MagicMock(), state=AsyncMock()
            )
        return calls, callback

    async def test_reseller_delete_commits_after_service_delete(self):
        calls, callback = await self._delete()

        self.assertEqual(calls, ["delete", "commit"])
        callback.answer.assert_awaited_with("حذف شد")

    async def test_reseller_delete_blocked_rolls_back_without_commit(self):
        from app.services.plans_catalog import PlanDeleteBlocked

        calls, callback = await self._delete(
            delete_side_effect=PlanDeleteBlocked("هنوز استفاده می‌شود")
        )

        self.assertEqual(calls, ["delete", "rollback"])
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])


if __name__ == "__main__":
    unittest.main()
