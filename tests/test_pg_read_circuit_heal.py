"""PasarGuard read circuit recovers under concurrent traffic and page budgets."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.services import pg_link_health as health
from app.services import pg_read_resilience as resilience
from app.services.pasarguard import _mutation_request

BASE = "https://panel.example"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class CircuitHealTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        health.reset()
        resilience.reset_read_circuits()
        self.clock = _Clock()
        self.enterContext(patch.object(resilience, "time", SimpleNamespace(monotonic=self.clock.monotonic)))
        self.calls: list[str] = []
        self.answers: dict[str, object] = {}
        self.client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(self._answer))

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        resilience.reset_read_circuits()
        health.reset()

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        answer = self.answers.get(request.url.path, 200)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(request)
        return httpx.Response(int(answer), json={})

    async def _page_read(self, path: str, *, background: bool = False) -> str | None:
        with resilience.web_read_budget(seconds=4.0, background=background) as budget:
            try:
                await resilience.panel_read_request(self.client, "GET", path)
            except resilience.PgReadUnavailable:
                pass
        return budget.failure

    async def _confirm_outage(self) -> resilience._PanelState:
        self.answers["/down"] = httpx.ConnectError("offline")
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        return resilience._STATES[BASE + "/"]

    async def test_heals_after_pause_despite_concurrent_straggler_failure(self) -> None:
        state = await self._confirm_outage()
        started_gen = state.generation
        # A straggler from before the outage bumps generation after the pause expires.
        self.clock.now = state.blocked_until + 0.01
        budget = resilience.ReadBudget(self.clock.now + 4.0)
        resilience._record_outage(state, budget, "unavailable", is_probe=False)
        self.assertNotEqual(state.generation, started_gen)
        self.assertFalse(resilience.circuit_status()["paused"])
        self.answers["/fine"] = 200
        self.assertIsNone(await self._page_read("/fine"))
        self.assertEqual(state.blocked_until, 0.0)
        self.assertEqual(state.pause_count, 0)

    async def test_probe_lease_expires_so_others_are_not_blocked_forever(self) -> None:
        state = await self._confirm_outage()
        self.clock.now = state.blocked_until + 0.01
        # Claim a probe lease without clearing it (simulates a hung probe).
        with resilience.web_read_budget(seconds=2.0) as budget:
            remaining, _gen, is_probe = resilience._claim_read(state, budget)
        self.assertTrue(is_probe)
        self.assertTrue(resilience.circuit_status()["probing"])
        # While the lease is live, other reads stay rejected.
        self.assertEqual(await self._page_read("/fine"), "unavailable")
        self.assertNotIn("/fine", self.calls)
        # After the lease expires, another read may proceed.
        self.clock.now = state.probe_until + 0.01
        state.in_flight = 0
        self.assertIsNone(await self._page_read("/fine"))
        self.assertIn("/fine", self.calls)

    async def test_budget_exhaustion_does_not_pause_or_bump_generation(self) -> None:
        state = resilience._panel_state(BASE)
        before_gen = state.generation

        async def slow(_request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={})

        self.answers["/slow"] = slow
        # Tiny budget so async_timeout fires as bare TimeoutError.
        with resilience.web_read_budget(seconds=0.001) as budget:
            with self.assertRaises(resilience.PgReadUnavailable):
                await resilience.panel_read_request(self.client, "GET", "/slow")
        self.assertEqual(budget.failure, "timeout")
        self.assertEqual(state.generation, before_gen)
        self.assertEqual(state.blocked_until, 0.0)
        self.assertEqual(health.recent_budget_exhaustions(), 1)
        self.assertEqual(health.recent_failures().get("timeout", 0), 0)
        snap = health.snapshot()
        self.assertEqual(snap["budget_exhaustion_label"], health.BUDGET_EXHAUSTION_LABEL)
        self.assertEqual(snap["budget_exhaustion_count"], 1)

    async def test_straggler_failure_after_expiry_does_not_double_cooldown(self) -> None:
        state = await self._confirm_outage()
        paused_until = state.blocked_until
        pause_count = state.pause_count
        self.clock.now = paused_until + 0.01
        # Straggler (not the recovery probe) fails after expiry.
        budget = resilience.ReadBudget(self.clock.now + 4.0)
        resilience._record_outage(state, budget, "unavailable", is_probe=False)
        # Not enough fresh failures yet to re-pause; must not treat as failed probe.
        self.assertEqual(state.pause_count, pause_count)
        self.assertLessEqual(state.blocked_until, self.clock.now)
        # A real probe failure would double to 10s from now.
        self.assertNotAlmostEqual(state.blocked_until - self.clock.now, 10.0, places=1)

    async def test_genuine_three_failures_still_pause(self) -> None:
        self.answers["/down"] = httpx.ConnectError("offline")
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        self.assertTrue(resilience.circuit_status()["paused"])

    async def test_cooldown_doubles_on_real_probe_failures(self) -> None:
        self.answers["/down"] = httpx.ConnectError("offline")
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

    async def test_one_probe_at_a_time_under_concurrency(self) -> None:
        state = await self._confirm_outage()
        self.clock.now = state.blocked_until + 0.01
        gate = asyncio.Event()
        release = asyncio.Event()

        async def blocked_ok(_request: httpx.Request) -> httpx.Response:
            gate.set()
            await release.wait()
            return httpx.Response(200, json={})

        self.answers["/probe"] = blocked_ok
        self.answers["/other"] = 200

        async def probe() -> None:
            with resilience.web_read_budget(seconds=4.0):
                await resilience.panel_read_request(self.client, "GET", "/probe")

        task = asyncio.create_task(probe())
        await gate.wait()
        self.assertTrue(resilience.circuit_status()["probing"])
        self.assertEqual(await self._page_read("/other"), "unavailable")
        self.assertNotIn("/other", self.calls)
        release.set()
        await task
        self.assertFalse(resilience.circuit_status()["probing"])
        self.assertEqual(state.blocked_until, 0.0)

    async def test_budgetless_success_heals_expired_pause(self) -> None:
        state = await self._confirm_outage()
        self.clock.now = state.blocked_until + 0.01
        self.answers["/job"] = 200
        # No web_read_budget → bot/job path.
        response = await resilience.panel_read_request(self.client, "GET", "/job")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state.blocked_until, 0.0)
        self.assertEqual(state.pause_count, 0)

    async def test_budgetless_failures_do_not_start_a_pause(self) -> None:
        self.answers["/job"] = httpx.ConnectError("offline")
        for _ in range(resilience.FAILURES_TO_PAUSE * 2):
            with self.assertRaises(httpx.ConnectError):
                await resilience.panel_read_request(self.client, "GET", "/job")
        self.assertFalse(resilience.circuit_status()["paused"])

    async def test_mutation_success_heals_expired_pause(self) -> None:
        state = await self._confirm_outage()
        self.clock.now = state.blocked_until + 0.01
        self.answers["/api/user"] = 200
        response = await _mutation_request(self.client, "POST", "/api/user", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state.blocked_until, 0.0)

    async def test_straggler_success_while_paused_does_not_heal(self) -> None:
        state = await self._confirm_outage()
        started_gen = state.generation - 1  # pretend this read started before the outage
        resilience._maybe_mark_healthy(state, started_generation=started_gen)
        self.assertGreater(state.blocked_until, self.clock.now)


if __name__ == "__main__":
    unittest.main()
