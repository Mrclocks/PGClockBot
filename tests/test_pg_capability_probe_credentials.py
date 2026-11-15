"""The capability probe must reject credentials the panel refuses.

An API key is never checked when the client is built, so ``GET /api/admin``
is the first request that can refuse it. The probe used to swallow that
refusal and accept a wrong key with an empty role.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Callable
from unittest.mock import patch

import httpx

from app.services import pg_read_cache
from app.services.pasarguard import PasarGuardClient
from app.services.pg_access import (
    AUDIT_NOTE_API_KEY_LIMITED,
    AUDIT_NOTE_CUSTOM_KEY_ON_OWNER,
    PG_ADMIN_DISABLED_MSG,
    PG_API_KEY_REJECTED_MSG,
    PG_CHECK_TIMEOUT_MSG,
    check_pg_credentials,
    public_pg_access_audit,
    resolve_platform_pg_capabilities,
)

BASE = "https://panel.example"
API_KEY = "pg_key_4f1c2a9e-3b7d-4c5e-9a8f-1d2e3c4b5a6f"

LIMITED_ADMIN = {
    "id": 2,
    "username": "seller",
    "role": {
        "id": 3,
        "name": "Seller",
        "is_owner": False,
        "permissions": {"users": {"create": True}, "templates": {"read": True}},
        "access": {"allowed_template_ids": None, "allowed_group_ids": None},
    },
}

Handler = Callable[[httpx.Request], httpx.Response]


def _json(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json"})


class _ProbeHarness(unittest.IsolatedAsyncioTestCase):
    """Runs the real probe and client against a fake panel; holds no tests."""

    def setUp(self) -> None:
        pg_read_cache.clear_store()
        self.requests: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        pg_read_cache.clear_store()

    async def _probe(self, handler: Handler, **credentials: str) -> dict:
        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append((request.method, request.url.path))
            return handler(request)

        def client_for(base: str, **_kwargs: object) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(recording))

        async def skip_dns_check(_self: PasarGuardClient, **_kwargs: object) -> None:
            return None

        with patch("app.services.pasarguard.new_panel_http_client", client_for), \
             patch.object(PasarGuardClient, "_ensure_safe_target", skip_dns_check), \
             patch("app.services.pg_access.assert_safe_pg_base_url", side_effect=lambda url, **_kw: url):
            return await resolve_platform_pg_capabilities(
                username="seller", base_url=BASE, use_cache=False, **credentials
            )


class CapabilityProbeCredentialTests(_ProbeHarness):
    async def test_rejected_api_key_fails_with_an_api_key_message(self) -> None:
        caps = await self._probe(
            lambda _req: _json(401, {"detail": "Could not validate credentials"}), api_key=API_KEY
        )
        self.assertFalse(caps["ok"])
        self.assertEqual(caps["error"], PG_API_KEY_REJECTED_MSG)
        self.assertEqual(caps["features"], [])
        self.assertEqual(self.requests, [("GET", "/api/admin")])

    async def test_rejected_api_key_is_not_rescued_by_a_valid_password(self) -> None:
        caps = await self._probe(
            lambda _req: _json(401, {"detail": "Could not validate credentials"}),
            api_key=API_KEY,
            password="right-password",
        )
        self.assertFalse(caps["ok"])
        self.assertEqual(caps["error"], PG_API_KEY_REJECTED_MSG)

    async def test_valid_api_key_reads_the_live_role(self) -> None:
        caps = await self._probe(lambda _req: _json(200, LIMITED_ADMIN), api_key=API_KEY)
        self.assertTrue(caps["ok"])
        self.assertFalse(caps["pg_is_owner"])
        self.assertIn("pg_templates", caps["features"])
        self.assertEqual(caps["role"]["id"], 3)

    async def test_disabled_admin_is_rejected(self) -> None:
        caps = await self._probe(
            lambda _req: _json(403, {"detail": "your account has been disabled"}), api_key=API_KEY
        )
        self.assertFalse(caps["ok"])
        self.assertEqual(caps["error"], PG_ADMIN_DISABLED_MSG)

    async def test_other_403_wording_is_not_read_as_a_disabled_account(self) -> None:
        caps = await self._probe(
            lambda _req: _json(403, {"detail": "Permission disabled for this role"}), api_key=API_KEY
        )
        self.assertTrue(caps["ok"])
        self.assertNotEqual(caps.get("error"), PG_ADMIN_DISABLED_MSG)

    async def test_unreachable_panel_fails_closed_for_an_api_key(self) -> None:
        def refuse(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno 111] Connection refused to http://10.0.0.5:8000")

        caps = await self._probe(refuse, api_key=API_KEY)
        self.assertFalse(caps["ok"])
        self.assertNotIn("10.0.0.5", caps["error"])
        self.assertIn("پاسارگارد", caps["error"])

    async def test_panel_gateway_error_fails_closed_for_an_api_key(self) -> None:
        caps = await self._probe(lambda _req: httpx.Response(503, text="down"), api_key=API_KEY)
        self.assertFalse(caps["ok"])
        self.assertIn("پاسارگارد", caps["error"])

    async def test_hidden_self_endpoint_keeps_the_username_fallback(self) -> None:
        """Older panels or roles that refuse ``/api/admin`` keep the old fallback."""
        caps = await self._probe(lambda _req: _json(403, {"detail": "Permission denied"}), api_key=API_KEY)
        self.assertTrue(caps["ok"])
        self.assertEqual(caps["username"], "seller")
        self.assertEqual(caps["features"], [])

    async def test_password_login_still_reads_the_role(self) -> None:
        def panel(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/admin/token":
                return _json(200, {"access_token": "header.payload.sig", "token_type": "bearer"})
            return _json(200, LIMITED_ADMIN)

        caps = await self._probe(panel, password="right-password")
        self.assertTrue(caps["ok"])
        self.assertIn("pg_templates", caps["features"])

    async def test_wrong_password_is_still_rejected(self) -> None:
        caps = await self._probe(lambda _req: _json(401, {"detail": "Incorrect username or password"}), password="x")
        self.assertFalse(caps["ok"])
        self.assertIn("نام کاربری یا رمز", caps["error"])


def _custom_key_admin(role_id: int, permissions: dict) -> dict:
    """``GET /api/admin`` for a key created without "inherit permissions"."""
    return {
        "id": 1,
        "username": "seller",
        "role": {"id": role_id, "name": "Owner", "is_owner": False, "permissions": permissions},
    }


def _roles_panel(admin: dict, roles: list[dict] | None) -> Handler:
    def panel(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin":
            return _json(200, admin)
        if request.url.path.startswith("/api/admin-roles"):
            if roles is None:
                return _json(403, {"detail": "Permission denied"})
            return _json(200, {"roles": roles, "total": len(roles)})
        return _json(404, {"detail": "Not Found"})

    return panel


class ApiKeyReportTests(_ProbeHarness):
    """The setup report explains a key that hides the owner role."""

    async def test_custom_key_on_the_owner_role_is_explained(self) -> None:
        perms = {"users": {"create": True}, "templates": {"read": True}, "groups": {"read": True}}
        roles = [{"id": 1, "name": "Owner", "is_owner": True}]
        caps = await self._probe(_roles_panel(_custom_key_admin(1, perms), roles), api_key=API_KEY)
        self.assertTrue(caps["ok"])
        self.assertFalse(caps["pg_is_owner"])
        self.assertIs(caps["api_key_overrides_owner_role"], True)
        self.assertEqual(public_pg_access_audit(caps)["notes"], [AUDIT_NOTE_CUSTOM_KEY_ON_OWNER])

    async def test_unreadable_role_list_gives_the_general_api_key_hint(self) -> None:
        perms = {"templates": {"read": True}, "groups": {"read": True}}
        caps = await self._probe(_roles_panel(_custom_key_admin(1, perms), None), api_key=API_KEY)
        self.assertTrue(caps["ok"])
        self.assertIsNone(caps["api_key_overrides_owner_role"])
        self.assertEqual(public_pg_access_audit(caps)["notes"], [AUDIT_NOTE_API_KEY_LIMITED])

    async def test_limited_role_without_catalog_access_names_what_is_missing(self) -> None:
        roles = [{"id": 3, "name": "Seller", "is_owner": False}]
        admin = _custom_key_admin(3, {"users": {"create": True}})
        caps = await self._probe(_roles_panel(admin, roles), api_key=API_KEY)
        self.assertIs(caps["api_key_overrides_owner_role"], False)
        notes = public_pg_access_audit(caps)["notes"]
        self.assertEqual(len(notes), 1)
        self.assertIn("«تمپلیت» و «گروه»", notes[0])

    async def test_password_login_has_no_api_key_note(self) -> None:
        def panel(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/admin/token":
                return _json(200, {"access_token": "header.payload.sig", "token_type": "bearer"})
            return _json(200, LIMITED_ADMIN)

        caps = await self._probe(panel, password="right-password")
        self.assertEqual(caps["auth_method"], "password")
        self.assertNotIn(("GET", "/api/admin-roles/simple"), self.requests)
        notes = public_pg_access_audit(caps)["notes"]
        self.assertEqual(len(notes), 1)
        self.assertIn("«گروه»", notes[0])
        self.assertNotIn("«تمپلیت»", notes[0])

    async def test_owner_report_has_no_notes(self) -> None:
        owner = {"id": 1, "username": "seller", "role": {"id": 1, "is_owner": True}}
        caps = await self._probe(_roles_panel(owner, []), api_key=API_KEY)
        self.assertTrue(caps["pg_is_owner"])
        self.assertNotIn(("GET", "/api/admin-roles/simple"), self.requests)
        self.assertEqual(public_pg_access_audit(caps)["notes"], [])


class SettingsCredentialCheckTests(_ProbeHarness):
    """Settings «ذخیره» runs this before writing credentials to .env."""

    async def _check(self, handler: Handler, **credentials: str) -> str | None:
        caps_probe = self._probe

        async def probe_for(**kwargs: object) -> dict:
            creds = {k: v for k, v in kwargs.items() if k in {"password", "api_key"} and v}
            return await caps_probe(handler, **creds)

        with patch("app.services.pg_access.resolve_platform_pg_capabilities", side_effect=probe_for):
            return await check_pg_credentials(
                username="seller", password=credentials.get("password", ""),
                api_key=credentials.get("api_key", ""), base_url=BASE,
            )

    async def test_working_credentials_pass(self) -> None:
        self.assertIsNone(await self._check(lambda _req: _json(200, LIMITED_ADMIN), api_key=API_KEY))

    async def test_refused_key_is_reported_with_the_reason(self) -> None:
        error = await self._check(lambda _req: _json(401, {"detail": "Could not validate credentials"}), api_key=API_KEY)
        self.assertIn(PG_API_KEY_REJECTED_MSG, error)

    async def test_slow_panel_reports_a_timeout(self) -> None:
        async def never_answers(**_kwargs: object) -> dict:
            await asyncio.sleep(1)
            return {"ok": True}

        with patch("app.services.pg_access.resolve_platform_pg_capabilities", side_effect=never_answers), \
             patch("app.services.pg_access.PG_CHECK_TIMEOUT_SEC", 0.01):
            error = await check_pg_credentials(username="seller", password="x", api_key="", base_url=BASE)
        self.assertEqual(error, PG_CHECK_TIMEOUT_MSG)


if __name__ == "__main__":
    unittest.main()
