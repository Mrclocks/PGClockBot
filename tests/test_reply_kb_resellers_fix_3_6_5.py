"""3.6.6 — fix نمایندگان label collision; shop chrome on reply; no static inline hubs."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Version365Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.6.8"', notes)


class ResellersLabelCollisionTests(unittest.TestCase):
    def test_admin_hub_maps_to_adm_resellers(self):
        from app.bot.keyboards import reply_action_map

        ui = {
            "btn_back": "⬅️ بازگشت",
            "btn_menu_home": "🏠 منوی اصلی",
            "menu_layout": "compact",
        }
        mapping = reply_action_map("admin", ui=ui, include_submenus=True, is_reseller_bot=False)
        self.assertEqual(mapping["🤝 نمایندگان"], "adm_resellers")
        self.assertEqual(mapping["🤝 فقط نمایندگان"], "bc_aud_resellers")
        self.assertNotEqual(mapping["🤝 نمایندگان"], "bc_aud_resellers")

    def test_broadcast_keyboard_unique_label(self):
        from app.bot.keyboards import admin_broadcast_reply_keyboard, admin_reply_keyboard

        ui = {"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی", "menu_layout": "compact"}
        admin_flat = [b.text for row in admin_reply_keyboard(ui).keyboard for b in row]
        bc_flat = [b.text for row in admin_broadcast_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("🤝 نمایندگان", admin_flat)
        self.assertIn("🤝 فقط نمایندگان", bc_flat)
        self.assertNotIn("🤝 نمایندگان", bc_flat)

    def test_classic_and_compact_both_reply(self):
        from app.bot.keyboards import admin_reply_keyboard, admin_resellers_reply_keyboard

        for layout in ("classic", "compact"):
            ui = {
                "menu_layout": layout,
                "btn_back": "⬅️ بازگشت",
                "btn_menu_home": "🏠 منوی اصلی",
            }
            admin = admin_reply_keyboard(ui)
            res = admin_resellers_reply_keyboard(ui)
            self.assertTrue(admin.keyboard)
            self.assertTrue(res.keyboard)
            flat = [b.text for row in admin.keyboard for b in row]
            self.assertIn("🤝 نمایندگان", flat)
            if layout == "compact":
                # paired rows (except possible odd last + footer)
                self.assertGreaterEqual(len(admin.keyboard[0]), 1)
            else:
                self.assertEqual(len(admin.keyboard[0]), 1)


class ShopChromeReplyTests(unittest.TestCase):
    def test_plans_keyboard_names_only(self):
        from app.bot.keyboards import plans_keyboard, shop_reply_keyboard

        class P:
            id = 1
            name = "Demo"
            price = 10000
            is_trial = False

        kb = plans_keyboard([P()], custom_enabled=True, wholesale_enabled=True)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Demo" in t for t in texts))
        self.assertFalse(any("دلخواه" in t for t in texts))
        self.assertFalse(any("عمده" in t for t in texts))
        # Inline back to kind picker is intentional chrome
        self.assertTrue(any("بازگشت" in t for t in texts))

        shop = shop_reply_keyboard(
            {"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"},
        )
        sflat = [b.text for row in shop.keyboard for b in row]
        self.assertNotIn("✨ پلن دلخواه", sflat)
        self.assertNotIn("عمده", sflat)
        self.assertIn("⬅️ بازگشت", sflat)

    def test_legacy_hubs_empty(self):
        from app.bot.keyboards import admin_main_menu, admin_resellers_menu

        self.assertEqual(admin_main_menu().inline_keyboard, [])
        self.assertEqual(admin_resellers_menu().inline_keyboard, [])

    def test_support_contacts_url_only(self):
        from app.bot.keyboards import support_contacts_keyboard

        kb = support_contacts_keyboard(
            [{"title": "Support", "telegram": "@support_bot"}],
            {"btn_back": "⬅️ بازگشت"},
        )
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Support" in t for t in texts))
        self.assertFalse(any("تیکت" in t for t in texts))
        self.assertFalse(any("بازگشت" in t for t in texts))


class CancelBroadcastSkipTests(unittest.TestCase):
    def test_global_cancel_skips_fsm_to_state_handlers(self):
        src = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("aiogram.dispatcher.event.bases", src)
        chunk = src.split("async def global_cancel_restore")[1].split("@router.")[0]
        self.assertIn("if current is not None", chunk)
        self.assertIn("SkipHandler", chunk)
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        chunk = admin.split("async def adm_broadcast_send")[1].split("async def")[0]
        self.assertIn("is_cancel_text", chunk)
        self.assertIn("admin_broadcast_reply_keyboard", chunk)


if __name__ == "__main__":
    unittest.main()
