"""Button-label field groups for Telegram / web settings hubs.

Telegram settings keep ≤8 fields per screen; groups map to related IA
categories (main menu, wallet submenu, pay methods, admin keyboard, …).

``PLATFORM_ONLY_BTN_KEYS`` must never appear on reseller/shop settings
(bot or web) — platform apply / admin keyboard labels.
"""

from __future__ import annotations

Field = tuple[str, str, str]

# Platform-only labels — shop/reseller must not edit these.
PLATFORM_ONLY_BTN_KEYS: frozenset[str] = frozenset(
    {
        "btn_reseller_apply",
        "btn_admin",
        "btn_reseller_creds",
        "btn_adm_orders",
        "btn_adm_payments",
        "btn_adm_tickets",
        "btn_adm_plans",
        "btn_adm_pg",
        "btn_adm_users",
        "btn_adm_settings",
        "btn_adm_broadcast",
        "btn_adm_preview",
        "btn_adm_edit_mode",
        "btn_adm_hub_ops",
        "btn_adm_hub_people",
        "btn_adm_hub_product",
        "btn_adm_hub_marketing",
        "btn_adm_hub_system",
        "btn_adm_dash",
        "btn_adm_reports",
        "btn_adm_resellers",
        "btn_adm_loyalty",
        "btn_adm_backup",
        "btn_adm_exit_preview",
    }
)

# --- Shop / customer-facing button groups (Telegram subsection payloads) ---

BTN_MAIN_MENU: list[Field] = [
    ("btn_shop", "خرید", "text"),
    ("btn_services", "سرویس‌ها", "text"),
    ("btn_wallet", "کیف پول", "text"),
    ("btn_support", "پشتیبانی", "text"),
    ("btn_restart", "شروع مجدد", "text"),
    ("btn_loyalty", "باشگاه مشتریان", "text"),
    ("btn_reseller_apply", "درخواست نمایندگی", "text"),
    ("btn_wholesale", "فروش عمده", "text"),
]

BTN_WALLET_SUB: list[Field] = [
    ("btn_wallet_topup", "شارژ کیف پول", "text"),
    ("btn_wallet_tx", "تراکنش‌ها", "text"),
]

BTN_SUPPORT_SUB: list[Field] = [
    ("btn_support_new", "تیکت جدید", "text"),
    ("btn_support_list", "تیکت‌های من", "text"),
]

BTN_LOYALTY_SUB: list[Field] = [
    ("btn_referral", "دعوت دوستان", "text"),
    ("btn_loy_points", "امتیاز من", "text"),
    ("btn_loy_rewards", "جوایز", "text"),
    ("btn_loy_wheel", "چرخ شانس", "text"),
    ("btn_loy_history", "تاریخچه باشگاه", "text"),
]

BTN_SERVICE_ACTIONS: list[Field] = [
    ("btn_renew", "تمدید", "text"),
    ("btn_svc_addon", "حجم / زمان", "text"),
    ("btn_sub_link", "لینک و QR", "text"),
    ("btn_guides", "آموزش‌ها", "text"),
    ("btn_cancel", "انصراف", "text"),
]

BTN_NAV: list[Field] = [
    ("btn_menu_home", "منوی اصلی", "text"),
    ("btn_back", "بازگشت", "text"),
    ("btn_guide", "راهنما", "text"),
    ("btn_faq", "سوالات", "text"),
]

BTN_ROLES: list[Field] = [
    ("btn_admin", "پنل ادمین", "text"),
    ("btn_reseller", "پنل نماینده", "text"),
    ("btn_reseller_creds", "اطلاعات ورود نماینده", "text"),
]

BTN_ADMIN_OPS: list[Field] = [
    ("btn_adm_orders", "سفارش‌ها", "text"),
    ("btn_adm_payments", "رسیدها", "text"),
    ("btn_adm_tickets", "تیکت‌ها", "text"),
    ("btn_adm_plans", "پلن‌ها", "text"),
    ("btn_adm_pg", "پاسارگارد", "text"),
]

