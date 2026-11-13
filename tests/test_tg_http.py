"""Tests for the proxy-aware httpx client helper in app.services.tg_http.

``tg_client`` builds an ``httpx.AsyncClient`` for direct Telegram Bot API calls.
When ``TG_PROXY`` is set the client must be bound to that proxy (http, socks4,
socks5 or socks5h).  These tests assert the proxy argument is forwarded to
``httpx.AsyncClient`` by mocking the constructor, so they do not require a
live proxy or the optional ``socks`` extra (``socksio``).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from app.services.tg_http import tg_client


class TgClientProxyTests(unittest.TestCase):
    """``tg_client`` wires the configured proxy into ``httpx.AsyncClient``."""

    def _constructor_kwargs(self, tg_proxy: str, **client_kwargs) -> dict:
        with patch("app.services.tg_http.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = tg_proxy
            with patch.object(httpx, "AsyncClient") as client_cls:
                tg_client(**client_kwargs)
                return client_cls.call_args.kwargs

    def test_no_proxy_omits_proxy_arg(self):
        self.assertNotIn("proxy", self._constructor_kwargs(""))

    def test_blank_proxy_omits_proxy_arg(self):
        self.assertNotIn("proxy", self._constructor_kwargs("   "))

    def test_http_proxy_is_forwarded(self):
        self.assertEqual(
            self._constructor_kwargs("http://127.0.0.1:10809")["proxy"],
            "http://127.0.0.1:10809",
        )

    def test_https_proxy_is_forwarded(self):
        self.assertEqual(
            self._constructor_kwargs("https://user:pass@host:8080")["proxy"],
            "https://user:pass@host:8080",
        )

    def test_socks_proxy_is_forwarded(self):
        # httpx routes a socks URL through the optional socks extra at request
        # time; tg_client only needs to forward the URL verbatim.
        self.assertEqual(
            self._constructor_kwargs("socks5://host:1080")["proxy"],
            "socks5://host:1080",
        )

    def test_socks_proxy_is_stripped(self):
        self.assertEqual(
            self._constructor_kwargs("  socks5h://host:1080  ")["proxy"],
            "socks5h://host:1080",
        )

    def test_timeout_forwarded_to_client(self):
        kwargs = self._constructor_kwargs("", timeout=3.5)
        self.assertEqual(kwargs["timeout"], 3.5)


if __name__ == "__main__":
    unittest.main()
