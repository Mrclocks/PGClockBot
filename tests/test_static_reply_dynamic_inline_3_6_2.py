"""3.6.2 — static menus on reply KB; dynamic lists inline; admin/reseller isolation."""

from __future__ import annotations

import unittest

from app.bot.keyboards import (
    admin_backup_reply_keyboard,
    admin_broadcast_reply_keyboard,
    admin_home,
    admin_plans_list_keyboard,
    admin_plans_reply_keyboard,
    admin_reply_keyboard,
    backup_files_keyboard,
    reply_action_map,
    reseller_home,
    reseller_settings_reply_keyboard,
)


class StaticReplyDynamicInline362Tests(unittest.TestCase):
    def test_version(self):
        from pathlib import Path

        from app.version import __version__
        from app.services.updates import is_same_or_newer

        self.assertTrue(is_same_or_newer(__version__, "0.1.0"))
        root = Path(__file__).resolve().parents[1]
        self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), __version__)

    def test_user_cannot_map_admin_labels(self):
        ui = {"btn_menu_home": "🏠 منوی اصلی", "btn_back": "⬅️ بازگشت", "menu_order": "shop,wallet"}
        mapping = reply_action_map("user", ui=ui, include_submenus=True, is_reseller_bot=False)
        self.assertNotIn("📊 داشبورد", mapping)
        self.assertNotIn("🗓 عملیات روزانه", mapping)
        self.assertNotIn("👤 افراد", mapping)
        self.assertNotIn("💾 بکاپ / ریستور", mapping)
        self.assertNotIn("🆕 ساخت بکاپ", mapping)
        self.assertNotIn("🆕 بکاپ + .env", mapping)
        self.assertNotIn("🖥 پاسارگارد", mapping)
        self.assertNotIn("🏠 خانه نماینده", mapping)

    def test_admin_labels_blocked_on_reseller_bot(self):
        ui = {"btn_menu_home": "🏠 منوی اصلی", "btn_back": "⬅️ بازگشت"}
        for role in ("admin", "user"):
            mapping = reply_action_map(
                role, ui=ui, include_submenus=True, is_reseller_bot=True
            )
            self.assertNotIn("📊 داشبورد", mapping)
            self.assertNotIn("🗓 عملیات روزانه", mapping)
            self.assertNotIn("🛠 سیستم", mapping)
            self.assertNotIn("🆕 ساخت بکاپ", mapping)
            self.assertNotIn("🆕 بکاپ + .env", mapping)
            self.assertNotIn("👥 کاربران VPN", mapping)
            # No platform admin entry forged onto reseller-bot maps
            admin_btn = mapping.get("🛠 پنل ادمین") or mapping.get("پنل ادمین")
            self.assertIsNone(admin_btn)

    def test_admin_map_has_static_hubs(self):
        ui = {
            "menu_layout": "compact",
            "btn_adm_orders": "🛒 سفارش‌ها",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        mapping = reply_action_map("admin", ui=ui, include_submenus=True, is_reseller_bot=False)
        self.assertEqual(mapping["🆕 ساخت بکاپ"], "backup_create")
        self.assertEqual(mapping["🆕 بکاپ + .env"], "backup_create_env")
        self.assertEqual(mapping["📢 همه"], "bc_aud_all")
        self.assertEqual(mapping["➕ افزودن پلن"], "adm_plans_add")
        flat = [b.text for row in admin_backup_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("🆕 ساخت بکاپ", flat)
        self.assertIn("🆕 بکاپ + .env", flat)
        self.assertIn("⬅️ بازگشت", flat)
        self.assertTrue(admin_broadcast_reply_keyboard(ui).keyboard)
        self.assertTrue(admin_plans_reply_keyboard(ui).keyboard)

    def test_reseller_bot_map_has_settings_sections(self):
        from types import SimpleNamespace

        ui = {"menu_layout": "compact", "btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"}
        profile = SimpleNamespace(
            is_active=True,
            web_permissions="dashboard,plans,orders,payments,tickets,stats,shop_settings",
        )
        mapping = reply_action_map(
            "reseller",
            ui=ui,
            include_submenus=True,
            is_reseller_bot=True,
            profile=profile,
        )
        self.assertEqual(mapping["⚙️ تنظیمات فروشگاه"], "res_settings")
        self.assertEqual(mapping["فروشگاه"], "res_st_shop")
        self.assertEqual(mapping["ربات"], "res_st_bot")
        self.assertEqual(mapping["➕ پلن جدید"], "res_plan_add")
        flat = [b.text for row in reseller_settings_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("فروشگاه", flat)
        self.assertIn("⬅️ بازگشت", flat)

    def test_reseller_map_fail_closed_without_profile(self):
        ui = {"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"}
        mapping = reply_action_map(
            "reseller", ui=ui, include_submenus=True, is_reseller_bot=True
        )
        self.assertNotIn("⚙️ تنظیمات فروشگاه", mapping)
        self.assertNotIn("💎 پلن‌های فروش", mapping)
        self.assertNotIn("🛒 سفارش‌های مشتریان", mapping)

    def test_reseller_map_respects_stripped_perms(self):
        from types import SimpleNamespace

        ui = {"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"}
        # orders+payments only — no soft-inject of plans/shop_settings.
        profile = SimpleNamespace(
            is_active=True,
            web_permissions="orders,payments",
        )
        mapping = reply_action_map(
            "reseller",
            ui=ui,
            include_submenus=True,
            is_reseller_bot=True,
            profile=profile,
        )
        self.assertIn("🛒 سفارش‌های مشتریان", mapping)
        self.assertIn("🧾 رسیدهای در انتظار", mapping)
        self.assertNotIn("💎 پلن‌های فروش", mapping)
        self.assertNotIn("⚙️ تنظیمات فروشگاه", mapping)
        # Thin hub: settings btn_reseller + legacy «پنل مدیریت» alias
        self.assertIn("🤝 پنل نماینده", mapping)
        self.assertIn("🤝 پنل مدیریت", mapping)
        self.assertIn("👥 مشتریان من", mapping)  # dashboard soft leaf still mapped
        self.assertNotIn("📊 آمار", mapping)
        self.assertNotIn("📊 آمار و کمیسیون", mapping)
        # Stale nav_mode=classic must not restore «خانه نماینده» as live path
        classic_map = reply_action_map(
            "reseller",
            ui={**ui, "nav_mode": "classic"},
            include_submenus=True,
            is_reseller_bot=True,
            profile=profile,
        )
        self.assertNotIn("🏠 خانه نماینده", classic_map)
        self.assertIn("🤝 پنل نماینده", classic_map)

    def test_legacy_hubs_no_longer_static_chrome(self):
        """Inline legacy hubs must not re-surface full static menus."""
        ah = admin_home()
        labels = [b.text for row in ah.inline_keyboard for b in row]
        self.assertNotIn("📊 داشبورد", labels)
        self.assertNotIn("💾 بکاپ / ریستور", labels)
        rh = reseller_home()
        rlabels = [b.text for row in rh.inline_keyboard for b in row]
        self.assertNotIn("🏠 خانه نماینده", rlabels)

    def test_dynamic_only_inline_helpers(self):
        class P:
            id = 1
            name = "Demo"
            is_active = True
            pg_template_id = 1
            pg_group_ids = ""

        plans_kb = admin_plans_list_keyboard([P()])
        texts = [b.text for row in plans_kb.inline_keyboard for b in row]
        self.assertTrue(any("Demo" in t for t in texts))
        self.assertFalse(any("پلن جدید" in t for t in texts))

        files = backup_files_keyboard(
            [{"id": "bk_abc123456789", "size_human": "1MB"}]
        )
        ftexts = [b.text for row in files.inline_keyboard for b in row]
        self.assertTrue(any("bk_abc" in t for t in ftexts))
        self.assertFalse(any("ساخت بکاپ" in t for t in ftexts))

    def test_admin_reply_still_has_core_entries(self):
        from app.bot.keyboards import admin_ops_reply_keyboard, admin_system_reply_keyboard

        ui = {
            "menu_layout": "compact",
            "btn_adm_orders": "🛒 سفارش‌ها",
            "btn_back": "⬅️ بازگشت",
            "btn_menu_home": "🏠 منوی اصلی",
        }
        flat = [b.text for row in admin_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("🗓 عملیات روزانه", flat)
        self.assertIn("🛠 سیستم", flat)
        self.assertNotIn("📊 داشبورد", flat)
        ops = [b.text for row in admin_ops_reply_keyboard(ui).keyboard for b in row]
        system = [b.text for row in admin_system_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("📊 داشبورد", ops)
        self.assertIn("💾 بکاپ / ریستور", system)

    def test_soft_admin_refuses_reseller_bot(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        src = (root / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("async def _refuse_admin", src)
        self.assertIn("async def _deny_unless_owner", src)
        self.assertIn("if is_reseller_bot:", src)
        self.assertIn("is_bot_owner_principal", src)
        self.assertIn("open_admin_backup_hub", src)
        self.assertIn("open_reseller_settings_hub", src)
        nav = (root / "app/bot/menu_nav.py").read_text(encoding="utf-8")
        self.assertIn("NAV_RESELLER_SETTINGS", nav)

    def test_block_middleware_still_on_admin_routers(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "app/bot/__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_BlockPlatformAdminOnResellerBot", src)
        self.assertIn("admin_backup.router", src)
        self.assertIn("admin_pg_users.router", src)

    def test_web_preview_mentions_static_reply(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        hint = (root / "app/web/templates/_tg_preview_chat.html").read_text(encoding="utf-8")
        self.assertIn("Reply Keyboard", hint)
        menu = (root / "app/web/templates/_settings_menu.html").read_text(encoding="utf-8")
        # Wave A: menu tab documents inline-first stable reply keyboard
        self.assertIn("اینلاین", menu)
        self.assertIn("کیبورد پایین", menu)


if __name__ == "__main__":
    unittest.main()
