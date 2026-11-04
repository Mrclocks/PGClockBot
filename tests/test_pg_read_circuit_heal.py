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

    async def test_unresponsive_panel_pauses_circuit_under_default_budget(self) -> None:
        """A panel that never answers must pause via timeout, not budget exhaustion.

        Production budgets (~4s) fire before httpx's 30s timeout; the old code
        always treated async_timeout's TimeoutError as budget exhaustion so the
        circuit never paused. Scale both the budget and MIN_OUTAGE_WAIT_SEC so
        the test stays fast while preserving the classification rule.
        """
        hang = asyncio.Event()

        async def never(_request: httpx.Request) -> httpx.Response:
            await hang.wait()
            return httpx.Response(200, json={})

        self.answers["/hang"] = never
        budget_sec = 0.5
        min_outage = 0.2
        with patch.object(resilience, "MIN_OUTAGE_WAIT_SEC", min_outage):
            for _ in range(resilience.FAILURES_TO_PAUSE):
                with resilience.web_read_budget(seconds=budget_sec):
                    with self.assertRaises(resilience.PgReadUnavailable):
                        await resilience.panel_read_request(self.client, "GET", "/hang")
        status = resilience.circuit_status()
        self.assertTrue(status["paused"])
        self.assertEqual(status["pause_count"], 1)
        self.assertEqual(health.recent_failures().get("timeout", 0), resilience.FAILURES_TO_PAUSE)
        self.assertEqual(health.recent_budget_exhaustions(), 0)

    async def test_budget_exhausted_by_earlier_reads_does_not_pause(self) -> None:
        """When earlier reads leave < MIN_OUTAGE_WAIT_SEC, classify as exhaustion."""
        hang = asyncio.Event()

        async def never(_request: httpx.Request) -> httpx.Response:
            await hang.wait()
            return httpx.Response(200, json={})

        self.answers["/late"] = never
        state = resilience._panel_state(BASE)
        before_gen = state.generation
        min_outage = 0.3
        with patch.object(resilience, "MIN_OUTAGE_WAIT_SEC", min_outage):
            with resilience.web_read_budget(seconds=1.0) as budget:
                # Earlier work on this page already spent the shared deadline;
                # claimed remaining for this read is below the outage threshold.
                budget.deadline = self.clock.now + 0.05
                with self.assertRaises(resilience.PgReadUnavailable):
                    await resilience.panel_read_request(self.client, "GET", "/late")
        self.assertEqual(budget.failure, "timeout")
        self.assertFalse(resilience.circuit_status()["paused"])
        self.assertEqual(state.generation, before_gen)
        self.assertEqual(health.recent_budget_exhaustions(), 1)
        self.assertEqual(health.recent_failures().get("timeout", 0), 0)

    async def test_probe_timeout_doubles_cooldown_only_for_fair_probe(self) -> None:
        hang = asyncio.Event()

        async def never(_request: httpx.Request) -> httpx.Response:
            await hang.wait()
            return httpx.Response(200, json={})

        self.answers["/down"] = httpx.ConnectError("offline")
        self.answers["/probe"] = never
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._page_read("/down")
        state = resilience._STATES[BASE + "/"]
        first_pause_until = state.blocked_until
        pause_count = state.pause_count
        self.clock.now = first_pause_until + 0.01

        # Fair probe: claimed remaining >= MIN_OUTAGE_WAIT_SEC → doubles cooldown.
        with patch.object(resilience, "MIN_OUTAGE_WAIT_SEC", 0.05):
            with resilience.web_read_budget(seconds=0.2):
                with self.assertRaises(resilience.PgReadUnavailable):
                    await resilience.panel_read_request(self.client, "GET", "/probe")
        self.assertEqual(state.pause_count, pause_count + 1)
        self.assertGreater(state.blocked_until, self.clock.now)

        # Unfair probe: tiny remaining → exhaustion only; pause state otherwise unchanged.
        self.clock.now = state.blocked_until + 0.01
        pause_count = state.pause_count
        blocked_at = state.blocked_until
        gen = state.generation
        with patch.object(resilience, "MIN_OUTAGE_WAIT_SEC", 0.5):
            with resilience.web_read_budget(seconds=0.05):
                with self.assertRaises(resilience.PgReadUnavailable):
                    await resilience.panel_read_request(self.client, "GET", "/probe")
        self.assertEqual(state.pause_count, pause_count)
        self.assertEqual(state.blocked_until, blocked_at)
        self.assertEqual(state.generation, gen)
        self.assertGreaterEqual(health.recent_budget_exhaustions(), 1)

    async def test_straggler_does_not_clear_live_probe_lease(self) -> None:
        """A non-probe finishing must not clear a live probe lease."""
        release_straggler = asyncio.Event()
        release_probe = asyncio.Event()
        straggler_entered = asyncio.Event()
        probe_entered = asyncio.Event()

        async def straggler_hang(_request: httpx.Request) -> httpx.Response:
            straggler_entered.set()
            await release_straggler.wait()
            raise httpx.ConnectError("straggler dropped")

        async def probe_hang(_request: httpx.Request) -> httpx.Response:
            probe_entered.set()
            await release_probe.wait()
            return httpx.Response(200, json={})

        self.answers["/straggler"] = straggler_hang
        self.answers["/down"] = httpx.ConnectError("offline")
        self.answers["/probe"] = probe_hang
        self.answers["/other"] = 200

        with (
            patch.object(resilience, "READ_COOLDOWN_SEC", 0.05),
            patch.object(resilience, "MAX_READ_COOLDOWN_SEC", 0.05),
        ):
            async def run_straggler() -> None:
                with resilience.web_read_budget(seconds=4.0):
                    try:
                        await resilience.panel_read_request(self.client, "GET", "/straggler")
                    except resilience.PgReadUnavailable:
                        pass

            straggler_task = asyncio.create_task(run_straggler())
            await straggler_entered.wait()

            for _ in range(resilience.FAILURES_TO_PAUSE):
                await self._page_read("/down")
            self.assertTrue(resilience.circuit_status()["paused"])
            state = resilience._STATES[BASE + "/"]
            # Let the short cooldown expire (real time; clock patch is separate).
            await asyncio.sleep(0.06)
            self.clock.now = state.blocked_until + 0.01

            async def run_probe() -> None:
                with resilience.web_read_budget(seconds=4.0):
                    try:
                        await resilience.panel_read_request(self.client, "GET", "/probe")
                    except resilience.PgReadUnavailable:
                        pass

            probe_task = asyncio.create_task(run_probe())
            await probe_entered.wait()
            self.assertTrue(resilience.circuit_status()["probing"])
            lease_until = state.probe_until
            self.assertGreater(lease_until, 0.0)

            # Straggler finishes as a non-probe; must leave the lease intact.
            release_straggler.set()
            await straggler_task
            self.assertEqual(state.probe_until, lease_until)
            self.assertTrue(state.probing)
            self.assertEqual(await self._page_read("/other"), "unavailable")
            self.assertNotIn("/other", self.calls)

            release_probe.set()
            await probe_task


if __name__ == "__main__":
    unittest.main()