BTN_ADMIN_SYS: list[Field] = [
    ("btn_adm_users", "کاربران", "text"),
    ("btn_adm_settings", "تنظیمات", "text"),
    ("btn_adm_broadcast", "پیام گروهی", "text"),
    ("btn_adm_preview", "پیش‌نمایش کاربر", "text"),
    ("btn_adm_st_panel", "تنظیمات: وب‌پنل", "text"),
    ("btn_adm_st_find", "تنظیمات: جستجو", "text"),
]

BTN_STORE_EXTRA: list[Field] = [
    ("btn_buy_continue", "ادامه خرید", "text"),
    ("btn_miniapp", "مینی‌اپ", "text"),
]

BTN_ADMIN_HUBS: list[Field] = [
    ("btn_adm_hub_ops", "گروه عملیات روزانه", "text"),
    ("btn_adm_hub_people", "گروه افراد", "text"),
    ("btn_adm_hub_product", "گروه محصول و PG", "text"),
    ("btn_adm_hub_marketing", "گروه بازاریابی", "text"),
    ("btn_adm_hub_system", "گروه سیستم", "text"),
    ("btn_adm_edit_mode", "حالت ویرایش منوها", "text"),
]

BTN_ADMIN_HUB_ITEMS: list[Field] = [
    ("btn_adm_dash", "داشبورد", "text"),
    ("btn_adm_reports", "گزارشات", "text"),
    ("btn_adm_resellers", "نمایندگان", "text"),
    ("btn_adm_loyalty", "باشگاه (ادمین)", "text"),
    ("btn_adm_backup", "بکاپ / ریستور", "text"),
    ("btn_adm_exit_preview", "خروج از پیش‌نمایش", "text"),
]


# Submenu labels that used to be fixed in code (≤8 fields per Telegram screen).
BTN_ADMIN_PG: list[Field] = [
    ("btn_pg_stats", "نمای کلی پاسارگارد", "text"),
    ("btn_pg_users", "کاربران پاسارگارد", "text"),
    ("btn_pg_create", "ساخت کاربر", "text"),
    ("btn_pg_search", "جستجوی یوزر", "text"),
    ("btn_pg_nodes", "نودها", "text"),
    ("btn_pg_group", "ساخت گروه", "text"),
    ("btn_pg_template", "ساخت تمپلیت", "text"),
]

BTN_ADMIN_PEOPLE_SUB: list[Field] = [
    ("btn_adm_users_list", "لیست کاربران", "text"),
    ("btn_adm_users_search", "جستجو با آیدی تلگرام", "text"),
    ("btn_adm_users_web", "مدیریت کاربران در وب‌پنل", "text"),
    ("btn_adm_res_list", "لیست نمایندگان", "text"),
    ("btn_adm_res_apps", "درخواست‌های نمایندگی", "text"),
    ("btn_adm_res_add", "افزودن نماینده", "text"),
]

BTN_ADMIN_SETTINGS_SUB: list[Field] = [
    ("btn_adm_st_shop", "تنظیمات: فروشگاه", "text"),
    ("btn_adm_st_menu", "تنظیمات: ظاهر و دکمه‌ها", "text"),
    ("btn_adm_st_pay", "تنظیمات: پرداخت", "text"),
    ("btn_adm_st_support", "تنظیمات: پشتیبان‌ها", "text"),
    ("btn_adm_st_access", "تنظیمات: دسترسی", "text"),
    ("btn_adm_st_limits", "تنظیمات: محدودیت", "text"),
    ("btn_adm_st_guides", "تنظیمات: آموزش اتصال", "text"),
    ("btn_adm_st_notify", "تنظیمات: اعلان‌ها", "text"),
]

