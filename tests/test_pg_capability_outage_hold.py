"""A capability probe that failed because PasarGuard is down is held briefly.

Every authenticated web request resolves the platform admin's PasarGuard
access. Without the hold, each page during an outage waited on the panel
again before it could render its local fallback.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.services import pg_read_cache
from app.services.pasarguard import PasarGuardError
from app.services.pg_access import clear_platform_pg_capability_cache, resolve_platform_pg_capabilities

SETTINGS = SimpleNamespace(
    pg_username="owner",
    pg_password="secret",
    pg_api_key="",
    pg_base_url="https://panel.example",
)
OWNER_ADMIN = {"username": "owner", "is_sudo": True, "role": {"id": 1, "is_owner": True, "permissions": {}}}


class _FakePanel:
    """Shared panel client whose self lookup answers with the configured result."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.self_lookups = 0

    async def ensure_token(self) -> None:
        return None

    async def get_current_admin(self) -> dict:
        self.self_lookups += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return dict(self.answer)

    async def get_admin(self, _username: str) -> dict:
        return {"username": "owner"}

    def __getattr__(self, name: str) -> object:
        async def unavailable(*_args: object, **_kwargs: object) -> None:
            raise PasarGuardError("not in this fake", status_code=404)

        return unavailable


class CapabilityOutageHoldTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        pg_read_cache.clear_store()
        self.clock = 1000.0
        self.addCleanup(pg_read_cache.clear_store)
        for target in (
            patch("app.config.get_settings", return_value=SETTINGS),
            patch.object(pg_read_cache.time, "monotonic", side_effect=lambda: self.clock),
        ):
            target.start()
            self.addCleanup(target.stop)

    async def _probe(self, panel: _FakePanel, **kwargs: object) -> dict:
        with patch("app.services.pasarguard.get_pg", return_value=panel):
            return await resolve_platform_pg_capabilities(**kwargs)

    async def test_an_unreachable_panel_is_asked_once_while_the_failure_is_held(self) -> None:
        panel = _FakePanel(httpx.ConnectError("offline"))
        first = await self._probe(panel)
        self.clock += pg_read_cache.CAPABILITY_OUTAGE_HOLD_SEC - 1
        second = await self._probe(panel)
        self.assertFalse(first["ok"])
        self.assertEqual(second, first)
        self.assertEqual(panel.self_lookups, 1)

    async def test_a_gateway_error_is_held_like_an_unreachable_panel(self) -> None:
        panel = _FakePanel(PasarGuardError("bad gateway", status_code=502))
        await self._probe(panel)
        await self._probe(panel)
        self.assertEqual(panel.self_lookups, 1)

    async def test_the_panel_is_asked_again_after_the_hold_and_a_recovery_is_used(self) -> None:
        panel = _FakePanel(httpx.ConnectError("offline"))
        await self._probe(panel)
        self.clock += pg_read_cache.CAPABILITY_OUTAGE_HOLD_SEC + 1
        panel.answer = OWNER_ADMIN
        recovered = await self._probe(panel)
        self.assertTrue(recovered["ok"])
        self.assertEqual(panel.self_lookups, 2)

    async def test_a_refused_login_is_not_held(self) -> None:
        panel = _FakePanel(PasarGuardError("unauthorized", status_code=401))
        first = await self._probe(panel)
        await self._probe(panel)
        self.assertFalse(first["ok"])
        self.assertEqual(panel.self_lookups, 2)

    async def test_clearing_the_capability_cache_drops_a_held_outage(self) -> None:
        panel = _FakePanel(httpx.ConnectError("offline"))
        await self._probe(panel)
        clear_platform_pg_capability_cache()
        panel.answer = OWNER_ADMIN
        self.assertTrue((await self._probe(panel))["ok"])
        self.assertEqual(panel.self_lookups, 2)

    async def test_an_uncached_probe_neither_reads_nor_stores_a_held_outage(self) -> None:
        panel = _FakePanel(httpx.ConnectError("offline"))
        await self._probe(panel, use_cache=False)
        await self._probe(panel)
        self.assertEqual(panel.self_lookups, 2)


if __name__ == "__main__":
    unittest.main()
