"""The admin usage reset hits the panel's admin reset route (never a user route) and tries fallbacks only on 404/405."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from app.services.pasarguard import PasarGuardClient, PasarGuardError


def _client(admin: dict | None, request: AsyncMock) -> PasarGuardClient:
    client = object.__new__(PasarGuardClient)
    client.get_admin = AsyncMock(return_value=admin)
    client.request = request
    return client


class AdminResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_resets_by_id_with_a_post(self) -> None:
        request = AsyncMock(return_value={"used_traffic": 0})
        await _client({"id": 7, "username": "shop"}, request).reset_admin_usage("shop")
        request.assert_awaited_once_with("POST", "/api/admin/by-id/7/reset")

    async def test_falls_back_to_the_username_routes_on_not_found(self) -> None:
        request = AsyncMock(side_effect=[PasarGuardError("x", 404), PasarGuardError("x", 405), {}])
        await _client({"id": 7, "username": "a b/c"}, request).reset_admin_usage("a b/c")
        self.assertEqual(request.await_args_list[-1].args[1], "/api/admin/a%20b%2Fc/reset")

    async def test_a_refusal_is_not_retried_on_another_route(self) -> None:
        request = AsyncMock(side_effect=PasarGuardError("no permission", 403))
        with self.assertRaises(PasarGuardError):
            await _client({"id": 7, "username": "shop"}, request).reset_admin_usage("shop")
        self.assertEqual(request.await_count, 1)

    async def test_unknown_admin_is_refused_before_any_write(self) -> None:
        request = AsyncMock()
        with self.assertRaises(PasarGuardError) as caught:
            await _client(None, request).reset_admin_usage("ghost")
        self.assertEqual(caught.exception.status_code, 404)
        request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
