"""The owner can see how the PasarGuard link behaves: failures by cause, response times, circuit."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from jinja2 import Environment, FileSystemLoader

from app.services import pg_link_health as health
from app.services import pg_read_resilience as resilience

BASE = "https://panel.example"
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"


class FailureKindTests(unittest.TestCase):
    def test_causes_are_told_apart(self) -> None:
        cases = {
            "timeout": httpx.ReadTimeout("slow"),
            "dns": httpx.ConnectError("[Errno -2] Name or service not known"),
            "connect": httpx.ConnectError("[Errno 111] Connection refused"),
            "dropped": httpx.RemoteProtocolError("Server disconnected"),
        }
        for kind, exc in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(health.failure_kind(exc), kind)
        self.assertEqual(health.failure_kind(TimeoutError()), "timeout")
        self.assertEqual(health.failure_kind(status_code=503), "gateway")
        self.assertEqual(health.failure_kind(status_code=500), "server_error")


class ReadPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        health.reset()
        resilience.reset_read_circuits()
        self.answer: object = 200

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if isinstance(self.answer, Exception):
            raise self.answer
        return httpx.Response(int(self.answer), json={})

    async def _read(self, path: str = "/api/system") -> None:
        async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(self._handler)) as client:
            with resilience.web_read_budget(seconds=4.0):
                try:
                    await resilience.panel_read_request(client, "GET", path)
                except resilience.PgReadUnavailable:
                    pass

    async def test_web_reads_are_counted_by_cause(self) -> None:
        await self._read()
        for answer in (httpx.ConnectError("Connection refused"), 502, 500):
            self.answer = answer
            await self._read()
        snap = health.snapshot(resilience.circuit_status())
        counts = {row["kind"]: row["count"] for row in snap["recent_failures"]}
        self.assertEqual(counts, {"connect": 1, "gateway": 1, "server_error": 1})
        self.assertEqual(snap["reads"], 4)
        self.assertIsNotNone(snap["latency_p50_ms"])
        self.assertIsNotNone(snap["last_ok_at"])
        self.assertEqual(snap["last_failure"], health.KIND_LABELS["server_error"])

    async def test_paths_with_customer_tokens_are_never_kept(self) -> None:
        self.answer = httpx.ConnectError("Connection refused to https://panel.example/sub/SECRET-TOKEN/info")
        await self._read("/sub/SECRET-TOKEN/info")
        self.assertNotIn("SECRET-TOKEN", json.dumps(health.snapshot()))

    async def test_circuit_state_is_reported(self) -> None:
        self.answer = httpx.ConnectError("Connection refused")
        for _ in range(resilience.FAILURES_TO_PAUSE):
            await self._read()
        circuit = resilience.circuit_status()
        self.assertTrue(circuit["paused"])
        self.assertGreater(circuit["retry_in_sec"], 0)
        self.assertEqual(circuit["pause_count"], 1)
        self.assertIn("connect:3", health.summary_text())


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset()

    def test_old_events_leave_the_hour_but_stay_in_totals(self) -> None:
        clock = SimpleNamespace(now=1_000_000.0)
        with patch.object(health, "time", SimpleNamespace(time=lambda: clock.now)):
            health.record_failure("timeout")
            clock.now += health.WINDOW_SEC + 1
            health.record_failure("gateway")
            snap = health.snapshot()
        rows = {row["kind"]: row for row in snap["recent_failures"]}
        self.assertEqual(rows["timeout"]["count"], 0)
        self.assertEqual(rows["timeout"]["total"], 1)
        self.assertEqual(rows["gateway"]["count"], 1)

    def test_percentiles_of_recent_response_times(self) -> None:
        for ms in range(1, 101):
            health.record_success(ms / 1000)
        snap = health.snapshot()
        self.assertEqual(snap["latency_p50_ms"], 50)
        self.assertEqual(snap["latency_p95_ms"], 95)

    def test_unknown_kinds_are_stored_as_dropped(self) -> None:
        health.record_failure("weird")
        self.assertEqual(health.recent_failures()["dropped"], 1)


class CardTests(unittest.TestCase):
    def _render(self, snap: dict) -> str:
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        return env.get_template("_pg_link_health.html").render(pg_link_health=snap)

    def test_card_shows_causes_and_paused_circuit(self) -> None:
        health.reset()
        health.record_success(0.12)
        health.record_failure("timeout")
        html = self._render(health.snapshot({"paused": True, "retry_in_sec": 20, "pause_count": 2}))
        self.assertIn("سلامت ارتباط با پاسارگارد", html)
        self.assertIn(health.KIND_LABELS["timeout"], html)
        self.assertIn(">20</bdi> ثانیه", html)
        self.assertIn("120 ms", html)

    def test_times_are_shown_in_tehran_time(self) -> None:
        health.reset()
        moment = 1_800_000_000.0  # 2027-01-15 08:00:00 UTC
        with patch.object(health, "time", SimpleNamespace(time=lambda: moment)):
            health.record_success(0.05)
            health.record_failure("gateway")
            snap = health.snapshot()
        self.assertEqual(snap["last_ok_time"], "11:30:00")
        html = self._render(snap)
        self.assertIn('<bdi dir="ltr">11:30:00</bdi> (تهران)', html)
        self.assertNotIn("UTC", html)

    def test_status_badge_sits_in_its_own_header_row(self) -> None:
        health.reset()
        health.record_success(0.05)
        html = self._render(health.snapshot())
        self.assertIn('<div class="link-health-head">', html)
        self.assertNotIn('class="stat-top"', html)

    def test_empty_card_says_there_is_no_data_yet(self) -> None:
        health.reset()
        self.assertIn("هنوز داده‌ای نیست", self._render(health.snapshot()))


if __name__ == "__main__":
    unittest.main()
