"""Tests for user menu_order as single source of visibility."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.bot.keyboards import (
    DEFAULT_MENU_ORDER,
    REMOVED_MENU_KEYS,
    _menu_order,
    main_menu,
    sync_show_flags_for_order,
)


class MenuOrderTests(unittest.TestCase):
    def test_does_not_reinject_removed_items(self):
        ui = {
            "menu_order": "shop,wallet",
            "show_support": "1",
            "show_guide": "1",
            "show_faq": "1",
        }
        self.assertEqual(_menu_order(ui), ["shop", "wallet"])

    def test_strips_legacy_guide_faq(self):
        ui = {"menu_order": "shop,guide,faq,wallet"}
        self.assertEqual(_menu_order(ui), ["shop", "wallet"])
        self.assertTrue({"guide", "faq"} <= REMOVED_MENU_KEYS)
        self.assertNotIn("guide", DEFAULT_MENU_ORDER)
        self.assertNotIn("faq", DEFAULT_MENU_ORDER)

    def test_shop_always_present(self):
        self.assertEqual(_menu_order({"menu_order": "wallet,support"}), ["shop", "wallet", "support"])

    def test_shop_keeps_custom_position_when_present(self):
        """Shop is required in the menu but must stay movable (not forced to index 0)."""
        self.assertEqual(
            _menu_order({"menu_order": "wallet,shop,support"}),
            ["wallet", "shop", "support"],
        )
        self.assertEqual(
            _menu_order({"menu_order": "services,wallet,shop"}),
            ["services", "wallet", "shop"],
        )

    def test_sync_show_flags(self):
        flags = sync_show_flags_for_order(["shop", "wallet", "referral"])
        self.assertEqual(flags["show_wallet"], "1")
        self.assertEqual(flags["show_referral"], "1")
        self.assertEqual(flags["show_support"], "0")
        self.assertNotIn("show_faq", flags)
        self.assertNotIn("show_guide", flags)

    def test_main_menu_follows_order_not_stale_show_flags(self):
        ui = {
            "menu_order": "shop,loyalty",
            "show_wallet": "1",  # stale — must not appear
            "btn_shop": "خرید",
            "btn_loyalty": "باشگاه",
            "btn_wallet": "کیف پول",
            "menu_layout": "classic",
            "support_contacts": "[]",
        }
        with patch("app.bot.keyboards.get_settings") as gs:
            gs.return_value.miniapp_enabled = False
            gs.return_value.miniapp_url = ""
            markup = main_menu("user", has_services=False, ui=ui)
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertIn("خرید", labels)
        self.assertIn("باشگاه", labels)
        self.assertNotIn("کیف پول", labels)

    def test_legacy_referral_order_migrates_to_loyalty(self):
        self.assertEqual(
            _menu_order({"menu_order": "shop,referral,wallet"}),
            ["shop", "loyalty", "wallet"],
        )

    def test_compact_layout_pairs_all_buttons_including_shop_services(self):
        """Regression: shop/services used to force full-width rows even in compact."""
        ui = {
            "menu_order": "shop,services,support,wallet,loyalty",
            "menu_layout": "compact",
            "btn_shop": "خرید سرویس",
            "btn_services": "سرویس‌های من",
            "btn_support": "پشتیبانی",
            "btn_wallet": "کیف پول",
            "btn_loyalty": "باشگاه",
            "support_contacts": "[]",
            "show_reseller_apply": "0",
        }
        with patch("app.bot.keyboards.get_settings") as gs:
            gs.return_value.miniapp_enabled = False
            gs.return_value.miniapp_url = ""
            markup = main_menu("user", has_services=True, ui=ui)
        rows = markup.inline_keyboard
        self.assertEqual(len(rows[0]), 2, rows)
        self.assertEqual([b.text for b in rows[0]], ["خرید سرویس", "سرویس‌های من"])
        self.assertEqual(len(rows[1]), 2, rows)
        self.assertEqual([b.text for b in rows[1]], ["پشتیبانی", "کیف پول"])
        self.assertEqual(len(rows[2]), 1)
        self.assertEqual(rows[2][0].text, "باشگاه")

    def test_classic_layout_one_button_per_row(self):
        ui = {
            "menu_order": "shop,wallet,support",
            "menu_layout": "classic",
            "btn_shop": "خرید",
            "btn_wallet": "کیف",
            "btn_support": "پشتیبانی",
            "support_contacts": "[]",
        }
        with patch("app.bot.keyboards.get_settings") as gs:
            gs.return_value.miniapp_enabled = False
            gs.return_value.miniapp_url = ""
            markup = main_menu("user", has_services=False, ui=ui)
        self.assertTrue(all(len(row) == 1 for row in markup.inline_keyboard))

    def test_menu_tab_has_no_display_toggles(self):
        from app.services.users import TAB_SETTING_GROUPS, keys_for_tab

        self.assertEqual(TAB_SETTING_GROUPS.get("menu"), [])
        self.assertEqual(keys_for_tab("menu"), set())


if __name__ == "__main__":
    unittest.main()
