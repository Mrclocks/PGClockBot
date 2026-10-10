"""v4.10.8 — force-join fail-closed, user delete wallet warn, no autofocus."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
USERS = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
USER_EDIT = (ROOT / "app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "app/web/templates/login.html").read_text(encoding="utf-8")
START = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
MW = (ROOT / "app/bot/middlewares.py").read_text(encoding="utf-8")


class ForceJoinFixTests(unittest.TestCase):
    def test_normalizes_tme_and_username(self):
        from app.services.users import (
            normalize_force_join_channel_id,
            parse_force_join_channels,
        )

        self.assertEqual(normalize_force_join_channel_id("https://t.me/MyChan"), "@mychan")
        self.assertEqual(normalize_force_join_channel_id("mychan"), "@mychan")
        self.assertEqual(normalize_force_join_channel_id("-100123"), "-100123")
        raw = '[{"id":"https://t.me/must","required":true},{"id":"@opt","required":false}]'
        self.assertEqual(parse_force_join_channels(raw), ["@must"])

    def test_blocks_on_unverified_and_missing(self):
        self.assertIn("force_join_block_message", MW)
        self.assertIn("if missing or unverified", MW)
        self.assertIn("هنوز عضو کانال‌ها نشده‌اید", MW)
        self.assertIn("clear_force_join_member_cache", START)
        self.assertIn("missing or unverified", START)
        # deep-link sub_ must not bypass the gate
        sub_idx = START.find('args.startswith("sub_")')
        force_idx = START.find("check_force_join_all")
        self.assertGreater(sub_idx, force_idx)

    def test_does_not_cache_negative_membership(self):
        from app.bot import middlewares as mw

        mw._FORCE_JOIN_MEMBER_CACHE.clear()
        bot = AsyncMock()
        member = MagicMock()
        member.status = "left"
        bot.get_chat_member = AsyncMock(return_value=member)

        async def _run():
            self.assertIs(await mw.check_force_join_member(bot, 7, "@chan"), False)
            self.assertEqual(mw._FORCE_JOIN_MEMBER_CACHE, {})
            member.status = "member"
            self.assertIs(await mw.check_force_join_member(bot, 7, "@chan"), True)
            self.assertTrue(mw._FORCE_JOIN_MEMBER_CACHE)

        asyncio.run(_run())

    def test_fail_closed_when_api_errors(self):
        from app.bot import middlewares as mw

        mw._FORCE_JOIN_MEMBER_CACHE.clear()
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(side_effect=RuntimeError("chat not found"))

        async def _run():
            missing, unverified = await mw.check_force_join_all(bot, 1, ["@chan"])
            self.assertEqual(missing, [])
            self.assertEqual(unverified, ["@chan"])
            text = mw.force_join_block_message(missing, unverified)
            self.assertIn("تأیید", text)
            self.assertIn("ادمین", text)

        asyncio.run(_run())


class UserDeleteWalletWarnTests(unittest.TestCase):
    def test_list_and_edit_warn_on_positive_wallet(self):
        self.assertIn("row.wallet_balance > 0", USERS)
        self.assertIn("موجودی کیف پول", USERS)
        self.assertIn("data-confirm-warn", USERS)
        self.assertIn("حذف کاربر", USER_EDIT)
        self.assertIn("موجودی کیف پول", USER_EDIT)
        self.assertIn('action="/users/{{ user.id }}/delete"', USER_EDIT)
        self.assertIn("wallet_amt > 0", USER_EDIT)
        self.assertIn("display_wallet", USER_EDIT)


class AuthFieldAlignTests(unittest.TestCase):
    def test_auth_form_ltr_inputs_right_aligned(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("Login / setup auth boxes", css)
        block = css.split("Login / setup auth boxes", 1)[1].split("CRITICAL:", 1)[0]
        self.assertIn("text-align: right;", block)
        self.assertIn(".auth-form input[dir=\"ltr\"]", block)


class NoAutofocusTests(unittest.TestCase):
    def test_open_modal_does_not_autofocus(self):
        block = JS.split("function openModal")[1].split("window.openModal")[0]
        self.assertIn("Never autofocus", block)
        self.assertNotIn(".focus()", block)
        self.assertNotIn("focus.focus", block)

    def test_force_channel_add_does_not_focus(self):
        add_block = JS.split("[data-force-channels-add]")[1].split("force-channel-remove")[0]
        self.assertNotIn(".focus()", add_block)

    def test_force_channel_guards_post_save_keyboard(self):
        block = JS.split("initForceChannels")[1].split("setupPanelConfirm")[0]
        self.assertIn("guardChannelInputs", block)
        self.assertIn("readonly", block)
        self.assertIn("blurForceChannelFocus", block)
        self.assertIn("pageshow", block)

    def test_confirm_open_does_not_focus_reason(self):
        self.assertIn("Do not autofocus reason", JS)
        self.assertNotIn("reasonInput.focus()", JS.split("openModal('modal-confirm')")[1].split("formEl.addEventListener")[0])

    def test_login_has_no_autofocus(self):
        self.assertNotIn("autofocus", LOGIN)


if __name__ == "__main__":
    unittest.main()
