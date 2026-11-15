"""Regression test: a Hybrid Owner's own PasarGuard group/template
restriction must not be silently ignored in the web panel.

``filter_templates_for_staff``, ``filter_groups_for_staff``,
``template_allowed_for_staff`` and ``groups_allowed_for_staff`` used to
short-circuit unconditionally for ``role == "admin"`` regardless of whether
the platform's own ``.env`` PasarGuard account is the real owner or a
*limited* admin (Hybrid Owner setup, ``pg_is_owner=False``). That let a
restricted admin account (e.g. one PasarGuard only grants access to groups 1
and 2) see/select every group and template in the web panel, exactly
contradicting the operator's own PasarGuard configuration. Genuine owners
(``pg_is_owner=True`` or unset, e.g. non-Hybrid single-owner deployments)
must keep their unrestricted behavior unchanged.
"""

from __future__ import annotations

import unittest

from app.services.plans_catalog import (
    PLAN_GROUPS_DENIED_MESSAGE,
    ROLE_REQUIRES_TEMPLATE_MESSAGE,
    filter_groups_for_staff,
    filter_templates_for_staff,
    groups_allowed_for_staff,
    plan_groups_denial,
    template_allowed_for_staff,
)

GROUPS = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
TEMPLATES = [{"id": 10}, {"id": 20}, {"id": 30}]


class HybridOwnerRestrictedAdminTests(unittest.TestCase):
    def _restricted_admin(self, allowed_group_ids=(1, 2), allowed_template_ids=(10,)):
        return {
            "role": "admin",
            "pg_is_owner": False,
            "pg_access": {
                "allowed_group_ids": list(allowed_group_ids),
                "allowed_template_ids": list(allowed_template_ids),
            },
        }

    def test_restricted_admin_groups_are_filtered(self):
        staff = self._restricted_admin()
        out = filter_groups_for_staff(GROUPS, staff)
        self.assertEqual({g["id"] for g in out}, {1, 2})

    def test_restricted_admin_templates_are_filtered(self):
        staff = self._restricted_admin()
        out = filter_templates_for_staff(TEMPLATES, staff)
        self.assertEqual({t["id"] for t in out}, {10})

    def test_restricted_admin_cannot_use_disallowed_group(self):
        staff = self._restricted_admin()
        self.assertFalse(groups_allowed_for_staff(staff, [3]))
        self.assertFalse(groups_allowed_for_staff(staff, [1, 3]))

    def test_restricted_admin_can_use_allowed_group(self):
        staff = self._restricted_admin()
        self.assertTrue(groups_allowed_for_staff(staff, [1, 2]))

    def test_restricted_admin_cannot_use_disallowed_template(self):
        staff = self._restricted_admin()
        self.assertFalse(template_allowed_for_staff(staff, 20))

    def test_restricted_admin_can_use_allowed_template(self):
        staff = self._restricted_admin()
        self.assertTrue(template_allowed_for_staff(staff, 10))


class GenuineOwnerUnaffectedTests(unittest.TestCase):
    """Existing single-owner (non-Hybrid) behavior must be unchanged."""

    def test_owner_sees_all_groups(self):
        staff = {"role": "admin", "pg_is_owner": True}
        self.assertEqual(filter_groups_for_staff(GROUPS, staff), GROUPS)

    def test_owner_sees_all_templates(self):
        staff = {"role": "admin", "pg_is_owner": True}
        self.assertEqual(filter_templates_for_staff(TEMPLATES, staff), TEMPLATES)

    def test_owner_can_use_any_group(self):
        staff = {"role": "admin", "pg_is_owner": True}
        self.assertTrue(groups_allowed_for_staff(staff, [1, 2, 3, 4]))

    def test_owner_can_use_any_template(self):
        staff = {"role": "admin", "pg_is_owner": True}
        self.assertTrue(template_allowed_for_staff(staff, 999))

    def test_missing_pg_is_owner_key_defaults_to_unrestricted(self):
        """Legacy / non-Hybrid staff dicts (no pg_is_owner key at all) keep
        prior unrestricted behavior — this fix must not break existing
        single-owner deployments."""
        staff = {"role": "admin"}
        self.assertEqual(filter_groups_for_staff(GROUPS, staff), GROUPS)
        self.assertTrue(groups_allowed_for_staff(staff, [1, 2, 3, 4]))


class NonAdminRolesUnaffectedTests(unittest.TestCase):
    def test_reseller_still_uses_own_pg_access(self):
        staff = {
            "role": "reseller",
            "pg_access": {"allowed_group_ids": [2]},
        }
        out = filter_groups_for_staff(GROUPS, staff, trust_client_scope=True)
        self.assertEqual({g["id"] for g in out}, {2})


