"""Reply-keyboard builders and label→action maps.

Extracted from ``app.bot.keyboards`` so that module can stay focused on shared
primitives and inline keyboards. Public names are re-exported from
``app.bot.keyboards`` for backward-compatible imports.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.db.models import Role
from app.services.users import on

# Bind helpers/constants from keyboards after that module has defined them
# (keyboards imports this module mid-file, once primitives exist).
from app.bot import keyboards as _k

BTN_CANCEL = "انصراف"
BTN_RESTART = "🏠 شروع مجدد"  # legacy alias only

REPLY_ACTION_SHOP_CUSTOM = "shop_custom"
REPLY_ACTION_SHOP_WHOLESALE = "shop_wholesale"
REPLY_ACTION_SVC_LINK = "svc_link"
REPLY_ACTION_SVC_RENEW = "svc_renew"
REPLY_ACTION_SVC_ADDON = "svc_addon"
REPLY_ACTION_SVC_AUTO = "svc_auto"
REPLY_ACTION_SVC_REFRESH = "svc_refresh"
REPLY_ACTION_SVC_DELETE = "svc_delete"
REPLY_ACTION_SVC_GUIDE = "svc_guide"
REPLY_ACTION_SVC_CANCEL = "svc_cancel"
REPLY_ACTION_REV_OK = "rev_ok"
REPLY_ACTION_REV_NO = "rev_no"
BTN_REV_OK = "🟢✅ تأیید"
BTN_REV_NO = "🔴❌ رد"

BTN_BACK = _k.BTN_BACK
REPLY_ACTION_ADMIN = _k.REPLY_ACTION_ADMIN
REPLY_ACTION_ADMIN_BACKUP = _k.REPLY_ACTION_ADMIN_BACKUP
REPLY_ACTION_ADMIN_BROADCAST = _k.REPLY_ACTION_ADMIN_BROADCAST
REPLY_ACTION_ADMIN_DASH = _k.REPLY_ACTION_ADMIN_DASH
REPLY_ACTION_ADMIN_LOYALTY = _k.REPLY_ACTION_ADMIN_LOYALTY
REPLY_ACTION_ADMIN_ORDERS = _k.REPLY_ACTION_ADMIN_ORDERS
REPLY_ACTION_ADMIN_PAYMENTS = _k.REPLY_ACTION_ADMIN_PAYMENTS
REPLY_ACTION_ADMIN_PG = _k.REPLY_ACTION_ADMIN_PG
REPLY_ACTION_ADMIN_PLANS = _k.REPLY_ACTION_ADMIN_PLANS
REPLY_ACTION_ADMIN_PREVIEW = _k.REPLY_ACTION_ADMIN_PREVIEW
REPLY_ACTION_ADMIN_REPORTS = _k.REPLY_ACTION_ADMIN_REPORTS
REPLY_ACTION_ADMIN_RESELLERS = _k.REPLY_ACTION_ADMIN_RESELLERS
REPLY_ACTION_ADMIN_SETTINGS = _k.REPLY_ACTION_ADMIN_SETTINGS
REPLY_ACTION_ADMIN_TICKETS = _k.REPLY_ACTION_ADMIN_TICKETS
REPLY_ACTION_ADMIN_USERS = _k.REPLY_ACTION_ADMIN_USERS
REPLY_ACTION_ADM_HUB_OPS = _k.REPLY_ACTION_ADM_HUB_OPS
REPLY_ACTION_ADM_HUB_PEOPLE = _k.REPLY_ACTION_ADM_HUB_PEOPLE
REPLY_ACTION_ADM_HUB_PRODUCT = _k.REPLY_ACTION_ADM_HUB_PRODUCT
REPLY_ACTION_ADM_HUB_SYSTEM = _k.REPLY_ACTION_ADM_HUB_SYSTEM
REPLY_ACTION_ADM_LOY_OVERVIEW = _k.REPLY_ACTION_ADM_LOY_OVERVIEW
REPLY_ACTION_ADM_LOY_REF_TEXT = _k.REPLY_ACTION_ADM_LOY_REF_TEXT
REPLY_ACTION_ADM_LOY_REWARDS = _k.REPLY_ACTION_ADM_LOY_REWARDS
REPLY_ACTION_ADM_LOY_RULES = _k.REPLY_ACTION_ADM_LOY_RULES
REPLY_ACTION_ADM_LOY_SETTINGS = _k.REPLY_ACTION_ADM_LOY_SETTINGS
REPLY_ACTION_ADM_LOY_TIERS = _k.REPLY_ACTION_ADM_LOY_TIERS
REPLY_ACTION_ADM_RES_ADD = _k.REPLY_ACTION_ADM_RES_ADD
REPLY_ACTION_ADM_RES_APPS = _k.REPLY_ACTION_ADM_RES_APPS
REPLY_ACTION_ADM_RES_LIST = _k.REPLY_ACTION_ADM_RES_LIST
REPLY_ACTION_ADM_ST_PANEL = _k.REPLY_ACTION_ADM_ST_PANEL
REPLY_ACTION_ADM_USERS_LIST = _k.REPLY_ACTION_ADM_USERS_LIST
REPLY_ACTION_ADM_USERS_SEARCH = _k.REPLY_ACTION_ADM_USERS_SEARCH
REPLY_ACTION_ADM_USERS_WEB = _k.REPLY_ACTION_ADM_USERS_WEB
REPLY_ACTION_BACK = _k.REPLY_ACTION_BACK
REPLY_ACTION_CREDS = _k.REPLY_ACTION_CREDS
REPLY_ACTION_HOME = _k.REPLY_ACTION_HOME
REPLY_ACTION_LOYALTY = _k.REPLY_ACTION_LOYALTY
REPLY_ACTION_LOY_HISTORY = _k.REPLY_ACTION_LOY_HISTORY
REPLY_ACTION_LOY_POINTS = _k.REPLY_ACTION_LOY_POINTS
REPLY_ACTION_LOY_REFERRAL = _k.REPLY_ACTION_LOY_REFERRAL
REPLY_ACTION_LOY_REWARDS = _k.REPLY_ACTION_LOY_REWARDS
REPLY_ACTION_LOY_WHEEL = _k.REPLY_ACTION_LOY_WHEEL
REPLY_ACTION_PAY_CARD = _k.REPLY_ACTION_PAY_CARD
REPLY_ACTION_PAY_CRYPTO = _k.REPLY_ACTION_PAY_CRYPTO
REPLY_ACTION_PAY_DISCOUNT = _k.REPLY_ACTION_PAY_DISCOUNT
REPLY_ACTION_PAY_GATEWAY = _k.REPLY_ACTION_PAY_GATEWAY
REPLY_ACTION_PAY_PSP = _k.REPLY_ACTION_PAY_PSP
REPLY_ACTION_PAY_STARS = _k.REPLY_ACTION_PAY_STARS
REPLY_ACTION_PAY_WALLET = _k.REPLY_ACTION_PAY_WALLET
REPLY_ACTION_PG_CREATE = _k.REPLY_ACTION_PG_CREATE
REPLY_ACTION_PG_GROUP = _k.REPLY_ACTION_PG_GROUP
REPLY_ACTION_PG_NODES = _k.REPLY_ACTION_PG_NODES
REPLY_ACTION_PG_SEARCH = _k.REPLY_ACTION_PG_SEARCH
REPLY_ACTION_PG_STATS = _k.REPLY_ACTION_PG_STATS
REPLY_ACTION_PG_TEMPLATE = _k.REPLY_ACTION_PG_TEMPLATE
REPLY_ACTION_PG_USERS = _k.REPLY_ACTION_PG_USERS
REPLY_ACTION_RESELLER = _k.REPLY_ACTION_RESELLER
REPLY_ACTION_RESELLER_APPLY = _k.REPLY_ACTION_RESELLER_APPLY
REPLY_ACTION_RES_ADD_REP = _k.REPLY_ACTION_RES_ADD_REP
REPLY_ACTION_RES_PREVIEW = _k.REPLY_ACTION_RES_PREVIEW
REPLY_ACTION_RES_ST_PANEL = _k.REPLY_ACTION_RES_ST_PANEL
REPLY_ACTION_SERVICES = _k.REPLY_ACTION_SERVICES
REPLY_ACTION_SHOP = _k.REPLY_ACTION_SHOP
REPLY_ACTION_SUPPORT = _k.REPLY_ACTION_SUPPORT
REPLY_ACTION_SUPPORT_LIST = _k.REPLY_ACTION_SUPPORT_LIST
REPLY_ACTION_SUPPORT_NEW = _k.REPLY_ACTION_SUPPORT_NEW
REPLY_ACTION_TOPUP_CARD = _k.REPLY_ACTION_TOPUP_CARD
REPLY_ACTION_TOPUP_CRYPTO = _k.REPLY_ACTION_TOPUP_CRYPTO
REPLY_ACTION_TOPUP_GATEWAY = _k.REPLY_ACTION_TOPUP_GATEWAY
REPLY_ACTION_TOPUP_PSP = _k.REPLY_ACTION_TOPUP_PSP
REPLY_ACTION_WALLET = _k.REPLY_ACTION_WALLET
REPLY_ACTION_WALLET_TOPUP = _k.REPLY_ACTION_WALLET_TOPUP
REPLY_ACTION_WALLET_TX = _k.REPLY_ACTION_WALLET_TX
_back_label = _k._back_label
_home_label = _k._home_label
_kb = _k._kb
_menu_layout = _k._menu_layout
_menu_order = _k._menu_order
_style = _k._style
_t = _k._t

def _reply_markup(
    rows: list[list[KeyboardButton]],
    *,
    placeholder: str = "از منوی پایین انتخاب کنید…",
) -> ReplyKeyboardMarkup:
    """Standard reply keyboard for bot menus.

    ``is_persistent=False`` (Telegram default): on Android the system back key
    can hide the custom keyboard first, then leave the chat to the dialog list.
    With ``is_persistent=True`` clients keep the keyboard forced open, so back
    often appears broken (cannot reach message list). The 4-square menu icon
    still reopens the keyboard after hide.

    Lasting chrome (never delete the ReplyKeyboard carrier) remains required so
    iOS does not drop the menu — that is independent of ``is_persistent``.
    """
    return ReplyKeyboardMarkup(
        keyboard=rows or [[_kb(_home_label(), action=REPLY_ACTION_HOME)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=False,
        input_field_placeholder=placeholder,
    )


def _reply_user_entries(
    role: str,
    *,
    has_services: bool,
    ui: dict | None,
    show_reseller_creds: bool = False,
    profile=None,
) -> list[tuple[str, str]]:
    """Ordered (action_key, button_text) for the customer/reseller reply keyboard."""
    from app.bot.nav_mode import is_inline_nav

    inline = is_inline_nav(ui)
    entries: list[tuple[str, str]] = []
    for key in _menu_order(ui):
        if key == "shop":
            entries.append((REPLY_ACTION_SHOP, _t(ui, "btn_shop")))
        elif key == "services" and has_services:
            entries.append((REPLY_ACTION_SERVICES, _t(ui, "btn_services")))
        elif key == "wallet":
            entries.append((REPLY_ACTION_WALLET, _t(ui, "btn_wallet")))
        elif key == "support":
            entries.append((REPLY_ACTION_SUPPORT, _t(ui, "btn_support")))
        elif key == "loyalty":
            # Feature toggle wins over menu_order presence.
            if on(_t(ui, "loyalty_enabled")):
                entries.append((REPLY_ACTION_LOYALTY, _t(ui, "btn_loyalty")))
        elif key == "reseller_apply" and role == Role.USER.value and not show_reseller_creds:
            # Inline nav: rare CTA lives on welcome/support hub, not main KB.
            # Keep registering via reply_action_map for legacy keyboards.
            if not inline:
                entries.append(
                    (REPLY_ACTION_RESELLER_APPLY, _t(ui, "btn_reseller_apply"))
                )
        elif key == "miniapp":
            if inline:
                # web_app KeyboardButton — packed specially in _pack_reply_rows
                entries.append(("miniapp", _t(ui, "btn_miniapp") or "📱 مینی‌اپ"))
            # classic: Mini App stays as separate inline bubble under welcome
            continue
        elif key == "services" and not has_services:
            continue
    if role == Role.RESELLER.value:
        entries.append((REPLY_ACTION_RESELLER, _t(ui, "btn_reseller")))
        if show_reseller_creds:
            entries.append((REPLY_ACTION_ADMIN_PG, _t(ui, "btn_adm_pg")))
    elif show_reseller_creds:
        entries.append((REPLY_ACTION_CREDS, _t(ui, "btn_reseller_creds")))
        # Platform bot: shop owner can renew / buy capacity without opening shop panel
        entries.extend(_reseller_capacity_entries(profile))
        entries.append((REPLY_ACTION_ADMIN_PG, _t(ui, "btn_adm_pg")))
    if role == Role.ADMIN.value:
        entries.append((REPLY_ACTION_ADMIN, _t(ui, "btn_admin")))
    return entries


def _reply_admin_hub_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    """Top-level admin groups (level 0 / NAV_ADMIN) — keeps the hub short."""
    _ = ui
    return [
        (REPLY_ACTION_ADM_HUB_OPS, "🗓 عملیات روزانه"),
        (REPLY_ACTION_ADM_HUB_PEOPLE, "👤 افراد"),
        (REPLY_ACTION_ADM_HUB_PRODUCT, "📦 محصول و PG"),
        (REPLY_ACTION_ADM_HUB_SYSTEM, "🛠 سیستم"),
    ]


def _reply_admin_ops_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_ADMIN_DASH, "📊 داشبورد"),
        (REPLY_ACTION_ADMIN_REPORTS, "📈 گزارشات"),
        (REPLY_ACTION_ADMIN_ORDERS, _t(ui, "btn_adm_orders")),
        (REPLY_ACTION_ADMIN_PAYMENTS, _t(ui, "btn_adm_payments")),
        (REPLY_ACTION_ADMIN_TICKETS, _t(ui, "btn_adm_tickets")),
    ]


def _reply_admin_people_entries(
    ui: dict | None = None,
    *,
    can_manage_representatives: bool = True,
) -> list[tuple[str, str]]:
    entries = [
        (REPLY_ACTION_ADMIN_USERS, _t(ui, "btn_adm_users")),
        (REPLY_ACTION_ADMIN_RESELLERS, "🤝 نمایندگان"),
        (REPLY_ACTION_ADMIN_LOYALTY, "⭐ باشگاه مشتریان"),
    ]
    if not can_manage_representatives:
        entries = [e for e in entries if e[0] != REPLY_ACTION_ADMIN_RESELLERS]
    return entries


def _reply_admin_product_entries(
    ui: dict | None = None,
    *,
    pg_features: frozenset[str] | set[str] | None = None,
) -> list[tuple[str, str]]:
    entries = [
        (REPLY_ACTION_ADMIN_PLANS, _t(ui, "btn_adm_plans")),
        (REPLY_ACTION_ADMIN_PG, _t(ui, "btn_adm_pg")),
    ]
    if pg_features is not None and not pg_features:
        entries = [e for e in entries if e[0] != REPLY_ACTION_ADMIN_PG]
    return entries


def _reply_admin_system_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_ADMIN_SETTINGS, _t(ui, "btn_adm_settings")),
        (REPLY_ACTION_ADMIN_BROADCAST, _t(ui, "btn_adm_broadcast")),
        (REPLY_ACTION_ADMIN_BACKUP, "💾 بکاپ / ریستور"),
        (REPLY_ACTION_ADMIN_PREVIEW, _t(ui, "btn_adm_preview")),
    ]


def _reply_admin_entries(
    ui: dict | None = None,
    *,
    pg_features: frozenset[str] | set[str] | None = None,
    can_manage_representatives: bool = True,
) -> list[tuple[str, str]]:
    """All admin leaf buttons (for action-map registration / ACL filtering)."""
    return [
        *_reply_admin_ops_entries(ui),
        *_reply_admin_people_entries(
            ui, can_manage_representatives=can_manage_representatives
        ),
        *_reply_admin_product_entries(ui, pg_features=pg_features),
        *_reply_admin_system_entries(ui),
    ]


def _pg_submenu_entries(
    ui: dict | None = None,
    *,
    features: frozenset[str] | set[str] | None = None,
    can_create_user: bool = True,
) -> list[tuple[str, str]]:
    """PasarGuard reply submenu — filtered by Hybrid PG feature keys when given."""
    _ = ui
    feats = features
    all_entries = [
        (REPLY_ACTION_PG_STATS, "🏠 نمای کلی", "pg_overview"),
        (REPLY_ACTION_PG_USERS, "👥 کاربران", "pg_users"),
        (REPLY_ACTION_PG_CREATE, "➕ ساخت کاربر", "pg_users"),
        (REPLY_ACTION_PG_SEARCH, "🔎 جستجوی یوزر", "pg_users"),
        (REPLY_ACTION_PG_NODES, "🕸 نودها", "pg_nodes"),
        (REPLY_ACTION_PG_GROUP, "📁 ساخت گروه", "pg_groups"),
        (REPLY_ACTION_PG_TEMPLATE, "📋 ساخت تمپلیت", "pg_templates"),
    ]
    out: list[tuple[str, str]] = []
    for key, label, feat in all_entries:
        if feats is not None and feat not in feats:
            continue
        if key == REPLY_ACTION_PG_CREATE and not can_create_user:
            continue
        out.append((key, label))
    return out


def _admin_users_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    return [
        (REPLY_ACTION_ADM_USERS_LIST, "📋 لیست کاربران"),
        (REPLY_ACTION_ADM_USERS_SEARCH, "🔎 جستجو با آیدی تلگرام"),
        (REPLY_ACTION_ADM_USERS_WEB, "🌐 مدیریت کامل در وب‌پنل"),
    ]


def _admin_resellers_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    return [
        (REPLY_ACTION_ADM_RES_LIST, "📋 لیست نمایندگان"),
        (REPLY_ACTION_ADM_RES_APPS, "📋 درخواست‌های منتظر"),
        (REPLY_ACTION_ADM_RES_ADD, "➕ افزودن دستی"),
    ]


def _admin_settings_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    # Labels MUST match admin_settings.SECTIONS[*]["title"] for HUB_ORDER keys
    return [
        ("adm_st_shop", "فروشگاه"),
        ("adm_st_menu", "منو"),
        ("adm_st_pay", "پرداخت"),
        ("adm_st_support", "پشتیبان‌ها"),
        ("adm_st_access", "دسترسی"),
        ("adm_st_limits", "محدودیت"),
        ("adm_st_guides", "آموزش اتصال"),
        ("adm_st_notify", "اعلان‌ها"),
        (REPLY_ACTION_ADM_ST_PANEL, "🌐 وب‌پنل"),
    ]


def _admin_backup_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    # Phase 2: default create is without .env; with-.env is a separate confirm path.
    return [
        ("backup_create", "🆕 ساخت بکاپ"),
        ("backup_create_env", "🆕 بکاپ + .env"),
        ("backup_upload", "📤 آپلود فایل بکاپ"),
        ("backup_refresh", "🔄 تازه‌سازی لیست"),
    ]


def _admin_broadcast_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    # Labels must NOT collide with admin hub («🤝 نمایندگان» → adm_resellers).
    return [
        ("bc_aud_all", "📢 همه"),
        ("bc_aud_users", "👥 کاربران عادی"),
        ("bc_aud_resellers", "🤝 فقط نمایندگان"),
        ("bc_aud_admins", "🛠 ادمین‌ها"),
    ]


def _admin_plans_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    return []


def _admin_plans_audience_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    """Labels MUST NOT collide with btn_adm_users («👥 کاربران») or admin resellers hub."""
    _ = ui
    return [
        (REPLY_ACTION_ADM_PLANS_AUD_USERS, "📦 پلن‌های کاربران"),
        (REPLY_ACTION_ADM_PLANS_AUD_RESELLERS, "🤝 پلن‌های نمایندگان"),
        (REPLY_ACTION_ADM_PLANS_CATEGORIES, "🏷 برچسب دسته"),
        (REPLY_ACTION_ADM_PLANS_ADDONS, "⏱ بسته حجم/زمان"),
    ]


def _admin_plans_list_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    """Reply keyboard on audience list screen — add plan + catalog extras (web parity)."""
    _ = ui
    return [
        (REPLY_ACTION_ADM_PLANS_ADD, "➕ افزودن پلن"),
        (REPLY_ACTION_ADM_PLANS_CATEGORIES, "🏷 برچسب دسته"),
        (REPLY_ACTION_ADM_PLANS_ADDONS, "⏱ بسته حجم/زمان"),
    ]


def _admin_plans_add_type_entries(audience: str, ui: dict | None = None) -> list[tuple[str, str]]:
    """Plan type picker shown after «افزودن پلن» — mirrors web modal kind step."""
    _ = ui
    if audience == "resellers":
        return [
            (REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED, "📦 اشتراک ثابت"),
            (REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG, "⚡ اشتراک PAYG"),
            (REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL, "📦 بسته حجم"),
            (REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS, "👤 بسته کاربر"),
        ]
    return [
        (REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED, "💎 ثابت"),
        (REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM, "✨ دلخواه"),
        (REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL, "🎁 تست"),
        (REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE, "📦 فروش عمده"),
    ]


def _admin_plans_kind_entries(audience: str, ui: dict | None = None) -> list[tuple[str, str]]:
    """Alias kept for nav restore — list screen uses add-only reply keyboard."""
    return _admin_plans_list_entries(ui)


REPLY_ACTION_ADM_PLANS_AUD_USERS = "adm_plans_aud_users"
REPLY_ACTION_ADM_PLANS_AUD_RESELLERS = "adm_plans_aud_resellers"
REPLY_ACTION_ADM_PLANS_ADD = "adm_plans_add"
REPLY_ACTION_ADM_PLANS_CATEGORIES = "adm_plans_categories"
REPLY_ACTION_ADM_PLANS_ADDONS = "adm_plans_addons"
REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED = "adm_plans_kind_users_fixed"
REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM = "adm_plans_kind_users_custom"
REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL = "adm_plans_kind_users_trial"
REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE = "adm_plans_kind_users_wholesale"
REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED = "adm_plans_kind_res_fixed"
REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG = "adm_plans_kind_res_payg"
REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL = "adm_plans_kind_res_addon_vol"
REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS = "adm_plans_kind_res_addon_users"
REPLY_ACTION_RES_PLAN_CATEGORIES = "res_plan_categories"
REPLY_ACTION_RES_PLAN_ADDONS = "res_plan_addons"


def _reseller_plans_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    return [
        ("res_plan_add", "➕ پلن جدید"),
        (REPLY_ACTION_RES_PLAN_CATEGORIES, "🏷 برچسب دسته"),
        (REPLY_ACTION_RES_PLAN_ADDONS, "⏱ بسته حجم/زمان"),
    ]


def _reseller_settings_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    # Labels MUST match reseller_settings.SECTIONS[*]["title"] / HUB_ORDER
    return [
        ("res_st_shop", "فروشگاه"),
        ("res_st_menu", "منو"),
        ("res_st_pay", "پرداخت"),
        ("res_st_support", "پشتیبان‌ها"),
        ("res_st_access", "دسترسی"),
        ("res_st_limits", "محدودیت"),
        ("res_st_guides", "آموزش اتصال"),
        ("res_st_notify", "اعلان‌ها"),
        ("res_st_bot", "ربات"),
        (REPLY_ACTION_RES_ST_PANEL, "🌐 وب‌پنل"),
    ]


def _wallet_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_WALLET_TOPUP, _t(ui, "btn_wallet_topup") or "🟢➕ شارژ کیف پول"),
        (REPLY_ACTION_WALLET_TX, _t(ui, "btn_wallet_tx") or "🟡📜 تراکنش‌ها"),
    ]


def _support_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_SUPPORT_NEW, _t(ui, "btn_support_new") or "🟣✉️ تیکت جدید"),
        (REPLY_ACTION_SUPPORT_LIST, _t(ui, "btn_support_list") or "📋 تیکت‌های من"),
    ]


def _loyalty_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    """Customer club main subsets on reply keyboard (invite is one subset).

    Secondary actions (share link, redeem, pagination, my-discounts) stay inline.
    Order from loyalty_submenu_order CSV; club button position uses main menu_order.
    Wheel is omitted when ``lucky_wheel_enabled`` is off (toggle beats CSV).
    """
    catalog = {
        REPLY_ACTION_LOY_REFERRAL: _t(ui, "btn_referral") or "👥 دعوت دوستان",
        REPLY_ACTION_LOY_POINTS: _t(ui, "btn_loy_points") or "⭐ امتیاز من",
        REPLY_ACTION_LOY_REWARDS: _t(ui, "btn_loy_rewards") or "🎁 جوایز",
        REPLY_ACTION_LOY_WHEEL: _t(ui, "btn_loy_wheel") or "🎡 چرخ شانس",
        REPLY_ACTION_LOY_HISTORY: _t(ui, "btn_loy_history") or "📜 تاریخچه",
    }
    from app.services.lucky_wheel import parse_submenu_order

    order = parse_submenu_order(_t(ui, "loyalty_submenu_order"), fill_missing=False)
    wheel_on = on(_t(ui, "lucky_wheel_enabled"))
    return [
        (key, catalog[key])
        for key in order
        if key in catalog and (key != REPLY_ACTION_LOY_WHEEL or wheel_on)
    ]


def _admin_loyalty_submenu_entries(ui: dict | None = None, *, include_tiers: bool = True) -> list[tuple[str, str]]:
    """Staff club management subsets (platform admin / reseller with loyalty perm)."""
    _ = ui
    entries = [
        (REPLY_ACTION_ADM_LOY_OVERVIEW, "📊 نمای کلی"),
        (REPLY_ACTION_ADM_LOY_RULES, "📐 قوانین امتیاز"),
        (REPLY_ACTION_ADM_LOY_REWARDS, "🎁 جوایز"),
    ]
    if include_tiers:
        entries.append((REPLY_ACTION_ADM_LOY_TIERS, "🏅 سطوح"))
    entries.extend(
        [
            (REPLY_ACTION_ADM_LOY_SETTINGS, "⚙️ تنظیمات باشگاه"),
            (REPLY_ACTION_ADM_LOY_REF_TEXT, "📝 متن دعوت"),
        ]
    )
    return entries


def _reseller_capacity_entries(profile=None) -> list[tuple[str, str]]:
    """Renew + unified volume/user packs hub — shop bot and main-bot shop owner.

    Unit buy-extra (GB/users) and catalog addon packs share one reply button
    («بسته‌های حجم/کاربر») so the hub is not cluttered with three similar entries.
    """
    if profile is None:
        return []
    entries: list[tuple[str, str]] = []
    try:
        from app.services.pg_admin_subscription import is_subscription_plan
        from app.services.reseller_capacity import plan_allows_buy_extra

        plan = getattr(profile, "plan", None)
        if plan is not None and is_subscription_plan(plan):
            # One hub: catalog packs + unit buy-extra (when plan allows).
            entries.append(("res_addon_packs", "📦 بسته‌های حجم/کاربر"))
            entries.append(("res_renew", "🔄 تمدید سرویس"))
        elif plan is not None and plan_allows_buy_extra(plan):
            # Subscription check failed closed above; still expose unit extras
            # only when allow_buy_extra is set (rare non-subscription edge).
            entries.append(("res_addon_packs", "📦 بسته‌های حجم/کاربر"))
    except Exception:
        # Fail closed — do not expose capacity without a verified subscription plan
        pass
    return entries


def _reseller_submenu_entries(
    profile=None, *, can_add_representative: bool = False
) -> list[tuple[str, str]]:
    from app.services.authz import shop_feature_allowed

    # Fail closed: without a live profile show nothing (matches reply_action_map)
    if profile is None:
        return []
    entries: list[tuple[str, str]] = []
    if shop_feature_allowed(key="dashboard", profile=profile):
        entries.append(("res_dash", "🏠 خانه نماینده"))
        entries.append(("res_users", "👥 مشتریان من"))
    if can_add_representative:
        entries.append((REPLY_ACTION_RES_ADD_REP, "➕ افزودن نماینده"))
    # PAYG billing wallet — only when mode is payg
    try:
        from app.services.billing import is_payg

        if is_payg(profile):
            entries.append(("res_billing", "💰 کیف پول PAYG"))
    except Exception:
        pass
    entries.extend(_reseller_capacity_entries(profile))
    if shop_feature_allowed(key="orders", profile=profile) or shop_feature_allowed(
        key="payments", profile=profile
    ):
        entries.append(("res_reports", "📈 گزارشات"))
    if shop_feature_allowed(key="stats", profile=profile):
        entries.append(("res_stats", "📊 آمار"))
    if shop_feature_allowed(key="plans", profile=profile):
        entries.append(("res_plans", "💎 پلن‌های فروش"))
    if shop_feature_allowed(key="orders", profile=profile):
        entries.append(("res_orders", "🛒 سفارش‌های مشتریان"))
    if shop_feature_allowed(key="payments", profile=profile):
        entries.append(("res_payments", "🧾 رسیدهای در انتظار"))
    if shop_feature_allowed(key="tickets", profile=profile):
        entries.append(("res_tickets", "🎫 تیکت‌های مشتریان"))
    if shop_feature_allowed(key="loyalty", profile=profile):
        entries.append(("res_loyalty", "⭐ باشگاه مشتریان"))
    if shop_feature_allowed(key="shop_settings", profile=profile):
        entries.append(("res_settings", "⚙️ تنظیمات فروشگاه"))
    # Shop owner is admin of their bot — preview customer keyboard
    entries.append((REPLY_ACTION_RES_PREVIEW, "👁 پیش‌نمایش منوی کاربر"))
    return entries


def reseller_hub_main_keyboard(
    profile=None,
    ui: dict | None = None,
    *,
    can_add_representative: bool = False,
) -> ReplyKeyboardMarkup:
    """Primary keyboard for shop owner/staff on their dedicated bot.

    Inline: thin entry (manage panel + user preview). Nested leaves live on
    the edited inline hub. Classic: full manage reply hub (rollback).
    """
    from app.bot.nav_mode import is_inline_nav

    if is_inline_nav(ui):
        _ = (profile, can_add_representative)  # ACL applied inside inline hub
        entries = [
            (REPLY_ACTION_RESELLER, "🤝 پنل مدیریت"),
            (REPLY_ACTION_RES_PREVIEW, "👁 پیش‌نمایش منوی کاربر"),
        ]
        rows = _pack_reply_rows(entries, ui, footer=[])
        return _reply_markup(
            rows or [[_kb(_home_label(ui), action=REPLY_ACTION_HOME, ui=ui)]],
            placeholder="از منوی پایین انتخاب کنید…",
        )
    entries = _reseller_submenu_entries(
        profile, can_add_representative=can_add_representative
    )
    rows = _pack_reply_rows(entries, ui, footer=[(REPLY_ACTION_HOME, _home_label(ui))])
    return _reply_markup(
        rows or [[_kb(_home_label(ui), action=REPLY_ACTION_HOME, ui=ui)]],
        placeholder="پنل مدیریت فروشگاه…",
    )


def _pay_method_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if on(_t(ui, "pay_wallet_enabled")):
        entries.append((REPLY_ACTION_PAY_WALLET, _t(ui, "btn_pay_wallet")))
    if on(_t(ui, "pay_card_enabled")):
        entries.append((REPLY_ACTION_PAY_CARD, _t(ui, "btn_pay_card")))
    if on(_t(ui, "pay_gateway_enabled")):
        entries.append((REPLY_ACTION_PAY_GATEWAY, _t(ui, "btn_pay_gateway")))
    if on(_t(ui, "pay_psp_enabled")):
        entries.append((REPLY_ACTION_PAY_PSP, _t(ui, "btn_pay_psp")))
    if on(_t(ui, "pay_crypto_enabled")):
        entries.append((REPLY_ACTION_PAY_CRYPTO, _t(ui, "btn_pay_crypto")))
    if on(_t(ui, "pay_stars_enabled")):
        entries.append((REPLY_ACTION_PAY_STARS, _t(ui, "btn_pay_stars")))
    if on(_t(ui, "pay_discount_enabled")):
        entries.append((REPLY_ACTION_PAY_DISCOUNT, _t(ui, "btn_pay_discount")))
    return entries


def _topup_method_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if on(_t(ui, "pay_card_enabled")):
        entries.append((REPLY_ACTION_TOPUP_CARD, _t(ui, "btn_pay_card")))
    if on(_t(ui, "pay_gateway_enabled")):
        entries.append((REPLY_ACTION_TOPUP_GATEWAY, _t(ui, "btn_pay_gateway")))
    if on(_t(ui, "pay_psp_enabled")):
        entries.append((REPLY_ACTION_TOPUP_PSP, _t(ui, "btn_pay_psp")))
    if on(_t(ui, "pay_crypto_enabled")):
        entries.append((REPLY_ACTION_TOPUP_CRYPTO, _t(ui, "btn_pay_crypto")))
    return entries


def _pack_reply_button(action: str, text: str, ui: dict | None) -> KeyboardButton | None:
    """Build one reply button; Mini App uses web_app (no text-message action)."""
    if action == "miniapp":
        from app.bot.nav_inline import miniapp_reply_button

        return miniapp_reply_button(ui)
    if not (text or "").strip():
        return None
    return _kb(text, action=action, ui=ui)


def _pack_reply_rows(
    entries: list[tuple[str, str]],
    ui: dict | None,
    *,
    footer: list[str | tuple[str, str]] | None = None,
    footer_row: list[str | tuple[str, str]] | None = None,
) -> list[list[KeyboardButton]]:
    layout = _menu_layout(ui)
    items = [(a, t) for a, t in entries if (t or "").strip() or a == "miniapp"]
    rows: list[list[KeyboardButton]] = []
    if layout == "compact":
        for i in range(0, len(items), 2):
            chunk = items[i : i + 2]
            btns = [b for a, t in chunk if (b := _pack_reply_button(a, t, ui)) is not None]
            if btns:
                rows.append(btns)
    else:
        for a, t in items:
            btn = _pack_reply_button(a, t, ui)
            if btn is not None:
                rows.append([btn])

    def _footer_btns(items_in: list[str | tuple[str, str]]) -> list[KeyboardButton]:
        out: list[KeyboardButton] = []
        for item in items_in:
            if isinstance(item, tuple):
                action, text = item[0], item[1]
            else:
                action, text = None, item
            if (text or "").strip():
                out.append(_kb(text, action=action, ui=ui))
        return out

    if footer_row:
        row = _footer_btns(list(footer_row))
        if row:
            rows.append(row)
    for item in footer or []:
        row = _footer_btns([item])
        if row:
            rows.append(row)
    return rows


def _submenu_footer(ui: dict | None = None) -> list[tuple[str, str]]:
    """Back (one level) + Main menu — always on submenu keyboards."""
    return [
        (REPLY_ACTION_BACK, _back_label(ui)),
        (REPLY_ACTION_HOME, _home_label(ui)),
    ]


def main_reply_keyboard(
    role: str,
    *,
    has_services: bool = False,
    ui: dict | None = None,
    as_user: bool = False,
    show_reseller_creds: bool = False,
    profile=None,
    pg_features: frozenset[str] | set[str] | None = None,
    can_manage_representatives: bool = True,
) -> ReplyKeyboardMarkup:
    """Primary navigation reply keyboard (level 0).

    Inline: admin gets the customer menu + «پنل ادمین» (groups live on the
    edited inline panel). Classic: admin keeps the short 4-group reply hub.
    """
    from app.bot.nav_mode import is_inline_nav

    home_label = _home_label(ui)
    inline = is_inline_nav(ui)
    # Inline nav: no Home row on level-0 (pointless at home); classic keeps footer.
    home_footer: list[tuple[str, str]] = (
        [] if inline else [(REPLY_ACTION_HOME, home_label)]
    )
    if role == Role.ADMIN.value and not as_user and not inline:
        # Classic rollback: short 4-group hub on reply keyboard.
        _ = (pg_features, can_manage_representatives)  # ACL applied inside groups
        entries = _reply_admin_hub_entries(ui)
        rows = _pack_reply_rows(entries, ui, footer=home_footer)
    else:
        # Inline admin / preview / customer — user entries (+ btn_admin for admin).
        map_role = Role.USER.value if as_user else role
        entries = _reply_user_entries(
            map_role,
            has_services=has_services,
            ui=ui,
            show_reseller_creds=False if as_user else show_reseller_creds,
            profile=None if as_user else profile,
        )
        rows = _pack_reply_rows(entries, ui, footer=home_footer)
    return _reply_markup(rows, placeholder="از منوی پایین انتخاب کنید…")


def admin_reply_keyboard(
    ui: dict | None = None,
    *,
    pg_features: frozenset[str] | set[str] | None = None,
    can_manage_representatives: bool = True,
) -> ReplyKeyboardMarkup:
    """Admin hub groups as an explicit submenu (with back + home)."""
    _ = (pg_features, can_manage_representatives)  # ACL applied inside groups
    rows = _pack_reply_rows(
        _reply_admin_hub_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="پنل ادمین…")


def admin_ops_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _reply_admin_ops_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="عملیات روزانه…")


def admin_people_reply_keyboard(
    ui: dict | None = None,
    *,
    can_manage_representatives: bool = True,
) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _reply_admin_people_entries(
            ui, can_manage_representatives=can_manage_representatives
        ),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="افراد…")


def admin_product_reply_keyboard(
    ui: dict | None = None,
    *,
    pg_features: frozenset[str] | set[str] | None = None,
) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _reply_admin_product_entries(ui, pg_features=pg_features),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="محصول و PG…")


def admin_system_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _reply_admin_system_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="سیستم…")


def wallet_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _wallet_submenu_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="کیف پول — یک گزینه را انتخاب کنید…")


def support_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _support_submenu_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="پشتیبانی — یک گزینه را انتخاب کنید…")


def loyalty_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _loyalty_submenu_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="باشگاه مشتریان — یک گزینه را انتخاب کنید…")


def admin_loyalty_reply_keyboard(
    ui: dict | None = None, *, include_tiers: bool = True
) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _admin_loyalty_submenu_entries(ui, include_tiers=include_tiers),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="مدیریت باشگاه مشتریان…")


def reseller_reply_keyboard(profile=None, ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Reply-keyboard mirror of reseller panel sections."""
    entries = _reseller_submenu_entries(profile)
    rows = _pack_reply_rows(entries, ui, footer_row=_submenu_footer(ui))
    return _reply_markup(
        rows or [[_kb(_home_label(ui), action=REPLY_ACTION_HOME, ui=ui)]],
        placeholder="پنل نماینده…",
    )


