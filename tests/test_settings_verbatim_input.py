"""Typing a menu label as a new settings value saves it instead of opening that menu."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State

from app.bot import keyboards as kb
from app.bot import menu_nav as nav
from app.bot.handlers import reply_nav, start
from app.bot.handlers.admin_settings import SettingsStates
from app.bot.handlers.guide_editor import OwnerGuideInput, ShopGuideInput
from app.bot.handlers.loyalty import LoyaltyManageStates
from app.bot.handlers.plan_catalog_manage import PlanCatalogStates
from app.bot.handlers.reseller_settings import ResellerSettingsStates
from app.bot.verbatim_input import VERBATIM_TEXT_INPUT_STATES
from tests.test_bot_ui_keyboard_snapshots import default_ui


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _message(text: str) -> MagicMock:
    message = MagicMock()
    message.text = text
    return message


async def _menu_filter(text: str, fsm_state: State | None) -> bool | dict:
    state = _state()
    await state.set_data({nav.NAV_LEVEL: nav.NAV_ADMIN_SETTINGS})
    if fsm_state is not None:
        await state.set_state(fsm_state)
    nav_context = (default_ui(), "admin", True, False)
    with patch.object(reply_nav, "_nav_context", AsyncMock(return_value=nav_context)):
        return await reply_nav.ReplyMenuTextFilter()(
            _message(text), MagicMock(), MagicMock(id=1), state, is_reseller_bot=False
        )


def _restart_handler():
    for handler in start.router.message.handlers:
        if handler.callback is start.cmd_restart:
            return handler
    raise AssertionError("restart handler is not registered")


async def _restart_matches(text: str, fsm_state: State | None) -> bool:
    state = _state()
    if fsm_state is not None:
        await state.set_state(fsm_state)
    matched, _ = await _restart_handler().check(_message(text), state=state)
    return matched


class SettingsValueIsNotMenuTapTests(unittest.TestCase):
    def test_reseller_apply_label_is_saved_while_editing_a_setting(self) -> None:
        label = default_ui()["btn_reseller_apply"]
        result = asyncio.run(_menu_filter(label, SettingsStates.edit_value))
        self.assertFalse(result)

    def test_label_without_emoji_is_saved_while_editing_a_setting(self) -> None:
        result = asyncio.run(_menu_filter("درخواست نمایندگی", SettingsStates.edit_value))
        self.assertFalse(result)

    def test_home_label_is_saved_while_editing_a_shop_setting(self) -> None:
        home = kb._home_label(default_ui())
        result = asyncio.run(_menu_filter(home, ResellerSettingsStates.edit_value))
        self.assertFalse(result)

    def test_settings_search_for_a_label_is_not_a_menu_tap(self) -> None:
        label = default_ui()["btn_reseller_apply"]
        result = asyncio.run(_menu_filter(label, SettingsStates.search_query))
        self.assertFalse(result)

    def test_same_label_still_navigates_outside_settings_prompt(self) -> None:
        label = default_ui()["btn_reseller_apply"]
        result = asyncio.run(_menu_filter(label, None))
        self.assertTrue(result)
        self.assertEqual(result["reply_action"], kb.REPLY_ACTION_RESELLER_APPLY)

    def test_cancel_still_reaches_the_settings_prompt(self) -> None:
        result = asyncio.run(_menu_filter("انصراف", SettingsStates.edit_value))
        self.assertFalse(result)


class RestartLabelIsNotRestartWhileEditingTests(unittest.TestCase):
    def test_restart_label_is_saved_while_editing_a_setting(self) -> None:
        label = default_ui()["btn_restart"]
        self.assertFalse(asyncio.run(_restart_matches(label, SettingsStates.edit_value)))

    def test_restart_label_still_restarts_outside_settings_prompt(self) -> None:
        label = default_ui()["btn_restart"]
        self.assertTrue(asyncio.run(_restart_matches(label, None)))


class VerbatimStateNamesTests(unittest.TestCase):
    def test_every_listed_state_exists(self) -> None:
        groups = (
            SettingsStates,
            ResellerSettingsStates,
            OwnerGuideInput,
            ShopGuideInput,
            LoyaltyManageStates,
            PlanCatalogStates,
        )
        real = {s.state for group in groups for s in group.__all_states__}
        self.assertEqual(VERBATIM_TEXT_INPUT_STATES - real, set())

    def test_settings_text_prompts_are_listed(self) -> None:
        for fsm_state in (
            SettingsStates.edit_value,
            SettingsStates.support_title,
            SettingsStates.trial_name,
            SettingsStates.search_query,
            ResellerSettingsStates.edit_value,
            ResellerSettingsStates.support_title,
        ):
            self.assertIn(fsm_state.state, VERBATIM_TEXT_INPUT_STATES)


if __name__ == "__main__":
    unittest.main()
