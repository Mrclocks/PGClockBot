"""UX20 feature unit tests — helpers, settings, templates, clone/gift/funnel."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.ux20 import (
    FUNNEL_STEPS,
    bot_deep_link,
    bot_tg_resolve_link,
    capacity_should_warn,
    compute_user_risk_flags,
    generate_charge_code,
    normalize_charge_code,
    parse_risk_flags,
    serialize_risk_flags,
    shop_bundle_to_json,
)


class Ux20HelpersTests(unittest.TestCase):
    def test_charge_code_normalize(self):
        self.assertEqual(normalize_charge_code("  ab cd-1 "), "ABCD-1")
        self.assertTrue(generate_charge_code("gift").startswith("GIFT-"))

    def test_risk_flags(self):
        self.assertEqual(parse_risk_flags("multi_trial, manual"), ["multi_trial", "manual"])
        self.assertEqual(serialize_risk_flags(["manual", "manual"]), "manual")
        flags = compute_user_risk_flags(
            trial_count=2, rejected_payments=3, open_tickets=3, existing=["manual"]
        )
        self.assertIn("multi_trial", flags)
        self.assertIn("repeat_reject", flags)
        self.assertIn("many_tickets", flags)
        self.assertIn("manual", flags)

    def test_capacity_warn(self):
        self.assertTrue(capacity_should_warn([{"pct": 85}], 80))
        self.assertFalse(capacity_should_warn([{"pct": 50}], 80))
        self.assertFalse(capacity_should_warn([None], 80))

    def test_deep_link(self):
        self.assertEqual(
            bot_deep_link("MyBot", "renew"),
            "https://t.me/MyBot?start=renew",
        )
        self.assertEqual(bot_deep_link("MyBot", ""), "https://t.me/MyBot")
        self.assertIsNone(bot_deep_link("", "wallet"))
        self.assertEqual(bot_tg_resolve_link("MyBot"), "tg://resolve?domain=MyBot")
        self.assertIsNone(bot_tg_resolve_link(""))

    def test_funnel_steps(self):
        self.assertIn("shop_open", FUNNEL_STEPS)
        self.assertIn("delivered", FUNNEL_STEPS)

    def test_shop_bundle_json(self):
        raw = shop_bundle_to_json({"format": "pgclock-shop-bundle", "version": 1})
        self.assertIn("pgclock-shop-bundle", raw)


class Ux20SettingsCatalogTests(unittest.TestCase):
    def test_default_settings_keys(self):
        from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS, TAB_SETTING_GROUPS

        for key in (
            "shop_maintenance_enabled",
            "admin_daily_report_enabled",
            "backup_schedule_enabled",
            "capacity_warn_pct",
            "receipt_auto_match_enabled",
            "funnel_tracking_enabled",
            "one_tap_renew_enabled",
        ):
            self.assertIn(key, DEFAULT_SETTINGS)
        self.assertNotIn("brand_primary_color", DEFAULT_SETTINGS)
        self.assertIn("وضعیت فروش", SETTING_GROUPS)
        self.assertIn("رسیدها", SETTING_GROUPS)
        self.assertIn("گزارش و عملیات", SETTING_GROUPS)
        self.assertIn("گزارش روزانه", SETTING_GROUPS)
        self.assertIn("بکاپ زمان‌بندی", SETTING_GROUPS)
        self.assertIn("وضعیت فروش", TAB_SETTING_GROUPS["payment"])
        self.assertIn("بکاپ زمان‌بندی", TAB_SETTING_GROUPS["backup"])
        self.assertIn("گزارش روزانه", TAB_SETTING_GROUPS["daily_report"])
        self.assertNotIn("admin_daily_report_enabled", TAB_SETTING_GROUPS["notifications"])
        # notifications still lists group name but daily keys moved out of that group
        ops_keys = {f[0] for f in SETTING_GROUPS["گزارش و عملیات"]}
        self.assertNotIn("admin_daily_report_enabled", ops_keys)

class Ux20TemplatePresenceTests(unittest.TestCase):
    def test_templates_exist(self):
        root = Path("app/web/templates")
        for name in (
            "home.html",
            "reseller_home.html",
            "pg_home.html",
            "finance.html",
            "_settings_links.html",
            "inbox.html",
        ):
            self.assertTrue((root / name).is_file(), name)
        self.assertFalse((root / "tools.html").is_file())
        self.assertFalse((root / "_home_inbox.html").is_file())
        self.assertTrue((root / "gift_codes.html").is_file())
        self.assertFalse((root / "magic_links.html").is_file())

    def test_home_overview_only_no_pg_open_or_old_shortcuts(self):
        home = Path("app/web/templates/_home_dash_body.html").read_text(encoding="utf-8")
        self.assertNotIn("ورود به پاسارگارد", home)
        self.assertNotIn("tools/gift-codes", home)
        self.assertNotIn("tools/export", home)
        self.assertNotIn("فانل خرید", home)
        self.assertNotIn("مراحل خرید", home)
        self.assertNotIn('href="/tools"', home)
        self.assertNotIn("_home_inbox.html", home)
        self.assertNotIn("میانبرها", home)
        self.assertNotIn("home-panel-bot", home)
        self.assertIn("home-pg-health", home)
        self.assertIn("_home_ops.html", home)
        self.assertIn("home-portal-bot", Path("app/web/templates/_home_ops.html").read_text(encoding="utf-8"))
        ops = Path("app/web/templates/_home_ops.html").read_text(encoding="utf-8")
        self.assertIn("صف کار", ops)
        self.assertIn("ac.entries", ops)
        self.assertIn("home-period", ops)
        panel = Path("app/web/templates/_funnel_panel.html").read_text(encoding="utf-8")
        self.assertIn("رفتار کاربر", panel)
        self.assertIn("home-panel-neutral", panel)

    def test_inbox_page_and_sidebar(self):
        inbox = Path("app/web/templates/inbox.html").read_text(encoding="utf-8")
        self.assertIn("مرکز اقدام امروز", inbox)
        self.assertIn("'مرکز اقدام'", inbox)
        self.assertIn("نماینده PAYG", inbox)
        self.assertIn("payg_risk", inbox)
        self.assertIn("ac.entries", inbox)
        self.assertNotIn("bot_token", inbox)
        self.assertNotIn("webhook_secret", inbox)
        from tests.sidebar_view import hrefs, owner_sidebar, sidebar

        # Inbox sits between dashboard and settings for the owner and for everyone else
        for system in (owner_sidebar()["system"], sidebar("reseller")["system"]):
            self.assertEqual(hrefs(system)[:2], ["/home", "/inbox"])
        home_src = Path("app/api/home_pages.py").read_text(encoding="utf-8")
        self.assertIn('"/inbox"', home_src)
        self.assertIn("build_inbox_context", home_src)

    def test_links_moved_to_bot_settings(self):
        tabs = Path("app/services/users.py").read_text(encoding="utf-8")
        self.assertIn('("links", "لینک‌های سریع")', tabs)
        links = Path("app/web/templates/_settings_links.html").read_text(encoding="utf-8")
        self.assertIn("لینک‌های سریع ربات", links)
        self.assertIn("data-copy=", links)

    def test_plans_page_does_not_carry_the_gift_code_page_or_its_old_modal(self):
        html = Path("app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertNotIn("modal-gift-codes", html)
        self.assertNotIn('href="/gift-codes"', html)

    def test_sidebar_no_tools_nav(self):
        html = Path("app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertNotIn(">ابزارها<", html)
        self.assertNotIn('href="/tools"', html)
        ux = Path("app/api/ux20_pages.py").read_text(encoding="utf-8")
        self.assertNotIn('"/tools"', ux)
        self.assertNotIn("tools_hub", ux)

    def test_settings_no_inline_export(self):
        html = Path("app/web/templates/settings.html").read_text(encoding="utf-8")
        self.assertNotIn("خروجی / ورودی تنظیمات فروشگاه", html)

    def test_backup_has_export_import(self):
        html = Path("app/web/templates/_settings_backup.html").read_text(encoding="utf-8")
        self.assertIn("خروجی / ورودی تنظیمات فروشگاه", html)
        self.assertIn("/settings/shop-export", html)
        self.assertIn("/settings/shop-import", html)
        self.assertNotIn("/tools/", html)
        export_at = html.find("خروجی / ورودی تنظیمات فروشگاه")
        create_at = html.find("backup-create-form")
        self.assertGreater(export_at, -1)
        self.assertLess(export_at, create_at)

    def test_behavior_panel_moved_out_of_finance(self):
        html = Path("app/web/templates/finance.html").read_text(encoding="utf-8")
        self.assertIn("finance?tab=reports", html)
        self.assertNotIn("finance?tab=behavior", html)
        self.assertNotIn("_funnel_panel.html", html)
        self.assertIn("_funnel_panel.html", Path("app/web/templates/funnel.html").read_text(encoding="utf-8"))
        self.assertIn("تحویل ناموفق", html)
        self.assertIn("retry-delivery", html)
        self.assertNotIn("staff-note", html)
        self.assertNotIn("finance-note-form", html)
        self.assertIn("data-receipt-open", html)
        self.assertIn("modal-receipt", html)
        self.assertNotIn('class="receipt-thumb"', html)
        # Shared panel: neutral chrome + count stats with icons (no rates/abandon).
        panel = Path("app/web/templates/_funnel_panel.html").read_text(encoding="utf-8")
        self.assertIn("home-panel-neutral", panel)
        self.assertNotIn("home-panel-bot", panel)
        self.assertNotIn("rates", panel)
        self.assertNotIn("abandoned_plans", panel)
        self.assertNotIn("پلن‌های رهاشده", panel)
        self.assertNotIn("٪", panel)
        self.assertNotIn("تبدیل", panel)
        self.assertIn("stat-ico", panel)
        self.assertEqual(panel.count("stat-ico"), 5)
        ux = Path("app/services/ux20.py").read_text(encoding="utf-8")
        self.assertNotIn("_funnel_pct", ux)
        self.assertNotIn("_abandoned_plans", ux)
        self.assertNotIn("abandoned_plans", ux)

    def test_settings_icons_distinct(self):
        macros = Path("app/web/templates/macros.html").read_text(encoding="utf-8")
        self.assertIn("panel-settings", macros)
        self.assertIn("bot-settings", macros)
        from app.services.panel_nav import build_sidebar, viewer_from_context

        # Panel settings: sliders; bot settings: robot face (not identical gear).
        owner = build_sidebar(viewer_from_context({"role": "admin"}, {}, is_owner=True), path="/home")
        icons = {i.key: i.icon for s in owner for i in s.items}
        self.assertEqual((icons["system_settings"], icons["bot_settings"]), ("sliders", "bot"))
        settings = Path("app/web/templates/settings.html").read_text(encoding="utf-8")
        self.assertIn("panel-settings", settings)
        self.assertIn("bot-settings", settings)
        self.assertNotIn("brand_primary_color", Path("app/web/templates/base.html").read_text(encoding="utf-8"))
        self.assertNotIn("brand_primary_color", Path("app/api/app.py").read_text(encoding="utf-8"))

    def test_finance_delivery_tab(self):
        html = Path("app/web/templates/finance.html").read_text(encoding="utf-8")
        self.assertIn("تحویل ناموفق", html)
        self.assertIn("retry-delivery", html)

    def test_plans_clone_button(self):
        html = Path("app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("/plans/{{ p.id }}/clone", html)

    def test_preview_no_purchase_simulator(self):
        html = Path("app/web/templates/_tg_preview_chat.html").read_text(encoding="utf-8")
        js = Path("app/web/templates/_tg_preview_chat_js.html").read_text(encoding="utf-8")
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertNotIn("pv-sim-buy", html)
        self.assertNotIn("شبیه‌ساز خرید", html)
        self.assertNotIn("tg-sim-bar", html)
        self.assertNotIn("simStep", js)
        self.assertNotIn("pv-sim-buy", js)
        self.assertNotIn(".tg-sim-bar", css)

    def test_backup_verify_ui(self):
        html = Path("app/web/templates/_settings_backup.html").read_text(encoding="utf-8")
        self.assertIn("/backup/verify-last", html)
        self.assertIn("backup-schedule-form", html)


class Ux20ModelsMigrationTests(unittest.TestCase):
    def test_models_have_new_fields(self):
        from app.db import models

        self.assertTrue(hasattr(models.BotUser, "staff_note"))
        self.assertTrue(hasattr(models.BotUser, "risk_flags"))
        self.assertFalse(hasattr(models.Order, "staff_note"))
        self.assertTrue(hasattr(models.UserService, "renew_nudge_sent_at"))
        self.assertTrue(hasattr(models.ResellerProfile, "capacity_warned_at"))
        self.assertTrue(hasattr(models, "DeliveryFailure"))
        self.assertTrue(hasattr(models, "ChargeCode"))
        self.assertTrue(hasattr(models, "FunnelEvent"))

    def test_alembic_revision_chain(self):
        text = Path("alembic/versions/0009_ux20_ops_features.py").read_text(encoding="utf-8")
        self.assertIn('revision: str = "0009_ux20_ops_features"', text)
        self.assertIn("0008_loyalty_discounts", text)


class Ux20RoutesRegistrationTests(unittest.TestCase):
    def test_ux20_pages_registered(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("register_ux20_pages", src)
        ux = Path("app/api/ux20_pages.py").read_text(encoding="utf-8")
        self.assertNotIn('"/tools"', ux)
        self.assertNotIn("tools_hub", ux)
        self.assertIn('"/plans/gift-codes"', ux)
        self.assertIn('"/settings/shop-export"', ux)
        self.assertIn("settings?tab=backup", ux)

    def test_settings_backup_no_get_all_settings_shadow(self):
        """Regression: local import of get_all_settings in backup tab 500'd all settings."""
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        # The buggy pattern was a local import inside the backup branch.
        self.assertNotIn(
            "from app.services.users import SETTING_GROUPS, TAB_SETTING_GROUPS, get_all_settings",
            src,
        )

    def test_scheduler_jobs(self):
        src = Path("app/jobs/scheduler.py").read_text(encoding="utf-8")
        self.assertIn("run_scheduled_backup", src)
        self.assertIn("run_admin_daily_report", src)
        self.assertIn("scheduled_backup", src)


