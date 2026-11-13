"""/home loads its slow parts at the same time, reuses what it just read, and caches the bot probe."""

from __future__ import annotations

import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx

from app.api import home_pages
from app.services import home_overview
from app.services.pg_read_cache import SYSTEM_SUMMARY_MAX_AGE_SEC

STEP = 0.3  # each slow step; sequential loads would take a multiple of it
EMPTY_OPS = ({}, {}, {})


def _slow(result, log: list | None = None, name: str = ""):
    async def step(*_args, **_kwargs):
        if log is not None:
            log.append((name, "start", time.monotonic()))
        await asyncio.sleep(STEP)
        if log is not None:
            log.append((name, "end", time.monotonic()))
        return result
    return step


class FakeOwnSession:
    """Stands in for SessionLocal(): each use is a distinct session object."""

    opened: list[object] = []

    def __init__(self) -> None:
        self.session = object()

    async def __aenter__(self):
        FakeOwnSession.opened.append(self.session)
        return self.session

    async def __aexit__(self, *_exc) -> None:
        return None


class OwnerDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_overview_ops_and_trend_load_at_the_same_time_and_health_follows_the_overview(self) -> None:
        log: list = []
        with patch.object(home_pages, "_safe_settings", AsyncMock(return_value={})), \
             patch.object(home_pages, "_safe_home_overview", _slow({"bot": {}}, log, "overview")), \
             patch.object(home_pages, "_safe_pg_health", _slow({"ok": True}, log, "health")), \
             patch.object(home_pages, "_parallel_home_ops", _slow(EMPTY_OPS)), \
             patch.object(home_pages, "_safe_trend", _slow({})), \
             patch.object(home_pages, "_visible_action_center", AsyncMock(side_effect=lambda s, st, ac: ac)), \
             patch.object(home_pages, "_staff_wallet_card", AsyncMock(return_value=None)), \
             patch.object(home_pages.pg_link_health, "load_persisted_snapshot", AsyncMock(return_value=None)), \
             patch("app.services.db_safe.recover_session", AsyncMock()):
            started = time.monotonic()
            template, ctx = await home_pages._owner_home_context({"role": "admin"}, object())
            elapsed = time.monotonic() - started
        self.assertEqual(template, "home.html")
        # overview → health is one chain (2 steps); ops and trend run beside it. Sequential was 4 steps.
        self.assertLess(elapsed, STEP * 2.8)
        overview_end = next(t for n, e, t in log if n == "overview" and e == "end")
        health_start = next(t for n, e, t in log if n == "health" and e == "start")
        self.assertGreaterEqual(health_start, overview_end, "health reads the stats the overview cached")
        self.assertEqual(ctx["pg_health"], {"ok": True})
        self.assertIsNone(ctx["pg_limits"])

    async def test_hybrid_owner_reads_pg_limits_in_its_own_session(self) -> None:
        FakeOwnSession.opened = []
        request_session = object()
        seen: list = []

        async def limits(staff, session, *, label):
            seen.append(session)
            return {"ready": True}

        with patch.object(home_pages, "_safe_settings", AsyncMock(return_value={})), \
             patch.object(home_pages, "_safe_home_overview", AsyncMock(return_value={})), \
             patch.object(home_pages, "_safe_pg_health", AsyncMock(return_value={})), \
             patch.object(home_pages, "_parallel_home_ops", AsyncMock(return_value=EMPTY_OPS)), \
             patch.object(home_pages, "_safe_trend", AsyncMock(return_value={})), \
             patch.object(home_pages, "_safe_pg_limits", limits), \
             patch.object(home_pages, "SessionLocal", FakeOwnSession), \
             patch.object(home_pages, "_visible_action_center", AsyncMock(side_effect=lambda s, st, ac: ac)), \
             patch.object(home_pages, "_staff_wallet_card", AsyncMock(return_value=None)), \
             patch.object(home_pages.pg_link_health, "load_persisted_snapshot", AsyncMock(return_value=None)), \
             patch("app.services.db_safe.recover_session", AsyncMock()):
            _t, ctx = await home_pages._owner_home_context({"role": "admin", "pg_is_owner": False}, request_session)
        self.assertEqual(ctx["pg_limits"], {"ready": True})
        self.assertEqual(seen, FakeOwnSession.opened)
        self.assertIsNot(seen[0], request_session)


class ShopDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_bot_pasarguard_ops_and_trend_load_at_the_same_time(self) -> None:
        with patch.object(home_pages, "_shop_profile", AsyncMock(return_value=None)), \
             patch("app.services.resellers.bot_needs_setup", return_value=False), \
             patch.object(home_pages, "_safe_settings", AsyncMock(return_value={})), \
             patch.object(home_pages, "_shop_stats", _slow({"users": 1})), \
             patch.object(home_pages, "_shop_bot_probe", _slow({"ok": True})), \
             patch.object(home_pages, "_shop_pg_state", _slow(({"ready": True}, {"ok": True}))), \
             patch.object(home_pages, "_parallel_home_ops", _slow(EMPTY_OPS)), \
             patch.object(home_pages, "_safe_trend", _slow({})), \
             patch.object(home_pages, "_warn_capacity", AsyncMock()), \
             patch.object(home_pages, "_shop_billing_card", AsyncMock(return_value=None)), \
             patch.object(home_pages, "_visible_action_center", AsyncMock(side_effect=lambda s, st, ac: ac)), \
             patch("app.services.db_safe.recover_session", AsyncMock()):
            started = time.monotonic()
            template, ctx = await home_pages._shop_home_context({"role": "reseller"}, object(), 42)
            elapsed = time.monotonic() - started
        self.assertEqual(template, "reseller_home.html")
        self.assertLess(elapsed, STEP * 2, "five slow steps take about one step, not five")
        self.assertEqual((ctx["pg_limits"], ctx["pg_health"], ctx["stats"]), ({"ready": True}, {"ok": True}, {"users": 1}))

    async def test_shop_pg_limits_and_health_run_together_in_their_own_sessions(self) -> None:
        FakeOwnSession.opened = []
        used: dict = {}

        async def limits(staff, session, *, label):
            used["limits"] = session
            await asyncio.sleep(STEP)
            return {"ready": True}

        async def health(*, reseller_user_id=None, session=None):
            used["health"] = (reseller_user_id, session)
            await asyncio.sleep(STEP)
            return {"ok": True}

        with patch.object(home_pages, "_safe_pg_limits", limits), \
             patch.object(home_pages, "_safe_pg_health", health), \
             patch.object(home_pages, "SessionLocal", FakeOwnSession):
            started = time.monotonic()
            result = await home_pages._shop_pg_state({"pg_admin_username": "shop1"}, 42)
            elapsed = time.monotonic() - started
        self.assertEqual(result, ({"ready": True}, {"ok": True}))
        self.assertLess(elapsed, STEP * 1.8)
        self.assertEqual(used["health"][0], 42)
        self.assertEqual(len(set(map(id, FakeOwnSession.opened))), 2)
        self.assertIsNot(used["limits"], used["health"][1])

    async def test_shop_without_its_own_pasarguard_login_shows_the_platform_reachability(self) -> None:
        health = AsyncMock(return_value={"ok": True})
        with patch.object(home_pages, "_safe_pg_health", health):
            result = await home_pages._shop_pg_state({}, 42)
        self.assertEqual(result, (None, {"ok": True}))
        health.assert_awaited_once_with()


class PgHealthStatsReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_probe_reuses_recent_system_stats(self) -> None:
        probe = AsyncMock(return_value={"ok": True})
        with patch("app.services.ux20.check_pg_connection", probe):
            await home_pages._safe_pg_health()
        self.assertEqual(probe.await_args.kwargs["stats_max_age"], SYSTEM_SUMMARY_MAX_AGE_SEC)

    async def test_live_endpoint_probe_keeps_reading_fresh_stats(self) -> None:
        from app.services import ux20

        class FakePg:
            base_url = "https://panel"

            def __init__(self) -> None:
                self.max_ages: list = []

            async def ensure_token(self) -> None:
                return None

            async def get_system_stats(self, *, cache_max_age=None):
                self.max_ages.append(cache_max_age)
                return {"version": "1.0"}

        pg = FakePg()
        with patch("app.services.pasarguard.get_pg", return_value=pg):
            live = await ux20.check_pg_connection()
            dashboard = await ux20.check_pg_connection(stats_max_age=SYSTEM_SUMMARY_MAX_AGE_SEC)
        self.assertEqual(pg.max_ages, [None, SYSTEM_SUMMARY_MAX_AGE_SEC])
        self.assertTrue(live["ok"] and dashboard["ok"])


class BotProbeCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.requests: list[str] = []
        self.answer = {"ok": True, "result": {"id": 1, "username": "shopbot", "first_name": "Shop"}}
        self.raise_error = False

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            if self.raise_error:
                raise httpx.ConnectError(f"cannot reach {request.url}")
            return httpx.Response(200, json=self.answer)

        home_overview._bot_probe_cache.clear()
        home_overview._telegram_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def asyncTearDown(self) -> None:
        await home_overview._telegram_client.aclose()
        home_overview._telegram_client = None
        home_overview._bot_probe_cache.clear()

    async def test_repeated_dashboard_loads_ask_telegram_once_a_minute(self) -> None:
        first = await home_overview.check_bot_connection("123:AAA")
        second = await home_overview.check_bot_connection("123:AAA")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(first, second)
        self.assertEqual(first["username"], "shopbot")
        later = time.monotonic() + home_overview.BOT_PROBE_TTL_SEC + 1
        with patch.object(home_overview.time, "monotonic", return_value=later):
            await home_overview.check_bot_connection("123:AAA")
        self.assertEqual(len(self.requests), 2)

    async def test_a_changed_token_is_checked_at_once(self) -> None:
        await home_overview.check_bot_connection("123:AAA")
        await home_overview.check_bot_connection("456:BBB")
        self.assertEqual(len(self.requests), 2)

    async def test_a_failing_bot_is_rechecked_sooner_and_its_error_never_carries_the_token(self) -> None:
        self.raise_error = True
        with self.assertLogs("app.services.home_overview", level="WARNING") as logs:
            result = await home_overview.check_bot_connection("123:SECRET")
        self.assertFalse(result["ok"])
        self.assertNotIn("detail", result)
        self.assertNotIn("SECRET", repr(result) + "".join(logs.output))
        soon = time.monotonic() + home_overview.BOT_PROBE_FAILURE_TTL_SEC + 1
        self.assertLess(home_overview.BOT_PROBE_FAILURE_TTL_SEC, home_overview.BOT_PROBE_TTL_SEC)
        with patch.object(home_overview.time, "monotonic", return_value=soon):
            await home_overview.check_bot_connection("123:SECRET")
        self.assertEqual(len(self.requests), 2)

    async def test_callers_get_a_copy_they_can_change(self) -> None:
        first = await home_overview.check_bot_connection("123:AAA")
        first["username"] = "changed"
        again = await home_overview.check_bot_connection("123:AAA")
        self.assertEqual(again["username"], "shopbot")


if __name__ == "__main__":
    unittest.main()
