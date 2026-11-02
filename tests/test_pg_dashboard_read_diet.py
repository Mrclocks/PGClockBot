"""Dashboards ask PasarGuard less often without showing wrong or foreign data."""

from __future__ import annotations

import asyncio
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.api import miniapp_pages
from app.services import pg_read_cache
from app.services.home_overview import pg_home_bundle
from app.services.pasarguard import PasarGuardClient
from app.services.pg_overview import _owned_user_stats


class _Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def monotonic(self) -> float:
        return self.now


class _PanelTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        pg_read_cache._STORE.clear()
        self.clock = _Clock()
        self.enterContext(patch.object(pg_read_cache, "time", SimpleNamespace(monotonic=self.clock.monotonic)))
        self.calls: Counter[str] = Counter()

    async def asyncTearDown(self) -> None:
        pg_read_cache._STORE.clear()

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.calls[request.url.path] += 1
        payloads = {
            "/api/admins/simple": {"admins": [{"username": "a"}, {"username": "b"}]},
            "/api/groups/simple": {"groups": [{"id": 1}]},
            "/api/hosts": [{"id": 1}, {"id": 2}, {"id": 3}],
            "/api/nodes/simple": {"nodes": [{"id": 1, "status": "connected"}]},
            "/api/system": {"total_user": 42, "version": "1.2.3"},
            "/api/users": {"users": [{"username": "u1", "status": "active", "admin": {"username": "shop1"}}]},
        }
        return httpx.Response(200, json=payloads.get(request.url.path, {}))

    async def _client(self, username: str = "owner") -> PasarGuardClient:
        client = PasarGuardClient(username=username, access_token="token")
        await client.close()
        client.base_url = "https://panel.example"
        client._api_base_resolved = True
        client._validated_base, client._validated_until = client.base_url, float("inf")
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(self._answer))
        self.addAsyncCleanup(client.close)
        return client


class ReadCacheMaxAgeTests(_PanelTestCase):
    async def test_default_readers_keep_the_short_lifetime(self) -> None:
        pg_read_cache.cache_put("owner", "/api/hosts", None, [1])
        self.clock.now += 10
        self.assertIsNone(pg_read_cache.cache_get("owner", "/api/hosts"))

    async def test_long_lived_entry_survives_a_short_lived_miss(self) -> None:
        pg_read_cache.cache_put("owner", "/api/hosts", None, [1])
        self.clock.now += 10
        self.assertIsNone(pg_read_cache.cache_get("owner", "/api/hosts"))
        self.assertEqual(pg_read_cache.cache_get("owner", "/api/hosts", max_age=120), [1])

    async def test_max_age_is_capped(self) -> None:
        pg_read_cache.cache_put("owner", "/api/hosts", None, [1])
        self.clock.now += pg_read_cache.MAX_ACCEPTED_AGE_SEC + 1
        self.assertIsNone(pg_read_cache.cache_get("owner", "/api/hosts", max_age=10_000))
        self.assertEqual(pg_read_cache._STORE, {})

    async def test_cached_copy_cannot_be_mutated_by_a_caller(self) -> None:
        pg_read_cache.cache_put("owner", "/api/hosts", None, [{"id": 1}])
        pg_read_cache.cache_get("owner", "/api/hosts", max_age=60)[0]["id"] = 99
        self.assertEqual(pg_read_cache.cache_get("owner", "/api/hosts", max_age=60), [{"id": 1}])

    async def test_mutation_drops_long_lived_copies_of_the_same_client(self) -> None:
        client = await self._client()
        await client.get_hosts(cache_max_age=120)
        await client.request("POST", "/api/host", json={"remark": "new"})
        await client.get_hosts(cache_max_age=120)
        self.assertEqual(self.calls["/api/hosts"], 2)

    async def test_long_lived_copies_never_cross_identities(self) -> None:
        owner = await self._client("owner")
        shop = await self._client("shop1")
        await owner.get_hosts(cache_max_age=120)
        await shop.get_hosts(cache_max_age=120)
        self.assertEqual(self.calls["/api/hosts"], 2)


