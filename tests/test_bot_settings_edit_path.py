"""Bot settings edit path: typed values are normalised like on the web, prompts survive stored HTML.

The shop bot used to normalise the force-join channel list and then overwrite it with the raw
message, and it never normalised the duplicate-receipt policy. The «فعلی» preview of both bots was
inserted into Telegram HTML unescaped, so a stored text with a tag (or a tag cut by the preview
length) made Telegram reject the prompt.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot.handlers import admin_settings, reseller_settings

SHOP_ID = 7
HOSTILE = '<b>راهنما</b> <a href="tg://x">لینک</a> & 2<3'


def _shop_profile() -> SimpleNamespace:
    return SimpleNamespace(user_id=SHOP_ID)


class ShopSaveNormalisationTests(unittest.IsolatedAsyncioTestCase):
    async def save(self, key: str, text: str) -> AsyncMock:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": key}))
        message = MagicMock(text=text, entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(_shop_profile(), None))),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
            patch.object(reseller_settings, "_close_input_to_location", AsyncMock()) as closed,
        ):
            await reseller_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        closed.assert_awaited_once()
        saved.assert_awaited_once()
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)
        return saved

    async def test_force_join_channel_is_saved_normalised(self) -> None:
        saved = await self.save("force_join_channel", "@MyChannel")
        self.assertEqual(saved.await_args.args[1:], ("force_join_channel", '[{"id": "@mychannel", "required": true}]'))

    async def test_duplicate_receipt_policy_is_saved_normalised(self) -> None:
        saved = await self.save("receipt_dup_policy", " BLOCK ")
        self.assertEqual(saved.await_args.args[1:], ("receipt_dup_policy", "block"))
        saved = await self.save("receipt_dup_policy", "<script>")
        self.assertIn(saved.await_args.args[2], {"warn", "block"})

    async def test_plain_text_field_keeps_the_typed_text(self) -> None:
        saved = await self.save("support_text", "  پشتیبانی ۲۴ ساعته  ")
        self.assertEqual(saved.await_args.args[1], "support_text")
        self.assertIn("پشتیبانی ۲۴ ساعته", saved.await_args.args[2])


class PromptEscapingTests(unittest.IsolatedAsyncioTestCase):
    def assert_escaped(self, prompt: str) -> None:
        self.assertNotIn("<a href", prompt)
        self.assertIn("&lt;b&gt;", prompt)
        self.assertIn("&amp;", prompt)
        self.assertIn("2&lt;3", prompt)

    async def test_owner_prompt_escapes_the_current_value(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)
        callback = MagicMock(data="adm:st:edit:support_text", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "get_setting", AsyncMock(return_value=HOSTILE)),
            patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
        ):
            await admin_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
        self.assert_escaped(asked.await_args.kwargs["prompt"])

    async def test_shop_prompt_escapes_the_current_value(self) -> None:
        callback = MagicMock(data="res:st:edit:support_text", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(_shop_profile(), None))),
            patch.object(reseller_settings, "get_setting", AsyncMock(return_value=HOSTILE)),
            patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
        ):
            await reseller_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
        self.assert_escaped(asked.await_args.kwargs["prompt"])

    async def test_preview_cut_inside_a_tag_stays_valid_html(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)
        long_text = "ا" * 118 + "<b>بخش پررنگ</b>"
        callback = MagicMock(data="adm:st:edit:support_text", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "get_setting", AsyncMock(return_value=long_text)),
            patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
        ):
            await admin_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
        current = asked.await_args.kwargs["prompt"].split("<code>", 1)[1].split("</code>", 1)[0]
        self.assertNotIn("<", current)


if __name__ == "__main__":
    unittest.main()