BTN_ADMIN_BACKUP_BROADCAST: list[Field] = [
    ("btn_backup_create", "ساخت بکاپ", "text"),
    ("btn_backup_create_env", "بکاپ همراه .env", "text"),
    ("btn_backup_upload", "آپلود فایل بکاپ", "text"),
    ("btn_backup_refresh", "تازه‌سازی لیست بکاپ", "text"),
    ("btn_bc_aud_all", "پیام همگانی: همه", "text"),
    ("btn_bc_aud_users", "پیام همگانی: کاربران", "text"),
    ("btn_bc_aud_resellers", "پیام همگانی: نمایندگان", "text"),
    ("btn_bc_aud_admins", "پیام همگانی: ادمین‌ها", "text"),
]

BTN_ADMIN_PLANS_SUB: list[Field] = [
    ("btn_adm_plans_aud_users", "پلن‌های کاربران", "text"),
    ("btn_adm_plans_aud_resellers", "پلن‌های نمایندگان", "text"),
    ("btn_adm_plans_add", "افزودن پلن", "text"),
    ("btn_adm_plans_categories", "برچسب دسته", "text"),
    ("btn_adm_plans_addons", "بسته حجم/زمان", "text"),
    ("btn_adm_plans_view", "مشاهده پلن‌ها", "text"),
]

BTN_ADMIN_MARKETING_SUB: list[Field] = [
    ("btn_adm_gift_codes", "کدهای هدیه", "text"),
    ("btn_adm_campaigns", "کمپین‌ها", "text"),
]

BTN_SERVICE_MORE: list[Field] = [
    ("btn_svc_refresh", "رفرش وضعیت", "text"),
    ("btn_svc_wireguard", "فایل WireGuard", "text"),
    ("btn_svc_auto", "تنظیمات خودکار", "text"),
    ("btn_svc_cancel", "درخواست لغو سرویس", "text"),
    ("btn_svc_delete", "حذف سرویس", "text"),
    ("btn_svc_add_existing", "اتصال اشتراک قبلی", "text"),
]

BTN_ADMIN_PLAN_KINDS: list[Field] = [
    ("btn_adm_plans_kind_res_fixed", "اشتراک ثابت", "text"),
    ("btn_adm_plans_kind_res_payg", "اشتراک PAYG", "text"),
    ("btn_adm_plans_kind_res_addon_vol", "بسته حجم", "text"),
    ("btn_adm_plans_kind_res_addon_users", "بسته کاربر", "text"),
]

BTN_ADMIN_LOYALTY_SUB: list[Field] = [
    ("btn_adm_loy_overview", "نمای کلی", "text"),
    ("btn_adm_loy_rules", "قوانین امتیاز", "text"),
    ("btn_adm_loy_rewards", "جوایز", "text"),
    ("btn_adm_loy_tiers", "سطوح", "text"),
    ("btn_adm_loy_settings", "تنظیمات باشگاه", "text"),
    ("btn_adm_loy_ref_text", "متن دعوت", "text"),
]

BTN_RESELLER_HUB: list[Field] = [
    ("btn_res_dash", "خانه نماینده", "text"),
    ("btn_res_users", "مشتریان من", "text"),
    ("btn_res_add_rep", "افزودن نماینده", "text"),
    ("btn_res_reports", "گزارشات", "text"),
    ("btn_res_stats", "آمار", "text"),
    ("btn_res_plans", "پلن‌های فروش", "text"),
    ("btn_res_orders", "سفارش‌های مشتریان", "text"),
    ("btn_res_payments", "رسیدهای در انتظار", "text"),
]

BTN_RESELLER_HUB_MORE: list[Field] = [
    ("btn_res_tickets", "تیکت‌های مشتریان", "text"),
    ("btn_res_loyalty", "باشگاه مشتریان", "text"),
    ("btn_res_settings", "تنظیمات فروشگاه", "text"),
    ("btn_res_billing", "کیف پول PAYG", "text"),
    ("btn_res_addon_packs", "بسته‌های حجم/کاربر", "text"),
    ("btn_res_renew", "تمدید سرویس", "text"),
]

