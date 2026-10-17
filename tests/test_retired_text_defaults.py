"""Reworded defaults reach installs that never edited them; edited texts and old keyboards keep working."""

from __future__ import annotations

from connection_monitor_support import MonitorDatabaseCase

from app.bot.keyboards import reply_action_map
from app.db.models import ResellerSetting, Setting
from app.services.retired_text_defaults import RETIRED_TEXT_DEFAULTS
from app.services.users import DEFAULT_SETTINGS, clear_settings_cache, get_all_settings

OLD_PRODUCT_LABEL = "📦 محصول و PG"
OLD_PEOPLE_CAPTION = "کاربران و نمایندگان."


class RetiredDefaultRefreshTests(MonitorDatabaseCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)

    async def test_an_untouched_old_default_shows_the_current_wording(self) -> None:
        async with self.Session() as session:
            session.add_all([
                Setting(key="btn_adm_hub_product", value=OLD_PRODUCT_LABEL),
                Setting(key="screen_caption_a_people", value=OLD_PEOPLE_CAPTION),
            ])
            await session.commit()
            ui = await get_all_settings(session)
        self.assertEqual(ui["btn_adm_hub_product"], DEFAULT_SETTINGS["btn_adm_hub_product"])
        self.assertEqual(ui["screen_caption_a_people"], DEFAULT_SETTINGS["screen_caption_a_people"])

    async def test_an_edited_text_stays_as_the_admin_wrote_it(self) -> None:
        async with self.Session() as session:
            session.add(Setting(key="btn_adm_hub_product", value="📦 محصولات من"))
            await session.commit()
            ui = await get_all_settings(session)
        self.assertEqual(ui["btn_adm_hub_product"], "📦 محصولات من")

    async def test_each_shop_follows_only_its_own_stored_value(self) -> None:
        async with self.Session() as session:
            session.add_all([
                ResellerSetting(reseller_user_id=2, key="screen_caption_a_people", value=OLD_PEOPLE_CAPTION),
                ResellerSetting(reseller_user_id=3, key="screen_caption_a_people", value="توضیح فروشگاه سه"),
            ])
            await session.commit()
            shop2 = await get_all_settings(session, reseller_id=2)
            shop3 = await get_all_settings(session, reseller_id=3)
        self.assertEqual(shop2["screen_caption_a_people"], DEFAULT_SETTINGS["screen_caption_a_people"])
        self.assertEqual(shop3["screen_caption_a_people"], "توضیح فروشگاه سه")


class RetiredDefaultTableTests(MonitorDatabaseCase):
    async def test_every_retired_entry_names_a_current_default_with_new_wording(self) -> None:
        for key, previous in RETIRED_TEXT_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertIn(key, DEFAULT_SETTINGS)
                self.assertNotIn(DEFAULT_SETTINGS[key], previous)

    async def test_an_old_label_on_a_stale_keyboard_still_opens_its_menu(self) -> None:
        mapping = reply_action_map("admin", ui=dict(DEFAULT_SETTINGS), include_submenus=True)
        self.assertEqual(mapping.get(OLD_PRODUCT_LABEL), "adm_hub_product")
        self.assertEqual(mapping.get("📈 گزارشات"), "adm_reports")
