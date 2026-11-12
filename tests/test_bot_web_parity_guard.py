"""Every web menu entry and every bot setting is classified for web/bot parity (docs/BOT_PARITY_MATRIX.md).

A new sidebar item, marketing section or owner-bot setting fails here until it is placed in one of
the lists below, with where it lives on the other side or why it stays on one side. This keeps a
feature from quietly landing in only the web panel or only the bot.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.handlers import admin_settings, reseller_settings
from app.services import marketing_nav, panel_nav
from app.services.users import SETTING_GROUPS

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
    "custom_service_name_enabled": "وب: تنظیمات ربات › قالب نام‌گذاری",
    "miniapp_shops_enabled": "وب: تنظیمات ربات › مینی‌اپ (فرم اختصاصی)",
    "miniapp_notice": "وب: تنظیمات ربات › مینی‌اپ (فرم اختصاصی)",
}


def _marketing_keys() -> set[str]:
    keys: set[str] = set()
    for value in vars(marketing_nav).values():
        sections = value if isinstance(value, tuple) else (value,)
        keys.update(s.key for s in sections if isinstance(s, marketing_nav.MarketingSection))
    return keys


def _web_setting_keys() -> set[str]:
    return {field[0] for fields in SETTING_GROUPS.values() for field in fields}


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
        self.assert_classified(_marketing_keys(), MARKETING_IN_BOT, MARKETING_PARTLY_IN_BOT)

    def test_every_owner_bot_setting_is_editable_on_the_web(self) -> None:
        outside = set(admin_settings.FIELDS) - _web_setting_keys()
        self.assertEqual(outside - set(BOT_SETTINGS_OUTSIDE_WEB_TABS), set(), "add a web field or classify it")

    def test_every_shop_bot_setting_is_editable_on_the_web(self) -> None:
        outside = set(reseller_settings.FIELDS) - _web_setting_keys()
        self.assertEqual(outside - set(BOT_SETTINGS_OUTSIDE_WEB_TABS), set(), "add a web field or classify it")
