"""The capability probe must reject credentials the panel refuses.

An API key is never checked when the client is built, so ``GET /api/admin``
is the first request that can refuse it. The probe used to swallow that
refusal and accept a wrong key with an empty role.
"""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from unittest.mock import patch

import httpx

from app.services import pg_read_cache
from app.services.pasarguard import PasarGuardClient
from app.services.pg_access import (
    PG_ADMIN_DISABLED_MSG,
    PG_API_KEY_REJECTED_MSG,
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


class CapabilityProbeCredentialTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
