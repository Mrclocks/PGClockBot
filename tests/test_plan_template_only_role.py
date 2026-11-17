"""Plan forms and messages for a PasarGuard role that only allows template users,
and for a platform role that could not be read."""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

from starlette.requests import Request

from app.services.plans_catalog import (
    CUSTOM_PLAN_LABEL,
    PG_ROLE_UNREAD_MESSAGE,
    PLAN_GROUPS_DENIED_MESSAGE,
    PLAN_TEMPLATE_DENIED_MESSAGE,
    ROLE_REQUIRES_TEMPLATE_MESSAGE,
    group_plans_blocked_by_role,
    plan_groups_denial,
    plan_template_denial,
)

OWNER_PRINCIPAL = {
    "role": "admin",
    "web_owner": True,
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}


def _limited_owner(*, require_template: bool = False, role_read: bool = True, **access: object) -> dict:
    staff = dict(OWNER_PRINCIPAL, pg_is_owner=False)
    if role_read:
        staff["pg_role"] = {"id": 3, "is_owner": False}
        staff["pg_access"] = {
            "require_template": require_template,
            "allowed_template_ids": None,
            "allowed_group_ids": None,
            **access,
        }
    return staff


def _plan(pid: int, name: str, *, groups: str | None = None, template: int | None = None, active: bool = True):
    return NS(id=pid, name=name, pg_group_ids=groups, pg_template_id=template, is_active=active)


class PlanDenialMessageTests(unittest.TestCase):
    def test_unread_platform_role_says_so_instead_of_no_access(self):
        staff = _limited_owner(role_read=False)
        self.assertEqual(plan_template_denial(staff, 10), PG_ROLE_UNREAD_MESSAGE)
        self.assertEqual(plan_groups_denial(staff, [1]), PG_ROLE_UNREAD_MESSAGE)

    def test_read_role_outside_the_allow_list_keeps_the_access_message(self):
        staff = _limited_owner(allowed_template_ids=[10], allowed_group_ids=[1])
        self.assertEqual(plan_template_denial(staff, 20), PLAN_TEMPLATE_DENIED_MESSAGE)
        self.assertEqual(plan_groups_denial(staff, [2]), PLAN_GROUPS_DENIED_MESSAGE)
        self.assertIsNone(plan_template_denial(staff, 10))

    def test_owner_and_unknown_staff_are_never_denied(self):
        owner = dict(OWNER_PRINCIPAL, pg_is_owner=True)
        self.assertIsNone(plan_template_denial(owner, 999))
        self.assertIsNone(plan_template_denial(None, 999))
        self.assertIsNone(plan_groups_denial(None, [1]))


class BlockedGroupPlansTests(unittest.TestCase):
    PLANS = [
        _plan(1, "Groups", groups="1,2"),
        _plan(2, "Template", template=10),
        _plan(3, "Old", groups="3", active=False),
        _plan(4, "Both", groups="1", template=10),
    ]

    def test_active_group_plans_are_named_for_a_template_only_role(self):
        staff = _limited_owner(require_template=True)
        self.assertEqual(group_plans_blocked_by_role(staff, self.PLANS), ["Groups"])

    def test_enabled_custom_plan_with_groups_is_listed(self):
        staff = _limited_owner(require_template=True)
        names = group_plans_blocked_by_role(
            staff, [], custom_plan_enabled=True, custom_plan_group_ids="1,2"
        )
        self.assertEqual(names, [CUSTOM_PLAN_LABEL])
        self.assertEqual(
            group_plans_blocked_by_role(staff, [], custom_plan_enabled=False, custom_plan_group_ids="1"), []
        )

    def test_nothing_is_listed_when_groups_are_allowed(self):
        self.assertEqual(group_plans_blocked_by_role(_limited_owner(), self.PLANS), [])
        owner = dict(OWNER_PRINCIPAL, pg_is_owner=True, pg_access={"require_template": True})
        self.assertEqual(group_plans_blocked_by_role(owner, self.PLANS), [])


# The group / template switch of the PasarGuard build method.
PG_MODE_CHOICE = 'aria-label="روش ساخت در پاسارگارد"'


