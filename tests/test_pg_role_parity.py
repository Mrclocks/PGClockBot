"""PasarGuard role → web panel and bot access, permission for permission.

The reference rules below transcribe PasarGuard's own enforcement: the server's
``enforce_permission`` (owner bypass, limited admins keep reads only, ``{scope: 0}``
denies) and the dashboard's page guard (a page opens with the resource ``read``;
``read_simple`` only feeds dropdowns). The panel must match them in both directions.
"""

from __future__ import annotations

import itertools
import unittest
from unittest.mock import AsyncMock

from app.bot import keyboards as kb
from app.services.pasarguard import PasarGuardError
from app.services.pg_access import (
    acl_from_admin_payload,
    map_pg_role_actions,
    map_pg_role_to_features,
    role_as_enforced,
    role_user_actions,
)

PAGE_RESOURCES = {
    "pg_users": "users",
    "pg_templates": "templates",
    "pg_groups": "groups",
    "pg_hosts": "hosts",
    "pg_nodes": "nodes",
}
GRANTS = (None, False, True, {"scope": 0}, {"scope": 1}, {"scope": 2})
READ_ACTIONS = {"read", "read_simple", "read_general", "logs", "stats"}


def pg_allows(admin: dict, resource: str, action: str) -> bool:
    """PasarGuard's ``enforce_permission`` for a signed-in admin."""
    role = admin.get("role") or {}
    if role.get("is_owner"):
        return True
    if admin.get("status") == "limited":
        if role.get("disabled_when_limited") or action not in READ_ACTIONS:
            return False
    value = ((role.get("permissions") or {}).get(resource) or {}).get(action)
    if value is True:
        return True
    return isinstance(value, dict) and value.get("scope") not in (None, 0)


def pg_dashboard_pages(admin: dict) -> set[str]:
    """Pages PasarGuard's dashboard opens for this admin (``canAccessRoute``)."""
    pages = {page for page, resource in PAGE_RESOURCES.items() if pg_allows(admin, resource, "read")}
    if pg_allows(admin, "system", "read"):
        pages |= {"pg_overview", "pg_inbounds"}
    return pages


def panel_pages(admin: dict) -> set[str]:
    features, _role, _owner = acl_from_admin_payload(admin)
    return set(features)


def _admin(permissions: dict, *, status: str = "active", disabled_when_limited: bool = False) -> dict:
    role = {"id": 4, "name": "r", "is_owner": False, "permissions": permissions,
            "disabled_when_limited": disabled_when_limited}
    return {"username": "a", "status": status, "role": role}


class PageParityTests(unittest.TestCase):
    def test_every_single_grant_opens_exactly_the_pasarguard_pages(self):
        resources = ("users", "templates", "groups", "hosts", "nodes", "system", "admins")
        actions = ("read", "read_simple", "create", "update", "delete", "stats", "reconnect")
        for resource, action, grant in itertools.product(resources, actions, GRANTS):
            admin = _admin({resource: {action: grant}})
            expected = pg_dashboard_pages(admin)
            if pg_allows(admin, "users", "create"):
                # The users page is where users are created; PasarGuard accepts users.create alone.
                expected.add("pg_users")
            if expected - {"pg_overview"}:
                # The admin's own overview (GET /api/admin) needs no permission.
                expected.add("pg_overview")
            with self.subTest(resource=resource, action=action, grant=grant):
                self.assertEqual(panel_pages(admin), expected)

    def test_read_simple_never_opens_a_page(self):
        perms = {res: {"read_simple": True} for res in ("users", "templates", "groups", "nodes")}
        self.assertEqual(panel_pages(_admin(perms)), set())

    def test_inbounds_follow_system_read_not_groups_or_hosts(self):
        self.assertNotIn("pg_inbounds", panel_pages(_admin({"groups": {"read": True}, "hosts": {"read": True}})))
        self.assertIn("pg_inbounds", panel_pages(_admin({"system": {"read": True}})))

    def test_owner_role_opens_everything(self):
        admin = {"username": "o", "role": {"id": 1, "is_owner": True, "permissions": {}}}
        self.assertIn("pg_admins", panel_pages(admin))
        self.assertTrue({"pg_users", "pg_nodes", "pg_hosts", "pg_inbounds"} <= panel_pages(admin))


