"""The shared Telegram getMe client is reused within one event loop and never across loops."""

from __future__ import annotations

import asyncio
import unittest

import httpx

from app.services import home_overview


async def _client() -> httpx.AsyncClient:
    return home_overview._telegram_http()


class BotProbeClientTests(unittest.TestCase):
    def setUp(self) -> None:
        home_overview._telegram_client = None
        home_overview._opened_client = None
        self.addCleanup(setattr, home_overview, "_telegram_client", None)
        self.addCleanup(setattr, home_overview, "_opened_client", None)

    def test_probes_in_one_loop_share_a_client(self) -> None:
        async def twice() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
            return await _client(), await _client()

        first, second = asyncio.run(twice())
        self.assertIs(first, second)

    def test_a_probe_from_another_loop_gets_its_own_client(self) -> None:
        first = asyncio.run(_client())
        second = asyncio.run(_client())
        self.assertIsNot(first, second, "connections of a closed loop must not be reused")

    def test_a_client_set_from_outside_is_used_even_after_another_loop_probed(self) -> None:
        asyncio.run(_client())  # an earlier probe opened a client on a loop that is now gone
        mock = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        home_overview._telegram_client = mock
        self.assertIs(asyncio.run(_client()), mock)


if __name__ == "__main__":
    unittest.main()
