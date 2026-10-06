"""Telegram Bot API 9.4 button colors — catalog, defaults, and lookups.

Telegram only supports: primary (blue), success (green), danger (red),
or omit style for the client default (white/gray).
"""

from __future__ import annotations

from typing import Any

# value, label, UI tone class (for colors tab selects)
STYLE_OPTIONS: list[tuple[str, str, str]] = [
    ("", "سفید", "default"),
    ("primary", "آبی", "primary"),
    ("success", "سبز", "success"),
    ("danger", "قرمز", "danger"),
]

# Reply review actions that share global confirm / reject chrome.
STYLE_ALIASES: dict[str, str] = {
    "rev_ok": "confirm",
    "rev_no": "reject",
    "referral": "loy_referral",
    "shop_custom": "shop_kind_custom",
    "shop_wholesale": "shop_kind_wholesale",
    "adm_plans_kind_users_fixed": "shop_kind_fixed",
    "adm_plans_kind_users_custom": "shop_kind_custom",
    "adm_plans_kind_users_wholesale": "shop_kind_wholesale",
    "adm_plans_kind_res_fixed": "plan_res_fixed",
    "adm_plans_kind_res_payg": "plan_res_payg",
    "adm_plans_kind_users_trial": "shop_kind_trial",
    "adm_plan_add": "adm_plans_add",
    "adm_plan_custom": "shop_kind_custom",
    "adm_plan_trial": "shop_kind_trial",
    # Legacy settings hub key (pre-8.2.1 «سرویس و دسترسی») → دسترسی
    "adm_st_service": "adm_st_access",
}

