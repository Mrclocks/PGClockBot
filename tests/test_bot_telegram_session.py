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


if __name__ == "__main__":
    unittest.main()