class Ux20AsyncServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_clone_plan_permission(self):
        from app.services.ux20 import clone_plan

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        with self.assertRaises(ValueError):
            await clone_plan(session, 1, owner_reseller_id=None)

    async def test_action_center_shape(self):
        from app.services.ux20 import build_action_center

        session = AsyncMock()

        async def _exec(q):
            m = MagicMock()
            # count queries return scalar
            m.scalar.return_value = 0
            m.all.return_value = []
            return m

        session.execute = AsyncMock(side_effect=_exec)
        out = await build_action_center(session, reseller_id=None, expire_days=3)
        self.assertIn("entries", out)
        self.assertNotIn("items", out)
        self.assertFalse(out["has_items"])


class FormatNumberSafetyTests(unittest.TestCase):
    def test_undefined_does_not_raise(self):
        from jinja2 import Undefined

        from app.services.formatting import format_number

        self.assertEqual(format_number(Undefined(name="shop_open")), "—")
        self.assertEqual(format_number(None), "—")
        self.assertEqual(format_number(12), "12")

    def test_home_funnel_empty_dict_renders(self):
        from jinja2 import BaseLoader, Environment

        from app.services.formatting import format_number

        env = Environment(loader=BaseLoader())
        env.filters["num"] = format_number
        tpl = env.from_string(
            "{{ funnel.get('shop_open', 0) | num }}|{{ funnel.shop_open | num }}"
        )
        self.assertEqual(tpl.render(funnel={}), "0|—")


