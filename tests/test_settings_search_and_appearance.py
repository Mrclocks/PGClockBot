"""Settings search, the appearance section, and legacy callback compatibility."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import admin_settings
from app.bot.nav_inline import admin_settings_hub_keyboard
from app.bot.nav_input import PROMPT_CHAT_ID, PROMPT_MSG_ID
from app.services.settings_button_labels import (
    PLATFORM_ONLY_BTN_KEYS,
    all_btn_field_keys,
    appearance_button_subs,
)
from app.services.settings_search import (
    SettingsSearchEntry,
    normalize_search_text,
    search_settings,
)
from app.services.users import DEFAULT_SETTINGS

UNREACHABLE_BEFORE = {
    "btn_buy_continue", "btn_adm_hub_ops", "btn_adm_hub_people",
    "btn_adm_hub_product", "btn_adm_hub_marketing", "btn_adm_hub_system", "btn_adm_dash",
    "btn_adm_reports", "btn_adm_resellers", "btn_adm_loyalty",
    "btn_adm_backup", "btn_adm_exit_preview", "btn_adm_edit_mode", "btn_adm_quick_links",
}


class SearchTests(unittest.TestCase):
    def test_arabic_letters_and_half_spaces_match(self):
        self.assertEqual(
            normalize_search_text("كارت‌به‌كارت"), normalize_search_text("کارت به کارت")
        )

    def test_search_finds_field_by_label_and_by_section(self):
        by_label = search_settings(admin_settings._SEARCH_ENTRIES, "شماره کارت")
        self.assertIn("card_number", [e.key for e in by_label])
        by_section = search_settings(admin_settings._SEARCH_ENTRIES, "قوانین")
        self.assertTrue(by_section)

    def test_every_word_must_match_and_results_are_bounded(self):
        entry = SettingsSearchEntry("نام فروشگاه", "shop", "فروشگاه", "identity", "هویت")
        self.assertEqual(search_settings([entry], "نام پرداخت"), [])
        many = [entry] * 50
        self.assertLessEqual(len(search_settings(many, "نام")), 8)
        self.assertEqual(search_settings(many, "   "), [])

    def test_hostile_query_is_escaped_in_panel_text_and_ignored_in_callbacks(self):
        body, markup = admin_settings._search_results_panel("<b>x</b>" * 20)
        self.assertNotIn("<b>x", body)
        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)


class AppearanceSectionTests(unittest.TestCase):
    def test_previously_unreachable_button_labels_are_editable(self):
        reachable = set(admin_settings.FIELDS)
        self.assertFalse(UNREACHABLE_BEFORE - reachable)
        self.assertTrue(UNREACHABLE_BEFORE <= set(DEFAULT_SETTINGS))

    def test_admin_label_keys_stay_hidden_from_shop_settings(self):
        keys = {f[0] for _, _, fields in appearance_button_subs() for f in fields}
        # «ادامه خرید» is a customer button a shop renames too, so it sits with the shop's labels.
        self.assertEqual(keys, UNREACHABLE_BEFORE - {"btn_buy_continue"})
        self.assertIn("btn_buy_continue", all_btn_field_keys(include_platform=False))
        admin_keys = {k for k in keys if k.startswith("btn_adm_")}
        self.assertTrue(admin_keys <= PLATFORM_ONLY_BTN_KEYS)
        self.assertFalse(admin_keys & all_btn_field_keys(include_platform=False))

    def test_each_field_key_has_one_owning_screen(self):
        seen: dict[str, str] = {}
        for sec_id, sec in admin_settings.SECTIONS.items():
            for sub in sec.get("subs") or []:
                if isinstance(sub[2], list):
                    for key, *_ in sub[2]:
                        if key.startswith("btn_force_join") or key == "btn_guides":
                            continue
                        self.assertNotIn(key, seen, f"{key} in {sec_id} and {seen.get(key)}")
                        seen[key] = sec_id

    def test_legacy_section_ids_still_exist(self):
        for sec_id in ("shop", "menu", "pay", "support", "access", "limits", "guides", "notify", "service"):
            self.assertIn(sec_id, admin_settings.SECTIONS)

    def test_hub_has_search_row_and_all_section_buttons(self):
        rows = admin_settings_hub_keyboard({}).inline_keyboard
        self.assertEqual(rows[0][0].callback_data, "adm:st:find")
        callbacks = {b.callback_data for row in rows for b in row}
        for sec in admin_settings.HUB_ORDER:
            self.assertIn(f"adm:st:sec:{sec}", callbacks)


class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_reply_edits_prompt_bubble(self):
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
        await state.update_data(**{PROMPT_MSG_ID: 5, PROMPT_CHAT_ID: 9})
        message = MagicMock()
        message.text = "کارت"
        message.chat = SimpleNamespace(id=9)
        message.bot.edit_message_text = AsyncMock()
        message.answer = AsyncMock()
        message.delete = AsyncMock()
        user = SimpleNamespace(role="admin", telegram_id=1)
        with patch.object(admin_settings, "_is_admin", return_value=True), patch.object(
            admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True)
        ):
            await admin_settings.settings_search_run(message, state, MagicMock(), user)
        message.answer.assert_not_awaited()
        self.assertEqual(message.bot.edit_message_text.await_args.kwargs["message_id"], 5)


if __name__ == "__main__":
    unittest.main()


class FuzzySearchTests(unittest.TestCase):
    def _keys(self, query: str) -> list[str]:
        return [e.key for e in search_settings(admin_settings._SEARCH_ENTRIES, query)]

    def test_one_typo_still_finds_the_field(self):
        self.assertIn("card_number", self._keys("شماره کارط"))

    def test_exact_matches_rank_before_typo_matches(self):
        entries = [
            SettingsSearchEntry("کارد", "s", "s", "a", "a", key="near"),
            SettingsSearchEntry("کارت", "s", "s", "b", "b", key="exact"),
        ]
        self.assertEqual([e.key for e in search_settings(entries, "کارت")], ["exact", "near"])

    def test_short_words_do_not_fuzzy_match(self):
        entry = SettingsSearchEntry("نام", "s", "s", "a", "a")
        self.assertEqual(search_settings([entry], "نخ"), [])

    def test_persian_and_latin_digits_are_equivalent(self):
        entry = SettingsSearchEntry("پورت 8080", "s", "s", "a", "a", key="port")
        self.assertEqual(len(search_settings([entry], "۸۰۸۰")), 1)
        entry_fa = SettingsSearchEntry("پورت ۸۰۸۰", "s", "s", "a", "a", key="port2")
        self.assertEqual(len(search_settings([entry_fa], "8080")), 1)
        self.assertEqual(normalize_search_text("٣٤"), "34")

    def test_unrelated_query_still_returns_nothing(self):
        self.assertEqual(self._keys("ززززز"), [])
