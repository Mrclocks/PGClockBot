"""A new install's PasarGuard account, from the access probe to saving a plan.

Runs the real probe, the real owner-staff enrichment, the plan catalog and
the plan gates against a fake panel that answers like PasarGuard: password
or ``pg_key_`` API key, the role's permissions, allow-lists and limits. A
login that really has access must get no access error, for both sign-in
methods, and every restriction of the role must still hold.
"""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services import pg_read_cache
from app.services.pasarguard import PasarGuardClient
from app.services.pg_access import enrich_platform_admin_staff, resolve_platform_pg_capabilities
from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
from app.services.plans_catalog import (
    PLAN_GROUPS_DENIED_MESSAGE,
    ROLE_REQUIRES_TEMPLATE_MESSAGE,
    load_pg_plan_options,
    plan_groups_denial,
    template_allowed_for_staff,
)

BASE = "https://panel.example"
USERNAME = "seller"
PASSWORD = "Seller-Pass-1"
API_KEY = "pg_key_4f1c2a9e-3b7d-4c5e-9a8f-1d2e3c4b5a6f"
GB = 1024**3

TEMPLATES = [{"id": 10, "name": "Monthly"}, {"id": 20, "name": "Weekly"}]
GROUPS = [{"id": 1, "name": "Main"}, {"id": 2, "name": "Backup"}]

SELLER_ROLE = {
    "id": 3,
    "name": "Seller",
    "is_owner": False,
    "permissions": {
        "users": {"create": True, "read": {"scope": 1}},
        "templates": {"read": True, "read_simple": True},
        "groups": {"read_simple": True},
    },
    "limits": {"data_limit_max": 50 * GB, "expire_max": None, "max_users": 100},
    "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": None},
}

OWNER_BASE = {
    "role": "admin",
    "web_owner": True,
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
    "username": "owner",
}


