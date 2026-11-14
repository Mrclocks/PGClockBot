"""Hub buttons that link to screens in other handlers take their label and color from settings."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.bot.nav_inline import (
    admin_broadcast_hub_keyboard,
    admin_marketing_hub_keyboard,
    admin_system_hub_keyboard,
    pg_hub_keyboard,
    reseller_group_hub_keyboard,
)
from app.services.button_styles import CATALOG_BY_ID, allowed_style_setting_keys
from app.services.screen_editor import EditorActor, can_edit_label
from app.services.settings_button_labels import PLATFORM_ONLY_BTN_KEYS, all_btn_field_keys
from app.services.users import DEFAULT_SETTINGS

ADMIN_KEYS = (
    "btn_adm_trial_users",
    "btn_adm_broadcast_history",
    "btn_adm_staff_audit",
    "btn_adm_service_shop_review",
    "btn_pg_hosts",
    "btn_pg_inbounds",
    "btn_pg_admins",
)
SHOP_KEYS = ("btn_res_trial_users", "btn_res_staff_audit")
OWNER = EditorActor(user_id=1, shop_id=None)
SHOP_OWNER = EditorActor(user_id=5, shop_id=5)


def _button(markup, callback: str):
    return next(b for row in markup.inline_keyboard for b in row if b.callback_data == callback)


def _ui(**labels: str) -> dict[str, str]:
    ui = dict(DEFAULT_SETTINGS)
    ui.update(labels)
    return ui


class RenamedLabelTests(unittest.TestCase):
    def test_admin_hubs_show_the_renamed_labels(self) -> None:
        ui = _ui(
            btn_adm_trial_users="تست‌ها",
            btn_adm_staff_audit="کارنامه",
            btn_adm_service_shop_review="سرویس بی‌فروشگاه",
            btn_adm_broadcast_history="ارسال‌های قبلی",
        )
        self.assertEqual(_button(admin_marketing_hub_keyboard(ui), "tru:l:all:0").text, "تست‌ها")
        system = admin_system_hub_keyboard(ui)
        self.assertEqual(_button(system, "saud:l:0").text, "کارنامه")
        self.assertEqual(_button(system, "ssr:l:0").text, "سرویس بی‌فروشگاه")
        self.assertEqual(_button(admin_broadcast_hub_keyboard(ui), "bch:l").text, "ارسال‌های قبلی")

    def test_the_pasarguard_hub_shows_the_renamed_labels(self) -> None:
        ui = _ui(btn_pg_hosts="میزبان‌ها", btn_pg_inbounds="ورودی‌ها", btn_pg_admins="مدیران")
        markup = pg_hub_keyboard(ui, features=frozenset({"pg_hosts", "pg_inbounds", "pg_admins"}))
        self.assertEqual(_button(markup, "adm:pg:hosts").text, "میزبان‌ها")
        self.assertEqual(_button(markup, "adm:pg:inbounds").text, "ورودی‌ها")
        self.assertEqual(_button(markup, "adm:pg:admins").text, "مدیران")

    def test_a_shop_panel_uses_the_shops_own_labels_not_the_admin_ones(self) -> None:
        ui = _ui(
            btn_res_trial_users="تست فروشگاه",
            btn_res_staff_audit="کارنامه فروشگاه",
            btn_adm_trial_users="نباید دیده شود",
            btn_adm_staff_audit="نباید دیده شود",
        )
        profile = MagicMock()
        with (
            patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=[("res_users", "👥")]),
            patch("app.bot.nav_inline.has_bot_perm", return_value=True),
        ):
            customers = reseller_group_hub_keyboard("customers", profile, ui)
            account = reseller_group_hub_keyboard("account", profile, ui)
        self.assertEqual(_button(customers, "tru:l:all:0").text, "تست فروشگاه")
        self.assertEqual(_button(account, "saud:l:0").text, "کارنامه فروشگاه")

    def test_a_blank_label_falls_back_to_the_default_text(self) -> None:
        markup = admin_marketing_hub_keyboard(_ui(btn_adm_trial_users="  "))
        self.assertEqual(_button(markup, "tru:l:all:0").text, "🧪 کاربران تست")

    def test_a_chosen_color_reaches_the_button(self) -> None:
        markup = admin_marketing_hub_keyboard(_ui(btn_style_adm_trial_users="danger"))
        self.assertEqual(_button(markup, "tru:l:all:0").style, "danger")


class EditableEverywhereTests(unittest.TestCase):
    def test_every_new_label_is_listed_in_the_label_settings(self) -> None:
        listed = all_btn_field_keys(include_platform=True)
        for key in (*ADMIN_KEYS, *SHOP_KEYS):
            self.assertIn(key, listed, key)
            self.assertIn(key, DEFAULT_SETTINGS, key)
            self.assertIn(key.removeprefix("btn_"), CATALOG_BY_ID, key)

    def test_admin_labels_and_colors_stay_platform_only(self) -> None:
        shop_colors = allowed_style_setting_keys(for_reseller=True)
        for key in ADMIN_KEYS:
            self.assertIn(key, PLATFORM_ONLY_BTN_KEYS, key)
            self.assertFalse(can_edit_label(SHOP_OWNER, key), key)
            self.assertTrue(can_edit_label(OWNER, key), key)
            self.assertNotIn(f"btn_style_{key.removeprefix('btn_')}", shop_colors, key)

    def test_a_shop_owner_may_rename_and_recolor_the_shop_copies(self) -> None:
        shop_colors = allowed_style_setting_keys(for_reseller=True)
        for key in SHOP_KEYS:
            self.assertNotIn(key, PLATFORM_ONLY_BTN_KEYS, key)
            self.assertTrue(can_edit_label(SHOP_OWNER, key), key)
            self.assertIn(f"btn_style_{key.removeprefix('btn_')}", shop_colors, key)

    def test_cashback_and_gift_codes_no_longer_share_an_icon(self) -> None:
        self.assertNotEqual(
            DEFAULT_SETTINGS["btn_adm_cashback"].split()[0], DEFAULT_SETTINGS["btn_adm_gift_codes"].split()[0]
        )
