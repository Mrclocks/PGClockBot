"""Tests for Telegram bot appearance helpers.

Covers appearance helpers (clip/validate/form dict) and the fetch_appearance
error handling added with TG_PROXY: Bot construction failures and timeouts are
reported as BotAppearance.error instead of propagating to the settings page.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.bot_appearance import (
    DESCRIPTION_MAX,
    NAME_MAX,
    SHORT_DESCRIPTION_MAX,
    BotAppearance,
    appearance_to_form_dict,
    clip,
    fetch_appearance,
    validate_appearance_form,
)

ROOT = Path(__file__).resolve().parents[1]


class ClipTests(unittest.TestCase):
    def test_clip_trims_and_limits(self):
        self.assertEqual(clip("  abc  ", 2), "ab")
        self.assertEqual(clip(None, 5), "")


class ValidateTests(unittest.TestCase):
    def test_ok(self):
        self.assertIsNone(
            validate_appearance_form(
                name="Clock",
                description="about",
                short_description="hi",
            )
        )

    def test_name_too_long(self):
        err = validate_appearance_form(
            name="x" * (NAME_MAX + 1),
            description="",
            short_description="",
        )
        self.assertIn("نام", err or "")

    def test_description_too_long(self):
        err = validate_appearance_form(
            name="n",
            description="d" * (DESCRIPTION_MAX + 1),
            short_description="",
        )
        self.assertIn("وسط", err or "")

    def test_short_too_long(self):
        err = validate_appearance_form(
            name="n",
            description="",
            short_description="s" * (SHORT_DESCRIPTION_MAX + 1),
        )
        self.assertIn("کپشن", err or "")


class FormDictTests(unittest.TestCase):
    def test_maps_fields(self):
        app = BotAppearance(
            name="A",
            username="bot",
            description="D",
            short_description="S",
        )
        d = appearance_to_form_dict(app, local_photo="uploads/x.jpg")
        self.assertEqual(d["bot_tg_name"], "A")
        self.assertEqual(d["bot_username"], "bot")
        self.assertEqual(d["bot_tg_photo"], "uploads/x.jpg")
        self.assertNotIn("bot_cmd_start", d)
        self.assertNotIn("bot_cmd_help", d)


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


class CommandsUiRemovedTests(unittest.TestCase):
    def test_settings_form_has_no_command_fields(self):
        ap = (ROOT / "app/web/templates/_settings_appearance.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("دستورهای منوی ربات", ap)
        self.assertNotIn("bot_cmd_start", ap)
        self.assertNotIn("bot_cmd_help", ap)
        self.assertNotIn("ap-cmd-start", ap)

    def test_service_no_longer_reads_or_sets_command_descriptions(self):
        src = (ROOT / "app/services/bot_appearance.py").read_text(encoding="utf-8")
        self.assertNotIn("get_my_commands", src)
        self.assertNotIn("set_my_commands", src)
        self.assertNotIn("form.get(\"bot_cmd_start\")", src)
        self.assertNotIn("form.get(\"bot_cmd_help\")", src)
        self.assertNotIn("DEFAULT_CMD_", src)
        self.assertIn("delete_my_commands", src)
        # Legacy keys cleared on save so old DB values disappear
        self.assertIn('"bot_cmd_start": ""', src)
        self.assertIn('"bot_cmd_help": ""', src)

    def test_defaults_drop_command_keys(self):
        from app.services.users import DEFAULT_SETTINGS

        self.assertNotIn("bot_cmd_start", DEFAULT_SETTINGS)
        self.assertNotIn("bot_cmd_help", DEFAULT_SETTINGS)


class TabsTests(unittest.TestCase):
    def test_appearance_in_admin_and_reseller_tabs(self):
        from app.services.resellers import RESELLER_SETTINGS_TABS
        from app.services.users import SETTINGS_TABS

        self.assertIn("appearance", {t[0] for t in SETTINGS_TABS})
        self.assertIn("appearance", {t[0] for t in RESELLER_SETTINGS_TABS})


if __name__ == "__main__":
    unittest.main()
