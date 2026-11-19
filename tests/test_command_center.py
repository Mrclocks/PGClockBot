"""Command center search: ranking, each viewer's catalogue, and the bot handler's access and paging."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.command_catalog import (
    MenuNode,
    ShopViewer,
    admin_command_entries,
    shop_command_entries,
    walk_menus,
)
from app.bot.handlers import admin_settings, command_center, reseller_settings
from app.bot.handlers.settings_labels import resolve_sub
from app.db.models import ResellerProfile
from app.services import bot_feedback_texts as feedback
from app.services.command_search import CommandEntry, CommandKind, search_commands
from tests.test_bot_ui_keyboard_snapshots import sample_reseller_profile

CALLBACK_LIMIT = 64


def _entry(label: str, target: str, *, kind: CommandKind = CommandKind.MENU, path: str = "پنل", terms: str = ""):
    return CommandEntry(label=label, path=path, target=target, kind=kind, terms=terms)


def _owner_entries() -> list[CommandEntry]:
    return admin_command_entries({}, admin_settings.SECTIONS)


def _shop_entries(profile: ResellerProfile) -> list[CommandEntry]:
    viewer = ShopViewer(profile, {}, can_add_representative=True, show_owner_creds=True)
    return shop_command_entries(viewer, reseller_settings.SECTIONS, reseller_settings.SUB_PERMISSIONS)


def _limited_profile(perms: str) -> ResellerProfile:
    return ResellerProfile(id=12, user_id=22, is_active=True, web_permissions=perms, bot_permissions=perms)


def _state() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))


def _callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class RankingTests(unittest.TestCase):
    def test_a_menu_whose_label_starts_with_the_query_comes_first_despite_its_icon(self) -> None:
        entries = [
            _entry("بکاپ همراه .env", "a", kind=CommandKind.LABEL),
            _entry("بکاپ زمان‌بندی", "b", kind=CommandKind.SETTING),
            _entry("💾 بکاپ / ریستور", "c"),
        ]
        self.assertEqual([e.target for e in search_commands(entries, "بکاپ")], ["c", "b", "a"])

    def test_a_synonym_finds_the_persian_label(self) -> None:
        entries = [_entry("🧪 کاربران تست", "trial"), _entry("کارت به کارت", "card", kind=CommandKind.SETTING)]
        self.assertEqual([e.target for e in search_commands(entries, "trial")], ["trial"])
        self.assertEqual([e.target for e in search_commands(entries, "card")], ["card"])

    def test_a_synonym_does_not_match_words_every_path_shares(self) -> None:
        entry = _entry("نمای کلی", "x", path="پنل ادمین › پاسارگارد")
        self.assertEqual(search_commands([entry], "پاسارگارد"), [entry])
        self.assertEqual(search_commands([entry], "pasarguard"), [])

    def test_hidden_terms_such_as_a_setting_key_are_searchable(self) -> None:
        entry = _entry("شماره", "pay", kind=CommandKind.SETTING, terms="card_number")
        self.assertEqual(search_commands([entry], "card_number"), [entry])

    def test_one_letter_and_blank_queries_return_nothing(self) -> None:
        entries = [_entry("کارت", "a")]
        self.assertEqual(search_commands(entries, "ک"), [])
        self.assertEqual(search_commands(entries, "   "), [])

    def test_the_same_place_reached_twice_is_listed_once(self) -> None:
        entries = [_entry("➕ افزودن پلن", "add", path="پلن‌های کاربران"), _entry("➕ افزودن پلن", "add", path="پلن‌های نمایندگان")]
        self.assertEqual(len(search_commands(entries, "افزودن")), 1)

    def test_every_word_must_match(self) -> None:
        self.assertEqual(search_commands([_entry("نام فروشگاه", "a")], "نام پرداخت"), [])


class MenuWalkTests(unittest.TestCase):
    def _markup(self, *buttons: tuple[str, str]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons]
        )

    def test_back_buttons_and_leaving_the_panel_are_not_results(self) -> None:
        nodes = {
            "home": MenuNode("خانه", lambda: self._markup(("گروه", "grp"), ("⬅️ بازگشت", "menu:home"))),
            "grp": MenuNode("گروه", lambda: self._markup(("📋 لیست", "list"), ("⬅️ بازگشت", "home"))),
        }
        walk = walk_menus("home", nodes)
        self.assertEqual([(e.label, e.path) for e in walk.entries], [("گروه", "خانه"), ("📋 لیست", "خانه › گروه")])

    def test_a_broken_menu_is_skipped_and_the_rest_is_searchable(self) -> None:
        def broken() -> InlineKeyboardMarkup:
            raise RuntimeError("menu builder failed")

        nodes = {
            "home": MenuNode("خانه", lambda: self._markup(("خراب", "bad"), ("سالم", "ok"))),
            "bad": MenuNode("خراب", broken),
        }
        with self.assertLogs("app.bot.command_catalog", level="WARNING"):
            walk = walk_menus("home", nodes)
        self.assertEqual([e.target for e in walk.entries], ["bad", "ok"])


class OwnerCatalogTests(unittest.TestCase):
    def test_owner_finds_menus_and_settings_with_valid_callbacks(self) -> None:
        entries = _owner_entries()
        targets = {e.target for e in entries}
        self.assertIn("nv:adm:backup", targets)
        self.assertIn("adm:st:sub:pay:card", targets)
        self.assertTrue(all(len(e.target.encode()) <= CALLBACK_LIMIT for e in entries))
        self.assertFalse({"menu:home", "nv:adm:home", "nv:adm:close", "adm:st:find"} & targets)
        self.assertFalse(any(t.startswith("cmd:") for t in targets))

    def test_every_owner_setting_result_opens_an_existing_subsection(self) -> None:
        for entry in _owner_entries():
            if entry.target.startswith("adm:st:sub:"):
                _a, _s, _sub, sec_id, sub_id = entry.target.split(":", 4)
                self.assertIsNotNone(resolve_sub(admin_settings.SECTIONS, sec_id, sub_id), entry.target)

    def test_searching_card_reaches_the_card_settings(self) -> None:
        hits = search_commands(_owner_entries(), "کارت")
        self.assertIn("adm:st:sub:pay:card", [e.target for e in hits])
        self.assertEqual(hits[0].kind, CommandKind.SETTING)


class ShopCatalogTests(unittest.TestCase):
    def test_shop_catalogue_never_offers_owner_screens(self) -> None:
        targets = {e.target for e in _shop_entries(sample_reseller_profile())}
        self.assertFalse([t for t in targets if t.startswith(("adm:", "nv:adm:"))])
        self.assertTrue(all(len(t.encode()) <= CALLBACK_LIMIT for t in targets))

    def test_shop_settings_are_found_only_with_the_settings_permission(self) -> None:
        full = {e.target for e in _shop_entries(sample_reseller_profile())}
        self.assertIn("res:st:sub:pay:card", full)
        limited = {e.target for e in _shop_entries(_limited_profile("dashboard,orders"))}
        self.assertFalse([t for t in limited if t.startswith("res:st:") or "res_st_" in t])

    def test_a_subsection_outside_the_shop_package_is_hidden(self) -> None:
        sub_id, permission = next(iter(reseller_settings.SUB_PERMISSIONS.items()))
        perms = "dashboard,shop_settings"
        self.assertNotIn(permission, perms)
        targets = {e.target for e in _shop_entries(_limited_profile(perms))}
        self.assertFalse([t for t in targets if t.endswith(f":{sub_id}")])

    def test_menus_the_shop_cannot_open_are_not_results(self) -> None:
        limited = {e.target for e in _shop_entries(_limited_profile("dashboard,orders"))}
        self.assertFalse([t for t in limited if t.startswith("shp:bc:")])


class ResultsPanelTests(unittest.TestCase):
    def _viewer(self, count: int) -> command_center.CommandViewer:
        menus = [_entry(f"کارت منو {i}", f"m:{i}") for i in range(count)]
        settings = [_entry(f"کارت تنظیم {i}", f"s:{i}", kind=CommandKind.SETTING) for i in range(3)]
        return command_center.CommandViewer([*menus, *settings], "nv:adm:home")

    def test_many_results_are_split_into_pages(self) -> None:
        viewer = self._viewer(10)
        body, first = command_center.results_panel(viewer, "کارت")
        self.assertIn("cmd:r:a:p:1", _callbacks(first))
        self.assertIn("۱۳", body)
        _body, second = command_center.results_panel(viewer, "کارت", page_index=1)
        results = [c for c in _callbacks(second) if c.startswith(("m:", "s:"))]
        self.assertEqual(len(results), 5)

    def test_a_kind_chip_shows_only_that_kind(self) -> None:
        _body, markup = command_center.results_panel(self._viewer(4), "کارت", kind_code="s")
        callbacks = _callbacks(markup)
        self.assertEqual([c for c in callbacks if c.startswith(("m:", "s:"))], ["s:0", "s:1", "s:2"])
        self.assertIn("cmd:r:m", callbacks)

    def test_no_results_and_too_short_queries_keep_the_way_back(self) -> None:
        viewer = self._viewer(1)
        for query in ("ززززز", "ک"):
            _body, markup = command_center.results_panel(viewer, query)
            self.assertEqual(_callbacks(markup), ["cmd:s", "nv:adm:home"])

    def test_hostile_query_is_escaped(self) -> None:
        body, _markup = command_center.results_panel(self._viewer(1), "<script>کارت</script>")
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)


class HandlerAccessTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, data: str) -> MagicMock:
        callback = MagicMock()
        callback.data = data
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        return callback

    async def test_platform_bot_refuses_anyone_but_the_owner(self) -> None:
        callback, state = self._callback("cmd:hs"), _state()
        with patch.object(command_center, "is_bot_owner_principal", AsyncMock(return_value=False)):
            await command_center.open_search(callback, MagicMock(), SimpleNamespace(id=5), state)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        self.assertIsNone(await state.get_state())

    async def test_shop_bot_refuses_a_person_without_shop_access(self) -> None:
        callback, state = self._callback("cmd:hs"), _state()
        with patch.object(command_center, "load_reseller_hub", AsyncMock(return_value=None)):
            await command_center.open_search(
                callback, MagicMock(), SimpleNamespace(id=5), state, is_reseller_bot=True, reseller_owner_id=21
            )
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        self.assertIsNone(await state.get_state())

    async def test_owner_gets_the_prompt_and_settings_hub_starts_on_settings(self) -> None:
        callback, state = self._callback("cmd:hs"), _state()
        viewer = command_center.CommandViewer([], "nv:adm:home")
        with patch.object(command_center, "load_command_viewer", AsyncMock(return_value=viewer)):
            await command_center.open_search(callback, MagicMock(), SimpleNamespace(id=5), state)
        self.assertEqual(await state.get_state(), command_center.CommandCenterStates.query.state)
        self.assertEqual((await state.get_data())[command_center.KIND_KEY], "s")
        self.assertEqual((await state.get_data())[command_center.BACK_KEY], "nv:adm:settings")

    async def test_results_lead_back_to_where_the_search_was_opened(self) -> None:
        owner = command_center.CommandViewer([_entry("کارت", "x")], "nv:adm:home")
        shop = command_center.CommandViewer([_entry("کارت", "x")], "nv:res:home")
        settings_back = {command_center.BACK_KEY: "nv:adm:settings"}
        _body, markup = command_center.results_panel(
            owner, "کارت", back_callback=command_center._stored_back(owner, settings_back)
        )
        self.assertEqual(_callbacks(markup)[-1], "nv:adm:settings")
        # A shop never goes back to an owner screen, whatever its stored state says.
        self.assertEqual(command_center._stored_back(shop, settings_back), "nv:res:home")

    async def test_a_page_turn_after_the_query_was_cleared_asks_again(self) -> None:
        callback, state = self._callback("cmd:r:a:p:1"), _state()
        viewer = command_center.CommandViewer([], "nv:adm:home")
        with patch.object(command_center, "load_command_viewer", AsyncMock(return_value=viewer)):
            await command_center.turn_results_page(callback, MagicMock(), SimpleNamespace(id=5), state)
        self.assertEqual(await state.get_state(), command_center.CommandCenterStates.query.state)

    async def test_a_page_turn_by_someone_who_lost_access_shows_nothing(self) -> None:
        callback, state = self._callback("cmd:r:a:p:1"), _state()
        await state.update_data(**{command_center.QUERY_KEY: "کارت"})
        with patch.object(command_center, "load_command_viewer", AsyncMock(return_value=None)):
            await command_center.turn_results_page(callback, MagicMock(), SimpleNamespace(id=5), state)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        callback.message.edit_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