class HomeBundleTests(_PanelTestCase):
    async def _bundle(self, client: PasarGuardClient) -> dict:
        with patch("app.services.home_overview.get_pg", return_value=client):
            summary, _nodes = await pg_home_bundle()
        return summary

    async def test_repeated_home_loads_reuse_counts_and_version(self) -> None:
        client = await self._client()
        first = await self._bundle(client)
        self.clock.now += 10
        second = await self._bundle(client)
        self.assertEqual(first, second)
        self.assertEqual((second["admins"], second["groups"], second["hosts"]), (2, 1, 3))
        self.assertEqual(second["users"], 42)
        for path in ("/api/admins/simple", "/api/groups/simple", "/api/hosts", "/api/system", "/api/nodes/simple"):
            self.assertEqual(self.calls[path], 1, path)

    async def test_counts_refresh_after_their_max_age(self) -> None:
        client = await self._client()
        await self._bundle(client)
        self.clock.now += pg_read_cache.DASHBOARD_COUNT_MAX_AGE_SEC + 1
        await self._bundle(client)
        for path in ("/api/admins/simple", "/api/groups/simple", "/api/hosts", "/api/system", "/api/nodes/simple"):
            self.assertEqual(self.calls[path], 2, path)

    async def test_node_health_refreshes_sooner_than_counts(self) -> None:
        client = await self._client()
        await self._bundle(client)
        self.clock.now += pg_read_cache.NODE_LIST_MAX_AGE_SEC + 1
        await self._bundle(client)
        self.assertEqual(self.calls["/api/nodes/simple"], 2)
        self.assertEqual(self.calls["/api/hosts"], 1)


class OwnedUserStatsTests(_PanelTestCase):
    async def test_reseller_dashboard_reuses_its_user_sample(self) -> None:
        client = await self._client("shop1")
        first = await _owned_user_stats(client, "shop1")
        self.clock.now += 30
        second = await _owned_user_stats(client, "shop1")
        self.assertEqual(first, second)
        self.assertEqual(second["active"], 1)
        self.assertEqual(self.calls["/api/users"], 1)

    async def test_sample_refreshes_after_its_max_age(self) -> None:
        client = await self._client("shop1")
        await _owned_user_stats(client, "shop1")
        self.clock.now += pg_read_cache.OWNED_USER_STATS_MAX_AGE_SEC + 1
        await _owned_user_stats(client, "shop1")
        self.assertEqual(self.calls["/api/users"], 2)


class MiniAppServiceInfoTests(unittest.IsolatedAsyncioTestCase):
    def _services(self, count: int) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(id=index, subscription_token=f"tok{index}", subscription_url=None, pg_user_id=None)
            for index in range(count)
        ]

    async def test_panel_reads_are_bounded_per_screen(self) -> None:
        in_flight = 0
        peak = 0

        async def fake_fetch(token, **kwargs) -> dict:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {"token": token}

        with patch.object(miniapp_pages, "_fetch_pg_info", side_effect=fake_fetch):
            infos = await miniapp_pages._fetch_service_infos(self._services(20))
        self.assertEqual([info["token"] for info in infos], [f"tok{index}" for index in range(20)])
        self.assertLessEqual(peak, miniapp_pages.MINIAPP_PANEL_CONCURRENCY)

    async def test_slow_services_show_timeout_without_delaying_the_screen(self) -> None:
        async def fake_fetch(token, **kwargs) -> dict:
            if token == "tok1":
                await asyncio.sleep(5)
            return {"token": token}

        loop = asyncio.get_running_loop()
        started = loop.time()
        with patch.object(miniapp_pages, "_fetch_pg_info", side_effect=fake_fetch), patch.object(
            miniapp_pages, "MINIAPP_INFO_DEADLINE_SEC", 0.1
        ):
            infos = await miniapp_pages._fetch_service_infos(self._services(3))
        self.assertLess(loop.time() - started, 1.0)
        self.assertEqual(infos[0], {"token": "tok0"})
        self.assertEqual(infos[1]["error_code"], "timeout")
        self.assertEqual(infos[2], {"token": "tok2"})

    async def test_failed_service_keeps_its_exception_for_the_safe_fallback(self) -> None:
        async def fake_fetch(token, **kwargs) -> dict:
            raise RuntimeError("internal detail")

        with patch.object(miniapp_pages, "_fetch_pg_info", side_effect=fake_fetch):
            infos = await miniapp_pages._fetch_service_infos(self._services(1))
        self.assertIsInstance(infos[0], RuntimeError)


if __name__ == "__main__":
    unittest.main()