BTN_RESELLER_PLANS_SUB: list[Field] = [
    ("btn_res_plan_add", "پلن جدید", "text"),
    ("btn_res_plan_categories", "برچسب دسته", "text"),
    ("btn_res_plan_addons", "بسته حجم/زمان", "text"),
]

BTN_RESELLER_SETTINGS_SUB: list[Field] = [
    ("btn_res_st_shop", "فروشگاه", "text"),
    ("btn_res_st_menu", "منو", "text"),
    ("btn_res_st_pay", "پرداخت", "text"),
    ("btn_res_st_support", "پشتیبان‌ها", "text"),
    ("btn_res_st_access", "دسترسی", "text"),
]

BTN_CONNECTION_STATUS: list[Field] = [
    ("btn_connection_status", "وضعیت سرورها (منوی کاربر)", "text"),
    ("btn_connection_status_admin", "وضعیت اتصال (پنل ادمین)", "text"),
]

BTN_RESELLER_SETTINGS_MORE: list[Field] = [
    ("btn_res_st_limits", "محدودیت", "text"),
    ("btn_res_st_guides", "آموزش اتصال", "text"),
    ("btn_res_st_notify", "اعلان‌ها", "text"),
    ("btn_res_st_bot", "ربات", "text"),
    ("btn_res_st_panel", "وب‌پنل", "text"),
]

# Admin submenu labels: platform-only like the admin hub labels above.
PLATFORM_ONLY_BTN_KEYS = PLATFORM_ONLY_BTN_KEYS | frozenset(
    key
    for group in (
        BTN_ADMIN_PG,
        BTN_ADMIN_PEOPLE_SUB,
        BTN_ADMIN_SETTINGS_SUB,
        BTN_ADMIN_BACKUP_BROADCAST,
        BTN_ADMIN_PLANS_SUB,
        BTN_ADMIN_PLAN_KINDS,
        BTN_ADMIN_LOYALTY_SUB,
        BTN_ADMIN_MARKETING_SUB,
    )
    for key, _label, _kind in group
) | frozenset({"btn_adm_st_panel", "btn_adm_st_find", "btn_connection_status_admin"})


def appearance_button_subs() -> list[tuple]:
    """Owner-only button labels that no other settings screen exposes."""
    return [
        ("btn_store_extra", "دکمه‌های خرید و مینی‌اپ", BTN_STORE_EXTRA),
        ("btn_adm_hubs", "دکمه‌های ادمین — گروه‌ها", BTN_ADMIN_HUBS),
        ("btn_adm_items", "دکمه‌های ادمین — بخش‌ها", BTN_ADMIN_HUB_ITEMS),
    ]


# Shared pay-method button labels (attached beside each method screen).
BTN_PAY_WALLET_DISCOUNT: list[Field] = [
    ("btn_pay_wallet", "پرداخت از کیف پول", "text"),
    ("btn_pay_discount", "کد تخفیف", "text"),
]

BTN_PAY_CARD: list[Field] = [("btn_pay_card", "متن دکمه کارت", "text")]
BTN_PAY_GATEWAY: list[Field] = [("btn_pay_gateway", "متن دکمه درگاه", "text")]
BTN_PAY_CRYPTO: list[Field] = [("btn_pay_crypto", "متن دکمه رمزارز", "text")]
BTN_PAY_STARS: list[Field] = [("btn_pay_stars", "متن دکمه استارز", "text")]
BTN_PAY_PSP: list[Field] = [("btn_pay_psp", "متن دکمه درگاه API", "text")]


def filter_btn_fields(
    fields: list[Field], *, include_platform: bool
) -> list[Field]:
    if include_platform:
        return list(fields)
    return [f for f in fields if f[0] not in PLATFORM_ONLY_BTN_KEYS]