def pay_reply_keyboard(order_id: int, ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Order payment methods on reply keyboard (order_id kept in FSM)."""
    _ = order_id  # identity reserved for callers / future per-order labels
    rows = _pack_reply_rows(_pay_method_entries(ui), ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="روش پرداخت را انتخاب کنید…")


def topup_pay_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(_topup_method_entries(ui), ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="روش شارژ را انتخاب کنید…")


def pg_reply_keyboard(
    ui: dict | None = None,
    *,
    features: frozenset[str] | set[str] | None = None,
    can_create_user: bool = True,
) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _pg_submenu_entries(ui, features=features, can_create_user=can_create_user),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="پاسارگارد — یک گزینه را انتخاب کنید…")


def admin_users_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(_admin_users_submenu_entries(ui), ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="کاربران بات…")


def admin_resellers_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _admin_resellers_submenu_entries(ui), ui, footer_row=_submenu_footer(ui)
    )
    return _reply_markup(rows, placeholder="نمایندگان…")


def admin_settings_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _admin_settings_submenu_entries(ui), ui, footer_row=_submenu_footer(ui)
    )
    return _reply_markup(rows, placeholder="تنظیمات…")


def admin_backup_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _admin_backup_submenu_entries(ui), ui, footer_row=_submenu_footer(ui)
    )
    return _reply_markup(rows, placeholder="بکاپ / ریستور…")


def admin_broadcast_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _admin_broadcast_submenu_entries(ui), ui, footer_row=_submenu_footer(ui)
    )
    return _reply_markup(rows, placeholder="مخاطب پیام گروهی…")


def admin_plans_audience_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _admin_plans_audience_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="مخاطب پلن را انتخاب کنید…")


def admin_plans_kind_reply_keyboard(
    audience: str,
    ui: dict | None = None,
) -> ReplyKeyboardMarkup:
    """List screen reply keyboard — «افزودن پلن» + back/home (audience kept for API compat)."""
    _ = audience
    rows = _pack_reply_rows(
        _admin_plans_list_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="پلن را از پیام انتخاب کنید یا افزودن…")


def admin_plans_add_type_reply_keyboard(
    audience: str,
    ui: dict | None = None,
) -> ReplyKeyboardMarkup:
    """Type picker on reply keyboard after «افزودن پلن»."""
    rows = _pack_reply_rows(
        _admin_plans_add_type_entries(audience, ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="نوع پلن را انتخاب کنید…")


def admin_plans_reply_keyboard(
    ui: dict | None = None,
    *,
    audience: str | None = None,
) -> ReplyKeyboardMarkup:
    """Plans hub: audience or kind step on reply keyboard."""
    if audience in {"users", "resellers"}:
        return admin_plans_kind_reply_keyboard(audience, ui)
    return admin_plans_audience_reply_keyboard(ui)


def reseller_settings_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _reseller_settings_submenu_entries(ui), ui, footer_row=_submenu_footer(ui)
    )
    return _reply_markup(rows, placeholder="تنظیمات فروشگاه نماینده…")


def reseller_plans_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Static chrome for reseller plans (list stays inline under the message)."""
    rows = _pack_reply_rows(
        _reseller_plans_submenu_entries(ui), ui, footer_row=_submenu_footer(ui)
    )
    return _reply_markup(rows, placeholder="پلن‌های فروشگاه…")

def reply_action_map(
    role: str,
    *,
    has_services: bool = False,
    ui: dict | None = None,
    as_user: bool = False,
    show_reseller_creds: bool = False,
    include_submenus: bool = True,
    is_reseller_bot: bool = False,
    profile=None,
    can_add_representative: bool = False,
) -> dict[str, str]:
    """Map button label → action key for the current role/bot.

    Security: admin/PG labels are never registered on reseller bots or for
    non-admin roles. Reseller panel labels only for reseller actors on shop bots,
    and only for permissions the profile actually has (no forgeable restricted
    labels).
    """
    home_label = _home_label(ui)
    back_label = _back_label(ui)
    mapping: dict[str, str] = {
        home_label: REPLY_ACTION_HOME,
        back_label: REPLY_ACTION_BACK,
        BTN_BACK: REPLY_ACTION_BACK,
    }
    # Legacy aliases
    mapping[BTN_RESTART] = REPLY_ACTION_HOME
    mapping["شروع مجدد"] = REPLY_ACTION_HOME
    mapping["🏠 شروع مجدد"] = REPLY_ACTION_HOME

    # Platform admin tools only on the main (non-reseller) bot
    platform_admin = (
        role == Role.ADMIN.value and not as_user and not is_reseller_bot
    )
    reseller_actor = role == Role.RESELLER.value and is_reseller_bot

    if platform_admin:
        from app.bot.nav_mode import is_inline_nav

        if is_inline_nav(ui):
            # Pure-inline: main KB is customer menu + پنل ادمین
            for key, text in _reply_user_entries(
                role,
                has_services=has_services,
                ui=ui,
                show_reseller_creds=show_reseller_creds,
                profile=profile if show_reseller_creds else None,
            ):
                if key == "miniapp":
                    continue
                mapping[(text or "").strip()] = key
            admin_label = (_t(ui, "btn_admin") or "").strip()
            if admin_label:
                mapping[admin_label] = REPLY_ACTION_ADMIN
        # Stale classic labels / rollback maps (leaves then hub groups)
        for key, text in _reply_admin_entries(ui):
            mapping.setdefault((text or "").strip(), key)
        for key, text in _reply_admin_hub_entries(ui):
            mapping[(text or "").strip()] = key
    else:
        # On reseller bots, never expose platform-admin entry even if role string is admin
        map_role = Role.USER.value if (is_reseller_bot and role == Role.ADMIN.value) else role
        for key, text in _reply_user_entries(
            map_role,
            has_services=has_services,
            ui=ui,
            show_reseller_creds=show_reseller_creds,
            profile=profile if (show_reseller_creds and not is_reseller_bot) else None,
        ):
            if key == "miniapp":
                continue  # web_app button — no text action
            mapping[(text or "").strip()] = key
        # Legacy: reseller-apply label stays mapped even when omitted from main KB
        if map_role == Role.USER.value and not show_reseller_creds:
            apply_label = (_t(ui, "btn_reseller_apply") or "").strip()
            if apply_label:
                mapping.setdefault(apply_label, REPLY_ACTION_RESELLER_APPLY)
        # L1 on the platform bot: register migrated PG submenu labels (not overview).
        if show_reseller_creds and not is_reseller_bot:
            l1_pg = frozenset(
                {"pg_users", "pg_nodes", "pg_templates", "pg_groups"}
            )
            for key, text in _pg_submenu_entries(ui, features=l1_pg):
                mapping[(text or "").strip()] = key
            # Capacity labels (renew / extras) — also registered via _reply_user_entries
            for key, text in _reseller_capacity_entries(profile):
                mapping[(text or "").strip()] = key
        # Preview escape on main bot only
        if role == Role.ADMIN.value and as_user and not is_reseller_bot:
            mapping[(_t(ui, "btn_admin") or "").strip()] = REPLY_ACTION_ADMIN

    if include_submenus:
        # Customer surfaces (wallet/support/loyalty/pay) — everyone except pure admin hub
        if not platform_admin:
            for key, text in _wallet_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _support_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _loyalty_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _pay_method_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _topup_method_entries(ui):
                mapping[(text or "").strip()] = key
            # Legacy service-action reply labels (stale keyboards after Wave B)
            for key, text in _service_action_entries(ui):
                mapping.setdefault((text or "").strip(), key)

        if platform_admin:
            for key, text in _wallet_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _support_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _loyalty_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _admin_loyalty_submenu_entries(ui, include_tiers=True):
                mapping[(text or "").strip()] = key
            for key, text in _pay_method_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _topup_method_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _pg_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _admin_users_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _admin_resellers_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _admin_settings_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _admin_backup_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _admin_broadcast_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _admin_plans_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            # Leaves then hub groups — hub labels win collisions
            for key, text in _reply_admin_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _reply_admin_hub_entries(ui):
                mapping[(text or "").strip()] = key

        # Legacy shop custom/wholesale labels (old reply keyboards) — route to kind flow
        for key, text in _shop_submenu_entries(
            ui, custom_enabled=True, wholesale_enabled=True
        ):
            mapping.setdefault((text or "").strip(), key)
        if platform_admin:
            for key, text in _admin_plans_audience_entries(ui):
                mapping.setdefault((text or "").strip(), key)
            for aud in ("users", "resellers"):
                for key, text in _admin_plans_list_entries(ui):
                    mapping.setdefault((text or "").strip(), key)
                for key, text in _admin_plans_add_type_entries(aud, ui):
                    mapping.setdefault((text or "").strip(), key)

        if reseller_actor:
            from app.bot.nav_mode import is_inline_nav
            from app.services.authz import shop_feature_allowed

            # Pure-inline thin entry labels (always, even without profile)
            mapping["🤝 پنل مدیریت"] = REPLY_ACTION_RESELLER
            mapping["👁 پیش‌نمایش منوی کاربر"] = REPLY_ACTION_RES_PREVIEW
            # Fail closed: without a live profile, register no reseller panel leaves
            if profile is not None:
                for key, text in _reseller_submenu_entries(
                    profile, can_add_representative=can_add_representative
                ):
                    # Under inline, thin entry owns the manage label; leaves stay for classic/stale
                    if is_inline_nav(ui) and key == "res_dash":
                        continue
                    mapping[(text or "").strip()] = key
                if shop_feature_allowed(key="shop_settings", profile=profile):
                    for key, text in _reseller_settings_submenu_entries(ui):
                        mapping[(text or "").strip()] = key
                if shop_feature_allowed(key="plans", profile=profile):
                    for key, text in _reseller_plans_submenu_entries(ui):
                        mapping[(text or "").strip()] = key
                if shop_feature_allowed(key="loyalty", profile=profile):
                    # Resellers manage shop-scoped club; tiers stay platform-global
                    for key, text in _admin_loyalty_submenu_entries(ui, include_tiers=False):
                        mapping[(text or "").strip()] = key

    return {k: v for k, v in mapping.items() if k}

def _shop_submenu_entries(
    ui: dict | None = None,
    *,
    custom_enabled: bool = False,
    wholesale_enabled: bool = False,
) -> list[tuple[str, str]]:
    """Legacy labels for stale reply keyboards — not shown on new shop KB."""
    entries: list[tuple[str, str]] = []
    if custom_enabled:
        entries.append((REPLY_ACTION_SHOP_CUSTOM, "✨ پلن دلخواه"))
    if wholesale_enabled:
        entries.append(
            (REPLY_ACTION_SHOP_WHOLESALE, _t(ui, "btn_wholesale") or "📦 فروش عمده")
        )
    return entries

def submenu_chrome_reply_keyboard(
    ui: dict | None = None,
    *,
    placeholder: str = "از کیبورد پایین بازگردید…",
) -> ReplyKeyboardMarkup:
    """Back + Home only — for screens whose choices live on inline keyboards."""
    rows = _pack_reply_rows([], ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder=placeholder)


def shop_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Shop nav chrome — plan kind + lists are inline under the message."""
    return submenu_chrome_reply_keyboard(
        ui,
        placeholder="فروشگاه — نوع پلن را از زیر پیام انتخاب کنید…",
    )


def reseller_apply_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Reseller-apply chrome — plan mode/list are inline under the message."""
    return submenu_chrome_reply_keyboard(
        ui,
        placeholder="درخواست نمایندگی — نوع پلن را از زیر پیام انتخاب کنید…",
    )


def _service_action_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_SVC_LINK, _t(ui, "btn_sub_link")),
        (REPLY_ACTION_SVC_GUIDE, _t(ui, "btn_guides") or "📘 آموزش اتصال"),
        (REPLY_ACTION_SVC_CANCEL, "📝 درخواست لغو سرویس"),
        (REPLY_ACTION_SVC_RENEW, _t(ui, "btn_renew")),
        (REPLY_ACTION_SVC_ADDON, _t(ui, "btn_svc_addon") or "➕ حجم / زمان"),
        (REPLY_ACTION_SVC_AUTO, "⚙️ تنظیمات خودکار"),
        (REPLY_ACTION_SVC_REFRESH, "♻️ رفرش وضعیت"),
        (REPLY_ACTION_SVC_DELETE, "🗑 حذف سرویس"),
    ]


def service_actions_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(_service_action_entries(ui), ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="عملیات سرویس…")

def _review_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    return [
        (REPLY_ACTION_REV_OK, BTN_REV_OK),
        (REPLY_ACTION_REV_NO, BTN_REV_NO),
    ]


def review_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Approve / reject on reply KB (entity already selected via FSM)."""
    rows = _pack_reply_rows(_review_submenu_entries(ui), ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="تأیید یا رد…")

def cancel_reply(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Thin cancel keyboard during text input — does not include home/restart."""
    label = (_t(ui, "btn_cancel") or "").strip()
    cancel_label = BTN_CANCEL
    if label and "انصراف" in label and label != BTN_CANCEL:
        cancel_label = BTN_CANCEL
    return _reply_markup(
        [[_kb(cancel_label, style=_style(ui, "cancel", fallback="danger"), ui=ui)]],
        placeholder="مقدار را بفرستید یا انصراف بزنید…",
    )


def persistent_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Fallback reply keyboard (home only) when role context is unavailable."""
    home = _home_label(ui)
    return _reply_markup([[_kb(home, action=REPLY_ACTION_HOME, ui=ui)]])
