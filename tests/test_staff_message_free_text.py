"""Staff DMs: menu labels stay message text, resellers can be messaged, sender is named."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import keyboards as kb
from app.bot import menu_nav as nav
from app.bot.handlers import admin, reply_nav
from app.bot.handlers.admin import AdminStates
from app.bot.handlers.reseller import ResellerStates
from app.bot.handlers.support import SupportStates
from app.bot.handlers.ticket_actions import TicketActionStates
from app.db.models import BotUser, Role
from app.services.formatting import bot_user_panel_label
from app.services.users_quick import wrap_staff_dm
from tests.test_bot_ui_keyboard_snapshots import default_ui


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


async def _press_admin(text: str, *, fsm_state=None, level: str = nav.NAV_ADMIN_PLANS_KIND):
    state = _state()
    await state.set_data({nav.NAV_LEVEL: level})
    if fsm_state is not None:
        await state.set_state(fsm_state)
    message = MagicMock()
    message.text = text
    nav_context = (default_ui(), "admin", True, False)
    with patch.object(reply_nav, "_nav_context", AsyncMock(return_value=nav_context)):
        return await reply_nav.ReplyMenuTextFilter()(
            message, MagicMock(), MagicMock(id=1), state, is_reseller_bot=False
        )


class FreeTextPromptFilterTests(unittest.TestCase):
    def test_hidden_submenu_label_is_message_text_while_composing_dm(self) -> None:
        trial_label = kb._label_or(default_ui(), "btn_shop_kind_trial", "تست")
        result = asyncio.run(_press_admin(trial_label, fsm_state=AdminStates.user_message))
        self.assertFalse(result)

    def test_same_label_still_navigates_outside_free_text_prompt(self) -> None:
        trial_label = kb._label_or(default_ui(), "btn_shop_kind_trial", "تست")
        result = asyncio.run(_press_admin(trial_label))
        self.assertEqual(result["reply_action"], kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL)

    def test_home_label_still_escapes_free_text_prompt(self) -> None:
        home = kb._home_label(default_ui())
        result = asyncio.run(_press_admin(home, fsm_state=AdminStates.user_message))
        self.assertEqual(result["reply_action"], kb.REPLY_ACTION_HOME)

    def test_free_text_state_names_match_real_states(self) -> None:
        for fsm_state in (
            AdminStates.user_message,
            AdminStates.ticket_reply,
            AdminStates.broadcast_text,
            ResellerStates.user_message,
            SupportStates.body,
            SupportStates.reply,
            TicketActionStates.reply,
        ):
            self.assertIn(fsm_state.state, reply_nav.FREE_TEXT_INPUT_STATES)


def _reseller(*, role: str = Role.RESELLER.value) -> BotUser:
    return BotUser(id=42, telegram_id=4242, full_name="Rep <b>", role=role, reseller_id=None)


def _owner() -> BotUser:
    return BotUser(id=1, telegram_id=6127326933, full_name="Support Team", username="pg_support")


async def _send_dm_as_owner(target: BotUser, card: str, *, can_manage_reps: bool = True):
    state = _state()
    await state.set_state(AdminStates.user_message)
    await state.update_data(msg_user_id=int(target.id), msg_card=card)
    session = MagicMock()
    session.get = AsyncMock(return_value=target)
    session.commit = AsyncMock()
    message = MagicMock()
    message.text = "تست"
    message.answer = AsyncMock()
    send_dm = AsyncMock()
    reseller_card = AsyncMock(return_value=("reseller card", None))
    user_card = AsyncMock(return_value=("user card", None))
    finish = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)))
        stack.enter_context(patch.object(admin, "_is_admin", return_value=True))
        stack.enter_context(
            patch.object(
                admin,
                "platform_can_manage_representatives",
                AsyncMock(return_value=can_manage_reps),
            )
        )
        stack.enter_context(patch.object(admin, "_reseller_card", reseller_card))
        stack.enter_context(patch.object(admin, "_user_card", user_card))
        stack.enter_context(patch.object(admin, "finish_text_step", finish))
        stack.enter_context(patch.object(admin, "get_all_settings", AsyncMock(return_value={})))
        stack.enter_context(patch("app.services.users_quick.send_staff_dm", send_dm))
        await admin.adm_users_message_send(
            message, state=state, session=session, db_user=_owner()
        )
    message.finish = finish  # the one in-place outcome
    return send_dm, reseller_card, user_card, message


class ResellerMessageTests(unittest.TestCase):
    def test_reseller_card_offers_message_button(self) -> None:
        markup = kb.admin_reseller_actions(42)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:resellers:msg:42", callbacks)

    def test_owner_message_reaches_reseller_and_returns_to_reseller_card(self) -> None:
        target = _reseller()
        send_dm, reseller_card, user_card, message = asyncio.run(
            _send_dm_as_owner(target, admin.MSG_CARD_RESELLER)
        )
        send_dm.assert_awaited_once()
        self.assertIs(send_dm.await_args.args[1], target)
        self.assertEqual(send_dm.await_args.args[2], "تست")
        self.assertIsNone(send_dm.await_args.kwargs["shop_id"])
        self.assertEqual(send_dm.await_args.kwargs["actor"], "Support Team (@pg_support)")
        reseller_card.assert_awaited_once()
        user_card.assert_not_awaited()
        self.assertIn("reseller card", message.finish.await_args.kwargs["text"])
        message.answer.assert_not_awaited()

    def test_reseller_message_target_rejects_non_reseller(self) -> None:
        target = _reseller(role=Role.USER.value)
        send_dm, reseller_card, _, message = asyncio.run(
            _send_dm_as_owner(target, admin.MSG_CARD_RESELLER)
        )
        send_dm.assert_not_awaited()
        reseller_card.assert_not_awaited()
        self.assertEqual(message.finish.await_args.kwargs["text"], "نماینده یافت نشد")
        message.answer.assert_not_awaited()

    def test_reseller_message_denied_when_representative_management_is_off(self) -> None:
        send_dm, reseller_card, _, _ = asyncio.run(
            _send_dm_as_owner(_reseller(), admin.MSG_CARD_RESELLER, can_manage_reps=False)
        )
        send_dm.assert_not_awaited()
        reseller_card.assert_not_awaited()


class StaffDmSenderLineTests(unittest.TestCase):
    def test_sender_line_shows_name_and_username_instead_of_numeric_id(self) -> None:
        payload = wrap_staff_dm("سلام", actor=bot_user_panel_label(_owner()))
        self.assertIn("از: Support Team (@pg_support)", payload)
        self.assertNotIn("6127326933", payload)

    def test_hostile_sender_name_is_escaped(self) -> None:
        sender = BotUser(id=2, telegram_id=7, full_name="<a href='x'>evil</a>", username=None)
        payload = wrap_staff_dm("سلام", actor=bot_user_panel_label(sender))
        self.assertNotIn("<a href", payload)
        self.assertIn("&lt;a href=", payload)


if __name__ == "__main__":
    unittest.main()
