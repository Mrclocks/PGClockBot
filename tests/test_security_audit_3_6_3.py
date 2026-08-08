"""3.6.3 — shop isolation, permission gates, reply ACL hardening."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


class Version363Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.6.8"', notes)


class WebShopOrderIsolationTests(unittest.TestCase):
    def test_order_approve_blocks_admin_on_shop(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        # Platform admin must refuse tenant shop orders (match bot ordrev)
        self.assertIn(
            "این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل تأیید است",
            src,
        )
        self.assertIn(
            "این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل رد است",
            src,
        )
        self.assertIn('order.reseller_id and staff.get("role") == "admin"', src)

    def test_orders_list_platform_scoped(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        # After is_platform_admin branch filters reseller_id IS NULL
        self.assertIn("q = q.where(Order.reseller_id.is_(None))", src)

    def test_pending_receipt_needs_payments_perm(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('if "payments" not in perms:', src)
        self.assertIn("تأیید رسید نیاز به دسترسی «پرداخت‌ها» دارد", src)

    def test_dashboard_requires_dashboard_perm(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('Depends(require_perm("dashboard"))', src)

    def test_require_staff_applies_with_shop_settings(self):
        # Stronger fail-closed: empty parse stays empty; non-empty gets core keys
        authz = (ROOT / "app/services/authz.py").read_text(encoding="utf-8")
        self.assertIn("with_shop_settings(parsed) if parsed else parsed", authz)
        app_src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("resolve_shop_permissions_from_profile", app_src)


class BotShopIsolationTests(unittest.TestCase):
    def test_adm_orders_platform_only(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn(".where(Order.reseller_id.is_(None))", src)
        self.assertIn("if order.reseller_id:", src)
        self.assertIn("if order.reseller_id:", src)  # view + approve

    def test_adm_home_no_reply_kb_on_edit(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        # Must not pass ReplyKeyboardMarkup into edit_text
        home = src.split("async def adm_home")[1].split("async def adm_dash")[0]
        self.assertNotIn("edit_text(\n            f\"🛠", home)
        self.assertIn("safe_edit_text", home)
        self.assertIn("admin_reply_keyboard()", home)

    def test_adm_payments_platform_filter(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        pay = src.split("async def adm_payments")[1].split("async def adm_plans")[0]
        self.assertIn("Payment.is_wallet_topup.is_(True)", pay)
        self.assertIn("Order.reseller_id.is_(None)", pay)


class ResellerReplyAclTests(unittest.TestCase):
    def test_settings_hub_checks_perm(self):
        src = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        hub = src.split("async def open_reseller_settings_hub")[1].split(
            "async def open_reseller_plans_hub"
        )[0]
        self.assertIn('has_bot_perm(profile, "shop_settings")', hub)

    def test_soft_callback_surfaces_denial(self):
        src = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        soft = src.split("class _SoftCallback:")[1].split("class ReplyMenuTextFilter")[0]
        self.assertIn("edit_text", soft)

    def test_plans_static_on_reply(self):
        from app.bot.keyboards import reseller_plans_list_keyboard, reseller_plans_reply_keyboard

        flat = [b.text for row in reseller_plans_reply_keyboard().keyboard for b in row]
        self.assertIn("➕ پلن جدید", flat)
        self.assertIn("⬅️ بازگشت", flat)

        class P:
            id = 1
            name = "Demo"
            is_active = True
            price = 1000

        texts = [b.text for row in reseller_plans_list_keyboard([P()]).inline_keyboard for b in row]
        self.assertTrue(any("Demo" in t for t in texts))
        self.assertFalse(any("پلن جدید" in t for t in texts))

    def test_map_without_profile_forges_nothing(self):
        from app.bot.keyboards import reply_action_map

        mapping = reply_action_map(
            "reseller",
            ui={"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"},
            include_submenus=True,
            is_reseller_bot=True,
            profile=None,
        )
        self.assertNotIn("⚙️ تنظیمات فروشگاه", mapping)
        self.assertNotIn("💎 پلن‌های فروش", mapping)


class PreviewDefaultsTests(unittest.TestCase):
    def test_preview_js_uses_emoji_defaults(self):
        js = (ROOT / "app/web/templates/_tg_preview_chat_js.html").read_text(encoding="utf-8")
        self.assertIn("🟢🛒 خرید سرویس", js)
        self.assertIn("🔵📦 سرویس‌های من", js)


if __name__ == "__main__":
    unittest.main()
