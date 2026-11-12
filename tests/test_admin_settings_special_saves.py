"""Owner bot settings that are normalized before saving are actually saved."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.handlers import admin_settings


class NormalizedSaveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)

    async def save(self, key: str, text: str) -> AsyncMock:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": key}))
        message = MagicMock(text=text, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()) as finished,
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        finished.assert_awaited_once()
        return saved

    async def test_the_force_join_channel_is_saved_normalized(self) -> None:
        saved = await self.save("force_join_channel", "@MyChannel")
        saved.assert_awaited_once()
        self.assertEqual(saved.await_args.args[1], "force_join_channel")
        self.assertEqual(saved.await_args.args[2], '[{"id": "@mychannel", "required": true}]')

    async def test_the_duplicate_receipt_policy_is_saved_normalized(self) -> None:
        saved = await self.save("receipt_dup_policy", " BLOCK ")
        saved.assert_awaited_once()
        self.assertEqual(saved.await_args.args[1:], ("receipt_dup_policy", "block"))
        saved = await self.save("receipt_dup_policy", "anything else")
        self.assertEqual(saved.await_args.args[1], "receipt_dup_policy")
        self.assertIn(saved.await_args.args[2], {"warn", "block"})