def _json(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json"})


class FakePasarGuard:
    """Answers the endpoints the install path uses, enforcing the role like PasarGuard."""

    def __init__(self, role: dict) -> None:
        self.role = role

    def _signed_in(self, request: httpx.Request) -> bool:
        if request.headers.get("X-Api-Key") == API_KEY:
            return True
        return request.headers.get("Authorization") == "Bearer header.payload.sig"

    def _allows(self, resource: str, action: str) -> bool:
        value = (self.role["permissions"].get(resource) or {}).get(action)
        return value is True or (isinstance(value, dict) and int(value.get("scope") or 0) > 0)

    def _visible(self, rows: list[dict], key: str) -> list[dict]:
        allowed = self.role["access"].get(key)
        return rows if allowed is None else [r for r in rows if r["id"] in allowed]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/admin/token":
            body = dict(httpx.QueryParams(request.content.decode()))
            if body.get("username") == USERNAME and body.get("password") == PASSWORD:
                return _json(200, {"access_token": "header.payload.sig", "token_type": "bearer"})
            return _json(401, {"detail": "Incorrect username or password"})
        if not self._signed_in(request):
            return _json(401, {"detail": "Could not validate credentials"})
        if path == "/api/admin":
            return _json(200, {"id": 2, "username": USERNAME, "role": self.role, "total_users": 4})
        routes = {
            "/api/user_templates": ("templates", "read", TEMPLATES, "allowed_template_ids"),
            "/api/user_templates/simple": ("templates", "read_simple", TEMPLATES, "allowed_template_ids"),
            "/api/groups/simple": ("groups", "read_simple", GROUPS, "allowed_group_ids"),
        }
        if path in routes:
            resource, action, rows, key = routes[path]
            if not self._allows(resource, action):
                return _json(403, {"detail": f"Permission denied: {resource}.{action}"})
            visible = self._visible(rows, key)
            return _json(200, visible if path == "/api/user_templates" else {resource: visible})
        return _json(404, {"detail": "Not Found"})


class NewInstallAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        pg_read_cache.clear_store()

    def tearDown(self) -> None:
        pg_read_cache.clear_store()

    async def _install(self, role: dict, **credentials: str) -> tuple[dict, dict, PasarGuardClient]:
        panel = FakePasarGuard(role)

        def client_for(base: str, **_kwargs: object) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(panel))

        async def skip_dns_check(_self: PasarGuardClient, **_kwargs: object) -> None:
            return None

        patches = [
            patch("app.services.pasarguard.new_panel_http_client", client_for),
            patch.object(PasarGuardClient, "_ensure_safe_target", skip_dns_check),
            patch("app.services.pg_access.assert_safe_pg_base_url", side_effect=lambda url, **_kw: url),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        caps = await resolve_platform_pg_capabilities(
            username=USERNAME, base_url=BASE, use_cache=False, **credentials
        )
        with patch("app.services.pg_access.resolve_platform_pg_capabilities", AsyncMock(return_value=caps)):
            staff = await enrich_platform_admin_staff(dict(OWNER_BASE))
        client = PasarGuardClient(username=USERNAME, **credentials)
        client.base_url = BASE
        client._client = client_for(BASE)
        self.addAsyncCleanup(client.close)
        return caps, staff, client

    async def _plan_options(self, staff: dict, client: PasarGuardClient) -> tuple[list, list, str | None]:
        with patch("app.services.plans_catalog.get_pg", return_value=client):
            return await load_pg_plan_options(staff, session=None)

    async def test_password_and_api_key_get_the_same_working_access(self) -> None:
        for credentials in ({"password": PASSWORD}, {"api_key": API_KEY}):
            with self.subTest(sign_in=next(iter(credentials))):
                caps, staff, client = await self._install(copy.deepcopy(SELLER_ROLE), **credentials)
                self.assertTrue(caps["ok"], caps["error"])
                self.assertFalse(staff["pg_is_owner"])
                templates, groups, err = await self._plan_options(staff, client)
                self.assertIsNone(err)
                self.assertEqual([t["id"] for t in templates], [10, 20])
                self.assertEqual([g["id"] for g in groups], [1, 2])
                for template in templates:
                    self.assertTrue(template_allowed_for_staff(staff, template["id"]))
                self.assertIsNone(plan_groups_denial(staff, [g["id"] for g in groups]))

    async def test_role_limits_are_read_for_both_sign_in_methods(self) -> None:
        for credentials in ({"password": PASSWORD}, {"api_key": API_KEY}):
            with self.subTest(sign_in=next(iter(credentials))):
                _caps, staff, client = await self._install(copy.deepcopy(SELLER_ROLE), **credentials)
                await assert_user_plan_within_limits(staff, data_limit=10 * GB, duration_days=30, client=client)
                with self.assertRaises(PgQuotaError):
                    await assert_user_plan_within_limits(
                        staff, data_limit=100 * GB, duration_days=30, client=client
                    )

    async def test_allow_lists_still_narrow_what_can_be_listed_and_saved(self) -> None:
        role = copy.deepcopy(SELLER_ROLE)
        role["access"].update(allowed_template_ids=[10], allowed_group_ids=[1])
        _caps, staff, client = await self._install(role, api_key=API_KEY)
        templates, groups, _err = await self._plan_options(staff, client)
        self.assertEqual([t["id"] for t in templates], [10])
        self.assertEqual([g["id"] for g in groups], [1])
        self.assertFalse(template_allowed_for_staff(staff, 20))
        self.assertEqual(plan_groups_denial(staff, [1, 2]), PLAN_GROUPS_DENIED_MESSAGE)

    async def test_read_simple_only_role_is_offered_its_templates(self) -> None:
        role = copy.deepcopy(SELLER_ROLE)
        role["permissions"]["templates"] = {"read_simple": True}
        _caps, staff, client = await self._install(role, password=PASSWORD)
        templates, _groups, err = await self._plan_options(staff, client)
        self.assertIsNone(err)
        self.assertEqual([t["id"] for t in templates], [10, 20])
        self.assertTrue(template_allowed_for_staff(staff, 10))

    async def test_template_only_role_cannot_save_a_group_plan(self) -> None:
        role = copy.deepcopy(SELLER_ROLE)
        role["access"]["require_template"] = True
        _caps, staff, _client = await self._install(role, password=PASSWORD)
        self.assertTrue(template_allowed_for_staff(staff, 10))
        self.assertEqual(plan_groups_denial(staff, [1]), ROLE_REQUIRES_TEMPLATE_MESSAGE)

    async def test_wrong_api_key_never_reaches_the_plan_form(self) -> None:
        caps, staff, _client = await self._install(
            copy.deepcopy(SELLER_ROLE), api_key="pg_key_00000000-0000-4000-8000-000000000000"
        )
        self.assertFalse(caps["ok"])
        self.assertEqual(staff["pg_permissions"], [])
        self.assertFalse(template_allowed_for_staff(staff, 10))
        self.assertIsNotNone(plan_groups_denial(staff, [1]))

    async def test_owner_account_keeps_full_unrestricted_access(self) -> None:
        owner_role = {"id": 1, "name": "Owner", "is_owner": True, "permissions": {}, "access": {}}
        caps, staff, client = await self._install(owner_role, password=PASSWORD)
        self.assertTrue(caps["pg_is_owner"])
        self.assertTrue(template_allowed_for_staff(staff, 999))
        self.assertIsNone(plan_groups_denial(staff, [1, 2, 3]))
        await assert_user_plan_within_limits(staff, data_limit=10_000 * GB, duration_days=0, client=client)


if __name__ == "__main__":
    unittest.main()