# id must match reply action keys where applicable (shop, services, …).
BUTTON_STYLE_CATALOG: list[dict[str, str]] = [
    # Global chrome — one place for consistent nav / confirm / cancel
    {
        "id": "home",
        "label": "منوی اصلی / صفحه اصلی",
        "group": "دکمه‌های سراسری",
        "default": "",
    },
    {
        "id": "back",
        "label": "بازگشت",
        "group": "دکمه‌های سراسری",
        "default": "",
    },
    {
        "id": "cancel",
        "label": "انصراف / لغو",
        "group": "دکمه‌های سراسری",
        "default": "danger",
    },
    {
        "id": "confirm",
        "label": "تأیید",
        "group": "دکمه‌های سراسری",
        "default": "success",
    },
    {
        "id": "reject",
        "label": "رد",
        "group": "دکمه‌های سراسری",
        "default": "danger",
    },
    # Main menu
    {"id": "shop", "label": "خرید اشتراک", "group": "منوی اصلی", "default": "primary"},
    {"id": "services", "label": "سرویس‌های من", "group": "منوی اصلی", "default": "primary"},
    {"id": "wallet", "label": "کیف پول", "group": "منوی اصلی", "default": "primary"},
    {"id": "support", "label": "پشتیبانی", "group": "منوی اصلی", "default": "primary"},
    {"id": "loyalty", "label": "باشگاه مشتریان", "group": "منوی اصلی", "default": "success"},
    {"id": "reseller_apply", "label": "درخواست نمایندگی", "group": "منوی اصلی", "default": ""},
    {"id": "reseller", "label": "پنل نماینده", "group": "منوی اصلی", "default": ""},
    {"id": "reseller_creds", "label": "اطلاعات ورود نماینده", "group": "منوی اصلی", "default": ""},
    {"id": "admin", "label": "پنل ادمین", "group": "منوی اصلی", "default": ""},
    # Wallet / support sub
    {"id": "wallet_topup", "label": "شارژ کیف پول", "group": "زیرمنوها", "default": "primary"},
    {"id": "support_new", "label": "تیکت جدید", "group": "زیرمنوها", "default": "primary"},
    {"id": "svc_renew", "label": "تمدید سرویس", "group": "زیرمنوها", "default": "primary"},
    {"id": "svc_link", "label": "لینک و QR", "group": "زیرمنوها", "default": ""},
    {"id": "wallet_tx", "label": "تراکنش‌های کیف پول", "group": "زیرمنوها", "default": ""},
    {"id": "support_list", "label": "تیکت‌های من", "group": "زیرمنوها", "default": ""},
    {"id": "svc_refresh", "label": "رفرش وضعیت سرویس", "group": "زیرمنوها", "default": ""},
    {"id": "miniapp", "label": "مینی‌اپ (منوی اصلی)", "group": "زیرمنوها", "default": "primary"},
    # Shop flow (inline) — submenu plan rows inherit the matching kind color
    {
        "id": "shop_kind_fixed",
        "label": "پلن ثابت — رنگ پیش‌فرض",
        "group": "فروشگاه",
        "default": "primary",
    },
    {
        "id": "shop_kind_custom",
        "label": "پلن دلخواه — رنگ پیش‌فرض",
        "group": "فروشگاه",
        "default": "primary",
    },
    {
        "id": "shop_kind_wholesale",
        "label": "پلن عمده — رنگ پیش‌فرض",
        "group": "فروشگاه",
        "default": "primary",
    },
    {
        "id": "shop_kind_trial",
        "label": "پلن تست — رنگ پیش‌فرض",
        "group": "فروشگاه",
        "default": "primary",
    },
    {"id": "buy_continue", "label": "ادامه خرید / تأیید پلن", "group": "فروشگاه", "default": "primary"},
    {"id": "force_join_check", "label": "عضو شدم (کانال اجباری)", "group": "فروشگاه", "default": "primary"},
    {"id": "one_tap_renew", "label": "تمدید یک‌ضربی (هشدار انقضا)", "group": "فروشگاه", "default": "primary"},
    # Terms/rules accept buttons — own group (also editable on تنظیمات ← قوانین)
    {"id": "terms_entry", "label": "موافقم (قوانین ورود)", "group": "قوانین", "default": "primary"},
    {"id": "terms_buy_user", "label": "موافقم (قوانین خرید کاربر)", "group": "قوانین", "default": "primary"},
    {"id": "terms_buy_reseller", "label": "موافقم (قوانین خرید نماینده)", "group": "قوانین", "default": "primary"},
    # Reseller audience plans (admin configures kinds; submenu rows inherit these colors)
    {
        "id": "plan_res_fixed",
        "label": "پلن نماینده ثابت — پیش‌فرض",
        "group": "پلن نمایندگی",
        "default": "primary",
    },
    {
        "id": "plan_res_payg",
        "label": "پلن PAYG نماینده — پیش‌فرض",
        "group": "پلن نمایندگی",
        "default": "primary",
    },
    # Payment
    {"id": "pay_wallet", "label": "پرداخت با کیف پول", "group": "پرداخت", "default": "success"},
    {"id": "pay_card", "label": "کارت به کارت", "group": "پرداخت", "default": "primary"},
    {"id": "pay_gateway", "label": "درگاه پرداخت", "group": "پرداخت", "default": "primary"},
    {"id": "pay_psp", "label": "درگاه آنلاین API", "group": "پرداخت", "default": "primary"},
    {"id": "pay_crypto", "label": "رمزارز", "group": "پرداخت", "default": "primary"},
    {"id": "pay_stars", "label": "استارز تلگرام", "group": "پرداخت", "default": "primary"},
    {"id": "pay_discount", "label": "کد تخفیف", "group": "پرداخت", "default": ""},
    {"id": "topup_card", "label": "شارژ — کارت", "group": "پرداخت", "default": "primary"},
    {"id": "topup_gateway", "label": "شارژ — درگاه", "group": "پرداخت", "default": "primary"},
    {"id": "topup_crypto", "label": "شارژ — رمزارز", "group": "پرداخت", "default": "primary"},
    # Customer loyalty sub
    {"id": "loy_referral", "label": "دعوت دوستان", "group": "باشگاه مشتریان", "default": "primary"},
    {"id": "loy_points", "label": "امتیاز من", "group": "باشگاه مشتریان", "default": ""},
    {"id": "loy_rewards", "label": "جوایز باشگاه", "group": "باشگاه مشتریان", "default": ""},
    {"id": "loy_wheel", "label": "چرخ شانس", "group": "باشگاه مشتریان", "default": "primary"},
    {"id": "loy_history", "label": "تاریخچه باشگاه", "group": "باشگاه مشتریان", "default": ""},
    # ── Admin hub (platform bot only) ──────────────────────────────────────────
    {"id": "adm_hub_ops", "label": "عملیات روزانه", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_hub_people", "label": "افراد", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_hub_product", "label": "محصول و پاسارگارد", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_hub_system", "label": "سیستم", "group": "منوی ادمین", "default": ""},
    {"id": "adm_dash", "label": "داشبورد ادمین", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_orders", "label": "سفارش‌ها (ادمین)", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_payments", "label": "رسیدها (ادمین)", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_tickets", "label": "تیکت‌ها (ادمین)", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_plans", "label": "پلن‌ها (ادمین)", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_users", "label": "کاربران (ادمین)", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_resellers", "label": "نمایندگان (ادمین)", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_loyalty", "label": "باشگاه مشتریان (ادمین)", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_pg", "label": "پاسارگارد (ادمین)", "group": "منوی ادمین", "default": "success"},
    {"id": "adm_settings", "label": "تنظیمات (ادمین)", "group": "منوی ادمین", "default": ""},
    {"id": "adm_broadcast", "label": "پیام همگانی (ادمین)", "group": "منوی ادمین", "default": ""},
    {"id": "adm_backup", "label": "بکاپ / ریستور (ادمین)", "group": "منوی ادمین", "default": ""},
    {"id": "adm_preview", "label": "پیش‌نمایش منوی کاربر (ادمین)", "group": "منوی ادمین", "default": ""},
    {"id": "adm_users_list", "label": "لیست کاربران", "group": "منوی ادمین", "default": ""},
    {"id": "adm_users_search", "label": "جستجوی کاربر", "group": "منوی ادمین", "default": ""},
    {"id": "adm_users_web", "label": "مدیریت کاربران در وب", "group": "منوی ادمین", "default": ""},
    {"id": "adm_res_list", "label": "لیست نمایندگان", "group": "منوی ادمین", "default": ""},
    {"id": "adm_res_apps", "label": "درخواست‌های نمایندگی", "group": "منوی ادمین", "default": ""},
    {"id": "adm_res_add", "label": "افزودن نماینده", "group": "منوی ادمین", "default": "primary"},
    {"id": "adm_plans_aud_users", "label": "پلن‌های کاربران", "group": "منوی ادمین", "default": "primary"},
    {"id": "adm_plans_aud_resellers", "label": "پلن‌های نمایندگان", "group": "منوی ادمین", "default": "primary"},
    {"id": "adm_plans_add", "label": "افزودن پلن", "group": "منوی ادمین", "default": "primary"},
    # Admin loyalty sub
    {"id": "adm_loy_overview", "label": "نمای کلی باشگاه", "group": "باشگاه ادمین", "default": "success"},
    {"id": "adm_loy_rules", "label": "قوانین امتیاز", "group": "باشگاه ادمین", "default": ""},
    {"id": "adm_loy_rewards", "label": "جوایز باشگاه (ادمین)", "group": "باشگاه ادمین", "default": ""},
    {"id": "adm_loy_tiers", "label": "سطوح باشگاه", "group": "باشگاه ادمین", "default": ""},
    {"id": "adm_loy_settings", "label": "تنظیمات باشگاه", "group": "باشگاه ادمین", "default": ""},
    {"id": "adm_loy_ref_text", "label": "متن دعوت باشگاه", "group": "باشگاه ادمین", "default": ""},
    # PasarGuard sub
    {"id": "pg_stats", "label": "نمای کلی پاسارگارد", "group": "پاسارگارد", "default": "success"},
    {"id": "pg_users", "label": "کاربران پاسارگارد", "group": "پاسارگارد", "default": ""},
    {"id": "pg_create", "label": "ساخت کاربر پاسارگارد", "group": "پاسارگارد", "default": "primary"},
    {"id": "pg_search", "label": "جستجوی کاربر پاسارگارد", "group": "پاسارگارد", "default": ""},
    {"id": "pg_nodes", "label": "نودهای پاسارگارد", "group": "پاسارگارد", "default": ""},
    {"id": "pg_group", "label": "ساخت گروه پاسارگارد", "group": "پاسارگارد", "default": "primary"},
    {"id": "pg_template", "label": "ساخت تمپلیت پاسارگارد", "group": "پاسارگارد", "default": "primary"},
    # Admin settings sub (reply hub — ids must match adm_st_* / adm_st_panel actions)
    {"id": "adm_st_shop", "label": "تنظیمات: فروشگاه", "group": "تنظیمات ادمین", "default": ""},
    {"id": "adm_st_menu", "label": "تنظیمات: منو", "group": "تنظیمات ادمین", "default": ""},
    {"id": "adm_st_pay", "label": "تنظیمات: پرداخت", "group": "تنظیمات ادمین", "default": ""},
    {"id": "adm_st_support", "label": "تنظیمات: پشتیبان‌ها", "group": "تنظیمات ادمین", "default": ""},
    {"id": "adm_st_access", "label": "تنظیمات: دسترسی", "group": "تنظیمات ادمین", "default": ""},
    {"id": "adm_st_notify", "label": "تنظیمات: اعلان‌ها", "group": "تنظیمات ادمین", "default": ""},
    {"id": "adm_st_panel", "label": "تنظیمات: وب‌پنل", "group": "تنظیمات ادمین", "default": "primary"},
    # Backup sub
    {"id": "backup_create", "label": "ساخت بکاپ کامل", "group": "بکاپ", "default": "primary"},
    {"id": "backup_create_noenv", "label": "بکاپ بدون .env", "group": "بکاپ", "default": "primary"},
    {"id": "backup_upload", "label": "آپلود فایل بکاپ", "group": "بکاپ", "default": ""},
    {"id": "backup_refresh", "label": "تازه‌سازی لیست بکاپ", "group": "بکاپ", "default": ""},
    # Broadcast audience
    {"id": "bc_aud_all", "label": "همگانی: همه", "group": "پیام همگانی", "default": ""},
    {"id": "bc_aud_users", "label": "همگانی: کاربران", "group": "پیام همگانی", "default": ""},
    {"id": "bc_aud_resellers", "label": "همگانی: نمایندگان", "group": "پیام همگانی", "default": ""},
    {"id": "bc_aud_admins", "label": "همگانی: ادمین‌ها", "group": "پیام همگانی", "default": ""},
    # ── Reseller hub (shop bot / reseller panel) ────────────────────────────
    {"id": "res_dash", "label": "خانه نماینده", "group": "منوی نماینده", "default": "success"},
    {"id": "res_users", "label": "مشتریان نماینده", "group": "منوی نماینده", "default": "success"},
    {"id": "res_billing", "label": "کیف پول PAYG", "group": "منوی نماینده", "default": "success"},
    {"id": "res_stats", "label": "آمار", "group": "منوی نماینده", "default": "success"},
    {"id": "res_orders", "label": "سفارش‌ها (نماینده)", "group": "منوی نماینده", "default": "success"},
    {"id": "res_payments", "label": "رسیدها (نماینده)", "group": "منوی نماینده", "default": "success"},
    {"id": "res_plans", "label": "پلن‌های فروش", "group": "منوی نماینده", "default": "primary"},
    {"id": "res_plan_add", "label": "افزودن پلن نماینده", "group": "منوی نماینده", "default": "primary"},
    {"id": "res_renew", "label": "تمدید ظرفیت نماینده", "group": "منوی نماینده", "default": "primary"},
    {"id": "res_buy_gb", "label": "خرید حجم نماینده", "group": "منوی نماینده", "default": "primary"},
    {"id": "res_buy_users", "label": "خرید کاربر نماینده", "group": "منوی نماینده", "default": "primary"},
    {"id": "res_tickets", "label": "تیکت‌های مشتریان", "group": "منوی نماینده", "default": "success"},
    {"id": "res_loyalty", "label": "باشگاه مشتریان (نماینده)", "group": "منوی نماینده", "default": "success"},
    {"id": "res_settings", "label": "تنظیمات فروشگاه", "group": "منوی نماینده", "default": ""},
    {"id": "res_preview", "label": "پیش‌نمایش منوی کاربر", "group": "منوی نماینده", "default": ""},
    # Reseller settings sub (reply hub — ids must match res_st_* / res_st_panel)
    {"id": "res_st_shop", "label": "تنظیمات نماینده: فروشگاه", "group": "تنظیمات نماینده", "default": ""},
    {"id": "res_st_menu", "label": "تنظیمات نماینده: منو", "group": "تنظیمات نماینده", "default": ""},
    {"id": "res_st_pay", "label": "تنظیمات نماینده: پرداخت", "group": "تنظیمات نماینده", "default": ""},
    {"id": "res_st_support", "label": "تنظیمات نماینده: پشتیبان‌ها", "group": "تنظیمات نماینده", "default": ""},
    {"id": "res_st_access", "label": "تنظیمات نماینده: دسترسی", "group": "تنظیمات نماینده", "default": ""},
    {"id": "res_st_notify", "label": "تنظیمات نماینده: اعلان‌ها", "group": "تنظیمات نماینده", "default": ""},
    {"id": "res_st_bot", "label": "تنظیمات نماینده: ربات", "group": "تنظیمات نماینده", "default": ""},
    {"id": "res_st_panel", "label": "تنظیمات نماینده: وب‌پنل", "group": "تنظیمات نماینده", "default": "primary"},
]

CATALOG_BY_ID: dict[str, dict[str, str]] = {item["id"]: item for item in BUTTON_STYLE_CATALOG}
CATALOG_IDS: frozenset[str] = frozenset(CATALOG_BY_ID) | frozenset(STYLE_ALIASES)


def resolve_style_id(button_id: str) -> str:
    """Map aliased actions onto their global chrome setting id."""
    bid = (button_id or "").strip()
    return STYLE_ALIASES.get(bid, bid)


def setting_key(button_id: str) -> str:
    return f"btn_style_{resolve_style_id(button_id)}"


def normalize_style(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    if v in {"primary", "success", "danger"}:
        return v
    return ""


# Plan form: inherit sentinel (stored as NULL) + explicit Telegram colors.
PLAN_BUTTON_STYLE_OPTIONS: list[tuple[str, str, str]] = [
    ("inherit", "ارث از نوع پلن", "default"),
    *STYLE_OPTIONS,
]

# Category shop-menu buttons: inherit → shop_kind_fixed color.
CATEGORY_BUTTON_STYLE_OPTIONS: list[tuple[str, str, str]] = [
    ("inherit", "ارث از پلن ثابت", "default"),
    *STYLE_OPTIONS,
]

# JSON list items (payment destinations, support contacts): inherit → parent style.
ITEM_BUTTON_STYLE_OPTIONS: list[tuple[str, str, str]] = [
    ("inherit", "ارث از پیش‌فرض", "default"),
    *STYLE_OPTIONS,
]

PAY_METHOD_STYLE_IDS: dict[str, str] = {
    "card": "pay_card",
    "gateway": "pay_gateway",
    "psp": "pay_psp",
    "crypto": "pay_crypto",
}

SUPPORT_CONTACT_STYLE_ID = "support"


def parse_plan_button_style_form(form, field: str = "button_style") -> str | None:
    """Parse plan color from HTML form. Returns None = inherit kind color."""
    if field not in form:
        return None
    raw = str(form.get(field, "")).strip().lower()
    if raw in {"inherit", "__inherit__"}:
        return None
    return normalize_style(raw)


def plan_button_style_form_value(stored: str | None) -> str:
    """Map DB value → form select value."""
    if stored is None:
        return "inherit"
    return normalize_style(stored)


def parse_custom_plan_button_style_form(form) -> str | None:
    """Persist custom-plan kind color override (setting key, not btn_style catalog)."""
    if "custom_plan_button_style" not in form:
        return None
    raw = str(form.get("custom_plan_button_style", "")).strip().lower()
    if raw in {"inherit", "__inherit__"}:
        return None
    return normalize_style(raw)


def parse_plan_button_style_callback(raw: str) -> str | None:
    """Parse bot inline color picker suffix. None = inherit kind color."""
    v = (raw or "").strip().lower()
    if v in {"inherit", "default", "__inherit__"}:
        return None
    return normalize_style(v)


def item_button_style_options(
    *,
    inherit_label: str = "ارث از پیش‌فرض",
) -> list[tuple[str, str, str]]:
    return [("inherit", inherit_label, "default"), *STYLE_OPTIONS]


def parse_item_button_style_raw(raw: object) -> str | None:
    """Parse stored JSON item style. None = inherit parent default."""
    if raw is None:
        return None
    if raw == "":
        return ""
    text = str(raw).strip().lower()
    if text in {"inherit", "__inherit__", "none"}:
        return None
    return normalize_style(text)


def parse_item_button_style_form(form, field: str = "button_style") -> str | None:
    if field not in form:
        return None
    return parse_item_button_style_raw(form.get(field))


def item_button_style_form_value(stored: object) -> str:
    parsed = parse_item_button_style_raw(stored)
    if parsed is None and stored is not None and str(stored).strip() == "":
        return ""
    if parsed is None:
        return "inherit"
    return parsed


def serialize_item_button_style(raw: object) -> dict[str, str]:
    """JSON storage: omit inherit, keep explicit white as \"\"."""
    parsed = parse_item_button_style_raw(raw)
    if parsed is None and raw is not None and str(raw).strip() == "":
        return {"button_style": ""}
    if parsed is not None:
        return {"button_style": parsed}
    return {}


def parse_item_button_style_callback(raw: str) -> str | None:
    return parse_item_button_style_raw(raw)


def resolve_payment_destination_style(
    ui: dict | None,
    item: dict | None,
    method: str,
) -> str | None:
    """Telegram inline style for one card/gateway/wallet row."""
    if item and "button_style" in item:
        raw = item.get("button_style")
        if raw == "":
            return None
        parsed = parse_item_button_style_raw(raw)
        if parsed is not None:
            return parsed or None
    style_id = PAY_METHOD_STYLE_IDS.get(method, "pay_card")
    return style_or_none(ui, style_id, fallback="primary")


def resolve_support_contact_style(
    ui: dict | None,
    contact: dict | None,
) -> str | None:
    if contact and "button_style" in contact:
        raw = contact.get("button_style")
        if raw == "":
            return None
        parsed = parse_item_button_style_raw(raw)
        if parsed is not None:
            return parsed or None
    return style_or_none(ui, SUPPORT_CONTACT_STYLE_ID, fallback="primary")


def get_button_style(
    ui: dict | None,
    button_id: str,
    *,
    fallback: str | None = None,
) -> str:
    """Resolve style for a catalog button. Empty string = Telegram default (white)."""
    resolved = resolve_style_id(button_id)
    key = f"btn_style_{resolved}"
    if ui is not None and key in ui:
        return normalize_style(ui.get(key))
    if fallback is not None:
        return normalize_style(fallback)
    item = CATALOG_BY_ID.get(resolved)
    if item:
        return normalize_style(item["default"])
    return ""


def style_or_none(
    ui: dict | None,
    button_id: str,
    *,
    fallback: str | None = None,
) -> str | None:
    """Like get_button_style but None when white/default (omit Telegram ``style``)."""
    s = get_button_style(ui, button_id, fallback=fallback)
    return s or None


def style_kwargs(
    ui: dict | None,
    button_id: str,
    *,
    fallback: str | None = None,
) -> dict[str, str]:
    s = style_or_none(ui, button_id, fallback=fallback)
    return {"style": s} if s else {}


def default_settings() -> dict[str, str]:
    return {f"btn_style_{item['id']}": item["default"] for item in BUTTON_STYLE_CATALOG}


def setting_group_fields() -> list[tuple[Any, ...]]:
    """SETTING_GROUPS entries (select) so keys_for_tab / save stay in sync."""
    opts = [(value, label) for value, label, _tone in STYLE_OPTIONS]
    fields: list[tuple[Any, ...]] = []
    for item in BUTTON_STYLE_CATALOG:
        fields.append(
            (
                f"btn_style_{item['id']}",
                item["label"],
                "select",
                item["group"],
                opts,
            )
        )
    return fields


# Groups only the platform owner may color (never shown/saved for shop resellers).
ADMIN_ONLY_GROUPS: frozenset[str] = frozenset(
    {
        "منوی ادمین",
        "پلن نمایندگی",
        "پاسارگارد",
        "تنظیمات ادمین",
        "بکاپ",
        "پیام همگانی",
        "باشگاه ادمین",
    }
)

# Groups that belong to the reseller hub (shown separately from admin in the panel).
RESELLER_HUB_GROUPS: frozenset[str] = frozenset({"منوی نماینده", "تنظیمات نماینده"})

# Shared customer / shop chrome (both owner and reseller may color these).
SHARED_GROUPS: frozenset[str] = frozenset(
    {
        "دکمه‌های سراسری",
        "منوی اصلی",
        "زیرمنوها",
        "فروشگاه",
        "قوانین",
        "پرداخت",
        "باشگاه مشتریان",
    }
)

# Map plan-kind screen keys → catalog style id (submenu rows inherit this color).
PLAN_KIND_STYLE_IDS: dict[str, str] = {
    "fixed": "shop_kind_fixed",
    "custom": "shop_kind_custom",
    "trial": "shop_kind_trial",
    "wholesale": "shop_kind_wholesale",
    "res_fixed": "plan_res_fixed",
    "payg": "plan_res_payg",
}


def plan_kind_style_id(kind: str | None) -> str | None:
    """Resolve a plan-kind key to the catalog button id whose color submenus inherit."""
    k = (kind or "").strip().lower()
    if k in {"resellers:fixed", "reseller_fixed", "fixed_res"}:
        k = "res_fixed"
    if k in {"resellers:payg", "reseller_payg"}:
        k = "payg"
    return PLAN_KIND_STYLE_IDS.get(k)


def infer_user_plan_kind(plan: object | None) -> str:
    if plan is not None and getattr(plan, "is_trial", False):
        return "trial"
    return "fixed"


def infer_reseller_plan_kind(plan: object | None) -> str:
    if plan is None:
        return "res_fixed"
    pk = (getattr(plan, "plan_kind", "") or "subscription").strip().lower()
    if pk in {"addon_volume", "addon_users"}:
        return "res_fixed"
    bm = (getattr(plan, "billing_mode", "") or "fixed").strip().lower()
    if bm == "payg":
        return "payg"
    return "res_fixed"


def resolve_plan_button_style(
    ui: dict | None,
    plan: object | None,
    *,
    kind: str | None = None,
    audience: str = "users",
) -> str | None:
    """Telegram inline style for one plan row — override then kind catalog."""
    stored = getattr(plan, "button_style", None) if plan is not None else None
    if stored is not None:
        explicit = normalize_style(stored)
        return explicit or None
    if kind is None:
        kind = (
            infer_user_plan_kind(plan)
            if (audience or "users") == "users"
            else infer_reseller_plan_kind(plan)
        )
    sid = plan_kind_style_id(kind) or (
        "shop_kind_fixed" if audience == "users" else "plan_res_fixed"
    )
    return style_or_none(ui, sid, fallback="primary")


def resolve_category_button_style(ui: dict | None, category: object | None) -> str | None:
    """Telegram style for a shop category menu button."""
    stored = getattr(category, "button_style", None) if category is not None else None
    if stored is not None:
        explicit = normalize_style(stored)
        return explicit or None
    return style_or_none(ui, "shop_kind_fixed", fallback="primary")


def resolve_custom_plan_button_style(ui: dict | None) -> str | None:
    raw = (ui or {}).get("custom_plan_button_style")
    if raw is not None and str(raw).strip() and str(raw).strip().lower() not in {
        "inherit",
        "none",
    }:
        explicit = normalize_style(str(raw))
        if explicit:
            return explicit
        return None
    return style_or_none(ui, "shop_kind_custom", fallback="primary")


def catalog_item_allowed_for_reseller(item: dict[str, str]) -> bool:
    """Whether a catalog row may appear / be saved on the shop colors tab."""
    bid = item["id"]
    if item["group"] in ADMIN_ONLY_GROUPS:
        return False
    if bid.startswith("adm_") or bid.startswith("pg_") or bid.startswith("backup_"):
        return False
    if bid.startswith("bc_"):
        return False
    if bid in {
        "admin",
        "reseller_apply",
        "reseller_creds",
        "plan_res_fixed",
        "plan_res_payg",
        "miniapp",  # platform WebApp only
    }:
        return False
    return True


def allowed_style_setting_keys(*, for_reseller: bool = False) -> set[str]:
    """btn_style_* keys a role may persist — blocks admin-key smuggling into ResellerSetting."""
    if not for_reseller:
        return {f"btn_style_{item['id']}" for item in BUTTON_STYLE_CATALOG}
    return {
        f"btn_style_{item['id']}"
        for item in BUTTON_STYLE_CATALOG
        if catalog_item_allowed_for_reseller(item)
    }


def grouped_catalog(*, for_reseller: bool = False) -> list[tuple[str, list[dict[str, str]]]]:
    """Preserve catalog order; group consecutive items by ``group``."""
    groups: list[tuple[str, list[dict[str, str]]]] = []
    current_name = ""
    current_items: list[dict[str, str]] = []
    for item in BUTTON_STYLE_CATALOG:
        if for_reseller and not catalog_item_allowed_for_reseller(item):
            continue
        g = item["group"]
        if g != current_name:
            if current_items:
                groups.append((current_name, current_items))
            current_name = g
            current_items = []
        current_items.append(
            {
                "id": item["id"],
                "key": f"btn_style_{item['id']}",
                "label": item["label"],
                "default": item["default"],
            }
        )
    if current_items:
        groups.append((current_name, current_items))
    return groups


def sectioned_catalog(
    *, for_reseller: bool = False
) -> list[tuple[str, str, str, list[tuple[str, list[dict[str, str]]]]]]:
    """Group catalog into audience sections for the colors settings UI.

    Returns (section_id, title, caption, groups).
    """
    flat = grouped_catalog(for_reseller=for_reseller)
    if for_reseller:
        return [
            (
                "shop",
                "رنگ‌بندی فروشگاه شما",
                "فقط دکمه‌های ربات فروشگاهتان — منوی ادمین اینجا نیست.",
                flat,
            )
        ]

    by_name = {name: items for name, items in flat}

    def _pick(names: list[str]) -> list[tuple[str, list[dict[str, str]]]]:
        out: list[tuple[str, list[dict[str, str]]]] = []
        for n in names:
            if n in by_name:
                out.append((n, by_name[n]))
        return out

    shared_names = [
        "دکمه‌های سراسری",
        "منوی اصلی",
        "زیرمنوها",
        "فروشگاه",
        "قوانین",
        "پرداخت",
        "باشگاه مشتریان",
    ]
    admin_names = [
        "پلن نمایندگی",
        "منوی ادمین",
        "باشگاه ادمین",
        "پاسارگارد",
        "تنظیمات ادمین",
        "بکاپ",
        "پیام همگانی",
    ]
    reseller_names = ["منوی نماینده", "تنظیمات نماینده"]
    return [
        (
            "shared",
            "دکمه‌های مشترک کاربر",
            "منوی اصلی و جریان خرید — برای همه کاربران ربات اصلی.",
            _pick(shared_names),
        ),
        (
            "admin",
            "منوی ادمین — ربات اصلی",
            "فقط دکمه‌های پنل ادمین روی ربات اصلی. با منوی نماینده قاطی نیست.",
            _pick(admin_names),
        ),
        (
            "reseller",
            "منوی نماینده — هاب فروشگاه",
            "دکمه‌های هاب نماینده (و تنظیمات فروشگاه). جدا از منوی ادمین.",
            _pick(reseller_names),
        ),
    ]


COLORS_PAGE_TABS: list[tuple[str, str, list[str]]] = [
    ("user", "کاربر", ["دکمه‌های سراسری", "منوی اصلی", "زیرمنوها", "فروشگاه", "قوانین", "باشگاه مشتریان"]),
    ("payment", "پرداخت", ["پرداخت"]),
    ("admin", "ادمین", [
        "پلن نمایندگی",
        "منوی ادمین",
        "باشگاه ادمین",
        "پاسارگارد",
        "تنظیمات ادمین",
        "بکاپ",
        "پیام همگانی",
    ]),
    ("reseller", "نماینده", ["منوی نماینده", "تنظیمات نماینده"]),
]


def colors_page_tabs(*, for_reseller: bool = False) -> list[tuple[str, str, list[str]]]:
    if for_reseller:
        return [("shop", "فروشگاه", [])]
    return list(COLORS_PAGE_TABS)


def colors_page_grouped_sections(
    *, for_reseller: bool = False
) -> list[tuple[str, str, str, list[tuple[str, list[dict[str, str]]]]]]:
    """Map catalog groups onto colors-page sub-tabs."""
    flat = grouped_catalog(for_reseller=for_reseller)
    if for_reseller:
        return [("shop", "فروشگاه شما", "رنگ دکمه‌های ربات فروشگاه.", flat)]

    by_name = {name: items for name, items in flat}

    def _pick(names: list[str]) -> list[tuple[str, list[dict[str, str]]]]:
        out: list[tuple[str, list[dict[str, str]]]] = []
        for name in names:
            if name in by_name:
                out.append((name, by_name[name]))
        return out

    sections: list[tuple[str, str, str, list[tuple[str, list[dict[str, str]]]]]] = []
    for tab_id, tab_label, group_names in COLORS_PAGE_TABS:
        groups = _pick(group_names)
        if not groups:
            continue
        if tab_id == "user":
            caption = "منوی اصلی، خرید، قوانین و باشگاه مشتریان."
        elif tab_id == "payment":
            caption = "روش‌های پرداخت و شارژ کیف پول."
        elif tab_id == "admin":
            caption = "دکمه‌های پنل ادمین ربات اصلی."
        else:
            caption = "منوی هاب نماینده و تنظیمات فروشگاه."
        sections.append((tab_id, tab_label, caption, groups))
    return sections


def _style_label(value: str | None) -> str:
    for opt_val, opt_label, _tone in STYLE_OPTIONS:
        if opt_val == (value or ""):
            return opt_label
    return "ارث"


def build_dynamic_color_summary(
    ui: dict | None,
    *,
    support_contacts: list[dict] | None = None,
    payment_cards: list[dict] | None = None,
    payment_gateways: list[dict] | None = None,
    payment_crypto_wallets: list[dict] | None = None,
) -> list[dict[str, str]]:
    """Read-only rows for colors tab — edited in their own settings screens."""
    rows: list[dict[str, str]] = []
    ui = ui or {}

    for contact in support_contacts or []:
        title = str(contact.get("title") or "پشتیبان").strip()
        stored = contact.get("button_style")
        if parse_item_button_style_raw(stored) is None and stored not in ("",):
            label = "ارث از پشتیبانی"
        elif stored == "":
            label = "سفید"
        else:
            label = _style_label(parse_item_button_style_raw(stored))
        rows.append(
            {
                "kind": "support",
                "tab": "user",
                "title": title,
                "color_label": label,
                "edit_url": "/tickets?supports=1",
                "edit_hint": "پشتیبان‌ها",
            }
        )

    for card in payment_cards or []:
        num = str(card.get("number") or "")
        tail = num[-4:] if len(num) >= 4 else num
        title = f"کارت …{tail}" if tail else "کارت"
        stored = card.get("button_style")
        if parse_item_button_style_raw(stored) is None and stored not in ("",):
            label = "ارث از کارت به کارت"
        elif stored == "":
            label = "سفید"
        else:
            label = _style_label(parse_item_button_style_raw(stored))
        rows.append(
            {
                "kind": "pay_card",
                "tab": "payment",
                "title": title,
                "color_label": label,
                "edit_url": "/settings?tab=payment",
                "edit_hint": "پرداخت",
            }
        )

    for gw in payment_gateways or []:
        title = str(gw.get("name") or "درگاه").strip()
        stored = gw.get("button_style")
        if parse_item_button_style_raw(stored) is None and stored not in ("",):
            label = "ارث از درگاه"
        elif stored == "":
            label = "سفید"
        else:
            label = _style_label(parse_item_button_style_raw(stored))
        rows.append(
            {
                "kind": "pay_gateway",
                "tab": "payment",
                "title": title,
                "color_label": label,
                "edit_url": "/settings?tab=payment",
                "edit_hint": "پرداخت",
            }
        )

    for wallet in payment_crypto_wallets or []:
        asset = str(wallet.get("asset") or "USDT").strip()
        addr = str(wallet.get("address") or "")
        tail = addr[-6:] if len(addr) >= 6 else addr
        title = f"{asset} …{tail}" if tail else asset
        stored = wallet.get("button_style")
        if parse_item_button_style_raw(stored) is None and stored not in ("",):
            label = "ارث از رمزارز"
        elif stored == "":
            label = "سفید"
        else:
            label = _style_label(parse_item_button_style_raw(stored))
        rows.append(
            {
                "kind": "pay_crypto",
                "tab": "payment",
                "title": title,
                "color_label": label,
                "edit_url": "/settings?tab=payment",
                "edit_hint": "پرداخت",
            }
        )

    return rows
