"""Bot settings: all button labels editable in-bot, categorized + security filters."""

from __future__ import annotations

import unittest


class BotButtonLabelCoverageTests(unittest.TestCase):
    def test_admin_exposes_every_default_btn_label(self):
        from app.bot.handlers.admin_settings import FIELDS
        from app.services.users import DEFAULT_SETTINGS

        # Retired: Mini App is MenuButton «Open» only — label not editable in-bot.
        retired = frozenset({"btn_miniapp"})
        expected = {
            k
            for k in DEFAULT_SETTINGS
            if k.startswith("btn_")
            and not k.startswith("btn_style_")
            and k not in retired
        }
        missing = expected - set(FIELDS)
        self.assertFalse(missing, f"admin FIELDS missing button keys: {sorted(missing)}")
        for key in expected:
            self.assertEqual(FIELDS[key][2], "text", key)
        self.assertNotIn("btn_miniapp", FIELDS)

    def test_reseller_excludes_platform_only_buttons(self):
        from app.bot.handlers.reseller_settings import FIELDS
        from app.services.settings_button_labels import (
            PLATFORM_ONLY_BTN_KEYS,
            all_btn_field_keys,
        )

        shop_keys = all_btn_field_keys(include_platform=False)
        self.assertTrue(shop_keys)
        self.assertTrue(PLATFORM_ONLY_BTN_KEYS.isdisjoint(shop_keys))
        for key in PLATFORM_ONLY_BTN_KEYS:
            self.assertNotIn(key, FIELDS, key)
        # Reseller still edits their own hub label + customer buttons.
        self.assertIn("btn_reseller", FIELDS)
        self.assertIn("btn_loyalty", FIELDS)
        self.assertIn("btn_pay_card", FIELDS)
        self.assertNotIn("btn_reseller_apply", FIELDS)
        self.assertNotIn("btn_admin", FIELDS)
        self.assertNotIn("btn_miniapp", FIELDS)

    def test_categories_stay_within_screen_budget(self):
        from app.services.settings_button_labels import shop_button_subs

        for include_platform in (True, False):
            for sub_id, title, fields in shop_button_subs(
                include_platform=include_platform
            ):
                self.assertLessEqual(
                    len(fields),
                    8,
                    f"{sub_id}/{title} has {len(fields)} fields",
                )
                self.assertTrue(fields)

    def test_labels_section_holds_the_categorized_button_subs(self):
        from app.bot.handlers.admin_settings import SECTIONS as admin_s
        from app.bot.handlers.reseller_settings import SECTIONS as res_s

        for sections, platform in ((admin_s, True), (res_s, False)):
            # «نام و متن‌ها» holds message texts only; button labels live in their own section.
            self.assertEqual(
                {s[0] for s in sections["shop"]["subs"]} - {"invite"},
                {"identity", "texts", "guide_texts", "result_texts", "status_texts"},
            )
            sub_ids = {s[0] for s in sections["labels"]["subs"]}
            self.assertIn("btn_main", sub_ids)
            self.assertIn("btn_loyalty_sub", sub_ids)
            self.assertNotIn("btn_labels", sub_ids)
            if platform:
                self.assertIn("btn_adm_ops", sub_ids)
                self.assertIn("btn_adm_sys", sub_ids)
            else:
                self.assertNotIn("btn_adm_ops", sub_ids)
                self.assertNotIn("btn_adm_sys", sub_ids)

    def test_pay_method_screens_include_button_labels(self):
        from app.bot.handlers.admin_settings import FIELDS as admin_f
        from app.bot.handlers.reseller_settings import FIELDS as res_f

        for key in (
            "btn_pay_wallet",
            "btn_pay_discount",
            "btn_pay_card",
            "btn_pay_gateway",
            "btn_pay_crypto",
            "btn_pay_stars",
            "btn_pay_psp",
            "btn_pay_bbc",
        ):
            self.assertIn(key, admin_f, key)
            self.assertIn(key, res_f, key)

    def test_button_edit_still_uses_premium_icon_path(self):
        from pathlib import Path

        for rel in (
            "app/bot/handlers/admin_settings.py",
            "app/bot/handlers/reseller_settings.py",
        ):
            src = Path(rel).read_text(encoding="utf-8")
            self.assertIn("is_button_label_key", src)
            self.assertIn("pack_setting_from_message", src)
            self.assertIn("icon_custom_emoji_id", src)

    def test_web_shop_settings_uses_same_platform_block(self):
        from pathlib import Path

        src = Path("app/api/shop_settings.py").read_text(encoding="utf-8")
        self.assertIn("PLATFORM_ONLY_BTN_KEYS", src)
        self.assertNotIn("_shop_btn_block", src)


class BotSettingsHubIATestsRegression(unittest.TestCase):
    """Keep hub IA invariants after expanding button catalogs."""

    def test_admin_hub_order_unchanged(self):
        from app.bot.handlers.admin_settings import HUB_ORDER

        self.assertEqual(
            HUB_ORDER,
            ["shop", "menu", "labels", "pay", "access", "limits", "buy", "support", "guides", "notify"],
        )
        self.assertNotIn("service", HUB_ORDER)

    def test_limits_owns_force_join_buttons(self):
        from app.bot.handlers.admin_settings import FIELDS, _owner_screen_for_key

        self.assertEqual(_owner_screen_for_key("btn_force_join"), ("limits", "force"))
        self.assertIn("btn_force_join_check", FIELDS)


if __name__ == "__main__":
    unittest.main()
