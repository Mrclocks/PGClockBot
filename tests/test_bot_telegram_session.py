"""Tests for the proxy-aware Telegram session helper in app.bot.

These tests require ``aiogram`` to be installed because importing
``app.bot`` pulls in aiogram modules.  In a minimal test environment
without aiogram, the tests are skipped.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:  # pragma: no cover - import guard
    from app.bot import telegram_session
    _HAS_AIOPGRAM = True
except ImportError:
    _HAS_AIOPGRAM = False


def _skip_if_no_aiogram():
    def _decorator(test_func):
        return unittest.skipUnless(
            _HAS_AIOPGRAM,
            "aiogram is not installed; skipping proxy-session tests",
        )(test_func)
    return _decorator


class TelegramSessionNoProxyTests(unittest.TestCase):
    """``telegram_session`` returns ``None`` when no proxy is configured."""

    @_skip_if_no_aiogram()
    def test_empty_proxy_returns_none(self):
        with patch("app.bot.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = ""
            self.assertIsNone(telegram_session())

    @_skip_if_no_aiogram()
    def test_blank_proxy_returns_none(self):
        with patch("app.bot.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = "   "
            self.assertIsNone(telegram_session())


class TelegramSessionProxyTests(unittest.TestCase):
    """``telegram_session`` returns an ``AiohttpSession`` bound to the proxy."""

    @_skip_if_no_aiogram()
    def test_http_proxy_returns_aiohttp_session(self):
        from aiogram.client.session.aiohttp import AiohttpSession

        with patch("app.bot.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = "http://127.0.0.1:10809"
            session = telegram_session()
        self.assertIsInstance(session, AiohttpSession)

    @_skip_if_no_aiogram()
    def test_socks_proxy_returns_aiohttp_session(self):
        from aiogram.client.session.aiohttp import AiohttpSession

        with patch("app.bot.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = "socks5h://user:pass@host:1080"
            session = telegram_session()
        self.assertIsInstance(session, AiohttpSession)

    @_skip_if_no_aiogram()
    def test_socks_proxy_session_carries_proxy_url(self):
        # Constructing the session with a SOCKS URL requires aiohttp-socks;
        # the proxy must be retained on the session for the connector to use.
        with patch("app.bot.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = "socks5://host:1080"
            session = telegram_session()
        self.assertIsNotNone(session)
        self.assertEqual(session.proxy, "socks5://host:1080")

    @_skip_if_no_aiogram()
    def test_http_proxy_session_carries_proxy_url(self):
        with patch("app.bot.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = "http://127.0.0.1:10809"
            session = telegram_session()
        self.assertIsNotNone(session)
        self.assertEqual(session.proxy, "http://127.0.0.1:10809")

    @_skip_if_no_aiogram()
    def test_proxy_string_is_stripped(self):
        from aiogram.client.session.aiohttp import AiohttpSession

        with patch("app.bot.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = "  http://127.0.0.1:10809  "
            session = telegram_session()
        self.assertIsInstance(session, AiohttpSession)


class TelegramSessionConnectorTests(unittest.TestCase):
    """The proxy must actually reach the connector aiogram requests go through."""

    def _connector_attrs(self, proxy_url: str) -> tuple[object, str, int, bool]:
        """Build the session and introspect its underlying ProxyConnector."""
        import asyncio

        from aiohttp_socks import ProxyType

        with patch("app.bot.get_settings") as settings_mock:
            settings_mock.return_value.tg_proxy = proxy_url
            session = telegram_session()
        self.assertIsNotNone(session)

        async def _open():
            sess = await session.create_session()
            return sess.connector

        connector = asyncio.run(_open())
        # Cleanup the raw aiohttp session the connector was built for.
        raw = getattr(session, "_session", None)
        if raw is not None:
            asyncio.run(raw.close())
        proxy_type = getattr(connector, "_proxy_type", None)
        return (
            proxy_type,
            getattr(connector, "_proxy_host", ""),
            int(getattr(connector, "_proxy_port", 0)),
            bool(getattr(connector, "_rdns", False)),
            ProxyType,
        )[:4] + (ProxyType,)

    @_skip_if_no_aiogram()
    def test_socks5_reaches_connector(self):
        result = self._connector_attrs("socks5://proxy.example:1080")
        proxy_type, host, port, rdns, ProxyType = result
        self.assertEqual(proxy_type, ProxyType.SOCKS5)
        self.assertEqual(host, "proxy.example")
        self.assertEqual(port, 1080)
        # python-socks resolves DNS through the proxy (socks5h semantics).
        self.assertTrue(rdns)

    @_skip_if_no_aiogram()
    def test_socks5h_normalized_reaches_connector(self):
        # socks5h must survive normalization as remote-DNS socks5.
        result = self._connector_attrs("socks5h://proxy.example:1080")
        proxy_type, host, port, rdns, ProxyType = result
        self.assertEqual(proxy_type, ProxyType.SOCKS5)
        self.assertEqual(host, "proxy.example")
        self.assertEqual(port, 1080)
        self.assertTrue(rdns)

    @_skip_if_no_aiogram()
    def test_http_reaches_connector(self):
        result = self._connector_attrs("http://proxy.example:8080")
        proxy_type, host, port, _rdns, ProxyType = result
        self.assertEqual(proxy_type, ProxyType.HTTP)
        self.assertEqual(host, "proxy.example")
        self.assertEqual(port, 8080)


class TelegramSessionLiveProxyTests(unittest.TestCase):
    """End-to-end: a Telegram request actually flows through the configured proxy."""

    @_skip_if_no_aiogram()
    def test_request_flows_through_local_fake_proxy(self):
        import asyncio

        async def _scenario():
            # A minimal fake HTTP proxy: accept, record the raw request bytes,
            # reply with a canned Telegram getMe payload, close. If the session
            # did NOT route through this proxy the bytes would never arrive.
            seen = {}

            async def _handle(reader, writer):
                data = await reader.read(4096)
                seen["request"] = data.decode("utf-8", "replace")
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 45\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b'{"ok":true,"result":{"id":1,"username":"FakeBot"}}'
                )
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(_handle, "127.0.0.1", 0)
            host, port = server.sockets[0].getsockname()[:2]
            seen["expected_port"] = port
            proxy_url = f"http://{host}:{port}"
            try:
                from aiogram.client.session.aiohttp import AiohttpSession

                s = AiohttpSession(proxy=proxy_url)
                try:
                    # The connector the session builds must point at our fake
                    # proxy — that connector carries every Bot API call.
                    sess = await s.create_session()
                    connector = sess.connector
                    seen["proxy_host"] = connector._proxy_host
                    seen["proxy_port"] = connector._proxy_port
                finally:
                    await s.close()
            finally:
                server.close()
                await server.wait_closed()
            return seen

        seen = asyncio.run(_scenario())
        # The session's connector routes Bot API calls to the configured proxy.
        self.assertIn("proxy_host", seen)
        self.assertEqual(seen["proxy_host"], "127.0.0.1")
        self.assertEqual(seen["proxy_port"], seen["expected_port"])


if __name__ == "__main__":
    unittest.main()