class ActionParityTests(unittest.TestCase):
    def test_host_delete_follows_hosts_update(self):
        for grant in GRANTS:
            role = {"is_owner": False, "permissions": {"hosts": {"read": True, "update": grant}}}
            with self.subTest(grant=grant):
                self.assertEqual(
                    map_pg_role_actions(role)["hosts"]["delete"],
                    pg_allows({"role": role}, "hosts", "update"),
                )

    def test_every_mapped_action_matches_pasarguard(self):
        pairs = {
            ("users", "create"), ("users", "update"), ("users", "delete"),
            ("templates", "create"), ("templates", "update"), ("templates", "delete"),
            ("groups", "create"), ("groups", "update"), ("groups", "delete"),
            ("hosts", "create"), ("hosts", "update"),
            ("nodes", "create"), ("nodes", "update"), ("nodes", "delete"), ("nodes", "reconnect"),
        }
        for (resource, action), grant in itertools.product(sorted(pairs), GRANTS):
            role = {"is_owner": False, "permissions": {resource: {action: grant}}}
            with self.subTest(resource=resource, action=action, grant=grant):
                self.assertEqual(map_pg_role_actions(role)[resource][action], pg_allows({"role": role}, resource, action))

    def test_user_actions_match_pasarguard(self):
        mapping = {"create": "create", "read": "read", "update": "update", "delete": "delete",
                   "reset_usage": "reset_usage", "revoke_sub": "revoke_sub", "disable": "update", "enable": "update"}
        for (ours, theirs), grant in itertools.product(mapping.items(), GRANTS):
            role = {"is_owner": False, "permissions": {"users": {theirs: grant}}}
            with self.subTest(action=ours, grant=grant):
                self.assertEqual(role_user_actions(role)[ours], pg_allows({"role": role}, "users", theirs))

    def test_user_read_simple_is_not_user_read(self):
        role = {"is_owner": False, "permissions": {"users": {"read_simple": True}}}
        self.assertFalse(role_user_actions(role)["read"])


class LimitedAdminTests(unittest.TestCase):
    FULL = {res: {"read": True, "create": True, "update": True, "delete": True, "reconnect": True}
            for res in ("users", "templates", "groups", "hosts", "nodes")}

    def test_limited_admin_keeps_reads_and_loses_every_write(self):
        admin = _admin(self.FULL, status="limited")
        _features, role, _owner = acl_from_admin_payload(admin)
        self.assertEqual(panel_pages(admin), pg_dashboard_pages(admin) | {"pg_overview"})
        actions = map_pg_role_actions(role)
        self.assertFalse(any(flag for block in actions.values() for flag in block.values()))
        self.assertTrue(role_user_actions(role)["read"])
        self.assertFalse(role_user_actions(role)["create"])

    def test_limited_admin_disabled_by_role_loses_everything(self):
        admin = _admin(self.FULL, status="limited", disabled_when_limited=True)
        self.assertEqual(panel_pages(admin), set())

    def test_active_admin_and_owner_are_untouched(self):
        role = {"is_owner": False, "permissions": self.FULL}
        self.assertIs(role_as_enforced({"status": "active"}, role), role)
        owner = {"is_owner": True, "permissions": {}}
        self.assertIs(role_as_enforced({"status": "limited"}, owner), owner)


class BotUsersMenuTests(unittest.TestCase):
    def test_list_and_search_need_users_read(self):
        feats = frozenset({"pg_users"})
        markup = kb.pg_reply_keyboard(None, features=feats, can_create_user=True, can_list_users=False)
        labels = {button.text for row in markup.keyboard for button in row}
        self.assertIn("➕ ساخت کاربر", labels)
        self.assertNotIn("👥 کاربران", labels)
        self.assertNotIn("🔎 جستجوی یوزر", labels)

    def test_bot_user_list_gate_follows_users_read(self):
        from app.services.bot_pg_user_authz import _capability_decision

        def staff(users: dict) -> dict:
            role = {"is_owner": False, "permissions": {"users": users}}
            return {
                "role": "admin", "web_owner": True, "org_principal_id": 1, "org_depth": 0,
                "org_parent_id": None, "org_status": "active", "org_visible_principal_ids": [1],
                "pg_capabilities_ok": True,
                "pg_permissions": map_pg_role_to_features(role), "pg_user_actions": role_user_actions(role),
            }

        self.assertFalse(_capability_decision(staff({"create": True}), "list").allowed)
        self.assertTrue(_capability_decision(staff({"read": True}), "list").allowed)


class UsersFormGroupsTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_without_group_list_gets_no_groups_not_an_error(self):
        from app.api.pg_pages import _groups_for_user_form

        pg = AsyncMock()
        pg.get_groups_simple = AsyncMock(side_effect=PasarGuardError("denied", 403))
        self.assertEqual(await _groups_for_user_form(pg, {"role": "admin", "pg_is_owner": True}), [])

    async def test_a_failed_group_read_is_still_a_failure(self):
        from app.api.pg_pages import _groups_for_user_form

        pg = AsyncMock()
        pg.get_groups_simple = AsyncMock(side_effect=PasarGuardError("down", 502))
        self.assertIsNone(await _groups_for_user_form(pg, {"role": "admin", "pg_is_owner": True}))


if __name__ == "__main__":
    unittest.main()