class ButtonStyleTests(unittest.TestCase):
    def test_review_and_buy_styles(self):
        from app.bot.keyboards import order_review, payment_review, plan_actions

        ok, no = order_review(1).inline_keyboard[0]
        self.assertEqual(ok.style, "success")
        self.assertEqual(no.style, "danger")
        pok, pno = payment_review(2).inline_keyboard[0]
        self.assertEqual(pok.style, "success")
        self.assertEqual(pno.style, "danger")
        self.assertEqual(plan_actions(3).inline_keyboard[0][0].style, "primary")

    def test_reply_keyboard_styles(self):
        from app.bot.keyboards import Role, main_reply_keyboard

        kb = main_reply_keyboard(Role.USER.value, has_services=True, ui={})
        styles = {b.style for row in kb.keyboard for b in row}
        self.assertIn("primary", styles)
        self.assertIn("success", styles)

    def test_settings_override_styles(self):
        from app.bot.keyboards import pay_methods, plan_actions
        from app.services.button_styles import get_button_style, setting_key

        ui = {setting_key("buy_continue"): "danger", setting_key("pay_wallet"): ""}
        self.assertEqual(plan_actions(9, ui).inline_keyboard[0][0].style, "danger")
        self.assertEqual(get_button_style(ui, "pay_wallet"), "")
        markup = pay_methods(
            1,
            {
                **ui,
                "pay_wallet_enabled": "1",
                "btn_pay_wallet": "کیف پول",
                "btn_cancel": "انصراف",
            },
        )
        wallet_btn = markup.inline_keyboard[0][0]
        self.assertFalse(getattr(wallet_btn, "style", None))

    def test_colors_tab_wired(self):
        from app.services.button_styles import BUTTON_STYLE_CATALOG, grouped_catalog
        from app.services.resellers import RESELLER_SETTINGS_TABS
        from app.services.users import DEFAULT_SETTINGS, SETTINGS_TABS, keys_for_tab

        self.assertIn(("colors", "رنگ دکمه‌ها"), SETTINGS_TABS)
        self.assertIn(("colors", "رنگ دکمه‌ها"), RESELLER_SETTINGS_TABS)
        self.assertIn(("appearance", "هویت ربات"), SETTINGS_TABS)
        self.assertIn(("appearance", "هویت ربات"), RESELLER_SETTINGS_TABS)
        keys = keys_for_tab("colors")
        for item in BUTTON_STYLE_CATALOG:
            k = f"btn_style_{item['id']}"
            self.assertIn(k, keys)
            self.assertIn(k, DEFAULT_SETTINGS)
            self.assertEqual(DEFAULT_SETTINGS[k], item["default"])
        self.assertTrue(grouped_catalog())
        html = Path("app/web/templates/_settings_colors.html").read_text(encoding="utf-8")
        self.assertIn("btn-color-grid", html)
        self.assertIn("btn-color-card", html)
        self.assertIn("btn-color-select", html)
        self.assertIn("data-tone", html)
        self.assertNotIn("btn-color-swatch", html)
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn('.btn-color-card .ui-select[data-tone="success"]', css)
        js = Path("app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("dataset.tone", js)
        self.assertIn("دکمه‌های سراسری", "".join(g for g, _ in grouped_catalog()))
        self.assertIn("پلن نمایندگی", "".join(g for g, _ in grouped_catalog()))
        self.assertIn("btn_style_home", DEFAULT_SETTINGS)
        self.assertIn("btn_style_confirm", DEFAULT_SETTINGS)
        self.assertIn("btn_style_plan_res_fixed", DEFAULT_SETTINGS)
        self.assertIn("btn_style_plan_res_payg", DEFAULT_SETTINGS)
        from app.services.button_styles import get_button_style

        self.assertEqual(get_button_style({}, "rev_ok"), "success")
        self.assertEqual(get_button_style({}, "rev_no"), "danger")
        from app.services.button_styles import STYLE_OPTIONS

        self.assertEqual(STYLE_OPTIONS[0], ("", "سفید", "default"))
        self.assertNotIn("پیش‌فرض", "".join(label for _v, label, _t in STYLE_OPTIONS))
        settings_html = Path("app/web/templates/_settings_colors.html").read_text(encoding="utf-8")
        # The actual invariant: no *style option* is labeled "پیش‌فرض" (that's
        # already asserted on STYLE_OPTIONS itself above). A later caption
        # legitimately uses the word in prose ("رنگ نوع پلن پیش‌فرض زیرمنوی
        # همان نوع است" — explaining inheritance, not naming a color option),
        # so scope this check to the rendered <option> tags only.
        for opt_line in re.findall(r"<option\b[^>]*>[^<]*", settings_html):
            self.assertNotIn("پیش‌فرض", opt_line)
        self.assertIn("style_options", settings_html)
        self.assertIn("colors_page_sections", settings_html)
        labels = "".join(item["label"] for item in BUTTON_STYLE_CATALOG)
        self.assertIn("پلن PAYG نماینده", labels)
        self.assertNotIn("Pay As You Go", labels)
        # Expanded catalog covers user/admin/reseller reply hubs.
        ids = {item["id"] for item in BUTTON_STYLE_CATALOG}
        for needed in (
            "shop_kind_trial",
            "adm_tickets",
            "pg_stats",
            "res_dash",
            "loy_referral",
            "wallet_tx",
            "backup_create",
        ):
            self.assertIn(needed, ids)
        self.assertGreaterEqual(len(BUTTON_STYLE_CATALOG), 100)
        settings = Path("app/web/templates/settings.html").read_text(encoding="utf-8")
        self.assertIn('tab == \'colors\'', settings)
        self.assertIn("'colors'", settings)
        self.assertIn("can-preview", settings)
        self.assertIn("has-preview", Path("app/web/static/panel.css").read_text(encoding="utf-8"))
        self.assertIn("data-tg-preview-open", Path("app/web/templates/_tg_preview_chat.html").read_text(encoding="utf-8"))
        preview_js = Path("app/web/templates/_tg_preview_chat_js.html").read_text(encoding="utf-8")
        self.assertNotIn("tab === 'colors'", preview_js)
        self.assertIn("styleTone", preview_js)
        self.assertIn("hasOwnProperty.call(values", preview_js)
        self.assertIn('data-tone', preview_js)
        self.assertIn("openPreview", preview_js)
        self.assertIn("DEBOUNCE_MS", preview_js)
        self.assertIn('.tg-key[data-tone="success"]', css)
        self.assertIn("receipt-modal-frame", css)
        admin_py = Path("app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn('_style(ui, "confirm"', admin_py)
        self.assertIn('_style(ui, "reject"', admin_py)
        self.assertNotIn('style="success"', admin_py.split("def _order_actions", 1)[1].split("\n\n", 1)[0])
        self.assertNotIn("فانل خرید", Path("app/web/templates/funnel.html").read_text(encoding="utf-8"))
        self.assertIn("رفتار کاربر", Path("app/web/templates/funnel.html").read_text(encoding="utf-8"))
        self.assertNotIn("/orders/{order_id}/staff-note", Path("app/api/ux20_pages.py").read_text(encoding="utf-8"))

    def test_reseller_plan_kind_styles(self):
        from app.bot.keyboards import admin_plan_kind_keyboard
        from app.services.button_styles import setting_key

        kb = admin_plan_kind_keyboard("resellers", ui={})
        fixed, payg = kb.inline_keyboard[0][0], kb.inline_keyboard[0][1]
        self.assertEqual(fixed.style, "primary")
        self.assertEqual(payg.style, "primary")
        custom = {
            setting_key("plan_res_fixed"): "success",
            setting_key("plan_res_payg"): "danger",
        }
        kb2 = admin_plan_kind_keyboard("resellers", ui=custom)
        self.assertEqual(kb2.inline_keyboard[0][0].style, "success")
        self.assertEqual(kb2.inline_keyboard[0][1].style, "danger")

    def test_global_home_back_footer_styles(self):
        from app.bot.keyboards import Role, main_reply_keyboard, wallet_reply_keyboard
        from app.services.button_styles import setting_key

        ui = {setting_key("home"): "primary", setting_key("back"): "danger"}
        main = main_reply_keyboard(Role.USER.value, has_services=False, ui=ui)
        home_btn = main.keyboard[-1][0]
        self.assertEqual(home_btn.style, "primary")
        wallet = wallet_reply_keyboard(ui)
        back_btn, home_btn2 = wallet.keyboard[-1]
        self.assertEqual(back_btn.style, "danger")
        self.assertEqual(home_btn2.style, "primary")


class Ux20VersionTests(unittest.TestCase):
    def test_version_aligned(self):
        from app.version import __version__
        from app.services.updates import is_same_or_newer

        self.assertEqual(Path("VERSION").read_text().strip(), __version__)
        self.assertTrue(is_same_or_newer(__version__, "0.1.0"))
        notes = Path("app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn(f'"{__version__}"', notes)
        self.assertIn('"0.1.0"', notes)

    def test_bot_tickets_controllable_from_panel(self):
        src = Path("app/api/panel_tickets_pages.py").read_text(encoding="utf-8")
        self.assertIn('"/tickets/bot/{ticket_id}/reply"', src)
        self.assertIn('"/tickets/bot/{ticket_id}/close"', src)
        self.assertIn("_bot_ticket_in_scope", src)
        self.assertIn("notify_ticket_message", src)
        html = Path("app/web/templates/tickets.html").read_text(encoding="utf-8")
        self.assertIn("تیکت‌های کاربران ربات", html)
        self.assertIn("/tickets/bot/{{ t.id }}/reply", html)
        self.assertIn("ارسال به تلگرام کاربر", html)

    def test_export_skips_secrets(self):
        from app.services.ux20 import _is_secret_setting

        for key in ("bot_token", "webhook_secret", "backup_telegram_bot_token"):
            self.assertTrue(_is_secret_setting(key), key)
        self.assertFalse(_is_secret_setting("welcome_text"))
        inbox = Path("app/services/panel_inbox.py").read_text(encoding="utf-8")
        self.assertIn("never includes bot tokens", inbox)


if __name__ == "__main__":
    unittest.main()
