"""v4.10.10 — force-join resolve, invite/t.me/c ids, inline-only channels, custom msg."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
MW = (ROOT / "app/bot/middlewares.py").read_text(encoding="utf-8")
START = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
USERS = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
JS = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")


class NormalizeResolveTests(unittest.TestCase):
    def test_tme_c_and_invite_kept(self):
        from app.services.users import (
            force_join_channel_url,
            normalize_force_join_channel_id,
            parse_force_join_entries,
        )

        self.assertEqual(
            normalize_force_join_channel_id("https://t.me/c/2156789012/5"),
            "-1002156789012",
        )
        self.assertEqual(normalize_force_join_channel_id("c/2156789012"), "-1002156789012")
        self.assertEqual(normalize_force_join_channel_id("@MyChan"), "@mychan")

        raw = '[{"id":"https://t.me/+AbCd","required":true,"title":"اخبار"}]'
        entries = parse_force_join_entries(raw)
        self.assertEqual(len(entries), 1)
        self.assertTrue(str(entries[0]["id"]).startswith("https://t.me/+"))
        self.assertEqual(entries[0]["title"], "اخبار")
        self.assertEqual(
            force_join_channel_url(entries[0]["id"], entries[0].get("link")),
            "https://t.me/+AbCd",
        )

    def test_title_serialized(self):
        from app.services.users import parse_force_join_entries, serialize_force_join_entries

        raw = serialize_force_join_entries(
            [{"id": "@news", "required": True, "title": "کانال اخبار", "link": ""}]
        )
        entries = parse_force_join_entries(raw)
        self.assertEqual(entries[0]["title"], "کانال اخبار")


class MembershipResolveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.bot import middlewares as mw

        mw._FORCE_JOIN_MEMBER_CACHE.clear()
        mw._FORCE_JOIN_RESOLVE_CACHE.clear()
        mw._FORCE_JOIN_BOT_ADMIN_CACHE.clear()

    async def test_invite_resolved_via_get_chat(self):
        from app.bot import middlewares as mw

        bot = AsyncMock()
        chat = MagicMock()
        chat.id = -100555
        bot.get_chat = AsyncMock(return_value=chat)
        bot.get_me = AsyncMock(side_effect=RuntimeError("skip admin probe"))

        member = MagicMock()
        member.status = "member"
        bot.get_chat_member = AsyncMock(return_value=member)

        ok = await mw.check_force_join_member(
            bot, 9, "https://t.me/+InviteHash", invite_link="https://t.me/+InviteHash"
        )
        self.assertIs(ok, True)
        bot.get_chat.assert_awaited()
        bot.get_chat_member.assert_awaited_with(-100555, 9)

    async def test_bot_not_admin_returns_unverified(self):
        from app.bot import middlewares as mw

        bot = AsyncMock()
        chat = MagicMock()
        chat.id = -1001
        bot.get_chat = AsyncMock(return_value=chat)
        me = MagicMock()
        me.id = 99
        bot.get_me = AsyncMock(return_value=me)

        bot_member = MagicMock()
        bot_member.status = "member"  # not admin
        bot.get_chat_member = AsyncMock(return_value=bot_member)

        result = await mw.check_force_join_member(bot, 1, "@chan")
        self.assertIsNone(result)

    async def test_status_member_still_true(self):
        from app.bot import middlewares as mw

        bot = AsyncMock()
        bot.get_chat = AsyncMock(side_effect=RuntimeError("no resolve"))
        bot.get_me = AsyncMock(side_effect=RuntimeError("skip"))
        member = MagicMock()
        member.status = "member"
        bot.get_chat_member = AsyncMock(return_value=member)
        self.assertIs(await mw.check_force_join_member(bot, 1, "@chan"), True)


class MessageKeyboardTests(unittest.TestCase):
    def test_default_message_has_no_channel_bullets(self):
        from app.bot.middlewares import force_join_block_message

        text = force_join_block_message(["@a", "@b"], [])
        self.assertNotIn("@a", text)
        self.assertIn("دکمه‌های زیر", text)
        unverified = force_join_block_message([], ["@a"])
        self.assertIn("ادمین", unverified)
        custom = force_join_block_message(["@a"], [], custom="فقط متن سفارشی")
        self.assertEqual(custom, "فقط متن سفارشی")
        custom_ch = force_join_block_message(
            ["@a"], [], custom="لیست:\n{channels}"
        )
        self.assertIn("@a", custom_ch)

    def test_inline_uses_title(self):
        from app.bot.keyboards import force_join_inline_keyboard

        kb = force_join_inline_keyboard(
            '[{"id":"@News","required":true,"title":"کانال رسمی"}]'
        )
        flat = [b for row in kb.inline_keyboard for b in row]
        labels = [b.text for b in flat]
        self.assertIn("کانال رسمی", labels)
        self.assertIn("forcejoin:check", [b.callback_data for b in flat if b.callback_data])

    def test_wiring(self):
        self.assertIn("resolve_force_join_chat_id", MW)
        self.assertIn("_ensure_bot_can_check", MW)
        self.assertIn("forcejoin:nolink", START)
        self.assertIn("force-channel-title", JS)
        self.assertIn("is_force_join_invite_ref", USERS)
        self.assertIn("t.me/c/", USERS)


if __name__ == "__main__":
    unittest.main()
