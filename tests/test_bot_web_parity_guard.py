"""Every web menu entry and every setting is classified for web/bot parity (docs/BOT_PARITY_MATRIX.md).

Both directions are checked. A new sidebar item, marketing section or bot setting fails here until
it is placed in one of the lists below, with where it lives on the other side or why it stays on one
side; so does a new web setting that the owner bot or the shop bot cannot edit. This keeps a feature
from quietly landing in only the web panel or only the bot. When a gap is closed in the bot, its entry
must leave the gap list, so the lists always describe the code.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.handlers import admin_settings, reseller_settings
from app.services import marketing_nav, panel_nav
from app.services.resellers import RESELLER_SETTINGS_TABS, SHOP_SETTINGS_DOMAIN_POST_TABS
from app.services.screen_titles import is_screen_text_key, platform_only_screen_text_keys
from app.services.service_import_config import PLATFORM_ONLY_KEYS as IMPORT_PLATFORM_ONLY_KEYS
from app.services.settings_button_labels import PLATFORM_ONLY_BTN_KEYS
from app.services.users import PLATFORM_ONLY_SETTING_GROUPS, SETTING_GROUPS, TAB_SETTING_GROUPS

# Sidebar item -> where the bot offers it.
NAV_IN_BOT: dict[str, str] = {
    "home": "پنل ادمین › عملیات روزانه › داشبورد",
    "shop_overview": "پنل نماینده › کارهای روزانه › خانه نماینده",
    "my_subscription": "پنل نماینده › حساب من › تمدید و بسته‌ها",
    "users": "پنل ادمین › افراد › کاربران",
    "customers": "پنل نماینده › مشتریان › مشتریان من",
    "my_resellers": "پنل نماینده › مشتریان › افزودن نماینده",
    "resellers": "پنل ادمین › افراد › نمایندگان",
    "finance": "سفارش‌ها، رسیدها، لغو سرویس و شکست تحویل در عملیات روزانه",
    "plans": "پنل ادمین › محصول › پلن‌ها؛ پنل نماینده › پلن‌های فروش",
    "tickets": "تیکت‌ها در عملیات روزانه",
    "staff_actions": "گزارش کارکنان (سیستم ادمین، حساب نماینده)",
    "bot_settings": "پنل ادمین › سیستم › تنظیمات",
    "shop_settings": "پنل نماینده › فروشگاه من › تنظیمات فروشگاه",
    "pg_overview": "پاسارگارد › نمای کلی",
    "pg_users": "پاسارگارد › کاربران، ساخت، جستجو",
    "pg_nodes": "پاسارگارد › نودها",
    "pg_groups": "پاسارگارد › گروه",
    "pg_templates": "پاسارگارد › تمپلیت",
    "pg_hosts": "پاسارگارد › هاست‌ها",
    "pg_inbounds": "پاسارگارد › اینباندها (فقط خواندنی، مانند وب)",
    "connection_test": "پنل ادمین › وضعیت اتصال",
}
# Sidebar item -> what the bot has and what it lacks.
NAV_PARTLY_IN_BOT: dict[str, str] = {
    "inbox": "ربات فقط صف رسید، تیکت، لغو و تحویل را نشان می‌دهد؛ هشدارهای سیستمی فقط در وب",
    "pg_admins": "ربات فهرست ادمین‌ها را نشان می‌دهد؛ ساخت، حذف و تمدید فقط در وب",
    "pg_tickets": "تیکت‌های داخلی پنل برای کارکنان پاسارگارد هنوز در ربات نیست",
    "service_cleanup": "پیش‌نمایش، مهلت و معافیت در ربات؛ قانون (روزها و هشدارها) فقط در وب",
}
# Sidebar item -> why it stays on the web.
NAV_WEB_ONLY: dict[str, str] = {
    "system_settings": "SSL، دامنه، به‌روزرسانی، بکاپ کامل و وب‌پنل: عملیات سیستمی با فرم و فایل",
    "account": "نام کاربری و رمز ورود به وب",
}

# Marketing section -> where the bot offers it, partly, or why not.
MARKETING_IN_BOT: dict[str, str] = {
    marketing_nav.CLUB: "باشگاه: نمای کلی، قوانین، جوایز، سطوح، تنظیمات",
    marketing_nav.CASHBACK: "کش‌بک: روشن/خاموش ویژگی و قانون",
    marketing_nav.TRIAL_USERS: "کاربران تست: فهرست و پیام",
    marketing_nav.BROADCAST: "پیام همگانی و تاریخچه",
    marketing_nav.SHOP_BROADCAST: "پیام گروهی فروشگاه",
}
MARKETING_PARTLY_IN_BOT: dict[str, str] = {
    marketing_nav.CLUB_TRANSACTIONS: "تغییر دستی امتیاز در کارت مشتری؛ فهرست تراکنش‌ها فقط در وب",
    marketing_nav.GIFT_CODES: "روشن/خاموش در ربات مالک و فروشگاه؛ ساخت و محدودیت پلن فقط در وب",
    marketing_nav.CAMPAIGNS: "شروع/توقف در ربات مالک و فروشگاه؛ ساخت فقط در وب",
    marketing_nav.DISCOUNTS: "فهرست و توقف در ربات؛ ساخت فقط در وب",
    marketing_nav.FUNNEL: "خلاصهٔ ۷ روزه در ربات؛ نمودار فقط در وب",
}

MARKETING_WEB_ONLY: dict[str, str] = {
    marketing_nav.RANDOM_DISCOUNTS: "مدیریت بازه و محافظت مبلغ در وب؛ اعمال خودکار و استفاده از کد در ربات و مینی‌اپ",
}

# Owner-bot settings that are not fields of the web settings tabs, and where the web edits them.
BOT_SETTINGS_OUTSIDE_WEB_TABS: dict[str, str] = {
    **{key: "وب: مالی › مقصدهای پرداخت" for key in (
        "card_holder", "card_number", "crypto_address", "crypto_asset", "crypto_network", "gateway_link", "gateway_name",
    )},
    **{key: "وب: پلن‌ها › پلن دلخواه" for key in (
        "custom_plan_max_days", "custom_plan_max_gb", "custom_plan_min_days", "custom_plan_min_gb",
        "custom_plan_price_per_day", "custom_plan_price_per_gb",
    )},
    # The web keeps several API gateways in psp_gateways; these legacy keys only feed the first one
    # while that list is empty, so bot edits stop counting once the web saves a gateway.
    **{key: "وب: درگاه‌های API (psp_gateways)؛ ربات فقط درگاه قدیمی" for key in (
        "psp_merchant_id", "psp_pay_url", "psp_provider", "psp_sandbox", "psp_sunpay_request_method",
    )},
    # The web edits each alert threshold as one number with a unit select beside it.
    **{key: "وب: هشدار سرویس کاربر › واحد کنار عدد آستانه" for key in (
        "user_alert_low_traffic_unit", "user_alert_low_time_unit",
    )},
    "custom_service_name_enabled": "وب: تنظیمات ربات › قالب نام‌گذاری",
    "miniapp_shops_enabled": "وب: تنظیمات ربات › مینی‌اپ (فرم اختصاصی)",
    "miniapp_notice": "وب: تنظیمات ربات › مینی‌اپ (فرم اختصاصی)",
}

# Web settings the bots edit outside the settings field lists, and where.
BOT_OTHER_EDITORS: dict[str, str] = {
    "menu_layout": "تنظیمات › ظاهر منو › چیدمان کیبورد",
    "connection_guides": "تنظیمات › آموزش اتصال (ویرایشگر آموزش‌ها، gde:*)",
}

# Web settings that stay on the web for both bots, and why.
WEB_ONLY_SETTINGS: dict[str, str] = {
    "qr_background": "آپلود تصویر با پیش‌نمایش",
    "admin_daily_report_template": "انتخاب‌گر متغیر و پیش‌نمایش گزارش",
    "service_import_paths": "مسیرهای پنل واحد؛ فقط مالک در وب",
    "service_import_admins": "ادمین‌های پنل واحد؛ فقط مالک در وب",
    # From a chat, these would let one stolen Telegram account send every backup and secret away.
    **{key: "امنیت: گیرنده، توکن و .env بکاپ فقط در وب" for key in (
        "backup_include_env_scheduled", "backup_telegram_allow_env", "backup_telegram_custom_bot",
        "backup_telegram_bot_token", "backup_telegram_chat_ids",
    )},
}

# Web settings the owner bot cannot edit yet: open work, shrinking as the bot catches up.
OWNER_BOT_GAPS: dict[str, str] = {
    "wallet_topup_presets": "مبالغ آمادهٔ شارژ (فهرست)",
    **{key: "فهرست چندتایی مقصدهای پرداخت" for key in (
        "payment_cards", "payment_gateways", "psp_gateways", "payment_crypto_wallets",
    )},
    **{key: "نرخ تبدیل رمزارز" for key in (
        "crypto_rate_mode", "crypto_rate_asset", "crypto_rate_manual", "crypto_rate_api_url", "crypto_rate_api_path",
        "crypto_rate_api_divisor", "crypto_rate_margin_percent", "crypto_quote_minutes",
    )},
}

# Web settings of «/shop-settings» the shop bot cannot edit yet.
SHOP_BOT_GAPS: dict[str, str] = {
    "wallet_topup_presets": OWNER_BOT_GAPS["wallet_topup_presets"],
    **{key: OWNER_BOT_GAPS[key] for key in (
        "payment_cards", "payment_gateways", "psp_gateways", "payment_crypto_wallets",
        "crypto_rate_mode", "crypto_rate_asset", "crypto_rate_manual", "crypto_rate_api_url", "crypto_rate_api_path",
        "crypto_rate_api_divisor", "crypto_rate_margin_percent", "crypto_quote_minutes",
    )},
}


def _marketing_keys() -> set[str]:
    keys: set[str] = set()
    for value in vars(marketing_nav).values():
        sections = value if isinstance(value, tuple) else (value,)
        keys.update(s.key for s in sections if isinstance(s, marketing_nav.MarketingSection))
    return keys


def _web_setting_keys() -> set[str]:
    return {field[0] for fields in SETTING_GROUPS.values() for field in fields}


def _edited_by_edit_mode(key: str) -> bool:
    """Menu titles, captions and button colours: «حالت ویرایش منوها» and the colour editor edit them."""
    return is_screen_text_key(key) or key.startswith("btn_style_")


def _shop_web_setting_keys() -> set[str]:
    """Fields a shop edits on the web: «/shop-settings» tabs and the domain forms (payment, loyalty, …).

    The same filters the shop page and save route apply.
    """
    tabs = [tab for tab, _label in RESELLER_SETTINGS_TABS] + sorted(SHOP_SETTINGS_DOMAIN_POST_TABS)
    keys = {
        field[0]
        for tab in tabs
        for name in TAB_SETTING_GROUPS.get(tab, [])
        if name in SETTING_GROUPS and name not in PLATFORM_ONLY_SETTING_GROUPS
        for field in SETTING_GROUPS[name]
    }
    hidden = set(IMPORT_PLATFORM_ONLY_KEYS) | set(PLATFORM_ONLY_BTN_KEYS) | set(platform_only_screen_text_keys())
    return keys - hidden - {"show_reseller_apply"}


def _not_in_bot(web_keys: set[str], bot_fields: set[str]) -> set[str]:
    return {
        key for key in web_keys - bot_fields - set(BOT_OTHER_EDITORS) - set(WEB_ONLY_SETTINGS)
        if not _edited_by_edit_mode(key)
    }


class ParityClassificationTests(unittest.TestCase):
    def assert_classified(self, found: set[str], *groups: dict[str, str]) -> None:
        listed = [key for group in groups for key in group]
        self.assertEqual(len(listed), len(set(listed)), "an entry is listed twice")
        self.assertEqual(found - set(listed), set(), "classify these in tests/test_bot_web_parity_guard.py")
        for group in groups:
            for key, note in group.items():
                self.assertTrue(note.strip(), key)

    def test_every_sidebar_item_is_classified(self) -> None:
        self.assert_classified(set(panel_nav._ALL_SPECS), NAV_IN_BOT, NAV_PARTLY_IN_BOT, NAV_WEB_ONLY)

    def test_every_marketing_section_is_classified(self) -> None:
        self.assert_classified(_marketing_keys(), MARKETING_IN_BOT, MARKETING_PARTLY_IN_BOT, MARKETING_WEB_ONLY)

    def test_every_owner_bot_setting_is_editable_on_the_web(self) -> None:
        outside = set(admin_settings.FIELDS) - _web_setting_keys()
        self.assertEqual(outside - set(BOT_SETTINGS_OUTSIDE_WEB_TABS), set(), "add a web field or classify it")

    def test_every_shop_bot_setting_is_editable_on_the_web(self) -> None:
        outside = set(reseller_settings.FIELDS) - _web_setting_keys()
        self.assertEqual(outside - set(BOT_SETTINGS_OUTSIDE_WEB_TABS), set(), "add a web field or classify it")

    def test_every_web_setting_is_editable_in_owner_bot_or_classified(self) -> None:
        missing = _not_in_bot(_web_setting_keys(), set(admin_settings.FIELDS))
        self.assertEqual(missing - set(OWNER_BOT_GAPS), set(), "add a bot field, or list it in OWNER_BOT_GAPS")

    def test_every_shop_web_setting_is_editable_in_shop_bot_or_classified(self) -> None:
        missing = _not_in_bot(_shop_web_setting_keys(), set(reseller_settings.FIELDS))
        self.assertEqual(missing - set(SHOP_BOT_GAPS), set(), "add a shop bot field, or list it in SHOP_BOT_GAPS")

    def test_gap_lists_only_name_settings_still_missing(self) -> None:
        self.assertEqual(set(OWNER_BOT_GAPS) & set(admin_settings.FIELDS), set(), "now in the owner bot: drop the gap")
        self.assertEqual(set(SHOP_BOT_GAPS) & set(reseller_settings.FIELDS), set(), "now in the shop bot: drop the gap")
        self.assertEqual(set(OWNER_BOT_GAPS) - _web_setting_keys(), set(), "not a web setting any more")
        self.assertEqual(set(SHOP_BOT_GAPS) - _shop_web_setting_keys(), set(), "not a shop web setting any more")

    def test_web_only_and_gap_lists_do_not_overlap(self) -> None:
        for group in (OWNER_BOT_GAPS, SHOP_BOT_GAPS, BOT_OTHER_EDITORS):
            self.assertEqual(set(group) & set(WEB_ONLY_SETTINGS), set())
        for group in (OWNER_BOT_GAPS, SHOP_BOT_GAPS, WEB_ONLY_SETTINGS, BOT_OTHER_EDITORS):
            for key, note in group.items():
                self.assertTrue(note.strip(), key)
