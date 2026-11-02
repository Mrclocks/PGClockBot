"""Connection behaviour of the PasarGuard client on slow or unstable links."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services import pasarguard
from app.services.pasarguard import PasarGuardClient, PasarGuardError
from app.services.pg_read_resilience import (
    PG_READ_UNAVAILABLE_MESSAGE,
    panel_read_request,
    reset_read_circuits,
    web_read_budget,
)

BASE = "https://panel.example"


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))


class InterruptedReadRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_read_circuits()

    async def test_dropped_keepalive_socket_is_retried_once(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if len(calls) == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return httpx.Response(200, json={"ok": True})

        async with _mock_client(handler) as client:
            response = await panel_read_request(client, "GET", "/api/system")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 2)

    async def test_web_read_also_retries_inside_its_budget(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadError("connection reset")
            return httpx.Response(200, json={})

        async with _mock_client(handler) as client:
            with web_read_budget(seconds=5.0) as budget:
                response = await panel_read_request(client, "GET", "/api/system")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(budget.failure)
        self.assertEqual(len(calls), 2)

    async def test_no_retry_when_budget_is_almost_spent(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ReadError("connection reset")

        async with _mock_client(handler) as client:
            with web_read_budget(seconds=0.3):
                with self.assertRaises(httpx.TimeoutException):
                    await panel_read_request(client, "GET", "/api/system")
        self.assertEqual(len(calls), 1)

    async def test_timeouts_are_not_retried(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ReadTimeout("slow panel")

        async with _mock_client(handler) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await panel_read_request(client, "GET", "/api/system")
        self.assertEqual(len(calls), 1)

    async def test_mutations_are_never_retried(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            raise httpx.ReadError("connection reset after send")

        client = PasarGuardClient(username="shop", api_key="key")
        client.base_url = BASE
        client._api_base_resolved = True
        client._validated_base, client._validated_until = BASE, float("inf")
        await client.close()
        client._client = _mock_client(handler)
        try:
            with self.assertRaises(httpx.ReadError):
                await client.request("POST", "/api/user", json={"username": "u1"})
        finally:
            await client.close()
        self.assertEqual(calls, ["POST"])


class ClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_login_closes_the_unshared_client(self) -> None:
        client = PasarGuardClient(username="shop", password="secret")
        client.ensure_token = AsyncMock(side_effect=PasarGuardError("login failed", 401))
        cache: dict[tuple[int, str], PasarGuardClient] = {}
        with self.assertRaises(PasarGuardError):
            await pasarguard._authenticate_and_cache(cache, (7, "shop"), client)
        self.assertEqual(cache, {})
        self.assertTrue(client._client.is_closed)

    async def test_replaced_clients_are_closed_after_the_grace_period(self) -> None:
        old = PasarGuardClient(username="old-admin", api_key="k1")
        same_key_old = PasarGuardClient(username="shop", api_key="k2")
        new = PasarGuardClient(username="shop", api_key="k3")
        new.ensure_token = AsyncMock(return_value="__apikey__")
        cache = {(7, "old-admin"): old, (7, "shop"): same_key_old, (8, "other"): None}
        with patch.object(pasarguard, "RETIRED_CLIENT_GRACE_SEC", 0):
            result = await pasarguard._authenticate_and_cache(cache, (7, "shop"), new)
            await asyncio.sleep(0.05)
        self.assertIs(result, new)
        self.assertEqual(set(cache), {(7, "shop"), (8, "other")})
        self.assertTrue(old._client.is_closed)
        self.assertTrue(same_key_old._client.is_closed)
        self.assertFalse(new._client.is_closed)
        await new.close()

    async def test_retired_client_still_held_by_a_caller_reopens(self) -> None:
        client = PasarGuardClient(username="shop", api_key="k")
        await client.close()
        reopened = client._http()
        self.assertFalse(reopened.is_closed)
        self.assertIs(reopened, client._client)
        await client.close()

    async def test_invalidating_a_shop_retires_its_clients(self) -> None:
        client = PasarGuardClient(username="shop", api_key="k")
        pasarguard._pg_reseller_cache[(41, "shop")] = client
        with patch.object(pasarguard, "RETIRED_CLIENT_GRACE_SEC", 0):
            pasarguard.invalidate_pg_reseller_cache(41)
            await asyncio.sleep(0.05)
        self.assertNotIn((41, "shop"), pasarguard._pg_reseller_cache)
        self.assertTrue(client._client.is_closed)


class LoginConnectionReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_reuses_the_pooled_connection(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/admin/token":
                return httpx.Response(200, json={"access_token": "jwt-1"})
            return httpx.Response(404)

        client = PasarGuardClient(username="shop", password="secret")
        client.base_url = BASE
        client._api_base_resolved = True
        client._validated_base, client._validated_until = BASE, float("inf")
        await client.close()
        client._client = _mock_client(handler)
        try:
            with patch.object(pasarguard, "new_panel_http_client", side_effect=AssertionError("new connection")):
                token = await client.ensure_token()
        finally:
            await client.close()
        self.assertEqual(token, "jwt-1")
        self.assertIsNone(client._login_password)


class OverviewEarlyStopTests(unittest.IsolatedAsyncioTestCase):
    async def _overview(self, current_admin_error: Exception) -> tuple[dict, AsyncMock]:
        from app.services.pg_overview import build_reseller_pg_overview

        fake = AsyncMock()
        fake.get_current_admin = AsyncMock(side_effect=current_admin_error)
        fake.get_admin = AsyncMock(return_value=None)
        with patch("app.services.pg_read.staff_pg_read_client", new=AsyncMock(return_value=fake)):
            out = await build_reseller_pg_overview({"pg_admin_username": "shop1", "role": "reseller"})
        return out, fake.get_admin

    async def test_unreachable_panel_skips_slow_fallback_lookups(self) -> None:
        out, get_admin = await self._overview(httpx.ConnectError("unreachable"))
        get_admin.assert_not_awaited()
        self.assertEqual(out["error"], PG_READ_UNAVAILABLE_MESSAGE)

    async def test_forbidden_own_account_still_tries_fallbacks(self) -> None:
        out, get_admin = await self._overview(PasarGuardError("forbidden", 403))
        get_admin.assert_awaited_once()
        self.assertNotEqual(out["error"], PG_READ_UNAVAILABLE_MESSAGE)


class ApiBaseDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        pasarguard._discovered_api_bases.clear()

    async def test_second_client_reuses_the_discovered_api_root(self) -> None:
        from app.config import Settings

        settings = Settings(
            _env_file=None, PG_BASE_URL="https://127.0.0.1/dashboard", PG_USERNAME="owner",
            PG_PASSWORD="x", PG_API_KEY="", PG_ACCESS_TOKEN="",
        )
        calls: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/openapi.json":
                return httpx.Response(200, text="PasarGuard")
            return httpx.Response(404)

        def factory(base_url: str, **kwargs) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(respond))

        clients = []
        with patch("app.services.pasarguard.get_settings", return_value=settings), patch.object(
            pasarguard, "new_panel_http_client", side_effect=factory
        ):
            for name in ("shop_a", "shop_b"):
                client = PasarGuardClient(username=name, access_token="t")
                client._validated_base, client._validated_until = client.base_url, float("inf")
                clients.append(client)
                await client._ensure_api_base()
        for client in clients:
            self.assertEqual(client.base_url, "https://127.0.0.1")
            await client.close()
        self.assertEqual(calls, ["/dashboard/openapi.json", "/openapi.json"])


if __name__ == "__main__":
    unittest.main()
