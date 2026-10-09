"""Panel IA: domain settings modals + finance hub."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app/web/templates"


class DomainSettingsRedirectTests(unittest.TestCase):
    def test_bot_settings_tabs_slimmed(self):
        from app.services.users import SETTINGS_DOMAIN_REDIRECTS, SETTINGS_TABS

        keys = {k for k, _ in SETTINGS_TABS}
        for removed in ("payment", "supports", "billing", "reseller"):
            self.assertNotIn(removed, keys)
            self.assertIn(removed, SETTINGS_DOMAIN_REDIRECTS)
        self.assertIn("loyalty", SETTINGS_DOMAIN_REDIRECTS)
        self.assertEqual(SETTINGS_DOMAIN_REDIRECTS["reseller"], "/resellers")

    def test_shop_settings_tabs_slimmed(self):
        from app.services.resellers import (
            RESELLER_SETTINGS_TABS,
            SHOP_SETTINGS_DOMAIN_POST_TABS,
            SHOP_SETTINGS_DOMAIN_REDIRECTS,
        )

        keys = {k for k, _ in RESELLER_SETTINGS_TABS}
        self.assertNotIn("payment", keys)
        self.assertNotIn("supports", keys)
        self.assertEqual(
            set(SHOP_SETTINGS_DOMAIN_REDIRECTS),
            set(SHOP_SETTINGS_DOMAIN_POST_TABS),
        )
        self.assertIn("loyalty", SHOP_SETTINGS_DOMAIN_REDIRECTS)

    def test_nav_has_finance_not_separate_orders_payments(self):
        src = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.assertIn('href="/finance"', src)
        self.assertIn("مدیریت مالی", src)
        # Separate sidebar entries for orders/payments removed
        self.assertNotRegex(src, r'href="/orders"\s+class="nav-item')
        self.assertNotRegex(src, r'href="/payments"\s+class="nav-item')


class DomainModalTemplateTests(unittest.TestCase):
    def test_finance_page_modal(self):
        html = (TEMPLATES / "finance.html").read_text(encoding="utf-8")
        self.assertIn('data-modal-open="modal-finance-settings"', html)
        self.assertIn("section-tabs", html)
        self.assertIn("_modal_finance_settings.html", html)
        self.assertTrue((TEMPLATES / "_modal_finance_settings.html").is_file())

    def test_tickets_supports_modal(self):
        html = (TEMPLATES / "tickets.html").read_text(encoding="utf-8")
        self.assertIn("پشتیبان‌ها", html)
        self.assertIn('data-modal-open="modal-supports-settings"', html)
        self.assertIn("_modal_supports_settings.html", html)

    def test_loyalty_settings_modal_not_page_tab(self):
        html = (TEMPLATES / "loyalty.html").read_text(encoding="utf-8")
        self.assertIn('data-modal-open="modal-loyalty-settings"', html)
        self.assertIn("section-tabs", html)
        self.assertIn("باشگاه مشتریان", html)
        self.assertIn("home-panel-grid", html)
        self.assertIn("home-panel-neutral", html)
        self.assertIn("loyalty-overview", html)
        self.assertNotIn("tabs tabs-scroll", html)
        self.assertNotIn('href="/loyalty?tab=settings"', html)
        self.assertNotIn('href="/loyalty?tab=rules"', html)
        self.assertNotIn('href="/loyalty?tab=rewards"', html)
        self.assertNotIn('href="/loyalty?tab=tiers"', html)
        self.assertIn("_modal_loyalty_settings.html", html)
        modal = (TEMPLATES / "_modal_loyalty_settings.html").read_text(encoding="utf-8")
        self.assertIn('data-modal-tab="club"', modal)
        self.assertIn('data-modal-tab="referral"', modal)
        self.assertIn('data-modal-tab="rules"', modal)
        self.assertIn('data-modal-tab="rewards"', modal)
        self.assertIn('data-modal-tab="tiers"', modal)

    def test_search_bars_have_no_title_label(self):
        for name in ("users.html", "finance.html", "resellers.html", "pg_users.html"):
            html = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertNotIn("search-bar-label", html)

    def test_bot_settings_nav_is_last(self):
        src = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        bot = src.split("nav-section-bot", 1)[1].split("nav-section-pg", 1)[0]
        loyalty_i = bot.find("باشگاه مشتریان")
        vars_i = bot.find("متغیرهای پیام")
        settings_i = bot.find(">تنظیمات</span>")
        self.assertGreater(loyalty_i, 0)
        self.assertGreater(vars_i, loyalty_i)
        self.assertGreater(settings_i, vars_i)
        self.assertIn("/message-variables", bot)

    def test_settings_html_no_supports_or_payment_branches(self):
        html = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
        self.assertNotIn("tab == 'supports'", html)
        self.assertNotIn("tab == 'payment'", html)
        self.assertNotIn("tab == 'reseller'", html)

    def test_limits_notices_sit_above_related_groups(self):
        html = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
        limits = html.split("tab == 'limits'", 1)[1].split("tab == 'guides'", 1)[0]
        # Notices are injected next to their groups, not dumped before the form.
        self.assertIn("group_name == 'کانال اجباری'", limits)
        self.assertIn("group_name == 'قوانین ورود به ربات'", limits)
        self.assertIn("_forcejoin_admin_notice.html", limits)
        self.assertIn("_terms_admin_notice.html", limits)
        force_i = limits.find("group_name == 'کانال اجباری'")
        terms_i = limits.find("group_name == 'قوانین ورود به ربات'")
        self.assertGreater(force_i, 0)
        self.assertGreater(terms_i, force_i)
        shop = (TEMPLATES / "shop_settings.html").read_text(encoding="utf-8")
        self.assertIn("tab == 'limits' and group_name == 'کانال اجباری'", shop)
        self.assertIn("tab == 'limits' and group_name == 'قوانین ورود به ربات'", shop)
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".guides-editor .pay-dest-color .plan-color-card", css)

    def test_reseller_settings_live_on_resellers_page(self):
        html = (TEMPLATES / "resellers.html").read_text(encoding="utf-8")
        self.assertIn("تنظیمات نمایندگی", html)
        self.assertIn("show_reseller_apply", html)
        self.assertIn("reseller_panel_base_url", html)
        self.assertIn("reseller_pg_panel_base_url", html)
        self.assertIn("/resellers/panel-url", html)
        from app.services.users import SETTING_GROUPS, keys_for_tab

        self.assertNotIn("نمایندگی", SETTING_GROUPS)
        self.assertEqual(keys_for_tab("reseller"), set())

    def test_legacy_order_payment_templates_removed(self):
        self.assertFalse((TEMPLATES / "orders.html").exists())
        self.assertFalse((TEMPLATES / "payments.html").exists())

    def test_modal_tab_js_and_css(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("data-modal-tabs", js)
        self.assertIn("data-modal-tab-panel", js)
        self.assertIn("data-modal-close-strip", js)
        self.assertIn("data-modal-strip-keys", js)
        self.assertIn("settings-modal-panel", css)
        self.assertIn(".section-tabs .tab-btn", css)
        self.assertIn("settings-modal-flash", css)

    def test_domain_modals_soft_close_and_inline_flash(self):
        for name in (
            "_modal_finance_settings.html",
            "_modal_supports_settings.html",
            "_modal_loyalty_settings.html",
        ):
            html = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertIn("data-modal-strip-keys", html)
            self.assertIn("data-modal-close-strip", html)
            self.assertIn("settings-modal-flash", html)
            self.assertNotIn("data-modal-close-nav", html)
            # Keys must not live on the close-strip of the modal root
            # (that made tab clicks soft-close the dialog).
            self.assertNotRegex(
                html,
                r'class="ui-modal[^"]*"[^>]*data-modal-close-strip="',
            )


class DomainSettingGroupsTests(unittest.TestCase):
    def test_support_and_referral_groups_split(self):
        from app.services.users import SETTING_GROUPS, TAB_SETTING_GROUPS, keys_for_tab

        self.assertIn("متن پشتیبانی ربات", SETTING_GROUPS)
        self.assertIn("متن دعوت دوستان", SETTING_GROUPS)
        msg_keys = {f[0] for f in SETTING_GROUPS["متن پیام‌ها"]}
        self.assertNotIn("support_text", msg_keys)
        self.assertNotIn("referral_text", msg_keys)
        self.assertEqual(keys_for_tab("supports"), {"support_text"})
        self.assertEqual(keys_for_tab("loyalty"), {"referral_text"})
        # Required-referral moved from payment modal → limits tab
        self.assertEqual(
            keys_for_tab("payment") & {"referral_required", "referral_required_text"},
            set(),
        )
        self.assertEqual(
            keys_for_tab("limits") & {"referral_required", "referral_required_text"},
            {"referral_required", "referral_required_text"},
        )
        self.assertEqual(TAB_SETTING_GROUPS["limits"][0], "معرف اجباری")
        self.assertIn("روش‌های پرداخت", TAB_SETTING_GROUPS["payment"])
        self.assertIn("معرف اجباری", SETTING_GROUPS)
        self.assertEqual(
            {f[0] for f in SETTING_GROUPS["معرف اجباری"]},
            {"referral_required", "referral_required_text"},
        )
        self.assertEqual({f[0] for f in SETTING_GROUPS["متن دعوت دوستان"]}, {"referral_text"})

    def test_preview_js_drops_moved_message_fields(self):
        src = (TEMPLATES / "_tg_preview_chat_js.html").read_text(encoding="utf-8")
        self.assertNotIn("s_support_text", src)
        self.assertNotIn("s_referral_text", src)
        self.assertNotIn("s_payment_reject_text", src)


class SafeNextTests(unittest.TestCase):
    def test_safe_internal_next_blocks_open_redirects(self):
        from app.api.safe_next import safe_internal_next

        self.assertEqual(
            safe_internal_next("/finance?tab=orders&settings=payment", "/home"),
            "/finance?tab=orders&settings=payment",
        )
        self.assertEqual(safe_internal_next("//evil.example", "/home"), "/home")
        self.assertEqual(safe_internal_next("https://evil.example/", "/home"), "/home")
        self.assertEqual(safe_internal_next("/finance\nLocation: x", "/home"), "/home")
        self.assertEqual(safe_internal_next("/not-allowed", "/home"), "/home")
        self.assertEqual(safe_internal_next("  /tickets?supports=1  ", "/home"), "/tickets?supports=1")



class PanelSettingsNavPolishTests(unittest.TestCase):
    def test_panel_general_tab_and_pasarguard_removed(self):
        from app.services.users import (
            PANEL_SETTINGS_TABS,
            SETTINGS_DOMAIN_REDIRECTS,
            SETTINGS_TABS,
        )

        bot_keys = [k for k, _ in SETTINGS_TABS]
        self.assertNotIn("pasarguard", bot_keys)
        self.assertEqual(SETTINGS_DOMAIN_REDIRECTS.get("pasarguard"), "/settings?tab=bot")
        panel_keys = [k for k, _ in PANEL_SETTINGS_TABS]
        self.assertEqual(panel_keys[0], "general")
        self.assertIn("general", panel_keys)
        tabs = (TEMPLATES / "_panel_settings_tabs.html").read_text(encoding="utf-8")
        self.assertIn('href="/settings?tab=general"', tabs)
        # general link appears before backup
        self.assertLess(tabs.index("tab=general"), tabs.index("tab=backup"))
        bot = (TEMPLATES / "_settings_bot.html").read_text(encoding="utf-8")
        self.assertNotIn("وب و عمومی", bot)
        general = (TEMPLATES / "_settings_general.html").read_text(encoding="utf-8")
        self.assertIn("وب و عمومی", general)
        self.assertIn('name="WEB_PORT"', general)
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.assertIn('href="/settings?tab=general"', base)
        self.assertNotIn('href="/settings?tab=pasarguard"', base)
        self.assertIn('href="/settings?tab=bot"', base)

    def test_bot_secret_placeholders_are_clean_rtl(self):
        bot = (TEMPLATES / "_settings_bot.html").read_text(encoding="utf-8")
        # No mixed LTR bullets + «یا …» — that bidi-scrambles inside dir=ltr boxes.
        self.assertNotIn("•••• یا توکن جدید", bot)
        self.assertNotIn("•••• یا رمز جدید", bot)
        self.assertIn('placeholder="توکن جدید"', bot)
        self.assertIn('placeholder="رمز جدید"', bot)
        self.assertIn("خالی بماند تا توکن فعلی حفظ شود", bot)
        self.assertIn("خالی بماند تا رمز فعلی حفظ شود", bot)
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("padding-right: var(--space-2)", css)
        self.assertIn("padding-left: var(--space-5)", css)
        # Physical padding lives on the main .pw-field input rule (not auth-form).
        self.assertIn(
            ".pw-field input {\n  width: 100%;\n  margin-top: 0;",
            css,
        )
        field = css.split(".pw-field input {\n  width: 100%;", 1)[1].split("}", 1)[0]
        self.assertIn("padding-right: var(--space-2)", field)
        self.assertIn("padding-left: var(--space-5)", field)
        self.assertIn("text-align: right", field)
        ph = css.split(".pw-field input::placeholder {", 1)[1].split("}", 1)[0]
        self.assertIn("direction: rtl", ph)
        self.assertIn("text-align: right", ph)

    def test_magic_links_copy_inside_field(self):
        html = (TEMPLATES / "_settings_links.html").read_text(encoding="utf-8")
        self.assertIn("magic-link-field", html)
        self.assertIn("magic-link-copy", html)
        self.assertIn("magic-link-input", html)
        self.assertIn('dir="ltr"', html)
        self.assertNotIn("btn btn-ghost", html)
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        block = css.split(".magic-link-field {", 1)[1].split("}", 1)[0]
        # Flex LTR shell — chip is a sibling on the right, never absolute over the URL.
        self.assertIn("display: flex", block)
        self.assertIn("direction: ltr", block)
        self.assertNotIn("position: relative", block)
        self.assertIn("--magic-link-h: 52px", block)
        self.assertIn("--magic-copy-h: 34px", block)
        self.assertIn("--magic-copy-w: 4.75rem", block)
        inp = css.split(".magic-link-field .magic-link-input {", 1)[1].split("}", 1)[0]
        self.assertIn("text-overflow: ellipsis", inp)
        self.assertIn("text-align: left", inp)
        self.assertIn("font-size: 10px !important", inp)
        self.assertIn("flex: 1 1 auto", inp)
        btn = css.split(".magic-link-copy {", 1)[1].split("}", 1)[0]
        self.assertIn("flex: 0 0 var(--magic-copy-w)", btn)
        self.assertIn("width: var(--magic-copy-w)", btn)
        self.assertIn("height: var(--magic-copy-h)", btn)
        self.assertIn("justify-content: center", btn)
        logo = css.split(".brand-logo {", 1)[1].split("}", 1)[0]
        self.assertIn("var(--brand-logo-size)", logo)
        self.assertIn("--brand-logo-size: 40px", css)
        self.assertIn("--topbar-h: 58px", css)

    def test_pg_connection_settings_is_last_sidebar_item(self):
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        section = base.split('id="nav-panel-pg"', 1)[1].split("</div>", 1)[0]
        self.assertIn("تنظیمات اتصال", section)
        self.assertIn('href="/settings?tab=bot"', section)
        # Owner connection link must come after every other PG nav item.
        self.assertGreater(section.rindex("تنظیمات اتصال"), section.rindex("اینباند"))
        self.assertGreater(section.rindex('href="/settings?tab=bot"'), section.index('href="/pg"'))

if __name__ == "__main__":
    unittest.main()
