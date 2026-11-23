"""Phase 2 — backup defaults exclude .env; with-env needs explicit confirm."""

from __future__ import annotations

import inspect
import unittest


class CreateBackupDefaultTests(unittest.TestCase):
    def test_create_backup_default_excludes_env(self):
        from app.services import backup as backup_mod

        sig = inspect.signature(backup_mod.create_backup)
        self.assertFalse(sig.parameters["include_env"].default)


class BotBackupConfirmTests(unittest.TestCase):
    def test_reply_keyboard_default_is_safe(self):
        from app.bot.reply_keyboards import _admin_backup_submenu_entries

        entries = dict(_admin_backup_submenu_entries())
        self.assertEqual(entries["backup_create"], "🆕 ساخت بکاپ")
        self.assertIn("backup_create_env", entries)
        self.assertNotIn("backup_create_noenv", entries)

    def test_hub_text_says_default_without_env(self):
        from app.bot.handlers.admin_backup import _hub_text

        text = _hub_text([])
        self.assertIn("بدون", text)
        self.assertIn(".env", text)


class ResellerBackupAclLockTests(unittest.TestCase):

    def test_reseller_bot_cannot_map_backup_labels(self):
        from app.bot.reply_keyboards import reply_action_map

        ui = {"btn_menu_home": "🏠 منوی اصلی", "btn_back": "⬅️ بازگشت"}
        mapping = reply_action_map(
            "admin", ui=ui, include_submenus=True, is_reseller_bot=True
        )
        self.assertNotIn("🆕 ساخت بکاپ", mapping)
        self.assertNotIn("🆕 بکاپ + .env", mapping)
        self.assertNotIn("💾 بکاپ / ریستور", mapping)


if __name__ == "__main__":
    unittest.main()
