"""Per-phase timing of PasarGuard requests: bounded, path-free, and shown on the owner card."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader

from app.services import pg_link_health, pg_link_timing
from app.services.formatting import format_number
from app.services.pasarguard import new_panel_http_client

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"
_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}"


async def _serve_keep_alive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer every request on the same socket until the client closes it."""
    try:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            if not head:
                break
            writer.write(_RESPONSE)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()


class EndpointLabelTests(unittest.TestCase):
    def test_only_the_api_resource_is_kept(self) -> None:
        self.assertEqual(pg_link_timing.endpoint_label("get", "/api/user/alice"), "GET /api/user/…")
        self.assertEqual(pg_link_timing.endpoint_label("POST", "/api/users"), "POST /api/users")
        self.assertEqual(pg_link_timing.endpoint_label("GET", "/dashboard/api/system"), "GET /api/system")

    def test_subscription_paths_and_odd_segments_never_reach_the_label(self) -> None:
        for path in ("/sub/SECRET-TOKEN-123/info", "/api/", "/api/<script>/x", "/api/TOKENabc"):
            with self.subTest(path=path):
                label = pg_link_timing.endpoint_label("GET", path)
                self.assertEqual(label, "GET other")
        self.assertEqual(pg_link_timing.endpoint_label("BREW", "/api/system"), "OTHER /api/system")


class TraceRecordingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        pg_link_timing.reset()

    async def _replay(self, path: str, events: list[str]) -> None:
        request = httpx.Request("GET", f"https://panel.example{path}")
        await pg_link_timing.attach_trace(request)
        trace = request.extensions["trace"]
        for name in events:
            await trace(name, {})

    async def test_a_reused_connection_records_no_connect_or_tls_phase(self) -> None:
        await self._replay("/api/system", [
            "http11.send_request_headers.started",
            "http11.receive_response_headers.complete",
        ])
        snap = pg_link_timing.snapshot()
        self.assertEqual((snap["requests"], snap["new_connections"], snap["new_connection_pct"]), (1, 0, 0))
        self.assertEqual(snap["connect"]["count"], 0)
        self.assertEqual(snap["tls"]["count"], 0)
        self.assertEqual(snap["first_byte"]["count"], 1)

    async def test_a_new_connection_records_connect_and_tls(self) -> None:
        await self._replay("/api/system", [
            "connection.connect_tcp.started",
            "connection.connect_tcp.complete",
            "connection.start_tls.started",
            "connection.start_tls.complete",
            "http11.send_request_headers.started",
            "http11.receive_response_headers.complete",
        ])
        snap = pg_link_timing.snapshot()
        self.assertEqual((snap["new_connections"], snap["new_connection_pct"]), (1, 100))
        self.assertEqual((snap["connect"]["count"], snap["tls"]["count"]), (1, 1))

    async def test_a_request_that_never_got_headers_is_not_a_sample(self) -> None:
        await self._replay("/api/system", ["connection.connect_tcp.started", "connection.connect_tcp.failed"])
        self.assertEqual(pg_link_timing.snapshot()["requests"], 0)

    async def test_samples_are_bounded(self) -> None:
        for _ in range(pg_link_timing.MAX_SAMPLES + 50):
            await self._replay("/api/system", [
                "http11.send_request_headers.started",
                "http11.receive_response_headers.complete",
            ])
        self.assertEqual(pg_link_timing.snapshot()["requests"], pg_link_timing.MAX_SAMPLES)

    async def test_endpoints_are_listed_once_they_have_enough_samples_and_carry_no_path(self) -> None:
        events = ["http11.send_request_headers.started", "http11.receive_response_headers.complete"]
        for name in ("alice", "bob", "carol"):
            await self._replay(f"/api/user/{name}", events)
        await self._replay("/api/rare", events)
        rows = pg_link_timing.snapshot()["endpoints"]
        self.assertEqual([row["endpoint"] for row in rows], ["GET /api/user/…"])
        self.assertEqual(rows[0]["count"], 3)
        self.assertNotIn("alice", repr(pg_link_timing.snapshot()))

    async def test_the_owner_card_snapshot_includes_the_timing(self) -> None:
        pg_link_health.reset()
        self.assertIn("timing", pg_link_health.snapshot())


class TimingCardTests(unittest.IsolatedAsyncioTestCase):
    def _render(self) -> str:
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        env.filters["num"] = format_number
        return env.get_template("_pg_link_health.html").render(pg_link_health=pg_link_health.snapshot())

    async def test_the_card_shows_phases_and_the_slowest_endpoint(self) -> None:
        pg_link_health.reset()
        pg_link_timing.reset()
        for _ in range(3):
            request = httpx.Request("GET", "https://panel.example/api/user/alice")
            await pg_link_timing.attach_trace(request)
            for name in ("connection.connect_tcp.started", "connection.connect_tcp.complete",
                         "http11.send_request_headers.started", "http11.receive_response_headers.complete"):
                await request.extensions["trace"](name, {})
        html = self._render()
        self.assertIn("مراحل هر درخواست", html)
        self.assertIn("اتصال TCP و DNS", html)
        self.assertIn("GET /api/user/…", html)
        self.assertNotIn("alice", html)

    def test_the_timing_section_is_hidden_without_samples(self) -> None:
        pg_link_health.reset()
        pg_link_timing.reset()
        self.assertNotIn("مراحل هر درخواست", self._render())


class PanelClientTimingTests(unittest.IsolatedAsyncioTestCase):
    """The shared panel client feeds the timing through httpx's trace hook (local socket only)."""

    async def asyncSetUp(self) -> None:
        pg_link_timing.reset()
        self.server = await asyncio.start_server(_serve_keep_alive, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.client = new_panel_http_client(f"http://127.0.0.1:{port}")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.server.close()
        await self.server.wait_closed()

    async def test_the_second_request_reuses_the_pooled_connection(self) -> None:
        for _ in range(2):
            response = await self.client.get("/api/system")
            self.assertEqual(response.status_code, 200)
        snap = pg_link_timing.snapshot()
        self.assertEqual(snap["requests"], 2)
        self.assertEqual(snap["new_connections"], 1)
        self.assertEqual(snap["connect"]["count"], 1)
        self.assertEqual(snap["tls"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
