"""Command center navigation: the hub search button, «بازگشت» everywhere, and the back history."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.broadcast_segments import targeted_keyboard
from app.bot.command_panel import PanelData, panel_rows
from app.bot.handlers import command_center, command_schedules
from app.bot.handlers.admin_resplan_payg import screen_keyboard
from app.bot.handlers.make_reseller_plan import _plan_keyboard
from app.bot.handlers.reseller_plan_extra_packs_admin import _packs_home_kb
from app.bot.nav_inline import admin_groups_hub_keyboard, reseller_manage_hub_keyboard
from app.bot.panel_back import planning_back
from app.bot.panel_history import PanelHistories, Shown, _plan, view_family
from app.services.admin_counters import PendingCounts
from app.services.quick_switches import OWNER_SWITCHES, SHOP_SWITCHES

CUSTOM_BACK = {"btn_back": "🔙 برگرد"}
NOW = __import__("datetime").datetime(2026, 11, 19, tzinfo=__import__("datetime").timezone.utc)


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _last_button(markup):
    return markup.inline_keyboard[-1][-1]


class HubSearchButtonTests(unittest.TestCase):
    def test_admin_and_shop_panels_offer_search_beside_the_command_center(self) -> None:
        self.assertEqual(_callbacks(admin_groups_hub_keyboard({}))[:2], ["cmd:h", "cmd:s"])
        with patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=[("res_dash", "خانه")]):
            self.assertEqual(_callbacks(reseller_manage_hub_keyboard(MagicMock(), {}))[:2], ["cmd:h", "cmd:s"])


class BackHistoryTests(unittest.TestCase):
    def test_after_the_command_center_the_panel_home_still_goes_back_to_the_main_menu(self) -> None:
        histories, key = PanelHistories(), (1, 2, 3)
        # Open the command center, then return to the panel home (its «بازگشت» replays the home).
        for data in ("cmd:h", "nv:adm:home"):
            family = view_family(data)
            plan, screens = _plan(histories.get(key), data, family)
            if family is not None:
                histories.show(key, screens, Shown(data, family), exit=None)
        plan, _screens = _plan(histories.get(key), "nv:adm:home", view_family("nv:adm:home"))
        with planning_back(plan):
            home = admin_groups_hub_keyboard({})
        self.assertEqual(_last_button(home).callback_data, "menu:home")

    def test_the_command_center_is_an_input_prompt_not_a_recorded_view(self) -> None:
        self.assertIsNone(view_family("cmd:h"))
        self.assertIsNotNone(view_family("cmd:sc"))


class PromptBackButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_command_center_leaves_with_the_configured_back_button(self) -> None:
        # A panel message: the prompt is edited into it.
        callback = MagicMock()
        callback.edit_text = AsyncMock()
        callback.chat = SimpleNamespace(id=9)
        callback.message_id = 4
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
        with patch("app.bot.nav_input._tidy_after_step", AsyncMock()):
            await command_center.ask_command_query(
                callback, state, kind_code="a", back_callback="nv:adm:home", ui=CUSTOM_BACK
            )
        markup = callback.edit_text.await_args.kwargs["reply_markup"]
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertNotIn("انصراف", texts)
        self.assertEqual(_last_button(markup).text, "🔙 برگرد")
        self.assertEqual(_last_button(markup).callback_data, "nv:cancel:cmd")

    async def test_the_hub_search_starts_on_every_kind_and_returns_home(self) -> None:
        callback = MagicMock(data="cmd:s")
        callback.answer = AsyncMock()
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
        viewer = command_center.CommandViewer([], "nv:adm:home")
        with patch.object(command_center, "load_command_viewer", AsyncMock(return_value=viewer)), patch.object(
            command_center, "ask_command_query", AsyncMock()
        ) as ask:
            await command_center.open_search(callback, MagicMock(), SimpleNamespace(id=1), state)
        self.assertEqual(ask.await_args.kwargs["kind_code"], "a")
        self.assertEqual(ask.await_args.kwargs["back_callback"], "nv:adm:home")


class CardsBesideCryptoTests(unittest.TestCase):
    def test_the_card_list_sits_in_the_crypto_row_for_the_owner_and_shops(self) -> None:
        data = PanelData(ui={}, queue=PendingCounts(), changes=[], now=NOW)
        for switches in (OWNER_SWITCHES, SHOP_SWITCHES, tuple(s for s in SHOP_SWITCHES if s.code != "ea")):
            rows = panel_rows(data, switches, cards_callback="adm:st:sub:pay:card")
            row = next(r for r in rows if any(b.callback_data == "adm:st:sub:pay:card" for b in r))
            self.assertEqual([b.callback_data for b in row], ["cmd:t:pr:1", "adm:st:sub:pay:card"])


class ConfiguredBackButtonTests(unittest.TestCase):
    def test_every_new_and_fixed_back_button_uses_the_back_setting(self) -> None:
        viewer = command_center.CommandViewer([], "nv:adm:home", ui=CUSTOM_BACK)
        _body, results = command_center.results_panel(viewer, "ززز")
        plan = SimpleNamespace(id=7, allow_pg_panel_access=True, billing_mode="fixed", payg_basis=None)
        built = {
            "results": _last_button(results),
            "schedules": command_schedules._back_row(CUSTOM_BACK, "cmd:h")[0],
            "segments": _last_button(targeted_keyboard(shop_id=None, pick_prefix="p:", back="hub", ui=CUSTOM_BACK)),
            "plan pick": _last_button(_plan_keyboard([], CUSTOM_BACK)),
            "payg": _last_button(screen_keyboard(plan, CUSTOM_BACK)),
            "packs": _last_button(_packs_home_kb(7, CUSTOM_BACK)),
        }
        for name, button in built.items():
            self.assertEqual(button.text, "🔙 برگرد", name)


if __name__ == "__main__":
    unittest.main()