def shop_button_subs(*, include_platform: bool) -> list[tuple]:
    """Telegram shop-section subsections for button label editing."""
    pairs: list[tuple[str, str, list[Field]]] = [
        ("btn_main", "دکمه‌های منوی اصلی", BTN_MAIN_MENU),
        ("btn_wallet_sub", "دکمه‌های کیف پول", BTN_WALLET_SUB),
        ("btn_support_sub", "دکمه‌های پشتیبانی", BTN_SUPPORT_SUB),
        ("btn_loyalty_sub", "دکمه‌های باشگاه", BTN_LOYALTY_SUB),
        ("btn_service", "دکمه‌های سرویس", BTN_SERVICE_ACTIONS),
        ("btn_nav", "دکمه‌های ناوبری", BTN_NAV),
        ("btn_roles", "دکمه‌های نقش‌ها", BTN_ROLES),
        ("btn_adm_ops", "دکمه‌های ادمین — عملیات", BTN_ADMIN_OPS),
        ("btn_adm_sys", "دکمه‌های ادمین — سیستم", BTN_ADMIN_SYS),
        ("btn_adm_pg_sub", "دکمه‌های ادمین — پاسارگارد", BTN_ADMIN_PG),
        ("btn_adm_people_sub", "دکمه‌های ادمین — کاربران و نمایندگان", BTN_ADMIN_PEOPLE_SUB),
        ("btn_adm_settings_sub", "دکمه‌های ادمین — بخش‌های تنظیمات", BTN_ADMIN_SETTINGS_SUB),
        ("btn_adm_backup_sub", "دکمه‌های ادمین — بکاپ و پیام همگانی", BTN_ADMIN_BACKUP_BROADCAST),
        ("btn_adm_plans_sub", "دکمه‌های ادمین — پلن‌ها", BTN_ADMIN_PLANS_SUB),
        ("btn_adm_plan_kinds", "دکمه‌های ادمین — نوع پلن نمایندگی", BTN_ADMIN_PLAN_KINDS),
        ("btn_adm_loy_sub", "دکمه‌های ادمین — باشگاه", BTN_ADMIN_LOYALTY_SUB),
        ("btn_adm_mkt_sub", "دکمه‌های ادمین — کدهای هدیه و کمپین", BTN_ADMIN_MARKETING_SUB),
        ("btn_service_more", "دکمه‌های سرویس (ادامه)", BTN_SERVICE_MORE),
        ("btn_res_hub", "دکمه‌های پنل نماینده", BTN_RESELLER_HUB),
        ("btn_res_hub_more", "دکمه‌های پنل نماینده (ادامه)", BTN_RESELLER_HUB_MORE),
        ("btn_res_plans_sub", "دکمه‌های پلن‌های فروش", BTN_RESELLER_PLANS_SUB),
        ("btn_res_st_sub", "دکمه‌های تنظیمات فروشگاه", BTN_RESELLER_SETTINGS_SUB),
        ("btn_res_st_more", "دکمه‌های تنظیمات فروشگاه (ادامه)", BTN_RESELLER_SETTINGS_MORE),
        ("btn_status", "دکمه‌های وضعیت اتصال", BTN_CONNECTION_STATUS),
    ]
    out: list[tuple] = []
    for sub_id, title, fields in pairs:
        filtered = filter_btn_fields(fields, include_platform=include_platform)
        if filtered:
            out.append((sub_id, title, filtered))
    return out


def all_btn_field_keys(*, include_platform: bool) -> frozenset[str]:
    """Every btn_* label key exposed via shop_button_subs + pay button lists."""
    keys: set[str] = set()
    for _, _, fields in shop_button_subs(include_platform=include_platform):
        keys.update(f[0] for f in fields)
    for group in (
        BTN_PAY_WALLET_DISCOUNT,
        BTN_PAY_CARD,
        BTN_PAY_GATEWAY,
        BTN_PAY_CRYPTO,
        BTN_PAY_STARS,
        BTN_PAY_PSP,
    ):
        keys.update(
            f[0]
            for f in filter_btn_fields(group, include_platform=include_platform)
        )
    return frozenset(keys)
