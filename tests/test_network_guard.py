"""Tests may reach this machine and nothing else."""

from __future__ import annotations

import asyncio
import os
import socket
import unittest

import httpx

from tests.network_guard import NetworkAccessDenied, is_local


class NetworkGuardTests(unittest.TestCase):
    def test_a_connection_to_another_host_fails_at_once_and_names_it(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        with self.assertRaises(NetworkAccessDenied) as caught:
            sock.connect(("149.154.167.220", 443))
        self.assertIn("149.154.167.220", str(caught.exception))

    def test_an_http_client_gets_a_connect_error_not_the_internet(self) -> None:
        async def fetch() -> None:
            async with httpx.AsyncClient(timeout=2) as client:
                await client.get("https://api.telegram.org/bot123:abc/getMe")

        with self.assertRaises(httpx.ConnectError):
            asyncio.run(fetch())

    def test_loopback_still_works(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()

    def test_which_addresses_count_as_local(self) -> None:
        for address in (("127.0.0.1", 5432), ("::1", 80, 0, 0), ("localhost", 80), ("0.0.0.0", 80), "/tmp/pg.sock"):
            with self.subTest(address=address):
                self.assertTrue(is_local(address))
        for address in (("8.8.8.8", 53), ("2001:4860:4860::8888", 53, 0, 0), ("example.com", 443)):
            with self.subTest(address=address):
                self.assertFalse(is_local(address))

    def test_no_proxy_can_carry_traffic_out(self) -> None:
        self.assertFalse({"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"} & set(os.environ))


if __name__ == "__main__":
    unittest.main()
