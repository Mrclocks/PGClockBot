"""Gateway errors get one short retry on reads; Bearer tokens are renewed before they expire."""

from __future__ import annotations

import base64
import json
import time
import unittest
from unittest.mock import patch

import httpx

from app.services import pasarguard
from app.services import pg_read_resilience as resilience
from app.services.pasarguard import PasarGuardClient, jwt_expires_within

BASE = "https://panel.example"


def _jwt(exp: object) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part({'sub': 'shop', 'exp': exp})}.signature"


class GatewayRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        resilience.reset_read_circuits()
        self.statuses: list[int] = []
        self.calls: list[str] = []
        self.delays: list[tuple[float, float]] = []
        patcher = patch.object(resilience._jitter, "uniform", side_effect=self._uniform)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _uniform(self, low: float, high: float) -> float:
        self.delays.append((low, high))
        return 0.0

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.method)
        return httpx.Response(self.statuses.pop(0) if self.statuses else 200, json={})

    async def _read(self, method: str = "GET", *, budget: float | None = None) -> httpx.Response:
        async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(self._answer)) as client:
            if budget is None:
                return await resilience.panel_read_request(client, method, "/api/system")
            with resilience.web_read_budget(seconds=budget):
                return await resilience.panel_read_request(client, method, "/api/system")

    async def test_gateway_error_on_a_read_is_retried_once(self) -> None:
        for status in (502, 503, 504):
            with self.subTest(status=status):
                self.calls.clear()
                self.statuses[:] = [status]
                self.assertEqual((await self._read()).status_code, 200)
                self.assertEqual(self.calls, ["GET", "GET"])

    async def test_retry_waits_a_short_random_pause(self) -> None:
        self.statuses[:] = [502]
        await self._read()
        self.assertEqual(self.delays, [resilience.GATEWAY_RETRY_DELAY_RANGE_SEC])

    async def test_a_second_gateway_error_is_returned_without_more_retries(self) -> None:
        self.statuses[:] = [503, 503, 503]
        self.assertEqual((await self._read()).status_code, 503)
        self.assertEqual(len(self.calls), 2)

    async def test_other_errors_are_not_retried(self) -> None:
        for status in (500, 404, 401):
            with self.subTest(status=status):
                self.calls.clear()
                self.statuses[:] = [status]
                self.assertEqual((await self._read()).status_code, status)
                self.assertEqual(len(self.calls), 1)

    async def test_login_post_is_not_retried_on_a_gateway_error(self) -> None:
        self.statuses[:] = [502]
        self.assertEqual((await self._read("POST")).status_code, 502)
        self.assertEqual(self.calls, ["POST"])

    async def test_no_retry_when_the_page_budget_is_almost_spent(self) -> None:
        self.statuses[:] = [502]
        with self.assertRaises(resilience.PgReadUnavailable):
            await self._read(budget=0.3)
        self.assertEqual(len(self.calls), 1)


class JwtExpiryTests(unittest.TestCase):
    def test_expiry_is_read_from_the_claim(self) -> None:
        now = time.time()
        self.assertTrue(jwt_expires_within(_jwt(now + 30), 120))
        self.assertTrue(jwt_expires_within(_jwt(now - 5), 120))
        self.assertFalse(jwt_expires_within(_jwt(now + 3600), 120))

    def test_unreadable_tokens_never_count_as_expiring(self) -> None:
        for token in ("", "abc", "a.b.c", _jwt(None), _jwt(True), _jwt("soon"), "x." + "!" * 8 + ".y"):
            with self.subTest(token=token):
                self.assertFalse(jwt_expires_within(token, 120))


class TokenRenewalTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, *, token: str, password: str | None) -> tuple[PasarGuardClient, list[str]]:
        logins: list[str] = []

        def answer(request: httpx.Request) -> httpx.Response:
            logins.append(request.url.path)
            return httpx.Response(200, json={"access_token": _jwt(time.time() + 86400)})

        client = PasarGuardClient(username="shop", password=password or "x")
        client._login_password = password
        client._token = token
        client.base_url = BASE
        client._api_base_resolved = True
        client._validated_base, client._validated_until = BASE, float("inf")
        await client.close()
        client._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(answer))
        self.addAsyncCleanup(client.close)
        return client, logins

    async def test_token_is_renewed_before_it_expires(self) -> None:
        expiring = _jwt(time.time() + 30)
        client, logins = await self._client(token=expiring, password="secret")
        renewed = await client.ensure_token()
        self.assertNotEqual(renewed, expiring)
        self.assertEqual(logins, ["/api/admin/token"])

    async def test_fresh_token_is_reused(self) -> None:
        fresh = _jwt(time.time() + 3600)
        client, logins = await self._client(token=fresh, password="secret")
        self.assertEqual(await client.ensure_token(), fresh)
        self.assertEqual(logins, [])

    async def test_cached_shop_client_without_its_password_is_rebuilt_in_time(self) -> None:
        client, logins = await self._client(token=_jwt(time.time() + 30), password=None)
        self.assertFalse(client.is_auth_ready)
        self.assertFalse(pasarguard._cached_client_ready(client))
        # Without a password the current token is still used until the cache replaces the client.
        await client.ensure_token()
        self.assertEqual(logins, [])

    def test_api_key_clients_are_always_ready(self) -> None:
        client = PasarGuardClient(username="shop", api_key="key")
        self.assertTrue(client.is_auth_ready)


if __name__ == "__main__":
    unittest.main()
