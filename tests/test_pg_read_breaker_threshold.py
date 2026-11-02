"""One slow or failed PasarGuard read must not hide the panel from every shop.

Web reads pause only after an outage is confirmed, the pause grows when
recovery probes keep failing, and automatic polling cannot start a pause.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.api.panel_connection import PanelConnectionMiddleware
from app.config import Settings
from app.services import pg_read_resilience as resilience

BASE = "https://panel.example"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class BreakerThresholdTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        resilience.reset_read_circuits()
        self.clock = _Clock()
        self.enterContext(patch.object(resilience, "time", SimpleNamespace(monotonic=self.clock.monotonic)))
        self.calls: list[str] = []
        self.answers: dict[str, object] = {}
        self.client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(self._answer))

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        resilience.reset_read_circuits()

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        answer = self.answers.get(request.url.path, 200)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(int(answer), json={})

    async def _page_read(self, path: str, *, background: bool = False) -> str | None:
        """One web page doing one read; returns the page's failure label."""
        with resilience.web_read_budget(seconds=4.0, background=background) as budget:
            try:
                await resilience.panel_read_request(self.client, "GET", path)
            except resilience.PgReadUnavailable:
                pass
        return budget.failure

    def _offline(self, path: str = "/down") -> None:
        self.answers[path] = httpx.ConnectError("offline")

    async def test_single_failure_does_not_block_the_next_page(self) -> None:
        self._offline()
        self.assertEqual(await self._page_read("/down"), "unavailable")
        self.assertIsNone(await self._page_read("/fine"))
        self.assertEqual(self.calls, ["/down", "/fine"])

    async def test_confirmed_outage_pauses_reads_for_every_page(self) -> None:
        self._offline()
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        self.assertEqual(await self._page_read("/fine"), "unavailable")
        self.assertNotIn("/fine", self.calls)

    async def test_failures_spread_beyond_the_window_do_not_pause(self) -> None:
        self._offline()
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
            self.clock.now += resilience.FAILURE_WINDOW_SEC + 1
            self.assertIsNone(await self._page_read("/fine"))
        self.assertEqual(self.calls.count("/fine"), resilience.FAILURES_TO_PAUSE)

    async def test_page_stops_reading_after_its_own_outage_signal(self) -> None:
        self._offline()
        with resilience.web_read_budget(seconds=4.0) as budget:
            with self.assertRaises(resilience.PgReadUnavailable):
                await resilience.panel_read_request(self.client, "GET", "/down")
            with self.assertRaises(resilience.PgReadUnavailable):
                await resilience.panel_read_request(self.client, "GET", "/fallback")
        self.assertEqual(self.calls, ["/down"])
        self.assertEqual(budget.failure, "unavailable")

    async def test_endpoint_error_does_not_stop_other_reads_or_pause(self) -> None:
        self.answers["/broken"] = 500
        with resilience.web_read_budget(seconds=4.0) as budget:
            for _ in range(resilience.FAILURES_TO_PAUSE + 1):
                with self.assertRaises(resilience.PgReadUnavailable):
                    await resilience.panel_read_request(self.client, "GET", "/broken")
            response = await resilience.panel_read_request(self.client, "GET", "/fine")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(budget.failure, "unavailable")
        self.assertIsNone(await self._page_read("/fine"))

    async def test_gateway_errors_count_as_outage(self) -> None:
        self.answers["/down"] = 502
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        await self._page_read("/fine")
        self.assertNotIn("/fine", self.calls)

    async def test_failed_recovery_probes_double_the_pause_up_to_the_ceiling(self) -> None:
        self._offline()
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        pauses = []
        for _ in range(6):
            state = resilience._STATES[BASE + "/"]
            pauses.append(state.blocked_until - self.clock.now)
            self.clock.now = state.blocked_until + 0.01
            await self._page_read("/down")
        self.assertEqual(pauses[:5], [5.0, 10.0, 20.0, 40.0, 60.0])
        self.assertAlmostEqual(pauses[5], 60.0, places=1)

    async def test_successful_probe_resets_the_pause_length(self) -> None:
        self._offline()
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        state = resilience._STATES[BASE + "/"]
        self.clock.now = state.blocked_until + 0.01
        self.assertIsNone(await self._page_read("/fine"))
        self.assertEqual(state.pause_count, 0)
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        self.assertAlmostEqual(state.blocked_until - self.clock.now, resilience.READ_COOLDOWN_SEC)

    async def test_straggler_failure_does_not_extend_an_active_pause(self) -> None:
        self._offline()
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        state = resilience._STATES[BASE + "/"]
        paused_until = state.blocked_until
        budget = resilience.ReadBudget(self.clock.now + 4.0)
        resilience._record_outage(state, budget, "timeout")
        self.assertEqual(state.blocked_until, paused_until)
        self.assertEqual(state.pause_count, 1)

    async def test_polling_failures_never_pause_reads(self) -> None:
        self._offline()
        for _ in range(resilience.FAILURES_TO_PAUSE * 2):
            self.assertEqual(await self._page_read("/down", background=True), "unavailable")
        self.assertIsNone(await self._page_read("/fine"))

    async def test_polling_still_honours_an_active_pause(self) -> None:
        self._offline()
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        self.assertEqual(await self._page_read("/fine", background=True), "unavailable")
        self.assertNotIn("/fine", self.calls)


class ReadBudgetSettingTests(unittest.TestCase):
    def _budget(self, raw: object) -> float:
        return Settings(_env_file=None, PG_WEB_READ_BUDGET_SEC=raw).pg_web_read_budget_sec

    def test_default_budget(self) -> None:
        self.assertEqual(Settings(_env_file=None).pg_web_read_budget_sec, 4.0)

    def test_valid_value_is_used(self) -> None:
        self.assertEqual(self._budget("6.5"), 6.5)

    def test_out_of_range_values_are_clamped(self) -> None:
        self.assertEqual(self._budget("0"), 1.0)
        self.assertEqual(self._budget("-3"), 1.0)
        self.assertEqual(self._budget("120"), 15.0)

    def test_invalid_values_fall_back_to_default(self) -> None:
        for raw in ("abc", "nan", "inf", "", True):
            with self.subTest(raw=raw):
                self.assertEqual(self._budget(raw), 4.0)

    def test_web_budget_uses_the_configured_seconds(self) -> None:
        settings = Settings(_env_file=None, PG_WEB_READ_BUDGET_SEC="7")
        with patch.object(resilience, "get_settings", return_value=settings):
            with resilience.web_read_budget() as budget:
                remaining = budget.deadline - resilience.time.monotonic()
        self.assertGreater(remaining, 6.5)
        self.assertLessEqual(remaining, 7.0)


class PollingPathTests(unittest.IsolatedAsyncioTestCase):
    async def _budget_for(self, path: str) -> resilience.ReadBudget:
        seen = {}

        async def app(scope, receive, send) -> None:
            seen["budget"] = scope["state"]["pg_read_budget"]

        middleware = PanelConnectionMiddleware(app)
        await middleware({"type": "http", "path": path}, None, None)
        return seen["budget"]

    async def test_metrics_polling_is_a_background_read(self) -> None:
        self.assertTrue((await self._budget_for("/pg/metrics")).background)

    async def test_pages_are_foreground_reads(self) -> None:
        self.assertFalse((await self._budget_for("/pg")).background)


if __name__ == "__main__":
    unittest.main()
