"""Capability probe cache: ~3 min TTL, per identity, fail closed, invalidation."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import pg_read_cache as cache
from app.services.pg_access import (
    clear_platform_pg_capability_cache,
    resolve_platform_pg_capabilities,
    resolve_reseller_pg_features,
)


class CapabilityProbeCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        cache.clear_store()
        clear_platform_pg_capability_cache()
        self.admin = {
            "username": "owner",
            "role": {
                "id": 1,
                "is_owner": True,
                "permissions": {},
            },
        }
        self.calls = {"admin": 0}

        async def current_admin():
            self.calls["admin"] += 1
            return dict(self.admin)

        self.client = AsyncMock()
        self.client.ensure_token = AsyncMock(return_value="tok")
        self.client.get_current_admin = AsyncMock(side_effect=current_admin)
        self.client.get_admin = AsyncMock(return_value=None)
        self.client.base_url = "https://pg.example"
        self.client._login_username = "owner"
        self.client._read_cache_ident = lambda: "owner@https://pg.example:0"
        self.settings = MagicMock(
            pg_username="owner",
            pg_password="secret",
            pg_api_key=None,
            pg_base_url="https://pg.example",
        )

    async def asyncTearDown(self) -> None:
        cache.clear_store()
        clear_platform_pg_capability_cache()

    async def test_second_call_within_ttl_skips_panel_request(self) -> None:
        with patch("app.services.pasarguard.get_pg", return_value=self.client), \
             patch("app.config.get_settings", return_value=self.settings):
            first = await resolve_platform_pg_capabilities(use_cache=True)
            second = await resolve_platform_pg_capabilities(use_cache=True)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(self.calls["admin"], 1)
        self.assertEqual(first["features"], second["features"])

    async def test_failure_is_fail_closed_and_not_cached(self) -> None:
        self.client.get_current_admin = AsyncMock(side_effect=RuntimeError("down"))
        self.client.get_admin = AsyncMock(side_effect=RuntimeError("down"))
        with patch("app.services.pasarguard.get_pg", return_value=self.client), \
             patch("app.config.get_settings", return_value=self.settings):
            failed = await resolve_platform_pg_capabilities(use_cache=True)
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["features"], [])
            # Recover: next call must probe again (failure was not cached).
            self.client.get_current_admin = AsyncMock(return_value=dict(self.admin))
            self.client.get_admin = AsyncMock(return_value=None)
            recovered = await resolve_platform_pg_capabilities(use_cache=True)
        self.assertTrue(recovered["ok"])

    async def test_shops_do_not_share_role_feature_cache(self) -> None:
        role = {
            "id": 7,
            "name": "ops",
            "is_owner": False,
            "permissions": {"users": {"read": True}},
        }
        client_a = AsyncMock()
        client_a.base_url = "https://pg.example"
        client_a._login_username = "shop_a"
        client_a._read_cache_ident = lambda: "u:shop_a@https://pg.example:1"
        client_a.get_admin_role = AsyncMock(return_value=role)
        client_b = AsyncMock()
        client_b.base_url = "https://pg.example"
        client_b._login_username = "shop_b"
        client_b._read_cache_ident = lambda: "u:shop_b@https://pg.example:2"
        client_b.get_admin_role = AsyncMock(side_effect=RuntimeError("isolated"))
        client_b.get_admin_roles = AsyncMock(side_effect=RuntimeError("isolated"))

        feats_a, _ = await resolve_reseller_pg_features(7, client=client_a)
        feats_a2, _ = await resolve_reseller_pg_features(7, client=client_a)
        feats_b, role_b = await resolve_reseller_pg_features(7, client=client_b)
        self.assertIn("pg_users", feats_a)
        self.assertEqual(feats_a, feats_a2)
        self.assertEqual(client_a.get_admin_role.await_count, 1)
        self.assertEqual(feats_b, [])
        self.assertIsNone(role_b)

    async def test_reset_pg_and_clear_invalidate_cache(self) -> None:
        with patch("app.services.pasarguard.get_pg", return_value=self.client), \
             patch("app.config.get_settings", return_value=self.settings):
            await resolve_platform_pg_capabilities(use_cache=True)
            self.assertEqual(self.calls["admin"], 1)
            clear_platform_pg_capability_cache()
            await resolve_platform_pg_capabilities(use_cache=True)
        self.assertEqual(self.calls["admin"], 2)

    async def test_ttl_constant_is_about_three_minutes(self) -> None:
        self.assertEqual(cache.CAPABILITY_PROBE_MAX_AGE_SEC, 180.0)


if __name__ == "__main__":
    unittest.main()
