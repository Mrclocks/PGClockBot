"""Regression tests for bot_appearance.fetch_appearance error handling.

Verifies that Bot construction failures and timeouts are caught and reported
as BotAppearance.error instead of propagating as 500s to the settings page.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.bot_appearance import BotAppearance, fetch_appearance


class FetchAppearanceErrorHandlingTests:
    """fetch_appearance must never raise; it returns BotAppearance(error=...)."""

    @pytest.mark.asyncio
    async def test_empty_token_returns_error(self):
        result = await fetch_appearance("")
        assert not result.ok
        assert "توکن" in result.error

    @pytest.mark.asyncio
    async def test_bot_construction_failure_is_caught(self):
        """If Bot() or _tg_session() raises, the error is reported, not raised."""
        with patch(
            "app.services.bot_appearance._tg_session", side_effect=RuntimeError("bad proxy")
        ):
            result = await fetch_appearance("123:ABC")
        assert not result.ok
        assert "bad proxy" in result.error

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        """A hung Bot API call is cut off by asyncio.wait_for and reported."""
        slow_bot = AsyncMock()
        slow_bot.get_me = AsyncMock(
            side_effect=lambda: asyncio.sleep(60)
        )

        async def _slow_data(bot):
            await bot.get_me()

        with (
            patch(
                "app.services.bot_appearance._tg_session", return_value=None
            ),
            patch(
                "app.services.bot_appearance.Bot",
                return_value=slow_bot,
            ),
            patch(
                "app.services.bot_appearance._fetch_appearance_data",
                side_effect=_slow_data,
            ),
            patch("asyncio.wait_for", side_effect=asyncio.TimeoutError),
        ):
            result = await fetch_appearance("123:ABC")
        assert not result.ok
        assert "زمان‌بسته" in result.error

    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        """When all four Bot API calls succeed, ok=True and fields are populated."""
        me = AsyncMock()
        me.username = "testbot"
        me.first_name = "TestBot"

        name_obj = AsyncMock()
        name_obj.name = "TestBot"

        desc_obj = AsyncMock()
        desc_obj.description = "About"

        short_obj = AsyncMock()
        short_obj.short_description = "Caption"

        fake_bot = AsyncMock()
        fake_bot.get_me = AsyncMock(return_value=me)
        fake_bot.session.close = AsyncMock()

        with (
            patch("app.services.bot_appearance._tg_session", return_value=None),
            patch("app.services.bot_appearance.Bot", return_value=fake_bot),
            patch(
                "app.services.bot_appearance._fetch_appearance_data",
                return_value=(me, name_obj, desc_obj, short_obj),
            ),
        ):
            result = await fetch_appearance("123:ABC")

        assert result.ok
        assert result.name == "TestBot"
        assert result.username == "testbot"
        assert result.description == "About"
        assert result.short_description == "Caption"
