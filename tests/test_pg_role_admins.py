"""Admins page run with the staff member's own PasarGuard account: mapping, menu and service rules."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.app import templates
from app.db import Base
from app.db.models import PgStaffAccess, ResellerProfile
from app.services.panel_nav import NavViewer, build_sidebar
from app.services.pasarguard import PasarGuardError
from app.services.pg_access import enrich_staff_pg_from_role, map_pg_role_to_features
from app.services.pg_role_admins import (
    DENIED_MSG,
    RoleAdminRow,
    RoleAdminsPage,
    MANAGED_MSG,
    OWNER_ROLE_MSG,
    SELF_MSG,
    RoleAdminError,
    admin_actions_for_staff,
    change_role_admin,
    create_role_admin,
    load_role_admins_page,
)
from app.services.principal_pg_authz import apply_level1_pg_local_safety, staff_may_pg_page
from app.services.principal_web_identity import ROLE_PRINCIPAL

FULL_ADMINS = {"create": True, "read": True, "update": True, "delete": True, "reset_usage": True}
STRONG_PASSWORD = "AaBb12!@CdEf"


def _role(admins: dict | None, *, is_owner: bool = False, extra: dict | None = None) -> dict:
    permissions: dict[str, Any] = {"users": {"read": True}, **(extra or {})}
    if admins is not None:
        permissions["admins"] = admins
    return {"id": 7, "name": "Reseller role", "is_owner": is_owner, "permissions": permissions}


def _reseller_staff(role: dict, *, pg_username: str = "deev") -> dict:
    staff = {
        "role": "reseller",
        "pg_admin_username": pg_username,
        "org_principal_id": 5,
        "org_depth": 1,
        "org_parent_id": 1,
        "org_status": "active",
        "org_visible_principal_ids": [5],
        "web_owner": False,
        "pg_is_owner": False,
    }
    return enrich_staff_pg_from_role(staff, map_pg_role_to_features(role), role)


class FakePanel:
    """PasarGuard client stand-in that records every change."""

    def __init__(self, admins: list[dict], roles: list[dict] | None = None) -> None:
        self.admins = admins
        self.roles = roles or []
        self.calls: list[tuple[str, Any]] = []

    async def get_admins(self) -> list[dict]:
        return list(self.admins)

    async def get_admins_simple(self) -> list[dict]:
        return [{"username": a["username"]} for a in self.admins]

    async def get_admin_roles(self) -> list[dict]:
        return list(self.roles)

    async def create_admin(self, payload: dict) -> dict:
        self.calls.append(("create", payload))
        return payload

    async def modify_admin(self, username: str, payload: dict) -> dict:
        self.calls.append(("modify", (username, payload)))
        return {}

    async def reset_admin_usage(self, username: str) -> dict:
        self.calls.append(("reset", username))
        return {}

    async def delete_admin(self, username: str) -> None:
        self.calls.append(("delete", username))


class FeatureMappingTests(unittest.TestCase):
    def test_role_that_reads_admins_gets_the_admins_page(self) -> None:
        self.assertIn("pg_role_admins", map_pg_role_to_features(_role({"read": True})))

    def test_scoped_read_simple_counts_as_reading_admins(self) -> None:
        self.assertIn("pg_role_admins", map_pg_role_to_features(_role({"read_simple": {"scope": 1}})))

    def test_role_without_admins_has_no_admins_page(self) -> None:
        self.assertNotIn("pg_role_admins", map_pg_role_to_features(_role(None)))
        self.assertNotIn("pg_role_admins", map_pg_role_to_features(_role({"read": {"scope": 0}})))

    def test_owner_role_gets_the_admins_page(self) -> None:
        self.assertIn("pg_role_admins", map_pg_role_to_features(_role(None, is_owner=True)))

    def test_actions_follow_each_pasarguard_action(self) -> None:
        staff = _reseller_staff(_role({"read": True, "update": True, "delete": {"scope": 0}}))
        actions = admin_actions_for_staff(staff)
        self.assertTrue(actions["list"])
        self.assertTrue(actions["update"])
        self.assertFalse(actions["delete"])
        self.assertFalse(actions["create"])
        self.assertFalse(actions["reset_usage"])

    def test_owner_role_allows_every_admin_action(self) -> None:
        staff = _reseller_staff(_role(None, is_owner=True))
        self.assertTrue(all(admin_actions_for_staff(staff).values()))


class AccessGateTests(unittest.TestCase):
    def _principal(self, role: dict) -> dict:
        staff = {
            "role": ROLE_PRINCIPAL,
            "pg_admin_username": "rep1",
            "pg_credentials_ready": True,
            "org_principal_id": 10,
            "org_depth": 1,
            "org_parent_id": 1,
            "org_status": "active",
            "org_visible_principal_ids": [10],
            "web_owner": False,
            "pg_is_owner": False,
        }
        staff = enrich_staff_pg_from_role(staff, map_pg_role_to_features(role), role)
        return apply_level1_pg_local_safety(staff, role)

    def test_representative_with_admins_role_may_open_own_admins_page(self) -> None:
        staff = self._principal(_role(FULL_ADMINS))
        self.assertTrue(staff_may_pg_page(staff, "pg_role_admins"))
        # The owner page (platform account, web-access grants) stays closed.
        self.assertFalse(staff_may_pg_page(staff, "pg_admins"))

    def test_representative_without_admins_role_may_not_open_it(self) -> None:
        staff = self._principal(_role(None))
        self.assertFalse(staff_may_pg_page(staff, "pg_role_admins"))


class SidebarTests(unittest.TestCase):
    @staticmethod
    def _hrefs(role: str, pg: set[str]) -> list[str]:
        viewer = NavViewer(role=role, perms=frozenset(), pg_perms=frozenset(pg), is_owner=role == "admin",
                           can_add_representative=False, can_manage_representatives=False)
        return [i.href for s in build_sidebar(viewer, path="/pg") for i in s.items]

    def test_reseller_with_admins_role_sees_admins_entry(self) -> None:
        hrefs = self._hrefs("reseller", {"pg_overview", "pg_role_admins"})
        self.assertIn("/pg/role-admins", hrefs)

    def test_reseller_with_owner_role_never_gets_the_owner_page_link(self) -> None:
        hrefs = self._hrefs("reseller", {"pg_overview", "pg_admins", "pg_role_admins"})
        self.assertNotIn("/pg/admins", hrefs)
        self.assertIn("/pg/role-admins", hrefs)

    def test_platform_owner_sees_a_single_admins_entry(self) -> None:
        hrefs = self._hrefs("admin", {"pg_overview", "pg_admins", "pg_role_admins"})
        self.assertIn("/pg/admins", hrefs)
        self.assertNotIn("/pg/role-admins", hrefs)


class RoleAdminServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(ResellerProfile(user_id=99, pg_admin_username="OtherShop"))
            session.add(PgStaffAccess(pg_username="helper", web_username="helper", web_password_hash="x"))
            await session.commit()
        self.panel = FakePanel(
            [
                {"username": "deev", "role": {"name": "Reseller"}, "total_users": 3},
                {"username": "otherShop", "status": "active"},
                {"username": "helper"},
                {"username": "root"},
                {"username": "plain", "status": "disabled", "used_traffic": 1024, "data_limit": 4096},
            ],
            roles=[{"id": 1, "name": "owner", "is_owner": True}, {"id": 4, "name": "operator"}],
        )
        self.settings_patch = patch("app.services.pg_role_admins.get_settings")
        self.settings_patch.start().return_value.pg_username = "root"

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_page_marks_own_and_panel_managed_admins(self) -> None:
        staff = _reseller_staff(_role(FULL_ADMINS))
        async with self.Session() as session:
            page = await load_role_admins_page(session, self.panel, staff)
        rows = {r.username: r for r in page.rows}
        self.assertTrue(rows["deev"].is_self)
        self.assertTrue(rows["otherShop"].is_managed)
        self.assertTrue(rows["helper"].is_managed)
        self.assertTrue(rows["root"].is_managed)
        self.assertFalse(rows["plain"].is_managed)
        self.assertTrue(rows["plain"].is_disabled)
        self.assertEqual(rows["plain"].data_limit, 4096)
        self.assertEqual([r["id"] for r in page.roles], [4])

    async def test_page_needs_admins_read(self) -> None:
        staff = _reseller_staff(_role(None))
        async with self.Session() as session:
            with self.assertRaises(RoleAdminError) as ctx:
                await load_role_admins_page(session, self.panel, staff)
        self.assertEqual(str(ctx.exception), DENIED_MSG)

    async def test_page_falls_back_to_simple_list_when_full_list_is_refused(self) -> None:
        staff = _reseller_staff(_role({"read": True, "read_simple": True}))

        async def refuse() -> list[dict]:
            raise PasarGuardError("forbidden", 403)

        self.panel.get_admins = refuse  # type: ignore[method-assign]  # simulate a 403 on the full list
        async with self.Session() as session:
            page = await load_role_admins_page(session, self.panel, staff)
        self.assertEqual(len(page.rows), 5)

    async def test_disable_plain_admin_calls_panel_with_status(self) -> None:
        staff = _reseller_staff(_role(FULL_ADMINS))
        async with self.Session() as session:
            await change_role_admin(session, self.panel, staff, username="plain", change="disable")
        self.assertEqual(self.panel.calls, [("modify", ("plain", {"status": "disabled"}))])

    async def test_cannot_change_another_shops_admin(self) -> None:
        staff = _reseller_staff(_role(FULL_ADMINS))
        for target in ("OTHERSHOP", "helper", "root"):
            async with self.Session() as session:
                with self.assertRaises(RoleAdminError) as ctx:
                    await change_role_admin(session, self.panel, staff, username=target, change="delete")
            self.assertEqual(str(ctx.exception), MANAGED_MSG)
        self.assertEqual(self.panel.calls, [])

    async def test_cannot_disable_or_delete_own_account(self) -> None:
        staff = _reseller_staff(_role(FULL_ADMINS))
        async with self.Session() as session:
            with self.assertRaises(RoleAdminError) as ctx:
                await change_role_admin(session, self.panel, staff, username="Deev", change="disable")
        self.assertEqual(str(ctx.exception), SELF_MSG)
        self.assertEqual(self.panel.calls, [])

    async def test_change_without_matching_permission_is_refused(self) -> None:
        staff = _reseller_staff(_role({"read": True, "update": True}))
        async with self.Session() as session:
            with self.assertRaises(RoleAdminError):
                await change_role_admin(session, self.panel, staff, username="plain", change="delete")
            with self.assertRaises(RoleAdminError):
                await change_role_admin(session, self.panel, staff, username="plain", change="reset_usage")
        self.assertEqual(self.panel.calls, [])

    async def test_unknown_change_is_refused(self) -> None:
        staff = _reseller_staff(_role(FULL_ADMINS))
        async with self.Session() as session:
            with self.assertRaises(RoleAdminError):
                await change_role_admin(session, self.panel, staff, username="plain", change="sudo")  # type: ignore[arg-type]  # hostile input
        self.assertEqual(self.panel.calls, [])

    async def test_create_uses_chosen_non_owner_role(self) -> None:
        staff = _reseller_staff(_role(FULL_ADMINS))
        created = await create_role_admin(
            self.panel, staff, username="newadmin", password=STRONG_PASSWORD, role_id=4
        )
        self.assertEqual(created, "newadmin")
        kind, payload = self.panel.calls[0]
        self.assertEqual(kind, "create")
        self.assertEqual(payload["role_id"], 4)

    async def test_create_refuses_owner_role_and_unknown_role(self) -> None:
        staff = _reseller_staff(_role(FULL_ADMINS))
        for role_id in (1, 99):
            with self.assertRaises(RoleAdminError) as ctx:
                await create_role_admin(
                    self.panel, staff, username="newadmin", password=STRONG_PASSWORD, role_id=role_id
                )
            self.assertEqual(str(ctx.exception), OWNER_ROLE_MSG)
        self.assertEqual(self.panel.calls, [])

    async def test_create_needs_admins_create(self) -> None:
        staff = _reseller_staff(_role({"read": True}))
        with self.assertRaises(RoleAdminError) as ctx:
            await create_role_admin(self.panel, staff, username="newadmin", password=STRONG_PASSWORD, role_id=4)
        self.assertEqual(str(ctx.exception), DENIED_MSG)
        self.assertEqual(self.panel.calls, [])


class RoleAdminsTemplateTests(unittest.TestCase):
    def _render(self, actions: dict[str, bool], rows: tuple[RoleAdminRow, ...]) -> str:
        request = SimpleNamespace(query_params={}, url=SimpleNamespace(path="/pg/role-admins"), state=SimpleNamespace())
        page = RoleAdminsPage(rows=rows, roles=({"id": 4, "name": "operator"},), truncated=False)
        staff = {"role": "reseller", "permissions": [], "pg_permissions": ["pg_role_admins"]}
        return templates.env.get_template("pg_role_admins.html").render(
            request=request, app_version="t", staff=staff, identity={"kind": "reseller"}, is_owner=False,
            page=page, admin_actions=actions, csrf_token="tok", active="pg_role_admins",
        )

    @staticmethod
    def _row(username: str, **flags: bool) -> RoleAdminRow:
        return RoleAdminRow(username=username, role_name="r", is_disabled=False, used_traffic=0, data_limit=0,
                            total_users=0, is_self=flags.get("is_self", False), is_managed=flags.get("is_managed", False))

    def test_hostile_admin_name_is_escaped(self) -> None:
        html = self._render({"update": True}, (self._row("<script>x</script>"),))
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_buttons_follow_role_and_skip_managed_rows(self) -> None:
        rows = (self._row("plain"), self._row("shop", is_managed=True), self._row("me", is_self=True))
        html = self._render({"update": True, "delete": False, "create": True}, rows)
        self.assertIn('action="/pg/role-admins/plain/disable"', html)
        self.assertNotIn("/pg/role-admins/plain/delete", html)
        self.assertNotIn("/pg/role-admins/shop/", html)
        self.assertNotIn("/pg/role-admins/me/", html)
        self.assertIn('id="modal-role-admin-create"', html)

    def test_no_create_button_without_create_permission(self) -> None:
        html = self._render({"update": True}, (self._row("plain"),))
        self.assertNotIn('id="modal-role-admin-create"', html)


if __name__ == "__main__":
    unittest.main()
