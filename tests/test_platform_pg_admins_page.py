"""The web owner sees «ادمین‌ها» when the connected PasarGuard role may read admins.

Covers the platform probe, the sidebar, the owner hint on the PasarGuard overview, and the two
places where a non-owner role must not be treated like a PasarGuard owner: the orphan purge and
admin deletion.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.services.authz import authz_from_staff, can_pg_page
from app.services.panel_nav import build_sidebar, viewer_from_context
from app.services.pg_access import (
    ADMINS_PAGE_HINT_API_KEY,
    ADMINS_PAGE_HINT_ROLE,
    acl_from_admin_payload,
    admins_page_hint,
    clear_platform_pg_capability_cache,
    enrich_platform_admin_staff,
    map_pg_role_to_features,
    resolve_platform_pg_capabilities,
    staff_may_delete_pg_admin,
    with_platform_admins_page,
)

FULL = {"read": True, "create": True, "update": True, "delete": True}


def _role(*, admins: dict | None, owner: bool = False) -> dict:
    perms = {res: dict(FULL) for res in ("system", "users", "templates", "groups", "hosts", "nodes")}
    if admins is not None:
        perms["admins"] = admins
    return {"id": 3, "name": "full", "is_owner": owner, "permissions": perms}


def _owner_staff(**extra: object) -> dict:
    staff = {
        "role": "admin",
        "web_owner": True,
        "org_principal_id": 1,
        "org_depth": 0,
        "org_parent_id": None,
        "org_status": "active",
        "pg_capabilities_ok": True,
        "pg_is_owner": False,
        "pg_permissions": ["pg_overview", "pg_users"],
    }
    staff.update(extra)
    return staff


async def _probe(admin: dict, *, api_key: str = "") -> dict:
    clear_platform_pg_capability_cache()
    with patch("app.services.pasarguard.get_pg") as get_pg:
        client = AsyncMock()
        client.ensure_token = AsyncMock(return_value="tok")
        client.get_current_admin = AsyncMock(return_value=admin)
        get_pg.return_value = client
        with patch("app.config.get_settings") as settings:
            settings.return_value.pg_username = "op"
            settings.return_value.pg_password = "x"
            settings.return_value.pg_api_key = api_key
            settings.return_value.pg_base_url = "https://pg.example"
            return await resolve_platform_pg_capabilities(use_cache=False)


class PlatformProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_role_that_reads_admins_gets_the_admins_page(self):
        caps = await _probe({"username": "op", "role": _role(admins=dict(FULL))})
        self.assertTrue(caps["ok"])
        self.assertFalse(caps["pg_is_owner"])
        self.assertIn("pg_admins", caps["features"])
        self.assertIn("pg_users", caps["features"])

    async def test_scoped_admins_read_counts_as_read(self):
        caps = await _probe({"username": "op", "role": _role(admins={"read": {"scope": 1}})})
        self.assertIn("pg_admins", caps["features"])

    async def test_role_without_admins_read_keeps_the_page_hidden(self):
        for admins in (None, {"read": False, "create": True}, {"read": {"scope": 0}}):
            with self.subTest(admins=admins):
                caps = await _probe({"username": "op", "role": _role(admins=admins)})
                self.assertTrue(caps["ok"])
                self.assertNotIn("pg_admins", caps["features"])
                self.assertIn("pg_users", caps["features"])

    async def test_role_id_fallback_also_grants_the_page(self):
        role = _role(admins={"read": True})
        clear_platform_pg_capability_cache()
        with (
            patch("app.services.pasarguard.get_pg") as get_pg,
            patch(
                "app.services.pg_access.resolve_reseller_pg_features",
                new=AsyncMock(return_value=(map_pg_role_to_features(role), role)),
            ),
            patch("app.config.get_settings") as settings,
        ):
            client = AsyncMock()
            client.ensure_token = AsyncMock(return_value="tok")
            client.get_current_admin = AsyncMock(return_value={"username": "op", "role_id": 3})
            get_pg.return_value = client
            settings.return_value.pg_username = "op"
            settings.return_value.pg_password = "x"
            settings.return_value.pg_api_key = ""
            settings.return_value.pg_base_url = "https://pg.example"
            caps = await resolve_platform_pg_capabilities(use_cache=False)
        self.assertIn("pg_admins", caps["features"])

    async def test_web_owner_sees_admins_in_sidebar_and_may_open_it(self):
        caps = await _probe({"username": "op", "role": _role(admins={"read": True})}, api_key="k")
        with patch(
            "app.services.pg_access.resolve_platform_pg_capabilities",
            new=AsyncMock(return_value=caps),
        ):
            staff = await enrich_platform_admin_staff({"role": "admin"})
        self.assertEqual(staff["pg_auth_method"], "api_key")
        self.assertTrue(can_pg_page(authz_from_staff(staff), "pg_admins"))
        viewer = viewer_from_context(staff, {}, is_owner=True)
        pg_section = next(s for s in build_sidebar(viewer, path="/pg") if s.key == "pg")
        self.assertIn("pg_admins", [item.key for item in pg_section.items])


class OtherAccountsStayOwnerOnlyTests(unittest.TestCase):
    def test_shop_and_staff_role_maps_never_grant_the_admins_page(self):
        role = _role(admins=dict(FULL))
        self.assertNotIn("pg_admins", map_pg_role_to_features(role))
        features, _role_out, is_owner = acl_from_admin_payload({"username": "shop", "role": role})
        self.assertFalse(is_owner)
        self.assertNotIn("pg_admins", features)

    def test_overview_is_added_when_admins_is_the_only_right(self):
        role = {"is_owner": False, "permissions": {"admins": {"read": True}}}
        self.assertEqual(with_platform_admins_page([], role), ["pg_overview", "pg_admins"])


class AdminsPageHintTests(unittest.TestCase):
    def test_api_key_login_gets_the_inherit_permissions_hint(self):
        hint = admins_page_hint(_owner_staff(pg_auth_method="api_key"))
        self.assertIsNotNone(hint)
        self.assertEqual(hint.detail, ADMINS_PAGE_HINT_API_KEY)
        self.assertEqual(hint.href, "/settings?tab=bot")

    def test_password_login_gets_the_role_hint(self):
        hint = admins_page_hint(_owner_staff(pg_auth_method="password"))
        self.assertEqual(hint.detail, ADMINS_PAGE_HINT_ROLE)

    def test_no_hint_when_the_page_is_available_or_the_probe_failed(self):
        cases = {
            "pg owner": _owner_staff(pg_is_owner=True),
            "page granted": _owner_staff(pg_permissions=["pg_overview", "pg_admins"]),
            "probe failed": _owner_staff(pg_capabilities_ok=False),
        }
        for name, staff in cases.items():
            with self.subTest(name):
                self.assertIsNone(admins_page_hint(staff))

    def test_no_hint_for_anyone_but_the_explicit_owner(self):
        for staff in (
            _owner_staff(org_depth=1, org_parent_id=1),
            {"role": "pg_staff", "pg_capabilities_ok": True, "pg_permissions": ["pg_users"]},
            {"role": "admin", "pg_capabilities_ok": True, "pg_permissions": ["pg_users"]},
            None,
        ):
            with self.subTest(staff=staff):
                self.assertIsNone(admins_page_hint(staff))


class PurgeNeedsTheFullAdminListTests(unittest.IsolatedAsyncioTestCase):
    async def test_purge_keeps_rows_when_the_list_may_be_scoped(self):
        from app.services.pg_staff_access import purge_orphaned_staff_access

        session = AsyncMock()
        with (
            patch(
                "app.services.pg_staff_access.list_access_rows",
                new=AsyncMock(return_value=[SimpleNamespace(id=2, pg_username="hidden")]),
            ),
            patch("app.services.pasarguard.get_pg") as get_pg,
        ):
            get_pg.return_value.get_admins = AsyncMock(return_value=[])
            removed = await purge_orphaned_staff_access(session, sees_every_admin=False)
        self.assertEqual(removed, 0)
        get_pg.return_value.get_admins.assert_not_awaited()
        session.delete.assert_not_awaited()


class DeleteAdminTests(unittest.TestCase):
    def test_delete_right_follows_owner_flag_or_admins_delete(self):
        self.assertTrue(staff_may_delete_pg_admin({"pg_is_owner": True}))
        self.assertTrue(staff_may_delete_pg_admin({"pg_is_owner": False, "pg_role": _role(admins=dict(FULL))}))
        self.assertFalse(
            staff_may_delete_pg_admin({"pg_is_owner": False, "pg_role": _role(admins={"read": True})})
        )
        self.assertFalse(staff_may_delete_pg_admin(None))

    def _client(self, staff: dict) -> TestClient:
        from app.api.pg_pages import register_pg_pages

        app = FastAPI()

        async def _db():
            yield AsyncMock()

        register_pg_pages(
            app,
            render=lambda *_a, **_k: HTMLResponse(""),
            require_pg_perm=lambda _perm: (lambda: staff),
            get_db=_db,
        )
        return TestClient(app)

    def test_read_only_role_cannot_delete_and_the_shop_is_untouched(self):
        staff = _owner_staff(pg_permissions=["pg_overview", "pg_admins"], pg_role=_role(admins={"read": True}))
        reseller = SimpleNamespace(user_id=7)
        with (
            patch("app.services.pg_staff_access.reseller_by_pg_username", new=AsyncMock(return_value=reseller)),
            patch("app.services.resellers.revoke_reseller", new=AsyncMock()) as revoke,
            patch("app.api.pg_pages.get_pg") as get_pg,
        ):
            get_pg.return_value.delete_admin = AsyncMock()
            resp = self._client(staff).post(
                "/pg/admins/shopadmin/delete", data={"reason": "no longer needed"}, follow_redirects=False
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("err=", resp.headers["location"])
        revoke.assert_not_awaited()
        get_pg.return_value.delete_admin.assert_not_awaited()

    def test_role_with_admins_delete_still_deletes(self):
        staff = _owner_staff(pg_permissions=["pg_overview", "pg_admins"], pg_role=_role(admins=dict(FULL)))
        with (
            patch("app.services.pg_staff_access.reseller_by_pg_username", new=AsyncMock(return_value=None)),
            patch("app.services.pg_staff_access.access_by_pg_username", new=AsyncMock(return_value=None)),
            patch("app.api.pg_pages.get_pg") as get_pg,
        ):
            get_pg.return_value.delete_admin = AsyncMock()
            resp = self._client(staff).post(
                "/pg/admins/plain/delete", data={"reason": "no longer needed"}, follow_redirects=False
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("ok=", resp.headers["location"])
        get_pg.return_value.delete_admin.assert_awaited_once_with("plain")


if __name__ == "__main__":
    unittest.main()
