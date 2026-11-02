"""The PasarGuard capability probe must not show raw transport errors to staff."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services.pasarguard import PasarGuardClient
from app.services.pg_access import resolve_platform_pg_capabilities


class CapabilityProbeErrorTextTests(unittest.IsolatedAsyncioTestCase):
    async def _probe_error(self, exc: Exception) -> str:
        with patch.object(PasarGuardClient, "ensure_token", new=AsyncMock(side_effect=exc)):
            caps = await resolve_platform_pg_capabilities(username="owner", password="secret", use_cache=False)
        self.assertFalse(caps["ok"])
        self.assertEqual(caps["features"], [])
        return caps["error"]

    async def test_connect_error_hides_internal_host_and_port(self) -> None:
        error = await self._probe_error(
            httpx.ConnectError("[Errno 111] Connection refused to http://10.0.0.5:8000/api/admin/token")
        )
        self.assertNotIn("10.0.0.5", error)
        self.assertNotIn("8000", error)
        self.assertIn("پاسارگارد", error)

    async def test_timeout_is_explained_in_persian(self) -> None:
        error = await self._probe_error(httpx.ConnectTimeout("timed out"))
        self.assertIn("پاسارگارد", error)
        self.assertNotIn("timed out", error)

    async def test_unknown_internal_error_uses_the_generic_message(self) -> None:
        error = await self._probe_error(RuntimeError('Traceback File "/app/services/x.py"'))
        self.assertEqual(error, "خطا در خواندن نقش پاسارگارد")


if __name__ == "__main__":
    unittest.main()
