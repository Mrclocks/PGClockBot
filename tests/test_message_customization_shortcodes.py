"""Focused tests: shortcodes, safe HTML, settings categories, orders/payments UX."""

from __future__ import annotations

import unittest
from pathlib import Path


class ShortcodeSecurityTests(unittest.TestCase):
    def test_known_and_unknown(self):
        from app.services.safe_format import safe_format
        from app.services.shortcodes import render_user_message

        self.assertEqual(safe_format("hi {name}", name="Ali"), "hi Ali")
        self.assertEqual(safe_format("hi {missing}", name="Ali"), "hi {missing}")
        self.assertEqual(
            render_user_message("سفارش #{order_id}", "fallback", order_id=9),
            "سفارش #9",
        )

    def test_no_arbitrary_execution(self):
        from app.services.safe_format import looks_like_format_injection, safe_format

        evil = "{order_id.__class__}"
        self.assertTrue(looks_like_format_injection(evil))
        self.assertEqual(safe_format(evil, order_id=1), evil)
        self.assertEqual(
            safe_format("{name!r}", name="x"),
            "{name!r}",
        )

    def test_html_escape_dynamic(self):
        from app.services.shortcodes import render_user_message

        out = render_user_message(
            "سلام <b>{name}</b>",
            "fallback",
            name="<script>x</script>",
        )
        self.assertIn("<b>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)

    def test_empty_and_fallback(self):
        from app.services.shortcodes import render_user_message

        self.assertEqual(render_user_message("", "DEF", name="a"), "DEF")
        self.assertEqual(render_user_message("   ", "DEF"), "DEF")

    def test_context_aware_registry(self):
        from app.services.shortcodes import MESSAGE_SHORTCODES, shortcodes_for

        welcome = {k for k, _ in shortcodes_for("welcome_text")}
        self.assertEqual(welcome, {"name"})
        purchase = {k for k, _ in shortcodes_for("purchase_success_text")}
        self.assertIn("order_id", purchase)
        self.assertIn("traffic_remaining", purchase)
        self.assertNotIn("card", purchase)
        self.assertIn("card", MESSAGE_SHORTCODES["card_pay_text"])
        self.assertEqual(shortcodes_for("guide_text"), [])

    def test_preview_sample_only(self):
        from app.services.shortcodes import SAMPLE_VALUES, preview_fill

        self.assertIn("نمونه", SAMPLE_VALUES["name"])
        filled = preview_fill("سلام {name} — {traffic_remaining}")
        self.assertIn("نمونه کاربر", filled)
        self.assertIn("73 GB", filled)
        self.assertNotIn("BOT_TOKEN", filled)
        self.assertNotIn("telegram_id", filled.lower())


class SettingsReorgTests(unittest.TestCase):
    def test_categories_and_aliases(self):
        from app.services.users import (
            SETTINGS_TAB_ALIASES,
            SETTINGS_TABS,
            TAB_SETTING_GROUPS,
            keys_for_tab,
        )

        tabs = dict(SETTINGS_TABS)
        self.assertIn("messages", tabs)
        self.assertNotIn("payment", tabs)
        self.assertNotIn("billing", tabs)
        self.assertNotIn("supports", tabs)
        self.assertIn("services", tabs)
        self.assertNotIn("welcome", tabs)
        self.assertNotIn("buttons", tabs)
        self.assertNotIn("qr", tabs)
        self.assertNotIn("naming", tabs)
        self.assertEqual(SETTINGS_TAB_ALIASES["welcome"], "messages")
        self.assertEqual(SETTINGS_TAB_ALIASES["naming"], "services")
        msg_keys = keys_for_tab("messages")
        self.assertIn("welcome_text", msg_keys)
        self.assertIn("purchase_success_text", msg_keys)
        self.assertIn("btn_shop", msg_keys)
        self.assertIn("qr_caption", msg_keys)
        self.assertNotIn("card_number", msg_keys)
        self.assertNotIn("support_text", msg_keys)
        pay_keys = keys_for_tab("finance")
        self.assertIn("card_number", pay_keys)
        self.assertIn("pay_wallet_enabled", pay_keys)
        self.assertIn("billing_enabled", pay_keys)
        self.assertNotIn("welcome_text", pay_keys)
        self.assertIn("نام‌گذاری سرویس در پاسارگارد", TAB_SETTING_GROUPS["services"])
        self.assertIn("کانال اجباری", TAB_SETTING_GROUPS["services"])

    def test_reseller_tabs_aligned(self):
        from app.services.resellers import RESELLER_SETTINGS_TABS

        keys = {k for k, _ in RESELLER_SETTINGS_TABS}
        self.assertIn("messages", keys)
        self.assertIn("services", keys)
        self.assertNotIn("payment", keys)
        self.assertNotIn("welcome", keys)
        self.assertNotIn("naming", keys)

    def test_help_ui_and_preview_wired(self):
        field = Path("app/web/templates/_settings_field.html").read_text(encoding="utf-8")
        self.assertIn("data-sc-help", field)
        self.assertIn("data-sc-copy", field)
        preview = Path("app/web/templates/_tg_preview_chat_js.html").read_text(encoding="utf-8")
        self.assertIn("SAMPLE", preview)
        self.assertIn("tgHtml", preview)
        self.assertIn("نمونه کاربر", preview)


class OrdersPaymentsUxTests(unittest.TestCase):
    def test_orders_show_payment_lifecycle(self):
        src = Path("app/web/templates/orders.html").read_text(encoding="utf-8")
        self.assertIn("payments_list_by_order", src)
        self.assertIn("pay_status", src)
        self.assertIn("تخفیف", src)
        self.assertIn('name="status"', src)
        self.assertIn('name="pay_status"', src)

    def test_payments_filters_and_order_link(self):
        src = Path("app/web/templates/payments.html").read_text(encoding="utf-8")
        self.assertIn('name="status"', src)
        self.assertIn('name="method"', src)
        self.assertIn('href="/orders?q=', src)
        self.assertIn('href="/settings?tab=payment"', src)

    def test_approve_paths_unchanged(self):
        app = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('require_perm("orders")', app)
        self.assertIn('require_perm("payments")', app)
        self.assertIn('/orders/{order_id}/approve', app)
        self.assertIn('/payments/{payment_id}/approve', app)


if __name__ == "__main__":
    unittest.main()
