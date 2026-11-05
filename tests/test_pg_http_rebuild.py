"""Rebuild the PasarGuard HTTP client after repeated transport failures."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.services import pg_http_rebuild as rebuild
from app.services.pasarguard import PasarGuardClient


class _FakeHolder:
    def __init__(self, base_url: str = "https://panel.example") -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url)


class RebuildPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = SimpleNamespace(now=1000.0)
        self.enterContext(
            patch.object(rebuild, "time", SimpleNamespace(monotonic=lambda: self.clock.now))
        )
        self.state = rebuild.HttpRebuildState()
        self.holder = _FakeHolder()
        self.retired: list[httpx.AsyncClient] = []
        self.enterContext(
            patch.object(
                rebuild,
                "rebuild_holder_http_client",
                side_effect=self._rebuild,
            )
        )

    def _rebuild(self, holder, state, *, kind: str) -> bool:
        old = holder._client
        holder._client = httpx.AsyncClient(base_url=holder.base_url)
        state.last_rebuild_at = self.clock.now
        state.rebuild_count += 1
        state.failures.clear()
        self.retired.append(old)
        return True

    def test_n_consecutive_connect_errors_trigger_one_rebuild(self) -> None:
        rebuilt = 0
        for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD):
            rebuilt += int(
                rebuild.note_http_transport_failure(
                    self.holder, self.state, httpx.ConnectError("down"), kind="connect"
                )
            )
            self.clock.now += 1.0
        self.assertEqual(rebuilt, 1)
        self.assertEqual(self.state.rebuild_count, 1)

    def test_read_errors_also_count(self) -> None:
        for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD - 1):
            self.assertFalse(
                rebuild.note_http_transport_failure(
                    self.holder, self.state, httpx.ReadError("rst"), kind="dropped"
                )
            )
            self.clock.now += 1.0
        self.assertTrue(
            rebuild.note_http_transport_failure(
                self.holder, self.state, httpx.ReadError("rst"), kind="dropped"
            )
        )

    def test_success_resets_the_counter(self) -> None:
        for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD - 1):
            rebuild.note_http_transport_failure(
                self.holder, self.state, httpx.ConnectError("down"), kind="connect"
            )
            self.clock.now += 1.0
        rebuild.note_http_success(self.state)
        self.assertFalse(
            rebuild.note_http_transport_failure(
                self.holder, self.state, httpx.ConnectError("down"), kind="connect"
            )
        )
        self.assertEqual(self.state.rebuild_count, 0)

    def test_rebuild_is_rate_limited(self) -> None:
        for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD):
            rebuild.note_http_transport_failure(
                self.holder, self.state, httpx.ConnectError("down"), kind="connect"
            )
            self.clock.now += 1.0
        self.assertEqual(self.state.rebuild_count, 1)
        for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD):
            rebuild.note_http_transport_failure(
                self.holder, self.state, httpx.ConnectError("down"), kind="connect"
            )
            self.clock.now += 1.0
        self.assertEqual(self.state.rebuild_count, 1)
        self.clock.now += rebuild.CLIENT_REBUILD_MIN_INTERVAL_SEC + 0.1
        for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD):
            rebuild.note_http_transport_failure(
                self.holder, self.state, httpx.ConnectError("down"), kind="connect"
            )
            self.clock.now += 1.0
        self.assertEqual(self.state.rebuild_count, 2)

    def test_budget_timeout_does_not_count(self) -> None:
        for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD + 2):
            self.assertFalse(
                rebuild.note_http_transport_failure(
                    self.holder, self.state, TimeoutError(), kind="timeout"
                )
            )
        self.assertEqual(self.state.rebuild_count, 0)

    def test_httpx_timeout_does_count(self) -> None:
        for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD - 1):
            rebuild.note_http_transport_failure(
                self.holder, self.state, httpx.ReadTimeout("slow"), kind="timeout"
            )
            self.clock.now += 1.0
        self.assertTrue(
            rebuild.note_http_transport_failure(
                self.holder, self.state, httpx.ReadTimeout("slow"), kind="timeout"
            )
        )


class RebuildSwapTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_client_is_retired_not_closed_immediately(self) -> None:
        holder = _FakeHolder()
        old = holder._client
        state = rebuild.HttpRebuildState()
        scheduled: list = []

        def capture_retire(client: httpx.AsyncClient) -> None:
            scheduled.append(client)

        with patch("app.services.pasarguard._retire_http_client", side_effect=capture_retire):
            rebuilt = rebuild.rebuild_holder_http_client(holder, state, kind="connect")
        self.assertTrue(rebuilt)
        self.assertIsNot(holder._client, old)
        self.assertEqual(scheduled, [old])
        self.assertFalse(old.is_closed)
        await old.aclose()
        await holder._client.aclose()

    async def test_client_request_path_rebuilds_after_transport_failures(self) -> None:
        answers: list[object] = [httpx.ConnectError("down")] * rebuild.TRANSPORT_FAILURES_TO_REBUILD

        def handler(request: httpx.Request) -> httpx.Response:
            item = answers.pop(0) if answers else 200
            if isinstance(item, Exception):
                raise item
            return httpx.Response(int(item), json={})

        with (
            patch("app.services.pasarguard.get_settings") as gs,
            patch("app.services.pasarguard.assert_safe_pg_base_url", side_effect=lambda u, **k: u),
            patch("app.services.pasarguard.validate_panel_base", side_effect=lambda u, **k: u),
        ):
            gs.return_value = SimpleNamespace(
                pg_base_url="https://panel.example",
                pg_username="u",
                pg_password="p",
                pg_access_token=None,
                pg_api_key=None,
                pg_subscription_path="/sub",
            )
            client = PasarGuardClient(api_key="test-key")
            client._client = httpx.AsyncClient(
                base_url="https://panel.example",
                transport=httpx.MockTransport(handler),
            )
            old = client._client
            retired: list = []
            with patch("app.services.pasarguard._retire_http_client", side_effect=retired.append):
                for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD):
                    with self.assertRaises(httpx.ConnectError):
                        await client.request("GET", "/api/system", use_cache=False, auth=False)
            self.assertEqual(client._http_rebuild.rebuild_count, 1)
            self.assertIsNot(client._client, old)
            self.assertEqual(retired, [old])
            await old.aclose()
            await client._client.aclose()

    async def test_5xx_answers_do_not_trigger_rebuild(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, json={"detail": "bad gateway"})

        with (
            patch("app.services.pasarguard.get_settings") as gs,
            patch("app.services.pasarguard.assert_safe_pg_base_url", side_effect=lambda u, **k: u),
            patch("app.services.pasarguard.validate_panel_base", side_effect=lambda u, **k: u),
        ):
            gs.return_value = SimpleNamespace(
                pg_base_url="https://panel.example",
                pg_username="u",
                pg_password="p",
                pg_access_token=None,
                pg_api_key=None,
                pg_subscription_path="/sub",
            )
            client = PasarGuardClient(api_key="test-key")
            client._client = httpx.AsyncClient(
                base_url="https://panel.example",
                transport=httpx.MockTransport(handler),
            )
            for _ in range(rebuild.TRANSPORT_FAILURES_TO_REBUILD + 2):
                with self.assertRaises(Exception):
                    await client.request("GET", "/api/system", use_cache=False, auth=False)
            self.assertEqual(client._http_rebuild.rebuild_count, 0)
            await client._client.aclose()


if __name__ == "__main__":
    unittest.main()