class PlansPageTemplateOnlyTests(unittest.TestCase):
    def _html(self, *, template_only: bool, blocked: list[str]) -> str:
        from app.api.app import render

        request = Request({
            "type": "http", "asgi": {"version": "3"}, "http_version": "1.1", "method": "GET", "scheme": "https",
            "path": "/plans", "raw_path": b"/plans", "query_string": b"", "headers": [(b"host", b"x.test")],
            "client": ("127.0.0.1", 1), "server": ("x.test", 443), "state": {},
        })
        ctx = {
            "staff": OWNER_PRINCIPAL, "plans": [], "plan_categories": [], "plan_category_map": {},
            "service_addon_packs": [], "category_linked_plans": {}, "values": {}, "reseller_plans": [],
            "templates": [{"id": 10, "name": "Monthly"}], "groups": [{"id": 1, "name": "<b>Main</b>"}],
            "gift_codes": [], "is_platform_admin": True,
            "pg_template_only": template_only, "role_blocked_plans": blocked,
        }
        return render(request, "plans.html", ctx).body.decode("utf-8")

    def test_template_only_role_is_not_offered_group_mode(self):
        html = self._html(template_only=True, blocked=[])
        self.assertNotIn('<option value="custom"', html)
        self.assertNotIn(PG_MODE_CHOICE, html)
        self.assertIn("فقط ساخت سرویس با تمپلیت", html)

    def test_other_roles_keep_both_modes(self):
        html = self._html(template_only=False, blocked=[])
        self.assertIn('<option value="custom"', html)
        self.assertIn(PG_MODE_CHOICE, html)

    def test_blocked_plans_are_named_and_escaped(self):
        html = self._html(template_only=True, blocked=["<i>VIP</i>", "Monthly"])
        self.assertIn("&lt;i&gt;VIP&lt;/i&gt;، Monthly", html)
        self.assertNotIn("<i>VIP</i>", html)


class PlanImportTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_refuses_a_template_outside_the_allow_list(self):
        from app.services.ux20 import _validate_import_plan

        staff = _limited_owner(allowed_template_ids=[10])
        with self.assertRaises(ValueError) as ctx:
            await _validate_import_plan(None, {"pg_template_id": "20"}, staff=staff)
        self.assertEqual(str(ctx.exception), PLAN_TEMPLATE_DENIED_MESSAGE)


class AdminBotPlanFlowTests(unittest.IsolatedAsyncioTestCase):
    """The platform admin's bot plan flow applies the same template and group rules."""

    def setUp(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.db.models import BotUser

        self.db_user = BotUser(id=1, telegram_id=100, role="admin")
        self.session = MagicMock()
        self.session.commit = AsyncMock()
        self.state = MagicMock()
        self.state.get_data = AsyncMock(return_value={"selected_groups": [1, 2], "new_plan": 1})
        self.state.update_data = AsyncMock()
        self.state.set_state = AsyncMock()
        self.callback = MagicMock()
        self.callback.answer = AsyncMock()
        owner_gate = patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True))
        owner_gate.start()
        self.addCleanup(owner_gate.stop)
        self.patch = patch

    def _staff(self, staff: dict):
        from unittest.mock import AsyncMock

        return self.patch("app.bot.handlers.admin._platform_catalog_staff", AsyncMock(return_value=staff))

    async def test_group_plan_is_refused_for_a_template_only_role(self):
        from app.bot.handlers import admin

        self.callback.data = "adm:plan:grpdone:0"
        with self._staff(_limited_owner(require_template=True)):
            await admin.adm_plan_grp_done(self.callback, session=self.session, state=self.state, db_user=self.db_user)
        self.callback.answer.assert_awaited_once_with(ROLE_REQUIRES_TEMPLATE_MESSAGE, show_alert=True)
        self.state.set_state.assert_not_awaited()

    async def test_template_outside_the_allow_list_is_refused(self):
        from app.bot.handlers import admin

        self.callback.data = "adm:plan:settpl:0:20"
        with self._staff(_limited_owner(allowed_template_ids=[10])):
            await admin.adm_plan_set_tpl(self.callback, session=self.session, state=self.state, db_user=self.db_user)
        self.callback.answer.assert_awaited_once_with(PLAN_TEMPLATE_DENIED_MESSAGE, show_alert=True)
        self.state.update_data.assert_not_awaited()

    async def test_allowed_groups_continue_the_flow(self):
        from unittest.mock import AsyncMock

        from app.bot.handlers import admin

        self.callback.data = "adm:plan:grpdone:0"
        with self._staff(_limited_owner()), \
             self.patch("app.bot.handlers.admin._show_user_plan_color_picker", AsyncMock()):
            await admin.adm_plan_grp_done(self.callback, session=self.session, state=self.state, db_user=self.db_user)
        self.state.update_data.assert_awaited()
        self.state.set_state.assert_awaited()

    def test_mode_menu_drops_the_group_button_only_for_a_template_only_role(self):
        from app.bot.handlers.admin import _plan_link_markup

        def callbacks(staff):
            return [row[0].callback_data for row in _plan_link_markup(staff).inline_keyboard]

        self.assertNotIn("adm:plan:new:mode:grp", callbacks(_limited_owner(require_template=True)))
        self.assertIn("adm:plan:new:mode:grp", callbacks(_limited_owner()))
        self.assertIn("adm:plan:new:mode:grp", callbacks(None))


if __name__ == "__main__":
    unittest.main()