class PlatformOwnerWithoutAllowListTests(unittest.TestCase):
    """A limited PasarGuard role with no template/group allow-list.

    PasarGuard stores ``None`` for "no restriction" (its default for new
    roles). The list already showed every template and group, so saving a
    plan with one of them must be accepted too.
    """

    def _platform_owner(self, *, role_read: bool = True) -> dict:
        staff = {
            "role": "admin",
            "web_owner": True,
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
            "pg_is_owner": False,
            "pg_access": {
                "require_template": False,
                "allowed_template_ids": None,
                "allowed_group_ids": None,
            },
        }
        if role_read:
            staff["pg_role"] = {"id": 3, "name": "Seller", "is_owner": False}
        return staff

    def test_saving_a_listed_template_is_allowed(self):
        staff = self._platform_owner()
        listed = filter_templates_for_staff(TEMPLATES, staff)
        self.assertEqual(listed, TEMPLATES)
        for template in listed:
            self.assertTrue(template_allowed_for_staff(staff, template["id"]))

    def test_saving_listed_groups_is_allowed(self):
        staff = self._platform_owner()
        listed = filter_groups_for_staff(GROUPS, staff)
        self.assertEqual(listed, GROUPS)
        self.assertTrue(groups_allowed_for_staff(staff, [g["id"] for g in listed]))

    def test_unread_role_is_still_denied(self):
        staff = self._platform_owner(role_read=False)
        self.assertFalse(template_allowed_for_staff(staff, 10))
        self.assertFalse(groups_allowed_for_staff(staff, [1]))

    def test_explicit_allow_list_is_still_enforced(self):
        staff = self._platform_owner()
        staff["pg_access"] = {"allowed_template_ids": [10], "allowed_group_ids": [1]}
        self.assertFalse(template_allowed_for_staff(staff, 20))
        self.assertFalse(groups_allowed_for_staff(staff, [1, 3]))
        self.assertTrue(groups_allowed_for_staff(staff, [1]))

    def test_empty_group_selection_is_still_rejected(self):
        self.assertFalse(groups_allowed_for_staff(self._platform_owner(), []))

    def test_template_and_groups_from_a_live_limited_role(self):
        from app.services.pg_access import acl_from_admin_payload, enrich_staff_pg_from_role

        admin = {
            "username": "seller",
            "role": {
                "id": 3,
                "name": "Seller",
                "is_owner": False,
                "permissions": {
                    "users": {"create": True},
                    "templates": {"read": True},
                    "groups": {"read_simple": True},
                },
                "access": {
                    "require_template": False,
                    "allowed_template_ids": None,
                    "allowed_group_ids": None,
                },
            },
        }
        features, role, is_owner = acl_from_admin_payload(admin)
        base = self._platform_owner(role_read=False)
        del base["pg_access"]
        staff = enrich_staff_pg_from_role(base, features, role)
        staff["pg_is_owner"] = is_owner
        self.assertTrue(template_allowed_for_staff(staff, 10))
        self.assertTrue(groups_allowed_for_staff(staff, [1, 2]))


class RoleRequiresTemplateTests(unittest.IsolatedAsyncioTestCase):
    """PasarGuard ``require_template`` refuses users created from groups alone.

    A group plan saved for such a role would fail at delivery, after the
    customer paid, so it must be refused when the plan is saved.
    """

    def _limited(self, *, require_template: bool, role: str = "admin") -> dict:
        return {
            "role": role,
            "web_owner": role == "admin",
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
            "pg_is_owner": False,
            "pg_role": {"id": 3, "is_owner": False},
            "pg_access": {
                "require_template": require_template,
                "allowed_template_ids": None,
                "allowed_group_ids": None,
            },
        }

    def test_group_plan_is_refused_when_the_role_requires_a_template(self):
        staff = self._limited(require_template=True)
        self.assertEqual(plan_groups_denial(staff, [1]), ROLE_REQUIRES_TEMPLATE_MESSAGE)
        self.assertTrue(template_allowed_for_staff(staff, 10))

    def test_group_plan_is_allowed_without_require_template(self):
        self.assertIsNone(plan_groups_denial(self._limited(require_template=False), [1, 2]))

    def test_owner_is_never_held_to_require_template(self):
        staff = {"role": "admin", "pg_is_owner": True, "pg_access": {"require_template": True}}
        self.assertIsNone(plan_groups_denial(staff, [1]))

    def test_reseller_role_requiring_a_template_is_refused_too(self):
        staff = {
            "role": "reseller",
            "reseller_id": 5,
            "pg_access": {"require_template": True, "allowed_group_ids": [1]},
        }
        self.assertEqual(plan_groups_denial(staff, [1]), ROLE_REQUIRES_TEMPLATE_MESSAGE)

    def test_disallowed_group_keeps_the_access_message(self):
        staff = self._limited(require_template=False)
        staff["pg_access"]["allowed_group_ids"] = [1]
        self.assertEqual(plan_groups_denial(staff, [2]), PLAN_GROUPS_DENIED_MESSAGE)

    async def test_plan_import_refuses_groups_for_a_template_only_role(self):
        from app.services.ux20 import _validate_import_plan

        staff = self._limited(require_template=True)
        with self.assertRaises(ValueError) as ctx:
            await _validate_import_plan(None, {"pg_group_ids": "1,2"}, staff=staff)
        self.assertEqual(str(ctx.exception), ROLE_REQUIRES_TEMPLATE_MESSAGE)

    async def test_plan_import_refuses_a_group_outside_the_allow_list(self):
        from app.services.ux20 import _validate_import_plan

        staff = self._limited(require_template=False)
        staff["pg_access"]["allowed_group_ids"] = [1]
        with self.assertRaises(ValueError) as ctx:
            await _validate_import_plan(None, {"pg_group_ids": "1,2"}, staff=staff)
        self.assertEqual(str(ctx.exception), PLAN_GROUPS_DENIED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
